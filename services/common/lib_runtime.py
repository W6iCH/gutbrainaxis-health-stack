#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_runtime.py — 服务运行期公共设施（优雅停机 / 日志 / /healthz 响应）
=============================================================================
各服务的 HTTP 服务器与常驻 worker 统一 import 本模块，获得：

1. `install_signal_handlers(on_stop)` —— SIGTERM/SIGINT 优雅停机：
   置停机标志 → 调用回调（关连接池 / flush 队列 / checkpoint WAL）→ 退出。
   systemd `systemctl stop` 默认发 SIGTERM，旧实现直接默认终止，可能截断
   正在写入的 SQLite 事务或丢失内存中的邮件/LLM 队列状态。
2. `healthz_payload(service, extra)` —— 统一 `/healthz` JSON 结构：
   {status, service, version, uptime_s, pid, checks:{...}, time}
3. `JsonLog` —— 结构化日志（可选 JSON 行），带级别与轮转友好格式。

用法（HTTP 服务）::

    from lib_runtime import install_signal_handlers, healthz_payload, STOP

    install_signal_handlers()
    ...
    if path == "/healthz":
        body = json.dumps(healthz_payload("survey_feedback")).encode()
        ...
    while not STOP.is_set():
        ...

零依赖：只用标准库。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time

__version__ = "1.0"

STOP = threading.Event()
_STARTED_AT = time.time()
_HANDLERS: list = []


def install_signal_handlers(on_stop=None, logger=None):
    """
    注册 SIGTERM / SIGINT 处理：设为优雅停机。
    幂等：重复调用只注册一次。
    """
    if _HANDLERS:
        if on_stop is not None:
            _HANDLERS.append(on_stop)
        return

    def _handler(signum, frame):            # noqa: ARG001
        name = signal.Signals(signum).name
        STOP.set()
        if logger:
            try:
                logger.info(f"收到 {name}，开始优雅停机…")
            except Exception:               # noqa: BLE001
                pass
        for cb in list(_HANDLERS):
            try:
                cb()
            except Exception as e:          # noqa: BLE001
                if logger:
                    try:
                        logger.warning(f"停机回调异常: {e}")
                    except Exception:       # noqa: BLE001
                        pass

    if on_stop is not None:
        _HANDLERS.append(on_stop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # 非主线程 / 平台不支持时跳过（不阻断服务启动）
            pass


def uptime_seconds() -> float:
    return round(time.time() - _STARTED_AT, 1)


def healthz_payload(service: str, extra: dict | None = None,
                    checks: dict | None = None, ok: bool = True) -> dict:
    """统一 /healthz 响应体。"""
    payload = {
        "status": "ok" if ok and not STOP.is_set() else ("stopping" if STOP.is_set() else "degraded"),
        "service": service,
        "runtime_version": __version__,
        "pid": os.getpid(),
        "uptime_s": uptime_seconds(),
        "checks": checks or {},
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if extra:
        payload.update(extra)
    return payload


def healthz_bytes(service: str, extra: dict | None = None,
                  checks: dict | None = None, ok: bool = True) -> bytes:
    return json.dumps(healthz_payload(service, extra, checks, ok),
                      ensure_ascii=False).encode("utf-8")


class JsonLog:
    """极简结构化日志：既写 stderr（journald 可读），也可加文件 handler。"""

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

    def __init__(self, name: str, level: str = "INFO", path: str = None):
        self.name = name
        self.threshold = self.LEVELS.get(str(level).upper(), 20)
        self.path = path
        self._fh = None

    def _emit(self, level: str, msg: str, **fields):
        if self.LEVELS.get(level, 20) < self.threshold:
            return
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "level": level,
               "logger": self.name, "msg": str(msg)}
        if fields:
            rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        print(line, file=sys.stderr, flush=True)
        if self.path:
            try:
                if self._fh is None:
                    os.makedirs(os.path.dirname(self.path), exist_ok=True)
                    self._fh = open(self.path, "a", encoding="utf-8")
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass

    def debug(self, msg, **kw):
        self._emit("DEBUG", msg, **kw)

    def info(self, msg, **kw):
        self._emit("INFO", msg, **kw)

    def warning(self, msg, **kw):
        self._emit("WARNING", msg, **kw)

    def error(self, msg, **kw):
        self._emit("ERROR", msg, **kw)


def graceful_sleep(seconds: float, interval: float = 0.5) -> bool:
    """可被信号打断的 sleep；返回 True 表示被要求停机。"""
    end = time.time() + seconds
    while time.time() < end:
        if STOP.wait(min(interval, max(0.0, end - time.time()))):
            return True
    return STOP.is_set()


if __name__ == "__main__":
    print(json.dumps(healthz_payload("lib_runtime-self-test"), ensure_ascii=False, indent=2))
