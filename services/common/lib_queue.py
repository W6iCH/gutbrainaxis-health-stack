#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_queue.py — 持久化文件队列（**单一真源**）
=============================================================================
把「目录 + JSON 任务文件」这一模式收敛为一份实现，供以下调用方复用：

  * `services/diet_survey/diet_llm_queue.py`          —— LLM 分析队列
  * `services/sjtu_survey_pro/survey_webhook_listener.py`、`diet_survey/webhook_listener.py`
      —— webhook 失败重试队列
  * `services/sjtu_survey_pro/email_feedback.py`、`diet_survey/diet_email_feedback.py`
      —— 邮件队列状态统计（不接管其写入语义，仅统一「统计/原子移动」）

核心保证
--------
* **原子落盘**：`enqueue()` 先写 `.tmp` 再 `os.replace()`，读者永远看到完整 JSON；
* **原子领取**：`claim()` 用 `os.rename()` 把任务从 pending 移到 processing，
  同一任务不可能被两个 worker 领到；
* **幂等恢复**：`recover()` 把 processing 里的残留任务放回 pending（进程被强杀后）；
* **失败隔离**：`fail()` 达到上限转 failed，否则带退避重新入队。

零依赖（仅标准库）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

__version__ = "1.0"


class FileQueue:
    """文件持久化队列。目录布局：<root>/{pending,processing,done,failed}/"""

    def __init__(self, root: str, name: str = "queue"):
        self.root = root
        self.name = name
        self.pending = os.path.join(root, "pending")
        self.processing = os.path.join(root, "processing")
        self.done = os.path.join(root, "done")
        self.failed = os.path.join(root, "failed")
        self._lock = threading.Lock()
        for d in (self.pending, self.processing, self.done, self.failed):
            os.makedirs(d, exist_ok=True)

    # ── 写入 ────────────────────────────────────────────────────────
    def enqueue(self, task: dict, key: str = None) -> str:
        """入队。key 用于文件名（缺省用时间戳+uuid）；原子落盘。"""
        fname = f"{key or f'{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}'}.json"
        path = os.path.join(self.pending, fname)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    def upsert(self, task: dict, key: str) -> str:
        """幂等入队：同 key 已存在（pending/processing）则更新内容而非新增。"""
        fname = f"{key}.json"
        for d in (self.pending, self.processing):
            p = os.path.join(d, fname)
            if os.path.exists(p):
                tmp = p + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(task, f, ensure_ascii=False, indent=2)
                os.replace(tmp, p)
                return p
        return self.enqueue(task, key=key)

    # ── 读取 ────────────────────────────────────────────────────────
    def claim(self) -> tuple:
        """领取最旧的一个任务。返回 (processing_path, task) 或 (None, None)。"""
        with self._lock:
            names = sorted(f for f in os.listdir(self.pending) if f.endswith(".json"))
            if not names:
                return None, None
            fname = names[0]
            src = os.path.join(self.pending, fname)
            dst = os.path.join(self.processing, f"{uuid.uuid4().hex[:8]}_{fname}")
            try:
                os.rename(src, dst)                            # 原子领取
            except OSError:
                return None, None
            try:
                with open(dst, encoding="utf-8") as f:
                    return dst, json.load(f)
            except (OSError, json.JSONDecodeError):
                os.replace(dst, os.path.join(self.failed, fname))
                return None, None

    # ── 完成 / 失败 ─────────────────────────────────────────────────
    def complete(self, processing_path: str) -> str:
        fname = os.path.basename(processing_path).split("_", 1)[-1]
        dst = os.path.join(self.done, fname)
        os.replace(processing_path, dst)
        return dst

    def fail(self, processing_path: str, task: dict, error: str,
             max_attempts: int = 5, backoff_seconds: float = 30,
             sleep_between: bool = True) -> str:
        """失败处理：超上限转 failed，否则带退避回 pending。返回最终路径。"""
        task = dict(task or {})
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["last_error"] = str(error)[:500]
        fname = os.path.basename(processing_path).split("_", 1)[-1]
        if task["attempts"] >= max_attempts:
            dst = os.path.join(self.failed, fname)
            os.replace(processing_path, dst)
            return dst
        if sleep_between:
            time.sleep(min(backoff_seconds * task["attempts"], 300))
        tmp = processing_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        dst = os.path.join(self.pending, fname)
        os.replace(tmp, dst)
        if os.path.exists(processing_path):
            os.remove(processing_path)
        return dst

    # ── 维护 ────────────────────────────────────────────────────────
    def recover(self) -> int:
        """把 processing 残留放回 pending（幂等）。返回恢复数量。"""
        n = 0
        for f in list(os.listdir(self.processing)):
            if not f.endswith(".json"):
                continue
            fname = f.split("_", 1)[-1]
            try:
                os.replace(os.path.join(self.processing, f),
                           os.path.join(self.pending, fname))
                n += 1
            except OSError:
                pass
        return n

    def stats(self) -> dict:
        return {
            "pending": count_json(self.pending),
            "processing": count_json(self.processing),
            "done": count_json(self.done),
            "failed": count_json(self.failed),
        }

    def dead_letters(self, limit: int = 20) -> list:
        out = []
        try:
            for f in sorted(os.listdir(self.failed))[:limit]:
                if f.endswith(".json"):
                    out.append(f)
        except OSError:
            pass
        return out


def count_json(path: str) -> int:
    """统计目录下的 .json 文件数（目录不存在返回 0）。"""
    try:
        return len([f for f in os.listdir(path) if f.endswith(".json")])
    except OSError:
        return 0


def dir_stats(root: str) -> dict:
    """按 <root>/{pending,sent,failed,...} 约定返回各目录计数（用于邮件队列）。"""
    out = {}
    try:
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if os.path.isdir(p):
                out[name] = count_json(p)
    except OSError:
        pass
    return out
