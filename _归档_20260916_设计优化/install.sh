#!/usr/bin/env bash
# ============================================================================
#  个性化饮食干预课题 — 服务端一键安装脚本
#  ⚠️ 需要 root 权限。幂等设计，可重复执行。
# ============================================================================
set -euo pipefail

# ── 颜色 ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}==>${NC} $*"; }
err()  { echo -e "${RED}❌ $*${NC}" >&2; }
die()  { err "$1"; echo -e "${YELLOW}💡 回滚提示：运行 uninstall.sh 可移除已安装组件${NC}" >&2; exit 1; }

# ── 默认变量 ──────────────────────────────────────────────────────────────
DRY_RUN="${DRY_RUN:-false}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/gutbrainaxis}"
ENV_DIR="/etc/research-app"
SERVICE_USER="root"
DRY_RUN_PREFIX=""

# 在 exec 前插入 echo（dry-run 模式）
dry() {
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} $*"
  else
    eval "$@"
  fi
}

cp_dry() {
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} cp $*"
  else
    cp "$@"
  fi
}

mkdir_dry() {
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} mkdir -p $*"
  else
    mkdir -p "$@"
  fi
}

ln_dry() {
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} ln -sf $*"
  else
    ln -sf "$@"
  fi
}

chmod_dry() {
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} chmod $*"
  else
    chmod "$@"
  fi
}

chown_dry() {
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} chown $*"
  else
    chown "$@"
  fi
}

systemctl_dry() {
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} systemctl $*"
  else
    systemctl "$@"
  fi
}

# ── 参数解析 ──────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    --prefix=*) INSTALL_ROOT="${1#*=}"; shift ;;
    --prefix) INSTALL_ROOT="$2"; shift 2 ;;
    -h|--help)
      echo "用法: sudo bash install.sh [--dry-run] [--prefix=/opt/gutbrainaxis]"
      exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

# ── 0. 权限检查 ──────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" != "true" ]; then
  die "需要 root 权限。请使用 sudo bash install.sh 或切换到 root 用户执行。"
fi

# ── 1. 平台检测 ──────────────────────────────────────────────────────────
log "平台检测..."
OS_ID=""; OS_VERSION=""; ARCH=""
if [ -f /etc/os-release ]; then
  . /etc/os-release
  OS_ID="$ID"
  OS_VERSION="$VERSION_ID"
fi
ARCH="$(uname -m)"

if [ "$DRY_RUN" = "true" ]; then
  echo "  [DRY-RUN] 跳过平台检查（生产环境要求 Ubuntu 22.04/24.04, aarch64/x86_64）"
elif [ "$OS_ID" != "ubuntu" ]; then
  die "仅支持 Ubuntu 22.04 / 24.04，当前 OS: ${OS_ID:-无法识别}"
else
  case "$OS_VERSION" in
    22.04|24.04) ;;
    *) die "仅支持 Ubuntu 22.04 / 24.04，当前版本: $OS_VERSION" ;;
  esac
  case "$ARCH" in
    aarch64|x86_64) ;;
    *) die "仅支持 aarch64 和 x86_64，当前架构: $ARCH" ;;
  esac
  echo "  ✅ ${OS_ID} ${OS_VERSION} (${ARCH})"
fi

# ── 日志 ──────────────────────────────────────────────────────────────────
LOG_DIR="$INSTALL_ROOT/logs"
mkdir_dry -p "$LOG_DIR"
if [ "$DRY_RUN" != "true" ]; then
  mkdir -p "$LOG_DIR"
  exec > >(tee -a "$LOG_DIR/install.log") 2>&1
fi

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   个性化饮食干预课题 — 一键安装                ║"
echo "║   目标: ${INSTALL_ROOT}           ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""

# ── 2. 安装 apt 依赖 ────────────────────────────────────────────────────
log "安装系统依赖..."
PACKAGES="python3 python3-venv python3-pip nginx sqlite3 curl"
dry "DEBIAN_FRONTEND=noninteractive apt-get update -y"
dry "DEBIAN_FRONTEND=noninteractive apt-get install -y $PACKAGES"
echo "  ✅ 系统依赖已安装"

# ── 3. 创建 venv ─────────────────────────────────────────────────────────
log "创建 Python 虚拟环境..."
VENV_DIR="$INSTALL_ROOT/venv"
if [ ! -d "$VENV_DIR" ]; then
  dry "python3 -m venv $VENV_DIR"
  echo "  ✅ venv 已创建: $VENV_DIR"
else
  echo "  ✅ venv 已存在，跳过创建"
fi

# ── 4. 复制 services ────────────────────────────────────────────────────
log "复制服务代码..."
# 仓库根目录（install.sh 位于仓库根）
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES_SRC="$PACKAGE_DIR/services"
if [ -d "$SERVICES_SRC" ]; then
  dry "cp -r $SERVICES_SRC/* $INSTALL_ROOT/"
  echo "  ✅ 服务代码已复制"
else
  die "services/ 目录不存在 ($SERVICES_SRC)"
fi

