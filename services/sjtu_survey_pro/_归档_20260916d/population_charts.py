#!/usr/bin/env python3
"""
Population Statistics & SVG Chart Renderer
===========================================
Loads population baseline data and generates inline SVG normal-distribution charts
showing the user's position within the university student population.
Lightweight: pure SVG, no external images, ~1KB per chart.

数据来源（按优先级）
--------------------
  1. population_stats.json          ← 本地覆盖（部署方可放真实基线）
  2. population_stats.default.json  ← 仓库内置默认数据（匿名聚合，见 README）
若两者都缺失/无对应量表，图表自动跳过（返回空串），不影响计分与报告生成。

口径选择
--------
内置默认数据同时含两个计分口径的块（`by_version`）。本模块按
`SCORING_VERSION`（环境变量，默认取 survey_analysis 的口径）选择匹配块；
无匹配时退回 `recommended_version`。旧式扁平格式（顶层 `scales`）仍兼容。

零 PII：本模块只读聚合统计，不接触个体作答。
"""

import json
import math
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(BASE_DIR, "population_stats.json")
DEFAULT_STATS_FILE = os.path.join(BASE_DIR, "population_stats.default.json")

# 程序计分口径（与 survey_analysis.SCORING_VERSION 保持一致的语义）
DEFAULT_SCORING_VERSION = os.environ.get("SCORING_VERSION", "3.0-native-ordinal")

# ── Load population data ───────────────────────────────────────────────

_population = None
_population_path = None

# 外部 key → 数据文件内的 scales 键
SCALE_KEY_MAP = {
    "GAD-7": "GAD-7",
    "PHQ-9": "PHQ-9",
    "PSQI": "PSQI",
    "PSS-14": "PSS-14",
    "GSRS": "GSRS",
    "VSI": "VSI",
    "IPAQ-S": "IPAQ-S",
    "BMI": "BMI",
    "WHOQOL-BREF": "WHOQOL-BREF",
    "WHOQOL-BREF-生理": "WHOQOL-BREF-生理",
    "WHOQOL-BREF-心理": "WHOQOL-BREF-心理",
    "WHOQOL-BREF-社会": "WHOQOL-BREF-社会",
    "WHOQOL-BREF-环境": "WHOQOL-BREF-环境",
    "debq_emotional": "DEBQ-emotional",
    "debq_external": "DEBQ-external",
    "debq_restrained": "DEBQ-restrained",
    "DEBQ-emotional": "DEBQ-emotional",
    "DEBQ-external": "DEBQ-external",
    "DEBQ-restrained": "DEBQ-restrained",
}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _normalize(data):
    """把 (旧式扁平 | 新式 by_version) 统一成 {'scales': {...}, 'version': str, ...}。"""
    if not data:
        return None
    if "by_version" in data and isinstance(data["by_version"], dict):
        blocks = data["by_version"]
        want = DEFAULT_SCORING_VERSION
        chosen = None
        if want in blocks:
            chosen = want
        else:
            # 前缀匹配（如 2.0 / 3.0）
            for k in blocks:
                if str(k).startswith(str(want).split("-")[0]):
                    chosen = k
                    break
        if chosen is None:
            chosen = data.get("recommended_version") or next(iter(blocks))
            blocks = {**blocks}
        blk = blocks.get(chosen, {})
        return {
            "scales": blk.get("scales", {}) if isinstance(blk, dict) else {},
            "version": chosen,
            "source": (data.get("source") or {}).get("file", ""),
            "generated_at": data.get("generated_at", ""),
            "path": _population_path,
        }
    # 旧式扁平
    return {
        "scales": data.get("scales", {}) or {},
        "version": data.get("computed_with", "unknown"),
        "source": data.get("source", ""),
        "generated_at": "",
        "path": _population_path,
    }


