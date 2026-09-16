#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_validate.py — 输入校验与净化（**单一真源**）
=============================================================================
webhook / HTTP 接口 / 控制台写配置 共用的校验工具。

原则
----
* **白名单校验**：只接受明确允许的形状，不做「猜意图」的宽松解析；
* **长度上限**：所有外部输入都设上限，防止内存/日志放大；
* **净化输出**：日志与响应里绝不回显原始大对象（只回显长度与摘要）。

零依赖。
"""

from __future__ import annotations

import re

__version__ = "1.0"

MAX_JSON_DEPTH = 8


def is_hhmm(value) -> bool:
    """是否为合法 HH:MM（24 小时制）。"""
    return bool(re.fullmatch(r"\s*([01]?\d|2[0-3]):[0-5]\d\s*", str(value or "")))


def is_cron_minutes(value) -> bool:
    """是否为合法的「每 N 分钟」取值（1..1440）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False
    return 1 <= n <= 1440


def clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def clamp_float(value, lo: float, hi: float, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def safe_str(value, max_len: int = 2000, strip: bool = True) -> str:
    """转字符串并按上限截断（防止超长输入进日志/DB）。"""
    s = "" if value is None else str(value)
    if strip:
        s = s.strip()
    return s[:max_len]


def sanitize_log(value, max_len: int = 200) -> str:
    """日志用净化：去掉换行/控制字符，限制长度，避免日志注入。"""
    s = safe_str(value, max_len=max_len * 4)
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s)
    return s[:max_len]


def json_depth(obj, _d: int = 0) -> int:
    """估算 JSON 嵌套深度（防御深度炸弹）。"""
    if _d > 32:
        return _d
    if isinstance(obj, dict):
        return max([json_depth(v, _d + 1) for v in obj.values()] or [_d])
    if isinstance(obj, list):
        return max([json_depth(v, _d + 1) for v in obj] or [_d])
    return _d


def validate_json_payload(payload, max_bytes: int = 1048576,
                          max_depth: int = MAX_JSON_DEPTH) -> tuple:
    """
    校验一个已解析的 JSON payload。返回 (ok, reason)。
    用于 webhook：拒绝超大/超深/非对象主体。
    """
    if not isinstance(payload, (dict, list)):
        return False, "payload must be a JSON object or array"
    try:
        import json
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return False, "payload not serializable"
    if size > max_bytes:
        return False, f"payload too large ({size} > {max_bytes})"
    if json_depth(payload) > max_depth:
        return False, f"payload too deeply nested (> {max_depth})"
    return True, ""


# ── 简单 schema 校验（控制台写 app.yaml 用）───────────────────────────────

def coerce_by_type(value, type_name: str):
    """按 schema 类型名把字符串表单值转成正确类型；不可转返回 (False, None)。"""
    t = (type_name or "str").lower()
    if t in ("str", "path", "url", "email"):
        return True, value if isinstance(value, str) else str(value)
    if t == "bool":
        if isinstance(value, bool):
            return True, value
        return True, str(value).strip().lower() in ("1", "true", "yes", "on", "y")
    if t == "int":
        try:
            return True, int(str(value).strip())
        except (TypeError, ValueError):
            return False, None
    if t == "float":
        try:
            return True, float(str(value).strip())
        except (TypeError, ValueError):
            return False, None
    if t == "enum":
        return True, value
    return True, value
