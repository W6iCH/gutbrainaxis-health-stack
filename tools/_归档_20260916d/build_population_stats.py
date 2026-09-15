#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_population_stats.py — 生成「人群基线统计」内置默认数据
=============================================================================
用途
----
把原始量表问卷按**原生编码口径（Edition B）**全量计分，聚合成**匿名人群统计**
（各量表 × 各时点的 mean / sd / n / 分位数），落为
    services/sjtu_survey_pro/population_stats.default.json

程序（survey_analysis.render_html_report → population_charts.make_bell_svg）
在**没有本地覆盖** population_stats.json 时使用本默认数据，用于画
「该分数在同侪人群中的位置」正态曲线与百分位。

零 PII 承诺
-----------
* 只落**聚合统计**：mean / sd / n / min / max / 分位数。**绝不落任何个体行。**
* 学工号仅在内存中作去重键，不写盘。
* 每个聚合单元样本量 < MIN_N（默认 5）时**整体跳过**，避免小样本反推个体。

口径
----
同时生成两个计分口径的块，便于与程序自身计分规则对齐：
  * "3.0-native-ordinal"  —— 权威口径（`14_评分标准/03_条目库_修正/survey_scoring_native.py`）
  * "2.0-official-rules"  —— 已发布程序的计分实现（`services/sjtu_survey_pro/survey_analysis.py`）
程序按 `SCORING_VERSION` 自动选择匹配块；两口径不一致的量表（PSS-14 / PSQI /
WHOQOL 四领域）其百分位会不同，详见 README「默认人群数据」章节。

用法
----
    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY tools/build_population_stats.py                       # 用默认路径
    $PY tools/build_population_stats.py --raw <...json> \
        --entrylib <...survey_scoring_native.py> --out <...json>
    $PY tools/build_population_stats.py --dry-run             # 只打印摘要

只读约束：`13_问卷原始数据/raw/`、`14_评分标准/` 全程只读。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import statistics
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)                      # 12_部署包_可发布
PARENT = os.path.dirname(PKG_DIR)                    # 整合包根
SVC_DIR = os.path.join(PKG_DIR, "services", "sjtu_survey_pro")

DEFAULT_RAW = os.path.join(PARENT, "13_问卷原始数据_20260916", "raw", "量表问卷_226份.json")
DEFAULT_ENTRYLIB = os.path.join(PARENT, "14_评分标准", "03_条目库_修正",
                                "survey_scoring_native.py")
DEFAULT_OUT = os.path.join(SVC_DIR, "population_stats.default.json")

MIN_N = 5                # 单个聚合单元最小样本量（更低则跳过）
SCORING_NATIVE = "3.0-native-ordinal"
SCORING_OFFICIAL = "2.0-official-rules"

# 输出量表键 → 展示量程（用于正态曲线裁剪）
SCALE_RANGES = {
    "GAD-7": (0, 21), "PHQ-9": (0, 27), "PSQI": (0, 21), "PSS-14": (0, 56),
    "GSRS": (15, 75), "VSI": (15, 90),
    "IPAQ-S": (0, 12000),
    "WHOQOL-BREF": (0, 100),
    "WHOQOL-BREF-生理": (0, 100), "WHOQOL-BREF-心理": (0, 100),
    "WHOQOL-BREF-社会": (0, 100), "WHOQOL-BREF-环境": (0, 100),
    "DEBQ-emotional": (1, 5), "DEBQ-external": (1, 5), "DEBQ-restrained": (1, 5),
    "BMI": (10, 40),
}

WAVE_LABELS = [("T0", "W1"), ("T1", "W2"), ("T2", "W3")]


# ── 模块按路径加载（避免同名碰撞）────────────────────────────────────────