def _load():
    global _population, _population_path
    if _population is None:
        for path in (STATS_FILE, DEFAULT_STATS_FILE):
            data = _read_json(path)
            if data:
                _population_path = path
                norm = _normalize(data)
                if norm:
                    _population = norm
                    return _population
        _population = {"scales": {}, "version": None, "path": None}
    return _population


def population_meta() -> dict:
    """暴露基线来源信息，供报告页脚/自检展示。"""
    p = _load()
    return {"path": p.get("path"), "version": p.get("version"),
            "source": p.get("source"), "n_scales": len(p.get("scales", {}))}


def get_population_stats(scale_key: str) -> dict:
    """Get population mean & sd for a scale. Returns None if unavailable."""
    data = _load()
    key = SCALE_KEY_MAP.get(scale_key)
    if not key:
        return None
    return (data.get("scales") or {}).get(key)


def _erf_approx(x: float) -> float:
    """Approximation of the error function."""
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * math.exp(-x * x)
    return sign * y


def _normal_cdf(x: float) -> float:
    """Cumulative distribution function for standard normal."""
    return 0.5 * (1.0 + _erf_approx(x / math.sqrt(2)))


def compute_percentile(user_score: float, mean: float, sd: float) -> float:
    """Compute the percentile of user_score in a normal distribution N(mean, sd)."""
    if sd == 0:
        return 50.0
    z = (user_score - mean) / sd
    return round(_normal_cdf(z) * 100, 1)


def _empirical_percentile(user_score: float, pop: dict):
    """在有经验分位数时给出更稳健的百分位（插值）；否则返回 None。"""
    q = (pop or {}).get("quantiles") or {}
    pts = []
    for pct in (5, 10, 25, 50, 75, 90, 95):
        v = q.get(f"q{pct:02d}")
        if v is not None:
            pts.append((float(v), pct))
    if len(pts) < 5:
        return None
    pts.sort()
    if user_score <= pts[0][0]:
        return float(max(0.5, pts[0][1]))
    if user_score >= pts[-1][0]:
        return float(min(99.5, pts[-1][1]))
    for (v0, p0), (v1, p1) in zip(pts, pts[1:]):
        if v0 <= user_score <= v1:
            if v1 == v0:
                return float(p0)
            return round(p0 + (p1 - p0) * (user_score - v0) / (v1 - v0), 1)
    return None


def percentile_report(user_score: float, scale_key: str) -> dict:
    """返回 {percentile, z, source: 'empirical'|'normal'|None}。"""
    pop = get_population_stats(scale_key)
    if not pop or pop.get("sd", 0) <= 0:
        return {"percentile": None, "z": None, "source": None}
    mean, sd = float(pop["mean"]), float(pop["sd"])
    emp = _empirical_percentile(user_score, pop)
    z = round((user_score - mean) / sd, 2)
    if emp is not None:
        return {"percentile": emp, "z": z, "source": "empirical"}
    return {"percentile": compute_percentile(user_score, mean, sd), "z": z, "source": "normal"}


