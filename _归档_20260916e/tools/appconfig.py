#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
appconfig.py — 统一配置加载 / 校验 / 渲染（app.yaml 单一入口）
=============================================================================
角色
----
本模块是**所有可配置项的唯一读取入口**。它把 `config/app.yaml`
（安装后位于 `/etc/research-app/app.yaml`）解析为带点分路径的扁平字典，
按 `config/app.schema.json` **启动即校验**，并可渲染为 systemd 使用的
环境文件（`/etc/research-app/env`）。

设计要点
--------
* **单一入口**：端口 / 域名 / DNS / 反代、服务路径、数据库路径、SMTP、
  告警、LLM（主+备）、三个问卷 Token、调度周期、日志级别、备份策略、
  自检开关，全部在 app.yaml。
* **密钥分离**：app.yaml 里写 `"${SMTP_PASSWORD}"` 之类引用；真实值来自
  `/etc/research-app/secrets.env`（0600）或进程环境变量。**仓库内零密钥。**
* **启动即校验**：缺项 / 类型错 / 超范围 / 未替换占位符 → 抛出
  `ConfigError`，异常消息**逐条给出点分键位置**与修复建议。
* **向后兼容**：服务仍可直接读环境变量；`to_env()` 保持历史键名
  （APP_BASE / SITE_DOMAIN / SURVEY_DB / SMTP_* ...）不变。

CLI
---
    python3 tools/appconfig.py --check                 # 校验并打印人类可读表
    python3 tools/appconfig.py --check --json          # 机器可读
    python3 tools/appconfig.py --render-env OUT        # 渲染 systemd 环境文件
    python3 tools/appconfig.py --render-timers DIR     # 生成 timer 覆盖片段
    python3 tools/appconfig.py --dump-keys             # 列出全部键与当前值（密钥打码）
退出码：0=通过；1=校验失败；2=文件/解析错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)

DEFAULT_CONFIG = os.environ.get("RESEARCH_APP_CONFIG", "/etc/research-app/app.yaml")
DEFAULT_SECRETS = os.environ.get("RESEARCH_APP_SECRETS", "/etc/research-app/secrets.env")
DEFAULT_SCHEMA = os.path.join(PKG_DIR, "config", "app.schema.json")
EXAMPLE_CONFIG = os.path.join(PKG_DIR, "config", "app.yaml.example")

# 占位符模式：命中即视为「未配置」
PLACEHOLDER_RE = re.compile(
    r"(__CHANGE_ME__|__REPLACE_[A-Z0-9_]*__|^<[A-Z_]+>$|^example\.com$|^$)"
)
# 明显是说明性占位（非密钥）的白名单键：允许为空且不算占位符错误
OPTIONAL_EMPTY_OK = {
    "smtp.cc_email", "backup.rclone_remote", "services.survey_feedback.port_alt",
    "llm.backup.base_url", "llm.backup.model", "llm.backup.api_key",
}


class ConfigError(Exception):
    """配置校验失败。message 逐条包含点分键位置。"""

    def __init__(self, errors: List[str], warnings: List[str] | None = None):
        self.errors = errors
        self.warnings = warnings or []
        super().__init__("配置校验失败：\n  - " + "\n  - ".join(errors))


# ── 基础工具 ──────────────────────────────────────────────────────────────

def _load_yaml(path: str) -> dict:
    try:
        import yaml
    except ImportError as e:                                  # pragma: no cover
        raise ConfigError([f"缺少依赖 PyYAML（pip install pyyaml）：{e}"])
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError([f"配置文件不存在: {path}（示例见 {EXAMPLE_CONFIG}）"])
    except OSError as e:
        raise ConfigError([f"配置文件无法读取: {path}: {e}"])
    except Exception as e:
        raise ConfigError([f"YAML 解析失败: {path}: {e}"])
    if not isinstance(data, dict):
        raise ConfigError([f"配置根节点必须是 mapping: {path}"])
    return data


def _read_secrets(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def _resolve_secrets(value: Any, secrets: Dict[str, str]) -> Any:
    """把 "\\${VAR}"（或 "$VAR"）解析为 secrets 文件 / 进程环境中的值。"""
    if not isinstance(value, str):
        return value
    m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
    if not m:
        m = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", value.strip())
    if not m:
        return value
    name = m.group(1)
    return os.environ.get(name) or secrets.get(name) or ""


def flatten(data: dict, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (data or {}).items():
        if str(k).startswith("_"):
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, f"{key}."))
        else:
            out[key] = v
    return out


