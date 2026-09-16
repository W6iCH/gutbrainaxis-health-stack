#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║     OpenClaw 任务异常监控系统 v2（脱敏版）                   ║
║     唯一告警编号 + 自动消除通知                              ║
║     所有凭据从环境变量读取，无需硬编码                        ║
╚══════════════════════════════════════════════════════════════╝

运行方式：
  python3 health_monitor.py            # 检查并发送告警
  python3 health_monitor.py --test     # 发送测试邮件
  python3 health_monitor.py --quiet    # 仅出错时告警（systemd timer 用）
  python3 health_monitor.py --status   # 查看当前告警状态
"""

import json, os, sys, sqlite3, smtplib, time, hashlib, subprocess, re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

# ── 可配置路径（从环境变量读取） ────────────────────────────────────────
OPT = os.environ.get("APP_BASE", "/opt/gutbrainaxis")
ALERT_EMAIL = os.environ.get("ALERT_TO", "")
STATE_FILE = os.environ.get("HEALTH_STATE_FILE",
                            os.path.join(OPT, ".health_state.json"))
COOLDOWN_MINUTES = 15

# ── SMTP 凭据（从环境变量读取） ─────────────────────────────────────────
SMTP_HOST = os.environ.get("ALERT_SMTP_HOST", "mail.sjtu.edu.cn")
SMTP_PORT = int(os.environ.get("ALERT_SMTP_PORT", "465"))
SENDER_EMAIL = os.environ.get("ALERT_FROM", "")
SENDER_NAME = os.environ.get("ALERT_SENDER_NAME", "Research Task Monitor")
USERNAME = os.environ.get("ALERT_SMTP_USERNAME", "")
PASSWORD = os.environ.get("ALERT_SMTP_PASSWORD", "")

# ── 阈值 ──────────────────────────────────────────────────────────────────
STALE_SURVEY_MINUTES = int(os.environ.get("STALE_SURVEY_MINUTES", "120"))
STALE_DIET_MINUTES = int(os.environ.get("STALE_DIET_MINUTES", "30"))
LLM_QUEUE_MAX_PENDING = int(os.environ.get("LLM_QUEUE_MAX_PENDING", "10"))
SYNC_MAX_GAP_MINUTES = int(os.environ.get("SYNC_MAX_GAP_MINUTES", "120"))

# ═══════════════════════ State Management ═══════════════════════

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"alerts": {}, "resolved": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)

def alert_id(text):
    """Generate unique alert ID from issue text + timestamp"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    h = hashlib.md5(text.encode()).hexdigest()[:4]
    return f"ALT-{ts}-{h}"

# ═══════════════════════ DB Helpers ═══════════════════════════

def _db_conn(rel_path):
    path = os.path.join(OPT, rel_path)
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def survey_db():
    return _db_conn(os.path.join("sjtu_survey_pro", "survey_data.db"))

def diet_db():
    return _db_conn(os.path.join("diet_survey", "diet_data.db"))

# ═══════════════════════ Notifications ══════════════════════════

def send_email(subject, body):
    if not SENDER_EMAIL or not PASSWORD or not ALERT_EMAIL:
        print(f"  ⚠ SMTP 未配置（缺 ALERT_FROM / ALERT_SMTP_PASSWORD / ALERT_TO），跳过邮件发送")
        return
    msg = MIMEMultipart()
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"] = ALERT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(USERNAME or SENDER_EMAIL, PASSWORD)
            s.sendmail(SENDER_EMAIL, [ALERT_EMAIL], msg.as_string())
        print(f"  📧 邮件已发送至 {ALERT_EMAIL}")
    except Exception as e:
        print(f"  ❌ 邮件发送失败: {e}")

# ═══════════════════════ Checks ═══════════════════════════════

