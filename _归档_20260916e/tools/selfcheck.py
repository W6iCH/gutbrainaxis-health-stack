#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck.py — 系统自检引擎（可被 CLI 与管理控制台共用）
=============================================================================
逐项验证
--------
1. 平台与环境（OS / 架构 / Python / venv）
2. 配置齐备性（app.yaml 存在、schema 校验通过、无占位符残留）
3. 目录与权限（服务目录、数据目录、日志目录可写）
4. systemd 单元（是否安装 / enabled / active）
5. systemd timer（是否 enabled / active / 下次触发时间）
6. 端口监听
7. HTTP 探活（/healthz → /health → /ping → /）
8. 数据库：可读、可写（回滚测试）、schema 完整
9. Nginx：`nginx -t`、站点已启用
10. 外部依赖可达性：问卷平台 API / SMTP / LLM（**串行 + 间隔限流**）
11. 磁盘余量、人口基线数据、模拟数据残留

输出
----
* `result.json`（结构化）：{ok, summary, items:[{id, group, status, title,
  detail, advice, duration_ms}], meta}
* 人类可读表格（stdout）
* **退出码**：0=无失败；1=有失败；2=致命（无法加载配置）

状态语义
--------
* `ok`   通过
* `fail` 失败（需要处置）
* `skip` 跳过（环境不支持 / 显式关闭 / 依赖缺失），带原因，不计入失败

用法
----
    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY tools/selfcheck.py                          # 全部检查
    $PY tools/selfcheck.py --json                   # 机器可读
    $PY tools/selfcheck.py --only config,systemd    # 只跑指定组
    $PY tools/selfcheck.py --skip external          # 跳过某组
    $PY tools/selfcheck.py --no-external            # 不探测外部依赖
    $PY tools/selfcheck.py --report out.json        # 另存 JSON

控制台复用：`services/admin_console/app.py` 的 `/api/selfcheck` 直接
`import selfcheck` 调用 `run_all()`，**同一后端、同一结果结构**。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import appconfig as AC                                   # noqa: E402

DEFAULT_UNITS_SERVICE = [
    "research-survey-feedback", "research-diet-feedback", "research-diet-webhook",
    "research-diet-llm-queue", "research-admin-console", "research-data-dashboard",
]
DEFAULT_UNITS_TIMER = [
    "research-survey-sync.timer", "research-diet-sync.timer",
    "research-exercise-sync.timer", "research-health-monitor.timer",
    "research-rclone-backup.timer", "research-daily-report.timer",
    "research-microbiome-import.timer",
]
DEFAULT_TIMER_SERVICE = [
    "research-survey-sync", "research-diet-sync", "research-exercise-sync",
    "research-health-monitor", "research-rclone-backup",
    "research-daily-report", "research-microbiome-import",
]

# 各数据库必须存在的表（schema 完整性）
REQUIRED_SCHEMA = {
    "survey_db": ["submissions", "email_log"],
    "diet_db": ["submissions"],
    "exercise_db": ["submissions"],
    "microbiome_db": ["omics_samples", "omics_measurements", "omics_import_log"],
}

GROUPS = ["platform", "config", "paths", "systemd", "ports", "http",
          "database", "nginx", "external", "storage"]


# ── 结果收集 ──────────────────────────────────────────────────────────────

class Results:
    def __init__(self):
        self.items = []
        self.t0 = time.time()

    def add(self, cid, group, status, title, detail="", advice="", duration_ms=None):
        self.items.append({
            "id": cid, "group": group, "status": status, "title": title,
            "detail": str(detail), "advice": advice,
            "duration_ms": duration_ms,
        })

    def ok(self, cid, group, title, detail="", advice=""):
        self.add(cid, group, "ok", title, detail, advice)

    def fail(self, cid, group, title, detail="", advice=""):
        self.add(cid, group, "fail", title, detail, advice)

    def skip(self, cid, group, title, detail="", advice=""):
        self.add(cid, group, "skip", title, detail, advice)

    def counts(self):
        c = {"ok": 0, "fail": 0, "skip": 0}
        for it in self.items:
            c[it["status"]] = c.get(it["status"], 0) + 1
        return c