def get(cfg: Dict[str, Any], key: str, default=None):
    """按点分键取配置（cfg 可为扁平或嵌套 dict）。"""
    if key in cfg:
        return cfg[key]
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def as_bool(v, default=False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


# ── 校验 ──────────────────────────────────────────────────────────────────

def _check_type(key: str, val: Any, rule: dict) -> str | None:
    t = rule.get("type", "str")
    if t in ("str", "path", "url", "email"):
        if not isinstance(val, str):
            return f"{key}: 期望字符串，实际 {type(val).__name__}"
    elif t == "int":
        if isinstance(val, bool) or not isinstance(val, int):
            if isinstance(val, str) and re.fullmatch(r"-?\d+", val.strip()):
                return None
            return f"{key}: 期望整数，实际 {val!r}"
    elif t == "float":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return f"{key}: 期望数值，实际 {val!r}"
    elif t == "bool":
        if not isinstance(val, (bool, str, int)):
            return f"{key}: 期望布尔，实际 {val!r}"
    elif t == "enum":
        allowed = rule.get("enum") or []
        if val not in allowed:
            return f"{key}: 取值 {val!r} 不在允许集合 {allowed} 内"
    return None


def _check_format(key: str, val: str, rule: dict) -> str | None:
    t = rule.get("type")
    if not isinstance(val, str) or not val.strip():
        return None
    if t == "path":
        if not (val.startswith("/") or val.startswith("~")):
            return f"{key}: 路径应为绝对路径，实际 {val!r}"
    elif t == "url":
        if not re.match(r"^https?://[^\s]+$", val.strip()):
            return f"{key}: URL 非法，实际 {val!r}"
    elif t == "email":
        if "${" not in val and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", val.strip()):
            return f"{key}: 邮箱格式非法，实际 {val!r}"
    return None


def _is_placeholder(val: Any) -> bool:
    return isinstance(val, str) and bool(PLACEHOLDER_RE.search(val.strip()))


def validate(raw: dict, schema: dict, secrets: dict | None = None,
             strict_placeholders: bool = True) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """
    返回 (扁平配置, errors, warnings)。errors 非空表示校验失败。
    strict_placeholders=False 时，占位符只告警（用于 CI/预览环境）。
    """
    secrets = secrets or {}
    flat = flatten(raw)
    resolved = {k: _resolve_secrets(v, secrets) for k, v in flat.items()}

    errors: List[str] = []
    warnings: List[str] = []
    fields = (schema or {}).get("fields", {})

    for key, rule in fields.items():
        if key.endswith(".*"):        # 自由命名空间：不参与必填/类型校验
            continue
        required = bool(rule.get("required"))
        secret = bool(rule.get("secret"))
        val = resolved.get(key, None)

        if val is None or (isinstance(val, str) and not val.strip()):
            if required and not secret:
                errors.append(f"{key}: 必填项缺失（请在 app.yaml 中配置）")
            elif required and secret:
                warnings.append(f"{key}: 密钥未提供（运行期相关功能将不可用）")
            continue

        err = _check_type(key, val, rule)
        if err:
            errors.append(err)
            continue
        if isinstance(val, str):
            err = _check_format(key, val, rule)
            if err:
                errors.append(err)
                continue

        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if rule.get("min") is not None and val < rule["min"]:
                errors.append(f"{key}: {val} 小于最小允许值 {rule['min']}")
            if rule.get("max") is not None and val > rule["max"]:
                errors.append(f"{key}: {val} 大于最大允许值 {rule['max']}")

        if _is_placeholder(val) and key not in OPTIONAL_EMPTY_OK:
            msg = f"{key}: 仍是占位符/示例值 {val!r}（请替换为真实配置）"
            (errors if strict_placeholders else warnings).append(msg)

    # 未在 schema 中声明的键 → 告警（防止拼写错误被静默忽略；`ns.*` 为自由命名空间）
    namespaces = [k[:-2] for k in fields if k.endswith(".*")]
    for key in resolved:
        if key in fields:
            continue
        if any(key.startswith(ns + ".") for ns in namespaces):
            continue
        warnings.append(f"{key}: 未在 app.schema.json 中声明（将被忽略）")

    # 交叉校验：端口冲突
    seen: Dict[int, str] = {}
    for key in ("services.survey_feedback.port", "services.diet_feedback.port",
                "services.diet_webhook.port", "services.admin_console.port",
                "services.data_dashboard.port"):
        p = resolved.get(key)
        if isinstance(p, int) and p > 0:
            if p in seen:
                errors.append(f"{key}: 端口 {p} 与 {seen[p]} 冲突")
            seen[p] = key

    # 交叉校验：base_dir 与各路径一致性（仅告警，允许刻意分离）
    base = resolved.get("app.base_dir")
    if isinstance(base, str) and base:
        for key in ("database.survey_db", "database.diet_db", "database.exercise_db"):
            p = resolved.get(key)
            if isinstance(p, str) and p and not p.startswith(base):
                warnings.append(f"{key}: 不在 app.base_dir({base}) 之下，请确认是有意为之")

    return resolved, errors, warnings


def load(path: str = None, schema_path: str = None, secrets_path: str = None,
         strict_placeholders: bool = True) -> Dict[str, Any]:
    """加载 + 校验；失败抛 ConfigError。返回**扁平**配置（含全部 schema 默认）。"""
    path = path or DEFAULT_CONFIG
    schema_path = schema_path or DEFAULT_SCHEMA
    secrets_path = secrets_path if secrets_path is not None else DEFAULT_SECRETS

    raw = _load_yaml(path)
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    secrets = _read_secrets(secrets_path)
    resolved, errors, warnings = validate(raw, schema, secrets, strict_placeholders)
    if strict_placeholders and errors:
        raise ConfigError(errors, warnings)
    resolved["_warnings"] = warnings
    resolved["_source"] = path
    return resolved


def try_load(path: str = None, **kw) -> Tuple[Dict[str, Any] | None, List[str]]:
    """不抛异常的加载；返回 (配置|None, 错误列表)。供自检/控制台使用。"""
    try:
        return load(path, **kw), []
    except ConfigError as e:
        return None, e.errors


# ── 渲染：环境文件 / timer 片段 ───────────────────────────────────────────

# 扁平配置键 → 历史环境变量名
ENV_MAP = {
    "app.base_dir": "APP_BASE", "app.data_dir": "DATA_DIR",
    "app.log_dir": "LOG_DIR", "app.log_level": "LOG_LEVEL",
    "app.timezone": "TZ", "app.scoring_version": "SCORING_VERSION",
    "site.domain": "SITE_DOMAIN",
    "site.nginx_sites_dir": "NGINX_SITES_DIR",
    "site.nginx_enabled_dir": "NGINX_ENABLED_DIR",
    "database.survey_db": "SURVEY_DB", "database.diet_db": "DIET_DB",
    "database.exercise_db": "EXERCISE_DB", "database.microbiome_db": "MICROBIOME_DB",
    "database.roster": "ROSTER_PATH",
    "smtp.host": "SMTP_HOST", "smtp.port": "SMTP_PORT",
    "smtp.ssl_host": "SMTP_SSL_HOST", "smtp.ssl_port": "SMTP_SSL_PORT",
    "smtp.use_ssl": "SMTP_USE_SSL",
    "smtp.username": "SMTP_USERNAME", "smtp.password": "SMTP_PASSWORD",
    "smtp.sender_email": "SMTP_SENDER_EMAIL", "smtp.sender_name": "SMTP_SENDER_NAME",
    "smtp.admin_email": "SMTP_ADMIN_EMAIL", "smtp.cc_email": "SMTP_CC_EMAIL",
    "alert.host": "ALERT_SMTP_HOST", "alert.port": "ALERT_SMTP_PORT",
    "alert.use_ssl": "ALERT_SMTP_USE_SSL",
    "alert.username": "ALERT_SMTP_USERNAME", "alert.password": "ALERT_SMTP_PASSWORD",
    "alert.sender_email": "ALERT_FROM", "alert.sender_name": "ALERT_SENDER_NAME",
    "alert.to": "ALERT_TO", "alert.cooldown_minutes": "ALERT_COOLDOWN_MINUTES",
    "llm.primary.base_url": "LLM_BASE_URL", "llm.primary.model": "LLM_MODEL_NAME",
    "llm.primary.api_keys": "LLM_API_KEYS",
    "llm.backup.base_url": "LLM_BACKUP_BASE_URL", "llm.backup.model": "LLM_BACKUP_MODEL_NAME",
    "llm.backup.api_key": "LLM_BACKUP_API_KEY",
    "survey_platforms.scale.token": "WJX_SURVEY_TOKEN",
    "survey_platforms.scale.page_size": "WJX_SURVEY_PAGE_SIZE",
    "survey_platforms.diet.token": "WJX_DIET_TOKEN",
    "survey_platforms.diet.page_size": "WJX_DIET_PAGE_SIZE",
    "survey_platforms.exercise.token": "WJX_EXERCISE_TOKEN",
    "survey_platforms.exercise.page_size": "WJX_EXERCISE_PAGE_SIZE",
    "backup.dir": "BACKUP_DIR", "backup.retention": "RETENTION",
    "backup.rclone_remote": "RCLONE_REMOTE", "backup.rclone_log": "BACKUP_LOG",
    "microbiome.dir": "MICROBIOME_DIR", "microbiome.import_dir": "MICROBIOME_IMPORT_DIR",
    "microbiome.enabled": "MICROBIOME_ENABLED",
    "selfcheck.enabled": "SELFCHECK_ENABLED",
    "selfcheck.min_free_disk_gb": "SELFCHECK_MIN_FREE_DISK_GB",
    "schedule.daily_report_at": "DAILY_REPORT_AT",
}

SECRET_KEYS = {k for k, r in json.load(open(DEFAULT_SCHEMA, encoding="utf-8"))["fields"].items()
               if r.get("secret")} if os.path.exists(DEFAULT_SCHEMA) else set()


def to_env(cfg: Dict[str, Any], include_secrets: bool = True) -> Dict[str, str]:
    """扁平配置 → 环境变量字典（历史键名，向后兼容）。"""
    out: Dict[str, str] = {}
    for key, env in ENV_MAP.items():
        v = cfg.get(key)
        if v is None or v == "":
            continue
        if not include_secrets and key in SECRET_KEYS:
            continue
        out[env] = ("1" if v is True else "0" if v is False else str(v))
    # 服务端口
    for svc, env in (("survey_feedback", "PORT_SURVEY_FEEDBACK"),
                     ("diet_feedback", "PORT_DIET_FEEDBACK"),
                     ("diet_webhook", "PORT_DIET_WEBHOOK"),
                     ("admin_console", "PORT_ADMIN_CONSOLE"),
                     ("data_dashboard", "PORT_DATA_DASHBOARD")):
        p = cfg.get(f"services.{svc}.port")
        if p:
            out[env] = str(p)
    return out


def render_env_file(cfg: Dict[str, Any], out_path: str, secrets_path: str = None) -> str:
    """写出 systemd EnvironmentFile（0600）。密钥以 ${VAR} 形式指向 secrets 文件。"""
    lines = [
        "# 由 tools/appconfig.py 从 app.yaml 渲染 —— 请勿手改，改 app.yaml 后重新渲染",
        f"# source: {cfg.get('_source', '?')}",
        "",
    ]
    env = to_env(cfg, include_secrets=False)
    for k in sorted(env):
        lines.append(f"{k}={env[k]}")
    lines.append("")
    lines.append("# ── 密钥（值来自 secrets.env，systemd 会做变量展开）──")
    for key in sorted(SECRET_KEYS):
        env_name = ENV_MAP.get(key)
        if env_name:
            val = cfg.get(key) or ""
            lines.append(f'{env_name}="{val}"')
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, out_path)
    return out_path


