"""
service_manager.py — 进程管理（systemd → supervisor → PID）
"""
import os, signal, time, json, subprocess
from pathlib import Path
from typing import Optional

# 白名单：只允许操作这些服务名
SERVICE_WHITELIST = {
    "survey_feedback": {
        "label": "问卷反馈页面",
        "port": 8000,
        "workdir": "sjtu_survey_pro",
        "cmd": ["python3", "feedback_server.py", "8000", "8080"],
    },
    "diet_feedback": {
        "label": "饮食反馈页面",
        "port": 8001,
        "workdir": "diet_survey",
        "cmd": ["python3", "diet_feedback_server.py", "8001"],
    },
    "webhook": {
        "label": "Webhook 接收器",
        "port": 9876,
        "workdir": "diet_survey",
        "cmd": ["python3", "webhook_listener.py", "9876"],
    },
    "diet_llm_queue": {
        "label": "LLM 分析队列",
        "port": -1,
        "workdir": "diet_survey",
        "cmd": ["python3", "diet_llm_queue.py"],
    },
    "diet_email": {
        "label": "饮食邮件守护进程",
        "port": -1,
        "workdir": "diet_survey",
        "cmd": ["python3", "diet_email_feedback.py", "daemon"],
    },
    "survey_email": {
        "label": "问卷邮件守护进程",
        "port": -1,
        "workdir": "sjtu_survey_pro",
        "cmd": ["python3", "email_feedback.py", "daemon"],
    },
    "admin_console": {
        "label": "管理控制台（自身）",
        "port": 9000,
        "workdir": "",
        "cmd": [],
    },
    "exercise_sync": {
        "label": "运动问卷同步",
        "port": -1,
        "workdir": "exercise_survey",
        "cmd": ["python3", "exercise_sync_cron.py"],
    },
}


