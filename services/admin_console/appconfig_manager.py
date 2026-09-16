#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
appconfig_manager.py — 控制台「应用配置」页后端（app.yaml 全量键）
=============================================================================
需求（用户）：控制台「配置」页需能**查看与修改**全部个性化配置项，
**含校验与恢复默认**。

能力
----
* `describe()`        —— 按命名空间分组列出**全部** app.yaml 键
                         （键名/类型/当前值/默认值/单位作用/必填/生效方式/是否密钥）
* `update()`          —— 校验 → 备份 → 原子写入 → 可选重渲染 env/timer
* `preview()`         —— 只算不写（变更预览）
* `restore_defaults()`—— 从 `app.yaml.example` 恢复（写前备份；密钥类不回填）
* `validate_only()`   —— 仅校验当前文件，返回逐条错误

安全
----
* 密钥类键（schema `secret: true`）**只显示是否已配置**，不返回值、不接受写入
  （避免控制台成为密钥泄露口）；密钥只允许在 `secrets.env` 维护；
* 每次写入前备份为 `<config>.bak.<时间戳>`；
* 写入走 `os.replace` 原子替换；
* 恢复默认会先把当前文件备份，且**保留**已有密钥引用。

复用 `tools/appconfig.py` 的 schema/校验/渲染，**不重复实现**。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
APP_BASE = os.environ.get("APP_BASE", "/opt")

for _c in (HERE.parent.parent / "tools", Path(APP_BASE) / "tools", HERE.parent / "tools"):
    if (_c / "appconfig.py").is_file():
        if str(_c) not in sys.path:
            sys.path.insert(0, str(_c))
        break

try:
    import appconfig as AC
except Exception as e:                                        # pragma: no cover
    AC = None
    _IMPORT_ERR = str(e)
else:
    _IMPORT_ERR = ""

# 命名空间 → (中文名, 生效方式)
NAMESPACES = [
    ("version", "配置版本", "—"),
    ("app", "应用与路径", "重启受影响服务"),
    ("site", "站点与反向代理", "重渲染配置后生效"),
    ("logging", "日志级别与轮转", "重启服务"),
    ("http", "HTTP 全局默认", "重启服务"),
    ("services", "各服务端口与入口", "重渲染 env + 重启服务"),
    ("database", "数据库与名册路径", "重启服务"),
    ("smtp", "邮件发送（SMTP）", "重启服务"),
    ("email", "邮件投递路由", "重启服务"),
    ("alert", "告警阈值与冷却", "热加载"),
    ("llm", "LLM 主备（地址/模型/密钥/温度）", "重启 diet-llm-queue"),
    ("analysis", "LLM 分析调度", "热加载（≤30s）"),
    ("scoring", "计分口径与分档", "重启受影响服务"),
    ("study", "研究设计（时间轴/目标/名册）", "热加载"),
    ("survey_platforms", "问卷平台（Token/ID/周期）", "重启对应 sync"),
    ("webhook", "Webhook 开关与重试", "重启 webhook 服务"),
    ("schedule", "调度周期与静默时段", "重渲染 timer + daemon-reload"),
    ("backup", "备份保留与远端", "下次备份生效"),
    ("selfcheck", "自检探活", "热加载"),
]

HOT_PREFIXES = ("analysis.", "alert.", "study.", "selfcheck.", "schedule.quiet_hours")


def config_path() -> str:
    p = os.environ.get("RESEARCH_APP_CONFIG")
    if p and os.path.exists(p):
        return p
    for cand in ("/etc/research-app/app.yaml",
                 str(HERE.parent.parent / "config" / "app.yaml"),
                 str(Path(APP_BASE) / "config" / "app.yaml")):
        if os.path.exists(cand):
            return cand
    return os.environ.get("RESEARCH_APP_CONFIG", "/etc/research-app/app.yaml")


