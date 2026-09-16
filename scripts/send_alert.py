#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
send_alert.py — 告警邮件发送器（分级 + 静默时段 + 重试）
=============================================================================
所有凭据从环境变量读取（由 install.sh / app.yaml 渲染）。

用法::

    python3 scripts/send_alert.py "主题" "正文"
    python3 scripts/send_alert.py --severity critical "主题" "正文"
    python3 scripts/send_alert.py --severity info --force "主题" "正文"   # 静默时段也发

分级（alert.severity_threshold 为下限，低于下限不发）
-----------------------------------------------------
    info < warning < critical

静默时段（`schedule.quiet_hours_*` / `alert.quiet_hours_*`）
-----------------------------------------------------------
* `critical` 级**始终发送**（安全/数据丢失类告警不能被静默吞掉）；
* 其它级别在静默时段内**只记录不发信**（返回码 0，附 reason=quiet_hours），
  避免夜间骚扰，同时保留可观测性。
"""
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "common"))
try:
    from lib_schedule import load_config, alert_quiet_hours   # noqa: E402
except Exception:                                             # pragma: no cover
    def load_config(path=None):                               # type: ignore
        return {}

    def alert_quiet_hours(cfg, now=None):                      # type: ignore
        return False

# 全部凭据来自环境变量，见 config/app.yaml.example（安装时由 install.sh 渲染）
SMTP_HOST = os.environ.get("ALERT_SMTP_HOST", "mail.sjtu.edu.cn")
SMTP_PORT = int(os.environ.get("ALERT_SMTP_PORT", "465"))
FROM = os.environ.get("ALERT_FROM", "")
SENDER_NAME = os.environ.get("ALERT_SENDER_NAME", "Research Task Monitor")
USERNAME = os.environ.get("ALERT_SMTP_USERNAME", "")
PASS = os.environ.get("ALERT_SMTP_PASSWORD", "")
TO = os.environ.get("ALERT_TO", "")

SEVERITIES = {"info": 10, "warning": 20, "critical": 30}
# 配置优先，环境变量兜底
_THRESHOLD_ENV = os.environ.get("ALERT_SEVERITY_THRESHOLD", "warning")


def _threshold() -> int:
    cfg = load_config()
    name = str(cfg.get("alert.severity_threshold") or _THRESHOLD_ENV or "warning").lower()
    return SEVERITIES.get(name, 20)


def _quiet(cfg) -> bool:
    """告警静默判定：alert.quiet_hours_* 优先，否则继承 schedule.quiet_hours_*。"""
    try:
        return bool(alert_quiet_hours(cfg))
    except Exception:                                         # noqa: BLE001
        return False


def send_alert(subject, body, severity: str = "warning", force: bool = False) -> dict:
    """
    发送告警邮件。返回 {sent: bool, reason: str}。
    不抛异常（告警通道自身故障不应把调用方打挂）。
    """
    sev = str(severity or "warning").lower()
    if sev not in SEVERITIES:
        sev = "warning"
    cfg = load_config()

    if SEVERITIES[sev] < _threshold():
        print(f"↩ 级别 {sev} 低于告警阈值（{_threshold()}），不发信")
        return {"sent": False, "reason": "below_threshold"}

    if sev != "critical" and not force and _quiet(cfg):
        print(f"🌙 静默时段：{sev} 级告警仅记录不发信（critical 仍会发送）")
        _log_quiet(subject, body, sev)
        return {"sent": False, "reason": "quiet_hours"}

    if not FROM or not PASS or not TO:
        print("⚠ SMTP 未完整配置（缺 ALERT_FROM / ALERT_SMTP_PASSWORD / ALERT_TO），跳过")
        return {"sent": False, "reason": "smtp_not_configured"}

    prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(sev, "⚠️")
    msg = MIMEMultipart()
    msg["From"] = f"{SENDER_NAME} <{FROM}>"
    msg["To"] = TO
    msg["Subject"] = f"{prefix} [{sev.upper()}] {subject}"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    timeout = int(cfg.get("smtp.timeout_seconds") or 30)
    max_attempts = int(cfg.get("smtp.send_retry_max_attempts") or 3)
    backoff = int(cfg.get("smtp.send_retry_backoff_seconds") or 60)
    last_err = ""
    for attempt in range(1, max_attempts + 2):
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout) as s:
                s.login(USERNAME or FROM, PASS)
                s.sendmail(FROM, [TO], msg.as_string())
            print(f"[{datetime.now()}] 告警邮件已发送至 {TO}（{sev}）")
            return {"sent": True, "reason": "ok", "attempts": attempt}
        except Exception as e:                                # noqa: BLE001
            last_err = str(e)
            print(f"  ❌ 第 {attempt} 次发送失败: {last_err}")
            if attempt <= max_attempts:
                import time as _t
                _t.sleep(min(backoff * attempt, 30))
    return {"sent": False, "reason": f"send_failed: {last_err}"}


def _log_quiet(subject, body, severity):
    """静默时段把告警落到本地文件，保证「不发但有据可查」。"""
    try:
        base = os.environ.get("APP_BASE", "/opt/gutbrainaxis")
        path = os.path.join(base, "logs", "alerts_quiet.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] [{severity}] "
                    f"{subject}\n{body}\n---\n")
    except OSError:
        pass


if __name__ == "__main__":
    argv = sys.argv[1:]
    severity = "warning"
    force = False
    while argv and argv[0].startswith("--"):
        if argv[0] == "--force":
            force = True
            argv.pop(0)
        elif argv[0] == "--severity" and len(argv) > 1:
            severity = argv[1]
            argv = argv[2:]
        else:
            break
    if len(argv) >= 2:
        send_alert(argv[0], argv[1], severity, force)
    elif len(argv) == 1:
        send_alert("告警通知", argv[0], severity, force)
    else:
        print("用法: python3 send_alert.py [--severity info|warning|critical] [--force] <主题> [正文]")
        print("凭据从环境变量读取（ALERT_SMTP_HOST, ALERT_FROM, ALERT_SMTP_PASSWORD, ALERT_TO）")
        sys.exit(1)
