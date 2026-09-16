#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
health_monitor.py — 系统健康巡检与分级告警
=============================================================================
逐项检查（频次由 `schedule.health_monitor_minutes` 决定）：
  问卷链路 / 饮食链路 / LLM 队列（含窗口状态） / 进程 / 端口 / Webhook 回调健康

告警语义（商用级）
------------------
* **分级**：`🔴`=critical（安全/数据丢失/服务不可用），`⚠`=warning（需关注）；
  低于 `alert.severity_threshold` 的级别不发信。
* **冷却**：同一告警 `alert.cooldown_minutes` 内不重复发信（抖动过滤）。
* **静默时段**：`schedule.quiet_hours_*` 内 warning 级只落本地日志；
  critical 级**始终发送**（不能被静默吞掉）。
* **自动消除**：告警恢复后发送「已消除」通知，闭环。
* **阈值全部可配**：`alert.stale_data_minutes` / `analysis.*`（队列窗口）。

运行方式：
  python3 health_monitor.py            # 检查并发送告警
  python3 health_monitor.py --test     # 发送测试邮件
  python3 health_monitor.py --quiet    # 仅出错时告警（systemd timer 用）
  python3 health_monitor.py --status   # 查看当前告警状态
"""

import json
import os
import sys
import sqlite3
import socket
import subprocess
import hashlib
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "common"))
try:
    from lib_schedule import load_config, alert_quiet_hours, is_quiet_hours, window_state
except Exception:                                             # pragma: no cover
    def load_config(path=None):                               # type: ignore
        return {}

    def alert_quiet_hours(cfg, now=None):                      # type: ignore
        return False

    def is_quiet_hours(cfg, now=None):                         # type: ignore
        return False

    def window_state(cfg, now=None):                           # type: ignore
        return {}

try:
    from send_alert import send_alert as _send_alert_mod
except Exception:                                             # pragma: no cover
    _send_alert_mod = None

CFG = load_config()

# ── 可配置路径（从环境变量读取） ────────────────────────────────────────
OPT = os.environ.get("APP_BASE", CFG.get("app.base_dir") or "/opt/gutbrainaxis")
ALERT_EMAIL = os.environ.get("ALERT_TO", "")
STATE_FILE = os.environ.get("HEALTH_STATE_FILE",
                            os.path.join(OPT, ".health_state.json"))
COOLDOWN_MINUTES = int(CFG.get("alert.cooldown_minutes") or 15)
SEVERITY_THRESHOLD = str(CFG.get("alert.severity_threshold") or "warning")

# ── SMTP 凭据（从环境变量读取） ─────────────────────────────────────────
SMTP_HOST = os.environ.get("ALERT_SMTP_HOST", "mail.sjtu.edu.cn")
SMTP_PORT = int(os.environ.get("ALERT_SMTP_PORT", "465"))
SENDER_EMAIL = os.environ.get("ALERT_FROM", "")
SENDER_NAME = os.environ.get("ALERT_SENDER_NAME", "Research Task Monitor")
USERNAME = os.environ.get("ALERT_SMTP_USERNAME", "")
PASSWORD = os.environ.get("ALERT_SMTP_PASSWORD", "")

# ── 阈值（全部来自 app.yaml，可用环境变量覆盖） ──────────────────────────
STALE_DATA_MINUTES = int(CFG.get("alert.stale_data_minutes") or 90)
STALE_SURVEY_MINUTES = int(os.environ.get("STALE_SURVEY_MINUTES", STALE_DATA_MINUTES))
STALE_DIET_MINUTES = int(os.environ.get("STALE_DIET_MINUTES", STALE_DATA_MINUTES))
LLM_QUEUE_MAX_PENDING = int(os.environ.get("LLM_QUEUE_MAX_PENDING", "50"))
SYNC_MAX_GAP_MINUTES = int(os.environ.get("SYNC_MAX_GAP_MINUTES", STALE_DATA_MINUTES))

SEV_LEVEL = {"info": 10, "warning": 20, "critical": 30}


# ═══════════════════════ State Management ═══════════════════════

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:                                     # noqa: BLE001
            pass
    return {"alerts": {}, "resolved": []}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, STATE_FILE)                               # 原子写：避免半写状态


def alert_id(text):
    """Generate stable alert ID from issue text (同一问题稳定去重)."""
    h = hashlib.md5(text.encode()).hexdigest()[:8]
    return f"ALT-{h}"


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

def send_email(subject, body, severity="warning"):
    """发送告警邮件；分级 + 静默时段 + 重试。"""
    # 优先复用 scripts/send_alert.py（同一实现，避免两套行为）
    if _send_alert_mod is not None:
        try:
            res = _send_alert_mod(subject, body, severity=severity)
            if res.get("reason") == "quiet_hours":
                print("  🌙 静默时段：告警已记录（未发信）")
            return res
        except Exception as e:                                # noqa: BLE001
            print(f"  ⚠ send_alert 调用失败，回退内置发送: {e}")

    if not SENDER_EMAIL or not PASSWORD or not ALERT_EMAIL:
        print("  ⚠ SMTP 未配置（缺 ALERT_FROM / ALERT_SMTP_PASSWORD / ALERT_TO），跳过邮件发送")
        return {"sent": False, "reason": "smtp_not_configured"}
    if SEV_LEVEL.get(severity, 20) < SEV_LEVEL.get(SEVERITY_THRESHOLD, 20):
        return {"sent": False, "reason": "below_threshold"}
    if severity != "critical" and alert_quiet_hours(CFG):
        print("  🌙 静默时段：仅记录不发信")
        return {"sent": False, "reason": "quiet_hours"}

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart()
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"] = ALERT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT,
                              timeout=int(CFG.get("smtp.timeout_seconds") or 30)) as s:
            s.login(USERNAME or SENDER_EMAIL, PASSWORD)
            s.sendmail(SENDER_EMAIL, [ALERT_EMAIL], msg.as_string())
        print(f"  📧 邮件已发送至 {ALERT_EMAIL}")
        return {"sent": True, "reason": "ok"}
    except Exception as e:                                    # noqa: BLE001
        print(f"  ❌ 邮件发送失败: {e}")
        return {"sent": False, "reason": f"send_failed: {e}"}


# ═══════════════════════ Checks ═══════════════════════════════

def check_survey():
    issues, ok = [], []
    try:
        conn = survey_db()
        if conn is None:
            ok.append("问卷数据库尚未初始化（首次安装正常）")
            return issues, ok
        total = conn.execute("SELECT COUNT(*) as n FROM submissions").fetchone()["n"]
        today = conn.execute("SELECT COUNT(*) as n FROM submissions "
                             "WHERE date(created_at)=date('now')").fetchone()["n"]
        ok.append(f"问卷数据库正常（共 {total} 条，今日 +{today}）")
        stale = conn.execute(
            "SELECT COUNT(*) as n FROM submissions WHERE analysis IS NULL "
            "AND created_at < datetime('now', ?)",
            (f'-{STALE_SURVEY_MINUTES} minutes',)).fetchone()["n"]
        if stale > 0:
            issues.append((f"🔴 {stale} 条问卷记录超过 {STALE_SURVEY_MINUTES} 分钟未完成分析",
                           "critical"))
        else:
            ok.append("所有问卷记录已完成分析")
        conn.close()
    except Exception as e:                                    # noqa: BLE001
        issues.append((f"🔴 问卷数据库异常: {e}", "critical"))
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
            "SELECT COUNT(*) as n FROM submissions WHERE dietary_advice IS NULL "
            "AND created_at < datetime('now', ?)",
            (f'-{STALE_DIET_MINUTES} minutes',)).fetchone()["n"]
        if stale > 0:
            issues.append((f"🔴 {stale} 条饮食记录超过 {STALE_DIET_MINUTES} 分钟未生成建议",
                           "critical"))
        else:
            ok.append("所有饮食记录已生成建议")
        conn.close()
    except Exception as e:                                    # noqa: BLE001
        issues.append((f"🔴 饮食数据库异常: {e}", "critical"))
    return issues, ok


def check_processes():
    """进程存活检查（进程名与 systemd 单元一致，修正旧版 admin_console.py 笔误）。"""
    issues, ok = [], []
    processes = [
        ("问卷反馈", "feedback_server.py"),
        ("饮食反馈", "diet_feedback_server.py"),
        ("饮食Webhook", "webhook_listener.py"),
        ("量表Webhook", "survey_webhook_listener.py"),
        ("LLM队列", "diet_llm_queue.py"),
        ("管理控制台", "admin_console/app.py"),
        ("数据看板", "data_dashboard/app.py"),
    ]
    for name, pattern in processes:
        try:
            result = subprocess.run(["pgrep", "-f", pattern],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                ok.append(f"{name} 运行中")
            else:
                issues.append((f"🔴 {name} 进程未运行", "critical"))
        except Exception:                                     # noqa: BLE001
            issues.append((f"🔴 无法检查 {name} 进程", "warning"))
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
        sev = "warning"
        if state == "pending" and count > LLM_QUEUE_MAX_PENDING:
            # 窗口调度（offpeak/hybrid）下积压是**预期**的，不告警
            ws = window_state(CFG)
            if ws.get("waiting"):
                ok.append(f"LLM队列 {state}: {count} 个（窗口外挂起，属预期）")
                continue
            issues.append((f"🔴 LLM队列积压: {count} 个待处理任务"
                           f"（阈值 {LLM_QUEUE_MAX_PENDING}）", "critical"))
        elif state == "failed" and count > 0:
            issues.append((f"⚠ LLM队列中有 {count} 个失败任务", sev))
        else:
            ok.append(f"LLM队列 {state}: {count} 个")
    return issues, ok


def check_ports():
    issues, ok = [], []
    ports = [
        (int(CFG.get("services.survey_feedback.port") or 8000), "问卷反馈"),
        (int(CFG.get("services.diet_feedback.port") or 8001), "饮食反馈"),
        (int(CFG.get("services.admin_console.port") or 9000), "管理控制台"),
        (int(CFG.get("services.data_dashboard.port") or 8090), "数据看板"),
        (int(CFG.get("services.diet_webhook.port") or 9876), "饮食Webhook"),
        (int(CFG.get("services.survey_webhook.port") or 9877), "量表Webhook"),
    ]
    for port, name in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        if result == 0:
            ok.append(f"端口 :{port} ({name}) 开放")
        else:
            issues.append((f"🔴 端口 :{port} ({name}) 未开放", "critical"))
    return issues, ok


def check_webhook_health():
    """Word Webhook 回调健康：长期无回调 → 提示回落定时（warning，非 critical）。"""
    issues, ok = [], []
    state_file = os.path.join(OPT, "sjtu_survey_pro", ".webhook_queue", "state.json")
    if not os.path.exists(state_file):
        ok.append("量表 Webhook 尚未收到过回调（依赖定时拉取，属正常起始态）")
        return issues, ok
    try:
        with open(state_file, encoding="utf-8") as f:
            st = json.load(f)
        last = st.get("last_received_at")
        if not last:
            ok.append("量表 Webhook 未收到过回调（依赖定时拉取）")
            return issues, ok
        idle_min = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60
        threshold = int(CFG.get("webhook.timer_only_after_minutes") or 180)
        if idle_min > threshold:
            issues.append((f"⚠ 量表 Webhook 已 {int(idle_min)} 分钟无回调"
                           f"（阈值 {threshold}），已自动回落定时拉取", "warning"))
        else:
            ok.append(f"量表 Webhook 正常（{int(idle_min)} 分钟前有回调）")
    except Exception as e:                                    # noqa: BLE001
        issues.append((f"⚠ 量表 Webhook 状态文件损坏: {e}", "warning"))
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
                   f"这是一封测试邮件。\n服务器: {os.uname().nodename}\n时间: {datetime.now()}",
                   severity="critical")
        return 0

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
        return 0

    state = load_state()
    all_issues = []          # [(text, severity)]
    all_ok = []
    now = datetime.now()

    for check_fn in (check_survey, check_diet, check_processes, check_llm_queue,
                     check_ports, check_webhook_health):
        try:
            issues, ok = check_fn()
            all_issues.extend(issues)
            all_ok.extend(ok)
        except Exception as e:                                # noqa: BLE001
            all_issues.append((f"检查异常: {e}", "warning"))

    max_sev = "info"
    has_new_alert = False
    for issue, sev in all_issues:
        if SEV_LEVEL[sev] > SEV_LEVEL[max_sev]:
            max_sev = sev
        if sev == "critical":
            aid = alert_id(issue)
            prev = state["alerts"].get(aid)
            if prev and prev.get("last_seen"):
                try:
                    since = datetime.now() - datetime.fromisoformat(prev["last_seen"])
                    if since.total_seconds() < COOLDOWN_MINUTES * 60:
                        prev["last_seen"] = now.isoformat()
                        continue
                except Exception:                             # noqa: BLE001
                    pass
            state["alerts"][aid] = {"text": issue, "opened_at":
                                    (prev or {}).get("opened_at", now.isoformat()),
                                    "last_seen": now.isoformat(), "severity": sev}
            has_new_alert = True
        elif sev == "warning":
            aid = alert_id(issue)
            if aid not in state["alerts"]:
                state["alerts"][aid] = {"text": issue, "opened_at": now.isoformat(),
                                        "last_seen": now.isoformat(), "severity": sev}
                has_new_alert = True

    # 告警消除检测（闭环）
    resolved = []
    active_texts = [t for t, _ in all_issues]
    for aid in list(state["alerts"].keys()):
        info = state["alerts"][aid]
        if not any(info.get("text", "") == t for t in active_texts):
            resolved.append(state["alerts"].pop(aid))
            resolved[-1]["id"] = aid

    # 发送通知
    if has_new_alert or resolved:
        subject_parts = []
        if has_new_alert:
            subject_parts.append(f"{len(state['alerts'])} 个告警")
        if resolved:
            subject_parts.append(f"{len(resolved)} 个已消除")
        subject = " | ".join(subject_parts) if subject_parts else "健康监控报告"

        body_lines = [f"服务器: {os.uname().nodename}", f"时间: {now}"]
        if all_ok:
            body_lines.extend(["", "✅ 正常项:", *[f"  {o}" for o in all_ok]])
        if all_issues:
            body_lines.extend(["", "⚠ 异常:", *[f"  {i}" for i, _ in all_issues]])
        if resolved:
            body_lines.extend(["", "✅ 已消除告警:"])
            for r in resolved:
                body_lines.append(f"  消除: {r.get('id','')}: {r.get('text','')}")
        body = "\n".join(body_lines)

        sev = "critical" if has_new_alert and max_sev == "critical" else "warning"
        if not args.quiet or has_new_alert:
            send_email(subject, body, severity=sev)
    else:
        if not args.quiet:
            print(f"✅ 全部正常（{len(all_ok)} 项检查通过）")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
