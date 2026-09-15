#!/usr/bin/env python3
"""
LLM Task Queue — 持久化任务队列
===============================
确保高并发时 LLM 任务排队完成不遗漏。
- 文件持久化（进程重启不丢任务）
- 单线程顺序处理（遵守 API 速率限制）
- 主 API 失败自动切换到备用 API
"""

import json, os, time, logging, threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_DIR = os.path.join(BASE_DIR, ".llm_queue")
PENDING_DIR = os.path.join(QUEUE_DIR, "pending")
DONE_DIR = os.path.join(QUEUE_DIR, "done")
FAILED_DIR = os.path.join(QUEUE_DIR, "failed")
QUEUE_LOG = os.path.join(BASE_DIR, "logs", "llm_queue.log")

for d in [QUEUE_DIR, PENDING_DIR, DONE_DIR, FAILED_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(QUEUE_LOG, encoding="utf-8"), logging.StreamHandler()])
qlog = logging.getLogger("llm_queue")

# ── 任务入队 ────────────────────────────────────────────────────────────

def enqueue_task(db_id: int, student_id: str, diet_desc: str,
                 record_date: str, submission_id: str, email: str):
    """将 LLM 分析任务加入持久化队列。"""
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    qlog.info(f"Enqueued task db_id={db_id} student={student_id} (pending={len(os.listdir(PENDING_DIR))})")
    return path

# ── 任务处理 ────────────────────────────────────────────────────────────

def process_one_task():
    """处理队列中最旧的一个任务。返回 True 表示处理了一个任务。"""
    pending = sorted([f for f in os.listdir(PENDING_DIR) if f.endswith(".json")])
    if not pending:
        return False

    fname = pending[0]
    path = os.path.join(PENDING_DIR, fname)

    # Atomic: rename to prevent double-processing
    import random, string
    tmp = os.path.join(PENDING_DIR, f"processing_{''.join(random.choices(string.ascii_lowercase, k=8))}.json")
    try:
        os.rename(path, tmp)
    except:
        return False

    try:
        with open(tmp, encoding="utf-8") as f:
            task = json.load(f)
    except Exception as e:
        qlog.error(f"Failed to read task {fname}: {e}")
        os.rename(tmp, os.path.join(FAILED_DIR, fname))
        return True

    qlog.info(f"Processing task db_id={task['db_id']} student={task['student_id']}")

    import sys
    sys.path.insert(0, BASE_DIR)

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
            except Exception as e:
                qlog.warning(f"Email failed for {task['email']}: {e} (result stored in DB, check admin console)")

        # Move to done
        os.rename(tmp, os.path.join(DONE_DIR, fname))
        return True

    except Exception as e:
        qlog.error(f"Task failed db_id={task['db_id']}: {e}")
        task["attempts"] = task.get("attempts", 0) + 1
        task["last_error"] = str(e)
        if task["attempts"] >= 5:
            os.rename(tmp, os.path.join(FAILED_DIR, fname))
            qlog.warning(f"Task permanently failed after {task['attempts']} attempts")
        else:
            # Re-enqueue with delay
            time.sleep(30 * task["attempts"])
            with open(os.path.join(PENDING_DIR, fname), "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False, indent=2)
            os.remove(tmp)
        return True

# ── Worker loop ──────────────────────────────────────────────────────────

def worker_loop():
    """持续处理队列中的任务。"""
    qlog.info("=== LLM Task Queue Worker Started ===")
    qlog.info(f"  Queue: {QUEUE_DIR}")
    qlog.info(f"  Pending: {len(os.listdir(PENDING_DIR))}")

    while True:
        try:
            processed = process_one_task()
            if not processed:
                time.sleep(2)  # idle
            else:
                time.sleep(3)  # respect rate limits between tasks
        except KeyboardInterrupt:
            qlog.info("Worker stopped")
            break
        except Exception as e:
            qlog.error(f"Worker loop error: {e}")
            time.sleep(10)

# ── Status ──────────────────────────────────────────────────────────────

def queue_status():
    pending = len([f for f in os.listdir(PENDING_DIR) if f.endswith(".json")])
    done = len([f for f in os.listdir(DONE_DIR) if f.endswith(".json")])
    failed = len([f for f in os.listdir(FAILED_DIR) if f.endswith(".json")])
    return {"pending": pending, "done": done, "failed": failed}

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(queue_status()))
    else:
        worker_loop()
