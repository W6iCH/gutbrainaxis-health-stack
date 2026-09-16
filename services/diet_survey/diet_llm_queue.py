#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Task Queue — 持久化任务队列 + 分析调度（realtime / offpeak / hybrid）
=============================================================================
功能
----
1. **持久化队列**：文件落盘（进程重启不丢任务），原子 rename 领取，避免重复处理。
2. **顺序/限流处理**：遵守 API 速率限制，任务之间按 `analysis.throttle_seconds` 间隔。
3. **主/备 API 切换**：`diet_llm.generate_dietary_advice()` 内置主备切换。
4. **分析调度模式（新增 B）**：
   - `realtime`：入队即消费（旧行为，默认）。
   - `offpeak` ：**仅在配置窗口内消费**（如 23:00–06:00 的 API 低谷）；
                窗口外只入队、不消费 → 队列积压但**不丢**。
   - `hybrid` ：窗口内全速；窗口外按 `analysis.hybrid_daytime_max` 少量消费，
                避免长队列饿死（白天高峰期只做极少量的“保命”处理）。

幂等与安全（B 的关键要求）
--------------------------
* **领取原子性**：`os.rename(pending→processing)`，同一任务不可能被两个 worker 领到；
* **挂起/恢复幂等**：窗口判定是**纯函数**（`lib_schedule.window_state`），
  每次循环重算；跨天窗口（23:00→06:00）由 `in_time_window` 统一处理；
* **断点续跑**：窗口关闭时**处理完当前任务再停**，正在被处理的任务留在
  `processing/`，下次启动由 `recover_processing()` 放回 pending（attempts 不变）；
* **状态可观测**：`.llm_queue/state.json`（原子写）记录 mode / waiting / 三个计数 /
  当日已消费数 / 上次处理时间，看板与控制台直接读取。

用法::

    python3 diet_llm_queue.py                # 常驻 worker（按配置的模式调度）
    python3 diet_llm_queue.py --status       # 打印队列 + 窗口状态（JSON）
    python3 diet_llm_queue.py --once         # 只处理一批（调试/定时补跑）
    python3 diet_llm_queue.py --show-window  # 只打印窗口状态