# ── 5. 安装 pip 依赖 ────────────────────────────────────────────────────
log "安装 Python 依赖..."
PIP_REQUIREMENTS=""
for f in "$INSTALL_ROOT"/*/requirements.txt "$INSTALL_ROOT"/requirements.txt; do
  if [ -f "$f" ]; then
    PIP_REQUIREMENTS="$PIP_REQUIREMENTS -r $f"
  fi
done

if [ -n "$PIP_REQUIREMENTS" ]; then
  dry "$VENV_DIR/bin/pip install --upgrade pip $PIP_REQUIREMENTS"
  echo "  ✅ pip 依赖已安装"
else
  # 基础依赖：flask / aiohttp / openai 等常见依赖
  dry "$VENV_DIR/bin/pip install --upgrade pip flask aiohttp openai requests pyyaml"
  echo "  ✅ 基础 pip 依赖已安装"
fi

# ── 6. 生成空 schema 数据库 ─────────────────────────────────────────────
log "初始化数据库（空 schema）..."
INIT_DB_SCRIPT="$PACKAGE_DIR/scripts/init_db.py"
if [ -f "$INIT_DB_SCRIPT" ]; then
  dry "APP_BASE=$INSTALL_ROOT $VENV_DIR/bin/python3 $INIT_DB_SCRIPT"
else
  warn "init_db.py 不存在，跳过数据库初始化"
fi

# ── 7. 创建配置目录与默认配置 ──────────────────────────────────────────
log "生成配置文件..."
CONFIG_SRC="$PACKAGE_DIR/config"
mkdir_dry "$ENV_DIR"

# env 配置
ENV_TARGET="$ENV_DIR/env"
if [ ! -f "$ENV_TARGET" ]; then
  if [ -f "$CONFIG_SRC/env.example" ]; then
    dry "cp $CONFIG_SRC/env.example $ENV_TARGET"
    # 生成随机 SECRET_KEY
    if [ "$DRY_RUN" != "true" ] && [ ! -f "$ENV_TARGET.generated" ]; then
      RANDOM_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
      # 只替换 env.example 中的 SECRET_KEY 占位符（不碰 LLM_API_KEYS 等）
      sed -i '' "s/^SECRET_KEY=.*/SECRET_KEY=${RANDOM_KEY}/" "$ENV_TARGET" 2>/dev/null || \
        sed -i "s/^SECRET_KEY=.*/SECRET_KEY=${RANDOM_KEY}/" "$ENV_TARGET" 2>/dev/null || true
      touch "$ENV_TARGET.generated"
    fi
    chmod_dry 600 "$ENV_TARGET"
    echo "  ✅ env 配置已生成: $ENV_TARGET"
  fi
else
  echo "  ✅ env 配置已存在，跳过"
fi

# rclone 配置
RCLONE_TARGET="/root/.config/rclone/rclone.conf"
if [ -f "$CONFIG_SRC/rclone.conf.example" ] && [ ! -f "$RCLONE_TARGET" ]; then
  mkdir_dry "/root/.config/rclone"
  dry "cp $CONFIG_SRC/rclone.conf.example $RCLONE_TARGET"
  chmod_dry 600 "$RCLONE_TARGET"
  echo "  ✅ rclone 配置模板已生成: $RCLONE_TARGET"
fi

# ── 8. 安装 systemd 单元 ────────────────────────────────────────────────
log "安装 systemd 单元..."
SYSTEMD_SRC="$PACKAGE_DIR/systemd"
VENV_PYTHON="$VENV_DIR/bin/python3"

