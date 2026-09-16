#!/usr/bin/env python3
"""
Exercise Survey Sync Cron — 定时同步运动记录数据
================================================
从 WJX 公共 API 拉取运动记录数据，检测新提交，
自动解析并存入 SQLite 数据库。

用法:
    python3 exercise_sync_cron.py              # 执行一次同步
    python3 exercise_sync_cron.py --force       # 强制重新处理
    python3 exercise_sync_cron.py --dry-run     # 只检查不写入
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from exercise_database import (
    get_conn, init_db, save_submission,
    get_submission_by_answer_id, get_all_answer_ids,
)

# ── API 配置 ─────────────────────────────────────────────────────────────
# ⚠️ 安全修正：旧版本在此**硬编码了真实的问卷公共查询 token**
#    （`.../result/<REDACTED_已轮换>/json`），随公开仓库发布
#    即构成凭据泄露。现改为从环境变量读取，仓库内只保留占位符。
#    配置位置：config/app.yaml → survey_platforms.exercise.token
#             （或 /etc/research-app/secrets.env 的 WJX_EXERCISE_TOKEN）
WJX_EXERCISE_TOKEN = os.environ.get("WJX_EXERCISE_TOKEN", "").strip()
API_BASE_URL = ("https://wj.sjtu.edu.cn/api/v1/public/result/"
                f"{WJX_EXERCISE_TOKEN}/json")
API_PAGE_SIZE = int(os.environ.get("WJX_EXERCISE_PAGE_SIZE", "50"))  # 大页提升效率
CST = timezone(timedelta(hours=8))

# ── 日志 ──────────────────────────────────────────────────────────────────

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "sync_cron.log")


def log(msg):
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_api_data() -> dict:
    """从 WJX 公共 API 获取所有运动记录问卷数据（全量分页）。"""
    log(f"📥 正在拉取运动记录 API 数据...")
    all_rows = []
    page_num = 1
    
    while True:
        url = f"{API_BASE_URL}?pageSize={API_PAGE_SIZE}&pageNum={page_num}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            log(f"  ❌ API 请求失败 (page {page_num}): {e}")
            break
        except json.JSONDecodeError as e:
            log(f"  ❌ API 解析失败 (page {page_num}): {e}")
            break
        except Exception as e:
            log(f"  ❌ 未知错误 (page {page_num}): {e}")
            break
        
        if not data.get("success"):
            log(f"  ⚠ API 返回 success=false (page {page_num})")
            break
        
        rows = data.get("data", {}).get("rows", [])
        if not rows:
            break
        
        all_rows.extend(rows)
        total = data.get("data", {}).get("total", 0)
        log(f"  📄 Page {page_num}: {len(rows)} 条 (累计 {len(all_rows)}/{total})")
        
        if len(all_rows) >= total or len(rows) < API_PAGE_SIZE:
            break
        page_num += 1
    
    if all_rows:
        log(f"  ✅ API 共返回 {len(all_rows)} 条记录")
        return {"success": True, "data": {"rows": all_rows}}
    else:
        log(f"  ⚠ API 无数据")
        return None


def extract_student_info(row: dict) -> dict:
    """从 API 返回的单条记录中提取学生信息。"""
    answers = row.get("answers", [])
    user = row.get("user", {})
    info = {
        "answer_id": str(row.get("id", "")),
        "student_id": None,
        "name": None,
        "email": None,
        "organization": user.get("organization", ""),
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

    # Fallback: use user info
    if not info["name"]:
        info["name"] = user.get("name", "")
    if not info["student_id"]:
        info["student_id"] = user.get("account", "")

    return info


def process_new_submission(row: dict, existing_ids: set,
                            dry_run: bool = False) -> bool:
    """处理单条新提交。返回 True 表示处理了（新数据）。"""
    info = extract_student_info(row)
    answer_id = info["answer_id"]
    student_id = info["student_id"]

    # 检查是否已存在
    if answer_id and answer_id in existing_ids:
        log(f"  ⏭ answerId={answer_id} 已存在，跳过")
        return False

    log(f"  🆕 发现新运动记录: answerId={answer_id} student={student_id} "
        f"name={info['name']}")

    if dry_run:
        log(f"  (dry-run 模式，不实际处理)")
        return True

    # 存入数据库
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
    existing_ids = set() if force else get_all_answer_ids()
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
    parser = argparse.ArgumentParser(description="Exercise Survey Sync Cron")
    parser.add_argument("--force", action="store_true", help="强制重新处理")
    parser.add_argument("--dry-run", action="store_true", help="只检查不写入")
    args = parser.parse_args()

    log("=" * 60)
    log("  Exercise Survey Sync Cron")
    log(f"  Force: {args.force}, Dry-run: {args.dry_run}")
    log("=" * 60)

    stats = sync(force=args.force, dry_run=args.dry_run)
    print(f"\n[MACHINE_PARSE] {json.dumps(stats)}")

    return 0 if stats.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
