#!/usr/bin/env python3
"""
Diet Survey Webhook Listener v2
================================
Receives POST callbacks from wj.sjtu.edu.cn and:
  1. Stores dietary records in SQLite
  2. Calls LLM to generate dietary advice (non-blocking background thread)
  3. Sends email with advice

Usage:
    python3 webhook_listener.py [port]

Port resolution: CLI arg > $PORT_DIET_WEBHOOK (rendered from app.yaml) > 9876
"""

import json
import sys
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 公共限流（单一真源）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "common"))
try:
    from lib_ratelimit import SlidingWindowLimiter
except Exception:                                            # pragma: no cover
    class SlidingWindowLimiter:                              # type: ignore
        def __init__(self, limit=60, window_seconds=60):
            pass

        def allow(self, key):
            return True

from diet_database import store_submission, update_submission_advice, get_conn

# 入站防护参数（可由 app.yaml 渲染的环境变量覆盖）
MAX_BODY_BYTES = int(os.environ.get("WEBHOOK_MAX_BODY_BYTES", "1048576"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("WEBHOOK_RATE_LIMIT_PER_MINUTE", "120"))
_LIMITER = SlidingWindowLimiter(RATE_LIMIT_PER_MINUTE, 60)


class DietWebhookHandler(BaseHTTPRequestHandler):

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # 入站防护（E：健壮性）：请求体上限 + 单 IP 限流，避免平台重投风暴打垮服务
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._json(400, {"status": "error", "message": "bad content-length"})
        if content_length > MAX_BODY_BYTES:
            print(f"[{datetime.now():%H:%M:%S}] 拒绝超大请求体 {content_length}")
            return self._json(413, {"status": "error", "message": "payload too large"})
        ip = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or self.client_address[0])
        if not _LIMITER.allow(ip):
            print(f"[{datetime.now():%H:%M:%S}] 限速拒绝 ip={ip}")
            return self._json(429, {"status": "error", "message": "rate limited"})
        body = self.rfile.read(content_length)

        try:
            callback_data = json.loads(body.decode("utf-8"))
            # Step 1: Store in database (synchronous)
            db_id, student_id, email, is_first = store_submission(callback_data)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] ✓ Stored: student={student_id} email={email} "
                  f"db_id={db_id} first={is_first}")

            # Step 2: Extract diet info for task queue
            diet_desc = ""
            record_date = ""
            submission_id = None
            for sheet in callback_data.get("answer_sheet", []):
                submission_id = str(sheet.get("id", ""))
                for item in sheet.get("answers", []):
                    title = item.get("question", {}).get("title", "")
                    ans = str(item.get("answer", ""))
                    if "记录日期" in title: record_date = ans
                    if "饮食" in title or "描述" in title: diet_desc = ans

            # Step 3: Enqueue LLM task (persistent queue, sequential processing)
            import diet_llm_queue
            diet_llm_queue.enqueue_task(db_id, student_id, diet_desc, record_date, submission_id, email)
            print(f"[{now}]   → LLM task enqueued (persistent queue)")

            # Step 3: Return OK immediately
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "requestId": db_id,
                "timestamp": datetime.now().isoformat()
            }).encode("utf-8"))

        except Exception as e:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] ✗ Error: {e}")
            import traceback
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "error",
                "message": str(e)
            }).encode("utf-8"))

    def do_GET(self):
        if self.path in ("/health", "/healthz", "/ping"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "service": "diet-webhook-v2",
                "pid": os.getpid(),
                "time": datetime.now().isoformat(timespec="seconds"),
            }).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def _run_llm_and_email(self, db_id, student_id, email, callback_data):
        """Background: call LLM → store advice → send email."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            # Extract diet description and record date
            diet_desc = ""
            record_date = ""
            answer_sheet = callback_data.get("answer_sheet", [])
            for sheet in answer_sheet:
                for item in sheet.get("answers", []):
                    q = item.get("question", {})
                    title = q.get("title", "")
                    ans = item.get("answer", "")
                    ans_text = str(ans) if not isinstance(ans, dict) else ans.get("label", str(ans))
                    if "记录日期" in title:
                        record_date = ans_text
                    if "饮食" in title or "描述" in title:
                        diet_desc = ans_text

            # Call LLM
            print(f"[{now_str}] 🧠 LLM analyzing for student={student_id}...")
            from diet_llm import generate_dietary_advice, update_submission_with_advice

            # Get submission_id for this record
            submission_id = None
            for sheet in answer_sheet:
                sid = sheet.get("id")
                if sid:
                    submission_id = str(sid)
                    break

            result = generate_dietary_advice(
                student_id=student_id,
                current_diet_desc=diet_desc,
                current_record_date=record_date,
                submission_id=submission_id,
            )

            if "error" in result:
                print(f"[{now_str}] ⚠ LLM failed: {result['error']}")
                return

            # Store advice
            update_submission_with_advice(db_id, result)
            print(f"[{now_str}] ✅ Advice stored for db_id={db_id} "
                  f"({result.get('tokens_used', '?')} tokens)")

            # Send email (if email available and advice exists)
            if email:
                # Get full record with advice
                conn = get_conn()
                full_record = conn.execute(
                    "SELECT * FROM submissions WHERE id = ?", (db_id,)
                ).fetchone()
                conn.close()

                if full_record:
                    from diet_email_feedback import send_immediate
                    record_dict = dict(full_record)
                    item_id = send_immediate(
                        recipient=email,
                        submission_id=submission_id or "",
                        record=record_dict,
                    )
                    if item_id:
                        print(f"[{now_str}] 📧 Email sent to {email}")
                    else:
                        print(f"[{now_str}] ⚠ Email failed for {email}")

        except Exception as e:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now_str}] ❌ Background LLM/email error: {e}")
            import traceback
            traceback.print_exc()

    def log_message(self, fmt, *args):
        msg = " ".join(str(a) for a in args) if args else ""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def main():
    # 端口优先级：命令行参数 > PORT_DIET_WEBHOOK（由 app.yaml 渲染）> 9876
    port = (int(sys.argv[1]) if len(sys.argv) > 1
            else int(os.environ.get("PORT_DIET_WEBHOOK", "9876")))
    # 多线程处理：避免单条慢请求阻塞后续回调（E：健壮性）
    server = ThreadingHTTPServer(("0.0.0.0", port), DietWebhookHandler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    server.request_queue_size = 64
    print("=" * 50)
    print("  Diet Survey Webhook Listener v2")
    print(f"  🚀 Port {port}")
    print(f"  📥 POST / → receive, store, analyze, email")
    print(f"  ❤️  GET /health → health check")
    print(f"  🧠 LLM analysis: background (non-blocking)")
    print(f"  📧 Email: auto-send after advice generated")
    print("=" * 50)
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