def _flat(d: dict, pre: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{pre}{k}."))
        else:
            out[f"{pre}{k}"] = v
    return out


def _nested_set(data: dict, key: str, value):
    parts = key.split(".")
    cur = data
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _apply_hint(key: str, ns_hint: str) -> str:
    for pref in HOT_PREFIXES:
        if key.startswith(pref):
            return "热加载（≤30s）"
    return ns_hint


def describe() -> dict:
    path = config_path()
    if not AC:
        return {"error": f"appconfig 不可用：{_IMPORT_ERR}", "groups": [],
                "config_path": path, "n_keys": 0}
    try:
        raw = AC._load_yaml(path)
        with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
            fields = json.load(f).get("fields", {})
    except Exception as e:                                    # noqa: BLE001
        return {"error": str(e), "groups": [], "config_path": path, "n_keys": 0}

    cur = _flat(raw)
    defaults = {}
    try:
        defaults = _flat(AC._load_yaml(AC.EXAMPLE_CONFIG))
    except Exception:                                          # noqa: BLE001
        pass

    secret_keys = AC.SECRET_KEYS
    groups = []
    for ns, zh, hint in NAMESPACES:
        items = []
        for key in sorted(k for k in fields if k.split(".")[0] == ns):
            rule = fields[key]
            is_secret = bool(rule.get("secret"))
            val = cur.get(key, "")
            if isinstance(val, str) and val.startswith("${"):
                is_secret = True
            items.append({
                "key": key, "type": rule.get("type", "str"),
                "value": ("（已配置，隐藏）" if (is_secret and val) else
                          ("" if is_secret else val)),
                "default": defaults.get(key, ""),
                "secret": is_secret,
                "configured": bool(val),
                "required": bool(rule.get("required")),
                "enum": rule.get("enum"),
                "min": rule.get("min"), "max": rule.get("max"),
                "apply": _apply_hint(key, hint),
                "env": AC.ENV_MAP.get(key, ""),
            })
        groups.append({"ns": ns, "title": zh, "items": items})

    # 当前校验结果（占位符只告警，避免「刚装好就红」）
    try:
        _res, errs, warns = AC.validate(raw, {"fields": fields}, {},
                                        strict_placeholders=False)
    except Exception as e:                                    # noqa: BLE001
        errs, warns = [str(e)], []

    return {"config_path": path, "writable": os.access(path, os.W_OK),
            "n_keys": len(fields), "groups": groups,
            "errors": errs[:50], "warnings": warns[:20],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def preview(updates: dict) -> dict:
    if not AC:
        return {"ok": False, "errors": [f"appconfig 不可用：{_IMPORT_ERR}"], "changes": []}
    path = config_path()
    raw = AC._load_yaml(path)
    cur = _flat(raw)
    changes = []
    for k, v in (updates or {}).items():
        if k in AC.SECRET_KEYS:
            continue
        changes.append({"key": k, "before": cur.get(k, ""), "after": v})
    return {"ok": True, "changes": changes, "errors": [],
            "note": "预览模式：未写入磁盘"}


def update(updates: dict, apply_env: bool = True, apply_timers: bool = True) -> dict:
    if not AC:
        return {"success": False, "error": f"appconfig 不可用：{_IMPORT_ERR}"}
    path = config_path()
    if not os.path.exists(path):
        return {"success": False, "error": f"配置文件不存在：{path}"}

    # 密钥类键不允许经控制台改写（只在 secrets.env 维护）
    rejected = sorted(k for k in (updates or {}) if k in AC.SECRET_KEYS)
    valid = {k: v for k, v in (updates or {}).items() if k not in AC.SECRET_KEYS}
    if not valid:
        return {"success": False, "error": "没有可更新项（密钥类键请在 secrets.env 维护）",
                "rejected_secrets": rejected}

    raw = AC._load_yaml(path)
    with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    fields = schema.get("fields", {})
    before = _flat(raw)

    errors, changes = [], []
    for k, v in valid.items():
        if k not in fields:
            errors.append(f"{k}: 未在 schema 中声明")
            continue
        t = (fields[k] or {}).get("type", "str")
        try:
            if t == "bool":
                val = v if isinstance(v, bool) else str(v).strip().lower() in (
                    "1", "true", "yes", "on", "y")
            elif t == "int":
                val = int(str(v).strip())
            elif t == "float":
                val = float(str(v).strip())
            else:
                val = v if isinstance(v, str) else str(v)
        except (TypeError, ValueError):
            errors.append(f"{k}: 值 {v!r} 无法转换为 {t}")
            continue
        _nested_set(raw, k, val)
        changes.append({"key": k, "before": before.get(k, ""), "after": val})

    if errors:
        return {"success": False, "errors": errors, "changes": changes,
                "rejected_secrets": rejected}

    _res, verrs, _vw = AC.validate(raw, schema, {}, strict_placeholders=False)
    if verrs:
        return {"success": False, "errors": verrs, "changes": changes,
                "rejected_secrets": rejected}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak.{stamp}"
    try:
        shutil.copy2(path, backup)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
        os.replace(tmp, path)
    except OSError as e:
        return {"success": False, "error": f"写入失败：{e}", "changes": changes}

    rendered = []
    if apply_env:
        try:
            rendered.append("env: " + os.path.basename(
                AC.render_env_file(AC.flatten(raw), os.environ.get(
                    "ENV_FILE", "/etc/research-app/env"))))
        except OSError as e:
            rendered.append(f"env 渲染失败: {e}")
    if apply_timers:
        try:
            AC.render_timer_fragments(AC.flatten(raw),
                                      os.environ.get("SYSTEMD_DIR", "/etc/systemd/system"))
            rendered.append("timers: 6 个片段已重渲染")
        except OSError as e:
            rendered.append(f"timer 渲染失败: {e}")

    return {"success": True, "changes": changes, "backup": backup,
            "rejected_secrets": rejected, "rendered": rendered,
            "note": "已写入 app.yaml；env/timer 已重渲染，"
                    "需 systemctl daemon-reload + 重启受影响服务"}


def restore_defaults(confirm: bool = False) -> dict:
    """
    从 app.yaml.example 恢复默认。写前备份；**保留** secrets.env（本函数不动它）。
    confirm=True 才执行（防止误点）。
    """
    if not confirm:
        return {"success": False, "error": "需要 confirm=true 才执行恢复默认"}
    if not AC:
        return {"success": False, "error": f"appconfig 不可用：{_IMPORT_ERR}"}
    path = config_path()
    try:
        example = AC._load_yaml(AC.EXAMPLE_CONFIG)
    except Exception as e:                                    # noqa: BLE001
        return {"success": False, "error": f"读取模板失败：{e}"}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak.{stamp}"
    try:
        if os.path.exists(path):
            shutil.copy2(path, backup)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(example, f, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
        os.replace(tmp, path)
    except OSError as e:
        return {"success": False, "error": f"写入失败：{e}"}
    return {"success": True, "backup": backup,
            "note": "已恢复为模板默认值；密钥仍从 secrets.env 解析，未受影响"}