def check_survey():
    issues, ok = [], []
    now = datetime.now()
    try:
        conn = survey_db()
        if conn is None:
            ok.append("问卷数据库尚未初始化（首次安装正常）")
            return issues, ok
        total = conn.execute("SELECT COUNT(*) as n FROM submissions").fetchone()["n"]
        today = conn.execute("SELECT COUNT(*) as n FROM submissions WHERE date(created_at)=date('now')").fetchone()["n"]
        ok.append(f"问卷数据库正常（共 {total} 条，今日 +{today}）")
        stale = conn.execute(
            "SELECT COUNT(*) as n FROM submissions WHERE analysis IS NULL AND created_at < datetime('now', ?)",
            (f'-{STALE_SURVEY_MINUTES} minutes',)).fetchone()["n"]
        if stale > 0:
            issues.append(f"🔴 {stale} 条问卷记录超过 {STALE_SURVEY_MINUTES} 分钟未完成分析")
        else:
            ok.append("所有问卷记录已完成分析")
        conn.close()
    except Exception as e:
        issues.append(f"🔴 问卷数据库异常: {e}")
    return issues, ok

def check_diet():
    issues, ok = [], []
    try:
        conn = diet_db()
        if conn is None:
            ok.append("饮食数据库尚未初始化（首次安装正常）")
            return issues, ok
        total = conn.execute("SELECT COUNT(*) as n FROM submissions").fetchone()["n"]
        ok.append(f"饮食数据库正常（共 {total} 条）")
        stale = conn.execute(
            "SELECT COUNT(*) as n FROM submissions WHERE dietary_advice IS NULL AND created_at < datetime('now', ?)",
            (f'-{STALE_DIET_MINUTES} minutes',)).fetchone()["n"]
        if stale > 0:
            issues.append(f"🔴 {stale} 条饮食记录超过 {STALE_DIET_MINUTES} 分钟未生成建议")
        else:
            ok.append("所有饮食记录已生成建议")
        conn.close()
    except Exception as e:
        issues.append(f"🔴 饮食数据库异常: {e}")
    return issues, ok

