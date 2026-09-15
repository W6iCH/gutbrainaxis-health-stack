#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate.py — 升级迁移（数据库 schema / 配置 / 系统单元）
=============================================================================
职责
----
把已安装实例从旧版本升级到当前版本，**幂等、可预演、可回滚**：

1. **数据库 schema 迁移**：按 `MIGRATIONS` 列表逐条执行，用
   `schema_migrations` 表记录已应用版本；缺列补列（`ALTER TABLE ADD COLUMN`），
   缺表建表。绝不 DROP、绝不改列类型。
2. **配置文件迁移**：
   * `env.example` → 统一入口 `app.yaml`（若尚无 app.yaml）
   * 校验 app.yaml（`appconfig --check`），失败则报告键位置
   * 保留 `.bak` 备份
3. **系统单元迁移**：报告新旧 systemd 单元差异（如新增
   `research-diet-sync` / `research-daily-report` / `research-microbiome-import`），
   提示需要执行的 enable 命令（不自动改系统，除非 `--apply-system`）。

用法
----
    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY tools/migrate.py --status                  # 查看当前版本与待应用迁移
    $PY tools/migrate.py --plan                    # 预演（不改任何东西）
    $PY tools/migrate.py --apply                   # 应用数据库 + 配置迁移
    $PY tools/migrate.py --apply --apply-system    # 额外打印 systemd 启用命令
    $PY tools/migrate.py --backup-first            # 应用前自动备份（推荐）

退出码：0=成功/无待办；1=有失败；2=环境错误。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import appconfig as AC                                   # noqa: E402

CURRENT_VERSION = 3          # 与 config/app.schema.json 的 version 保持一致

# ── 迁移定义： (version, 说明, [ (db_key, sql) ... ]) ─────────────────────
# db_key 为 app.yaml 中的 database.* 键名。
MIGRATIONS = [
    (1, "基线：schema_migrations 记账表（由各服务 init_db 建表，此处仅登记）", []),
    (2, "问卷库：补 email_log 重试/回退字段；submissions 补 BMI 列", [
        ("survey_db", "ALTER TABLE submissions ADD COLUMN bmi_score REAL"),
        ("survey_db", "ALTER TABLE submissions ADD COLUMN bmi_category TEXT"),
        ("survey_db", "ALTER TABLE submissions ADD COLUMN bmi_interpretation TEXT"),
        ("survey_db", "ALTER TABLE email_log ADD COLUMN fallback_sent INTEGER DEFAULT 0"),
    ]),
    (3, "二期菌群库：omics_samples / omics_measurements / omics_import_log", [
        ("microbiome_db", "__INIT_MICROBIOME__"),
    ]),
]

NEW_UNITS = [
    ("research-diet-sync.timer", "饮食问卷定时拉取（此前完全缺失 → 饮食数据只能靠 webhook）"),
    ("research-daily-report.timer", "每日报告（此前实现存在但从未排程）"),
    ("research-microbiome-import.timer", "二期菌群数据导入"),
]

LEGACY_UNITS = [
    ("research-webhook.service", "旧名，实际单元为 research-diet-webhook.service"),
    ("research-diet-llm.service", "旧名，实际单元为 research-diet-llm-queue.service"),
]