def make_bell_svg(user_score: float, scale_name: str, scale_key: str = "",
                  width: int = 340, height: int = 140) -> str:
    """
    Generate an inline SVG showing:
      - Normal distribution bell curve
      - User's score marked on the curve
      - Percentile text
    Returns empty string if no population data available.
    """
    pop = get_population_stats(scale_key) if scale_key else None
    if not pop:
        return ""

    mean = float(pop["mean"])
    sd = float(pop["sd"])
    if sd <= 0:
        return ""

    rep = percentile_report(user_score, scale_key)
    pct = rep["percentile"]

    # Chart geometry
    pad_left, pad_right = 40, 20
    pad_top, pad_bottom = 10, 28
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom
    center_x = pad_left + chart_w / 2
    zero_y = pad_top + chart_h

    # X range: 人群 ±3.5sd，并与量表量程求交（量程缺失时不裁剪）
    rng = pop.get("range") or [None, None]
    lo_r, hi_r = (rng + [None, None])[:2] if isinstance(rng, list) else (None, None)
    x_min = mean - 3.5 * sd
    x_max = mean + 3.5 * sd
    if lo_r is not None:
        x_min = max(float(lo_r), x_min)
    if hi_r is not None:
        x_max = min(float(hi_r), x_max)
    if x_max <= x_min:                      # 极端退化保护
        x_min, x_max = mean - 3.5 * sd, mean + 3.5 * sd

    def x_to_svg(val):
        return pad_left + (val - x_min) / (x_max - x_min) * chart_w if x_max > x_min else center_x

    def y_to_svg(density):
        return zero_y - density * chart_h

    # Generate bell curve path
    n_points = 80
    max_density = 1.0 / (sd * math.sqrt(2 * math.pi))
    points = []
    for i in range(n_points + 1):
        x = x_min + (x_max - x_min) * i / n_points
        z = (x - mean) / sd
        density = math.exp(-0.5 * z * z) / (sd * math.sqrt(2 * math.pi))
        density_norm = density / max_density  # normalize to 0-1
        sx = x_to_svg(x)
        sy = y_to_svg(density_norm)
        points.append(f"{sx:.1f},{sy:.1f}")

    path_d = "M" + " L".join(points)
    area_d = path_d + f" L{x_to_svg(x_max):.1f},{zero_y:.1f} L{x_to_svg(x_min):.1f},{zero_y:.1f} Z"

    # User marker
    user_x = x_to_svg(user_score)
    # clamp user_x within chart
    user_x = max(pad_left, min(pad_left + chart_w, user_x))
    z_user = (user_score - mean) / sd
    user_density = math.exp(-0.5 * z_user * z_user) / (sd * math.sqrt(2 * math.pi)) / max_density
    user_y = y_to_svg(user_density)

    # Label: show score and percentile
    pct_color = "#28a745" if 25 <= pct <= 75 else ("#ffc107" if 10 <= pct <= 90 else "#dc3545")
    pct_label = f"高于 {pct}% 同龄人" if pct >= 50 else f"低于 {100-pct}% 同龄人" if pct <= 50 else f"处于 {pct}% 位置"
    src_note = "经验分位" if rep.get("source") == "empirical" else "正态近似"

    return f'''
    <div style="margin-top:8px;">
        <svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"
             xmlns="http://www.w3.org/2000/svg" style="display:block;margin:0 auto;">
            <!-- Background -->
            <rect x="0" y="0" width="{width}" height="{height}" fill="#fafbff" rx="8"/>

            <!-- Bell curve area fill -->
            <path d="{area_d}" fill="rgba(102,126,234,0.12)" stroke="none"/>

            <!-- Bell curve line -->
            <path d="{path_d}" fill="none" stroke="#667eea" stroke-width="1.8" stroke-linecap="round"/>

            <!-- Baseline -->
            <line x1="{pad_left}" y1="{zero_y}" x2="{pad_left+chart_w}" y2="{zero_y}"
                  stroke="#ddd" stroke-width="0.8"/>

            <!-- Mean line -->
            <line x1="{x_to_svg(mean):.1f}" y1="{pad_top}" x2="{x_to_svg(mean):.1f}" y2="{zero_y}"
                  stroke="#aaa" stroke-width="0.6" stroke-dasharray="3,3"/>

            <!-- User marker: dot + vertical line -->
            <line x1="{user_x:.1f}" y1="{pad_top+2}" x2="{user_x:.1f}" y2="{zero_y}"
                  stroke="#e74c3c" stroke-width="1.5" stroke-dasharray="4,2"/>
            <circle cx="{user_x:.1f}" cy="{user_y:.1f}" r="5" fill="#e74c3c" stroke="white" stroke-width="2"/>

            <!-- Score label under marker -->
            <text x="{user_x:.1f}" y="{height-6}" text-anchor="middle"
                  font-size="10" fill="#333" font-weight="600">{user_score}</text>

            <!-- Mean label -->
            <text x="{x_to_svg(mean):.1f}" y="{height-6}" text-anchor="middle"
                  font-size="9" fill="#aaa">均值{mean}</text>
        </svg>
        <div style="text-align:center;font-size:12px;color:{pct_color};margin-top:2px;font-weight:500;">
            📊 {pct_label} ｜ Z={z_user:+.1f}
        </div>
        <div style="text-align:center;font-size:10px;color:#aaa;margin-top:1px;">
            人群基线 n={pop.get("n", "?")}（{src_note}）
        </div>
    </div>
    '''


