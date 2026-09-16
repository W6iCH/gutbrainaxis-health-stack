#!/usr/bin/env python3
"""
Survey Feedback Server — Strict Mode + Auto Trigger
====================================================
仅响应格式为 /report?user=xxx&quest=yyy&answer=zzz 的请求。
其他所有路径均返回 403 Forbidden，不返回任何数据。
每个 answer ID 生成独立的页面，互不干扰。

当请求的数据尚未处理时，自动触发一次同步流水线，
避免用户等待 60 分钟的 cron 周期。
"""

import json, os, signal, sys, urllib.parse, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from survey_analysis import render_html_report
from survey_database import get_conn
from survey_sync_cron import sync as run_sync

DEFAULT_PORT = int(os.environ.get("PORT_SURVEY_FEEDBACK", "8000"))
# 兼容监听端口（可留空）：PORT_SURVEY_FEEDBACK_ALT，由 app.yaml services.survey_feedback.port_alt 渲染
DEFAULT_PORT_ALT = int(os.environ.get("PORT_SURVEY_FEEDBACK_ALT", "0") or "0")
TRIGGER_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback_trigger.log")


def render_403():
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>403 Forbidden</title>
<style>
body{font-family:-apple-system,sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;background:#f8f9fa;margin:0;}
.card{background:white;padding:40px;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.08);
max-width:400px;text-align:center;}
h2{color:#e74c3c;margin-bottom:12px;}
p{color:#888;font-size:13px;line-height:1.8;}
code{background:#f0f0f0;padding:2px 6px;border-radius:4px;font-size:12px;}
</style></head>
<body>
<div class="card">
<h2>🔒 403 Forbidden</h2>
<p>此页面仅可通过问卷提交后的跳转链接访问，<br>无法直接访问。</p>
<p style="font-size:11px;color:#bbb;margin-top:16px;">
格式: <code>/report?user=xxx&amp;quest=yyy&amp;answer=zzz</code></p>
</div>
</body>
</html>"""


def log_trigger(msg):
    try:
        os.makedirs(os.path.dirname(TRIGGER_LOG), exist_ok=True)
        with open(TRIGGER_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except:
        pass


# ── 即时触发同步 ─────────────────────────────────────────────

_TRIGGER_LOCK = threading.Lock()
_TRIGGER_INFLIGHT = set()


def trigger_sync_async(answer_id: str = ""):
    """
    在后台线程中立即触发一次同步，避免用户等待 60 分钟的 cron 周期。
    同一 answerId 只会触发一次（去重）。
    """
    with _TRIGGER_LOCK:
        if answer_id and answer_id in _TRIGGER_INFLIGHT:
            log_trigger(f"sync already in-flight for answer={answer_id}, skipping")
            return
        if answer_id:
            _TRIGGER_INFLIGHT.add(answer_id)

    def _run():
        try:
            log_trigger(f"sync triggered by user visit (answer={answer_id})")
            stats = run_sync(force=False, dry_run=False)
            new_count = stats.get("new", 0)
            log_trigger(f"sync completed: {new_count} new (answer={answer_id})")
        except Exception as e:
            log_trigger(f"sync error (answer={answer_id}): {e}")
        finally:
            with _TRIGGER_LOCK:
                _TRIGGER_INFLIGHT.discard(answer_id)

    t = threading.Thread(target=_run, daemon=True, name=f"sync-{answer_id}")
    t.start()
    log_trigger(f"sync thread started for answer={answer_id}")


class FeedbackHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)

        # ONLY /report?user=X&quest=Y&answer=Z is allowed
        if path == "/report":
            user_param = params.get("user", [None])[0]
            quest_param = params.get("quest", [None])[0]
            answer_param = params.get("answer", [None])[0]

            if user_param and quest_param and answer_param:
                self._serve_individual_report(user_param, quest_param, answer_param)
                return

        # 统一健康探针（供 tools/selfcheck.py 与容器/反代探活使用）
        if path in ("/healthz", "/health", "/ping"):
            self._send_healthz(path)
            return

        # Everything else → 403 Forbidden
        self._send_403()

    def _send_healthz(self, path):
        """GET /healthz → 统一 JSON 结构（含 DB 可读性检查）。"""
        checks = {}
        ok = True
        try:
            conn = get_conn()
            n = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
            conn.close()
            checks["survey_db"] = {"readable": True, "submissions": n}
        except Exception as e:                      # noqa: BLE001
            checks["survey_db"] = {"readable": False, "error": str(e)}
            ok = False
        payload = {
            "status": "ok" if ok else "degraded",
            "service": "survey-feedback",
            "pid": os.getpid(),
            "checks": checks,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        if path == "/ping":
            body = b"pong"
            ctype = "text/plain; charset=utf-8"
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_individual_report(self, user_param, quest_param, answer_param):
        """Serve the analysis report for ONE specific answer ID."""
        ua = self.headers.get("User-Agent", "")[:60]
        log_trigger(f"answer={answer_param} user={user_param} ua={ua}")
        print(f"  [serve] answer={answer_param} user={user_param} from={self.client_address[0]}")

        # Look up in database by answer ID
        conn = get_conn()
        row = conn.execute(
            "SELECT analysis, raw_data FROM submissions WHERE CAST(redirect_answer AS INTEGER) = ?",
            (int(answer_param),)
        ).fetchone()

        if not row:
            # Try string match
            row = conn.execute(
                "SELECT analysis, raw_data FROM submissions WHERE redirect_answer = ?",
                (str(answer_param),)
            ).fetchone()
        conn.close()

        if row and row["analysis"]:
            analysis = json.loads(row["analysis"])
            html = render_html_report(analysis, answer_id=answer_param)
            self._send_200(html)
            print(f"  \u2713 Served report for answer={answer_param}")
        else:
            # Data not ready yet — trigger immediate sync in background
            trigger_sync_async(answer_param)
            html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta http-equiv="refresh" content="8">
<title>分析报告中</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;
background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:20px;margin:0}}
.card{{background:white;border-radius:24px;padding:40px 48px;max-width:460px;width:100%;
text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.25)}}
.spinner{{width:44px;height:44px;margin:0 auto 20px;border:3px solid #e8ecf4;
border-top:3px solid #667eea;border-radius:50%;animation:spin .8s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
h1{{font-size:21px;color:#333;margin-bottom:6px;font-weight:600}}
.status{{color:#888;font-size:13px;margin-bottom:20px}}
.email-hint{{background:#fff8e1;border:1px solid #ffe082;border-radius:12px;padding:14px 16px;
font-size:13px;color:#795548;line-height:1.7;text-align:left}}
.email-hint .emoji{{font-size:20px;float:left;margin-right:10px}}
.answer-tag{{color:#bbb;font-size:11px;margin-top:20px}}
.refresh-info{{color:#bbb;font-size:11px;margin-top:4px}}
</style></head>
<body>
<div class="card">
<div class="spinner"></div>
<h1>正在分析您的问卷数据</h1>
<p class="status">预计需要 30~60 秒，页面将自动刷新</p>
<div class="email-hint">
<span class="emoji">📧</span>
<strong>温馨提示</strong><br>分析完成后，结果将自动发送至您的邮箱。您可以关闭此页面，稍后查收邮件即可。
</div>
<p class="refresh-info">页面每 8 秒自动刷新一次</p>
<p class="answer-tag">提交编号: {answer_param}</p>
</div>
</body>
</html>"""
            self._send_200(html)
            print(f"  \u23f3 Data pending for answer={answer_param}, sync triggered")

    def _send_200(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _send_403(self):
        html = render_403()
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, fmt, *args):
        msg = " ".join(str(a) for a in args) if args else ""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def main():
    if len(sys.argv) > 1:
        ports = [int(a) for a in sys.argv[1:]]
    else:
        # 端口来自 app.yaml（services.survey_feedback.port / .port_alt）
        ports = [DEFAULT_PORT]
        if DEFAULT_PORT_ALT and DEFAULT_PORT_ALT != DEFAULT_PORT:
            ports.append(DEFAULT_PORT_ALT)

    print("=" * 50)
    print("  Survey Feedback Server — STRICT MODE + AUTO TRIGGER")
    print("  Only /report?user=X&quest=Y&answer=Z allowed")
    print("= " * 25)

    servers = []
    for port in ports:
        try:
            srv = HTTPServer(("0.0.0.0", port), FeedbackHandler)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            servers.append((port, srv))
            print(f"  \U0001f680 Port {port}")
        except OSError as e:
            print(f"  \u26a0 Port {port}: {e}")

    if not servers:
        print("  \u2717 No ports available.")
        return

    print()
    print("  \u2705 Accepting:  /report?user=xxx&quest=yyy&answer=zzz")
    print("  \u274c All other paths return 403")
    print("  \U0001f4a1 Auto-trigger: yes (sync on cache miss)")
    print()
    print("  Press Ctrl+C to stop.\n")

    # 优雅停机：SIGTERM/SIGINT → 停止接受新请求 → 关服务器 → 退出
    stop = threading.Event()

    def _on_signal(signum, frame):           # noqa: ARG001
        print(f"\n  [shutdown] 收到信号 {signum}，优雅停机中…")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    try:
        while not stop.is_set():
            stop.wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        for port, srv in servers:
            try:
                srv.shutdown()
                srv.server_close()
                print(f"  [shutdown] :{port} 已关闭")
            except Exception:                # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