def check_processes():
    issues, ok = [], []
    processes = [
        ("问卷反馈", "feedback_server.py"),
        ("饮食反馈", "diet_feedback_server.py"),
        ("Webhook", "webhook_listener.py"),
        ("LLM队列", "diet_llm_queue.py"),
        ("管理控制台", "admin_console.py"),
    ]
    running = []
    for name, pattern in processes:
        try:
            result = subprocess.run(["pgrep", "-f", pattern],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                running.append(name)
                ok.append(f"{name} 运行中")
            else:
                issues.append(f"🔴 {name} 进程未运行")
        except Exception:
            issues.append(f"🔴 无法检查 {name} 进程")
    return issues, ok

def check_llm_queue():
    issues, ok = [], []
    queue_dir = os.path.join(OPT, "diet_survey", ".llm_queue")
    if not os.path.exists(queue_dir):
        ok.append("LLM队列目录尚未创建（首次安装正常）")
        return issues, ok
    for state in ("pending", "processing", "failed"):
        d = os.path.join(queue_dir, state)
        count = len(os.listdir(d)) if os.path.isdir(d) else 0
        if state == "pending" and count > LLM_QUEUE_MAX_PENDING:
            issues.append(f"🔴 LLM队列积压: {count} 个待处理任务（阈值 {LLM_QUEUE_MAX_PENDING}）")
        elif state == "failed" and count > 0:
            issues.append(f"⚠ LLM队列中有 {count} 个失败任务")
        else:
            ok.append(f"LLM队列 {state}: {count} 个")
    return issues, ok

def check_ports():
    issues, ok = [], []
    ports = [(8000, "问卷反馈"), (8001, "饮食反馈"),
             (9000, "管理控制台"), (9876, "Webhook")]
    import socket
    for port, name in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        if result == 0:
            ok.append(f"端口 :{port} ({name}) 开放")
        else:
            issues.append(f"🔴 端口 :{port} ({name}) 未开放")
    return issues, ok

# ═══════════════════════ Main ═════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description="系统健康监控")
    ap.add_argument("--test", action="store_true", help="发送测试邮件")
    ap.add_argument("--quiet", action="store_true", help="仅异常时告警")
    ap.add_argument("--status", action="store_true", help="查看告警状态")
    args = ap.parse_args()

    if args.test:
        send_email("✅ [测试] 健康监控测试",
                   f"这是一封测试邮件。\n服务器: {os.uname().nodename}\n时间: {datetime.now()}")
        return

    if args.status:
        state = load_state()
        if state["alerts"]:
            print("当前活跃告警:")
            for aid, info in state["alerts"].items():
                print(f"  {aid}: {info.get('text','')} (since {info.get('opened_at','')})")
        else:
            print("✅ 无活跃告警")
        if state["resolved"]:
            print(f"\n已消除告警 ({len(state['resolved'])} 条):")
            for r in state["resolved"][-5:]:
                print(f"  ✅ {r.get('id','')}: {r.get('text','')}")
        return

    state = load_state()
    all_issues = []
    all_ok = []
    now = datetime.now()

    for check_fn in (check_survey, check_diet, check_processes, check_llm_queue, check_ports):
        try:
            issues, ok = check_fn()
            all_issues.extend(issues)
            all_ok.extend(ok)
        except Exception as e:
            all_issues.append(f"检查异常: {e}")

    # 汇总
    has_new_alert = False
    for issue in all_issues:
        if issue.startswith("🔴"):
            aid = alert_id(issue)
            # 抖动过滤
            if aid in state["alerts"]:
                last_seen = state["alerts"][aid].get("last_seen", "")
                if last_seen:
                    try:
                        since = datetime.now() - datetime.fromisoformat(last_seen)
                        if since.total_seconds() < COOLDOWN_MINUTES * 60:
                            continue
                    except:
                        pass
            state["alerts"][aid] = {
                "text": issue,
                "opened_at": now.isoformat(),
                "last_seen": now.isoformat(),
            }
            has_new_alert = True
        elif issue.startswith("⚠"):
            state["alerts"][f"WARN-{now.strftime('%Y%m%d-%H%M%S')}"] = {
                "text": issue, "opened_at": now.isoformat(), "last_seen": now.isoformat()
            }

    # 告警消除检测
    resolved = []
    for aid in list(state["alerts"].keys()):
        if aid.startswith("ALT-"):
            text = state["alerts"][aid].get("text", "")
            # 看这个告警是否仍出现在本轮 all_issues 中
            still_active = any(text in iss for iss in all_issues)
            if not still_active:
                resolved.append(state["alerts"].pop(aid))

    # 发送通知
    if has_new_alert or resolved:
        subject_parts = []
        if has_new_alert:
            subject_parts.append(f"🚨 {len(state['alerts'])} 个告警")
        if resolved:
            subject_parts.append(f"✅ {len(resolved)} 个已消除")
        subject = " | ".join(subject_parts) if subject_parts else "健康监控报告"

        body_lines = [f"服务器: {os.uname().nodename}", f"时间: {now}"]
        if all_ok:
            body_lines.extend(["", "✅ 正常项:", *[f"  {o}" for o in all_ok]])
        if all_issues:
            body_lines.extend(["", "⚠ 异常:", *[f"  {i}" for i in all_issues]])
        if resolved:
            body_lines.extend(["", "✅ 已消除告警:"])
            for r in resolved:
                body_lines.append(f"  消除: {r.get('id','')}: {r.get('text','')}")
        body = "\n".join(body_lines)

        if not args.quiet or has_new_alert:
            send_email(subject, body)
    else:
        if not args.quiet:
            ok_count = len(all_ok)
            print(f"✅ 全部正常（{ok_count} 项检查通过）")

    save_state(state)

if __name__ == "__main__":
    sys.exit(main())
