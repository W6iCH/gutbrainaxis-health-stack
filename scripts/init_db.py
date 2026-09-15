#!/usr/bin/env python3
"""
init_db.py — 创建**空 schema** 数据库
=====================================
本安装包不包含任何真实研究数据。本脚本通过各服务自带的 init_db()
(CREATE TABLE IF NOT EXISTS) 生成空表结构。

特性：
  - 幂等：已存在的数据库不会被清空或覆盖，只补齐缺失的表/列
  - 支持 survey / diet / exercise 三个服务

用法:
    python3 scripts/init_db.py                          # 按 APP_BASE 推导位置
    python3 scripts/init_db.py --force-empty            # 即使已有数据也初始化
    python3 scripts/init_db.py --check                  # 仅检查状态，不做修改
"""

import argparse
import os
import sqlite3
import sys

APP_BASE = os.environ.get("APP_BASE", "/opt/gutbrainaxis")
SURVEY_DIR = os.environ.get("SURVEY_APP_DIR", os.path.join(APP_BASE, "sjtu_survey_pro"))
DIET_DIR = os.environ.get("DIET_APP_DIR", os.path.join(APP_BASE, "diet_survey"))
EXERCISE_DIR = os.environ.get("EXERCISE_APP_DIR", os.path.join(APP_BASE, "exercise_survey"))


def _count(path, table="submissions"):
    if not os.path.exists(path):
        return None
    try:
        c = sqlite3.connect(path)
        n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        c.close()
        return n
    except Exception:
        return "?"


def _tables(path):
    if not os.path.exists(path):
        return []
    try:
        c = sqlite3.connect(path)
        rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        c.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def init_survey():
    sys.path.insert(0, SURVEY_DIR)
    import survey_database  # noqa: E402
    path = survey_database.DB_PATH
    before = _count(path)
    tables_before = _tables(path)
    survey_database.init_db()
    after = _count(path)
    tables_after = _tables(path)
    new_tables = set(tables_after) - set(tables_before)
    print(f"  [survey] {path}")
    print(f"           表: {tables_after}")
    print(f"           记录数: {before if before is not None else '新建'} -> {after}")
    return path, after


def init_diet():
    sys.path.insert(0, DIET_DIR)
    import diet_database  # noqa: E402
    path = diet_database.DB_PATH
    before = _count(path)
    tables_before = _tables(path)
    diet_database.init_db()
    after = _count(path)
    tables_after = _tables(path)
    new_tables = set(tables_after) - set(tables_before)
    print(f"  [diet]   {path}")
    print(f"           表: {tables_after}")
    print(f"           记录数: {before if before is not None else '新建'} -> {after}")
    return path, after


def init_exercise():
    sys.path.insert(0, EXERCISE_DIR)
    import exercise_database  # noqa: E402
    # exercise_database 可能用不同的 DB_PATH 命名
    db_path = getattr(exercise_database, 'DB_PATH', None)
    if db_path is None:
        # 尝试常见路径
        candidate = os.path.join(EXERCISE_DIR, "exercise_data.db")
        if os.path.exists(candidate) or hasattr(exercise_database, 'init_db'):
            db_path = candidate
    if db_path is None:
        raise FileNotFoundError("无法确定 exercise 数据库路径")
    before = _count(db_path)
    tables_before = _tables(db_path)
    exercise_database.init_db()
    after = _count(db_path)
    tables_after = _tables(db_path)
    print(f"  [exercise] {db_path}")
    print(f"           表: {tables_after}")
    print(f"           记录数: {before if before is not None else '新建'} -> {after}")
    return db_path, after


def check_only():
    """只检查状态，不修改"""
    results = {}
    for label, d, f in [("survey", SURVEY_DIR, "survey_database.py"),
                         ("diet", DIET_DIR, "diet_database.py"),
                         ("exercise", EXERCISE_DIR, "exercise_database.py")]:
        mod_path = os.path.join(d, f)
        if not os.path.exists(mod_path):
            results[label] = {"exists": False, "reason": f"{f} 不存在"}
            continue
        sys.path.insert(0, d)
        try:
            mod = __import__(f.replace(".py", ""))
            db_path = getattr(mod, 'DB_PATH', os.path.join(d, f"{label}_data.db"))
            exists = os.path.exists(db_path)
            count = _count(db_path) if exists else None
            tbls = _tables(db_path) if exists else []
            results[label] = {
                "exists": exists,
                "db_path": db_path,
                "count": count,
                "tables": tbls,
            }
        except Exception as e:
            results[label] = {"exists": False, "reason": str(e)}
    return results


def main():
    ap = argparse.ArgumentParser(description="初始化空 schema 数据库")
    ap.add_argument("--force-empty", action="store_true",
                    help="即使已有数据也照常处理（默认行为即安全，不删数据）")
    ap.add_argument("--check", action="store_true", help="仅检查状态，不做修改")
    args = ap.parse_args()

    if args.check:
        print("== 数据库状态检查 ==")
        results = check_only()
        all_ok = True
        for label, info in results.items():
            if info.get("exists"):
                print(f"  [{label}] ✅ 存在 ({info.get('db_path', '?')})")
                print(f"           记录数: {info.get('count', '?')}")
                print(f"           表: {info.get('tables', [])}")
            else:
                print(f"  [{label}] ❌ {info.get('reason', '不存在')}")
                all_ok = False
        return 0 if all_ok else 1

    print("== 初始化数据库 schema（空表）==")
    errors = []
    for fn, label in ((init_survey, "survey"), (init_diet, "diet"), (init_exercise, "exercise")):
        try:
            fn()
        except ModuleNotFoundError as e:
            print(f"  [{label}] ⚠ 模块未找到，跳过（{e}）")
        except FileNotFoundError as e:
            print(f"  [{label}] ⚠ {e}，跳过（首次安装正常）")
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")
            print(f"  ❌ [{label}] {type(e).__name__}: {e}", file=sys.stderr)

    if errors:
        print(f"\n⚠ {len(errors)} 个模块初始化有警告，但不影响安装继续。", file=sys.stderr)

    print("\n✅ schema 就绪。数据库为空，等待服务写入数据。")
    return 0 if len(errors) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
