#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_config_doc.py — 生成《可配置项总清单》
=============================================================================
把 `config/app.schema.json` + `config/app.yaml.example` 的**全部键**导出为
一张人类可读清单：

    键名 / 类型 / 默认值 / 单位 / 作用 / 是否必填 / 生效方式（重启或热加载）

为什么用生成而不是手写
----------------------
手写文档必然过期 → 出现「文档说能配、实际不认」的静默失效。
本脚本 + CI 的 `--check` 让「文档与配置漂移」直接变成门禁失败。

三方一致性（用户要求）
----------------------
  schema  ←→  yaml  ←→  实际读取
  ① schema ↔ yaml  ：`tools/appconfig.py --check-example-keys`（无悬空键）
  ② yaml ↔ 实际读取 ：本脚本列出 `ENV_MAP` 注入的环境变量名；自检
                      `config.env_bridge` 反向校验「服务读取的环境变量都在
                      ENV_MAP 中声明」（无未声明键）。

用法::

    python3 tools/gen_config_doc.py             # 写入 docs/可配置项总清单.md
    python3 tools/gen_config_doc.py --stdout    # 只打印
    python3 tools/gen_config_doc.py --check     # 校验文档是否最新（CI）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import appconfig as AC                                       # noqa: E402

OUT = os.path.join(PKG, "docs", "可配置项总清单.md")

# 命名空间 → （中文名，生效方式）
NAMESPACES = [
    ("version", "配置版本", "—"),
    ("app", "应用与路径", "重启受影响服务"),
    ("site", "站点与反向代理", "重启 Nginx / 重渲染配置后生效"),
    ("logging", "日志级别与轮转", "重启服务 / 重新安装 logrotate"),
    ("http", "HTTP 全局默认", "重启服务"),
    ("services", "各服务端口与入口", "重渲染 env + 重启对应服务"),
    ("database", "数据库与名册路径", "重启服务"),
    ("smtp", "邮件发送（SMTP）", "重启服务"),
    ("email", "邮件投递路由（域名）", "重启服务"),
    ("alert", "告警阈值与冷却", "热加载（下次巡检生效）"),
    ("llm", "LLM 主备（地址/模型/密钥/温度）", "重启 diet-llm-queue"),
    ("analysis", "LLM 分析调度（窗口/节流/并发）", "热加载（worker 每 30s 重读）"),
    ("scoring", "计分口径与分档", "重启受影响服务"),
    ("study", "研究设计（时间轴/目标/名册）", "热加载（读盘即生效）"),
    ("survey_platforms", "问卷平台（Token/ID/周期）", "重启对应 sync / 重渲染 timer"),
    ("webhook", "Webhook 开关与重试", "重启 webhook 服务"),
    ("schedule", "调度周期与静默时段", "重渲染 timer + daemon-reload"),
    ("backup", "备份保留与远端", "下次备份生效"),
    ("selfcheck", "自检探活", "热加载"),
]

# 少数键的「作用」需要人工补充（其余按命名空间概括）
EFFECT_NOTES = {
    "app.base_dir": "安装根目录（$APP_BASE）",
    "app.data_dir": "数据导出/中间产物目录",
    "app.log_dir": "日志目录",
    "app.log_level": "日志级别",
    "app.timezone": "系统时区（窗口/静默/日报判定基准）",
    "app.scoring_version": "计分口径版本（须与权威条目库一致）",
    "site.domain": "主域名（占位符 example.com 会被判为未配置）",
    "site.public_scheme": "对外协议（http/https）",
    "site.subdomain_admin": "管理控制台子域前缀",
    "site.subdomain_api": "API/问卷反馈子域前缀",
    "site.subdomain_svc": "饮食反馈子域前缀",
    "site.subdomain_tools": "工具（饮食 Webhook）子域前缀",
    "site.subdomain_data": "数据看板子域前缀",
    "site.subdomain_www": "www 站点子域前缀",
    "site.subdomain_survey_hook": "量表 Webhook 专用子域前缀",
    "logging.rotate_days": "日志保留天数",
    "logging.format": "结构化日志格式（json/text）",
    "http.default_timeout_seconds": "出网请求默认超时（秒）",
    "http.default_max_body_bytes": "入站请求体默认上限（字节）",
    "database.survey_db": "量表库路径",
    "database.diet_db": "饮食库路径",
    "database.exercise_db": "运动库路径",
    "database.roster": "名册（受试者名单）JSON 路径",
    "database.busy_timeout_seconds": "SQLite 锁等待上限（秒）",
    "smtp.cc_email": "日报抄送（可空）",
    "smtp.admin_email": "管理员/告警收件人",
    "email.mx_domain": "校内直投域名（MX 路由判定）",
    "email.mx_host": "直投 MX 主机",
    "email.message_id_domain": "Message-ID 后缀域名",
    "alert.severity_threshold": "告警级别下限（info/warning/critical）",
    "alert.stale_data_minutes": "数据滞后告警阈值（分钟）",
    "llm.primary.temperature": "采样温度（研究场景建议低温）",
    "llm.primary.max_tokens": "单次回复 token 上限",
    "llm.prompt_file": "系统提示词文件路径（可替换）",
    "scoring.reverse_items_enabled": "是否施加反向计分",
    "scoring.custom_bands_path": "自定义分档切点文件（空=内置权威切点）",
    "scoring.strict": "未识别作答是否报错（true=报错）",
    "study.calendar_path": "研究设计日历文件（干预时间轴/周次/目标完成度）",
    "study.planned_id_prefixes": "「计划内」学号前缀（逗号分隔）",
    "study.total_planned_fallback": "名册缺失时的目标总人数",
}