def render_timer_fragments(cfg: Dict[str, Any], out_dir: str) -> List[str]:
    """按调度周期生成 timer OnCalendar 片段（供 install.sh 落地到 systemd）。"""
    sched = {
        "research-survey-sync.timer": f"*:0/{max(1, int(cfg.get('schedule.survey_sync_minutes', 60)))}",
        "research-diet-sync.timer": f"*:0/{max(1, int(cfg.get('schedule.diet_sync_minutes', 15)))}",
        "research-exercise-sync.timer": f"*:0/{max(1, int(cfg.get('schedule.exercise_sync_minutes', 15)))}",
        "research-health-monitor.timer": f"*:0/{max(1, int(cfg.get('schedule.health_monitor_minutes', 30)))}",
        "research-microbiome-import.timer": f"*:0/{max(1, int(cfg.get('schedule.microbiome_import_minutes', 60)))}",
    }
    daily = str(cfg.get("schedule.daily_report_at", "08:30"))
    backup = str(cfg.get("schedule.backup_at", "03:00"))
    sched["research-daily-report.timer"] = f"*-*-* {daily}:00"
    sched["research-rclone-backup.timer"] = f"*-*-* {backup}:00"

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, oncal in sched.items():
        path = os.path.join(out_dir, name + ".d")
        os.makedirs(path, exist_ok=True)
        frag = os.path.join(path, "10-schedule.conf")
        with open(frag, "w", encoding="utf-8") as f:
            f.write("# 由 tools/appconfig.py 生成（周期来自 app.yaml schedule.*）\n")
            f.write("[Timer]\n")
            f.write(f"OnCalendar=\nOnCalendar={oncal}\n")
        written.append(frag)
    return written


