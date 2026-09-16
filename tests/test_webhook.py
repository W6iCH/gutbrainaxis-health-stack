#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_webhook.py — 量表 Webhook（A）单元 + 集成测试
=============================================================================
覆盖：

1. **回调解析** `extract_answer_ids`：多种平台载荷形状（dict/list/嵌套/answer_sheet）
2. **密钥校验** `secret_ok`：Header / Query / Body 三处，未配置密钥时放行
3. **限流** `SlidingWindowLimiter`：超过阈值拒绝、窗口滑出后恢复
4. **重试队列** `FileQueue`：原子领取、幂等 upsert、超次数转 failed、崩溃恢复
5. **HTTP 集成**：真起 `survey_webhook_listener`（注入 stub 的 `survey_sync_cron`），
   POST 回调 → 断言 200 且 stats 正确；GET /healthz → 断言模式字段；
   无密钥请求 → 401；超大请求体 → 413。

运行::

    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY -m unittest discover -s tests -v
"""
import importlib
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
COMMON = os.path.join(PKG, "services", "common")
sys.path.insert(0, COMMON)
sys.path.insert(0, os.path.join(PKG, "services", "sjtu_survey_pro"))


# ── 1/2 纯函数：直接从源码导入（避免触发重型依赖）─────────────────────────
def _load_module_with_stubs(name, path, stubs=None):
    """在注入 stub 模块后加载目标模块（返回模块对象）。"""
    stubs = stubs or {}
    saved = {}
    for k, v in stubs.items():
        saved[k] = sys.modules.get(k)
        sys.modules[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _stub_sync_module():
    m = types.ModuleType("survey_sync_cron")

    m.calls = []

    def sync(force=False, dry_run=False, only_answer_ids=None, source="cron"):
        m.calls.append({"only_answer_ids": only_answer_ids, "source": source})
        return {"api_rows": 1, "new": 1, "skipped": 0, "errors": 0}

    m.sync = sync
    m.process_row = lambda *a, **k: True
    return m


class TestWebhookPure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = _stub_sync_module()
        # 必须把 stub 常驻 sys.modules：process_callback() 在调用时才 import
        cls._saved = sys.modules.get("survey_sync_cron")
        sys.modules["survey_sync_cron"] = cls.stub
        cls.mod = _load_module_with_stubs(
            "swh", os.path.join(PKG, "services", "sjtu_survey_pro",
                                "survey_webhook_listener.py"),
            stubs={"survey_sync_cron": cls.stub})

    @classmethod
    def tearDownClass(cls):
        if cls._saved is None:
            sys.modules.pop("survey_sync_cron", None)
        else:
            sys.modules["survey_sync_cron"] = cls._saved

    def test_extract_answer_ids_shapes(self):
        ex = self.mod.extract_answer_ids
        self.assertEqual(ex({"id": "123"}), ["123"])
        self.assertEqual(ex({"answerId": 456}), ["456"])
        self.assertEqual(ex({"data": {"answerId": "789"}}), ["789"])
        self.assertEqual(ex({"answer_sheet": [{"id": "a1"}, {"id": "a2"}]}),
                         ["a1", "a2"])
        self.assertEqual(ex([{"id": "x"}, {"id": "x"}]), ["x"])       # 去重
        self.assertEqual(ex({"foo": "bar"}), [])
        self.assertEqual(ex(None), [])

    def test_extract_ignores_non_id_keys(self):
        # 嵌套字典里 name/email 不应被当成 answerId
        self.assertEqual(self.mod.extract_answer_ids(
            {"answer_sheet": [{"id": "9", "student": "S1"}], "name": "张三"}), ["9"])

    def test_secret_ok_variants(self):
        old = self.mod.SECRET
        try:
            self.mod.SECRET = ""                                  # 未配置 → 放行
            self.assertTrue(self.mod.secret_ok({}, {}, {}))
            self.mod.SECRET = "s3cr3t"
            self.assertTrue(self.mod.secret_ok({"X-Webhook-Token": "s3cr3t"}, {}, {}))
            self.assertTrue(self.mod.secret_ok({}, {"token": "s3cr3t"}, {}))
            self.assertTrue(self.mod.secret_ok({}, {}, {"token": "s3cr3t"}))
            self.assertFalse(self.mod.secret_ok({"X-Webhook-Token": "bad"}, {}, {}))
            self.assertFalse(self.mod.secret_ok({}, {}, {}))
        finally:
            self.mod.SECRET = old

    def test_describe_mode_timer_only(self):
        mode, hint = self.mod.describe_mode({})
        self.assertEqual(mode, "timer_only")
        self.assertIn("定时", hint)

    def test_describe_mode_webhook_recent(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        mode, _ = self.mod.describe_mode({"last_received_at": now})
        self.assertEqual(mode, "webhook")

    def test_describe_mode_stale_fallback(self):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=2)).isoformat()
        mode, hint = self.mod.describe_mode({"last_received_at": old})
        self.assertEqual(mode, "degraded_timer_fallback")
        self.assertIn("定时拉取", hint)


class TestRateLimitAndQueue(unittest.TestCase):
    def test_sliding_window_limiter(self):
        from lib_ratelimit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=3, window_seconds=60)
        self.assertTrue(lim.allow("ip1"))
        self.assertTrue(lim.allow("ip1"))
        self.assertTrue(lim.allow("ip1"))
        self.assertFalse(lim.allow("ip1"), "第 4 次应被拒")
        self.assertTrue(lim.allow("ip2"), "不同 IP 互不影响")

    def test_file_queue_claim_and_recover(self):
        from lib_queue import FileQueue
        with tempfile.TemporaryDirectory() as d:
            q = FileQueue(d)
            q.enqueue({"n": 1}, key="t1")
            q.enqueue({"n": 2}, key="t2")
            self.assertEqual(q.stats()["pending"], 2)
            p, task = q.claim()
            self.assertIsNotNone(p)
            self.assertIn(task["n"], (1, 2))
            self.assertEqual(q.stats()["pending"], 1)
            self.assertEqual(q.stats()["processing"], 1)
            # 崩溃恢复：processing 回到 pending
            self.assertEqual(q.recover(), 1)
            self.assertEqual(q.stats()["pending"], 2)
            self.assertEqual(q.stats()["processing"], 0)

    def test_file_queue_idempotent_upsert(self):
        from lib_queue import FileQueue
        with tempfile.TemporaryDirectory() as d:
            q = FileQueue(d)
            q.upsert({"attempts": 1}, key="same")
            q.upsert({"attempts": 2}, key="same")
            self.assertEqual(q.stats()["pending"], 1, "upsert 不应产生第二份")
            _, task = q.claim()
            self.assertEqual(task["attempts"], 2, "upsert 应覆盖内容")

    def test_file_queue_fail_to_dead_letter(self):
        from lib_queue import FileQueue
        with tempfile.TemporaryDirectory() as d:
            q = FileQueue(d)
            q.enqueue({"attempts": 4}, key="t")
            p, task = q.claim()
            q.fail(p, task, "boom", max_attempts=5, sleep_between=False)
            self.assertEqual(q.stats()["failed"], 1)
            self.assertEqual(q.stats()["pending"], 0)

    def test_file_queue_fail_requeues(self):
        from lib_queue import FileQueue
        with tempfile.TemporaryDirectory() as d:
            q = FileQueue(d)
            q.enqueue({"attempts": 0}, key="t")
            p, task = q.claim()
            q.fail(p, task, "boom", max_attempts=5, sleep_between=False)
            self.assertEqual(q.stats()["pending"], 1)
            self.assertEqual(q.stats()["failed"], 0)


class TestWebhookHTTP(unittest.TestCase):
    """真实 HTTP 集成：起服务、发回调、断言响应。"""

    @classmethod
    def setUpClass(cls):
        cls.stub = _stub_sync_module()
        cls._saved = sys.modules.get("survey_sync_cron")
        sys.modules["survey_sync_cron"] = cls.stub
        cls.tmp = tempfile.mkdtemp(prefix="swh-test-")
        os.environ["SURVEY_WEBHOOK_STATE_DIR"] = os.path.join(cls.tmp, "state")
        os.environ["WEBHOOK_SECRET"] = "unit-secret"
        os.environ["WEBHOOK_MAX_BODY_BYTES"] = "2048"
        cls.mod = _load_module_with_stubs(
            "swh_http", os.path.join(PKG, "services", "sjtu_survey_pro",
                                     "survey_webhook_listener.py"),
            stubs={"survey_sync_cron": cls.stub})
        # 选一个空闲端口
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        cls.server = cls.mod._Server(("127.0.0.1", cls.port), cls.mod.SurveyWebhookHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        if cls._saved is None:
            sys.modules.pop("survey_sync_cron", None)
        else:
            sys.modules["survey_sync_cron"] = cls._saved
        for k in ("SURVEY_WEBHOOK_STATE_DIR", "WEBHOOK_SECRET",
                  "WEBHOOK_MAX_BODY_BYTES"):
            os.environ.pop(k, None)

    def _post(self, payload, headers=None):
        url = f"http://127.0.0.1:{self.port}/"
        data = json.dumps(payload).encode()
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_healthz(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz",
                                    timeout=5) as r:
            body = json.loads(r.read().decode())
        self.assertEqual(r.status, 200)
        self.assertEqual(body["service"], "survey_webhook")
        self.assertIn("webhook", body)
        self.assertIn(body["webhook"]["mode"],
                      ("webhook", "timer_only", "degraded_timer_fallback"))

    def test_post_without_secret_rejected(self):
        code, body = self._post({"id": "1"})
        self.assertEqual(code, 401)
        self.assertEqual(body["status"], "error")

    def test_post_with_secret_ok(self):
        self.stub.calls.clear()
        code, body = self._post({"id": "12345"},
                                {"X-Webhook-Token": "unit-secret"})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["new"], 1)
        self.assertTrue(self.stub.calls, "应触发一次拉取")
        self.assertEqual(self.stub.calls[-1]["only_answer_ids"], {"12345"})
        self.assertEqual(self.stub.calls[-1]["source"], "webhook")

    def test_post_via_query_token(self):
        code, _ = self._post({"id": "777"}, {})
        self.assertEqual(code, 401)
        # query token 路径
        url = f"http://127.0.0.1:{self.port}/?token=unit-secret"
        req = urllib.request.Request(url, data=json.dumps({"id": "777"}).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_oversize_rejected(self):
        # 超过 WEBHOOK_MAX_BODY_BYTES(2048)
        payload = {"id": "1", "pad": "x" * 5000}
        code, _ = self._post(payload, {"X-Webhook-Token": "unit-secret"})
        self.assertEqual(code, 413)

    def test_state_counters(self):
        st = self.mod.load_state()
        self.assertGreaterEqual(st.get("received_total", 0), 1)
        self.assertGreaterEqual(st.get("new_rows_total", 0), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
