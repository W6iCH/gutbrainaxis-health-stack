#!/usr/bin/env python3
"""
Diet Survey Sync Cron — 定时同步饮食问卷数据
=============================================
从 WJX 公共 API 拉取饮食问卷数据，检测新提交，
自动处理（解析 → 存储 → LLM 分析 → 存储建议 → 发送邮件）。

用法:
    python3 diet_sync_cron.py              # 执行一次同步
    python3 diet_sync_cron.py --force       # 强制重新处理
    python3 diet_sync_cron.py --dry-run     # 只检查不写入
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from diet_database import get_conn, init_db, save_submission, get_submission_by_answer_id

# ── API 配置 ─────────────────────────────────────────────────────────────

# WJX 公共访问令牌通过环境变量注入（WJX_DIET_TOKEN）
WJX_TOKEN = os.environ.get("WJX_DIET_TOKEN", "")
WJX_PAGE_SIZE = os.environ.get("WJX_DIET_PAGE_SIZE", "10")
API_URL = ("https://wj.sjtu.edu.cn/api/v1/public/result/"
           f"{WJX_TOKEN}/json?pageSize={WJX_PAGE_SIZE}&pageNum=1")
CST = timezone(timedelta(hours=8))

# ── 日志 ──────────────────────────────────────────────────────────────────

LOG_FILE = os.path.join(BASE_DIR, ".email_queue", "sync_cron.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


def log(msg):
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_api_data() -> dict:
    """从 WJX 公共 API 获取问卷数据。"""
    log(f"📥 正在拉取 API 数据...")
    req = urllib.request.Request(API_URL, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("success") and data.get("data", {}).get("rows"):
            rows = data["data"]["rows"]
            log(f"  ✅ API 返回 {len(rows)} 条记录")
            return data
        else:
            log(f"  ⚠ API 返回 success=false 或无数据")
            return None
    except urllib.error.URLError as e:
        log(f"  ❌ API 请求失败: {e}")
        return None
    except json.JSONDecodeError as e:
        log(f"  ❌ API 解析失败: {e}")
        return None
    except Exception as e:
        log(f"  ❌ 未知错误: {e}")
        return None


def get_existing_answer_ids() -> set:
    """查询数据库中已有的 answerId 集合。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT redirect_answer, submission_id FROM submissions "
            "WHERE redirect_answer IS NOT NULL OR submission_id IS NOT NULL"
        ).fetchall()
        ids = set()
        for r in rows:
            if r["redirect_answer"]:
                ids.add(str(r["redirect_answer"]))
            if r["submission_id"]:
                ids.add(str(r["submission_id"]))
        return ids
    finally:
        conn.close()


def extract_student_info(row: dict) -> dict:
    """从 API 返回的单条记录中提取学生信息。"""
    answers = row.get("answers", [])
    info = {
        "answer_id": str(row.get("id", "")),
        "student_id": None,
        "name": None,
        "email": None,
        "record_date": None,
        "diet_description": None,
        "organization": None,
    }

    for ans in answers:
        q = ans.get("question", {})
        title = q.get("title", "")
        a = ans.get("answer", "")
        if isinstance(a, dict):
            a = a.get("label", str(a))
        a = str(a)

        if "学工号" in title or "学号" in title:
            info["student_id"] = a
        elif "姓名" in title:
            info["name"] = a
        elif "邮箱" in title:
            info["email"] = a
        elif "记录日期" in title:
            info["record_date"] = a
        elif "饮食" in title or "描述" in title:
            info["diet_description"] = a
        elif "组织" in title or "学院" in title:
            info["organization"] = a

    return info


