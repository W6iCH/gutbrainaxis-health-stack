"""
health_check.py — 服务探活 / 日志尾查看 / DB 检查
"""
import os, subprocess, sqlite3
from pathlib import Path
from typing import Optional


class HealthChecker:
    """各服务健康检查"""

    def __init__(self, app_base: str):
        self.app_base = Path(app_base)
        self.dry_run = bool(os.environ.get("DRY_RUN"))

    # ── 服务探活 ──────────────────────────────────────────────────

    def probe_service(self, name: str, port: int, health_url: str = "") -> dict:
        """HTTP 探活 + 端口探测"""
        result = {
            "name": name,
            "port": port,
            "http_status": None,
            "http_time_ms": None,
            "port_open": False,
            "error": None,
        }
        from datetime import datetime
        import urllib.request, urllib.error
        import socket

        # 端口探测
        if port > 0:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                r = s.connect_ex(("127.0.0.1", port))
                result["port_open"] = (r == 0)
                s.close()
            except Exception as e:
                result["error"] = f"端口探测失败: {e}"

        # HTTP 探活
        url = health_url or f"http://127.0.0.1:{port}/health"
        try:
            start = datetime.now()
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                elapsed = (datetime.now() - start).total_seconds() * 1000
                result["http_status"] = resp.status
                result["http_time_ms"] = round(elapsed, 1)
        except urllib.error.HTTPError as e:
            elapsed = (datetime.now() - start).total_seconds() * 1000 if 'start' in dir() else 0
            result["http_status"] = e.code
            result["http_time_ms"] = round(elapsed, 1)
        except Exception as e:
            result["error"] = result.get("error") or str(e)

        return result

    def probe_all(self) -> list[dict]:
        """探测所有已知服务"""
        from service_manager import SERVICE_WHITELIST
        results = []
        for sname, info in SERVICE_WHITELIST.items():
            p = info.get("port", -1)
            if p <= 0:
                continue
            r = self.probe_service(sname, p)
            r["label"] = info["label"]
            results.append(r)
        return results

    # ── 日志尾查看 ────────────────────────────────────────────────

    # 白名单日志路径模式（防止路径穿越）
    LOG_WHITELIST = {
        "admin_console": ("logs/admin_console.log", "控制台日志"),
        "diet_llm": ("diet_survey/logs/llm_queue.log", "LLM 队列"),
        "diet_feedback": ("diet_survey/logs/feedback_server.log", "饮食反馈页面"),
        "diet_webhook": ("diet_survey/logs/webhook.log", "Webhook"),
        "survey_feedback": ("sjtu_survey_pro/server.log", "问卷反馈页面"),
        "survey_sync": ("sjtu_survey_pro/.email_queue/sync_cron.log", "问卷同步"),
    }

    def read_log(self, log_key: str, n_lines: int = 100) -> dict:
        """
        读取指定日志的尾部。
        n_lines 上限 500。
        返回 {key, label, path, lines, total, error}
        """
        n_lines = min(max(n_lines, 10), 500)
        result = {"key": log_key, "label": "", "path": "", "lines": [], "total": 0, "error": None}

        if log_key not in self.LOG_WHITELIST:
            result["error"] = f"未知日志 key: {log_key}"
            return result

        rel_path, label = self.LOG_WHITELIST[log_key]
        result["label"] = label

        # 路径穿越检测：确保 rel_path 不包含 ..
        abs_path = Path(rel_path)
        full_path = (self.app_base / rel_path).resolve()
        if ".." in rel_path.split("/") or not str(full_path).startswith(str(self.app_base.resolve())):
            result["error"] = "路径穿越被禁止"
            return result

        full = self.app_base / rel_path
        result["path"] = str(full)

        if not full.exists():
            result["error"] = "日志文件不存在"
            return result

        try:
            all_lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
            result["total"] = len(all_lines)
            result["lines"] = all_lines[-n_lines:]
        except OSError as e:
            result["error"] = str(e)

        return result

    # ── DB 可读写检查 ─────────────────────────────────────────────

    def check_db(self, db_path: str, label: str = "") -> dict:
        """SQLite 数据库健康检查"""
        result = {
            "path": db_path,
            "label": label or db_path,
            "readable": False,
            "writable": False,
            "integrity": None,
            "record_count": 0,
            "error": None,
        }
        full = Path(db_path)
        if not full.is_absolute():
            full = self.app_base / db_path
        result["path"] = str(full)

        if not full.exists():
            result["error"] = "数据库文件不存在"
            return result

        try:
            conn = sqlite3.connect(f"file:{full}?mode=ro", uri=True)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM submissions")
            result["record_count"] = c.fetchone()[0]
            c.execute("PRAGMA quick_check")
            result["integrity"] = c.fetchone()[0]
            result["readable"] = True
            conn.close()
        except Exception as e:
            result["error"] = f"读取失败: {e}"
            return result

        # 写测试（写入临时表后回滚）
        try:
            conn = sqlite3.connect(str(full))
            c = conn.cursor()
            c.execute("CREATE TEMP TABLE IF NOT EXISTS _console_test (id INTEGER)")
            c.execute("INSERT INTO _console_test VALUES (1)")
            conn.rollback()
            conn.close()
            result["writable"] = True
        except Exception:
            result["writable"] = False

        return result

    def check_all_dbs(self) -> list[dict]:
        """检查所有已知数据库"""
        dbs = [
            ("sjtu_survey_pro/survey_data.db", "问卷数据库"),
            ("diet_survey/diet_data.db", "饮食数据库"),
            ("exercise_survey/exercise_data.db", "运动数据库"),
        ]
        results = []
        for rel, label in dbs:
            results.append(self.check_db(rel, label))
        return results
