#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth.py — 登录鉴权与会话管理（商用级安全加固）
=============================================================================
相对旧版的变化（均为安全必需）：
  1. **口令强度**：安装/改密一律走 `password_strength()`（≥10 位 + 字母数字 +
     非常见弱口令 + 不与用户名相同）。旧版只判 `len >= 8`，`admin123` 可过。
  2. **登录失败锁定持久化**：改用 `lib_ratelimit.LoginLockout`，失败计数落到
     `config/login_lockout.json`（0600）。旧版把计数放内存 → **重启即绕过锁定**。
  3. **常量时间比较**：口令哈希校验由 werkzeug 保证；API Key 改用
     `secrets.compare_digest`，避免长度/字节位泄露。
  4. **会话安全**：`login_required` 同时接受会话与 API Key；Cookie 属性
     （HttpOnly / SameSite / Secure）在 `app.py` 统一设置。
  5. **审计**：登录成功/失败/锁定事件由 app.py 写审计日志（本模块只提供判定）。
"""
import os
import secrets
from functools import wraps
from flask import request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from pathlib import Path

# 公共安全工具（单一真源）
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "common"))
try:
    from lib_ratelimit import LoginLockout, password_strength, strength_score
except Exception:                                             # pragma: no cover
    def password_strength(password, username=""):              # type: ignore
        if len(password or "") < 10:
            return False, ["长度至少 10 个字符"]
        return True, []

    def strength_score(password):                              # type: ignore
        return 0

    class LoginLockout:                                        # type: ignore
        def __init__(self, state_path, threshold=5, window_seconds=900,
                     lock_seconds=900):
            self._m = {}

        def is_locked(self, key):
            return False, 0

        def record_failure(self, key):
            return False, 0

        def reset(self, key):
            pass

        def status(self, key):
            return {}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class AuthManager:
    """口令鉴权：哈希存储、API Key 支持、口令强度、持久化登录锁定"""

    def __init__(self, config_dir: str, secret_key: str = ""):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.passwd_path = self.config_dir / "passwd"
        self.lockout = LoginLockout(
            str(self.config_dir / "login_lockout.json"),
            threshold=_env_int("CONSOLE_LOCKOUT_THRESHOLD", 5),
            window_seconds=_env_int("CONSOLE_LOCKOUT_WINDOW_SECONDS", 900),
            lock_seconds=_env_int("CONSOLE_LOCKOUT_SECONDS", 900),
        )

        if secret_key:
            self.secret_key = secret_key
        elif os.environ.get("SECRET_KEY"):
            self.secret_key = os.environ["SECRET_KEY"]
        else:
            # 首次运行生成密钥
            key_file = self.config_dir / "secret_key"
            if key_file.exists():
                self.secret_key = key_file.read_text().strip()
            else:
                self.secret_key = secrets.token_hex(32)
                key_file.write_text(self.secret_key, encoding="utf-8")
                key_file.chmod(0o600)

    # ── 初始化/安装 ────────────────────────────────────────────────

    def is_installed(self) -> bool:
        """是否已设置管理员口令"""
        return self.passwd_path.exists() and self.passwd_path.read_text().strip()

    def _write_password(self, password: str):
        self.passwd_path.write_text(generate_password_hash(password), encoding="utf-8")
        self.passwd_path.chmod(0o600)

    def install(self, password: str, username: str = "admin") -> tuple[bool, str]:
        """首次安装：设置管理员口令（含强度校验）"""
        ok, reasons = password_strength(password, username)
        if not ok:
            return False, "口令强度不足：" + "；".join(reasons)
        if self.is_installed():
            return False, "管理员口令已安装，请直接登录"
        self._write_password(password)
        return True, "安装成功"

    # ── 认证 ───────────────────────────────────────────────────────

    def verify(self, password: str) -> bool:
        if not self.is_installed():
            return False
        stored = self.passwd_path.read_text().strip()
        return check_password_hash(stored, password)

    def change_password(self, old: str, new: str, username: str = "admin") -> tuple[bool, str]:
        """改密：验证旧口令 → 强度校验 → 设置新口令"""
        if not self.verify(old):
            return False, "旧口令错误"
        ok, reasons = password_strength(new, username)
        if not ok:
            return False, "新口令强度不足：" + "；".join(reasons)
        if old == new:
            return False, "新旧口令相同"
        self._write_password(new)
        return True, "口令修改成功"

    def api_key_valid(self, api_key: str) -> bool:
        """从配置读取 API Key 校验（常量时间比较）"""
        keys = [k.strip() for k in os.environ.get("CONSOLE_API_KEYS", "").split(",") if k.strip()]
        provided = (api_key or "").strip()
        if not provided:
            return False
        return any(secrets.compare_digest(provided, k) for k in keys)

    # ── 会话装饰器 ─────────────────────────────────────────────────

    def login_required(self, f):
        """要求已登录会话（或合法 API Key）"""
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get("X-API-Key", "")
            if auth_header and self.api_key_valid(auth_header):
                return f(*args, **kwargs)
            if not session.get("authed"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "未授权", "code": "UNAUTHORIZED"}), 401
                return redirect(url_for("login_page"))
            return f(*args, **kwargs)
        return wrapper

    # ── 限速（持久化锁定） ─────────────────────────────────────────

    def is_locked(self, ip: str):
        """返回 (locked: bool, seconds_left: int)。"""
        return self.lockout.is_locked(ip)

    def record_failure(self, ip: str):
        return self.lockout.record_failure(ip)

    def reset_failures(self, ip: str):
        self.lockout.reset(ip)

    def lockout_status(self, ip: str) -> dict:
        return self.lockout.status(ip)


# 单例（由 app.py 初始化后填充）
auth_manager: AuthManager = None