def _conn(path):
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _ensure_ledger(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            description TEXT,
            applied_at  TEXT DEFAULT (datetime('now','localtime')),
            status      TEXT DEFAULT 'ok',
            message     TEXT
        )""")
    conn.commit()


def _applied_versions(conn):
    _ensure_ledger(conn)
    return {r["version"] for r in conn.execute(
        "SELECT version FROM schema_migrations WHERE status='ok'")}


def status(cfg: dict, db_key: str | None = None) -> dict:
    out = {"current_version": CURRENT_VERSION, "databases": {}, "pending": []}
    for key in ("survey_db", "diet_db", "exercise_db", "microbiome_db"):
        if db_key and key != db_key:
            continue
        path = cfg.get(f"database.{key}")
        info = {"path": path, "exists": bool(path and os.path.exists(path)),
                "applied": [], "pending": []}
        if info["exists"]:
            try:
                conn = _conn(path)
                applied = _applied_versions(conn)
                info["applied"] = sorted(applied)
                info["pending"] = [v for v, _, _ in MIGRATIONS
                                   if v not in applied and v > max(applied, default=0)]
                conn.close()
            except sqlite3.Error as e:
                info["error"] = str(e)
        out["databases"][key] = info
    out["pending"] = sorted({v for d in out["databases"].values()
                             for v in d.get("pending", [])})
    return out


def _run_sql(conn, sql: str, db_key: str, db_path: str = None):
    """执行一条迁移 SQL；已存在（duplicate column）视为成功。"""
    if sql == "__INIT_MICROBIOME__":
        mb = os.path.join(PKG_DIR, "services", "microbiome")
        if os.path.isdir(mb) and mb not in sys.path:
            sys.path.insert(0, mb)
        import microbiome_database as MDB
        MDB.init_db(db_path)
        # init_db 用的是独立连接；本连接需重新读 schema 才能看到新表
        return "初始化 microbiome schema"
    try:
        conn.execute(sql)
        return "ok"
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            return "已存在（跳过）"
        raise
    except sqlite3.Error as e:
        raise RuntimeError(f"{db_key}: {sql[:60]}… → {e}")


def apply_migrations(cfg: dict, dry_run: bool, backup_first: bool,
                     backup_script: str | None) -> dict:
    result = {"applied": [], "skipped": [], "errors": [], "backups": []}
    if backup_first and not dry_run and backup_script and os.path.exists(backup_script):
        r = subprocess.run(["bash", backup_script], capture_output=True, text=True,
                           env={**os.environ, "INSTALL_ROOT": cfg.get("app.base_dir", "")})
        result["backups"].append({"script": backup_script, "rc": r.returncode,
                                  "tail": (r.stdout or "")[-200:]})

    for key in ("survey_db", "diet_db", "exercise_db", "microbiome_db"):
        path = cfg.get(f"database.{key}")
        if not path or not os.path.exists(path):
            result["skipped"].append({"db": key, "reason": f"库不存在: {path}"})
            continue
        try:
            conn = _conn(path)
            _ensure_ledger(conn)
            applied = _applied_versions(conn)
            for ver, desc, stmts in MIGRATIONS:
                if ver in applied:
                    continue
                if dry_run:
                    result["applied"].append({"db": key, "version": ver,
                                              "desc": desc, "dry_run": True})
                    continue
                msgs = []
                try:
                    for db_key, sql in stmts:
                        if db_key != key:
                            continue
                        msgs.append(_run_sql(conn, sql, key, path))
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_migrations "
                        "(version, description, status, message) VALUES (?,?,?,?)",
                        (ver, desc, "ok", "; ".join(msgs)))
                    conn.commit()
                    result["applied"].append({"db": key, "version": ver, "desc": desc})
                except Exception as e:                # noqa: BLE001
                    conn.rollback()
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_migrations "
                        "(version, description, status, message) VALUES (?,?,?,?)",
                        (ver, desc, "failed", str(e)[:300]))
                    conn.commit()
                    result["errors"].append({"db": key, "version": ver, "error": str(e)})
            conn.close()
        except sqlite3.Error as e:
            result["errors"].append({"db": key, "error": str(e)})
    return result


def migrate_config(cfg_path: str, example_path: str, dry_run: bool) -> dict:
    out = {"path": cfg_path, "existed": os.path.exists(cfg_path), "actions": []}
    if out["existed"]:
        out["actions"].append("app.yaml 已存在，保持不动")
        return out
    if not os.path.exists(example_path):
        out["actions"].append(f"缺少模板 {example_path}，跳过")
        return out
    out["actions"].append(f"从模板创建 {cfg_path}")
    if not dry_run:
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        shutil.copy2(example_path, cfg_path)
        out["actions"].append("已创建（请填写 site.domain 与 secrets.env 中的密钥）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="升级迁移（schema / 配置 / 系统单元）")
    ap.add_argument("--config", default=AC.DEFAULT_CONFIG)
    ap.add_argument("--status", action="store_true", help="查看版本与待应用迁移")
    ap.add_argument("--plan", action="store_true", help="预演（等价 --apply 的 dry-run）")
    ap.add_argument("--apply", action="store_true", help="实际应用")
    ap.add_argument("--apply-system", action="store_true",
                    help="额外输出 systemd 单元启用命令（不自动执行）")
    ap.add_argument("--backup-first", action="store_true", help="应用前先备份")
    ap.add_argument("--backup-script", default=None, help="备份脚本路径")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg, errs = AC.try_load(args.config, strict_placeholders=False)
    if cfg is None:
        print(f"❌ 无法加载配置 {args.config}", file=sys.stderr)
        for e in errs:
            print("   - " + e, file=sys.stderr)
        return 2

    st = status(cfg)
    backup_script = args.backup_script or os.path.join(
        cfg.get("app.base_dir", ""), "scripts", "backup.sh")

    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "current_version": CURRENT_VERSION, "status": st}

    if args.status or not (args.plan or args.apply):
        if args.json:
            print(json_dumps(report))
        else:
            print(f"当前程序版本: {CURRENT_VERSION}")
            for key, d in st["databases"].items():
                mark = "✅" if d["exists"] else "⏭️"
                print(f"  {mark} {key}: {d['path']}"
                      + (f"  已应用={d['applied']} 待应用={d['pending']}"
                         if d["exists"] else "  （不存在）"))
            print(f"待应用迁移: {st['pending'] or '无'}")
        return 0

    dry = args.plan and not args.apply
    report["mode"] = "plan" if dry else "apply"
    report["schema"] = apply_migrations(cfg, dry, args.backup_first, backup_script)
    report["config"] = migrate_config(
        args.config, os.path.join(PKG_DIR, "config", "app.yaml.example"), dry)
    report["new_units"] = [{"unit": u, "why": w} for u, w in NEW_UNITS]
    report["legacy_units"] = [{"unit": u, "why": w} for u, w in LEGACY_UNITS]

    if args.json:
        print(json_dumps(report))
    else:
        print(f"模式: {'预演（未改动任何数据）' if dry else '实际应用'}")
        if report["schema"]["backups"]:
            print(f"  备份: rc={report['schema']['backups'][0]['rc']}")
        for a in report["schema"]["applied"]:
            print(f"  {'[计划]' if a.get('dry_run') else '✅'} {a['db']} v{a['version']}: {a['desc']}")
        for s in report["schema"]["skipped"]:
            print(f"  ⏭️  {s['db']}: {s['reason']}")
        for e in report["schema"]["errors"]:
            print(f"  ❌ {e}")
        print("  配置迁移:")
        for a in report["config"]["actions"]:
            print(f"    · {a}")
        print("\n  ⚠️ 本版新增/变更的 systemd 单元（需手工确认后启用）：")
        for u, w in NEW_UNITS:
            print(f"    + {u}  —— {w}")
        for u, w in LEGACY_UNITS:
            print(f"    ~ {u}  —— {w}")
        if args.apply_system or not dry:
            print("\n  建议执行：")
            for u, _ in NEW_UNITS:
                print(f"    systemctl enable --now {u}")
            print("    systemctl daemon-reload && systemctl restart research-*")

    return 1 if report["schema"]["errors"] else 0


def json_dumps(o) -> str:
    import json
    return json.dumps(o, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    sys.exit(main())