"""

from __future__ import annotations

import json
import os
import sys
import time
import logging
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_DIR = os.path.join(BASE_DIR, ".llm_queue")
PENDING_DIR = os.path.join(QUEUE_DIR, "pending")
DONE_DIR = os.path.join(QUEUE_DIR, "done")
FAILED_DIR = os.path.join(QUEUE_DIR, "failed")
PROCESSING_DIR = os.path.join(QUEUE_DIR, "processing")
STATE_FILE = os.path.join(QUEUE_DIR, "state.json")
QUEUE_LOG = os.path.join(BASE_DIR, "logs", "llm_queue.log")

# 队列目录与日志目录都必须存在：
# 旧实现只建了队列目录，`logs/` 缺失时 logging.FileHandler 会抛
# FileNotFoundError → 整个 worker 起不来（systemd Restart 循环）。
for d in [QUEUE_DIR, PENDING_DIR, DONE_DIR, FAILED_DIR, PROCESSING_DIR,
          os.path.dirname(QUEUE_LOG)]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(QUEUE_LOG, encoding="utf-8"), logging.StreamHandler()])
qlog = logging.getLogger("llm_queue")

# ── 公共库（时间窗口：单一真源）────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(BASE_DIR), "common"))
try:
    from lib_runtime import install_signal_handlers, STOP, graceful_sleep
except Exception:                                            # pragma: no cover
    class _Stop:                                             # type: ignore
        def is_set(self):
            return False
        def set(self):
            pass
        def wait(self, t=None):
            time.sleep(t or 0)
            return False
    STOP = _Stop()                                           # type: ignore

    def install_signal_handlers(*a, **kw):                   # type: ignore
        pass

    def graceful_sleep(seconds, interval=0.5):               # type: ignore
        time.sleep(seconds)
        return False

try:
    from lib_schedule import load_config, window_state, is_quiet_hours
except Exception:                                            # pragma: no cover
    def load_config(path=None):                              # type: ignore
        return {}

    def window_state(cfg, now=None):                         # type: ignore
        return {"mode": "realtime", "in_window": True, "waiting": False,
                "window_start": None, "window_end": None, "timezone": "Asia/Shanghai",
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "workday_ok": True,
                "next_window_hint": ""}

    def is_quiet_hours(cfg, now=None):                       # type: ignore
        return False

_CONFIG_CACHE = {"cfg": {}, "at": 0.0}
_CONFIG_TTL = 30.0


def get_config(force: bool = False) -> dict:
    """读取统一配置（带 TTL 缓存）——**改 app.yaml 即改行为**（最多滞后 30s）。"""
    now = time.time()
    if force or now - _CONFIG_CACHE["at"] > _CONFIG_TTL:
        try:
            _CONFIG_CACHE["cfg"] = load_config() or {}
        except Exception as e:                               # noqa: BLE001
            qlog.warning(f"加载配置失败，沿用上次配置: {e}")
        _CONFIG_CACHE["at"] = now
    return _CONFIG_CACHE["cfg"]


def _num(key: str, default, cast=int):
    v = get_config().get(key, default)
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


# ── 任务入队 ────────────────────────────────────────────────────────────

def enqueue_task(db_id: int, student_id: str, diet_desc: str,
                 record_date: str, submission_id: str, email: str):
    """将 LLM 分析任务加入持久化队列。幂等：同一 db_id 已存在 pending 则复用。"""
    task = {
        "db_id": db_id,
        "student_id": student_id,
        "diet_desc": diet_desc,
        "record_date": record_date or "",
        "submission_id": submission_id or "",
        "email": email or "",
        "created_at": time.time(),
        "attempts": 0,
    }
    fname = f"{int(time.time()*1000)}_{db_id}.json"
    path = os.path.join(PENDING_DIR, fname)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)                    # 原子落盘：读者永远看到完整 JSON
    qlog.info(f"Enqueued task db_id={db_id} student={student_id} "
              f"(pending={count_pending()})")
    return path


def count_pending() -> int:
    return len([f for f in os.listdir(PENDING_DIR) if f.endswith(".json")])


# ── 状态文件（原子写，供看板/控制台读取）────────────────────────────────

def write_state(**kw):
    try:
        st = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
        st.update(kw)
        st["pending"] = count_pending()
        st["done"] = len([f for f in os.listdir(DONE_DIR) if f.endswith(".json")])
        st["failed"] = len([f for f in os.listdir(FAILED_DIR) if f.endswith(".json")])
        st["processing"] = len([f for f in os.listdir(PROCESSING_DIR) if f.endswith(".json")])
        st["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        qlog.warning(f"写状态文件失败: {e}")


def read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def recover_processing():
    """启动时把上次未完成（进程被强杀）的任务放回 pending —— 幂等恢复。"""
    n = 0
    for f in list(os.listdir(PROCESSING_DIR)):
        if f.endswith(".json"):
            try:
                os.replace(os.path.join(PROCESSING_DIR, f),
                           os.path.join(PENDING_DIR, f))
                n += 1
            except OSError:
                pass
    if n:
        qlog.info(f"恢复 {n} 个未完成任务（processing → pending）")
    return n


# ── 任务处理 ────────────────────────────────────────────────────────────

def process_one_task() -> bool:
    """处理队列中最旧的一个任务。返回 True 表示处理了一个任务。"""
    pending = sorted([f for f in os.listdir(PENDING_DIR) if f.endswith(".json")])
    if not pending:
        return False

    fname = pending[0]
    path = os.path.join(PENDING_DIR, fname)

    # Atomic: rename to prevent double-processing
    import random, string
    tmp = os.path.join(PROCESSING_DIR,
                       f"{''.join(random.choices(string.ascii_lowercase, k=8))}_{fname}")
    try:
        os.rename(path, tmp)
    except OSError:
        return False

    try:
        with open(tmp, encoding="utf-8") as f:
            task = json.load(f)
    except Exception as e:                                   # noqa: BLE001
        qlog.error(f"Failed to read task {fname}: {e}")
        os.replace(tmp, os.path.join(FAILED_DIR, fname))
        return True

    qlog.info(f"Processing task db_id={task['db_id']} student={task['student_id']}")

    try:
        from diet_llm import generate_dietary_advice, update_submission_with_advice
        result = generate_dietary_advice(
            task["student_id"], task["diet_desc"],
            task["record_date"], task["submission_id"]
        )
        if "error" in result:
            raise Exception(result["error"])

        # Store advice
        update_submission_with_advice(task["db_id"], result)
        qlog.info(f"Advice stored db_id={task['db_id']} tokens={result.get('tokens_used','?')}")

        # Email notification (safeguard) — send result to user's email
        if task.get("email"):
            try:
                from diet_database import get_conn
                conn = get_conn()
                row = conn.execute("SELECT * FROM submissions WHERE id=?", (task["db_id"],)).fetchone()
                conn.close()
                if row:
                    from diet_email_feedback import send_immediate
                    send_immediate(task["email"], task["submission_id"], dict(row))
                    qlog.info(f"Email sent to {task['email']}")
            except Exception as e:                           # noqa: BLE001
                qlog.warning(f"Email failed for {task['email']}: {e} "
                             "(result stored in DB, check admin console)")

        # Move to done
        os.replace(tmp, os.path.join(DONE_DIR, fname))
        return True

    except Exception as e:                                   # noqa: BLE001
        qlog.error(f"Task failed db_id={task['db_id']}: {e}")
        task["attempts"] = task.get("attempts", 0) + 1
        task["last_error"] = str(e)
        max_attempts = _num("webhook.retry_max_attempts", 5)
        if task["attempts"] >= max_attempts:
            os.replace(tmp, os.path.join(FAILED_DIR, fname))
            qlog.warning(f"Task permanently failed after {task['attempts']} attempts")
        else:
            backoff = _num("llm.primary.retry_backoff_seconds", 30) * task["attempts"]
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False, indent=2)
            if not graceful_sleep(min(backoff, 300)):
                os.replace(tmp, os.path.join(PENDING_DIR, fname))
            else:
                os.replace(tmp, os.path.join(PENDING_DIR, fname))
        return True


# ── Worker loop（含窗口调度）─────────────────────────────────────────────

def _day_key(cfg) -> str:
    ws = window_state(cfg)
    return str(ws.get("now", ""))[:10]


def _consume_batch(limit: int, throttle: float) -> int:
    """消费至多 limit 个任务（limit<=0 表示不限）。返回处理数。"""
    n = 0
    while not STOP.is_set():
        if limit and n >= limit:
            break
        if count_pending() == 0:
            break
        if not process_one_task():
            break
        n += 1
        if throttle > 0 and n < (limit or 1 << 30):
            if graceful_sleep(throttle):
                break
    return n


def worker_loop():
    """常驻 worker：按 analysis.mode 决定何时消费。"""
    concurrency = max(1, _num("analysis.concurrency", 1))
    qlog.info("=== LLM Task Queue Worker Started ===")
    qlog.info(f"  Queue: {QUEUE_DIR}")
    recover_processing()
    qlog.info(f"  Pending: {count_pending()}  并发: {concurrency}")
    install_signal_handlers(lambda: qlog.info("收到停机信号，等待当前任务结束…"))

    if concurrency > 1:
        threads = []
        for i in range(concurrency):
            t = threading.Thread(target=_worker_loop_single, name=f"llm-w-{i}", daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return
    _worker_loop_single()


def _worker_loop_single():
    """单 worker 主循环：窗口外挂起、窗口内消费；状态原子落盘。"""
    cfg = get_config(force=True)
    state = window_state(cfg)
    qlog.info(f"  调度模式={state['mode']} 窗口={state['next_window_hint']} "
              f"时区={state.get('timezone')} 当前{'在窗口内' if state['in_window'] else '在窗口外'}")
    write_state(mode=state["mode"], waiting=state["waiting"],
                window_start=state["window_start"], window_end=state["window_end"],
                timezone=state["timezone"], day=_day_key(cfg), day_consumed=0,
                suspended=False, started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    st = read_state()
    day = st.get("day")
    day_consumed = int(st.get("day_consumed", 0))
    # 窗口配额按「窗口实例」计数：窗口（或自然日）变化即归零（幂等，可安全重启）
    win_key = st.get("window_key")
    prev_in_window = False

    while not STOP.is_set():
        try:
            cfg = get_config()
            ws = window_state(cfg)
            throttle = float(_num("analysis.throttle_seconds", 3))
            idle = float(_num("analysis.idle_sleep_seconds", 2))
            check = float(_num("analysis.window_check_seconds", 30))
            max_window = _num("analysis.max_per_window", 0)
            hybrid_day_max = _num("analysis.hybrid_daytime_max", 20)

            # 跨天重置（hybrid 的白天额度按自然日计）
            today = str(ws.get("now", ""))[:10]
            if today != day:
                day, day_consumed = today, 0

            # 新的窗口实例 → 重置本窗口配额
            cur_win_key = f"{today}|{ws['window_start']}|{ws['window_end']}"
            if ws["in_window"] and not prev_in_window or cur_win_key != win_key:
                if ws["in_window"]:
                    write_state(window_consumed=0, window_key=cur_win_key)
                    win_key = cur_win_key
            prev_in_window = bool(ws["in_window"])

            pending = count_pending()
            allow = False
            reason = ""

            if ws["mode"] == "realtime":
                allow = True
                reason = "realtime"
            elif ws["mode"] == "offpeak":
                allow = bool(ws["in_window"])
                reason = "窗口内" if allow else "窗口外挂起"
            elif ws["mode"] == "hybrid":
                if ws["in_window"]:
                    allow = True
                    reason = "窗口内全速"
                else:
                    allow = day_consumed < hybrid_day_max
                    reason = (f"窗口外限量（{day_consumed}/{hybrid_day_max}）"
                              if allow else "窗口外额度用尽，挂起")

            # 单窗口配额
            if allow and ws["in_window"] and max_window and \
                    int(read_state().get("window_consumed", 0)) >= max_window:
                allow = False
                reason = f"本窗口配额已满（{max_window}）"

            write_state(mode=ws["mode"], waiting=not allow, in_window=ws["in_window"],
                        window_start=ws["window_start"], window_end=ws["window_end"],
                        timezone=ws["timezone"], day=day, day_consumed=day_consumed,
                        suspended=not allow, reason=reason,
                        last_window_check=ws["now"],
                        last_processed_at=read_state().get("last_processed_at"))

            if not allow:
                # 挂起：不消费，等窗口/额度；静默时段也在此等待（幂等，无副作用）
                if graceful_sleep(min(check, idle * 5 if pending == 0 else check)):
                    break
                continue

            if pending == 0:
                if graceful_sleep(idle):
                    break
                continue

            done = _consume_batch(0 if (max_window == 0) else
                                  max(0, max_window - int(read_state().get("window_consumed", 0))),
                                  throttle)
            if done:
                day_consumed += done if not ws["in_window"] else 0
                st = read_state()
                win_used = int(st.get("window_consumed", 0)) + done if ws["in_window"] else 0
                write_state(day=day, day_consumed=day_consumed,
                            window_consumed=win_used,
                            last_processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            last_batch_size=done)
                qlog.info(f"本轮处理 {done} 个任务（已消费 当日={day_consumed} "
                          f"窗口={win_used}）")
            else:
                if graceful_sleep(idle):
                    break
        except Exception as e:                               # noqa: BLE001
            qlog.error(f"Worker loop error: {e}")
            if graceful_sleep(10):
                break

    write_state(suspended=True, reason="worker stopped")
    qlog.info("Worker stopped")


# ── Status ──────────────────────────────────────────────────────────────

def queue_status() -> dict:
    pending = len([f for f in os.listdir(PENDING_DIR) if f.endswith(".json")])
    done = len([f for f in os.listdir(DONE_DIR) if f.endswith(".json")])
    failed = len([f for f in os.listdir(FAILED_DIR) if f.endswith(".json")])
    processing = len([f for f in os.listdir(PROCESSING_DIR) if f.endswith(".json")])
    return {"pending": pending, "done": done, "failed": failed, "processing": processing}


def status_extended() -> dict:
    """队列 + 窗口 + 调度模式的完整状态（看板/控制台用）。"""
    out = dict(queue_status())
    st = read_state()
    cfg = get_config()
    ws = window_state(cfg)
    out.update({
        "mode": ws["mode"],
        "in_window": ws["in_window"],
        "waiting_window": ws["waiting"] or bool(st.get("suspended")),
        "window_start": ws["window_start"],
        "window_end": ws["window_end"],
        "timezone": ws.get("timezone"),
        "now": ws["now"],
        "workdays": ws.get("workdays"),
        "workday_ok": ws.get("workday_ok"),
        "throttle_seconds": _num("analysis.throttle_seconds", 3),
        "concurrency": _num("analysis.concurrency", 1),
        "max_per_window": _num("analysis.max_per_window", 0),
        "hybrid_daytime_max": _num("analysis.hybrid_daytime_max", 20),
        "day_consumed": int(st.get("day_consumed", 0)),
        "window_consumed": int(st.get("window_consumed", 0)),
        "last_processed_at": st.get("last_processed_at"),
        "last_batch_size": st.get("last_batch_size"),
        "state_updated_at": st.get("updated_at"),
        "reason": st.get("reason", ""),
    })
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="LLM 任务队列 worker / 状态查看")
    ap.add_argument("--status", action="store_true", help="打印队列+窗口状态（JSON）")
    ap.add_argument("--show-window", action="store_true", help="只打印窗口状态（JSON）")
    ap.add_argument("--once", action="store_true", help="只处理一批（最多 --limit 个）")
    ap.add_argument("--limit", type=int, default=0, help="--once 时的最大处理数（0=不限）")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(status_extended(), ensure_ascii=False, indent=2))
    elif args.show_window:
        print(json.dumps(window_state(get_config(force=True)), ensure_ascii=False, indent=2))
    elif args.once:
        recover_processing()
        n = _consume_batch(args.limit, float(_num("analysis.throttle_seconds", 3)))
        print(json.dumps({"processed": n, **queue_status()}, ensure_ascii=False))
    else:
        worker_loop()