# ── 探测辅助 ──────────────────────────────────────────────────────────────

def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 126, str(e)


def _has(cmd):
    return shutil.which(cmd) is not None


def _systemd_available():
    if platform.system() != "Linux":
        return False
    if not _has("systemctl"):
        return False
    rc, _ = _run(["systemctl", "--version"], timeout=5)
    return rc == 0


def _port_open(host, port, timeout=2.0):
    if not port or port <= 0:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _tcp_reachable(host, port, timeout=4.0):
    return _port_open(host, port, timeout)


def _http_probe(host, port, timeout=5):
    """依次尝试 /healthz /health /ping /，返回 (ok, detail)。"""
    for path in ("/healthz", "/health", "/ping", "/"):
        url = f"http://{host}:{port}{path}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
            if 200 <= code < 400:
                return True, f"{path} → HTTP {code}"
            last = f"{path} → HTTP {code}"
        except urllib.error.HTTPError as e:
            last = f"{path} → HTTP {e.code}"
            if e.code in (401, 403):        # 有响应即视为存活（如控制台需登录）
                return True, f"{path} → HTTP {e.code}（需鉴权，视为存活）"
        except Exception as e:              # noqa: BLE001
            last = f"{path} → {type(e).__name__}: {e}"
    return False, last or "无响应"


def _tcp_host_port_from_url(url):
    m = __import__("re").match(r"^https?://([^/:]+)(?::(\d+))?", str(url or ""))
    if not m:
        return None, None
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else (443 if url.startswith("https") else 80)
    return host, port


# ── 各组检查 ──────────────────────────────────────────────────────────────

def check_platform(R, cfg):
    R.ok("platform.os", "platform", "操作系统",
         f"{platform.system()} {platform.release()} ({platform.machine()})")
    if platform.system() == "Linux":
        if os.path.exists("/etc/os-release"):
            info = {}
            for line in open("/etc/os-release", encoding="utf-8"):
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    info[k] = v.strip('"')
            osid, ver = info.get("ID", ""), info.get("VERSION_ID", "")
            if osid == "ubuntu" and ver in ("22.04", "24.04"):
                R.ok("platform.distro", "platform", "发行版支持", f"{osid} {ver}")
            else:
                R.fail("platform.distro", "platform", "发行版支持",
                       f"{osid} {ver}", "安装脚本仅支持 Ubuntu 22.04 / 24.04")
        else:
            R.skip("platform.distro", "platform", "发行版支持", "/etc/os-release 不存在")
    else:
        R.skip("platform.distro", "platform", "发行版支持（仅 Linux）",
               f"当前为 {platform.system()}，跳过 Ubuntu 检查")

    R.ok("platform.python", "platform", "Python 版本",
         f"{platform.python_version()} ({os.path.basename(sys.executable)})")
    venv = os.environ.get("VENV_DIR") or os.path.join(cfg.get("app.base_dir", ""), "venv")
    if venv and os.path.exists(os.path.join(venv, "bin", "python3")):
        R.ok("platform.venv", "platform", "虚拟环境", venv)
    else:
        R.skip("platform.venv", "platform", "虚拟环境",
               f"未找到 {venv}/bin/python3（本地开发环境可忽略）")


