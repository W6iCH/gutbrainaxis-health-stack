#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_errors.py — 统一错误模型与 HTTP 错误响应（**单一真源**）
=============================================================================
目标：把「异常 → 用户可见响应」的映射收敛到一处，避免各服务各自
`except Exception: return jsonify({'error': str(e)}), 500` 导致

  * **信息泄露**：把数据库路径/堆栈直接回给调用方；
  * **口径不一**：有的返回 500 有的 400，有的回 `message` 有的回 `error`；
  * **静默失败**：异常被吞掉且不留日志。

用法::

    from lib_errors import guard, error_response, AppError, NotFound

    @guard(service="survey_webhook", logger=log)
    def do_post(self):
        ...
        raise NotFound("submission not found")
"""

from __future__ import annotations

import json
import traceback

__version__ = "1.0"


class AppError(Exception):
    """业务错误：带 HTTP 状态码与稳定错误码，可安全回给调用方。"""

    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, status: int = None, code: str = None,
                 detail=None):
        super().__init__(message)
        self.message = message
        self.status = status or self.status
        self.code = code or self.code
        self.detail = detail


class ValidationError(AppError):
    status = 422
    code = "VALIDATION_ERROR"


class NotFound(AppError):
    status = 404
    code = "NOT_FOUND"


class Unauthorized(AppError):
    status = 401
    code = "UNAUTHORIZED"


class RateLimited(AppError):
    status = 429
    code = "RATE_LIMITED"


class UpstreamError(AppError):
    status = 502
    code = "UPSTREAM_ERROR"


def error_payload(exc: Exception, request_id: str = None) -> dict:
    """把异常转成**对外安全**的 JSON 体（不透出堆栈与内部路径）。"""
    if isinstance(exc, AppError):
        body = {"status": "error", "code": exc.code, "message": exc.message}
        if exc.detail is not None:
            body["detail"] = exc.detail
    else:
        body = {"status": "error", "code": "INTERNAL_ERROR",
                "message": "internal server error"}
    if request_id:
        body["request_id"] = request_id
    return body


def error_response(exc: Exception, request_id: str = None) -> tuple:
    """返回 (status_code, json_bytes)。供 stdlib HTTP 处理器使用。"""
    status = exc.status if isinstance(exc, AppError) else 500
    return status, json.dumps(error_payload(exc, request_id),
                              ensure_ascii=False).encode("utf-8")


def log_exception(logger, exc: Exception, ctx: dict = None, level: str = "error"):
    """统一异常日志：结构化 + 堆栈（只进日志，不进响应）。"""
    rec = {"exc_type": type(exc).__name__, "exc": str(exc)[:500],
           "traceback": traceback.format_exc()[-2000:]}
    if ctx:
        rec.update({k: str(v)[:200] for k, v in ctx.items()})
    if logger is None:
        print(json.dumps(rec, ensure_ascii=False))
        return
    try:
        getattr(logger, level)(json.dumps(rec, ensure_ascii=False))
    except Exception:                                         # noqa: BLE001
        pass


def guard(service: str = "app", logger=None, rethrow: bool = False,
          default_status: int = 500):
    """
    装饰器：统一捕获异常 → 记日志 → 返回安全错误响应。
    被装饰函数的返回值约定为 `(status, body_bytes)`；异常时返回同形状。
    """
    def deco(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except AppError as e:
                log_exception(logger, e, {"service": service}, level="warning")
                if rethrow:
                    raise
                return error_response(e)
            except Exception as e:                            # noqa: BLE001
                log_exception(logger, e, {"service": service})
                if rethrow:
                    raise
                return default_status, json.dumps(
                    error_payload(e), ensure_ascii=False).encode("utf-8")
        wrapper.__name__ = getattr(fn, "__name__", "wrapper")
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco


def result_ok(**kw) -> tuple:
    """统一成功响应：返回 (200, json_bytes)。"""
    body = {"status": "ok"}
    body.update(kw)
    return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