def load_module(path: str, name: str):
    if not os.path.exists(path):
        raise SystemExit(f"❌ 模块不存在: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 原始行解析（与 13_.../核算/scripts/lib_raw.py 口径一致）───────────────

def rows_of(obj):
    if isinstance(obj, dict):
        data = obj.get("data", obj)
        if isinstance(data, dict):
            return data.get("rows", []) or []
        if isinstance(data, list):
            return data
    if isinstance(obj, list):
        return obj
    return []


def _flat_answers(row):
    return [(it.get("question", {}) or {}, it.get("answer", "")) for it in row.get("answers", [])]


def canonical_answers(row):
    """把一行答卷展开为计分核心所需条目（与 feedback_server 转换口径一致）。"""
    out = []
    for q, ans in _flat_answers(row):
        title = q.get("title", "")
        qtype = q.get("question_type", "")
        if qtype == "矩阵单选题":
            ans_name = q.get("answer_name", []) or []
            if isinstance(ans, list):
                for i, sub in enumerate(ans):
                    q_text = ""
                    if i < len(ans_name):
                        k = ans_name[i]
                        q_text = k.get("key", "") if isinstance(k, dict) else str(k)
                    sub_text = sub.get("title", "") if isinstance(sub, dict) else str(sub)
                    val = sub.get("value", "") if isinstance(sub, dict) else ""
                    m = re.match(r"option(\d+)$", str(val))
                    out.append({"question": q_text or title, "answer_text": sub_text,
                                "matrix": title, "option_idx": int(m.group(1)) if m else None})
            else:
                out.append({"question": title, "answer_text": str(ans),
                            "matrix": title, "option_idx": None})
        else:
            ans_text = ans.get("label", str(ans)) if isinstance(ans, dict) else str(ans)
            out.append({"question": title, "answer_text": ans_text,
                        "matrix": None, "option_idx": None})
    return out


def basic_field(row, title_contains: str):
    for q, ans in _flat_answers(row):
        if title_contains in (q.get("title") or ""):
            return ans.get("label", str(ans)) if isinstance(ans, dict) else ans
    return None


def norm_sid(v):
    if v in (None, ""):
        return None
    digits = re.sub(r"\D", "", str(v))
    return digits or None


def wave_of(ts):
    """时点划分（沿用既有口径）：W1 ≤2026-07-10；W2 ≤2026-07-21；W3 其后。"""
    d = str(ts)[:10]
    if d <= "2026-07-10":
        return "T0"
    if d <= "2026-07-21":
        return "T1"
    return "T2"


# ── 计分：原生口径（权威）────────────────────────────────────────────────

def score_native(row, SN):
    ans = canonical_answers(row)
    sc = SN.score_all(ans, strict=False)
    out = {}
    for k in ("GAD-7", "PHQ-9", "PSS-14", "GSRS", "VSI", "PSQI"):
        out[k] = sc[k]["total"]
    sub = sc["DEBQ"]["subscales"]
    out["DEBQ-emotional"] = sub.get("情绪性饮食", {}).get("mean")
    out["DEBQ-external"] = sub.get("外部性饮食", {}).get("mean")
    out["DEBQ-restrained"] = sub.get("限制性饮食", {}).get("mean")
    dom = sc["WHOQOL-BREF"]["domain_scores"]
    dom100 = [dom[d]["score_0_100"] for d in
              ("生理领域", "心理领域", "社会关系领域", "环境领域") if d in dom]
    out["WHOQOL-BREF"] = round(sum(dom100) / len(dom100), 2) if dom100 else None
    for zh, key in (("生理领域", "WHOQOL-BREF-生理"), ("心理领域", "WHOQOL-BREF-心理"),
                    ("社会关系领域", "WHOQOL-BREF-社会"), ("环境领域", "WHOQOL-BREF-环境")):
        out[key] = dom.get(zh, {}).get("score_0_100")
    by_q = {}
    for it in ans:
        by_q.setdefault(it.get("question", ""), it.get("answer_text"))
    out["IPAQ-S"] = SN.score_ipaq(by_q)["total"]
    bmi = SN.compute_bmi(basic_field(row, "身高"), basic_field(row, "体重"))
    out["BMI"] = (bmi or {}).get("total")
    return out, len(sc.get("_audit", []))


# ── 计分：已发布程序口径（2.0-official-rules）────────────────────────────

def score_official(row, SA):
    rep = SA.analyze_survey_responses({"data": {"rows": [row]}})
    scores = rep.get("scores", {}) or {}
    out = {}
    for k in ("GAD-7", "PHQ-9", "PSS-14", "GSRS", "VSI", "PSQI"):
        out[k] = (scores.get(k) or {}).get("total")
    debq = (scores.get("DEBQ") or {}).get("subscales", {}) or {}
    out["DEBQ-emotional"] = (debq.get("情绪性饮食") or {}).get("mean")
    out["DEBQ-external"] = (debq.get("外部性饮食") or {}).get("mean")
    out["DEBQ-restrained"] = (debq.get("限制性饮食") or {}).get("mean")
    who = scores.get("WHOQOL-BREF") or {}
    dom = who.get("domain_scores", {}) or {}
    dom100 = [dom[d]["score_0_100"] for d in
              ("生理领域", "心理领域", "社会关系领域", "环境领域")
              if d in dom and dom[d].get("score_0_100") is not None]
    out["WHOQOL-BREF"] = round(sum(dom100) / len(dom100), 2) if dom100 else None
    for zh, key in (("生理领域", "WHOQOL-BREF-生理"), ("心理领域", "WHOQOL-BREF-心理"),
                    ("社会关系领域", "WHOQOL-BREF-社会"), ("环境领域", "WHOQOL-BREF-环境")):
        out[key] = dom.get(zh, {}).get("score_0_100")
    out["IPAQ-S"] = (scores.get("IPAQ-S") or {}).get("total")
    bmi = rep.get("scores", {}).get("BMI") or {}
    out["BMI"] = bmi.get("total")
    return out


# ── 聚合 ──────────────────────────────────────────────────────────────────

def _quantile(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 3)
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    frac = k - lo
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac, 3)


