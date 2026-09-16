#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_study.py — 研究设计参数（干预时间轴 / 目标完成度 / 名册规则）单一真源
=============================================================================
为什么需要它
------------
干预**起止日期、各轮次截止与补交窗口、周次（T0/T1/T2）切点、目标完成度**都是
**部署个性化**的：换一个课题、换一学期就要改。此前这些值硬编码在
`data_dashboard/data.py`（`SCALE_WINDOWS` / `DIET_START` / `248` / 学号前缀）
与 `tools/build_population_stats.py`（时点切点）里，改一处漏一处。

现在统一为：
  * 配置文件：`config/study_calendar.example.yaml` → 安装后 `study.calendar_path`
  * 读取入口：本模块 `load_calendar()`（带**内置默认值**，文件缺失不报错）
  * 路径可配：`study.calendar_path`（app.yaml）或环境变量 `STUDY_CALENDAR`

设计
----
* 零依赖（PyYAML 可选：无 PyYAML 时退回内置默认值并告警，不让服务起不来）；
* **向后兼容**：默认值与改动前的硬编码值完全一致 → 不改变现有行为；
* 所有读取点都经过 `get()`，缺字段自动回退默认值，不抛异常。
"""

from __future__ import annotations

import os
from datetime import date, datetime

__version__ = "1.0"

# ── 内置默认值（= 改动前硬编码值，保证行为不变）───────────────────────────
DEFAULT_CALENDAR = {
    "study_start": "2026-07-06",
    "study_end": "2026-07-29",
    # 量表轮次：deadline / 开放窗口 / 补交截止
    "scale_rounds": [
        {"name": "第1次量表", "deadline": "2026-07-06", "window_start": "2026-07-03",
         "window_end": "2026-07-13", "extended_end": "2026-07-13"},
        {"name": "第2次量表", "deadline": "2026-07-15", "window_start": "2026-07-12",
         "window_end": "2026-07-18"},
        {"name": "第3次量表", "deadline": "2026-07-27", "window_start": "2026-07-24",
         "window_end": "2026-07-30"},
    ],
    "exercise_rounds": [
        {"name": "第1次运动", "deadline": "2026-07-13", "window_start": "2026-07-10",
         "window_end": "2026-07-16"},
        {"name": "第2次运动", "deadline": "2026-07-20", "window_start": "2026-07-17",
         "window_end": "2026-07-23"},
        {"name": "第3次运动", "deadline": "2026-07-27", "window_start": "2026-07-24",
         "window_end": "2026-07-30"},
    ],
    # 饮食打卡：整段每日 1 次
    "diet_start": "2026-07-06",
    "diet_end": "2026-07-29",
    # 周次（时点）切点：用于人群基线与分析
    "waves": {"t0_end": "2026-07-10", "t1_end": "2026-07-21"},
    # 目标完成度（赋分口径）：量表 3 次（上限 10 分）＋ 运动 3 次×1 分 ＋ 饮食 22 天×1 分 = 35
    # 注：本口径仅供**控制台显示目标完成度**；科研统计的完成度/依从性口径见
    #     docs/分析口径一致性核对.md（二者不同，不要混用）。
    "completion": {
        "scale_rounds": 3, "scale_points_per_round": 1, "scale_max": 10,
        "exercise_rounds": 3, "exercise_points_per_round": 1, "exercise_max": 3,
        "diet_days": 22, "diet_points_per_day": 1, "diet_max": 22,
        "total_max": 35,
        "green_threshold": 0.85,          # ≥85% 记「已完成」
        "yellow_threshold": 0.5,          # ≥50% 记「进行中」
        "grace_days": 3,                  # 截止日后 ±N 天内提交仍计入
        "scale_first_extended": "2026-07-13",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def calendar_path(cfg: dict = None) -> str:
    """解析日历文件路径：app.yaml > 环境变量 > 惯例位置。"""
    if cfg:
        p = cfg.get("study.calendar_path") if hasattr(cfg, "get") else None
        if p:
            return str(p)
    for key in ("STUDY_CALENDAR", "RESEARCH_STUDY_CALENDAR"):
        if os.environ.get(key):
            return os.environ[key]
    base = os.environ.get("APP_BASE", "/opt/gutbrainaxis")
    return os.path.join(base, "study_calendar.yaml")


def load_calendar(cfg: dict = None, path: str = None) -> dict:
    """
    读取研究设计日历。**永不抛异常**：解析失败/文件缺失 → 返回内置默认值。
    返回 dict（含 DEFAULT_CALENDAR 的全部键）。
    """
    path = path or calendar_path(cfg)
    if not path or not os.path.exists(path):
        return dict(DEFAULT_CALENDAR)
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return dict(DEFAULT_CALENDAR)
        return _deep_merge(DEFAULT_CALENDAR, data)
    except Exception:                                            # noqa: BLE001
        return dict(DEFAULT_CALENDAR)


# ── 便捷取用 ──────────────────────────────────────────────────────────────

def planned_id_prefixes(cfg: dict = None) -> tuple:
    """「计划内」学号前缀（逗号分隔）。默认 `5,1`。"""
    raw = (cfg or {}).get("study.planned_id_prefixes")
    if raw is None:
        raw = os.environ.get("STUDY_PLANNED_ID_PREFIXES", "5,1")
    return tuple(p.strip() for p in str(raw).split(",") if p.strip())


def is_planned(student_id, cfg: dict = None) -> bool:
    """学号是否属于计划内名单（按配置前缀）。"""
    prefixes = planned_id_prefixes(cfg)
    s = str(student_id or "")
    return any(s.startswith(p) for p in prefixes) if prefixes else True


def total_planned_fallback(cfg: dict = None) -> int:
    try:
        v = (cfg or {}).get("study.total_planned_fallback")
        if v is None:
            v = os.environ.get("STUDY_TOTAL_PLANNED_FALLBACK", 248)
        return int(v)
    except (TypeError, ValueError):
        return DEFAULT_CALENDAR and 248


def diet_date_labels(cal: dict = None) -> list:
    """饮食打卡日期标签（M/D），由日历的 diet_start/end 推导（不再硬编码 7/6）。"""
    cal = cal or dict(DEFAULT_CALENDAR)
    try:
        s = datetime.strptime(cal["diet_start"], "%Y-%m-%d").date()
        e = datetime.strptime(cal["diet_end"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return []
    if e < s:
        return []
    out, cur = [], s
    while cur <= e:
        out.append(f"{cur.month}/{cur.day}")
        cur = date.fromordinal(cur.toordinal() + 1)
    return out


def wave_of(submitted_at: str, cal: dict = None) -> str:
    """按日历切点判定时点（T0/T1/T2）——与 build_population_stats 同一口径。"""
    cal = cal or dict(DEFAULT_CALENDAR)
    d = str(submitted_at or "")[:10]
    if not d:
        return "T?"
    w = cal.get("waves") or {}
    if d <= str(w.get("t0_end", "")):
        return "T0"
    if d <= str(w.get("t1_end", "")):
        return "T1"
    return "T2"


def completion_targets(cal: dict = None) -> dict:
    """目标完成度口径（前端赋分说明与计算共用）。"""
    return dict((cal or DEFAULT_CALENDAR).get("completion") or {})


if __name__ == "__main__":                                       # pragma: no cover
    import json
    cal = load_calendar()
    print(json.dumps({"path": calendar_path(), "calendar": cal,
                      "diet_labels": diet_date_labels(cal)[:5],
                      "planned_prefixes": planned_id_prefixes()},
                     ensure_ascii=False, indent=2))