def check_config(R, cfg, cfg_path):
    if cfg is None:
        R.fail("config.load", "config", "配置加载",
               f"{cfg_path} 缺失或解析失败",
               f"复制 {os.path.join('config', 'app.yaml.example')} 到 {cfg_path} 后填写")
        return
    R.ok("config.load", "config", "配置加载", cfg_path)

    raw, errs = AC.try_load(cfg_path)
    if raw is None:
        for e in errs:
            R.fail("config.schema", "config", "配置 schema 校验", e,
                   "修正 app.yaml 中该键的值后重新校验")
    else:
        R.ok("config.schema", "config", "配置 schema 校验",
             f"通过（{len([k for k in raw if not k.startswith('_')])} 键）")

    warns = (cfg or {}).get("_warnings") or []
    placeholders = [w for w in warns if "占位符" in w]
    if placeholders:
        for w in placeholders[:5]:
            R.fail("config.placeholder", "config", "占位符已替换", w,
                   "填入真实值；密钥写入 /etc/research-app/secrets.env（0600）")
    else:
        R.ok("config.placeholder", "config", "占位符已替换", "未发现占位符残留")
    secret_warns = [w for w in warns if "密钥未提供" in w]
    if secret_warns:
        R.skip("config.secrets", "config", "密钥齐备",
               f"{len(secret_warns)} 项密钥未提供：" +
               "; ".join(w.split(":")[0] for w in secret_warns[:4]),
               "写入 /etc/research-app/secrets.env 后重启受影响服务")
    else:
        R.ok("config.secrets", "config", "密钥齐备", "全部密钥已提供")


def check_paths(R, cfg):
    base = cfg.get("app.base_dir", "")
    if not base:
        R.fail("paths.base_missing", "paths", "安装根目录",
               "app.base_dir 未配置或配置加载失败",
               "修正 app.yaml；键位置 app.base_dir")
    for key, cid, title in (("app.base_dir", "paths.base", "安装根目录"),
                            ("app.log_dir", "paths.log", "日志目录"),
                            ("app.data_dir", "paths.data", "数据目录"),
                            ("backup.dir", "paths.backup", "备份目录")):
        p = cfg.get(key, "")
        if not p:
            R.fail(cid, "paths", title, "未配置", f"在 app.yaml 设置 {key}")
            continue
        if not os.path.exists(p):
            R.skip(cid, "paths", title, f"{p} 不存在",
                   "首次安装前属正常；安装后仍缺失请运行 install.sh")
            continue
        if key in ("app.log_dir", "app.data_dir", "backup.dir", "app.base_dir"):
            if os.access(p, os.W_OK):
                R.ok(cid, "paths", title, f"{p}（可写）")
            else:
                R.fail(cid, "paths", title, f"{p} 不可写",
                       f"chown/chmod 修正 {p} 的属主与权限")
        else:
            R.ok(cid, "paths", title, p)

    for svcdir in ("sjtu_survey_pro", "diet_survey", "exercise_survey",
                   "data_dashboard", "admin_console"):
        if not base:
            break
        p = os.path.join(base, svcdir)
        if os.path.isdir(p):
            R.ok(f"paths.svc.{svcdir}", "paths", f"服务目录 {svcdir}/", p)
        else:
            R.fail(f"paths.svc.{svcdir}", "paths", f"服务目录 {svcdir}/", "缺失",
                   "重新运行 install.sh（会复制 services/*）")

    for rel in ("scripts/health_monitor.py", "scripts/backup.sh", "tools/selfcheck.py"):
        if not base:
            break
        p = os.path.join(base, rel)
        if os.path.exists(p):
            R.ok(f"paths.tool.{rel}", "paths", f"运行期脚本 {rel}", p)
        else:
            R.fail(f"paths.tool.{rel}", "paths", f"运行期脚本 {rel}", "缺失",
                   "install.sh 需复制 scripts/ 与 tools/ 到安装根目录（见 变更清单）")

    pop = os.path.join(base, "sjtu_survey_pro", "population_stats.default.json") \
        if base else ""
    if not pop or not os.path.exists(pop):
        pop = os.path.join(PKG_DIR, "services", "sjtu_survey_pro",
                           "population_stats.default.json")
    if os.path.exists(pop):
        try:
            d = json.load(open(pop, encoding="utf-8"))
            n = len(d.get("by_version", {}).get(d.get("recommended_version", ""), {})
                    .get("scales", {}) or d.get("scales", {}))
            R.ok("paths.population", "paths", "人群基线默认数据",
                 f"{os.path.basename(pop)}（{n} 个量表）")
        except (OSError, ValueError) as e:
            R.fail("paths.population", "paths", "人群基线默认数据", f"解析失败: {e}",
                   "运行 python3 tools/build_population_stats.py 重新生成")
    else:
        R.fail("paths.population", "paths", "人群基线默认数据", "缺失",
               "运行 python3 tools/build_population_stats.py 重新生成")