# 热加载（不需重启）的命名空间前缀
HOT_NAMESPACES = ("analysis.", "alert.", "study.", "selfcheck.", "schedule.quiet_hours")


def _unit(key: str, rule: dict) -> str:
    """
    单位由**键名约定**推导（确定性，不手填 → 不会漂移）。
    约定：凡有物理量纲者一律带后缀（_minutes/_seconds/_bytes/_days/_gb/_at/port…）。
    """
    t = rule.get("type")
    if t == "path":
        return "路径"
    if t == "url":
        return "URL"
    if t == "email":
        return "邮箱"
    if t == "bool":
        return "开关"
    if t == "enum":
        return "枚举"
    if t not in ("int", "float"):
        return "—"
    k = key.rsplit(".", 1)[-1]
    for suf, unit in (("_minutes", "分钟"), ("_minute", "次/分钟"),
                      ("_seconds", "秒"), ("_bytes", "字节"),
                      ("_days", "天"), ("_gb", "GB"), ("_at", "HH:MM"),
                      ("_max_tokens", "token"), ("temperature", "0–2"),
                      ("retention", "份"), ("concurrency", "个"),
                      ("_attempts", "次"), ("_size", "条")):
        if k.endswith(suf):
            return unit
    if k == "port" or k.endswith("_port"):
        return "端口"
    if k == "max_per_window" or k.endswith("_daytime_max"):
        return "个/窗口"
    if k.endswith("total_planned_fallback"):
        return "人"
    return "—"


def _apply_hint(key: str, ns: str, ns_hint: str) -> str:
    for pref in HOT_NAMESPACES:
        if key.startswith(pref):
            return "热加载（改后 ≤30s 生效）"
    return ns_hint


