#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_config_consistency.py — 配置一致性与运行期行为（C）测试
=============================================================================
覆盖：

  * `config/app.schema.json` 与 `config/app.yaml.example` **无悬空键**（双向）
  * schema 与 example 的 `version` 一致
  * 全部时间项（HH:MM）格式合法、枚举取值合法
  * 端口唯一性交叉校验（重复端口必须报错）
  * `tools/appconfig.py --render-timers` 能把 `schedule.*` **渲染进 timer**
    （「改配置即改行为」的可执行证据）
  * `--render-env` 能把端口/窗口渲染进 systemd 环境文件

运行::

    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PKG, "tools"))

import appconfig as AC                                       # noqa: E402


class TestSchemaExampleConsistency(unittest.TestCase):
    def test_no_dangling_keys(self):
        errs, warns = AC.check_example_keys()
        self.assertEqual(errs, [], "schema 与 example 必须键集合一致：\n" + "\n".join(errs))
        self.assertTrue(warns)

    def test_schema_is_valid_json(self):
        with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
            schema = json.load(f)
        self.assertIn("fields", schema)
        self.assertGreater(len(schema["fields"]), 100)

    def test_time_keys_present(self):
        """C 要求：全部时间项都在 schema 中声明。"""
        required = [
            "schedule.survey_sync_minutes", "schedule.diet_sync_minutes",
            "schedule.exercise_sync_minutes", "schedule.health_monitor_minutes",
            "schedule.daily_report_at", "schedule.backup_at",
            "schedule.timer_randomized_delay_seconds", "schedule.timer_accuracy_seconds",
            "schedule.timer_persistent", "schedule.quiet_hours_start",
            "schedule.quiet_hours_end", "schedule.quiet_hours_suppress_alerts",
            "schedule.quiet_hours_skip_sync", "schedule.quiet_hours_skip_backup",
            "alert.cooldown_minutes", "alert.stale_data_minutes",
            "selfcheck.http_timeout_seconds", "selfcheck.external_probe_interval_seconds",
            "analysis.mode", "analysis.window_start", "analysis.window_end",
            "analysis.timezone", "analysis.workdays", "analysis.throttle_seconds",
            "analysis.concurrency", "analysis.max_per_window",
            "webhook.retry_max_attempts", "webhook.retry_backoff_seconds",
            "webhook.retry_poll_seconds", "webhook.timer_only_after_minutes",
            "backup.timeout_minutes",
            "survey_platforms.scale.fetch_interval_minutes",
            "survey_platforms.diet.fetch_interval_minutes",
            "survey_platforms.exercise.fetch_interval_minutes",
        ]
        with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
            fields = json.load(f)["fields"]
        missing = [k for k in required if k not in fields]
        self.assertEqual(missing, [], f"schema 缺少时间项：{missing}")


class TestValidation(unittest.TestCase):
    def setUp(self):
        with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
            self.schema = json.load(f)
        self.example = AC._load_yaml(AC.EXAMPLE_CONFIG)

    def test_example_passes_except_placeholders(self):
        _res, errs, _w = AC.validate(self.example, self.schema, {},
                                     strict_placeholders=False)
        self.assertEqual(errs, [], f"example 不应有硬错误：{errs}")

    def test_bad_hhmm_rejected(self):
        raw = dict(self.example)
        raw = json.loads(json.dumps(self.example))
        raw["schedule"]["daily_report_at"] = "8:30 am"
        _res, errs, _w = AC.validate(raw, self.schema, {}, strict_placeholders=False)
        self.assertTrue(any("daily_report_at" in e for e in errs), errs)

    def test_duplicate_ports_rejected(self):
        raw = json.loads(json.dumps(self.example))
        raw["services"]["diet_webhook"]["port"] = raw["services"]["survey_feedback"]["port"]
        _res, errs, _w = AC.validate(raw, self.schema, {}, strict_placeholders=False)
        self.assertTrue(any("端口" in e for e in errs), errs)

    def test_bad_workdays_rejected(self):
        raw = json.loads(json.dumps(self.example))
        raw["analysis"]["workdays"] = "1,2,9"
        _res, errs, _w = AC.validate(raw, self.schema, {}, strict_placeholders=False)
        self.assertTrue(any("workdays" in e for e in errs), errs)

    def test_bad_analysis_mode_rejected(self):
        raw = json.loads(json.dumps(self.example))
        raw["analysis"]["mode"] = "nightly"
        _res, errs, _w = AC.validate(raw, self.schema, {}, strict_placeholders=False)
        self.assertTrue(any("analysis.mode" in e for e in errs), errs)


