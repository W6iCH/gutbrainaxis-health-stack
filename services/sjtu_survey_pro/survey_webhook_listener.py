#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
量表问卷 Webhook 接收器（research-survey-webhook）
=====================================================
角色
----
问卷平台（wj.sjtu.edu.cn）在有人提交**量表问卷**时回调本服务，本服务**立即**
拉取该条答卷 → 计分 → 入邮件队列，把「最长滞后 1 小时」压到「秒级」。

与定时拉取的关系（**双通道，webhook 为主、定时为兜底**）
--------------------------------------------------------
* webhook 只负责「**触发**」：收到事件后立刻调用 `survey_sync_cron.sync()`；
* 真正的「收数→计分→入库→邮件」逻辑**只有一份**，即 `survey_sync_cron.process_row()`；
* 幂等：写入层 `submissions.submission_id` 为 UNIQUE，重复投递直接跳过；
  定时拉取与 webhook 同时到达也不会产生重复行。
* 兜底：`research-survey-sync.timer`（默认每小时）**始终启用**。若平台不支持
  回调、回调丢失、或被网络阻断，定时任务仍会补上——**丢失事件不会丢数据**。

平台是否支持回调（**如何启用 / 如何验证**）
------------------------------------------
1. 登录问卷平台 → 问卷「设置」→「数据推送 / Webhook」；
2. 推送地址填：`${PUBLIC_SCHEME}://${SITE_DOMAIN}/survey-webhook`（由 Nginx 反代到本服务）；
3. 若平台支持自定义 Header/密钥，填 `survey_platforms.scale.webhook_secret` 的值；
4. 在平台上**提交一份测试答卷**，随后执行：
   `curl -s http://127.0.0.1:${PORT}/healthz | python3 -m json.tool`
   查看 `webhook.last_received_at` 是否刷新、`webhook.received_total` 是否 +1；
5. 若平台**不支持回调**：本服务可保持运行（无副作用），系统自动**完全依赖定时拉取**；
   `GET /healthz` 会返回 `webhook.mode="timer_only"` 并给出提示；
   自检项 `config.webhook` 亦会报告「从未收到回调」。

安全
----
* 可选共享密钥校验（Header `X-Webhook-Token` / Query `?token=` / Body `secret`）；
* 请求体大小上限（`services.survey_webhook.max_body_bytes`）；
* 单 IP 简单限速（`services.survey_webhook.rate_limit_per_minute`）；
* 失败重试：平台重投 + 本地重试队列（`.webhook_queue/retry/`）+ 定时金兜底。

用法::

    python3 survey_webhook_listener.py [port]
环境变量: PORT_SURVEY_WEBHOOK / WEBHOOK_SECRET / WEBHOOK_MAX_BODY_BYTES /
          WEBHOOK_RATE_LIMIT_PER_MINUTE / SURVEY_WEBHOOK_STATE_DIR
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 公共运行期设施（优雅停机 / /healthz 结构）+ 公共安全与队列（单一真源）
sys.path.insert(0, os.path.join(os.path.dirname(BASE_DIR), "common"))
try:
    from lib_runtime import install_signal_handlers, healthz_payload, STOP  # noqa: E402
except Exception:                                            # pragma: no cover
    class _Stop:                                             # type: ignore
        def is_set(self):
            return False
    STOP = _Stop()                                           # type: ignore

    def install_signal_handlers(*a, **kw):                   # type: ignore
        pass

    def healthz_payload(service, extra=None, checks=None, ok=True):  # type: ignore
        return {"status": "ok" if ok else "degraded", "service": service,
                "checks": checks or {}, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                **(extra or {})}

try:
    from lib_ratelimit import SlidingWindowLimiter
    from lib_queue import FileQueue, count_json
    from lib_validate import validate_json_payload, sanitize_log
    from lib_errors import log_exception
except Exception:                                            # pragma: no cover
    class SlidingWindowLimiter:                              # type: ignore
        def __init__(self, limit=60, window_seconds=60):
            pass

        def allow(self, key):
            return True

    class FileQueue:                                         # type: ignore
        def __init__(self, root, name="queue"):
            self.pending = root

        def stats(self):
            return {"pending": 0}

    def count_json(path):                                    # type: ignore
        return 0

    def validate_json_payload(payload, max_bytes=0, max_depth=8):  # type: ignore
        return True, ""

    def sanitize_log(v, max_len=200):                        # type: ignore
        return str(v)[:max_len]

    def log_exception(logger, exc, ctx=None, level="error"):  # type: ignore
        print(f"ERROR {exc}")

