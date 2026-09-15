#!/usr/bin/env python3
"""
Diet Survey Email Feedback — 饮食建议邮件发送系统
==================================================
发送个性化饮食建议报告邮件（通过 SJTU SMTP 双路由（校内MX直连 + 校外SSL认证））。
与 sjtu_survey_pro 邮件配置完全一致。
支持持久化队列、指数退避重试、HTML 邮件。

用法:
    python3 diet_email_feedback.py send <recipient_email> [submission_id]
    python3 diet_email_feedback.py queue
    python3 diet_email_feedback.py process
    python3 diet_email_feedback.py daemon
    python3 diet_email_feedback.py status
"""

import json
import os
import sys
import smtplib
import ssl
import time
import logging
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ── Email Configuration — 与 sjtu_survey_pro 一致 ──────────────────────

SMTP_HOST = os.environ.get("SMTP_HOST", "mx01.sjtu.edu.cn")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
SENDER_EMAIL = os.environ.get("SMTP_SENDER_EMAIL", "")
SENDER_NAME = os.environ.get("SMTP_SENDER_NAME", "Diet Feedback System")
USERNAME = os.environ.get("SMTP_USERNAME", "")
PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_SSL_HOST = os.environ.get("SMTP_SSL_HOST", "mail.sjtu.edu.cn")
SMTP_SSL_PORT = int(os.environ.get("SMTP_SSL_PORT", "465"))
ADMIN_EMAIL = os.environ.get("SMTP_ADMIN_EMAIL", "")

# ── Queue Configuration ───────────────────────────────────────────────────

QUEUE_DIR = os.path.join(BASE_DIR, ".email_queue")
PENDING_DIR = os.path.join(QUEUE_DIR, "pending")
FAILED_DIR = os.path.join(QUEUE_DIR, "failed")
SENT_DIR = os.path.join(QUEUE_DIR, "sent")
LOG_FILE = os.path.join(QUEUE_DIR, "email_worker.log")

MAX_RETRIES = 10
RETRY_DELAYS = [60, 120, 300, 600, 1800, 3600, 7200, 14400, 28800, 86400]
QUEUE_POLL_INTERVAL = 30

# ── Logging Setup ─────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("diet_email")


# ── Queue Management ──────────────────────────────────────────────────────

def ensure_dirs():
    for d in [QUEUE_DIR, PENDING_DIR, FAILED_DIR, SENT_DIR]:
        os.makedirs(d, exist_ok=True)