def make_debq_sub_chart(user_mean: float, sub_key: str, sub_name: str,
                         width: int = 300, height: int = 100) -> str:
    """Generate a mini SVG chart for a DEBQ subscale."""
    pop = get_population_stats(sub_key)
    if not pop:
        return f'<span style="font-size:12px;color:#888;">(基线数据不足，无法生成对比图)</span>'

    mean = float(pop["mean"])
    sd = float(pop["sd"])
    if sd <= 0:
        return f'<span style="font-size:12px;color:#888;">均分: {user_mean:.2f}</span>'

    pct = percentile_report(user_mean, sub_key)["percentile"]

    # Mini chart
    pad_l, pad_r = 30, 15
    pad_t, pad_b = 5, 16
    cw = width - pad_l - pad_r
    ch = height - pad_t - pad_b
    cx = pad_l + cw / 2
    zy = pad_t + ch

    rng = pop.get("range") or [1, 5]
    lo_r = float(rng[0]) if rng[0] is not None else 1.0
    hi_r = float(rng[1]) if rng[1] is not None else 5.0
    x_min = max(lo_r, mean - 3.5 * sd)
    x_max = min(hi_r, mean + 3.5 * sd)
    if x_max <= x_min:
        x_min, x_max = lo_r, hi_r

    def xs(v): return pad_l + (v - x_min) / (x_max - x_min) * cw if x_max > x_min else cx

    max_d = 1.0 / (sd * math.sqrt(2 * math.pi))
    pts = []
    for i in range(50):
        x = x_min + (x_max - x_min) * i / 49
        z = (x - mean) / sd
        d = math.exp(-0.5 * z * z) / (sd * math.sqrt(2 * math.pi)) / max_d
        pts.append(f"{xs(x):.1f},{zy-d*ch:.1f}")
    path_d = "M" + " L".join(pts)
    area_d = path_d + f" L{xs(x_max):.1f},{zy:.1f} L{xs(x_min):.1f},{zy:.1f} Z"

    ux = xs(user_mean)
    uz = (user_mean - mean) / sd
    uy = zy - math.exp(-0.5 * uz * uz) / (sd * math.sqrt(2 * math.pi)) / max_d * ch

    pct_color = "#28a745" if 25 <= pct <= 75 else ("#ffc107" if 10 <= pct <= 90 else "#dc3545")

    return f'''
    <div style="margin:4px 0;">
        <svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"
             style="display:block;margin:0 auto;">
            <rect width="{width}" height="{height}" fill="#fafbff" rx="6"/>
            <path d="{area_d}" fill="rgba(102,126,234,0.08)"/>
            <path d="{path_d}" fill="none" stroke="#667eea" stroke-width="1.5"/>
            <line x1="{pad_l}" y1="{zy}" x2="{pad_l+cw}" y2="{zy}" stroke="#ddd" stroke-width="0.6"/>
            <line x1="{xs(user_mean):.1f}" y1="{pad_t}" x2="{xs(user_mean):.1f}" y2="{zy}"
                  stroke="#e74c3c" stroke-width="1.2" stroke-dasharray="3,2"/>
            <circle cx="{ux:.1f}" cy="{uy:.1f}" r="4" fill="#e74c3c" stroke="white" stroke-width="1.5"/>
            <text x="{xs(user_mean):.1f}" y="{height-3}" text-anchor="middle" font-size="9" fill="#333">{user_mean:.2f}</text>
            <text x="{xs(mean):.1f}" y="{height-3}" text-anchor="middle" font-size="8" fill="#bbb">μ{mean}</text>
        </svg>
        <div style="text-align:center;font-size:11px;color:{pct_color};margin-top:1px;">
            {sub_name} P{pct}%
        </div>
    </div>
    '''