def process_new_submission(row: dict, existing_ids: set,
                            dry_run: bool = False) -> bool:
    """
    处理单条新提交。
    1. 存入数据库
    2. 调用 LLM 生成饮食建议
    3. 发送邮件
    返回 True 表示处理了（新数据）。
    """
    info = extract_student_info(row)
    answer_id = info["answer_id"]
    student_id = info["student_id"]

    # 检查是否已存在
    if answer_id and answer_id in existing_ids:
        log(f"  ⏭ answerId={answer_id} 已存在，跳过")
        return False

    log(f"  🆕 发现新提交: answerId={answer_id} student={student_id} "
        f"name={info['name']}")

    if dry_run:
        log(f"  (dry-run 模式，不实际处理)")
        return True

    # ── Step 1: 存入数据库 ────────────────────────────────────────────
    try:
        db_id = save_submission(
            student_id=student_id or f"unknown_{answer_id}",
            submission_id=answer_id,
            name=info["name"],
            email=info["email"],
            raw_data=row,
            redirect_params={"answer": answer_id},
        )
        log(f"  ✅ 已存入数据库 (id={db_id})")
    except Exception as e:
        log(f"  ❌ 数据库写入失败: {e}")
        return False

    # ── Step 2: 调用 LLM 生成饮食建议 ──────────────────────────────
    # 首次提交也会生成建议，但无落实情况报告
    advice_generated = False
    try:
        from diet_llm import generate_dietary_advice, update_submission_with_advice
        advice_result = generate_dietary_advice(
            student_id=student_id or f"unknown_{answer_id}",
            current_diet_desc=info["diet_description"] or "",
            current_record_date=info["record_date"] or datetime.now().strftime("%Y-%m-%d"),
            submission_id=answer_id,
        )

        if "error" in advice_result:
            log(f"  ⚠ LLM 建议生成失败: {advice_result['error']}")
        else:
            update_submission_with_advice(db_id, advice_result)
            log(f"  ✅ 饮食建议已生成 ({advice_result.get('tokens_used', '?')} tokens)")
            advice_generated = True
    except Exception as e:
        log(f"  ⚠ LLM 分析异常: {e}")

    # ── Step 3: 发送邮件 ──────────────────────────────────────────────
    if info["email"] and not dry_run:
        try:
            # 重新获取完整记录（包含 advice）
            from diet_database import get_conn as get_diet_conn
            conn = get_diet_conn()
            full_record = conn.execute(
                "SELECT * FROM submissions WHERE id = ?", (db_id,)
            ).fetchone()
            conn.close()

            if full_record:
                from diet_email_feedback import send_immediate
                record_dict = dict(full_record)
                item_id = send_immediate(
                    recipient=info["email"],
                    submission_id=answer_id,
                    record=record_dict,
                )
                if item_id:
                    log(f"  📧 邮件已发送到 {info['email']}")
                else:
                    log(f"  ⚠ 邮件发送失败 {info['email']}")
        except Exception as e:
            log(f"  ⚠ 邮件异常: {e}")

    return True


def sync(force: bool = False, dry_run: bool = False) -> dict:
    """执行同步。"""
    init_db()
    stats = {"api_rows": 0, "new": 0, "skipped": 0, "errors": 0}

    # Step 1: 获取 API 数据
    api_data = fetch_api_data()
    if not api_data:
        stats["error"] = "API fetch failed"
        return stats

    rows = api_data["data"]["rows"]
    stats["api_rows"] = len(rows)

    # Step 2: 获取已有 ID
    existing_ids = set() if force else get_existing_answer_ids()
    log(f"  数据库中已有 {len(existing_ids)} 个记录")

    # Step 3: 逐条处理
    for row in rows:
        try:
            processed = process_new_submission(row, existing_ids, dry_run)
            if processed:
                stats["new"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            log(f"  ❌ 处理异常: {e}")
            stats["errors"] += 1

    log(f"\n📊 同步完成: API={stats['api_rows']}条, "
        f"新增={stats['new']}, 跳过={stats['skipped']}, 错误={stats['errors']}")

    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Diet Survey Sync Cron")
    parser.add_argument("--force", action="store_true", help="强制重新处理")
    parser.add_argument("--dry-run", action="store_true", help="只检查不写入")
    args = parser.parse_args()

    log("=" * 60)
    log("  Diet Survey Sync Cron")
    log(f"  Force: {args.force}, Dry-run: {args.dry_run}")
    log("=" * 60)

    stats = sync(force=args.force, dry_run=args.dry_run)
    print(f"\n[MACHINE_PARSE] {json.dumps(stats)}")

    return 0 if stats.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
