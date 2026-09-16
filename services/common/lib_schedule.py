#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_schedule.py — 时间窗口 / 时区 / 静默时段（**单一真源**）
=============================================================================
本模块是全系统**唯一**的「时间窗口判定」实现，被下列调用方共用：

  * `tools/appconfig.py`            —— 校验 HH:MM、渲染 timer、--show-window
  * `services/diet_survey/diet_llm_queue.py` —— B：分析窗口（realtime/offpeak/hybrid）
  * `services/sjtu_survey_pro/survey_webhook_listener.py`、……/webhook_listener.py
  * `scripts/health_monitor.py` / `scripts/send_alert.py` —— 告警静默时段
  * 控制台「调度」页 / 看板「系统健康」—— 状态展示

零依赖（仅标准库）。所有函数都可传入 `now` 以便单元测试。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

__version__ = "1.0"

DEFAULT_TZ = "Asia/Shanghai"


# ── 解析 ──────────────────────────────────────────────────────────────────

def parse_hhmm(value) -> int:
    """'23:00' → 1380（自午夜起的分钟数）。非法/缺失返回 -1。"""
    if value is None:
        return -1
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
    if not m:
        return -1
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return -1
    return h * 60 + mi


def in_time_window(now_min: int, start_min: int, end_min: int) -> bool:
    """
    分钟数是否落在 [start, end) 内，**支持跨天**（如 23:00→06:00）。
    start 或 end 非法 → 不限制（返回 True）；start==end → 24 小时全开。
    """
    if start_min < 0 or end_min < 0:
        return True
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def weekday_allowed(workdays, isoweekday: int) -> bool:
    """workdays='1,2,3' → 是否允许该星期（1=周一 … 7=周日）。空/非法=全允许。"""
    if workdays is None or str(workdays).strip() == "":
        return True
    parts = [p.strip() for p in str(workdays).split(",") if p.strip()]
    if not parts:
        return True
    try:
        return isoweekday in {int(p) for p in parts}
    except ValueError:
        return True


def resolve_tz(name: str):
    """解析 IANA 时区；失败退回 UTC+8（本课题默认时区）。"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name) or DEFAULT_TZ)
    except Exception:                                        # noqa: BLE001
        return timezone(timedelta(hours=8))


def now_in(tz_name: str, now=None) -> datetime:
    tz = resolve_tz(tz_name)
    if now is None:
        return datetime.now(tz)
    return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)


def _get(cfg, key, default=None):
    """按点分键取值，兼容扁平与嵌套 dict。"""
    if cfg is None:
        return default
    if key in cfg:
        return cfg[key]
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# ── 业务判定 ──────────────────────────────────────────────────────────────

def window_state(cfg, now=None) -> dict:
    """
    LLM 分析调度窗口状态（B）。返回：
      {mode, in_window, waiting, now, timezone, window_start, window_end,
       workdays, workday_ok, next_window_hint}
    mode=realtime → in_window 恒 True（等价旧行为）。
    """
    mode = str(_get(cfg, "analysis.mode", "realtime") or "realtime").lower()
    dt = now_in(str(_get(cfg, "analysis.timezone", "") or
                    _get(cfg, "app.timezone", "") or DEFAULT_TZ), now)
    start = parse_hhmm(_get(cfg, "analysis.window_start"))
    end = parse_hhmm(_get(cfg, "analysis.window_end"))
    workday_ok = weekday_allowed(_get(cfg, "analysis.workdays"), dt.isoweekday())
    now_min = dt.hour * 60 + dt.minute

    if mode in ("offpeak", "hybrid"):
        in_win = bool(workday_ok and in_time_window(now_min, start, end))
    else:
        in_win = True                    # realtime / 未知模式 → 不阻塞（保守）
    return {
        "mode": mode,
        "in_window": in_win,
        "waiting": (mode in ("offpeak", "hybrid")) and not in_win,
        "now": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": str(_get(cfg, "analysis.timezone", "") or DEFAULT_TZ),
        "window_start": _get(cfg, "analysis.window_start"),
        "window_end": _get(cfg, "analysis.window_end"),
        "workdays": _get(cfg, "analysis.workdays"),
        "workday_ok": bool(workday_ok),
        "next_window_hint": (f"{_get(cfg, 'analysis.window_start')}–"
                             f"{_get(cfg, 'analysis.window_end')}"),
    }


def is_quiet_hours(cfg, now=None) -> bool:
    """是否处于静默时段（schedule.quiet_hours_*，留空=不启用）。"""
    start = parse_hhmm(_get(cfg, "schedule.quiet_hours_start"))
    end = parse_hhmm(_get(cfg, "schedule.quiet_hours_end"))
    if start < 0 or end < 0:
        return False
    dt = now_in(str(_get(cfg, "app.timezone", "") or DEFAULT_TZ), now)
    return in_time_window(dt.hour * 60 + dt.minute, start, end)


def alert_quiet_hours(cfg, now=None) -> bool:
    """告警专用静默判定：alert.quiet_hours_* 优先，留空则继承 schedule.quiet_hours_*。"""
    a_start = parse_hhmm(_get(cfg, "alert.quiet_hours_start"))
    a_end = parse_hhmm(_get(cfg, "alert.quiet_hours_end"))
    if a_start >= 0 and a_end >= 0:
        dt = now_in(str(_get(cfg, "app.timezone", "") or DEFAULT_TZ), now)
        return in_time_window(dt.hour * 60 + dt.minute, a_start, a_end)
    return is_quiet_hours(cfg, now)


def load_config(path: str = None) -> dict:
    """
    便捷加载 app.yaml（供 services/ 下的脚本使用）。
    优先用 tools/appconfig.py（含 schema 校验）；不可用时退回最小 YAML 加载。
    """
    path = path or os.environ.get("RESEARCH_APP_CONFIG", "/etc/research-app/app.yaml")
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(os.path.dirname(here), ".."),
                 os.path.join(os.path.dirname(here), "..", "..")):
        p = os.path.abspath(os.path.join(cand, "tools"))
        if os.path.isfile(os.path.join(p, "appconfig.py")) and p not in __import__("sys").path:
            __import__("sys").path.insert(0, p)
    try:
        import appconfig as AC
        cfg, errs = AC.try_load(path, strict_placeholders=False)
        if cfg is not None:
            return cfg
    except Exception:                                        # noqa: BLE001
        pass
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        flat = {}

        def _flat(d, pre=""):
            for k, v in (d or {}).items():
                if str(k).startswith("_"):
                    continue
                if isinstance(v, dict):
                    _flat(v, f"{pre}{k}.")
                else:
                    flat[f"{pre}{k}"] = v
        _flat(data)
        return flat
    except Exception:                                        # noqa: BLE001
        return {}


if __name__ == "__main__":                                   # pragma: no cover
    import json
    cfg = load_config()
    print(json.dumps({"window": window_state(cfg),
                      "quiet_hours_now": is_quiet_hours(cfg)},
                     ensure_ascii=False, indent=2))
