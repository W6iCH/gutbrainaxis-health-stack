#!/usr/bin/env python3
"""
Admin Console v6 — 可发布的管理控制台
功能：配置管理·进程开关·端口映射·自检面板·安全鉴权·审计日志
"""
import os
import json
import subprocess
import shutil
import socket
import sys
import time
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, request, jsonify, session, redirect, url_for,
    render_template_string, send_from_directory, Response, g
)

from config_manager import ConfigManager
from auth import AuthManager
from service_manager import ServiceManager, SERVICE_WHITELIST
from port_manager import PortManager, PortConflictError
from health_check import HealthChecker
from audit import AuditLogger

# ── 基准路径 ──────────────────────────────────────────────────────────
# 可移植：基准目录来自环境变量，默认取脚本所在目录
BASE_DIR = Path(__file__).resolve().parent
APP_BASE = Path(os.environ.get("APP_BASE", "/opt"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(BASE_DIR / "config")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

for d in (CONFIG_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── 初始化模块 ────────────────────────────────────────────────────────
config_mgr = ConfigManager(str(CONFIG_DIR))
auth_mgr = AuthManager(str(CONFIG_DIR))
svc_mgr = ServiceManager(str(APP_BASE))
port_mgr = PortManager(
    str(Path(os.environ.get("NGINX_SITES_DIR", "/etc/nginx/sites-available"))),
    str(Path(os.environ.get("NGINX_ENABLED_DIR", "/etc/nginx/sites-enabled"))),
    str(APP_BASE),
)
health = HealthChecker(str(APP_BASE))
audit_logger = AuditLogger(str(LOG_DIR))

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ── 安全：签名 Cookie / CSRF ─────────────────────────────────────────
def _get_secret_key() -> str:
    key = config_mgr.get("SECRET_KEY")
    if not key:
        key = secrets.token_hex(32)
        config_mgr.save({"SECRET_KEY": key})
    return key

app.secret_key = _get_secret_key()

# ── 首次运行引导：生成随机初始口令 ────────────────────────────────
# 安全要求：首次安装生成随机初始口令，写入 config/initial_password.txt（0600），
# 并强制首次登录后立即改密；改密成功后删除该文件。
INIT_PW_FILE = CONFIG_DIR / "initial_password.txt"
MUST_CHANGE_FLAG = CONFIG_DIR / ".must_change_password"


def bootstrap_admin_password() -> None:
    """未设置管理员口令时，生成一个随机初始口令并落盘（权限 600）。"""
    if auth_mgr.is_installed():
        return
    initial = secrets.token_urlsafe(12)
    ok, _msg = auth_mgr.install(initial)
    if not ok:
        return
    try:
        INIT_PW_FILE.write_text(
            "# 管理控制台初始口令\n"
            "# 首次登录后请立即修改口令；改密成功后本文件会被自动删除。\n"
            f"USERNAME=admin\n"
            f"PASSWORD={initial}\n",
            encoding="utf-8",
        )
        INIT_PW_FILE.chmod(0o600)
        MUST_CHANGE_FLAG.write_text("1", encoding="utf-8")
        MUST_CHANGE_FLAG.chmod(0o600)
    except OSError:
        pass
    print("\n" + "=" * 62)
    print("  首次安装：已生成管理控制台初始口令")
    print(f"  文件: {INIT_PW_FILE}  (权限 600)")
    print("  请登录后立即修改口令。")
    print("=" * 62 + "\n")


bootstrap_admin_password()


def must_change_password() -> bool:
    return MUST_CHANGE_FLAG.exists()


# 请求上下文注入当前用户
@app.before_request
def before_request():
    g.user = session.get("user")
    g.ip = request.remote_addr or "unknown"
    if not request.path.startswith("/api/"):
        return None
    if request.path in ("/api/login", "/api/auth", "/api/health", "/api/setup"):
        return None
    if not g.user:
        return jsonify({"error": "未授权", "code": "UNAUTHORIZED"}), 401
    # 强制首次登录改密：未改密前只放行改密/登出/状态查询
    if must_change_password() and request.path not in (
            "/api/change_password", "/api/logout", "/api/login_status"):
        return jsonify({"error": "首次登录必须修改口令", "code": "MUST_CHANGE_PASSWORD"}), 403
    return None

# ── 核心 HTML ────────────────────────────────────────────────────────

def render_app(title: str = "管理控制台") -> str:
    """渲染 SPA 外壳"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<div id="app"></div>
<script src="/static/app.js"></script>
</body>
</html>"""

@app.route("/")
def index():
    return render_app()

@app.route("/login")
def login_page():
    return render_app("登录 — 管理控制台")

# ── 静态资源 ─────────────────────────────────────────────────────────
@app.route("/static/<path:filename>")
def static_files(filename: str):
    static_dir = BASE_DIR / "static"
    return send_from_directory(str(static_dir), filename)

# ── 认证路由 ─────────────────────────────────────────────────────────
@app.route("/api/auth", methods=["GET", "POST"])
def api_auth():
    if request.method == "GET":
        return jsonify({"authed": bool(session.get("authed")), "user": session.get("user")})

    data = request.get_json() or {}
    password = data.get("password", "")
    if auth_mgr.verify(password):
        session["authed"] = True
        session["user"] = "admin"
        audit_logger.log("login", "admin", "success", "管理员登录")
        return jsonify({"success": True, "user": "admin",
                        "must_change_password": must_change_password()})
    audit_logger.log("login", f"ip:{g.ip}", "failure", "口令错误")
    return jsonify({"error": "口令错误"}), 401


@app.route("/api/setup", methods=["GET", "POST"])
@app.route("/api/install", methods=["GET", "POST"])
def api_setup():
    """首次安装：设置管理员初始口令（仅在尚未安装时可用）。"""
    if request.method == "GET":
        return jsonify({"installed": auth_mgr.is_installed(),
                        "must_change_password": must_change_password()})
    if auth_mgr.is_installed():
        return jsonify({"error": "管理员口令已安装，请直接登录"}), 409
    data = request.get_json() or {}
    password = data.get("password", "")
    ok, msg = auth_mgr.install(password)
    if not ok:
        audit_logger.log("setup", f"ip:{g.ip}", "failure", msg)
        return jsonify({"error": msg}), 400
    try:
        INIT_PW_FILE.unlink(missing_ok=True)
        MUST_CHANGE_FLAG.write_text("1", encoding="utf-8")
    except OSError:
        pass
    audit_logger.log("setup", f"ip:{g.ip}", "success", "管理员口令已初始化")
    return jsonify({"success": True, "must_change_password": True})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    audit_logger.log("logout", g.user or "admin", "success", "登出")
    return jsonify({"success": True})


@app.route("/api/change_password", methods=["POST"])
def api_change_password():
    data = request.get_json() or {}
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if len(new_pw) < 8:
        return jsonify({"error": "新口令至少 8 个字符"}), 400
    ok, msg = auth_mgr.change_password(old_pw, new_pw)
    if not ok:
        audit_logger.log("password_change", g.user, "failure", msg)
        return jsonify({"error": msg}), 401
    # 改密成功：清除初始口令文件与强制改密标记
    try:
        INIT_PW_FILE.unlink(missing_ok=True)
        MUST_CHANGE_FLAG.unlink(missing_ok=True)
    except OSError:
        pass
    audit_logger.log("password_change", g.user, "success", "口令已更新")
    return jsonify({"success": True, "message": "口令已更新，请重新登录"})


@app.route("/api/login_status")
def api_login_status():
    return jsonify({"authed": bool(session.get("authed")), "user": session.get("user"),
                    "must_change_password": must_change_password()})

# ── 配置管理路由 ─────────────────────────────────────────────────────
@app.route("/api/config")
def api_get_config():
    cfg = config_mgr.load()
    cfg.pop("SECRET_KEY", None)
    cfg.pop("SMTP_PASSWORD", None)
    return jsonify({"data": cfg})

@app.route("/api/config", methods=["POST", "PUT"])
def api_save_config():
    if not session.get("authed"):
        return jsonify({"error": "未授权"}), 401
    data = request.get_json()
    before = config_mgr.load()
    result = config_mgr.save(data)
    if not result["success"]:
        return jsonify(result), 400
    after = config_mgr.load()
    # 变更摘要
    changed = [k for k in data.keys() if before.get(k, "") != after.get(k, "")]
    audit_logger.log("config_change", g.user, "success",
                     f"修改配置: {', '.join(changed) or '未知项'}",
                     f"before={json.dumps({k: before.get(k,'') for k in changed}, ensure_ascii=False)}",
                     f"after={json.dumps({k: after.get(k,'') for k in changed}, ensure_ascii=False)}")
    return jsonify(result)

@app.route("/api/config/restore", methods=["POST"])
def api_restore_config():
    if not session.get("authed"):
        return jsonify({"error": "未授权"}), 401
    cfg = config_mgr.restore_defaults()
    audit_logger.log("config_restore", g.user, "success", "恢复默认配置")
    return jsonify({"success": True, "data": cfg})

# ── 服务进程路由 ─────────────────────────────────────────────────────
@app.route("/api/processes")
def api_processes():
    results = svc_mgr.all_status()
    return jsonify(results)

@app.route("/api/processes/<service>/<action>", methods=["POST"])
def api_process_action(service: str, action: str):
    if not session.get("authed"):
        return jsonify({"error": "未授权"}), 401
    if service not in SERVICE_WHITELIST:
        return jsonify({"error": f"未知服务: {service}"}), 400
    if action not in ("start", "stop", "restart", "enable", "disable"):
        return jsonify({"error": f"未知操作: {action}"}), 400
    result = svc_mgr.act(service, action)
    label = SERVICE_WHITELIST[service]["label"]
    if result.get("success"):
        audit_logger.log("process_" + action, g.user, "success", f"{label} {action}")
    else:
        audit_logger.log("process_" + action, g.user, "failure", f"{label} {action}: {result.get('error','')}")
    return jsonify(result)

# ── 端口映射 + Nginx 路由 ────────────────────────────────────────────
@app.route("/api/ports")
def api_get_ports():
    mappings = port_mgr.get_mappings()
    statuses = {}
    for name, m in mappings.items():
        if m.get("port", -1) > 0:
            r = health.probe_service(name, m["port"])
            statuses[name] = {"port_open": r["port_open"], "http_status": r["http_status"]}
        else:
            statuses[name] = {"port_open": False, "http_status": None}
    return jsonify({"mappings": mappings, "statuses": statuses})

@app.route("/api/ports/preview", methods=["POST"])
def api_preview_ports():
    if not session.get("authed"):
        return jsonify({"error": "未授权"}), 401
    data = request.get_json() or {}
    mappings = data.get("mappings", {})
    gen = port_mgr.generate_config(mappings, preview=True)
    return jsonify(gen)

@app.route("/api/ports/apply", methods=["POST"])
def api_apply_ports():
    if not session.get("authed"):
        return jsonify({"error": "未授权"}), 401
    data = request.get_json() or {}
    mappings = data.get("mappings", {})
    result = port_mgr.apply_and_reload(mappings)
    if result.get("success"):
        audit_logger.log("port_change", g.user, "success", "端口映射已更新", result.get("message", ""))
    else:
        audit_logger.log("port_change", g.user, "failure", f"{result.get('message','')}: {result.get('errors',[])}")
    return jsonify(result)

# ── 自检面板路由 ─────────────────────────────────────────────────────
# ── 自检（复用 tools/selfcheck.py 的同一后端）─────────────────────────
# 设计要点：控制台**不再另写一套探活逻辑**，而是直接 import 与 CLI
# 完全相同的 `tools/selfcheck.py`，保证「页面看到的」与「CI 跑的」一致。

def _load_selfcheck():
    """按 APP_BASE/tools 与包内 tools 两个位置尝试加载 selfcheck 后端。"""
    for cand in (Path(APP_BASE) / "tools", BASE_DIR.parent / "tools"):
        if (cand / "selfcheck.py").exists():
            p = str(cand)
            if p not in sys.path:
                sys.path.insert(0, p)
            import importlib
            import selfcheck as _sc
            importlib.reload(_sc)
            return _sc
    return None


def _selfcheck_kwargs():
    only = [g for g in (request.args.get("only") or "").split(",") if g.strip()]
    skip = [g for g in (request.args.get("skip") or "").split(",") if g.strip()]
    external = request.args.get("external")
    cfg_path = "/etc/research-app/app.yaml"
    try:
        import appconfig as _ac
        cfg, _errs = _ac.try_load(cfg_path)
    except Exception:                             # noqa: BLE001
        cfg = None
    return {"cfg": cfg, "cfg_path": cfg_path, "only": only, "skip": skip,
            "external": (False if external == "0" else None)}


@app.route("/api/selfcheck")
def api_selfcheck():
    """GET /api/selfcheck → 结构化自检结果（与 tools/selfcheck.py --json 一致）。"""
    sc = _load_selfcheck()
    if sc is None:
        return jsonify({"ok": False,
                        "error": f"selfcheck.py 未安装到 {APP_BASE}/tools/"}), 500
    return jsonify(sc.run_all(**_selfcheck_kwargs()))


@app.route("/api/selfcheck/run", methods=["POST"])
def api_selfcheck_run():
    """POST /api/selfcheck/run → 运行自检并记审计。"""
    try:
        audit_logger.log(session.get("user", "?"), "selfcheck.run", "-", "自检")
    except Exception:                             # noqa: BLE001
        pass
    return api_selfcheck()


@app.route("/selfcheck")
def selfcheck_page():
    """服务端渲染的自检页（与 /api/selfcheck 同一后端、同一结果）。"""
    sc = _load_selfcheck()
    if sc is None:
        return f"<h3>自检后端未安装</h3><p>请确认 {APP_BASE}/tools/selfcheck.py 存在。</p>", 500
    result = sc.run_all(**_selfcheck_kwargs())
    icon = {"ok": "✅", "fail": "❌", "skip": "⏭️"}
    rows, cur = [], None
    for it in result["items"]:
        if it["group"] != cur:
            cur = it["group"]
            rows.append(f'<tr class="grp"><td colspan="3">{cur}</td></tr>')
        advice = (f'<div class="advice">💡 {it["advice"]}</div>'
                  if it["status"] == "fail" and it["advice"] else "")
        rows.append(f'<tr class="{it["status"]}"><td>{icon.get(it["status"],"?")}</td>'
                    f'<td>{it["title"]}{advice}</td><td>{it["detail"]}</td></tr>')
    s = result["summary"]
    banner = ("#e8f5e9;color:#1b5e20" if result["ok"] else "#ffebee;color:#b71c1c")
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>自检 — 管理控制台</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border-bottom:1px solid #eee;padding:6px 8px;vertical-align:top;text-align:left}}
tr.grp td{{background:#f5f7ff;font-weight:600;color:#444}}
tr.fail{{background:#fff5f5}} tr.skip{{color:#888}}
.advice{{font-size:12px;color:#b26a00;margin-top:2px}}
.banner{{padding:10px 14px;border-radius:8px;background:{banner};margin:12px 0}}
button{{padding:6px 14px;border-radius:6px;border:1px solid #ccc;background:#fff;cursor:pointer}}
code{{background:#f4f4f4;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h2>🩺 系统自检</h2>
<div class="banner">通过 <b>{s['ok']}</b> ｜ 失败 <b>{s['fail']}</b> ｜ 跳过 <b>{s['skip']}</b>
 ｜ 共 <b>{s['total']}</b> 项 ｜ 耗时 {result['meta']['duration_ms']} ms<br>
 主机 {result['meta']['host']} ｜ 配置 <code>{result['meta']['config']}</code></div>
<p><button onclick="location.reload()">重新自检</button>
 &nbsp;<code>GET /api/selfcheck?only=config</code>
 &nbsp;<code>POST /api/selfcheck/run</code>
 &nbsp;<a href="/">← 返回控制台</a></p>
<table><thead><tr><th></th><th>检查项</th><th>结果</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p style="color:#888;font-size:12px;margin-top:18px">与命令行
 <code>python3 tools/selfcheck.py</code> 使用同一后端；退出码 0=全部通过。</p>
</body></html>"""


@app.route("/api/health")
def api_health():
    services = health.probe_all()
    dbs = health.check_all_dbs()
    sys_info = {}
    try:
        with open("/proc/loadavg") as f:
            sys_info["loadavg"] = f.read().split()[:3]
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            lines = {}
            for line in f:
                if line.startswith(("MemTotal", "MemFree", "MemAvailable")):
                    k, _, v = line.partition(":")
                    lines[k] = v.strip()
            sys_info["memory"] = lines
    except Exception:
        pass
    return jsonify({"services": services, "databases": dbs, "system": sys_info})

@app.route("/api/logs/<log_key>")
def api_get_logs(log_key: str):
    result = health.read_log(log_key)
    return jsonify(result)

@app.route("/api/logs/list")
def api_log_list():
    keys = sorted(health.LOG_WHITELIST.keys())
    out = []
    for k in keys:
        rel, label = health.LOG_WHITELIST[k]
        full = Path(APP_BASE) / rel
        out.append({
            "key": k,
            "label": label,
            "path": str(full),
            "exists": full.exists(),
            "size_kb": round(full.stat().st_size / 1024, 1) if full.exists() else 0,
        })
    return jsonify({"logs": out})

# ── 审计日志路由 ─────────────────────────────────────────────────────
@app.route("/api/audit")
def api_audit_logs():
    limit = min(request.args.get("limit", 200, type=int), 500)
    entries = audit_logger.read(limit=limit)
    return jsonify({"entries": entries})

# ── 系统信息 ─────────────────────────────────────────────────────────
@app.route("/api/system")
def api_system_info():
    info = {"server_time": datetime.now().isoformat(),
            "dry_run": bool(os.environ.get("DRY_RUN") == "1")}
    try:
        with open("/proc/uptime") as f:
            info["uptime_seconds"] = float(f.read().split()[0])
    except Exception:
        pass
    try:
        info["hostname"] = socket.gethostname()
    except Exception:
        pass
    try:
        r = subprocess.run(["nproc"], capture_output=True, text=True, timeout=5)
        info["cpu_count"] = r.stdout.strip()
    except Exception:
        pass
    if DRY_RUN:
        info["mode"] = "DRY_RUN（模拟模式，不修改系统）"
    return jsonify(info)

# ── 健康探针 ─────────────────────────────────────────────────────────
@app.route("/healthz")
def healthz():
    """统一健康探针：/healthz（selftest 与反代统一使用本端点）。"""
    checks, ok = {}, True
    try:
        checks["config"] = {
            "file": str(CONFIG_DIR / "console.env"),
            "exists": (CONFIG_DIR / "console.env").exists(),
        }
        checks["audit_log"] = {
            "dir": str(LOG_DIR), "writable": os.access(str(LOG_DIR), os.W_OK),
        }
    except Exception as e:                        # noqa: BLE001
        checks["error"] = str(e)
        ok = False
    return jsonify({
        "status": "ok" if ok else "degraded",
        "service": "admin-console",
        "pid": os.getpid(),
        "checks": checks,
        "time": datetime.now().isoformat(timespec="seconds"),
    }), (200 if ok else 503)


@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# ── 启动 ─────────────────────────────────────────────────────────────
def main():
    port = int(config_mgr.get("CONSOLE_PORT") or "9000")
    host = config_mgr.get("CONSOLE_HOST") or "127.0.0.1"
    print(f"\n  ⚡ Admin Console v6")
    print(f"  ────────────────────────────")
    print(f"  URL:   http://{host}:{port}")
    print(f"  模式:  {'DRY_RUN（模拟）' if DRY_RUN else '生产'}")
    print(f"  配置:  {CONFIG_DIR / 'console.env'}")
    print(f"  审计:  {LOG_DIR / 'console_audit.log'}")
    print()
    app.run(host=host, port=port, debug=False, threaded=True)

if __name__ == "__main__":
    main()
