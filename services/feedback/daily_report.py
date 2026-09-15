#!/usr/bin/env python3
"""
每日流水报告 — 汇总 diet_survey 和 sjtu_survey_pro 两流程状态
============================================================
每天生成一次，发送到 operator@example.edu。

用法: python3 daily_report.py [--test]  (=test 立即发送测试邮件)
"""

import json, os, sys, smtplib, ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Portable defaults: assume the standard install layout where the service
# directories are siblings; override with DIET_APP_DIR / SURVEY_APP_DIR.
_DIET_DEFAULT_DIR = os.path.normpath(os.path.join(BASE_DIR, os.pardir, "diet_survey"))
_SURVEY_DEFAULT_DIR = os.path.normpath(os.path.join(BASE_DIR, os.pardir, "sjtu_survey_pro"))
CST = datetime.now().astimezone().tzinfo  # local timezone

# Email config (same as both projects)
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.sjtu.edu.cn")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SENDER = os.environ.get("SMTP_SENDER_EMAIL", "")
SENDER_NAME = os.environ.get("SMTP_SENDER_NAME", "System Daily Report")
USERNAME = os.environ.get("SMTP_USERNAME", "")
PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ADMIN = os.environ.get("SMTP_ADMIN_EMAIL", "")
CC_EMAIL = os.environ.get("SMTP_CC_EMAIL", "")  # 抄送邮箱，填写后每日报告同步抄送

# ── Data gathering ───────────────────────────────────────────────────────

def get_diet_stats():
    """Gather diet_survey statistics."""
    sys.path.insert(0, os.environ.get("DIET_APP_DIR", _DIET_DEFAULT_DIR))
    from diet_database import get_conn
    conn = get_conn()
    today = datetime.now(CST).strftime("%Y-%m-%d")
    try:
        total = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        today_count = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE record_date=?", (today,)
        ).fetchone()[0]
        with_advice = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE dietary_advice IS NOT NULL"
        ).fetchone()[0]
        students = conn.execute(
            "SELECT COUNT(DISTINCT student_id) FROM submissions"
        ).fetchone()[0]
    finally:
        conn.close()

    # LLM queue status
    queue_dir = os.path.join(os.environ.get("DIET_APP_DIR", _DIET_DEFAULT_DIR), ".llm_queue")
    pending = len([f for f in os.listdir(f"{queue_dir}/pending") if f.endswith(".json")]) if os.path.isdir(f"{queue_dir}/pending") else 0
    done = len([f for f in os.listdir(f"{queue_dir}/done") if f.endswith(".json")]) if os.path.isdir(f"{queue_dir}/done") else 0
    failed = len([f for f in os.listdir(f"{queue_dir}/failed") if f.endswith(".json")]) if os.path.isdir(f"{queue_dir}/failed") else 0

    # Email queue
    email_dir = os.path.join(os.environ.get("DIET_APP_DIR", _DIET_DEFAULT_DIR), ".email_queue")
    email_pending = len([f for f in os.listdir(f"{email_dir}/pending") if f.endswith(".json")]) if os.path.isdir(f"{email_dir}/pending") else 0
    email_sent = len([f for f in os.listdir(f"{email_dir}/sent") if f.endswith(".json")]) if os.path.isdir(f"{email_dir}/sent") else 0

    return {
        "total": total, "today": today_count, "with_advice": with_advice,
        "students": students, "llm_pending": pending, "llm_done": done,
        "llm_failed": failed, "email_pending": email_pending, "email_sent": email_sent,
    }

def get_survey_stats():
    """Gather sjtu_survey_pro statistics."""
    sys.path.insert(0, os.environ.get("SURVEY_APP_DIR", _SURVEY_DEFAULT_DIR))
    from survey_database import get_conn
    conn = get_conn()
    today = datetime.now(CST).strftime("%Y-%m-%d")
    try:
        total = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        # Today submissions (by created_at)
        today_count = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE date(created_at)=?", (today,)
        ).fetchone()[0]
        students = conn.execute(
            "SELECT COUNT(DISTINCT student_id) FROM submissions"
        ).fetchone()[0]
        # Email stats
        email_total = conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0]
        email_sent = conn.execute(
            "SELECT COUNT(*) FROM email_log WHERE status='sent'"
        ).fetchone()[0]
    finally:
        conn.close()

    # Email queue
    email_dir = os.path.join(os.environ.get("SURVEY_APP_DIR", _SURVEY_DEFAULT_DIR), ".email_queue")
    email_pending = len([f for f in os.listdir(f"{email_dir}/pending") if f.endswith(".json")]) if os.path.isdir(f"{email_dir}/pending") else 0

    return {
        "total": total, "today": today_count, "students": students,
        "email_total": email_total, "email_sent": email_sent,
        "email_pending": email_pending,
    }

