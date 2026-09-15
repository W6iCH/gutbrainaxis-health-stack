#!/usr/bin/env bash
# ============================================================================
#  自检脚本 — 验证安装完整性
#  输出 ✅/❌ 清单，退出码：0=全部通过，1=有失败项
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'
NC='\033[0m'
ok()   { echo -e "  ${GREEN}✅${NC} $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}❌${NC} $1"; FAIL=$((FAIL+1)); }
warn() { echo -e "  ${YELLOW}⚠️${NC} $1"; SKIP=$((SKIP+1)); }

INSTALL_ROOT="${INSTALL_ROOT:-/opt/gutbrainaxis}"
ENV_FILE="${ENV_FILE:-/etc/research-app/env}"
PASS=0; FAIL=0; SKIP=0

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   自检 — 个性化饮食干预课题                      ║"
echo "║   INSTALL_ROOT=${INSTALL_ROOT}       ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""

# ── 文件完整性 ───────────────────────────────────────────────────────────
echo "━━━ 文件完整性 ━━━"
for dir in sjtu_survey_pro diet_survey exercise_survey admin_console data_dashboard; do
  if [ -d "$INSTALL_ROOT/$dir" ]; then
    ok "目录存在: $dir/"
  else
    fail "目录缺失: $dir/"
  fi
done

for svc_entry in \
  "sjtu_survey_pro/feedback_server.py" \
  "diet_survey/webhook_listener.py" \
  "diet_survey/diet_feedback_server.py" \
  "diet_survey/diet_llm_queue.py" \
  "exercise_survey/exercise_sync_cron.py" \
  "admin_console/admin_console.py" \
  "data_dashboard/app.py"; do
  if [ -f "$INSTALL_ROOT/$svc_entry" ]; then
    ok "入口文件存在: $svc_entry"
  else
    fail "入口文件缺失: $svc_entry"
  fi
done

# ── venv 检查 ─────────────────────────────────────────────────────────────
echo ""
echo "━━━ Python 虚拟环境 ━━━"
VENV_PYTHON="$INSTALL_ROOT/venv/bin/python3"
if [ -f "$VENV_PYTHON" ] && [ -x "$VENV_PYTHON" ]; then
  VENV_VER=$("$VENV_PYTHON" --version 2>&1)
  ok "venv 正常 ($VENV_VER)"
else
  fail "venv 未创建或缺少 python3"
fi

# ── 配置文件 ─────────────────────────────────────────────────────────────
echo ""
echo "━━━ 配置文件 ━━━"
if [ -f "$ENV_FILE" ]; then
  ok "env 配置文件存在: $ENV_FILE"
  # 检查未替换占位符
  if grep -q '__CHANGE_ME__\|__REPLACE_\|<[A-Z_]*>' "$ENV_FILE" 2>/dev/null; then
    warn "env 配置中包含未替换的占位符，请填写真实值"
  else
    ok "env 配置无占位符残留"
  fi
  perms=$(stat -c "%a" "$ENV_FILE" 2>/dev/null || stat -f "%OLp" "$ENV_FILE" 2>/dev/null)
  if [ "$perms" = "600" ] || [ "$perms" = "400" ]; then
    ok "env 文件权限正确 ($perms)"
  else
    warn "env 文件权限建议 600 (当前: $perms)"
  fi
else
  fail "env 配置文件不存在"
fi

RCLONE_CONF="/root/.config/rclone/rclone.conf"
if [ -f "$RCLONE_CONF" ]; then
  ok "rclone 配置存在"
  if grep -q '__REPLACE_' "$RCLONE_CONF" 2>/dev/null; then
    warn "rclone 配置含未替换的占位符"
  fi
fi

# ── systemd ───────────────────────────────────────────────────────────────
echo ""
echo "━━━ systemd 服务状态 ━━━"
SERVICES=(
  "research-survey-feedback"
  "research-diet-feedback"
  "research-diet-webhook"
  "research-diet-llm-queue"
  "research-exercise-sync"
  "research-admin-console"
  "research-data-dashboard"
)
for svc in "${SERVICES[@]}"; do
  if systemctl is-enabled "$svc" > /dev/null 2>&1; then
    if systemctl is-active "$svc" > /dev/null 2>&1; then
      ok "systemd $svc: 已启用 ✅ 运行中"
    else
      warn "systemd $svc: 已启用 ❌ 未运行"
    fi
  else
    if [ -f "/etc/systemd/system/${svc}.service" ] || [ -f "/etc/systemd/system/${svc}.timer" ]; then
      warn "systemd $svc: 单元存在但未启用"
    else
      warn "systemd $svc: 单元不存在（可能定时服务只有 timer）"
    fi
  fi