def check_systemd(R, cfg):
    if not _systemd_available():
        R.skip("systemd.available", "systemd", "systemd 可用性",
               f"{platform.system()} 无 systemd（本地开发环境属正常）",
               "生产环境（Ubuntu）应有 systemd")
        return
    R.ok("systemd.available", "systemd", "systemd 可用性", "systemctl 可用")

    for unit in DEFAULT_UNITS_SERVICE:
        rc, out = _run(["systemctl", "is-active", unit], timeout=5)
        active = out.strip()
        rc2, out2 = _run(["systemctl", "is-enabled", unit], timeout=5)
        enabled = out2.strip()
        exists = os.path.exists(f"/etc/systemd/system/{unit}.service") or \
            os.path.exists(f"/lib/systemd/system/{unit}.service")
        if not exists and enabled in ("", "not-found"):
            R.fail(f"systemd.svc.{unit}", "systemd", f"单元 {unit}",
                   "未安装", "运行 install.sh 安装 systemd 单元")
        elif active == "active":
            R.ok(f"systemd.svc.{unit}", "systemd", f"单元 {unit}",
                 f"active / {enabled or 'disabled'}")
        else:
            R.fail(f"systemd.svc.{unit}", "systemd", f"单元 {unit}", f"{active} / {enabled}",
                   f"systemctl status {unit} 查看日志；必要时 systemctl restart {unit}")

    for unit in DEFAULT_UNITS_TIMER:
        rc, out = _run(["systemctl", "is-enabled", unit], timeout=5)
        enabled = out.strip()
        rc2, out2 = _run(["systemctl", "is-active", unit], timeout=5)
        active = out2.strip()
        if enabled == "enabled" and active == "active":
            rc3, out3 = _run(["systemctl", "list-timers", unit, "--no-legend",
                              "--no-pager"], timeout=5)
            nxt = out3.split()[0:2] if out3.strip() else []
            R.ok(f"systemd.timer.{unit}", "systemd", f"定时器 {unit}",
                 "enabled/active" + (f"，下次 {nxt[0]} {nxt[1]}" if nxt else ""))
        else:
            R.fail(f"systemd.timer.{unit}", "systemd", f"定时器 {unit}",
                   f"{enabled or 'not-found'} / {active or 'unknown'}",
                   f"systemctl enable --now {unit}")


def check_ports(R, cfg):
    host = "127.0.0.1"
    any_port = False
    for svc, info in (cfg.get("services") or {}).items():
        if not isinstance(info, dict):
            continue
        port = info.get("port") or 0
        if port <= 0 or not info.get("enabled", True):
            continue
        any_port = True
        title = f"端口 {port}（{svc}）"
        open_ = _port_open(host, port)
        if open_:
            R.ok(f"ports.{svc}", "ports", title, "LISTEN")
        else:
            R.fail(f"ports.{svc}", "ports", title, "未监听",
                   f"systemctl status 对应单元；或 bash run_all.sh 手动起")
    if not any_port:
        R.skip("ports.none", "ports", "端口监听", "配置中无启用端口的服务")


def check_http(R, cfg):
    timeout = int(cfg.get("selfcheck.http_timeout_seconds", 5))
    for svc, info in (cfg.get("services") or {}).items():
        if not isinstance(info, dict) or not info.get("enabled", True):
            continue
        port = info.get("port") or 0
        if port <= 0:
            continue
        ok, detail = _http_probe("127.0.0.1", port, timeout)
        if ok:
            R.ok(f"http.{svc}", "http", f"HTTP 探活 {svc}", detail)
        else:
            R.fail(f"http.{svc}", "http", f"HTTP 探活 {svc}", detail,
                   f"curl -v http://127.0.0.1:{port}/healthz 定位；查看 journalctl -u 对应单元")


