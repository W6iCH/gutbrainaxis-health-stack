#!/usr/bin/env python3
"""
send_alert.py — 发送告警邮件
所有凭据从环境变量读取（由 install.sh 在配置中生成）。

用法:
    python3 scripts/send_alert.py "主题" "正文"
"""
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# 全部凭据来自环境变量，见 config/env.example（安装时由 install.sh 生成）
SMTP_HOST = os.environ.get("ALERT_SMTP_HOST", "mail.sjtu.edu.cn")
SMTP_PORT = int(os.environ.get("ALERT_SMTP_PORT", "465"))
FROM = os.environ.get("ALERT_FROM", "")
USERNAME = os.environ.get("ALERT_SMTP_USERNAME", "")
PASS = os.environ.get("ALERT_SMTP_PASSWORD", "")
TO = os.environ.get("ALERT_TO", "")

def send_alert(subject, body):
    if not FROM or not PASS or not TO:
        print("⚠ SMTP 未完整配置（缺 ALERT_FROM / ALERT_SMTP_PASSWORD / ALERT_TO），跳过")
        return

    msg = MIMEMultipart()
    msg["From"] = FROM
    msg["To"] = TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.login(USERNAME or FROM, PASS)
        s.sendmail(FROM, [TO], msg.as_string())
    print(f"[{datetime.now()}] 告警邮件已发送至 {TO}")

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        send_alert(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 2:
        send_alert("告警通知", sys.argv[1])
    else:
        print("用法: python3 send_alert.py <主题> <正文>")
        print("所有凭据从环境变量读取（ALERT_SMTP_HOST, ALERT_FROM, ALERT_SMTP_PASSWORD, ALERT_TO）")
        sys.exit(1)