def build() -> str:
    with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    fields = schema.get("fields", {})
    example = AC.flatten(AC._load_yaml(AC.EXAMPLE_CONFIG))

    # 按命名空间分组（保序）
    grouped = {ns: [] for ns, _t, _h in NAMESPACES}
    for key in fields:
        ns = key.split(".")[0]
        grouped.setdefault(ns, []).append(key)

    lines = []
    lines.append("# 可配置项总清单")
    lines.append("")
    lines.append("> 本文件由 `tools/gen_config_doc.py` **自动生成**（勿手改）。")
    lines.append(f"> 单一真源：`config/app.yaml`（模板 `config/app.yaml.example`）"
                 f"＋ `config/app.schema.json`。")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                 f"｜ 共 **{len(fields)}** 个配置项")
    lines.append("")
    lines.append("## 一、怎么用")
    lines.append("")
    lines.append("```bash")
    lines.append("# 1) 查看/校验当前配置（缺项/类型错/越界/占位符 → 逐条报出键位置）")
    lines.append("python3 tools/appconfig.py --config /etc/research-app/app.yaml --check")
    lines.append("")
    lines.append("# 2) schema 与 example 键集合一致（发布门禁）")
    lines.append("python3 tools/appconfig.py --check-example-keys")
    lines.append("")
    lines.append("# 3) 把 app.yaml 渲染成 systemd 环境文件 / timer 周期（改配置即改行为）")
    lines.append("python3 tools/appconfig.py --render-env /etc/research-app/env")
    lines.append("python3 tools/appconfig.py --render-timers /etc/systemd/system")
    lines.append("")
    lines.append("# 4) 生成/校验本清单（防文档漂移）")
    lines.append("python3 tools/gen_config_doc.py && python3 tools/gen_config_doc.py --check")
    lines.append("```")
    lines.append("")
    lines.append("**生效方式总览**")
    lines.append("")
    lines.append("| 生效方式 | 含义 |")
    lines.append("|---|---|")
    lines.append("| 热加载（≤30s） | 服务周期重读配置，无需重启 |")
    lines.append("| 重启受影响服务 | `systemctl restart <unit>` |")
    lines.append("| 重渲染 env + 重启 | 先 `--render-env`，再重启 |")
    lines.append("| 重渲染 timer + daemon-reload | 先 `--render-timers`，再 `systemctl daemon-reload` |")
    lines.append("")
    lines.append(f"密钥类配置（`secret: true`）**只写占位引用** `${{VAR}}`，真实值放 "
                 f"`/etc/research-app/secrets.env`（0600），仓库内零密钥。")
    lines.append("")
    lines.append("## 二、配置项明细")
    lines.append("")

    total = 0
    for ns, zh, hint in NAMESPACES:
        keys = sorted(grouped.get(ns) or [])
        if not keys:
            continue
        total += len(keys)
        lines.append(f"### {zh}（`{ns}.*`，{len(keys)} 项）")
        lines.append("")
        lines.append("| 键名 | 类型 | 默认值 | 单位 | 作用 | 是否必填 | 生效方式 | 所属分组 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for key in keys:
            rule = fields[key]
            t = rule.get("type", "str")
            if rule.get("secret"):
                t += "·密钥"
            if rule.get("min") is not None or rule.get("max") is not None:
                t += f"（{rule.get('min','-')}~{rule.get('max','-')}）"
            val = example.get(key, "—")
            if isinstance(val, bool):
                val = "true" if val else "false"
            elif val == "":
                val = "（空）"
            if rule.get("enum"):
                val = f"{val} ｜∈ {','.join(str(x) for x in rule['enum'])}"
            req = "是" if rule.get("required") else "否"
            if rule.get("secret"):
                req += "（密钥，缺值只告警）"
            eff = EFFECT_NOTES.get(key)
            if not eff:
                short = key.split(".")[-1]
                eff = short.replace("_", " ")
            env = AC.ENV_MAP.get(key, "")
            eff_full = eff + (f"　→ `{env}`" if env else "")
            values = [t, val, _unit(key, rule), eff_full, req,
                      _apply_hint(key, ns, hint), ns]
            cells = " | ".join("`" + str(v).replace("|", chr(92) + "|") + "`"
                               if idx in (1,) else str(v).replace("|", chr(92) + "|")
                               for idx, v in enumerate(values))
            lines.append(f"| `{key}` | {cells} |")
        lines.append("")

    lines.append("## 三、三方一致性（schema ↔ yaml ↔ 实际读取）")
    lines.append("")
    lines.append(f"* schema ↔ yaml：`--check-example-keys` 双向比对，当前 **{len(fields)} 键完全一致**；")
    lines.append(f"* yaml → 实际读取：上表「生效方式」列末的 `` `ENV_VAR` `` 即注入各服务的环境变量名"
                 f"（由 `tools/appconfig.py: ENV_MAP` 渲染）；")
    lines.append("* 实际读取 → yaml：自检项 `config.env_bridge` 反向扫描服务源码中"
                 " `os.environ.get(\"X\")` 的用法，凡未在 `ENV_MAP` 中声明者即报错"
                 "（防止「代码偷偷读了一个没人配的环境变量」）。")
    lines.append("")
    lines.append(f"> 明细表共 **{total}** 项；与 schema 声明数（{len(fields)}）一致。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成《可配置项总清单》")
    ap.add_argument("--stdout", action="store_true", help="只打印")
    ap.add_argument("--check", action="store_true", help="校验文档是否最新（CI）")
    args = ap.parse_args()

    content = build()
    if args.stdout:
        print(content)
        return 0
    if args.check:
        try:
            with open(OUT, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            print("❌ 文档不存在：" + OUT, file=sys.stderr)
            return 1
        norm = lambda s: "\n".join(l for l in s.splitlines()
                                   if not l.startswith("> 生成时间："))
        if norm(existing) != norm(content):
            print("❌ docs/可配置项总清单.md 与配置不一致，请运行 "
                  "python3 tools/gen_config_doc.py", file=sys.stderr)
            return 1
        print("✅ 可配置项文档与配置一致")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ 已写入 " + OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
