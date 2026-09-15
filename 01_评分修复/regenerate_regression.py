#!/usr/bin/env python3
"""
旧分 vs 新分回归对照 — regenerate_regression.py
============================================================
用途
----
对只读答卷库中的历史答卷，用**修正后的计分核心**重算一遍，并与库中
由**旧代码**写入的分值列对比，产出统计对照。

数据来源（只读，禁止修改）
--------------------------
问卷答卷库（SQLite），**必须显式指定**，不硬编码任何个人目录：

    SOURCE_DB=/path/to/survey_data.db python3 regenerate_regression.py
    # 或
    python3 regenerate_regression.py --db /path/to/survey_data.db

脚本以 `mode=ro` 只读打开，绝不写入。
表 `submissions` 的 `responses` 列（每道题的答案 JSON）用于重算；
库中旧分列（pss14_score / psqi_score / whoqol_score / vsi_score / gsrs_score /
gad7_score / phq9_score / ipaq_met_min_week / debq_* / bmi_score）作为旧分对照。
可选：`OLD_SCORING_DIR` 指向包含旧 `survey_analysis.py` 的目录，用于统计旧归类函数的条目识别数。

隐私红线
--------
本脚本**只输出统计量**（N、均值、标准差、极值、差值、分档迁移矩阵），
绝不输出姓名 / 学号 / 邮箱 / submission_id 等任何可识别信息。

用法
----
    python3 regenerate_regression.py                 # 打印报告
    python3 regenerate_regression.py --out 旧新分回归对照.md
    SOURCE_DB=/path/to.db python3 regenerate_regression.py
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "services", "sjtu_survey_pro"))

import survey_scoring as S  # noqa: E402

DEFAULT_DB = os.environ.get("SOURCE_DB", "")
OLD_SCORING_DIR = os.environ.get("OLD_SCORING_DIR", "")

# 旧分列 → 新计分键
COLUMN_MAP = {
    "gad7_score": "GAD-7",
    "phq9_score": "PHQ-9",
    "psqi_score": "PSQI",
    "pss14_score": "PSS-14",
    "gsrs_score": "GSRS",
    "vsi_score": "VSI",
    "ipaq_met_min_week": "IPAQ-S",
}

DEBQ_COLS = {
    "debq_emotional_score": "情绪性饮食",
    "debq_external_score": "外部性饮食",
    "debq_restrained_score": "限制性饮食",
}

BANDS = {
    "GAD-7": S.GAD7_BANDS,
    "PHQ-9": S.PHQ9_BANDS,
    "PSS-14": S.PSS14_BANDS,
    "GSRS": S.GSRS_BANDS_CUSTOM,
    "VSI": S.VSI_BANDS_CUSTOM,
}


def load_rows(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(submissions)")]
    wanted = ["responses"] + [c for c in COLUMN_MAP if c in cols] \
             + [c for c in DEBQ_COLS if c in cols] \
             + [c for c in ("bmi_score",) if c in cols]
    sql = "SELECT " + ", ".join(wanted) + " FROM submissions ORDER BY id ASC"
    return list(conn.execute(sql)), cols


def stats(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return None
    return {
        "n": len(v), "mean": statistics.fmean(v),
        "sd": statistics.stdev(v) if len(v) > 1 else 0.0,
        "min": min(v), "max": max(v),
    }


def fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def describe(old, new):
    """对同一指标的两组配对值出统计。"""
    pairs = [(o, n) for o, n in zip(old, new) if o is not None and n is not None]
    so, sn = stats(old), stats(new)
    if not pairs:
        return None
    diffs = [n - o for o, n in pairs]
    changed = sum(1 for d in diffs if abs(d) > 1e-9)
    return {
        "n_paired": len(pairs),
        "old": so, "new": sn,
        "diff_mean": statistics.fmean(diffs),
        "diff_sd": statistics.stdev(diffs) if len(diffs) > 1 else 0.0,
        "diff_min": min(diffs), "diff_max": max(diffs),
        "absdiff_mean": statistics.fmean([abs(d) for d in diffs]),
        "changed": changed,
        "changed_pct": 100.0 * changed / len(pairs),
    }


def migration_matrix(scale, old_vals, new_vals):
    bands = BANDS.get(scale)
    if not bands:
        return None
    labels = [b[2] for b in bands]
    mat = {a: {b: 0 for b in labels} for a in labels}

    def lab(x):
        if x is None:
            return None
        for lo, hi, name in bands:
            if lo <= x <= hi:
                return name
        # 越界（旧量程错误时会出现，例如 GSRS>75）
        return "越界(旧量程不可达)"

    for o, n in zip(old_vals, new_vals):
        if o is None or n is None:
            continue
        lo_, ln_ = lab(o), lab(n)
        if lo_ is None or ln_ is None:
            continue
        mat.setdefault(lo_, {}).setdefault(ln_, 0)
        mat[lo_][ln_] = mat[lo_].get(ln_, 0) + 1
    return mat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="只读答卷库路径（也可用环境变量 SOURCE_DB）")
    ap.add_argument("--out")
    args = ap.parse_args()

    if not args.db:
        print("错误：未指定只读答卷库路径。请用 --db /path/to/survey_data.db "
              "或设置环境变量 SOURCE_DB。", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"错误：只读答卷库不存在：{args.db}", file=sys.stderr)
        return 2

    rows, db_cols = load_rows(args.db)
    total = len(rows)

    lines = []
    A = lines.append

    A("# 旧分 vs 新分 回归对照表")
    A("")
    A("> 本表由 `01_评分修复/regenerate_regression.py` 自动生成。")
    A("> **仅含统计量**：不含任何姓名、学号、邮箱或提交编号。")
    A("")
    A(f"- 答卷样本数：**{total}**")
    A(f"- 数据源：只读答卷库（`survey_data.db`，本次未做任何写操作）")
    A("- 旧分来源：库中由旧计分代码写入的分值列（`*_score` / `*_met_min_week`）")
    A(f"- 新分来源：修正后计分核心 `survey_scoring.py`（规则版本 "
      f"`2.0-official-rules`）")
    A("")

    # ── 条目覆盖率对照 ────────────────────────────────────────────────────
    A("## 一、条目识别覆盖率（这是差异最大的根源）")
    A("")
    A("| 量表 | 旧代码识别条目数 | 新代码识别条目数 | 期望条目数 | 旧覆盖率 | 新覆盖率 |")
    A("|---|---|---|---|---|---|")
    old_cls = _old_coverage(rows)
    new_cov = _new_coverage(rows)
    for scale in ["PSS-14", "GAD-7", "PHQ-9", "PSQI", "IPAQ-S",
                  "WHOQOL-BREF", "DEBQ", "GSRS", "VSI"]:
        o = old_cls.get(scale, 0)
        n = new_cov.get(scale, {}).get("recognized", 0)
        e = new_cov.get(scale, {}).get("expected", 0)
        A(f"| {scale} | {o} | {n} | {e} | "
          f"{_pct(o, e)} | {_pct(n, e)} |")
    A("")
    A("> 旧代码的识别数由原 `find_scale_by_title()` 逐条判定得出；"
      "新代码由条目库显式声明。")
    A("")

    # ── 各量表分值对照 ────────────────────────────────────────────────────
    A("## 二、各量表总分对照")
    A("")
    A("| 量表 | 配对样本 | 旧均值 (SD) | 新均值 (SD) | 旧范围 | 新范围 | "
      "差值均值 | 差值 SD | 平均绝对差 | 发生变化例数 | 变化率 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|")

    per_scale = {}
    for col, scale in COLUMN_MAP.items():
        if col not in db_cols:
            continue
        old_vals, new_vals = [], []
        for r in rows:
            try:
                resp = json.loads(r["responses"] or "{}")
            except Exception:
                resp = {}
            out = S.score_all(resp)
            nv = out.get(scale, {}).get("total")
            ov = r[col]
            old_vals.append(ov)
            new_vals.append(nv)
        d = describe(old_vals, new_vals)
        per_scale[scale] = (d, old_vals, new_vals)
        if not d:
            A(f"| {scale} | 0 | — | — | — | — | — | — | — | — | — |")
            continue
        A(f"| {scale} | {d['n_paired']} | "
          f"{fmt(d['old']['mean'])} ({fmt(d['old']['sd'])}) | "
          f"{fmt(d['new']['mean'])} ({fmt(d['new']['sd'])}) | "
          f"{fmt(d['old']['min'],1)}–{fmt(d['old']['max'],1)} | "
          f"{fmt(d['new']['min'],1)}–{fmt(d['new']['max'],1)} | "
          f"{fmt(d['diff_mean'])} | {fmt(d['diff_sd'])} | "
          f"{fmt(d['absdiff_mean'])} | {d['changed']} | {fmt(d['changed_pct'],1)}% |")
    A("")

    # ── DEBQ 子量表 ───────────────────────────────────────────────────────
    A("## 三、DEBQ 子量表对照（旧=原始分求和，新=1–5 均分）")
    A("")
    A("> 旧代码把子量表各题 0–4 分**直接求和**且含 total；"
      "新代码按官方 **1–5** 计分并只报**均分**（含外部性饮食 1 条反向条目）。")
    A("")
    A("| 子量表 | 配对样本 | 旧均值 (SD) | 新均值 (SD) | 差值均值 | 平均绝对差 |")
    A("|---|---|---|---|---|---|")
    for col, sub in DEBQ_COLS.items():
        if col not in db_cols:
            continue
        old_vals, new_vals = [], []
        for r in rows:
            try:
                resp = json.loads(r["responses"] or "{}")
            except Exception:
                resp = {}
            sub_data = S.score_debq(S.group_items(resp)[0].get("DEBQ", []))["subscales"]
            ov = r[col]
            nv = sub_data.get(sub, {}).get("sum")
            old_vals.append(ov)
            new_vals.append(nv)
        d = describe(old_vals, new_vals)
        if d:
            A(f"| {sub} | {d['n_paired']} | {fmt(d['old']['mean'])} ({fmt(d['old']['sd'])}) | "
              f"{fmt(d['new']['mean'])} ({fmt(d['new']['sd'])}) | "
              f"{fmt(d['diff_mean'])} | {fmt(d['absdiff_mean'])} |")
    A("")

    # ── 分档迁移矩阵 ──────────────────────────────────────────────────────
    A("## 四、分档迁移矩阵（旧分档 → 新分档）")
    A("")
    A("> 行=旧代码分档，列=新代码分档。数值为该格答卷数。")
    A("> 旧分档按**新**分档定义重算，以便同尺比较；越界值单列。")
    A("")
    for scale, (d, ov, nv) in per_scale.items():
        mat = migration_matrix(scale, ov, nv)
        if not mat:
            continue
        A(f"### {scale}")
        A("")
        cols = sorted({c for row in mat.values() for c in row})
        A("| 旧 \\ 新 | " + " | ".join(cols) + " |")
        A("|---" * (len(cols) + 1) + "|")
        for r_ in sorted(mat):
            A(f"| {r_} | " + " | ".join(str(mat[r_].get(c, 0)) for c in cols) + " |")
        A("")

    # ── 缺项情况 ──────────────────────────────────────────────────────────
    A("## 五、缺项与可计分条目情况（新代码）")
    A("")
    A("| 量表 | 平均可计分条目数 | 平均期望条目数 | 完全可计分答卷占比 | 结论不可靠答卷占比 |")
    A("|---|---|---|---|---|")
    agg = {}
    for r in rows:
        try:
            resp = json.loads(r["responses"] or "{}")
        except Exception:
            resp = {}
        out = S.score_all(resp)
        for scale, res in out.items():
            if scale in ("_meta", "BMI"):
                continue
            a = agg.setdefault(scale, {"n": 0, "sum_scored": 0, "sum_exp": 0,
                                       "full": 0, "invalid": 0})
            a["n"] += 1
            a["sum_scored"] += res.get("n_items") or 0
            a["sum_exp"] += res.get("n_expected") or 0
            if (res.get("n_items") or 0) >= (res.get("n_expected") or 0):
                a["full"] += 1
            if not res.get("valid"):
                a["invalid"] += 1
    for scale, a in sorted(agg.items()):
        if not a["n"]:
            continue
        A(f"| {scale} | {a['sum_scored']/a['n']:.1f} | {a['sum_exp']/a['n']:.1f} | "
          f"{100*a['full']/a['n']:.1f}% | {100*a['invalid']/a['n']:.1f}% |")
    A("")

    # ── 结论 ──────────────────────────────────────────────────────────────
    A("## 六、结论要点")
    A("")
    A("1. 旧分的最大问题不是算式细节，而是**条目识别遗漏**：多个量表只用了"
      "其应有条目的一小部分参与计分（见第一节），因此旧分与量表定义不对应。")
    A("2. 量程错误（GSRS/VSI 上界 105、VSI 定义 75）导致旧分档存在**不可达区间**，"
      "旧分档标签系统性偏移。")
    A("3. 反向计分缺失（PSS-14、DEBQ 外部性、VSI 正向条目）使旧分方向性错误。")
    A("4. 新旧分**不可比**：任何使用旧分的分析结论（分档比例、均值比较）都应"
      "在新分基础上重跑；建议在论文方法学中说明该修正。")
    A("")

    text = "\n".join(lines)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"\n[已写出] {args.out}", file=sys.stderr)
    return 0


def _pct(a, b):
    if not b:
        return "—"
    return f"{100.0*a/b:.0f}%"


def _old_coverage(rows):
    """用旧 `find_scale_by_title` 统计识别条目数（需 OLD_SCORING_DIR 指向旧代码目录）。"""
    counts = {}
    if not OLD_SCORING_DIR:
        print("[info] 未设置 OLD_SCORING_DIR，跳过旧覆盖率统计（可复现时再设）",
              file=sys.stderr)
        return counts
    try:
        sys.path.insert(0, OLD_SCORING_DIR)
        import survey_analysis as OLD  # noqa
    except Exception as e:  # pragma: no cover
        print(f"[warn] 无法加载旧计分模块，跳过旧覆盖率统计: {e}", file=sys.stderr)
        return counts
    titles = set()
    for r in rows[:1]:
        try:
            resp = json.loads(r["responses"] or "{}")
            titles.update(resp.keys())
        except Exception:
            pass
    for t in titles:
        s = OLD.find_scale_by_title(t)
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts


def _new_coverage(rows):
    for r in rows[:1]:
        try:
            resp = json.loads(r["responses"] or "{}")
        except Exception:
            continue
        cov, _ = S.coverage_report(resp)
        return cov
    return {}


if __name__ == "__main__":
    sys.exit(main())