CST = timezone(timedelta(hours=8))

# ── 配置（全部来自 app.yaml → systemd 环境文件）─────────────────────────────
PORT = int(os.environ.get("PORT_SURVEY_WEBHOOK", "9877"))
SECRET = os.environ.get("WEBHOOK_SECRET", "") or os.environ.get("SURVEY_WEBHOOK_SECRET", "")
MAX_BODY = int(os.environ.get("WEBHOOK_MAX_BODY_BYTES", "1048576"))       # 1 MiB
RATE_LIMIT = int(os.environ.get("WEBHOOK_RATE_LIMIT_PER_MINUTE", "120"))
STATE_DIR = os.environ.get("SURVEY_WEBHOOK_STATE_DIR",
                           os.path.join(BASE_DIR, ".webhook_queue"))
RETRY_DIR = os.path.join(STATE_DIR, "retry")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE = os.path.join(STATE_DIR, "webhook.log")
os.makedirs(RETRY_DIR, exist_ok=True)

# 未收到回调多久后判定「平台可能不支持回调」（分钟）
TIMER_ONLY_AFTER_MINUTES = int(os.environ.get("WEBHOOK_TIMER_ONLY_AFTER_MINUTES", "180"))
# 本地重试队列最大尝试次数
RETRY_MAX_ATTEMPTS = int(os.environ.get("WEBHOOK_RETRY_MAX_ATTEMPTS", "5"))
# 重试退避基数（秒）
RETRY_BACKOFF_SECONDS = int(os.environ.get("WEBHOOK_RETRY_BACKOFF_SECONDS", "60"))

_STATE_LOCK = threading.Lock()
# 单 IP 限流（滑动窗口，实现见 services/common/lib_ratelimit.py）
_LIMITER = SlidingWindowLimiter(limit=RATE_LIMIT, window_seconds=60)
# 失败重试队列（实现见 services/common/lib_queue.py）
RETRY_Q = FileQueue(RETRY_DIR, name="survey-webhook-retry")


# ── 状态与日志 ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def log(msg: str, level: str = "INFO"):
    line = json.dumps({"ts": _now_iso(), "level": level, "service": "survey-webhook",
                       "msg": msg}, ensure_ascii=False)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict):
    """原子写：临时文件 + rename，避免半写状态被读到。"""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def bump_state(**kw) -> dict:
    """幂等安全的状态累加（加锁 + 原子写）。"""
    with _STATE_LOCK:
        st = load_state()
        for k, v in kw.items():
            if k.endswith("__inc"):
                st[k[:-5]] = int(st.get(k[:-5], 0)) + int(v)
            elif k.endswith("__add"):
                st[k[:-5]] = float(st.get(k[:-5], 0)) + float(v)
            else:
                st[k] = v
        save_state(st)
        return st


def describe_mode(state: dict) -> tuple:
    """返回 (mode, hint)。timer_only 表示未收到过回调或已超时。"""
    last = state.get("last_received_at")
    if not last:
        return "timer_only", ("尚未收到任何回调；量表数据完全由定时拉取（每 "
                              f"{os.environ.get('SURVEY_SYNC_MINUTES', '60')} 分钟）提供。"
                              "若问卷平台支持 Webhook，请按 docs/Webhook配置与验证.md 启用。")
    try:
        last_dt = datetime.fromisoformat(last)
        idle_min = (datetime.now(CST) - last_dt).total_seconds() / 60
    except ValueError:
        return "unknown", "回调时间戳无法解析"
    if idle_min > TIMER_ONLY_AFTER_MINUTES:
        return "degraded_timer_fallback", (
            f"已 {int(idle_min)} 分钟未收到回调（阈值 {TIMER_ONLY_AFTER_MINUTES} 分钟）；"
            "系统自动回落定时拉取，数据不会丢失。请检查平台回调配置与 Nginx 反代。")
    return "webhook", f"最近一次回调 {last}（{int(idle_min)} 分钟前）"


# ── 限速（单 IP 滑动窗口）─────────────────────────────────────────────────