def queue_email(recipient: str, subject: str, html_body: str,
                submission_id: str = "") -> str:
    ensure_dirs()

    unique = f"{recipient}_{datetime.now().isoformat()}_{os.urandom(4).hex()}"
    item_id = hashlib.sha256(unique.encode()).hexdigest()[:16]

    item = {
        "id": item_id,
        "recipient": recipient,
        "subject": subject,
        "html_body": html_body,
        "submission_id": submission_id,
        "created_at": datetime.now().isoformat(),
        "attempts": 0,
        "last_attempt": None,
        "next_attempt": datetime.now().isoformat(),
        "status": "pending",
        "error_log": [],
    }

    filepath = os.path.join(PENDING_DIR, f"{item_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)

    log.info(f"Queued email [{item_id}] -> {recipient}")
    return item_id


def load_queue_items(directory: str) -> list:
    items = []
    if not os.path.isdir(directory):
        return items
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(directory, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                items.append(json.load(f))
        except Exception as e:
            log.warning(f"Failed to load {filepath}: {e}")
            corrupt_path = os.path.join(QUEUE_DIR, "corrupted")
            os.makedirs(corrupt_path, exist_ok=True)
            os.rename(filepath, os.path.join(corrupt_path, filename))
    return items


def move_item(item: dict, source_dir: str, dest_dir: str):
    ensure_dirs()
    src = os.path.join(source_dir, f"{item['id']}.json")
    dst = os.path.join(dest_dir, f"{item['id']}.json")

    if os.path.exists(src):
        if os.path.exists(dst):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = os.path.join(dest_dir, f"{item['id']}_{ts}.json")
        try:
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(item, f, ensure_ascii=False, indent=2)
            os.remove(src)
            return dst
        except Exception as e:
            log.error(f"Failed to move item {item['id']}: {e}")
            return None
    else:
        if not os.path.exists(dst):
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(item, f, ensure_ascii=False, indent=2)
        return dst


def update_item(item: dict):
    directory = {
        "pending": PENDING_DIR,
        "failed": FAILED_DIR,
        "sent": SENT_DIR,
    }.get(item.get("status", "pending"), PENDING_DIR)

    filepath = os.path.join(directory, f"{item['id']}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)


# ── Email Building ────────────────────────────────────────────────────────

def _markdown_to_email_html(text: str) -> str:
    """Convert markdown advice to email-safe HTML."""
    if not text:
        return "<p style='color:#888;text-align:center;'>暂无饮食建议数据。</p>"

    lines = text.split("\n")
    result = []
    in_list = False

    for line in lines:
        s = line.strip()
        if s.startswith("## "):
            if in_list:
                result.append("</ul>")
                in_list = False
            result.append(f'<h2 style="color:#2d3436;border-bottom:2px solid #43e97b;padding-bottom:8px;margin-top:28px;">{s[3:]}</h2>')
        elif s.startswith("### "):
            if in_list:
                result.append("</ul>")
                in_list = False
            result.append(f'<h3 style="color:#444;margin-top:16px;">{s[4:]}</h3>')
        elif s.startswith("- ") or s.startswith("* "):
            if not in_list:
                result.append('<ul style="padding-left:20px;">')
                in_list = True
            result.append(f"<li>{s[2:]}</li>")
        elif not s:
            if in_list:
                result.append("</ul>")
                in_list = False
            result.append("<br>")
        else:
            if in_list:
                result.append("</ul>")
                in_list = False
            result.append(f'<p style="margin:8px 0;line-height:1.8;">{s}</p>')

    if in_list:
        result.append("</ul>")

    return "\n".join(result)


def build_diet_email(record: dict) -> tuple:
    """Build email subject and HTML body from a diet record."""
    name = record.get("name", "尊敬的用户")
    record_date = record.get("record_date", "未知日期")
    advice = record.get("dietary_advice") or ""
    answer_id = record.get("redirect_answer") or record.get("submission_id", "")

    advice_html = _markdown_to_email_html(advice)

    subject = f"🥗 饮食建议报告 — {name} ({record_date})"

    html_body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
        background: #f0f2f5;
        padding: 20px;
        margin: 0;
    }}
    .container {{
        max-width: 640px;
        margin: 0 auto;
        background: white;
        border-radius: 16px;
        overflow: hidden;
        box-shadow: 0 4px 20px rgba(0,0,0,0.08);
    }}
    .email-header {{
        background: linear-gradient(135deg, #43e97b, #38f9d7);
        padding: 32px;
        text-align: center;
        color: white;
    }}
    .email-header h1 {{
        margin: 0;
        font-size: 24px;
    }}
    .email-header p {{
        margin: 8px 0 0 0;
        opacity: 0.9;
        font-size: 14px;
    }}
    .email-body {{
        padding: 32px;
        line-height: 1.9;
        font-size: 15px;
        color: #333;
    }}
    .email-body h2 {{
        font-size: 20px;
        color: #2d3436;
        border-bottom: 2px solid #43e97b;
        padding-bottom: 8px;
        margin: 28px 0 16px 0;
    }}
    .email-body h3 {{
        font-size: 17px;
        color: #444;
        margin: 18px 0 10px 0;
    }}
    .email-body ul {{
        padding-left: 20px;
        margin: 10px 0;
    }}
    .email-body li {{
        margin: 6px 0;
    }}
    .email-body p {{
        margin: 8px 0;
    }}
    .email-footer {{
        background: #f8f9fa;
        padding: 20px 32px;
        text-align: center;
        font-size: 12px;
        color: #888;
        line-height: 1.8;
    }}
    .email-footer hr {{
        border: none;
        border-top: 1px solid #eee;
        margin-bottom: 16px;
    }}
    .badge {{
        display: inline-block;
        background: #e8f8f0;
        color: #27ae60;
        padding: 3px 10px;
        border-radius: 10px;
        font-size: 12px;
        margin: 4px;
    }}
</style>
</head>
<body>
<div class="container">
    <div class="email-header">
        <h1>🥗 饮食建议报告</h1>
        <p>{name} · 上海交通大学</p>
    </div>
    <div class="email-body">
        <p style="color:#888;text-align:center;margin-bottom:24px;">
            📅 {record_date} ｜ 📋 答卷 #{answer_id}
        </p>
        {advice_html}
    </div>
    <div class="email-footer">
        <hr>
        <p>本报告由上海交通大学饮食记录系统自动生成</p>
        <p>饮食建议基于《中国居民膳食指南（2022）》，仅供课程学习参考</p>
        <p style="color:#bbb;">如有健康方面的疑问，请咨询专业医疗人员</p>
    </div>
</div>
</body>
</html>"""

    return subject, html_body


# ── SMTP Sending ──────────────────────────────────────────────────────────



def _is_sjtu_email(recipient: str) -> bool:
    return recipient.lower().endswith("@sjtu.edu.cn")


def send_email(item: dict) -> bool:
    """Send a single email via appropriate route.
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

def _build_message(item: dict) -> MIMEMultipart:
    """Build MIME message for an email item."""
    recipient = item["recipient"]
    subject = item["subject"]
    html_body = item["html_body"]
    plain_text = '请使用支持HTML的邮件客户端查看本报告。如需帮助，请联系系统管理员。'

    msg = MIMEMultipart("alternative")
    msg['From'] = f"{Header(SENDER_NAME, 'utf-8').encode()} <{SENDER_EMAIL}>"
    msg['To'] = recipient
    msg['Subject'] = Header(subject, 'utf-8')
    msg['Message-ID'] = f"<diet-feedback-{item['id']}@sjtu.edu.cn>"
    msg["Date"] = Header(datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800"))
    msg.attach(MIMEText(plain_text, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    return msg


def _send_via_mx(item: dict) -> bool:
    """Send via mx01.sjtu.edu.cn:25 -- direct MX, no auth (internal only)."""
    recipient = item["recipient"]
    msg = _build_message(item)
    conn = None
    try:
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        conn.ehlo("ecs-aab7")
        conn.sendmail(SENDER_EMAIL, [recipient], msg.as_string())
        log.info(f'\u2713 [MX] Email sent to {recipient} [{item["id"]}]')
        return True
    except smtplib.SMTPAuthenticationError as e:
        log.error(f'\u2717 [MX] Auth failed for {recipient}: {e}')
        return False
    except smtplib.SMTPRecipientsRefused as e:
        log.error(f'\u2717 [MX] Recipient refused {recipient}: {e}')
        return False
    except smtplib.SMTPSenderRefused as e:
        log.error(f'\u2717 [MX] Sender refused: {e}')
        return False
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as e:
        log.warning(f'\u26a0 [MX] Connection issue for {recipient}: {e}')
        return False
    except smtplib.SMTPException as e:
        log.warning(f'\u26a0 [MX] SMTP error for {recipient}: {type(e).__name__}: {e}')
        return False
    except TimeoutError as e:
        log.warning(f'\u26a0 [MX] Timeout for {recipient}: {e}')
        return False
    except Exception as e:
        log.error(f'\u2717 [MX] Unexpected error for {recipient}: {type(e).__name__}: {e}')
        return False
    finally:
        if conn:
            try:
                conn.quit()
            except:
                pass


def _send_via_ssl(item: dict) -> bool:
    """Send via mail.sjtu.edu.cn:465 -- SMTP SSL with auth (external)."""
    recipient = item["recipient"]
    msg = _build_message(item)
    conn = None
    try:
        context = ssl.create_default_context()
        conn = smtplib.SMTP_SSL(SMTP_SSL_HOST, SMTP_SSL_PORT, context=context, timeout=30)
        conn.login(USERNAME, PASSWORD)
        conn.sendmail(SENDER_EMAIL, [recipient], msg.as_string())
        log.info(f'\u2713 [SSL] Email sent to {recipient} [{item["id"]}]')
        return True
    except smtplib.SMTPAuthenticationError as e:
        log.error(f'\u2717 [SSL] Auth failed for {recipient}: {e}')
        return False
    except smtplib.SMTPRecipientsRefused as e:
        log.error(f'\u2717 [SSL] Recipient refused {recipient}: {e}')
        return False
    except smtplib.SMTPSenderRefused as e:
        log.error(f'\u2717 [SSL] Sender refused: {e}')
        return False
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as e:
        log.warning(f'\u26a0 [SSL] Connection issue for {recipient}: {e}')
        return False
    except smtplib.SMTPException as e:
        log.warning(f'\u26a0 [SSL] SMTP error for {recipient}: {type(e).__name__}: {e}')
        return False
    except TimeoutError as e:
        log.warning(f'\u26a0 [SSL] Timeout for {recipient}: {e}')
        return False
    except Exception as e:
        log.error(f'\u2717 [SSL] Unexpected error for {recipient}: {type(e).__name__}: {e}')
        return False
    finally:
        if conn:
            try:
                conn.quit()
            except:
                pass

def send_fallback_notification(item: dict, error_msg: str):
    """通知管理员：某封邮件彻底失败。"""
    recipient = item["recipient"]
    subject = f"[饮食记录] 投递失败 — {recipient}"
    body = (
        f"以下饮食建议邮件在多次重试后未能成功投递，已转交给管理员处理。\n\n"
        f"原始收件人: {recipient}\n"
        f"提交ID: {item.get('submission_id', 'N/A')}\n"
        f"重试次数: {item['attempts']}\n"
        f"最后错误: {error_msg}\n\n"
        f"—— 饮食记录系统（自动通知）"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = f"{Header(SENDER_NAME, 'utf-8').encode()} <{SENDER_EMAIL}>"
    msg["To"] = ADMIN_EMAIL
    msg["Subject"] = Header(subject, "utf-8")
    conn = None
    try:
        context = ssl.create_default_context()
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        conn.ehlo("ecs-aab7")
        conn.sendmail(SENDER_EMAIL, [ADMIN_EMAIL], msg.as_string())
        log.info(f"✓ Fallback notification sent to admin for {recipient}")
        return True
    except Exception as e:
        log.error(f"✗ Fallback notification also failed: {e}")
        return False
    finally:
        if conn:
            try:
                conn.quit()
            except:
                pass


# ── Queue Processing ──────────────────────────────────────────────────────

def process_queue(max_emails: int = None) -> dict:
    stats = {"sent": 0, "failed": 0, "skipped": 0, "total": 0}
    now = datetime.now()

    pending_items = load_queue_items(PENDING_DIR)
    stats["total"] = len(pending_items)

    if max_emails:
        pending_items = pending_items[:max_emails]

    for item in pending_items:
        next_attempt = item.get("next_attempt")
        if next_attempt:
            try:
                if datetime.fromisoformat(next_attempt) > now:
                    stats["skipped"] += 1
                    continue
            except:
                pass

        success = send_email(item)

        if success:
            item["status"] = "sent"
            item["last_attempt"] = now.isoformat()
            item["sent_at"] = now.isoformat()
            move_item(item, PENDING_DIR, SENT_DIR)
            stats["sent"] += 1
        else:
            item["attempts"] += 1
            item["last_attempt"] = now.isoformat()
            item.setdefault("error_log", [])

            if item["attempts"] >= MAX_RETRIES:
                log.warning(f"MAX RETRIES for [{item['id']}] -> {item['recipient']}")
                item["status"] = "failed"
                error_msg = item["error_log"][-1].get("error", "Max retries exceeded") if item["error_log"] else "Max retries exceeded" 
                item["error_log"].append({
                    "time": now.isoformat(),
                    "attempt": item["attempts"],
                    "error": "Max retries exceeded",
                })
                send_fallback_notification(item, error_msg)
                move_item(item, PENDING_DIR, FAILED_DIR)
                stats["failed"] += 1
            else:
                delay_idx = min(item["attempts"] - 1, len(RETRY_DELAYS) - 1)
                delay = RETRY_DELAYS[delay_idx]
                next_time = now + timedelta(seconds=delay)
                item["next_attempt"] = next_time.isoformat()
                item["error_log"].append({
                    "time": now.isoformat(),
                    "attempt": item["attempts"],
                    "next_retry": next_time.isoformat(),
                    "delay_seconds": delay,
                })
                update_item(item)

    return stats


def daemon_loop():
    log.info("=" * 60)
    log.info("  Diet Email Feedback Daemon Started")
    log.info(f"  Queue: {QUEUE_DIR}")
    log.info(f"  Max retries: {MAX_RETRIES}")
    log.info(f"  Poll interval: {QUEUE_POLL_INTERVAL}s")
    log.info("=" * 60)

    while True:
        try:
            pending = len(load_queue_items(PENDING_DIR))
            if pending > 0:
                log.info(f"Processing queue ({pending} pending)...")
                stats = process_queue()
                log.info(f"  Sent: {stats['sent']}, Failed: {stats['failed']}")
            time.sleep(QUEUE_POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("Daemon stopped.")
            break
        except Exception as e:
            log.error(f"Daemon error: {e}")
            time.sleep(60)


def send_immediate(recipient: str, submission_id: str = "",
                   record: dict = None) -> str:
    """
    Send diet advice email immediately.
    Falls back to queue if immediate send fails.
    """
    if record is None:
        log.error(f"No record data for {recipient}")
        return None

    subject, html_body = build_diet_email(record)

    unique = f"{recipient}_{datetime.now().isoformat()}_{os.urandom(4).hex()}"
    item_id = hashlib.sha256(unique.encode()).hexdigest()[:16]

    item = {
        "id": item_id,
        "recipient": recipient,
        "subject": subject,
        "html_body": html_body,
        "submission_id": submission_id,
        "attempts": 0,
        "status": "pending",
        "next_attempt": datetime.now().isoformat(),
        "error_log": [],
    }

    success = send_email(item)
    if success:
        item["status"] = "sent"
        item["sent_at"] = datetime.now().isoformat()
        move_item(item, SENT_DIR, SENT_DIR)
        log.info(f"✓ Immediate send to {recipient} (ID: {item_id})")
    else:
        filepath = os.path.join(PENDING_DIR, f"{item_id}.json")
        ensure_dirs()
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False, indent=2)
        item["attempts"] = 1
        item["last_attempt"] = datetime.now().isoformat()
        delay = RETRY_DELAYS[0]
        item["next_attempt"] = (datetime.now() + timedelta(seconds=delay)).isoformat()
        update_item(item)
        log.info(f"⚠ Queued {item_id} for retry in {delay}s")

    return item_id


# ── CLI Interface ─────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("""
Diet Survey Email Feedback System
==================================
Usage:
    python3 diet_email_feedback.py send <email> [submission_id]
    python3 diet_email_feedback.py queue
    python3 diet_email_feedback.py process [max]
    python3 diet_email_feedback.py daemon
    python3 diet_email_feedback.py status
    python3 diet_email_feedback.py resend
    python3 diet_email_feedback.py clear
""")
        return 1

    cmd = sys.argv[1]
    args = sys.argv[2:]
    ensure_dirs()

    if cmd == "send":
        recipient = args[0] if args else input("Recipient: ").strip()
        sid = args[1] if len(args) > 1 else ""
        item_id = send_immediate(recipient, sid)
        if item_id:
            print(f"✓ Email to {recipient} (ID: {item_id})")
        else:
            print(f"✗ Failed for {recipient}")

    elif cmd == "queue":
        items = load_queue_items(PENDING_DIR)
        if not items:
            print("📪 Queue is empty.")
        else:
            print(f"\n📨 Pending: {len(items)}")
            for item in items:
                print(f"  [{item['id'][:8]}] {item['recipient']:<30s} "
                      f"attempt {item['attempts']}/{MAX_RETRIES}")

    elif cmd == "process":
        max_n = int(args[0]) if args else None
        stats = process_queue(max_n)
        print(f"Sent: {stats['sent']}, Failed: {stats['failed']}, "
              f"Skipped: {stats['skipped']}/{stats['total']}")

    elif cmd == "daemon":
        daemon_loop()

    elif cmd == "status":
        pending = len(load_queue_items(PENDING_DIR)) if os.path.isdir(PENDING_DIR) else 0
        sent = len([f for f in os.listdir(SENT_DIR) if f.endswith(".json")]) if os.path.isdir(SENT_DIR) else 0
        failed = len([f for f in os.listdir(FAILED_DIR) if f.endswith(".json")]) if os.path.isdir(FAILED_DIR) else 0
        print(f"""
╔══════════════════════════════════════╗
║   Diet Email Queue Status            ║
╠══════════════════════════════════════╣
║  Pending: {pending:>4d}                            ║
║  Sent:    {sent:>4d}                            ║
║  Failed:  {failed:>4d}                            ║
║  Max Retries: {MAX_RETRIES:>2d}                         ║
║  SMTP:     {SMTP_HOST}:{SMTP_PORT}               ║
╚══════════════════════════════════════╝
""")

    elif cmd == "resend":
        if os.path.isdir(FAILED_DIR):
            count = 0
            for f in os.listdir(FAILED_DIR):
                if not f.endswith(".json"):
                    continue
                src = os.path.join(FAILED_DIR, f)
                try:
                    with open(src, "r", encoding="utf-8") as fh:
                        item = json.load(fh)
                    item["status"] = "pending"
                    item["attempts"] = 0
                    item["next_attempt"] = datetime.now().isoformat()
                    move_item(item, FAILED_DIR, PENDING_DIR)
                    count += 1
                except Exception as e:
                    log.error(f"Failed to resend {f}: {e}")
            print(f"✓ Moved {count} failed emails back to pending queue.")

    elif cmd == "clear":
        print("⚠️  This will remove ALL queued emails.")
        confirm = input("Type 'YES' to confirm: ")
        if confirm == "YES":
            for d in [PENDING_DIR, FAILED_DIR, SENT_DIR]:
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        if f.endswith(".json"):
                            os.remove(os.path.join(d, f))
            print("✓ All queues cleared.")

    else:
        print(f"Unknown command: {cmd}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
