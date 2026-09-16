#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_schedule_window.py — LLM 分析窗口调度（B）单元测试
=============================================================================
覆盖：

  * `realtime`：窗口恒开，行为等价旧版（不阻塞）
  * `offpeak` ：窗口内消费、窗口外挂起（waiting=True）
  * `hybrid`  ：窗口内全开；窗口外限量
  * **跨天窗口**（23:00→06:00）在 00:30 / 12:00 的判定
  * `workdays` 星期过滤
  * 静默时段（schedule.quiet_hours_*）与告警静默（alert.* 覆盖 schedule.*）
  * HH:MM 解析与非法值处理

运行::

    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PKG, "services", "common"))

from lib_schedule import (                                  # noqa: E402
    parse_hhmm, in_time_window, weekday_allowed, window_state,
    is_quiet_hours, alert_quiet_hours, now_in,
)

BASE_CFG = {
    "analysis.mode": "offpeak",
    "analysis.window_start": "23:00",
    "analysis.window_end": "06:00",
    "analysis.timezone": "Asia/Shanghai",
    "analysis.workdays": "1,2,3,4,5,6,7",
    "schedule.quiet_hours_start": "22:30",
    "schedule.quiet_hours_end": "07:00",
    "app.timezone": "Asia/Shanghai",
}


def cfg(**kw):
    c = dict(BASE_CFG)
    c.update(kw)
    return c


class TestParseHHMM(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_hhmm("23:00"), 1380)
        self.assertEqual(parse_hhmm("00:00"), 0)
        self.assertEqual(parse_hhmm("06:30"), 390)
        self.assertEqual(parse_hhmm(" 7:05 "), 425)

    def test_invalid(self):
        for bad in ("", None, "24:00", "12:60", "abc", "1200", "12"):
            self.assertEqual(parse_hhmm(bad), -1, f"{bad!r} 应为非法")


class TestInTimeWindow(unittest.TestCase):
    def test_same_day(self):
        self.assertTrue(in_time_window(600, 540, 720))      # 10:00 ∈ 09:00-12:00
        self.assertFalse(in_time_window(800, 540, 720))     # 13:20 ∉
        self.assertTrue(in_time_window(540, 540, 720))      # 左闭：start 计入
        self.assertFalse(in_time_window(720, 540, 720))     # 右开：end 不计入

    def test_wrap_midnight(self):
        start, end = parse_hhmm("23:00"), parse_hhmm("06:00")
        self.assertTrue(in_time_window(parse_hhmm("23:30"), start, end))
        self.assertTrue(in_time_window(parse_hhmm("00:30"), start, end))
        self.assertTrue(in_time_window(parse_hhmm("05:59"), start, end))
        self.assertFalse(in_time_window(parse_hhmm("06:00"), start, end))
        self.assertFalse(in_time_window(parse_hhmm("12:00"), start, end))
        self.assertFalse(in_time_window(parse_hhmm("22:59"), start, end))

    def test_invalid_means_unrestricted(self):
        self.assertTrue(in_time_window(600, -1, 720))
        self.assertTrue(in_time_window(600, 540, -1))
        self.assertTrue(in_time_window(600, 540, 540))      # start==end → 全开


class TestWeekdayAllowed(unittest.TestCase):
    def test_basic(self):
        self.assertTrue(weekday_allowed("1,2,3,4,5", 1))    # 周一
        self.assertFalse(weekday_allowed("1,2,3,4,5", 6))   # 周六
        self.assertTrue(weekday_allowed("1,2,3,4,5,6,7", 7))
        self.assertTrue(weekday_allowed("", 7))             # 空 = 全允许
        self.assertTrue(weekday_allowed(None, 7))
        self.assertTrue(weekday_allowed("bad", 7))          # 非法 = 全允许

    def test_sunday(self):
        self.assertTrue(weekday_allowed("7", 7))
        self.assertFalse(weekday_allowed("7", 1))


class TestWindowState(unittest.TestCase):
    def test_realtime_always_open(self):
        c = cfg(**{"analysis.mode": "realtime"})
        for hour in (0, 6, 12, 23):
            st = window_state(c, datetime(2026, 9, 16, hour, 0))
            self.assertTrue(st["in_window"], f"{hour}:00 应恒开")
            self.assertFalse(st["waiting"])

    def test_offpeak_inside(self):
        st = window_state(cfg(), datetime(2026, 9, 16, 2, 0))
        self.assertEqual(st["mode"], "offpeak")
        self.assertTrue(st["in_window"])
        self.assertFalse(st["waiting"])

    def test_offpeak_outside(self):
        st = window_state(cfg(), datetime(2026, 9, 16, 14, 0))
        self.assertFalse(st["in_window"])
        self.assertTrue(st["waiting"])

    def test_hybrid_same_window_open(self):
        st = window_state(cfg(**{"analysis.mode": "hybrid"}),
                          datetime(2026, 9, 16, 1, 0))
        self.assertTrue(st["in_window"])

    def test_hybrid_outside_is_waiting(self):
        # hybrid 窗口外由 worker 按白天额度决定，window_state 本身报 waiting
        st = window_state(cfg(**{"analysis.mode": "hybrid"}),
                          datetime(2026, 9, 16, 15, 0))
        self.assertFalse(st["in_window"])
        self.assertTrue(st["waiting"])

    def test_workday_filter(self):
        # 2026-09-20 是周日
        st = window_state(cfg(**{"analysis.workdays": "1,2,3,4,5"}),
                          datetime(2026, 9, 20, 2, 0))
        self.assertFalse(st["workday_ok"])
        self.assertFalse(st["in_window"])

    def test_unknown_mode_does_not_block(self):
        st = window_state(cfg(**{"analysis.mode": "weird"}),
                          datetime(2026, 9, 16, 12, 0))
        self.assertTrue(st["in_window"], "未知模式必须保守放行，不能堵死队列")


class TestQuietHours(unittest.TestCase):
    def test_schedule_quiet_window(self):
        self.assertTrue(is_quiet_hours(cfg(), datetime(2026, 9, 16, 23, 0)))
        self.assertTrue(is_quiet_hours(cfg(), datetime(2026, 9, 16, 6, 0)))
        self.assertFalse(is_quiet_hours(cfg(), datetime(2026, 9, 16, 12, 0)))

    def test_disabled_when_empty(self):
        c = cfg(**{"schedule.quiet_hours_start": "", "schedule.quiet_hours_end": ""})
        self.assertFalse(is_quiet_hours(c, datetime(2026, 9, 16, 23, 0)))

    def test_alert_override(self):
        c = cfg(**{"alert.quiet_hours_start": "01:00", "alert.quiet_hours_end": "02:00"})
        self.assertTrue(alert_quiet_hours(c, datetime(2026, 9, 16, 1, 30)))
        # 01:30 不在 schedule 的 22:30-07:00 之外？—— 用 12:00 证明未继承
        self.assertFalse(alert_quiet_hours(c, datetime(2026, 9, 16, 12, 0)))

    def test_alert_inherits_schedule(self):
        c = cfg(**{"alert.quiet_hours_start": "", "alert.quiet_hours_end": ""})
        self.assertTrue(alert_quiet_hours(c, datetime(2026, 9, 16, 23, 30)))


class TestTimezone(unittest.TestCase):
    def test_tz_applied(self):
        dt = now_in("Asia/Shanghai", datetime(2026, 9, 16, 12, 0))
        self.assertEqual(dt.hour, 12)

    def test_invalid_tz_fallback(self):
        dt = now_in("Not/AZone", datetime(2026, 9, 16, 12, 0))
        self.assertEqual(dt.hour, 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