def rate_limited(ip: str) -> bool:
    """单 IP 滑窗限流（返回 True 表示应拒绝）。"""
    return not _LIMITER.allow(ip)


# ── 回调解析 ──────────────────────────────────────────────────────────────

def extract_answer_ids(payload) -> list:
    """
    从平台回调体里尽力抽取 answerId / submission_id。
    平台回调结构不统一，这里做**宽松**抽取：只要出现 id 类字段就纳入候选，
    最终由 `survey_sync_cron.sync(only_answer_ids=...)` 定点过滤 + 全量比对兜底。
    """
    ids: list = []
    ID_KEYS = {"id", "answerId", "answer_id", "submission_id", "submissionId",
               "sid", "recordId", "record_id", "responseId", "response_id"}

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ID_KEYS and isinstance(v, (str, int)) and str(v).strip():
                    ids.append(str(v).strip())
                elif k == "answer_sheet" or k == "answers" or k == "data" or isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for it in node:
                walk(it, depth + 1)

    walk(payload)
    # 去重且保持顺序
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def secret_ok(headers, query: dict, payload) -> bool:
    if not SECRET:
        return True
    cands = [headers.get("X-Webhook-Token", ""), headers.get("X-Webhook-Secret", ""),
             query.get("token", "")]
    if isinstance(payload, dict):
        for k in ("secret", "token", "webhook_token"):
            v = payload.get(k)
            if isinstance(v, str):
                cands.append(v)
    return any(c and c == SECRET for c in cands)


# ── 核心：拉取 + 计分 + 入队（复用 survey_sync_cron）──────────────────────

def process_callback(answer_ids: list, source: str = "webhook") -> dict:
    """立即处理回调指向的答卷。返回统计。异常不外抛（隔离失败）。"""
    import survey_sync_cron as sync_mod
    only = set(answer_ids) if answer_ids else None
    try:
        stats = sync_mod.sync(force=False, dry_run=False,
                              only_answer_ids=only, source=source)
        return stats or {}
    except Exception as e:                                   # noqa: BLE001
        log(f"处理回调失败: {e}", level="ERROR")
        return {"error": str(e)}


