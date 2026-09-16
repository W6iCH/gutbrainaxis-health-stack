#!/usr/bin/env python3
"""
Survey Data Sync Cron — 定时同步问卷数据
===========================================
每 60 分钟运行一次，从 WJX 公共 API 拉取数据，
检测是否有数据库中未存储的新提交，如有则处理。

本模块同时是**量表链路的唯一「收数→计分→入库→邮件」实现**：
  · `survey_sync_cron.py`（本文件，定时兜底拉取）调用它；
  · `survey_webhook_listener.py`（平台回调，低延迟）也调用它。
两条入口共用同一份 `process_row()` / `sync_rows()`，因此：
  - **幂等**：写入前查 `submission_id`（库内 UNIQUE），重复投递直接跳过；
  - **行为一致**：webhook 与定时拉取不会产生两套口径。

用法:
    python3 survey_sync_cron.py              # 执行一次同步
    python3 survey_sync_cron.py --force       # 强制重新处理所有数据
    python3 survey_sync_cron.py --dry-run     # 只检查不写入
    python3 survey_sync_cron.py --only-ids 123,456   # 只处理指定 answerId（手工补漏）
    python3 survey_sync_cron.py --source webhook     # 标注来源（日志/审计）
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
# API 基础地址可配（app.yaml → survey_platforms.scale.api_base）；换平台不改代码
API_BASE = os.environ.get("WJX_SURVEY_API_BASE",
                          "https://wj.sjtu.edu.cn/api/v1/public/result").rstrip("/")
API_URL = f"{API_BASE}/{WJX_TOKEN}/json?pageSize={WJX_PAGE_SIZE}&pageNum=1"
CST = timezone(timedelta(hours=8))

# HTTP 超时（可配；防止平台抖动把 worker 挂死）
FETCH_TIMEOUT_SECONDS = int(os.environ.get("SURVEY_FETCH_TIMEOUT_SECONDS", "30"))

# ── 日志 ──────────────────────────────────────────────────────────────────

LOG_FILE = os.path.join(BASE_DIR, ".email_queue", "sync_cron.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


# ── 静默时段（C：各 timer 可配静默时段）───────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(BASE_DIR), "common"))
try:
    from lib_schedule import load_config as _load_cfg, is_quiet_hours as _is_quiet
except Exception:                                           # pragma: no cover
    def _load_cfg(path=None):                               # type: ignore
        return {}

    def _is_quiet(cfg, now=None):                           # type: ignore
        return False


def in_quiet_hours() -> bool:
    """静默时段内是否跳过定时拉取（webhook 仍实时接收，不影响低延迟链路）。"""
    try:
        cfg = _load_cfg()
        if not cfg.get("schedule.quiet_hours_skip_sync", True):
            return False
        return bool(_is_quiet(cfg))
    except Exception:                                       # noqa: BLE001
        return False


def log(msg):
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # 日志写不进去不能影响主流程


def fetch_api_data(page_num: int = 1) -> dict | None:
    """从 WJX 公共 API 获取问卷数据。失败返回 None（由调用方决定兜底）。"""
    url = (f"{API_BASE}/{WJX_TOKEN}/json"
           f"?pageSize={WJX_PAGE_SIZE}&pageNum={page_num}")
    log(f"📥 正在拉取 API 数据 (page={page_num})...")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("success") and data.get("data", {}).get("rows"):
            rows = data["data"]["rows"]
            log(f"  ✅ API 返回 {len(rows)} 条记录")
            return data
        log("  ⚠ API 返回 success=false 或无数据")
        return None
    except urllib.error.URLError as e:
        log(f"  ❌ API 请求失败: {e}")
        return None
    except json.JSONDecodeError as e:
        log(f"  ❌ API 响应解析失败: {e}")
        return None
    except (TimeoutError, OSError) as e:
        log(f"  ❌ API 网络超时/错误: {e}")
        return None
    except Exception as e:                                   # noqa: BLE001
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


def extract_identity(row: dict) -> tuple:
    """从一行 API 记录抽取 (answer_id, student_id, name, email)。"""
    answers = row.get("answers", []) or []
    answer_id = str(row.get("id", row.get("answerId", "")) or "")
    student_id = name = email = None
    for ans in answers:
        q = ans.get("question", {}) or {}
        title = q.get("title", "") or ""
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
    return answer_id, student_id, name, email


def process_row(row: dict, existing_ids: set, dry_run: bool = False,
                source: str = "cron") -> bool:
    """
    处理单条新提交数据（webhook 与定时拉取共用）。
    返回 True 表示处理了（新数据），False 表示跳过（已存在/失败）。

    幂等性：
      * 入口处按 `answerId` 与库内既有 id 比对；
      * 写入层 `save_submission()` 对 `submission_id` 有 UNIQUE 约束，
        并发双入口同时到达时以数据库为准（第二个调用返回既有行 id）。
    """
    answer_id, student_id, name, email = extract_identity(row)

    # 已有 answer_id 或 student_id 在数据库中 → 跳过（幂等去重）
    if answer_id and answer_id in existing_ids:
        log(f"  ⏭ [{source}] answerId={answer_id} 已存在，跳过")
        return False

    if not answer_id:
        log("  ⚠ 记录无 answerId (id字段为空)，尝试用学号判断...")
        if student_id and student_id in existing_ids:
            log(f"  ⏭ [{source}] student={student_id} 已存在，跳过")
            return False

    # 新数据！需要处理
    log(f"  🆕 [{source}] 发现新提交: answerId={answer_id} student={student_id} name={name}")

    if dry_run:
        log("  (dry-run 模式，不实际写入)")
        return True

    # ── 分析数据 ────────────────────────────────────────────────────
    try:
        analysis = analyze_survey_responses({"data": {"rows": [row]}})
        if "error" in analysis:
            log(f"  ❌ 分析失败: {analysis['error']}")
            return False
        scales = list(analysis.get("scores", {}).keys())
        log(f"  ✅ 分析完成: {len(scales)} 个量表")
    except Exception as e:                                   # noqa: BLE001
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
    except Exception as e:                                   # noqa: BLE001
        log(f"  ❌ 数据库写入失败: {e}")
        return False

    # 后续新数据不再重复分析同一条
    existing_ids.add(answer_id) if answer_id else None

    # ── 邮件通知（防护机制：单条失败不影响其它条目）────────────────
    if email and not dry_run:
        try:
            from email_feedback import queue_email
            item_id = queue_email(recipient=email, analysis=analysis,
                                  submission_id=answer_id)
            if item_id:
                log(f"  📧 邮件已入队 → {email}")
            else:
                log(f"  ⚠ 邮件入队失败 {email}，稍后重试")
        except Exception as e:                               # noqa: BLE001
            log(f"  ⚠ 邮件异常: {e}（数据已入库，不影响其它条目）")

    return True


# 兼容旧名（历史调用点：仅本文件与旧文档）
def process_new_submission(row: dict, existing_ids: set, dry_run: bool = False) -> bool:
    return process_row(row, existing_ids, dry_run, source="cron")


def sync_rows(rows: list, existing_ids: set | None = None, dry_run: bool = False,
              source: str = "cron") -> dict:
    """对一批 API 记录执行「计分→入库→邮件」。逐条隔离失败。"""
    if existing_ids is None:
        existing_ids = get_existing_answer_ids()
    stats = {"rows": len(rows or []), "new": 0, "skipped": 0, "errors": 0}
    for row in rows or []:
        try:
            if process_row(row, existing_ids, dry_run, source=source):
                stats["new"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:                               # noqa: BLE001
            log(f"  ❌ [{source}] 处理异常: {e}")
            stats["errors"] += 1
    log(f"📊 [{source}] 处理完成: 记录={stats['rows']}, 新增={stats['new']}, "
        f"跳过={stats['skipped']}, 错误={stats['errors']}")
    return stats


def cleanup_old_duplicates():
    """清理没有 redirect_answer 的旧重复记录。"""
    conn = get_conn()
    try:
        deleted = conn.execute(
            "DELETE FROM submissions WHERE redirect_answer IS NULL"
        ).rowcount
        conn.commit()
        if deleted:
            log(f"  🧹 清理了 {deleted} 条无 answerId 的旧记录")
        return deleted
    finally:
        conn.close()


def sync(force: bool = False, dry_run: bool = False,
         only_answer_ids: set | None = None, source: str = "cron") -> dict:
    """执行同步。返回统计信息。

    only_answer_ids: 仅处理这些 answerId（webhook 定点补拉时使用）。
    """
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

    # Step 1b: 定点过滤（webhook 只关心回调里的那几条）
    if only_answer_ids:
        wanted = {str(x) for x in only_answer_ids if str(x).strip()}
        filtered = [r for r in rows
                    if str(r.get("id", r.get("answerId", ""))) in wanted]
        if filtered:
            log(f"  🎯 定点过滤：命中 {len(filtered)}/{len(rows)} 条")
            rows = filtered
        else:
            # 平台 API 首页未包含该条（分页/延迟）→ 退回全量比对，保证不漏
            log("  ⚠ 定点过滤未命中（平台首页未返回该条），退回全量比对")

    # Step 2: 获取已有 ID 集合
    existing_ids = set() if force else get_existing_answer_ids()
    log(f"  数据库中已有 {len(existing_ids)} 个记录")

    # Step 3: 逐条处理
    sub = sync_rows(rows, existing_ids, dry_run, source=source)
    stats["new"] = sub["new"]
    stats["skipped"] = sub["skipped"]
    stats["errors"] = sub["errors"]

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
    parser.add_argument("--only-ids", default="",
                        help="只处理指定 answerId（逗号分隔，用于手工补漏）")
    parser.add_argument("--source", default="cron",
                        help="来源标签（cron|webhook|manual），用于日志与审计")

    args = parser.parse_args()

    # 静默时段（可配）：定时拉取跳过；webhook 不受影响，数据不丢
    if in_quiet_hours() and not args.force:
        log("🌙 静默时段（schedule.quiet_hours_*），跳过本次定时拉取；"
            "Webhook 仍实时接收，下次窗口外自动恢复")
        print(f"[MACHINE_PARSE] {json.dumps({'skipped': 'quiet_hours'})}")
        return 0

    log("=" * 60)
    log("  Survey Data Sync Cron")
    log(f"  Force: {args.force}, Dry-run: {args.dry_run}, Source: {args.source}")
    log("=" * 60)

    only_ids = {x.strip() for x in args.only_ids.split(",") if x.strip()} or None
    stats = sync(force=args.force, dry_run=args.dry_run,
                 only_answer_ids=only_ids, source=args.source)

    # Output machine-readable JSON for cron monitoring
    print(f"\n[MACHINE_PARSE] {json.dumps(stats)}")

    return 0 if stats.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
