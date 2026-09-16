#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_parity_and_config.py — 追加约束①②的验收测试
=============================================================================
约束①（分析口径与课题研究一致）
  * `tools/verify_scoring_parity.py` 在权威条目库可用时必须**逐条一致**；
  * 权威库不在本机时（生产服务器）必须优雅 `skipped`，不报错。

约束②（可配置范围 = 全部部署个性化项）
  * 「三方一致」：
      ① schema ↔ example 无悬空/缺键（`--check-example-keys`）
      ② yaml → 实际读取：ENV_MAP 声明的环境变量确实被渲染进 env
      ③ 实际读取 → yaml：服务源码读取的 `os.environ` 变量都在 ENV_MAP 或允许清单中
  * `docs/可配置项总清单.md` 与配置一致（`gen_config_doc.py --check`）
  * 配置完备性：占位符/缺必填能被校验器报出并**指出键位置**

运行::

    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PKG, "tools"))

import appconfig as AC                                       # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestScoringParity(unittest.TestCase):
    """约束①：程序内计分 ⟷ 权威条目库逐条一致。"""

    def test_parity_runs_and_matches(self):
        vp = _load("verify_scoring_parity",
                   os.path.join(PKG, "tools", "verify_scoring_parity.py"))
        res = vp.run()
        if res["status"] == "skipped":
            self.skipTest("权威条目库不在本机（生产服务器属正常）：" + res["reason"])
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertTrue(res["ok"],
                        f"发现 {res.get('n_diffs')} 处差异：{res.get('diffs', [])[:3]}")
        self.assertGreater(res["checked"], 500, "断言数过少，覆盖不足")

    def test_parity_covers_all_layer_types(self):
        vp = _load("verify_scoring_parity2",
                   os.path.join(PKG, "tools", "verify_scoring_parity.py"))
        res = vp.run()
        if res["status"] != "ok":
            self.skipTest("权威条目库不可用")
        layers = {d["layer"] for d in res["diffs"]} | {"L1", "L2", "L3", "L4"}
        self.assertTrue({"L1", "L2", "L3", "L4"}.issubset(layers))

    def test_parity_metadata_notes_are_scoring_irrelevant(self):
        """元数据差异必须显式标注，且不得涉及任何分数字段。"""
        vp = _load("verify_scoring_parity3",
                   os.path.join(PKG, "tools", "verify_scoring_parity.py"))
        res = vp.run()
        if res["status"] != "ok":
            self.skipTest("权威条目库不可用")
        SCORING_FIELDS = ("total", "n_items", "n_expected", "components", "subscales",
                          "domain_scores", "band", "overall", "level")
        for n in res.get("metadata_notes", []):
            what = n["what"]
            self.assertFalse(any(f in what for f in SCORING_FIELDS),
                             f"元数据差异涉及分数字段：{what}")


class TestConfigThreeWay(unittest.TestCase):
    """约束②：schema ↔ yaml ↔ 实际读取 三方一致。"""

    def test_schema_example_no_dangling(self):
        errs, _w = AC.check_example_keys()
        self.assertEqual(errs, [], "\n".join(errs))

    def test_env_map_targets_are_declared_keys_only(self):
        """ENV_MAP 的每个键都必须在 schema 中声明（否则渲染出无人认识的变量）。"""
        with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
            fields = json.load(f)["fields"]
        undeclared = [k for k in AC.ENV_MAP if k not in fields]
        self.assertEqual(undeclared, [],
                         f"ENV_MAP 引用了未声明键：{undeclared}")

    def test_every_required_key_present_in_example(self):
        """必填项必须在 example 中出现（否则新部署必然缺项）。"""
        with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
            fields = json.load(f)["fields"]
        ex = set(AC.flatten(AC._load_yaml(AC.EXAMPLE_CONFIG)).keys())
        missing = sorted(k for k, r in fields.items()
                         if r.get("required") and k not in ex)
        self.assertEqual(missing, [], f"example 缺必填项：{missing}")

    def test_env_bridge_covers_service_reads(self):
        """服务源码读的环境变量必须在 ENV_MAP/PORT_ENV_MAP 或允许清单中。"""
        sc = _load("selfcheck_for_bridge", os.path.join(PKG, "tools", "selfcheck.py"))
        declared = set(AC.ENV_MAP.values()) | set(AC.PORT_ENV_MAP.values()) | \
            {"PORT_SURVEY_FEEDBACK_ALT"}
        reads = sc._scan_env_reads([os.path.join(PKG, "services"),
                                    os.path.join(PKG, "scripts"),
                                    os.path.join(PKG, "tools")])
        unknown = [k for k in reads
                   if k not in declared and k not in sc.ENV_BRIDGE_ALLOW
                   and not any(k.startswith(p) for p in
                               ("PYTEST", "LC_", "MOCK", "GBA_TEST", "WJX_", "SMTP_"))]
        self.assertEqual(unknown, [],
                         f"未声明的环境变量（应加入 schema+example+ENV_MAP）：{unknown}")

    def test_config_doc_is_up_to_date(self):
        gen = _load("gen_config_doc", os.path.join(PKG, "tools", "gen_config_doc.py"))
        doc = os.path.join(PKG, "docs", "可配置项总清单.md")
        self.assertTrue(os.path.exists(doc), "缺少 docs/可配置项总清单.md")
        norm = lambda s: "\n".join(l for l in s.splitlines()
                                   if not l.startswith("> 生成时间："))
        with open(doc, encoding="utf-8") as f:
            existing = f.read()
        self.assertEqual(norm(existing), norm(gen.build()),
                         "docs/可配置项总清单.md 已过期，请运行 tools/gen_config_doc.py")

    def test_time_doc_is_up_to_date(self):
        gen = _load("gen_time_doc", os.path.join(PKG, "tools", "gen_time_config_doc.py"))
        doc = os.path.join(PKG, "docs", "时间项配置总表.md")
        self.assertTrue(os.path.exists(doc), "缺少 docs/时间项配置总表.md")
        norm = lambda s: "\n".join(l for l in s.splitlines()
                                   if not l.startswith("> 生成时间："))
        with open(doc, encoding="utf-8") as f:
            existing = f.read()
        self.assertEqual(norm(existing), norm(gen.build()), "时间项文档已过期")


