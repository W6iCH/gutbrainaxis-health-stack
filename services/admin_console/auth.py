"""
auth.py — 登录鉴权与会话管理
"""
import os
import secrets
from functools import wraps
from flask import request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from pathlib import Path
from datetime import datetime
import re


class AuthManager:
    """口令鉴权：哈希存储、API Key 支持、登录限速"""

    def __init__(self, config_dir: str, secret_key: str = ""):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.passwd_path = self.config_dir / "passwd"
        # 限速记录: {ip: [timestamps]}
        self._attempts = {}
        self._lockout = {}

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

    def install(self, password: str) -> tuple[bool, str]:
        """首次安装：设置管理员口令"""
        if len(password) < 8:
            return False, "口令至少需要 8 个字符"
        if self.is_installed():
            return False, "管理员口令已安装，请直接登录"
        self.passwd_path.write_text(generate_password_hash(password), encoding="utf-8")
        self.passwd_path.chmod(0o600)
        return True, "安装成功"

    # ── 认证 ───────────────────────────────────────────────────────

    def verify(self, password: str) -> bool:
        if not self.is_installed():
            return False
        stored = self.passwd_path.read_text().strip()
        return check_password_hash(stored, password)

    def change_password(self, old: str, new: str) -> tuple[bool, str]:
        """改密：验证旧口令 → 设置新口令"""
        if not self.verify(old):
            return False, "旧口令错误"
        if len(new) < 8:
            return False, "新口令至少需要 8 个字符"
        if old == new:
            return False, "新旧口令相同"
        self.passwd_path.write_text(generate_password_hash(new), encoding="utf-8")
        self.passwd_path.chmod(0o600)
        return True, "口令修改成功"

    def api_key_valid(self, api_key: str) -> bool:
        """从配置读取 API Key 校验"""
        keys = os.environ.get("CONSOLE_API_KEYS", "").split(",")
        return api_key.strip() in [k.strip() for k in keys if k.strip()]

    # ── 会话装饰器 ─────────────────────────────────────────────────

    def login_required(self, f):
        """要求已登录会话"""
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

    # ── 限速 ────────────────────────────────────────────────────────

    def is_locked(self, ip: str) -> bool:
        """5 次失败 → 锁 15 分钟"""
        now = datetime.now().timestamp()
        if ip in self._lockout:
            if now - self._lockout[ip] > 900:
                del self._lockout[ip]
            else:
                return True
        return False

    def record_failure(self, ip: str):
        self._attempts.setdefault(ip, []).append(datetime.now().timestamp())
        # 保留最近 15 分钟记录
        cutoff = datetime.now().timestamp() - 900
        self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]
        if len(self._attempts[ip]) >= 5:
            self._lockout[ip] = datetime.now().timestamp()
            self._attempts[ip] = []

    def reset_failures(self, ip: str):
        self._attempts.pop(ip, None)
        self._lockout.pop(ip, None)


# 单例（由 app.py 初始化后填充）
auth_manager: AuthManager = None