def enqueue_retry(answer_ids: list, error: str):
    """把失败的回调放入本地重试队列（幂等：同一批 id 只保留一份，attempts 递增）。"""
    key = ",".join(sorted(set(answer_ids))) or f"empty-{int(time.time())}"
    safe = "".join(c for c in key if c.isalnum() or c in ",-")[:120] or str(int(time.time()))
    prev = 0
    p = os.path.join(RETRY_Q.pending, f"{safe}.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                prev = int(json.load(f).get("attempts", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            prev = 0
    RETRY_Q.upsert({"answer_ids": answer_ids, "attempts": prev + 1,
                    "last_error": sanitize_log(error, 500),
                    "updated_at": _now_iso(),
                    "next_try_after": time.time() + RETRY_BACKOFF_SECONDS * (prev + 1)},
                   key=safe)


def drain_retry_queue() -> int:
    """重试队列消费：到期且未超次数的重试；超次数转 dead（*.json.dead）。"""
    done = 0
    now = time.time()
    for fname in sorted(os.listdir(RETRY_Q.pending)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(RETRY_Q.pending, fname)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            os.replace(path, path + ".dead")
            continue
        if float(rec.get("next_try_after", 0)) > now:
            continue
        if int(rec.get("attempts", 1)) > RETRY_MAX_ATTEMPTS:
            os.replace(path, path + ".dead")
            log(f"重试超次数，转入 dead: {fname}", level="WARNING")
            continue
        stats = process_callback(rec.get("answer_ids") or [], source="webhook-retry")
        if "error" in stats:
            enqueue_retry(rec.get("answer_ids") or [], stats["error"])
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            try:
                os.remove(path)
            except OSError:
                pass
            done += 1
    return done


def retry_loop():
    while not STOP.is_set():
        try:
            drain_retry_queue()
        except Exception as e:                               # noqa: BLE001
            log(f"重试循环异常: {e}", level="ERROR")
        for _ in range(30):
            if STOP.is_set():
                return
            time.sleep(1)


# ── HTTP ─────────────────────────────────────────────────────────────────

class SurveyWebhookHandler(BaseHTTPRequestHandler):
    server_version = "SurveyWebhook/1.0"
    protocol_version = "HTTP/1.1"

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd else None) or self.client_address[0]

    def do_POST(self):                                        # noqa: N802
        ip = self._client_ip()
        if rate_limited(ip):
            log(f"限速拒绝 ip={ip}", level="WARNING")
            return self._json(429, {"status": "error", "message": "rate limited"})

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._json(400, {"status": "error", "message": "bad content-length"})
        if length > MAX_BODY:
            log(f"请求体过大 {length}>{MAX_BODY} ip={ip}", level="WARNING")
            return self._json(413, {"status": "error", "message": "payload too large"})

        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"_raw": raw.decode("utf-8", "replace")[:2000]}

        # 输入校验（大小/深度/形状）——拒绝深度炸弹与超大对象
        ok, reason = validate_json_payload(payload, max_bytes=MAX_BODY)
        if not ok:
            log(f"回调载荷非法 ip={ip}: {reason}", level="WARNING")
            return self._json(400, {"status": "error", "message": reason})

        query = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    query[k] = v

        bump_state(received_total__inc=1, last_ip=ip,
                   last_received_at=_now_iso())

        if not secret_ok(self.headers, query, payload):
            bump_state(rejected_total__inc=1)
            log(f"密钥校验失败 ip={ip}", level="WARNING")
            return self._json(401, {"status": "error", "message": "invalid token"})

        answer_ids = extract_answer_ids(payload)
        log(f"收到回调 ip={ip} 候选 answerId={answer_ids[:5]}"
            f"{'...' if len(answer_ids) > 5 else ''} (n={len(answer_ids)})")

        stats = process_callback(answer_ids, source="webhook")
        if "error" in stats:
            bump_state(failed_total__inc=1)
            enqueue_retry(answer_ids, stats["error"])
            # 仍返回 200：避免平台持续重投造成风暴；本地重试队列 + 定时任务兜底
            return self._json(200, {"status": "accepted_with_warning",
                                    "detail": stats["error"]})

        new_n = int(stats.get("new", 0) or 0)
        bump_state(processed_total__inc=1, new_rows_total__inc=new_n,
                   last_new_rows=new_n)
        return self._json(200, {"status": "ok", "accepted": len(answer_ids),
                                "new": new_n, "skipped": stats.get("skipped", 0),
                                "timestamp": _now_iso()})

    def do_GET(self):                                         # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/health", "/ping"):
            state = load_state()
            mode, hint = describe_mode(state)
            checks = {"state_dir_writable": os.access(STATE_DIR, os.W_OK),
                      "webhook_mode": mode}
            return self._json(200,
                              healthz_payload("survey_webhook",
                                              extra={"webhook": {
                                                  "mode": mode, "hint": hint,
                                                  "received_total": state.get("received_total", 0),
                                                  "new_rows_total": state.get("new_rows_total", 0),
                                                  "failed_total": state.get("failed_total", 0),
                                                  "last_received_at": state.get("last_received_at"),
                                                  "retry_pending": count_json(RETRY_Q.pending),
                                                  "secret_enabled": bool(SECRET),
                                              }},
                                              checks=checks))
        return self._json(404, {"status": "error", "message": "not found"})

    def log_message(self, fmt, *args):                        # noqa: A003
        if os.environ.get("WEBHOOK_VERBOSE") == "1":
            log((fmt % args) if args else str(fmt), level="DEBUG")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    install_signal_handlers(lambda: log("收到停机信号，结束接收"))
    threading.Thread(target=retry_loop, daemon=True).start()

    state = load_state()
    mode, hint = describe_mode(state)
    server = _Server(("0.0.0.0", port), SurveyWebhookHandler)
    log("=" * 58)
    log("  量表问卷 Webhook 接收器 (research-survey-webhook)")
    log(f"  🚀 端口 {port}")
    log(f"  🔐 共享密钥: {'已启用' if SECRET else '未启用（建议在 app.yaml 配置）'}")
    log(f"  📥 POST / → 立即拉取该条答卷 → 计分 → 入邮件队列")
    log(f"  ❤️  GET /healthz → 健康 + webhook 模式（当前: {mode}）")
    log(f"  ♻️  重试队列: {RETRY_DIR}（最多 {RETRY_MAX_ATTEMPTS} 次）")
    log(f"  ℹ️  {hint}")
    log("=" * 58)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        log("已停止")


if __name__ == "__main__":
    main()