def check_database(R, cfg):
    write_test = bool(cfg.get("selfcheck.db_write_test", True))
    for key, cid in (("survey_db", "survey"), ("diet_db", "diet"),
                     ("exercise_db", "exercise"), ("microbiome_db", "microbiome")):
        path = cfg.get(f"database.{key}")
        if not path:
            R.fail(f"db.{cid}.path", "database", f"{key} 路径", "未配置",
                   f"在 app.yaml 设置 database.{key}")
            continue
        if not os.path.exists(path):
            R.fail(f"db.{cid}.exists", "database", f"{cid} 数据库存在",
                   f"{path} 缺失",
                   "运行 python3 scripts/init_db.py 创建空 schema")
            continue
        try:
            conn = sqlite3.connect(path, timeout=5)
            cur = conn.cursor()
            cur.execute("PRAGMA quick_check")
            integrity = cur.fetchone()[0]
            if integrity != "ok":
                R.fail(f"db.{cid}.integrity", "database", f"{cid} 完整性",
                       str(integrity), "从最近备份恢复该库")
                conn.close()
                continue
            tabs = {r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            need = REQUIRED_SCHEMA.get(key, [])
            missing = [t for t in need if t not in tabs]
            if missing:
                R.fail(f"db.{cid}.schema", "database", f"{cid} schema 完整",
                       f"缺表: {', '.join(missing)}",
                       "运行 python3 scripts/init_db.py 补建（不会删除已有数据）")
            else:
                R.ok(f"db.{cid}.schema", "database", f"{cid} schema 完整",
                     f"{len(tabs)} 张表（必需 {len(need)} 张齐全）")
            n = cur.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] \
                if "submissions" in tabs else 0
            R.ok(f"db.{cid}.read", "database", f"{cid} 可读",
                 f"submissions {n} 行")
            if write_test:
                try:
                    cur.execute("CREATE TEMP TABLE _selfcheck_probe(x INTEGER)")
                    cur.execute("INSERT INTO _selfcheck_probe VALUES (1)")
                    conn.rollback()
                    R.ok(f"db.{cid}.write", "database", f"{cid} 可写",
                         "临时表写入并在事务中回滚（无残留）")
                except sqlite3.Error as e:
                    R.fail(f"db.{cid}.write", "database", f"{cid} 可写", str(e),
                           "检查文件权限与磁盘空间")
            else:
                R.skip(f"db.{cid}.write", "database", f"{cid} 可写", "已在配置中关闭写测试")
            conn.close()
        except sqlite3.Error as e:
            R.fail(f"db.{cid}.read", "database", f"{cid} 可读", str(e),
                   "检查文件权限；必要时用 scripts/backup.sh 的备份恢复")


def check_nginx(R, cfg):
    if not _has("nginx"):
        R.skip("nginx.available", "nginx", "Nginx 可用性", "未安装 nginx",
               "apt-get install nginx（生产环境必需）")
        return
    rc, out = _run(["nginx", "-t"], timeout=10)
    if rc == 0:
        R.ok("nginx.syntax", "nginx", "nginx -t 配置语法", out.strip().splitlines()[-1]
             if out.strip() else "ok")
    else:
        R.fail("nginx.syntax", "nginx", "nginx -t 配置语法", out.strip()[-300:],
               "按报错行号修正 /etc/nginx/sites-available/ 下对应文件")
    enabled = cfg.get("site.nginx_enabled_dir", "/etc/nginx/sites-enabled")
    if os.path.isdir(enabled):
        links = [f for f in os.listdir(enabled) if f.endswith(".conf")]
        if links:
            R.ok("nginx.enabled", "nginx", "站点已启用", f"{len(links)} 个: {', '.join(links[:4])}")
        else:
            R.fail("nginx.enabled", "nginx", "站点已启用", f"{enabled} 下无 .conf",
                   "install.sh 会软链 nginx/*.conf 到 sites-enabled")
    else:
        R.skip("nginx.enabled", "nginx", "站点已启用", f"{enabled} 不存在")
    if _systemd_available():
        rc, out = _run(["systemctl", "is-active", "nginx"], timeout=5)
        if out.strip() == "active":
            R.ok("nginx.active", "nginx", "nginx 运行中", "active")
        else:
            R.fail("nginx.active", "nginx", "nginx 运行中", out.strip() or "unknown",
                   "systemctl restart nginx")
    else:
        R.skip("nginx.active", "nginx", "nginx 运行中", "无 systemd")