for svc in "$SYSTEMD_SRC"/*.service; do
  [ -f "$svc" ] || continue
  SVC_NAME="$(basename "$svc")"
  TARGET="/etc/systemd/system/$SVC_NAME"

  # 替换占位符
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} 安装并替换占位符: $SVC_NAME"
  else
    sed "s|__APP_BASE__|$INSTALL_ROOT|g; s|__VENV__|$VENV_PYTHON|g; s|__ENV_FILE__|$ENV_DIR/env|g; s|__SERVICE_USER__|$SERVICE_USER|g" \
      "$svc" > "$TARGET"
    echo "  ✅ $SVC_NAME 已安装"
  fi
done

# timer 文件
for tm in "$SYSTEMD_SRC"/*.timer; do
  [ -f "$tm" ] || continue
  TM_NAME="$(basename "$tm")"
  TARGET="/etc/systemd/system/$TM_NAME"
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} 安装 timer: $TM_NAME"
  else
    cp "$tm" "$TARGET"
    echo "  ✅ $TM_NAME 已安装"
  fi
done

dry "systemctl daemon-reload"

# enable & start 所有 service（不含 timer，timer 单独 enable）
for svc_file in "$SYSTEMD_SRC"/*.service; do
  [ -f "$svc_file" ] || continue
  SVC_NAME="$(basename "$svc_file")"
  # 跳过 .timer 配套的 service（它们由 timer 触发）
  TIMER_SVC="${SVC_NAME%.service}"
  if [ -f "$SYSTEMD_SRC/${TIMER_SVC}.timer" ]; then
    # 有 timer 配套：enable timer（不 start service，timer 会启动它）
    dry "systemctl enable ${TIMER_SVC}.timer"
    dry "systemctl start ${TIMER_SVC}.timer"
    echo "  ✅ ${TIMER_SVC}.timer 已启用"
  else
    dry "systemctl enable $SVC_NAME"
    dry "systemctl start $SVC_NAME"
    echo "  ✅ $SVC_NAME 已启动"
  fi
done

# ── 9. Nginx 配置 ──────────────────────────────────────────────────────
log "安装 Nginx 配置..."
NGINX_SRC="$PACKAGE_DIR/nginx"

for conf in "$NGINX_SRC"/*.conf; do
  [ -f "$conf" ] || continue
  CONF_NAME="$(basename "$conf")"
  TARGET="/etc/nginx/sites-available/$CONF_NAME"
  LINK="/etc/nginx/sites-enabled/$CONF_NAME"

  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} 安装: $CONF_NAME"
  else
    sed "s|__LOG_DIR__|/var/log/nginx|g" "$conf" > "$TARGET"
    ln -sf "$TARGET" "$LINK"
    echo "  ✅ $CONF_NAME 已安装"
  fi
done

if [ "$DRY_RUN" != "true" ]; then
  if nginx -t 2>&1; then
    dry "systemctl reload nginx"
    echo "  ✅ Nginx 配置验证通过，已 reload"
  else
    err "Nginx 配置语法错误，请检查 /etc/nginx/sites-available/"
    echo "  运行: nginx -t 查看具体错误"
  fi
fi

# ── 10. 探活 ────────────────────────────────────────────────────────────
log "服务健康检查（超时 60 秒）..."
HEALTH_ENDPOINTS=(
  "8000:问卷反馈"
  "8001:饮食反馈"
  "9000:管理控制台"
  "9876:Webhook"
)

if [ "$DRY_RUN" = "true" ]; then
  echo -e "${YELLOW}[DRY-RUN]${NC} 将检查以下端口:"
  for ep in "${HEALTH_ENDPOINTS[@]}"; do
    port="${ep%%:*}"
    name="${ep##*:}"
    echo "    :$port ($name)"
  done
else
  TIMEOUT=60
  for ep in "${HEALTH_ENDPOINTS[@]}"; do
    port="${ep%%:*}"
    name="${ep##*:}"
    echo -n "  等待 $name (:${port})..."
    for ((i=0; i<TIMEOUT; i+=2)); do
      if curl -sf "http://localhost:${port}/" > /dev/null 2>&1 || \
         curl -sf "http://localhost:${port}/health" > /dev/null 2>&1 || \
         curl -sf "http://localhost:${port}/ping" > /dev/null 2>&1; then
        echo -e " ${GREEN}✅${NC}"
        break
      fi
      sleep 2
    done
    if ! curl -sf "http://localhost:${port}/" > /dev/null 2>&1 && \
       ! curl -sf "http://localhost:${port}/health" > /dev/null 2>&1 && \
       ! curl -sf "http://localhost:${port}/ping" > /dev/null 2>&1; then
      echo -e " ${RED}❌ (超时)${NC}"
      warn "  $name 未能在 ${TIMEOUT}s 内就绪，请检查: systemctl status research-*"
    fi
  done
fi

# ── 11. 验证 ────────────────────────────────────────────────────────────
log "运行自检..."
if [ "$DRY_RUN" != "true" ]; then
  VERIFY_SCRIPT="$PACKAGE_DIR/verify.sh"
  if [ ! -f "$VERIFY_SCRIPT" ] && [ -f "$PACKAGE_DIR/scripts/verify.sh" ]; then
    VERIFY_SCRIPT="$PACKAGE_DIR/scripts/verify.sh"
  fi
  if [ -f "$VERIFY_SCRIPT" ]; then
    bash "$VERIFY_SCRIPT" || warn "自检发现警告，请查看详情"
  fi
else
  echo -e "${YELLOW}[DRY-RUN]${NC} 跳过自检（dry-run 模式）"
fi

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   ✅ 安装完成                                 ║"
echo "║                                                ║"
echo "║   安装目录: ${INSTALL_ROOT}          ║"
echo "║   日志:     ${LOG_DIR}/install.log         ║"
echo "║                                                ║"
echo "║   管理命令:                                    ║"
echo "║     systemctl status research-*                ║"
echo "║     journalctl -u research-survey-feedback     ║"
echo "║     sudo bash uninstall.sh                     ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""
echo -e "${YELLOW}⚠️  请务必手动填写以下配置文件中的真实凭据：${NC}"
echo "    ${ENV_DIR}/env"
echo "      SMTP_PASSWORD, LLM_API_KEYS, ALERT_SMTP_PASSWORD, WJX_SURVEY_TOKEN, WJX_DIET_TOKEN"
echo "    /root/.config/rclone/rclone.conf"
echo "      drive_id, token"
echo ""
echo -e "${YELLOW}  填写完成后重启服务: systemctl restart research-*${NC}"
