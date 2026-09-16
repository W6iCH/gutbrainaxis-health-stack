#!/usr/bin/env python3
"""
SJTU Email Feedback System v3 — Server-Only
=============================================
Dual-route delivery (MX + SMTP SSL) via mx01.sjtu.edu.cn:25 (no auth needed for @sjtu.edu.cn).
Fully self-contained on server — no tunnels, no external relays.

Usage:
    python3 email_feedback.py daemon       # persistent queue processor
    python3 email_feedback.py process      # process queue once
    python3 email_feedback.py status       # show queue stats
"""

import os
import json, os, sys, smtplib, ssl, time, logging, hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime, timedelta
from pathlib import Path

# Full report renderer (same as web report)
from survey_analysis import render_html_report

# ── Paths ──
BASE_DIR = Path(__file__).resolve().parent
QUEUE_ROOT = BASE_DIR / ".email_queue"
PENDING_DIR = QUEUE_ROOT / "pending"
SENT_DIR = QUEUE_ROOT / "sent"
FAILED_DIR = QUEUE_ROOT / "failed"
LOG_FILE = QUEUE_ROOT / "email_worker.log"
TRACKING_FILE = QUEUE_ROOT / "delivery_tracking.json"

# ── SMTP — Direct MX Delivery ──
SMTP_HOST = os.environ.get("SMTP_HOST", "mx01.sjtu.edu.cn")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
SENDER_EMAIL = os.environ.get("SMTP_SENDER_EMAIL", "")
SENDER_NAME = os.environ.get("SMTP_SENDER_NAME", "Survey Feedback System")
SMTP_SSL_HOST = os.environ.get("SMTP_SSL_HOST", "mail.sjtu.edu.cn")
SMTP_SSL_PORT = int(os.environ.get("SMTP_SSL_PORT", "465"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

# ── Queue Settings ──
MAX_RETRIES = 5
RETRY_DELAYS = [60, 300, 900, 3600, 14400]
POLL_INTERVAL = 30
RATE_LIMIT_GAP = 1.5  # seconds between sends
MAX_PER_BATCH = 30

# ── Setup ──
for d in [QUEUE_ROOT, PENDING_DIR, SENT_DIR, FAILED_DIR]:
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("email")


# ══════════════════════════════════════════════════════════════════════
# Queue Operations
# ══════════════════════════════════════════════════════════════════════

def _gen_id() -> str:
    return hashlib.sha256(os.urandom(16)).hexdigest()[:16]


def load_items(directory: Path) -> list:
    if not directory.is_dir():
        return []
    items = []
    for f in sorted(directory.glob("*.json")):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
            item["_file"] = str(f)
            items.append(item)
        except Exception:
            pass
    return items


def write_item(item: dict, directory: Path):
    item_id = item.get("id") or _gen_id()
    item["id"] = item_id
    item.pop("_file", None)
    (directory / f"{item_id}.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


def move_item(item: dict, src: Path, dst: Path):
    item_id = item["id"]
    src_file = src / f"{item_id}.json"
    if src_file.exists():
        src_file.rename(dst / f"{item_id}.json")
    else:
        write_item(item, dst)


def count_items(directory: Path) -> int:
    return len(list(directory.glob("*.json"))) if directory.is_dir() else 0


# ══════════════════════════════════════════════════════════════════════
# Tracking
# ══════════════════════════════════════════════════════════════════════

def load_tracking() -> dict:
    if TRACKING_FILE.exists():
        try:
            return json.loads(TRACKING_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"entries": {}}


def save_tracking(data: dict):
    TRACKING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_delivery(email: str, status: str, subject: str, error: str = ""):
    tracking = load_tracking()
    now = datetime.now().isoformat()
    key = email.lower()
    if key not in tracking["entries"]:
        tracking["entries"][key] = []
    tracking["entries"][key].append({
        "time": now, "status": status, "subject": subject, "error": error,
    })
    tracking["entries"][key] = tracking["entries"][key][-30:]
    save_tracking(tracking)


# ══════════════════════════════════════════════════════════════════════
# Email Building
# ══════════════════════════════════════════════════════════════════════

# (Removed - using survey_analysis.render_html_report instead)


def build_email_html(analysis: dict, recipient: str) -> tuple:
    """Build HTML email from analysis report using full renderer."""
    basic = analysis.get("basic_info", {})
    name = basic.get("姓名", "用户")
    ts = analysis.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Use the same full report renderer as the web page
    report_html = render_html_report(analysis)

    # Extract body and styles
    body_start = report_html.find("<body>")
    body_end = report_html.find("</body>")
    style_start = report_html.find("<style>")
    style_end = report_html.find("</style>")

    styles = report_html[style_start:style_end + 8] if style_start >= 0 else ""
    inner = report_html[body_start + 6:body_end] if body_start >= 0 and body_end >= 0 else report_html

    # Replace footer with email-specific one
    inner = inner.replace('<div class="footer">', '<div class="footer" style="text-align:center">')

    subject = f"📊 问卷结果分析报告 — {name}"
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
{styles}
<style>body{{background:#f0f2f5!important;padding:20px!important}}.container{{max-width:640px!important}}</style>
</head>
<body>
<div class="container">
{inner}
</div>
</body></html>"""

    return subject, html


# ══════════════════════════════════════════════════════════════════════
# Core API
# ══════════════════════════════════════════════════════════════════════

def queue_email(recipient: str, analysis: dict, submission_id: str = "") -> str:
    """
    Queue an email for delivery. The ONLY entry point.
    Returns item_id.
    """
    if not analysis or "error" in analysis:
        log.error(f"Invalid analysis for {recipient}")
        return ""

    try:
        subject, html_body = build_email_html(analysis, recipient)
    except Exception as e:
        log.error(f"Build failed for {recipient}: {e}")
        return ""

    item_id = _gen_id()
    item = {
        "id": item_id, "recipient": recipient, "subject": subject,
        "html_body": html_body, "submission_id": submission_id,
        "attempts": 0, "status": "pending",
        "created_at": datetime.now().isoformat(),
        "next_attempt": datetime.now().isoformat(),
    }
    write_item(item, PENDING_DIR)
    record_delivery(recipient, "pending", subject)
    log.info(f"📥 Queued {recipient} [{item_id}]")
    return item_id


def _is_sjtu_email(recipient: str) -> bool:
    return recipient.lower().endswith("@sjtu.edu.cn")


def send_email(item: dict) -> bool:
    """Send one email via appropriate route.
    - @sjtu.edu.cn -> MX direct (mx01:25), fallback to SSL (mail:465)
    - external      -> SMTP SSL (mail:465)
    """
    recipient = item["recipient"]
    if _is_sjtu_email(recipient):
        result = _send_via_mx(item)
        if not result:
            log.info(f"MX failed for {recipient}, trying SSL fallback...")
            result = _send_via_ssl(item)
        return result
    else:
        return _send_via_ssl(item)

def _build_message_survey(item: dict) -> MIMEMultipart:
    recipient = item["recipient"]
    subject = item["subject"]
    html_body = item["html_body"]
    msg = MIMEMultipart("alternative")
    msg['From'] = f"{Header(SENDER_NAME, 'utf-8').encode()} <{SENDER_EMAIL}>"
    msg['To'] = recipient
    msg['Subject'] = Header(subject, 'utf-8')
    msg['Message-ID'] = f"<survey-{item['id']}@sjtu.edu.cn>"
    msg["Date"] = Header(datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800"))
    msg.attach(MIMEText('请使用支持HTML的邮件客户端查看本报告。', 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    return msg


def _send_via_mx(item: dict) -> bool:
    """Send via mx01.sjtu.edu.cn:25 -- direct MX, no auth."""
    recipient = item["recipient"]
    msg = _build_message_survey(item)
    try:
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        conn.ehlo("ecs-aab7")
        conn.sendmail(SENDER_EMAIL, [recipient], msg.as_string())
        conn.quit()
        log.info(f'\u2705 [MX] Sent {recipient}')
        record_delivery(recipient, "sent", item["subject"])
        return True
    except Exception as e:
        log.warning(f'\u26a0\ufe0f  [MX] {recipient}: {e}')
        return False


def _send_via_ssl(item: dict) -> bool:
    """Send via mail.sjtu.edu.cn:465 -- SMTP SSL with auth."""
    recipient = item["recipient"]
    msg = _build_message_survey(item)
    try:
        context = ssl.create_default_context()
        conn = smtplib.SMTP_SSL(SMTP_SSL_HOST, SMTP_SSL_PORT, context=context, timeout=30)
        conn.login(SMTP_USERNAME, SMTP_PASSWORD)
        conn.sendmail(SENDER_EMAIL, [recipient], msg.as_string())
        conn.quit()
        log.info(f'\u2705 [SSL] Sent {recipient}')
        record_delivery(recipient, "sent", item["subject"])
        return True
    except Exception as e:
        log.warning(f'\u26a0\ufe0f  [SSL] {recipient}: {e}')
        return False

# Queue Processing
# ══════════════════════════════════════════════════════════════════════

def process_queue(max_emails: int = None) -> dict:
    if max_emails is None:
        max_emails = MAX_PER_BATCH

    stats = {"processed": 0, "sent": 0, "failed": 0, "deferred": 0}
    now = datetime.now()
    items = load_items(PENDING_DIR)

    for item in items:
        if stats["processed"] >= max_emails:
            break
        na = item.get("next_attempt", "")
        if na:
            try:
                if now < datetime.fromisoformat(na):
                    stats["deferred"] += 1
                    continue
            except ValueError:
                pass

        stats["processed"] += 1
        success = send_email(item)

        if success:
            item["status"] = "sent"
            item["sent_at"] = now.isoformat()
            move_item(item, PENDING_DIR, SENT_DIR)
            stats["sent"] += 1
        else:
            item["attempts"] = item.get("attempts", 0) + 1
            if item["attempts"] >= MAX_RETRIES:
                item["status"] = "failed"
                item["failed_at"] = now.isoformat()
                move_item(item, PENDING_DIR, FAILED_DIR)
                stats["failed"] += 1
            else:
                delay_idx = min(item["attempts"] - 1, len(RETRY_DELAYS) - 1)
                delay = RETRY_DELAYS[delay_idx]
                item["next_attempt"] = (now + timedelta(seconds=delay)).isoformat()
                write_item(item, PENDING_DIR)
                stats["deferred"] += 1

        time.sleep(RATE_LIMIT_GAP)

    return stats


def daemon_loop():
    log.info("=" * 60)
    log.info("  Email Daemon v3 — Direct MX Delivery")
    log.info(f"  SMTP: {SMTP_HOST}:{SMTP_PORT}")
    log.info(f"  Poll: {POLL_INTERVAL}s | Retries: {MAX_RETRIES} | Batch: {MAX_PER_BATCH}")
    log.info("=" * 60)

    while True:
        try:
            pending = count_items(PENDING_DIR)
            if pending > 0:
                log.info(f"📬 Processing {pending} pending items...")
                stats = process_queue()
                log.info(f"   Sent={stats['sent']} Failed={stats['failed']} Deferred={stats['deferred']}")
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("Daemon stopped")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(10)


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

def cmd_status():
    p, s, f = count_items(PENDING_DIR), count_items(SENT_DIR), count_items(FAILED_DIR)
    print(f"📧 Queue: pending={p} sent={s} failed={f}")
    print(f"   SMTP: {SMTP_HOST}:{SMTP_PORT} (direct MX)")

    tracking = load_tracking()
    entries = tracking.get("entries", {})
    su = sum(1 for e in entries.values() if e and e[-1].get("status") == "sent")
    pu = sum(1 for e in entries.values() if e and e[-1].get("status") == "pending")
    print(f"   Tracked: {len(entries)} | Delivered: {su} | Pending: {pu}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "daemon":
        daemon_loop()
    elif cmd == "process":
        s = process_queue()
        print(f"Processed: sent={s['sent']} failed={s['failed']} deferred={s['deferred']}")
    elif cmd == "status":
        cmd_status()
    else:
        print("Commands: daemon | process | status")


if __name__ == "__main__":
    main()