class TestRenderChangesBehavior(unittest.TestCase):
    """「改配置即改行为」的可执行证据：改 schedule.* → 渲染出的 OnCalendar 变化。"""

    def test_timer_oncalendar_follows_config(self):
        cfg = AC.flatten(AC._load_yaml(AC.EXAMPLE_CONFIG))
        self.assertEqual(
            AC.timer_oncalendar({**cfg, "schedule.survey_sync_minutes": 60},
                                "research-survey-sync.timer"), "*:0/60")
        self.assertEqual(
            AC.timer_oncalendar({**cfg, "schedule.survey_sync_minutes": 5},
                                "research-survey-sync.timer"), "*:0/5")
        self.assertEqual(
            AC.timer_oncalendar({**cfg, "schedule.daily_report_at": "06:15"},
                                "research-daily-report.timer"), "*-*-* 06:15:00")
        self.assertEqual(
            AC.timer_oncalendar({**cfg, "schedule.backup_at": "01:45"},
                                "research-rclone-backup.timer"), "*-*-* 01:45:00")

    def test_render_timers_writes_all_units(self):
        cfg = AC.flatten(AC._load_yaml(AC.EXAMPLE_CONFIG))
        with tempfile.TemporaryDirectory() as d:
            written = AC.render_timer_fragments(cfg, d)
            self.assertEqual(len(written), len(AC.TIMER_SPEC))
            for p in written:
                self.assertTrue(os.path.exists(p))
                body = open(p, encoding="utf-8").read()
                self.assertIn("OnCalendar=", body)
                self.assertIn("RandomizedDelaySec=", body)

    def test_render_timers_reflects_quiet_config(self):
        cfg = AC.flatten(AC._load_yaml(AC.EXAMPLE_CONFIG))
        cfg["schedule.timer_randomized_delay_seconds"] = 7
        with tempfile.TemporaryDirectory() as d:
            AC.render_timer_fragments(cfg, d)
            body = open(os.path.join(d, "research-survey-sync.timer.d",
                                     "10-schedule.conf"), encoding="utf-8").read()
        self.assertIn("RandomizedDelaySec=7", body)

    def test_render_env_contains_ports_and_window(self):
        resolved, errs, _ = AC.validate(AC._load_yaml(AC.EXAMPLE_CONFIG),
                                        json.load(open(AC.DEFAULT_SCHEMA, encoding="utf-8")),
                                        {}, strict_placeholders=False)
        self.assertEqual(errs, [])
        env = AC.to_env(resolved, include_secrets=False)
        self.assertEqual(env["PORT_SURVEY_WEBHOOK"], "9877")
        self.assertEqual(env["PORT_DIET_WEBHOOK"], "9876")
        self.assertEqual(env["PORT_SURVEY_FEEDBACK"], "8000")
        self.assertEqual(env["PORT_SURVEY_FEEDBACK_ALT"], "8080")
        self.assertEqual(env["ANALYSIS_MODE"], "realtime")
        self.assertEqual(env["SCHEDULE_SURVEY_SYNC_MINUTES"], "60")
        self.assertNotIn("SMTP_PASSWORD", env, "非密钥模式下不得输出密钥")

    def test_secret_keys_never_in_plain_env(self):
        resolved, _e, _w = AC.validate(AC._load_yaml(AC.EXAMPLE_CONFIG),
                                       json.load(open(AC.DEFAULT_SCHEMA, encoding="utf-8")),
                                       {}, strict_placeholders=False)
        env = AC.to_env(resolved, include_secrets=False)
        for k in AC.SECRET_KEYS:
            name = AC.ENV_MAP.get(k)
            if name:
                self.assertNotIn(name, env, f"密钥 {k} 不应出现在非密钥环境变量中")


if __name__ == "__main__":
    unittest.main(verbosity=2)