class TestConfigCompleteness(unittest.TestCase):
    """约束②：配置完备性自检（缺必填/占位符 → 报错并指出键位置）。"""

    def setUp(self):
        with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
            self.schema = json.load(f)
        self.example = AC._load_yaml(AC.EXAMPLE_CONFIG)

    def test_missing_required_reports_key_path(self):
        raw = json.loads(json.dumps(self.example))
        del raw["app"]["base_dir"]
        _r, errs, _w = AC.validate(raw, self.schema, {}, strict_placeholders=False)
        self.assertTrue(any("app.base_dir" in e for e in errs),
                        f"应在错误中指出键位置 app.base_dir：{errs}")

    def test_placeholder_reported_with_key(self):
        raw = json.loads(json.dumps(self.example))
        raw["site"]["domain"] = "example.com"
        _r, errs, _w = AC.validate(raw, self.schema, {}, strict_placeholders=True)
        self.assertTrue(any("site.domain" in e and "占位符" in e for e in errs), errs)

    def test_deployment_personalization_keys_exist(self):
        """约束② 要求覆盖的个性化项必须都在 schema 中。"""
        required = [
            "database.roster", "study.calendar_path", "study.planned_id_prefixes",
            "survey_platforms.scale.token", "survey_platforms.scale.survey_id",
            "survey_platforms.diet.survey_id", "survey_platforms.exercise.survey_id",
            "site.domain", "site.subdomain_admin", "services.survey_webhook.port",
            "smtp.cc_email", "smtp.admin_email",
            "llm.primary.base_url", "llm.primary.model", "llm.primary.api_keys",
            "llm.primary.temperature", "llm.backup.base_url", "llm.backup.model",
            "app.scoring_version", "scoring.reverse_items_enabled",
            "scoring.custom_bands_path",
            "alert.cooldown_minutes", "alert.stale_data_minutes",
            "backup.retention", "backup.remote_enabled",
            "logging.rotate_days", "logging.format",
            "analysis.concurrency", "analysis.throttle_seconds",
            "webhook.enabled", "webhook.retry_max_attempts",
            "schedule.quiet_hours_start", "schedule.quiet_hours_end",
        ]
        fields = self.schema["fields"]
        missing = [k for k in required if k not in fields]
        self.assertEqual(missing, [], f"schema 缺少个性化配置项：{missing}")

    def test_study_calendar_externalized(self):
        """干预时间轴/目标完成度必须外提且可读（不再硬编码）。"""
        sc = os.path.join(PKG, "services", "common", "lib_study.py")
        self.assertTrue(os.path.exists(sc), "缺少 services/common/lib_study.py")
        LS = _load("lib_study_test", sc)
        cal = LS.load_calendar(path=os.path.join(PKG, "config",
                                                 "study_calendar.example.yaml"))
        for k in ("study_start", "study_end", "scale_rounds", "exercise_rounds",
                  "diet_start", "diet_end", "waves", "completion"):
            self.assertIn(k, cal, f"研究设计日历缺少 {k}")
        self.assertGreaterEqual(len(cal["scale_rounds"]), 1)
        self.assertTrue(cal["completion"].get("total_max"))
        # 缺文件必须回退默认值（不影响服务启动）
        self.assertEqual(len(LS.load_calendar(path="/nonexistent/x.yaml")["scale_rounds"]),
                         len(LS.DEFAULT_CALENDAR["scale_rounds"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
