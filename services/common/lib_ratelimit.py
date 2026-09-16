#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_ratelimit.py — 限流 / 登录失败锁定 / 口令强度（**单一真源**）
=============================================================================
被下列调用方共用：

  * `services/admin_console/auth.py`          —— 登录失败锁定、口令强度校验
  * `services/sjtu_survey_pro/survey_webhook_listener.py`、`diet_survey/webhook_listener.py`
      —— 单 IP 请求限流（防平台重投风暴 / 恶意探测）
  * `services/data_dashboard/app.py`          —— 导出接口限流（可选）

设计
----
* **滑动窗口限流**：`SlidingWindowLimiter(limit, window_seconds)`；
* **登录锁定持久化**：`LoginLockout` 把失败计数落到 JSON 文件（0600），
  **进程重启后锁定依然有效**（否则攻击者可用重启绕过）；
* **口令强度**：`password_strength()` 返回 (ok, reasons)，规则：
  长度 ≥ 10、含字母、含数字、非纯数字、非常见弱口令、非与用户名相同。

零依赖（仅标准库）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

__version__ = "1.0"

WEAK_PASSWORDS = {
    "password", "passw0rd", "12345678", "123456789", "1234567890", "qwerty",
    "admin123", "admin888", "letmein", "iloveyou", "abc123456", "a1234567",
    "changeme", "root1234", "administrator", "research", "gutbrain",
}


class SlidingWindowLimiter:
    """单键（如 IP）滑动窗口限流。线程安全。"""

    def __init__(self, limit: int = 60, window_seconds: int = 60):
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self._hits: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """记录一次访问；返回 False 表示已超限（调用方应返回 429）。"""
        now = time.time()
        with self._lock:
            bucket = [t for t in self._hits.get(key, []) if now - t < self.window]
            bucket.append(now)
            self._hits[key] = bucket
            if len(self._hits) > 10000:                      # 防内存膨胀
                for k in [k for k, v in self._hits.items()
                          if not v or now - v[-1] > self.window * 4]:
                    self._hits.pop(k, None)
            return len(bucket) <= self.limit

    def remaining(self, key: str) -> int:
        now = time.time()
        with self._lock:
            used = len([t for t in self._hits.get(key, []) if now - t < self.window])
        return max(0, self.limit - used)


class LoginLockout:
    """
    登录失败锁定（持久化）。
    默认：window 分钟内失败 >= threshold 次 → 锁定 lock_seconds。
    状态文件为 JSON（0600），可跨进程重启保留。
    """

    def __init__(self, state_path: str, threshold: int = 5,
                 window_seconds: int = 900, lock_seconds: int = 900):
        self.path = state_path
        self.threshold = max(1, int(threshold))
        self.window = max(1, int(window_seconds))
        self.lock_seconds = max(1, int(lock_seconds))
        self._lock = threading.Lock()

    # ── 持久化 ────────────────────────────────────────────────────
    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ── 查询/更新 ─────────────────────────────────────────────────
    def is_locked(self, key: str) -> tuple:
        """返回 (locked: bool, seconds_left: int)。"""
        now = time.time()
        with self._lock:
            data = self._read()
            rec = data.get(key) or {}
            until = float(rec.get("locked_until", 0) or 0)
            if until > now:
                return True, int(until - now)
            return False, 0

    def record_failure(self, key: str) -> tuple:
        """记录一次失败。返回 (locked: bool, seconds_left: int)。"""
        now = time.time()
        with self._lock:
            data = self._read()
            rec = data.get(key) or {"failures": []}
            fails = [t for t in rec.get("failures", []) if now - t < self.window]
            fails.append(now)
            rec["failures"] = fails
            rec["last_failure_at"] = now
            locked = len(fails) >= self.threshold
            if locked:
                rec["locked_until"] = now + self.lock_seconds
                rec["failures"] = []
            data[key] = rec
            # 清理过期条目
            for k in [k for k, v in data.items()
                      if float(v.get("locked_until", 0) or 0) < now - self.window]:
                data.pop(k, None)
            self._write(data)
            return (True, self.lock_seconds) if locked else (False, 0)

    def reset(self, key: str):
        with self._lock:
            data = self._read()
            if key in data:
                data.pop(key)
                self._write(data)

    def status(self, key: str) -> dict:
        with self._lock:
            rec = (self._read().get(key) or {})
        now = time.time()
        return {
            "failures_recent": len([t for t in rec.get("failures", [])
                                    if now - t < self.window]),
            "locked_until": rec.get("locked_until"),
            "threshold": self.threshold,
            "window_seconds": self.window,
            "lock_seconds": self.lock_seconds,
        }


# ── 口令强度 ──────────────────────────────────────────────────────────────

def password_strength(password: str, username: str = "") -> tuple:
    """
    返回 (ok: bool, reasons: list[str])。ok=False 时 reasons 说明不达标原因。
    规则（商用级最低要求）：
      * 长度 ≥ 10
      * 同时含字母与数字
      * 不是纯数字 / 纯字母
      * 不在常见弱口令表内（大小写不敏感）
      * 不与用户名相同（大小写不敏感）
    """
    reasons = []
    pw = password or ""
    if len(pw) < 10:
        reasons.append("长度至少 10 个字符")
    if not re.search(r"[A-Za-z]", pw):
        reasons.append("需包含字母")
    if not re.search(r"\d", pw):
        reasons.append("需包含数字")
    if pw and (pw.isdigit() or pw.isalpha()):
        reasons.append("不能是纯数字或纯字母")
    if pw.lower() in WEAK_PASSWORDS:
        reasons.append("属于常见弱口令")
    if username and pw.lower() == str(username).lower():
        reasons.append("不能与用户名相同")
    return (len(reasons) == 0), reasons


def strength_score(password: str) -> int:
    """0-4 的粗略强度分（用于前端提示）。"""
    pw = password or ""
    score = 0
    if len(pw) >= 8:
        score += 1
    if len(pw) >= 12:
        score += 1
    if re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw):
        score += 1
    if re.search(r"[^A-Za-z0-9]", pw):
        score += 1
    return score