def agg(values, lo=None, hi=None):
    """对一组分值做匿名聚合；样本量不足返回 None。"""
    vals = [v for v in values if v is not None]
    if len(vals) < MIN_N:
        return None
    vals_sorted = sorted(float(v) for v in vals)
    mean = statistics.fmean(vals_sorted)
    sd = statistics.stdev(vals_sorted) if len(vals_sorted) > 1 else 0.0
    return {
        "n": len(vals_sorted),
        "mean": round(mean, 3),
        "sd": round(sd, 3),
        "min": round(vals_sorted[0], 3),
        "max": round(vals_sorted[-1], 3),
        "median": _quantile(vals_sorted, 0.50),
        "quantiles": {f"q{int(p*100):02d}": _quantile(vals_sorted, p)
                      for p in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)},
        "range": [lo, hi],
    }


def build_block(per_sub, lo_hi_of):
    """per_sub: [{'T0': {scale: val}, ...}] 形式的分层字典列表 → 量表聚合块。"""
    scales = sorted({k for rec in per_sub for k in rec if not k.startswith("_")})
    block = {}
    for s in scales:
        lo, hi = lo_hi_of(s)
        pooled = agg([rec.get(s) for rec in per_sub], lo, hi)
        if pooled is None:
            continue
        entry = dict(pooled)
        entry["by_timepoint"] = {}
        for label in ("T0", "T1", "T2"):
            sub = [rec[s] for rec in per_sub if rec.get("_wave") == label]
            a = agg(sub, lo, hi)
            if a:
                entry["by_timepoint"][label] = a
        block[s] = entry
    return block


