#!/usr/bin/env python3
"""
Diet Survey Feedback Server — Redirect Landing Page
=====================================================
用户提交饮食问卷后跳转到的反馈页面。
仅响应 /report?user=X&quest=Y&answer=Z 的请求。
展示 LLM 生成的饮食建议。

参数说明：
  - user:   用户账号 ({{.User}})
  - quest:  问卷 ID ({{.QuestID}})
  - answer: 答卷 ID ({{.AnswerID}})
"""

import json
import os
import sys
import urllib.parse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diet_database import get_submission_by_answer_id, get_conn

DEFAULT_PORT = 8001
TRIGGER_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "feedback_trigger.log")


def log_trigger(msg):
    try:
        os.makedirs(os.path.dirname(TRIGGER_LOG), exist_ok=True)
        with open(TRIGGER_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except:
        pass


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


def render_loading(answer_param):
    """Render the loading page with progress bar + email hint."""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta http-equiv="refresh" content="8">
<title>分析报告中</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;
background:linear-gradient(135deg,#43e97b 0%,#38f9d7 100%);padding:20px;margin:0}}
.card{{background:white;border-radius:24px;padding:40px 48px;max-width:460px;width:100%;
text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.2)}}
.spinner{{width:44px;height:44px;margin:0 auto 20px;border:3px solid #e8f5e9;
border-top:3px solid #43e97b;border-radius:50%;animation:spin .8s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
h1{{font-size:21px;color:#333;margin-bottom:6px;font-weight:600}}
.status{{color:#888;font-size:13px;margin-bottom:20px}}
.email-hint{{background:#e8f5e9;border:1px solid #a5d6a7;border-radius:12px;padding:14px 16px;
font-size:13px;color:#2e7d32;line-height:1.7;text-align:left}}
.email-hint .emoji{{font-size:20px;float:left;margin-right:10px}}
.answer-tag{{color:#bbb;font-size:11px;margin-top:20px}}
.refresh-info{{color:#bbb;font-size:11px;margin-top:4px}}
</style></head>
<body>
<div class="card">
<div class="spinner"></div>
<h1>正在生成您的饮食建议</h1>
<p class="status">预计需要 30~60 秒，页面将自动刷新</p>
<div class="email-hint">
<span class="emoji">📧</span>
<strong>温馨提示</strong><br>分析完成后，饮食建议将自动发送至您的邮箱。您可以关闭此页面，稍后查收邮件即可。
</div>
<p class="refresh-info">页面每 8 秒自动刷新一次</p>
<p class="answer-tag">提交编号: {answer_param}</p>
</div>
</body>
</html>"""


def render_report(record: dict, answer_id: str):
    """Render the dietary advice report page."""
    name = record.get("name", "用户")
    record_date = record.get("record_date", "未知日期")
    diet_desc = record.get("diet_description", "")
    meal_count = record.get("meal_count", 0)
    advice = record.get("dietary_advice") or ""
    fulfillment = record.get("fulfillment_report") or ""

    # Convert markdown-like advice to HTML
    advice_html = _markdown_to_html(advice)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>饮食建议报告 — {name}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Microsoft YaHei", sans-serif;
    background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%);
    min-height: 100vh;
    padding: 20px;
}}
.container {{
    max-width: 680px;
    margin: 0 auto;
}}
.header {{
    background: white;
    border-radius: 20px;
    padding: 32px;
    text-align: center;
    box-shadow: 0 10px 40px rgba(0,0,0,0.1);
    margin-bottom: 20px;
}}
.header .icon {{
    font-size: 48px;
    margin-bottom: 12px;
}}
.header h1 {{
    font-size: 26px;
    color: #2d3436;
    margin-bottom: 8px;
}}
.header .meta {{
    color: #888;
    font-size: 13px;
    margin-top: 8px;
}}
.header .meta span {{
    background: #f0f0f0;
    border-radius: 12px;
    padding: 4px 12px;
    margin: 0 4px;
}}
.advice-card {{
    background: white;
    border-radius: 20px;
    padding: 32px;
    box-shadow: 0 10px 40px rgba(0,0,0,0.1);
    line-height: 1.9;
    font-size: 15px;
    color: #333;
}}
.advice-card h2 {{
    font-size: 20px;
    color: #2d3436;
    border-bottom: 2px solid #43e97b;
    padding-bottom: 8px;
    margin: 28px 0 16px 0;
}}
.advice-card h2:first-child {{
    margin-top: 0;
}}
.advice-card h3 {{
    font-size: 17px;
    color: #444;
    margin: 18px 0 10px 0;
}}
.advice-card strong {{
    color: #2d3436;
}}
.advice-card ul, .advice-card ol {{
    margin: 10px 0;
    padding-left: 24px;
}}
.advice-card li {{
    margin: 6px 0;
}}
.advice-card p {{
    margin: 10px 0;
}}
.footer {{
    text-align: center;
    color: #888;
    font-size: 12px;
    margin-top: 24px;
    padding: 16px;
}}
.footer a {{
    color: #666;
}}
.print-btn {{
    display: inline-block;
    background: #43e97b;
    color: white !important;
    text-decoration: none;
    padding: 10px 24px;
    border-radius: 24px;
    font-size: 14px;
    margin-top: 16px;
    transition: transform 0.2s;
}}
.print-btn:hover {{
    transform: scale(1.05);
}}
</style>
</head>
<body>
<div class="container">

<div class="header">
    <div class="icon">🥗</div>
    <h1>饮食建议报告</h1>
    <p style="color:#666;margin-top:4px;">{name} · 每日健康建议</p>
    <div class="meta">
        <span>📅 {record_date}</span>
        <span>🍽️ 约{meal_count}餐</span>
        <span>📋 #{answer_id}</span>
    </div>
</div>

<div class="advice-card">
{advice_html}
</div>

<div class="footer">
    <a href="#" class="print-btn" onclick="window.print()">🖨️ 打印 / 保存 PDF</a>
    <p style="margin-top:16px;">本建议基于《中国居民膳食指南（2022）》生成<br>仅供参考，不构成医疗建议</p>
    
</div>

</div>
</body>
</html>"""


def _markdown_to_html(text: str) -> str:
    """Simple markdown to HTML conversion for the advice content."""
    if not text:
        return "<p style='color:#999;text-align:center;'>饮食建议生成中，请稍后刷新页面...</p>"

    lines = text.split("\n")
    result = []
    in_list = False
    in_ol = False

    for line in lines:
        stripped = line.strip()

        # Headers
        if stripped.startswith("## "):
            if in_list:
                result.append("</ul>")
                in_list = False
            if in_ol:
                result.append("</ol>")
                in_ol = False
            title = stripped[3:]
            # Convert emoji headers to h3
            clean = title.strip("# ").strip()
            result.append(f"<h2>{clean}</h2>")
            continue
        elif stripped.startswith("### "):
            if in_list:
                result.append("</ul>")
                in_list = False
            if in_ol:
                result.append("</ol>")
                in_ol = False
            clean = stripped[4:].strip()
            result.append(f"<h3>{clean}</h3>")
            continue
        elif stripped.startswith("**") and "**" in stripped[2:]:
            # Bold sub-headers like **1. 主食** → strip the ** markers
            if in_list:
                result.append("</ul>")
                in_list = False
            if in_ol:
                result.append("</ol>")
                in_ol = False
            clean = stripped.strip("*")
            result.append(f"<h3>{clean}</h3>")
            continue

        # Lists
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                if in_ol:
                    result.append("</ol>")
                    in_ol = False
                result.append("<ul>")
                in_list = True
            content = stripped[2:]
            result.append(f"<li>{content}</li>")
            continue
        elif stripped and stripped[0].isdigit() and ". " in stripped[:4]:
            if not in_ol:
                if in_list:
                    result.append("</ul>")
                    in_list = False
                result.append("<ol>")
                in_ol = True
            content = stripped.split(". ", 1)[1] if ". " in stripped else stripped
            result.append(f"<li>{content}</li>")
            continue
        else:
            if in_list:
                result.append("</ul>")
                in_list = False
            if in_ol:
                result.append("</ol>")
                in_ol = False

        # Empty line
        if not stripped:
            result.append("<br>")
            continue

        # Regular paragraph
        result.append(f"<p>{stripped}</p>")

    if in_list:
        result.append("</ul>")
    if in_ol:
        result.append("</ol>")

    return "\n".join(result)


# ── Background sync trigger ──────────────────────────────────────────

_TRIGGER_LOCK = threading.Lock()
_TRIGGER_INFLIGHT = set()


def trigger_sync_async(answer_id: str = ""):
    """Trigger a background sync for the given answer ID."""
    with _TRIGGER_LOCK:
        if answer_id and answer_id in _TRIGGER_INFLIGHT:
            return
        if answer_id:
            _TRIGGER_INFLIGHT.add(answer_id)

    def _run():
        try:
            log_trigger(f"sync triggered for answer={answer_id}")
            from diet_sync_cron import sync
            stats = sync(force=False, dry_run=False)
            log_trigger(f"sync done: {stats}")
        except Exception as e:
            log_trigger(f"sync error (answer={answer_id}): {e}")
        finally:
            with _TRIGGER_LOCK:
                _TRIGGER_INFLIGHT.discard(answer_id)

    t = threading.Thread(target=_run, daemon=True, name=f"sync-{answer_id}")
    t.start()


# ── HTTP Handler ─────────────────────────────────────────────────────

class DietFeedbackHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/report":
            user_param = params.get("user", [None])[0]
            quest_param = params.get("quest", [None])[0]
            answer_param = params.get("answer", [None])[0]

            if user_param and quest_param and answer_param:
                self._serve_report(user_param, quest_param, answer_param)
                return
        elif path == "/health":
            self._send_json({"status": "ok", "service": "diet-feedback"})
            return
        elif path == "/ping":
            self._send_text("pong")
            return

        self._send_403()

    def _serve_report(self, user_param, quest_param, answer_param):
        ua = self.headers.get("User-Agent", "")[:60]
        log_trigger(f"report request: answer={answer_param} user={user_param} ua={ua}")

        record = get_submission_by_answer_id(answer_param)

        if record and record.get("dietary_advice"):
            # Data is ready — show the report
            html = render_report(record, answer_param)
            self._send_200(html)
            print(f"  ✓ Served report for answer={answer_param}")
        elif record:
            # Data exists but advice not yet generated — trigger sync, show loading
            trigger_sync_async(answer_param)
            html = render_loading(answer_param)
            self._send_200(html)
            print(f"  ⏳ Data exists, advice pending for answer={answer_param}")
        else:
            # No data at all — trigger sync, show loading
            trigger_sync_async(answer_param)
            html = render_loading(answer_param)
            self._send_200(html)
            print(f"  ⏳ No data for answer={answer_param}, sync triggered")

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
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_text(self, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def log_message(self, fmt, *args):
        msg = " ".join(str(a) for a in args) if args else ""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def main():
    ports = [int(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else [DEFAULT_PORT]

    print("=" * 50)
    print("  Diet Survey Feedback Server")
    print("  Only /report?user=X&quest=Y&answer=Z allowed")
    print("=" * 25)

    servers = []
    for port in ports:
        try:
            srv = HTTPServer(("0.0.0.0", port), DietFeedbackHandler)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            servers.append((port, srv))
            print(f"  🚀 Port {port}")
        except OSError as e:
            print(f"  ⚠ Port {port}: {e}")

    if not servers:
        print("  ✗ No ports available.")
        return

    print()
    print("  ✅ Accepting:  /report?user=xxx&quest=yyy&answer=zzz")
    print("  ❌ All other paths return 403")
    print("  💡 Auto-trigger sync on cache miss")
    print()
    print("  Press Ctrl+C to stop.\n")

    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for _, srv in servers:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    main()