done

# ── 端口监听 ─────────────────────────────────────────────────────────────
echo ""
echo "━━━ 端口监听 ━━━"
PORTS=(8000 8001 9000 9876)
for port in "${PORTS[@]}"; do
  if ss -tlnp "sport = :$port" 2>/dev/null | grep -q ":$port" || \
     lsof -i:"$port" 2>/dev/null | grep -q LISTEN; then
    ok "端口 :$port 已在监听"
  else
    fail "端口 :$port 未监听"
  fi
done

# ── HTTP 探活 ─────────────────────────────────────────────────────────────
echo ""
echo "━━━ HTTP 探活 ━━━"
ENDPOINTS=(
  "8000:问卷反馈"
  "8001:饮食反馈"
  "9000:管理控制台"
  "9876:Webhook"
)
for ep in "${ENDPOINTS[@]}"; do
  port="${ep%%:*}"
  name="${ep##*:}"
  if curl -sf "http://localhost:${port}/health" > /dev/null 2>&1 || \
     curl -sf "http://localhost:${port}/ping" > /dev/null 2>&1 || \
     curl -so /dev/null -w "%{http_code}" "http://localhost:${port}/" 2>/dev/null | grep -q .; then
    ok "HTTP :$port ($name) 响应正常"
  else
    fail "HTTP :$port ($name) 无响应"
  fi
done

# ── 数据库 ───────────────────────────────────────────────────────────────
echo ""
echo "━━━ 数据库 ━━━"
DB_FILES=(
  "sjtu_survey_pro/survey_data.db:问卷数据库"
  "diet_survey/diet_data.db:饮食数据库"
)
for entry in "${DB_FILES[@]}"; do
  relpath="${entry%%:*}"
  label="${entry##*:}"
  dbpath="$INSTALL_ROOT/$relpath"
  if [ -f "$dbpath" ]; then
    ok "$label 存在: $relpath"
    if python3 -c "
import sqlite3
c = sqlite3.connect('$dbpath')
c.execute('SELECT 1 FROM submissions LIMIT 1')
c.close()
print('OK', end='')
" 2>/dev/null | grep -q OK; then
      ok "$label 可读写"
    else
      fail "$label 不可读写（可能是空数据库）"
    fi
  else
    warn "$label 不存在（首次安装时由 init_db.py 创建）"
  fi
done

# ── Nginx ─────────────────────────────────────────────────────────────────
echo ""
echo "━━━ Nginx ─━━"
if command -v nginx > /dev/null 2>&1; then
  if nginx -t 2>&1; then
    ok "Nginx 配置语法正确"
  else
    fail "Nginx 配置语法有误"
  fi
  if systemctl is-active nginx > /dev/null 2>&1; then
    ok "Nginx 运行中"
  else
    warn "Nginx 未运行"
  fi
fi

# ── 配置完整性：检查未替换占位符 ──────────────────────────────────────
echo ""
echo "━━━ 配置完整性 ━━━"
for f in "$INSTALL_ROOT" "$ENV_FILE" "$RCLONE_CONF"; do
  [ -f "$f" ] || continue
  if grep -q '__CHANGE_ME__\|__REPLACE_\|<[A-Z_]*>' "$f" 2>/dev/null; then
    warn "$f 含有未替换占位符"
  fi
done

# ── 权限 ─────────────────────────────────────────────────────────────────
echo ""
echo "━━━ 权限 ━━━"
PRIVATE_FILES=(
  "$ENV_FILE"
  "$RCLONE_CONF"
)
for f in "${PRIVATE_FILES[@]}"; do
  [ -f "$f" ] || continue
  perms=$(stat -c "%a" "$f" 2>/dev/null || stat -f "%OLp" "$f" 2>/dev/null)
  if [ "$perms" = "600" ] || [ "$perms" = "400" ] || [ "$perms" = "0" ]; then
    ok "$f 权限正确 ($perms)"
  else
    warn "$f 权限建议 600 (当前: $perms)"
  fi
done

# ── 汇总 ─────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   检查通过: ${GREEN}${PASS}${NC}  |  失败: ${RED}${FAIL}${NC}  |  跳过/警告: ${YELLOW}${SKIP}${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo -e "${RED}❌ 自检未通过，请修复上述失败项${NC}"
  exit 1
else
  echo -e "${GREEN}✅ 全部检查通过${NC}"
  exit 0
fi
