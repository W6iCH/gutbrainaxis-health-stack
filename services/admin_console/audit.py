"""
audit.py — 审计日志模块
"""
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


class AuditLogger:
    """结构化审计日志，写入 logs/console_audit.log"""

    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / "console_audit.log"

    def log(self, event: str, operator: str, result: str,
            summary_before: str = "", summary_after: str = "",
            detail: str = "") -> None:
        """
        写入审计记录。
        event:  事件类型（config_change / service_start / port_update / login / ...）
        operator: 操作人（用户名或 IP）
        result: 结果（success / failure / denied）
        summary_before / summary_after: 变更前后摘要
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{timestamp}] EVENT={event} "
            f"OPERATOR={operator} "
            f"RESULT={result} "
            f"BEFORE={summary_before} "
            f"AFTER={summary_after} "
            f"DETAIL={detail or '-'}\n"
        )
        try:
            with open(str(self.log_path), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # 日志写入失败不应影响主流程

    def read(self, limit: int = 200) -> list[str]:
        """读取最近 N 条审计日志"""
        if not self.log_path.exists():
            return ["(审计日志为空)"]
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
            return lines[-limit:] if len(lines) > limit else lines
        except OSError as e:
            return [f"(读取审计日志失败: {e})"]


# 单例
audit_logger: AuditLogger = None