# ── DEBQ 2D Distribution Chart ─────────────────────────────────────────

def make_debq_2d_svg(user_emotional, user_external, user_restrained,
                      width=380, height=320):
    """
    DEBQ 解除抑制-抑制 二维分布图。
    X轴 = 抑制（限制性饮食）；Y轴 = 解除抑制（(情绪性+外部性)/2）。
    人群分布由**内置聚合统计**（各子量表 mean/sd）导出二元正态密度，
    不再硬编码任何个体会话点。
    """
    s_emo = get_population_stats("debq_emotional")
    s_ext = get_population_stats("debq_external")
    s_res = get_population_stats("debq_restrained")
    if not (s_emo and s_ext and s_res):
        return ""

    # 人群参数：解除抑制 = (emo + ext)/2（独立近似），抑制 = restrained
    y_mean = (float(s_emo["mean"]) + float(s_ext["mean"])) / 2.0
    y_sd = 0.5 * math.sqrt(float(s_emo["sd"]) ** 2 + float(s_ext["sd"]) ** 2) or 0.5
    x_mean = float(s_res["mean"])
    x_sd = float(s_res["sd"]) or 0.5
    n_pop = s_res.get("n", 0)

    user_y = (user_emotional + user_external) / 2  # 解除抑制
    user_x = user_restrained                      # 抑制

    # Chart geometry
    pad = 45
    pad_b = 35
    pad_r = 20
    chart_w = width - pad - pad_r
    chart_h = height - pad - pad_b

    x_min, x_max = 1.0, 5.0
    y_min, y_max = 1.0, 5.0

    def xs(v): return pad + (v - x_min) / (x_max - x_min) * chart_w
    def ys(v): return pad + chart_h - (v - y_min) / (y_max - y_min) * chart_h

    # 二元正态密度等高线（环状点云，替代硬编码核）
    grid_size = 60
    density = [[0.0] * grid_size for _ in range(grid_size)]
    max_d = 0.0
    rho = 0.0
    for i in range(grid_size):
        gx = x_min + (x_max - x_min) * i / (grid_size - 1)
        for j in range(grid_size):
            gy = y_min + (y_max - y_min) * j / (grid_size - 1)
            zx = (gx - x_mean) / x_sd
            zy_ = (gy - y_mean) / y_sd
            d = math.exp(-(zx * zx - 2 * rho * zx * zy_ + zy_ * zy_) /
                         (2 * (1 - rho * rho)))
            density[j][i] = d
            max_d = max(max_d, d)

    circles = []
    if max_d > 0:
        for i in range(grid_size):
            for j in range(grid_size):
                a = density[j][i] / max_d
                if a > 0.05:
                    gx = x_min + (x_max - x_min) * i / (grid_size - 1)
                    gy = y_min + (y_max - y_min) * j / (grid_size - 1)
                    circles.append(
                        f'<circle cx="{xs(gx):.1f}" cy="{ys(gy):.1f}" r="{3.5 * a ** 0.5:.1f}" '
                        f'fill="#667eea" opacity="{a * 0.45:.2f}"/>'
                    )

    ux = max(pad, min(pad + chart_w, xs(user_x)))
    uy = max(pad, min(pad + chart_h, ys(user_y)))

    quadrants = [
        f'<text x="{pad+chart_w*0.25:.0f}" y="{pad+15}" text-anchor="middle" font-size="10" fill="#aaa">低抑制·高解除</text>',
        f'<text x="{pad+chart_w*0.75:.0f}" y="{pad+15}" text-anchor="middle" font-size="10" fill="#aaa">高抑制·高解除</text>',
        f'<text x="{pad+chart_w*0.25:.0f}" y="{pad+chart_h-5}" text-anchor="middle" font-size="10" fill="#aaa">低抑制·低解除</text>',
        f'<text x="{pad+chart_w*0.75:.0f}" y="{pad+chart_h-5}" text-anchor="middle" font-size="10" fill="#aaa">高抑制·低解除</text>',
    ]

    return f'''
    <div style="margin:8px 0;">
        <svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"
             xmlns="http://www.w3.org/2000/svg" style="display:block;margin:0 auto;">
            <rect width="{width}" height="{height}" fill="#fafbff" rx="8"/>

            <!-- 人群二元正态密度 -->
            {"".join(circles)}

            <!-- 人群中心 -->
            <circle cx="{xs(x_mean):.1f}" cy="{ys(y_mean):.1f}" r="4" fill="#667eea" opacity="0.85"/>

            <!-- 用户点 -->
            <circle cx="{ux:.1f}" cy="{uy:.1f}" r="7" fill="#e74c3c" stroke="white" stroke-width="2.5"/>
            <text x="{ux:.1f}" y="{uy-10:.1f}" text-anchor="middle" font-size="11" fill="#e74c3c" font-weight="700">You</text>

            <!-- 坐标轴 -->
            <line x1="{pad}" y1="{pad}" x2="{pad}" y2="{pad+chart_h}" stroke="#ccc" stroke-width="1"/>
            <line x1="{pad}" y1="{pad+chart_h}" x2="{pad+chart_w}" y2="{pad+chart_h}" stroke="#ccc" stroke-width="1"/>

            <!-- 轴标题 -->
            <text x="{width/2:.0f}" y="{height-6}" text-anchor="middle" font-size="10" fill="#888">抑制（限制性饮食）→</text>
            <text x="12" y="{height/2:.0f}" text-anchor="middle" font-size="10" fill="#888" transform="rotate(-90,12,{height/2:.0f})">← 解除抑制（情绪+外部/2）</text>

            <!-- 刻度 -->
            {"".join(f'<text x="{xs(t):.0f}" y="{pad+chart_h+14}" text-anchor="middle" font-size="8" fill="#bbb">{t}</text>' for t in range(1,6))}
            {"".join(f'<text x="{pad-8}" y="{ys(t)+4:.0f}" text-anchor="end" font-size="8" fill="#bbb">{t}</text>' for t in range(1,6))}

            <!-- 象限分割 -->
            <line x1="{xs(3):.0f}" y1="{pad}" x2="{xs(3):.0f}" y2="{pad+chart_h}" stroke="#eee" stroke-width="0.5" stroke-dasharray="4,4"/>
            <line x1="{pad}" y1="{ys(3):.0f}" x2="{pad+chart_w}" y2="{ys(3):.0f}" stroke="#eee" stroke-width="0.5" stroke-dasharray="4,4"/>

            {"".join(quadrants)}
        </svg>
        <div style="text-align:center;font-size:12px;color:#888;margin-top:4px;">
            解除抑制={user_y:.2f} ｜ 抑制={user_x:.2f} ｜ 人群 n={n_pop}
        </div>
    </div>
    '''


# ── Test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("基线来源:", population_meta())
    svg = make_bell_svg(10, "GAD-7", "GAD-7")
    print(svg[:300])
    print(f"\nGAD-7 10 分 -> {percentile_report(10, 'GAD-7')}")
    print(f"PSS-14 30 分 -> {percentile_report(30, 'PSS-14')}")
    print(f"IPAQ-S 3000 -> {percentile_report(3000, 'IPAQ-S')}")
    print("DEBQ 2D:", "ok" if make_debq_2d_svg(2.5, 3.1, 2.4) else "EMPTY")
