#!/usr/bin/env bash
# ============================================================================
#  自检入口 — 个性化饮食干预课题 服务器端研究系统
# ============================================================================
#  ⚠️ 本脚本自 2026-09-16 起**不再自己实现探活逻辑**，而是调用统一自检引擎
#     `tools/selfcheck.py`（与管理控制台「自检」页共用同一后端），
#     以保证「安装后自检」「控制台页面」「CI」三者结果一致、口径单一。
#
#  历史问题（已修）：
#    · 旧脚本检查入口文件 `admin_console/admin_console.py` —— 该文件并不存在，
#      实际入口是 `admin_console/app.py`，因此该项**永远报缺失**。
#    · 端口清单漏了 8090（数据看板）。
#    · 直接依赖 ss/lsof/stat -c，在 macOS 或最小化系统上误报。
#
#  用法：
#     sudo bash verify.sh                       # 全部检查
#     sudo bash verify.sh --no-external         # 不探测外网（离线环境）
#     sudo bash verify.sh --json                # 机器可读
#     sudo bash verify.sh --only config,paths   # 只跑指定组
#
#  退出码：0=全部通过；1=存在失败项；2=无法运行自检
# ============================================================================
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'

INSTALL_ROOT="${INSTALL_ROOT:-/opt/gutbrainaxis}"
ENV_DIR="${ENV_DIR:-/etc/research-app}"
CONFIG_FILE="${CONFIG_FILE:-$ENV_DIR/app.yaml}"

# 定位 Python：优先 venv，其次系统 python3
PY=""
for cand in "$INSTALL_ROOT/venv/bin/python3" "$(command -v python3 2>/dev/null || true)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo -e "${RED}❌ 找不到可用的 python3${NC}" >&2
  exit 2
fi

# 定位 selfcheck.py：安装根 → 包内
SELFCHECK=""
for cand in "$INSTALL_ROOT/tools/selfcheck.py" "$(cd "$(dirname "$0")" && pwd)/tools/selfcheck.py"; do
  if [ -f "$cand" ]; then SELFCHECK="$cand"; break; fi
done

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   自检 — 个性化饮食干预课题                      ║"
echo "║   INSTALL_ROOT=${INSTALL_ROOT}"
echo "║   配置=${CONFIG_FILE}"
echo "╚═══════════════════════════════════════════════╝"

if [ -z "$SELFCHECK" ]; then
  echo -e "${YELLOW}⚠️  未找到 tools/selfcheck.py，回退到最小化 shell 检查${NC}"
  FAIL=0
  for dir in sjtu_survey_pro diet_survey exercise_survey admin_console data_dashboard; do
    if [ -d "$INSTALL_ROOT/$dir" ]; then echo -e "  ${GREEN}✅${NC} 目录 $dir/"; else echo -e "  ${RED}❌${NC} 缺失 $dir/"; FAIL=$((FAIL+1)); fi
  done
  # 入口文件（修正：admin_console 的入口是 app.py）
  for f in \
    sjtu_survey_pro/feedback_server.py \
    diet_survey/webhook_listener.py \
    diet_survey/diet_feedback_server.py \
    diet_survey/diet_llm_queue.py \
    exercise_survey/exercise_sync_cron.py \
    admin_console/app.py \
    data_dashboard/app.py \
    microbiome/import_microbiome.py; do
    if [ -f "$INSTALL_ROOT/$f" ]; then echo -e "  ${GREEN}✅${NC} 入口 $f"; else echo -e "  ${RED}❌${NC} 缺失 $f"; FAIL=$((FAIL+1)); fi
  done
  echo ""
  echo -e "${YELLOW}建议安装完整包以启用统一自检（tools/selfcheck.py）${NC}"
  exit $([ "$FAIL" -gt 0 ] && echo 1 || echo 0)
fi

echo -e "使用自检引擎: $SELFCHECK"
echo ""

# 透传参数；--config 默认指向统一配置
HAS_CONFIG=0
for a in "$@"; do [ "$a" = "--config" ] && HAS_CONFIG=1; done
if [ "$HAS_CONFIG" = "1" ]; then
  "$PY" "$SELFCHECK" "$@"
else
  "$PY" "$SELFCHECK" --config "$CONFIG_FILE" "$@"
fi
rc=$?

echo ""
if [ "$rc" -eq 0 ]; then
  echo -e "${GREEN}✅ 自检全部通过${NC}"
else
  echo -e "${RED}❌ 自检存在失败项（退出码 $rc），请按每项的 💡 提示处置${NC}"
fi
exit "$rc"