def check_external(R, cfg):
    if not cfg.get("selfcheck.external_probe", True):
        R.skip("external.disabled", "external", "外部依赖探测", "已在配置中关闭")
        return
    interval = float(cfg.get("selfcheck.external_probe_interval_seconds", 7))
    first = True

    def _throttle():
        nonlocal first
        if first:
            first = False
            return
        time.sleep(max(0.0, interval))

    # ① 问卷平台（三个 Token）—— 逐个、串行、带间隔，避免 429
    for pname, cid in (("scale", "survey"), ("diet", "diet"), ("exercise", "exercise")):
        token = cfg.get(f"survey_platforms.{pname}.token", "")
        if not token or "${" in str(token):
            R.skip(f"external.survey.{cid}", "external", f"问卷平台 {pname} Token",
                   "未配置", "在 app.yaml 或 secrets.env 填入 WKJX Token")
            continue
        _throttle()
        url = f"https://wj.sjtu.edu.cn/api/v1/public/result/{token}/json"
        t0 = time.time()
        try:
            req = urllib.request.Request(url, method="GET",
                                         headers={"User-Agent": "selfcheck/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read(2048)
            dt = int((time.time() - t0) * 1000)
            try:
                j = json.loads(body.decode("utf-8", "replace")[:2048]
                               .split("}")[0] + "}") if body else {}
            except Exception:                       # noqa: BLE001
                j = {}
            R.add(f"external.survey.{cid}", "external", "ok",
                  f"问卷平台 {pname} 可达",
                  f"HTTP {resp.status}（{dt}ms）", "", dt)
        except urllib.error.HTTPError as e:
            hint = "Token 可能失效或权限不足" if e.code in (401, 403, 404) else "稍后重试"
            R.fail(f"external.survey.{cid}", "external", f"问卷平台 {pname} 可达",
                   f"HTTP {e.code}", hint)
        except Exception as e:                       # noqa: BLE001
            R.fail(f"external.survey.{cid}", "external", f"问卷平台 {pname} 可达",
                   f"{type(e).__name__}: {e}", "检查出网与 DNS")

    # ② SMTP
    host = cfg.get("smtp.ssl_host") or cfg.get("smtp.host")
    port = cfg.get("smtp.ssl_port") if cfg.get("smtp.use_ssl") else cfg.get("smtp.port")
    if host and port:
        _throttle()
        if _tcp_reachable(host, int(port)):
            R.ok("external.smtp", "external", "SMTP 可达", f"{host}:{port} TCP 可连")
        else:
            R.fail("external.smtp", "external", "SMTP 可达", f"{host}:{port} 不可达",
                   "检查网络/防火墙；校内 SMTP 可能需在校内网或 VPN")

    # ③ LLM（主 + 备）
    for lname, cid in (("primary", "llm"), ("backup", "llm_backup")):
        base = cfg.get(f"llm.{lname}.base_url")
        if not base or (lname == "backup" and not cfg.get("llm.backup.enabled", True)):
            R.skip(f"external.{cid}", "external", f"LLM {lname} 可达",
                   "未配置或已关闭")
            continue
        bhost, bport = _tcp_host_port_from_url(base)
        if not bhost:
            R.fail(f"external.{cid}", "external", f"LLM {lname} 可达",
                   f"base_url 非法: {base}", "修正 llm.*.base_url")
            continue
        _throttle()
        if _tcp_reachable(bhost, bport):
            R.ok(f"external.{cid}", "external", f"LLM {lname} 可达",
                 f"{bhost}:{bport} TCP 可连")
        else:
            R.fail(f"external.{cid}", "external", f"LLM {lname} 可达",
                   f"{bhost}:{bport} 不可达", "校验 base_url/出网；备份通道用于主通道失败时")


def check_storage(R, cfg):
    min_gb = float(cfg.get("selfcheck.min_free_disk_gb", 2))
    for key, cid, title in (("app.log_dir", "disk.log", "日志盘余量"),
                            ("backup.dir", "disk.backup", "备份盘余量")):
        p = cfg.get(key)
        if not p or not os.path.exists(p):
            R.skip(f"storage.{cid}", "storage", title, f"{p} 不存在")
            continue
        try:
            u = shutil.disk_usage(p)
            free_gb = u.free / (1024 ** 3)
            detail = f"{p} 剩余 {free_gb:.1f} GB / 共 {u.total / (1024 ** 3):.1f} GB"
            if free_gb < min_gb:
                R.fail(f"storage.{cid}", "storage", title, detail,
                       f"清理日志/旧备份（scripts/backup.sh 保留策略），阈值 {min_gb} GB")
            else:
                R.ok(f"storage.{cid}", "storage", title, detail)
        except OSError as e:
            R.skip(f"storage.{cid}", "storage", title, f"无法读取: {e}")

    base = cfg.get("app.base_dir", "")
    mock_markers = []
    for rel, marker in (("sjtu_survey_pro/survey_data.db", "MOCK-"),
                        ("diet_survey/diet_data.db", "MOCK-"),
                        ("exercise_survey/exercise_data.db", "MOCK-")):
        p = os.path.join(base, rel) if base else ""
        if not p or not os.path.exists(p):
            continue
        try:
            conn = sqlite3.connect(p, timeout=5)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM submissions WHERE COALESCE(student_id,'') LIKE ?",
                    (marker + "%",)).fetchone()[0]
            except sqlite3.Error:
                n = 0
            conn.close()
            if n:
                mock_markers.append(f"{rel}: {n}")
        except sqlite3.Error:
            pass
    if mock_markers:
        R.fail("storage.mock_residue", "storage", "模拟数据残留",
               "; ".join(mock_markers),
               "运行 python3 tools/clear_mock_data.py 清除后重新自检")
    else:
        R.ok("storage.mock_residue", "storage", "模拟数据残留", "未发现 MOCK- 前缀记录")

    # 日志轮转配置
    lr_glob = ["/etc/logrotate.d/research-app"]
    if any(os.path.exists(p) for p in lr_glob):
        R.ok("storage.logrotate", "storage", "日志轮转配置", "/etc/logrotate.d/research-app")
    else:
        R.skip("storage.logrotate", "storage", "日志轮转配置",
               "未安装", "install.sh 会安装 config/logrotate.research-app")


# ── 主入口 ────────────────────────────────────────────────────────────────

def run_all(cfg=None, cfg_path=None, only=None, skip=None, external=None) -> dict:
    """
    运行全部（或指定的）检查，返回结构化结果 dict。
    可被控制台直接调用：`import selfcheck; selfcheck.run_all()`。
    """
    cfg_path = cfg_path or AC.DEFAULT_CONFIG
    load_errors = []
    if cfg is None:
        # 两阶段：先严格加载；失败则非严格加载（保留可用的值），
        # 让每项检查仍能给出具体结论，而不是全部报「未配置」。
        cfg, errs = AC.try_load(cfg_path)
        if cfg is None:
            load_errors = list(errs)
            cfg, errs2 = AC.try_load(cfg_path, strict_placeholders=False)
            if cfg is None:
                cfg = {}
            else:
                cfg["_load_errors"] = load_errors

    only = set(only or [])
    skip = set(skip or [])
    if external is False:
        skip.add("external")

    R = Results()

    def want(g):
        return (not only or g in only) and g not in skip

    if want("platform"):
        check_platform(R, cfg)
    if want("config"):
        check_config(R, cfg, cfg_path)
    if want("paths"):
        check_paths(R, cfg)
    if want("systemd"):
        check_systemd(R, cfg)
    if want("ports"):
        check_ports(R, cfg)
    if want("http"):
        check_http(R, cfg)
    if want("database"):
        check_database(R, cfg)
    if want("nginx"):
        check_nginx(R, cfg)
    if want("external"):
        check_external(R, cfg)
    if want("storage"):
        check_storage(R, cfg)

    c = R.counts()
    return {
        "ok": c.get("fail", 0) == 0,
        "summary": {"ok": c.get("ok", 0), "fail": c.get("fail", 0),
                    "skip": c.get("skip", 0), "total": len(R.items)},
        "meta": {
            "config": cfg_path,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "host": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "duration_ms": int((time.time() - R.t0) * 1000),
            "groups_run": sorted((only or set(GROUPS)) - skip),
        },
        "items": R.items,
    }


ICON = {"ok": "✅", "fail": "❌", "skip": "⏭️"}


def print_table(result: dict, show_skip=True):
    print(f"\n自检 @ {result['meta']['generated_at']}  主机 {result['meta']['host']}")
    print(f"配置 {result['meta']['config']}")
    cur = None
    for it in result["items"]:
        if it["group"] != cur:
            cur = it["group"]
            print(f"\n━━━ {cur} ━━━")
        if it["status"] == "skip" and not show_skip:
            continue
        line = f"  {ICON.get(it['status'], '?')} {it['title']}"
        if it["detail"]:
            line += f": {it['detail']}"
        print(line)
        if it["status"] == "fail" and it["advice"]:
            print(f"      💡 {it['advice']}")
    s = result["summary"]
    print("\n" + "─" * 52)
    print(f"  通过 {s['ok']}  |  失败 {s['fail']}  |  跳过 {s['skip']}"
          f"  |  共 {s['total']}  （{result['meta']['duration_ms']} ms）")
    print("─" * 52)
    print("✅ 自检全部通过" if result["ok"] else "❌ 自检存在失败项，请按 💡 处置")


def main() -> int:
    ap = argparse.ArgumentParser(description="系统自检（结构化 JSON + 人类可读表）")
    ap.add_argument("--config", default=AC.DEFAULT_CONFIG)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--report", metavar="PATH", help="另存 JSON 报告")
    ap.add_argument("--only", default="", help="只跑指定组（逗号分隔）")
    ap.add_argument("--skip", default="", help="跳过指定组（逗号分隔）")
    ap.add_argument("--no-external", action="store_true", help="不探测外部依赖")
    ap.add_argument("--no-skip-lines", action="store_true", help="表中不显示跳过项")
    args = ap.parse_args()

    only = [g for g in args.only.split(",") if g.strip()]
    skip = [g for g in args.skip.split(",") if g.strip()]
    for g in only + skip:
        if g not in GROUPS:
            print(f"❌ 未知检查组: {g}（可选: {', '.join(GROUPS)}）", file=sys.stderr)
            return 2

    cfg, errs = AC.try_load(args.config)
    if cfg is None and not os.path.exists(args.config):
        print(f"⚠️  配置 {args.config} 不存在，按默认值继续自检（不会修改系统）",
              file=sys.stderr)
    result = run_all(cfg=cfg, cfg_path=args.config, only=only, skip=skip,
                     external=(False if args.no_external else None))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_table(result, show_skip=not args.no_skip_lines)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.report}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