class ServiceManager:
    """进程管理：优先 systemd，降级 PID 文件管理"""

    def __init__(self, app_base: str):
        self.app_base = Path(app_base)
        self.dry_run = bool(os.environ.get("DRY_RUN"))
        self._pid_dir = Path(app_base) / ".console_pids"
        if not self.dry_run:
            self._pid_dir.mkdir(parents=True, exist_ok=True)
        self._systemd_map = {
            "survey_feedback": "research-survey-feedback",
            "diet_feedback": "research-diet-feedback",
            "webhook": "research-webhook",
            "diet_llm_queue": "research-diet-llm",
        }

    def _systemd_available(self) -> bool:
        """检查 systemd 是否可用"""
        if self.dry_run:
            return False
        try:
            r = subprocess.run(["systemctl", "--version"],
                               capture_output=True, timeout=3)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _systemd_unit(self, service: str) -> Optional[str]:
        return self._systemd_map.get(service)

    def _pid_file(self, service: str) -> Path:
        return self._pid_dir / f"{service}.pid"

    # ── 状态查询 ──────────────────────────────────────────────────

    def status(self, service: str) -> dict:
        """
        返回 {name, label, active, enabled, pid, start_time, memory, port}
        """
        if service not in SERVICE_WHITELIST:
            return {"active": "unknown", "error": "未知服务"}
        info = SERVICE_WHITELIST[service]
        result = {
            "name": service,
            "label": info["label"],
            "port": info["port"],
            "active": "inactive",
            "enabled": False,
            "pid": None,
            "start_time": None,
            "memory_mb": None,
        }

        if self.dry_run:
            result["active"] = "active" if self._dry_pid_exists(service) else "inactive"
            result["enabled"] = self._dry_enabled(service)
            pid = self._dry_read_pid(service)
            if pid:
                result["pid"] = pid
            return result

        # systemd
        unit = self._systemd_unit(service)
        if unit:
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", unit],
                    capture_output=True, text=True, timeout=5
                )
                result["active"] = r.stdout.strip()
                r2 = subprocess.run(
                    ["systemctl", "is-enabled", unit],
                    capture_output=True, text=True, timeout=5
                )
                result["enabled"] = r2.stdout.strip() == "enabled"
                # PID
                r3 = subprocess.run(
                    ["systemctl", "show", "-p", "MainPID", unit],
                    capture_output=True, text=True, timeout=5
                )
                pid = r3.stdout.strip().replace("MainPID=", "")
                if pid and pid != "0":
                    result["pid"] = int(pid)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        # Fallback: PID file
        if not result.get("pid"):
            pid_file = self._pid_file(service)
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    if self._pid_alive(pid):
                        result["pid"] = pid
                        result["active"] = "active"
                    else:
                        pid_file.unlink(missing_ok=True)
                except (ValueError, OSError):
                    pass

        return result

    def all_status(self) -> list[dict]:
        return [self.status(s) for s in SERVICE_WHITELIST]

    # ── 操作 ───────────────────────────────────────────────────────

    def start(self, service: str) -> tuple[bool, str]:
        if service not in SERVICE_WHITELIST:
            return False, f"未知服务: {service}"
        if self.dry_run:
            self._dry_write_pid(service, 12345)
            return True, f"[DRY_RUN] 已启动 {service}"

        info = SERVICE_WHITELIST[service]
        workdir = self.app_base / info["workdir"] if info["workdir"] else None

        # systemd
        unit = self._systemd_unit(service)
        if unit:
            try:
                subprocess.run(["systemctl", "start", unit],
                               capture_output=True, timeout=10, check=True)
                return True, f"{info['label']} 已通过 systemd 启动"
            except subprocess.CalledProcessError as e:
                return False, f"systemctl start 失败: {e.stderr.decode()[:200]}"

        # PID file fallback
        try:
            proc = subprocess.Popen(
                info["cmd"],
                cwd=str(workdir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid if hasattr(os, 'setsid') else None,
            )
            self._pid_file(service).write_text(str(proc.pid))
            time.sleep(1)
            if self._pid_alive(proc.pid):
                return True, f"{info['label']} 已启动 (PID {proc.pid})"
            else:
                return False, f"{info['label']} 启动后立即退出"
        except Exception as e:
            return False, f"启动失败: {str(e)}"

    def stop(self, service: str) -> tuple[bool, str]:
        if service not in SERVICE_WHITELIST:
            return False, f"未知服务: {service}"
        if self.dry_run:
            self._dry_remove_pid(service)
            return True, f"[DRY_RUN] 已停止 {service}"

        info = SERVICE_WHITELIST[service]
        unit = self._systemd_unit(service)
        if unit:
            try:
                subprocess.run(["systemctl", "stop", unit],
                               capture_output=True, timeout=15, check=True)
                return True, f"{info['label']} 已通过 systemd 停止"
            except subprocess.CalledProcessError as e:
                return False, f"systemctl stop 失败: {e.stderr.decode()[:200]}"

        # PID file fallback
        pid_file = self._pid_file(service)
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                pid_file.unlink(missing_ok=True)
                return True, f"{info['label']} 已停止 (PID {pid})"
            except ProcessLookupError:
                pid_file.unlink(missing_ok=True)
                return True, f"{info['label']} 进程已不存在"
            except Exception as e:
                return False, f"停止失败: {str(e)}"
        return False, f"{info['label']} 未在运行"

    def restart(self, service: str) -> tuple[bool, str]:
        ok, msg = self.stop(service)
        time.sleep(1)
        return self.start(service)

    # ── 内部辅助 ──────────────────────────────────────────────────

    def act(self, service: str, action: str) -> dict:
        if action == "start":
            ok, msg = self.start(service)
        elif action == "stop":
            ok, msg = self.stop(service)
        elif action == "restart":
            ok, msg = self.restart(service)
        elif action == "enable":
            ok, msg = self.enable(service)
        elif action == "disable":
            ok, msg = self.disable(service)
        else:
            return {"success": False, "error": f"Unknown action: {action}"}
        return {"success": ok, "message": msg}

    def enable(self, service: str) -> tuple[bool, str]:
        if self.dry_run:
            self._dry_pid_dir().joinpath(f"{service}.enabled").touch()
            return True, f"[DRY_RUN] 已启用 {service}"
        unit = self._systemd_unit(service)
        if unit:
            try:
                subprocess.run(["systemctl", "enable", unit], capture_output=True, timeout=10, check=True)
                return True, f"{service} 已启用"
            except subprocess.CalledProcessError as e:
                return False, e.stderr.decode()[:200]
        return False, "systemd 不可用"

    def disable(self, service: str) -> tuple[bool, str]:
        if self.dry_run:
            pf = self._dry_pid_dir().joinpath(f"{service}.enabled")
            if pf.exists(): pf.unlink()
            return True, f"[DRY_RUN] 已禁用 {service}"
        unit = self._systemd_unit(service)
        if unit:
            try:
                subprocess.run(["systemctl", "disable", unit], capture_output=True, timeout=10, check=True)
                return True, f"{service} 已禁用"
            except subprocess.CalledProcessError as e:
                return False, e.stderr.decode()[:200]
        return False, "systemd 不可用"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    # ── DRY_RUN 模拟 ───────────────────────────────────────────────

    def _dry_pid_dir(self) -> Path:
        p = Path("/tmp/console-dryrun/pids")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _dry_pid_file(self, service: str) -> Path:
        return self._dry_pid_dir() / f"{service}.pid"

    def _dry_pid_exists(self, service: str) -> bool:
        return self._dry_pid_file(service).exists()

    def _dry_read_pid(self, service: str) -> Optional[int]:
        pf = self._dry_pid_file(service)
        if pf.exists():
            try:
                return int(pf.read_text().strip())
            except (ValueError, OSError):
                pass
        return None

    def _dry_write_pid(self, service: str, pid: int):
        self._dry_pid_file(service).write_text(str(pid))

    def _dry_remove_pid(self, service: str):
        self._dry_pid_file(service).unlink(missing_ok=True)

    def _dry_enabled(self, service: str) -> bool:
        ef = self._dry_pid_dir() / f"{service}.enabled"
        return ef.exists()