# ── CLI ───────────────────────────────────────────────────────────────────

def _mask(cfg: dict, key: str) -> str:
    v = cfg.get(key)
    if key in SECRET_KEYS and v:
        s = str(v)
        return (s[:3] + "*" * max(3, len(s) - 6) + s[-3:]) if len(s) > 8 else "***"
    return "" if v is None else str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="统一配置加载/校验/渲染（app.yaml）")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--secrets", default=DEFAULT_SECRETS)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--check", action="store_true", help="校验并打印结果")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--dump-keys", action="store_true", help="列出全部键与当前值")
    ap.add_argument("--render-env", metavar="OUT", help="渲染 systemd 环境文件")
    ap.add_argument("--render-timers", metavar="DIR", help="生成 timer OnCalendar 片段")
    ap.add_argument("--no-strict", action="store_true", help="占位符只告警，不判失败")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        msg = f"配置文件不存在: {args.config}（示例: {EXAMPLE_CONFIG}）"
        if args.json:
            print(json.dumps({"ok": False, "errors": [msg]}, ensure_ascii=False))
        else:
            print(f"❌ {msg}", file=sys.stderr)
        return 2

    try:
        raw = _load_yaml(args.config)
        with open(args.schema, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except ConfigError as e:
        print("❌ " + "\n❌ ".join(e.errors), file=sys.stderr)
        return 2

    secrets = _read_secrets(args.secrets)
    resolved, errors, warnings = validate(raw, schema, secrets,
                                          strict_placeholders=not args.no_strict)

    if args.render_env:
        if errors:
            print("❌ 校验未通过，拒绝渲染环境文件：", file=sys.stderr)
            for e in errors:
                print("   - " + e, file=sys.stderr)
            return 1
        print("✅ 已渲染 " + render_env_file(resolved, args.render_env))
        return 0

    if args.render_timers:
        if errors:
            print("❌ 校验未通过，拒绝渲染 timer：", file=sys.stderr)
            return 1
        for p in render_timer_fragments(resolved, args.render_timers):
            print("✅ " + p)
        return 0

    if args.dump_keys:
        for k in sorted(resolved):
            if k.startswith("_"):
                continue
            print(f"{k} = {_mask(resolved, k)}")

    if args.json:
        print(json.dumps({
            "ok": not errors, "source": args.config,
            "errors": errors, "warnings": warnings,
            "n_keys": len([k for k in resolved if not k.startswith('_')]),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"配置文件: {args.config}")
        print(f"schema:   {args.schema}")
        print(f"键数:     {len([k for k in resolved if not k.startswith('_')])}")
        if warnings:
            print(f"\n⚠️  告警 {len(warnings)} 条：")
            for w in warnings:
                print("   - " + w)
        if errors:
            print(f"\n❌ 失败 {len(errors)} 条：")
            for e in errors:
                print("   - " + e)
            return 1
        print("\n✅ 配置校验通过")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
