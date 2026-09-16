#!/usr/bin/env python3
"""
Survey Data Sync Cron — 定时同步问卷数据
===========================================
每 60 分钟运行一次，从 WJX 公共 API 拉取数据，
检测是否有数据库中未存储的新提交，如有则处理。

用法:
    python3 survey_sync_cron.py              # 执行一次同步
    python3 survey_sync_cron.py --force       # 强制重新处理所有数据
    python3 survey_sync_cron.py --dry-run     # 只检查不写入
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

from survey_analysis import analyze_survey_responses
from survey_database import get_conn, init_db, save_submission, get_all_students

# ── API 配置 ──────────────────────────────────────────────────────────────

# WJX 公共访问令牌通过环境变量注入（WJX_SURVEY_TOKEN）
WJX_TOKEN = os.environ.get("WJX_SURVEY_TOKEN", "")
WJX_PAGE_SIZE = os.environ.get("WJX_SURVEY_PAGE_SIZE", "50")
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
        log(f"  ❌ API 响应解析失败: {e}")
        return None
    except Exception as e:
        log(f"  ❌ 未知错误: {e}")
        return None


def get_existing_answer_ids() -> set:
    """查询数据库中已有的 answerId/redirect_answer 集合。"""
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


def process_new_submission(row: dict, existing_ids: set, dry_run: bool = False) -> bool:
    """
    处理单条新提交数据。
    返回 True 表示处理了（新数据），False 表示跳过。
    """
    # 提取 answerId (API 中 id 字段就是 answerId)
    answers = row.get("answers", [])
    answer_id = str(row.get("id", row.get("answerId", "")))

    # 尝试从 answers 中提取学号和邮箱
    student_id = None
    name = None
    email = None
    for ans in answers:
        q = ans.get("question", {})
        title = q.get("title", "")
        a = ans.get("answer", "")
        if isinstance(a, dict):
            a = a.get("label", "")
        a = str(a)

        if "学工号" in title or "student" in title.lower():
            student_id = a
        elif "姓名" in title or "name" in title.lower():
            name = a
        elif "邮箱" in title or "email" in title.lower():
            email = a

    # 如果已有 answer_id 或 student_id 在数据库中，跳过
    if answer_id and answer_id in existing_ids:
        log(f"  ⏭ answerId={answer_id} 已存在，跳过")
        return False

    if not answer_id:
        # 如果 answerId 为空（应不会），尝试用学号判断
        if answer_id == "" or answer_id == "None":
            log(f"  ⚠ 记录无 answerId (id字段为空)，尝试用学号判断...")
            if student_id and student_id in existing_ids:
                return False

    # 新数据！需要处理
    log(f"  🆕 发现新提交: answerId={answer_id} student={student_id} name={name}")

    if dry_run:
        log(f"  (dry-run 模式，不实际写入)")
        return True

    # ── 分析数据 ────────────────────────────────────────────────────
    try:
        analysis = analyze_survey_responses({"data": {"rows": [row]}})
        if "error" in analysis:
            log(f"  ❌ 分析失败: {analysis['error']}")
            return False
        scales = list(analysis.get("scores", {}).keys())
        log(f"  ✅ 分析完成: {len(scales)} 个量表")
    except Exception as e:
        log(f"  ❌ 分析异常: {e}")
        return False

    # ── 存入数据库 ──────────────────────────────────────────────────
    try:
        db_id = save_submission(
            student_id=student_id or f"unknown_{answer_id}",
            submission_id=answer_id,
            name=name,
            email=email,
            raw_data=row,
            analysis=analysis,
            redirect_params={"answer": answer_id},
        )
        log(f"  ✅ 已存入数据库 (id={db_id})")
    except Exception as e:
        log(f"  ❌ 数据库写入失败: {e}")
        return False

    # ── 邮件通知（防护机制） ──────────────────────────────────────
    # 每条新数据分析完成后自动发送邮件，确保用户能收到结果
    if email and not dry_run:
        try:
            from email_feedback import queue_email
            item_id = queue_email(recipient=email, analysis=analysis, submission_id=answer_id)
            if item_id:
                log(f"  📧 邮件已发送到 {email}")
            else:
                log(f"  ⚠ 邮件发送失败 {email}，稍后重试")
        except Exception as e:
            log(f"  ⚠ 邮件异常: {e}")

    return True


def cleanup_old_duplicates():
    """清理没有 redirect_answer 的旧重复记录。"""
    from survey_database import get_conn
    conn = get_conn()
    try:
        # 删除所有没有 redirect_answer 的旧记录
        deleted = conn.execute(
            "DELETE FROM submissions WHERE redirect_answer IS NULL"
        ).rowcount
        conn.commit()
        if deleted:
            log(f"  🧹 清理了 {deleted} 条无 answerId 的旧记录")
        return deleted
    finally:
        conn.close()


def sync(force: bool = False, dry_run: bool = False) -> dict:
    """执行同步。返回统计信息。"""
    init_db()
    stats = {"api_rows": 0, "new": 0, "skipped": 0, "errors": 0}

    # Step 0: 清理旧重复记录
    cleanup_old_duplicates()

    # Step 1: 获取 API 数据
    api_data = fetch_api_data()
    if not api_data:
        stats["error"] = "API fetch failed"
        return stats

    rows = api_data["data"]["rows"]
    stats["api_rows"] = len(rows)

    # Step 2: 获取已有 ID 集合
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

    if stats["new"] > 0 and not dry_run:
        log("  ✅ 新数据已存入数据库，后续访问 /report 将展示最新数据")

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Survey Data Sync Cron")
    parser.add_argument("--force", action="store_true",
                        help="强制重新处理所有数据")
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查不写入")

    args = parser.parse_args()

    log("=" * 60)
    log("  Survey Data Sync Cron")
    log(f"  Force: {args.force}, Dry-run: {args.dry_run}")
    log("=" * 60)

    stats = sync(force=args.force, dry_run=args.dry_run)

    # Output machine-readable JSON for cron monitoring
    print(f"\n[MACHINE_PARSE] {json.dumps(stats)}")

    return 0 if stats.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