def main():
    global MIN_N
    ap = argparse.ArgumentParser(description="生成匿名人群基线统计（内置默认数据）")
    ap.add_argument("--raw", default=DEFAULT_RAW, help="量表问卷原始 JSON")
    ap.add_argument("--entrylib", default=DEFAULT_ENTRYLIB,
                    help="原生编码条目库 survey_scoring_native.py")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    ap.add_argument("--min-n", type=int, default=MIN_N, help="聚合单元最小样本量")
    ap.add_argument("--dry-run", action="store_true", help="只打印摘要，不写文件")
    args = ap.parse_args()
    MIN_N = args.min_n

    with open(args.raw, "r", encoding="utf-8") as f:
        raw = json.load(f)
    rows = rows_of(raw)
    if not rows:
        raise SystemExit(f"❌ 未从 {args.raw} 解析到答卷")

    SN = load_module(args.entrylib, "survey_scoring_native")
    sys.path.insert(0, SVC_DIR)
    sys.path.insert(0, os.path.join(SVC_DIR, "..", "admin_console"))
    import survey_analysis as SA            # 已发布程序口径

    native_per, official_per = [], []
    sids, unmapped_total = set(), 0
    for row in rows:
        sid = norm_sid(basic_field(row, "学工号"))
        if sid:
            sids.add(sid)
        wave = wave_of(row.get("submitted_at"))
        nv, n_unmapped = score_native(row, SN)
        unmapped_total += n_unmapped
        nv["_wave"] = wave
        native_per.append(nv)
        ov = score_official(row, SA)
        ov["_wave"] = wave
        official_per.append(ov)

    def lo_hi(s):
        return SCALE_RANGES.get(s, (None, None))

    by_version = {
        SCORING_NATIVE: {"scales": build_block(native_per, lo_hi)},
        SCORING_OFFICIAL: {"scales": build_block(official_per, lo_hi)},
    }

    out = {
        "_README": [
            "人群基线统计 —— 内置默认数据（匿名聚合，由 tools/build_population_stats.py 生成）",
            "",
            "内容：各量表 × 各时点的 mean / sd / n / min / max / median / 分位数。",
            "⚠️ 本文件**只含聚合统计**，不含任何个体作答行。",
            "",
            "程序读取顺序（population_charts._load）：",
            "  1) services/sjtu_survey_pro/population_stats.json        ← 本地覆盖（部署方可用真实基线替换）",
            "  2) services/sjtu_survey_pro/population_stats.default.json ← 本文件（内置默认）",
            "  · 按程序 SCORING_VERSION 选择 by_version 中匹配的口径块；无匹配则退回原生口径块。",
            "",
            "替换方式：见 README.md「默认人群数据」章节。",
        ],
        "schema": 2,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator": "tools/build_population_stats.py",
        "source": {
            "file": os.path.basename(args.raw),
            "entrylib": os.path.basename(args.entrylib),
            "n_submissions": len(rows),
            "n_students": len(sids),
            "waves": {lbl: sum(1 for r in native_per if r["_wave"] == lbl)
                      for lbl in ("T0", "T1", "T2")},
            "wave_rule": "T0≤2026-07-10；T1≤2026-07-21；T2 其后",
            "unmapped_items_native": unmapped_total,
            "min_n_per_cell": MIN_N,
            "note": "聚合统计；无个体行；n<min_n 的单元不输出",
        },
        "scoring_versions": [SCORING_NATIVE, SCORING_OFFICIAL],
        "recommended_version": SCORING_NATIVE,
        "by_version": by_version,
        # 兼容旧格式：顶层 scales 直接放「推荐口径」块
        "scales": by_version[SCORING_NATIVE]["scales"],
    }

    print(f"✅ 解析答卷 {len(rows)} 份｜学生 {len(sids)} 名｜"
          f"时点 T0/T1/T2 = {out['source']['waves']['T0']}/"
          f"{out['source']['waves']['T1']}/{out['source']['waves']['T2']}")
    print(f"   原生口径未映射条目：{unmapped_total} 处")
    for ver, blk in by_version.items():
        print(f"   [{ver}] 量表数 {len(blk['scales'])}："
              f"{', '.join(sorted(blk['scales']))}")
    if args.dry_run:
        print("（--dry-run：未写文件）")
        return
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✅ 已写入 {args.out}")


if __name__ == "__main__":
    main()