# ── Report building ──────────────────────────────────────────────────────

def build_html(diet, survey):
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body{{font-family:-apple-system,sans-serif;background:#f5f7fa;padding:20px;}}
.container{{max-width:600px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);}}
.header{{background:linear-gradient(135deg,#667eea,#764ba2);color:white;padding:24px;text-align:center;}}
.header h2{{margin:0;}}
.card{{padding:20px;}}
.card h3{{color:#333;border-bottom:2px solid #eee;padding-bottom:8px;}}
table{{width:100%;border-collapse:collapse;}}
td{{padding:8px 12px;border-bottom:1px solid #f0f0f0;}}
td:last-child{{text-align:right;font-weight:600;}}
.good{{color:#28a745;}}.warn{{color:#ffc107;}}.bad{{color:#dc3545;}}
.footer{{text-align:center;color:#aaa;font-size:11px;padding:16px;}}
</style></head><body>
<div class="container">
<div class="header"><h2>📊 每日流水报告</h2><p style="margin:4px 0 0;opacity:0.8;">{now}</p></div>
<div class="card">
<h3>🥗 饮食记录系统 (diet_survey)</h3>
<table>
<tr><td>总记录数</td><td>{diet['total']}</td></tr>
<tr><td>今日新增</td><td class="{'good' if diet['today'] else ''}">{diet['today']}</td></tr>
<tr><td>已生成建议</td><td>{diet['with_advice']}</td></tr>
<tr><td>参与学生数</td><td>{diet['students']}</td></tr>
<tr><td>LLM 队列待处理</td><td class="{'warn' if diet['llm_pending'] else 'good'}">{diet['llm_pending']}</td></tr>
<tr><td>LLM 已完成</td><td>{diet['llm_done']}</td></tr>
<tr><td>邮件队列待发</td><td class="{'warn' if diet['email_pending'] else 'good'}">{diet['email_pending']}</td></tr>
</table>
</div>
<div class="card">
<h3>📋 问卷反馈系统 (sjtu_survey_pro)</h3>
<table>
<tr><td>总记录数</td><td>{survey['total']}</td></tr>
<tr><td>今日新增</td><td class="{'good' if survey['today'] else ''}">{survey['today']}</td></tr>
<tr><td>参与学生数</td><td>{survey['students']}</td></tr>
<tr><td>邮件已发送</td><td>{survey['email_sent']}</td></tr>
<tr><td>邮件队列待发</td><td class="{'warn' if survey['email_pending'] else 'good'}">{survey['email_pending']}</td></tr>
</table>
</div>
<div class="footer">系统自动生成 · 如有异常请检查服务器</div>
</div></body></html>"""

# ── Send ─────────────────────────────────────────────────────────────────

def send_report(html_body):
    subject = f"📊 每日流水报告 — {datetime.now(CST).strftime('%Y-%m-%d')}"
    msg = MIMEText(html_body, "html", "utf-8")
    msg["From"] = f"{Header(SENDER_NAME, 'utf-8').encode()} <{SENDER}>"
    msg["To"] = ADMIN
    if CC_EMAIL:
        msg["Cc"] = CC_EMAIL
    msg["Subject"] = Header(subject, "utf-8")

    ctx = ssl.create_default_context()
    conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30)
    try:
        conn.ehlo_or_helo_if_needed()
        conn.login(USERNAME, PASSWORD)
        recipients = [ADMIN] + ([CC_EMAIL] if CC_EMAIL else [])
        conn.sendmail(SENDER, recipients, msg.as_string())
        print(f"✅ Report sent to {ADMIN}")
        return True
    except Exception as e:
        print(f"❌ Failed: {e}")
        return False
    finally:
        conn.quit()

# ── Main ─────────────────────────────────────────────────────────────────

def main():
    diet = get_diet_stats()
    survey = get_survey_stats()
    html = build_html(diet, survey)
    if "--test" in sys.argv:
        print("Test mode: sending immediately...")
    send_report(html)

if __name__ == "__main__":
    main()
