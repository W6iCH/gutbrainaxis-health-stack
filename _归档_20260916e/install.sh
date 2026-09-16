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
    echo -e "${YELLOW}[DRY-RUN]${NC} mkdir $*"
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
    --dry-run-prefix=*) DRY_RUN_PREFIX="${1#*=}"; shift ;;
    --dry-run-prefix) DRY_RUN_PREFIX="$2"; shift 2 ;;
    --prefix=*) INSTALL_ROOT="${1#*=}"; shift ;;
    --prefix) INSTALL_ROOT="$2"; shift 2 ;;
    -h|--help)
      echo "用法: sudo bash install.sh [--dry-run] [--prefix=/opt/gutbrainaxis]"
      echo "      sudo bash install.sh --dry-run [--dry-run-prefix=/tmp/gutbrainaxis-dryrun]"
      exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

# ── 0. 权限检查 + dry-run 路径消歧 ───────────────────────────────────
# ⚠️ 修正（设计复查发现）：旧版定义了 DRY_RUN_PREFIX 但**从未使用**，
#    `--dry-run --prefix=/tmp/x` 会一边显示真实系统路径（/etc/systemd/...）、
#    一边又声称“不修改系统”，语义二义。现统一：
#      · 真实安装：SYSROOT 为空，路径就是真实系统路径。
#      · dry-run ：所有系统级路径统一加上 DRY_RUN_PREFIX（默认 /tmp/…），
#                  输出的就是“将要写入的确切位置”，且肯定不碰真实系统。
if [ "$DRY_RUN" = "true" ] && [ -z "$DRY_RUN_PREFIX" ]; then
  DRY_RUN_PREFIX="/tmp/gutbrainaxis-dryrun"
fi
SYSROOT=""
if [ "$DRY_RUN" = "true" ]; then
  SYSROOT="$DRY_RUN_PREFIX"
  mkdir -p "$SYSROOT" 2>/dev/null || true
fi
ENV_DIR="$SYSROOT/etc/research-app"
SYSTEMD_DIR="$SYSROOT/etc/systemd/system"
NGINX_AVAIL_DIR="$SYSROOT/etc/nginx/sites-available"
NGINX_ENABLED_DIR="$SYSROOT/etc/nginx/sites-enabled"
LOGROTATE_DIR="$SYSROOT/etc/logrotate.d"
RCLONE_DIR="$SYSROOT/root/.config/rclone"

if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" != "true" ]; then
  die "需要 root 权限。请使用 sudo bash install.sh 或切换到 root 用户执行。"
fi
if [ "$DRY_RUN" = "true" ]; then
  warn "DRY-RUN 模式：不修改系统；系统级路径前缀为 ${SYSROOT}"
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

# ── 4. 复制 services / scripts / tools / docs / 配置模板 ──────────────
log "复制服务代码与运行期脚本..."
# 仓库根目录（install.sh 位于仓库根）
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES_SRC="$PACKAGE_DIR/services"
if [ -d "$SERVICES_SRC" ]; then
  dry "cp -r $SERVICES_SRC/* $INSTALL_ROOT/"
  echo "  ✅ 服务代码已复制（含 microbiome/ 与 common/）"
else
  die "services/ 目录不存在 ($SERVICES_SRC)"
fi

# ⚠️ 修正：旧版**只复制 services/**，但 systemd 单元的 ExecStart 指向
#    __APP_BASE__/scripts/health_monitor.py、__APP_BASE__/scripts/rclone-backup.sh，
#    并且自检/建库工具在 __APP_BASE__/tools/ —— 这些目录从未被安装，
#    导致 research-health-monitor.timer 与 research-rclone-backup.timer
#    装上即失败（unit 找不到脚本）。现一并安装。
for sub in scripts tools docs; do
  if [ -d "$PACKAGE_DIR/$sub" ]; then
    dry "cp -r $PACKAGE_DIR/$sub $INSTALL_ROOT/"
    echo "  ✅ $sub/ 已复制"
  else
    warn "$sub/ 不存在，跳过（相关 timer/自检可能不可用）"
  fi
done

# 清理可能随包带出的编辑器/系统垃圾文件（.DS_Store / __pycache__ / *.pyc）
if [ "$DRY_RUN" != "true" ]; then
  find "$INSTALL_ROOT" -name '.DS_Store' -delete 2>/dev/null || true
  find "$INSTALL_ROOT" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$INSTALL_ROOT" -name '*.pyc' -delete 2>/dev/null || true
fi

# 创建数据目录
for d in data logs backups microbiome microbiome/import; do
  mkdir_dry -p "$INSTALL_ROOT/$d"
done

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

# ① 统一配置入口 app.yaml（单一真源）
# ⚠️ 修正：旧版只生成 env，没有统一配置入口，也没有 schema 校验。
APP_YAML="$ENV_DIR/app.yaml"
if [ ! -f "$APP_YAML" ] && [ -f "$CONFIG_SRC/app.yaml.example" ]; then
  dry "cp $CONFIG_SRC/app.yaml.example $APP_YAML"
  # 把示例里的安装前缀替换为本次实际安装路径
  if [ "$DRY_RUN" != "true" ]; then
    sed -i.bak "s|/opt/gutbrainaxis|$INSTALL_ROOT|g" "$APP_YAML" 2>/dev/null || \
      sed -i "s|/opt/gutbrainaxis|$INSTALL_ROOT|g" "$APP_YAML" 2>/dev/null || true
    rm -f "$APP_YAML.bak"
  fi
  chmod_dry 644 "$APP_YAML"
  echo "  ✅ 统一配置已生成: $APP_YAML（请修改 site.domain 与密钥）"
fi

# ② 密钥文件 secrets.env（0600）——真实密钥只存这里，不入库
SECRETS_FILE="$ENV_DIR/secrets.env"
if [ ! -f "$SECRETS_FILE" ] && [ "$DRY_RUN" != "true" ]; then
  cat > "$SECRETS_FILE" <<'SECRETS_EOF'
# 真实密钥 — 权限 0600，绝不入库
# 由 app.yaml 里的 "${VAR}" 引用解析
SMTP_USERNAME=
SMTP_PASSWORD=__CHANGE_ME__
SMTP_SENDER_EMAIL=
SMTP_ADMIN_EMAIL=
ALERT_SMTP_USERNAME=
ALERT_SMTP_PASSWORD=__CHANGE_ME__
ALERT_FROM=
ALERT_TO=
LLM_API_KEYS=__CHANGE_ME__
LLM_BACKUP_API_KEY=__CHANGE_ME__
WJX_SURVEY_TOKEN=__CHANGE_ME__
WJX_DIET_TOKEN=__CHANGE_ME__
WJX_EXERCISE_TOKEN=__CHANGE_ME__
SECRETS_EOF
  chmod_dry 600 "$SECRETS_FILE"
  echo "  ✅ 密钥模板已生成: $SECRETS_FILE（0600）"
fi

# ③ 渲染 systemd 环境文件（由 app.yaml → env）
# ⚠️ 修正：旧版直接 cp env.example → env，与 app.yaml 不联动；
#    另 SECRET_KEY 的 sed 因为 env.example 根本没这个键而静默失败。
env_file_path="$ENV_DIR/env"
if [ "$DRY_RUN" = "true" ]; then
  echo -e "${YELLOW}[DRY-RUN]${NC} 将由 app.yaml 渲染 $env_file_path"
elif [ -f "$APP_YAML" ] && [ -f "$PACKAGE_DIR/tools/appconfig.py" ]; then
  if APP_BASE="$INSTALL_ROOT" "$VENV_DIR/bin/python3" "$PACKAGE_DIR/tools/appconfig.py" \
       --config "$APP_YAML" --secrets "$SECRETS_FILE" --no-strict \
       --render-env "$env_file_path" >/dev/null 2>&1; then
    echo "  ✅ 环境文件已由 app.yaml 渲染: $env_file_path"
  else
    warn "app.yaml 渲染失败，回退为直接复制 env.example"
    cp "$CONFIG_SRC/env.example" "$env_file_path"
  fi
else
  [ -f "$env_file_path" ] || cp "$CONFIG_SRC/env.example" "$env_file_path"
fi
chmod_dry 600 "$env_file_path" 2>/dev/null || true

# ④ 控制台配置 console.env
# ⚠️ 修正：旧版从未复制 console.env.example，控制台因此一直读不到配置。
CONSOLE_ENV="$ENV_DIR/console.env"
if [ ! -f "$CONSOLE_ENV" ] && [ -f "$CONFIG_SRC/console.env.example" ]; then
  dry "cp $CONFIG_SRC/console.env.example $CONSOLE_ENV"
  if [ "$DRY_RUN" != "true" ]; then
    RANDOM_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    RANDOM_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
    sed -i.bak "s|^SECRET_KEY=.*|SECRET_KEY=${RANDOM_SECRET}|" "$CONSOLE_ENV" 2>/dev/null || \
      sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${RANDOM_SECRET}|" "$CONSOLE_ENV" 2>/dev/null || true
    sed -i.bak "s|^CONSOLE_PASSWORD=.*|CONSOLE_PASSWORD=${RANDOM_PW}|" "$CONSOLE_ENV" 2>/dev/null || \
      sed -i "s|^CONSOLE_PASSWORD=.*|CONSOLE_PASSWORD=${RANDOM_PW}|" "$CONSOLE_ENV" 2>/dev/null || true
    rm -f "$CONSOLE_ENV.bak"
    # 初始口令单独落盘（600），供首次登录
    printf '%s\n' "$RANDOM_PW" > "$CONFIG_SRC/../config/initial_password.txt" 2>/dev/null || true
    cp "$CONFIG_SRC/../config/initial_password.txt" "$ENV_DIR/initial_password.txt" 2>/dev/null || true
    printf '%s\n' "$RANDOM_PW" > "$ENV_DIR/initial_password.txt"
    chmod_dry 600 "$ENV_DIR/initial_password.txt"
  fi
  chmod_dry 600 "$CONSOLE_ENV"
  echo "  ✅ 控制台配置已生成: $CONSOLE_ENV"
fi

# rclone 配置
RCLONE_TARGET="$RCLONE_DIR/rclone.conf"
if [ -f "$CONFIG_SRC/rclone.conf.example" ] && [ ! -f "$RCLONE_TARGET" ]; then
  mkdir_dry "$RCLONE_DIR"
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
  TARGET="$SYSTEMD_DIR/$SVC_NAME"

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
  TARGET="$SYSTEMD_DIR/$TM_NAME"
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

# ── 8b. 日志轮转 + 按 app.yaml 渲染 timer 周期 ────────────────────────
log "安装日志轮转与定时周期..."
LR_SRC="$CONFIG_SRC/logrotate.research-app"
if [ -f "$LR_SRC" ]; then
  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} 安装 $LOGROTATE_DIR/research-app"
  else
    mkdir -p "$LOGROTATE_DIR"
    sed "s|__APP_BASE__|$INSTALL_ROOT|g; s|__SERVICE_USER__|$SERVICE_USER|g" \
      "$LR_SRC" > "$LOGROTATE_DIR/research-app"
    # 语法校验（logrotate -d 为 dry-run）
    if logrotate -d "$LOGROTATE_DIR/research-app" >/dev/null 2>&1; then
      echo "  ✅ 日志轮转已安装且语法正确（保留 14 份，按天）"
    else
      warn "logrotate 配置语法校验未通过，请手工检查 $LOGROTATE_DIR/research-app"
    fi
  fi
else
  warn "未找到 config/logrotate.research-app（日志将不轮转，可能撑满磁盘）"
fi

# 用 app.yaml 的 schedule.* 覆盖 timer 周期（systemd 支持 .d/ 片段覆盖）
if [ "$DRY_RUN" = "true" ]; then
  echo -e "${YELLOW}[DRY-RUN]${NC} 按 app.yaml schedule.* 生成 timer 周期片段"
elif [ -f "$APP_YAML" ] && [ -f "$PACKAGE_DIR/tools/appconfig.py" ]; then
  if "$VENV_DIR/bin/python3" "$PACKAGE_DIR/tools/appconfig.py" \
       --config "$APP_YAML" --secrets "$SECRETS_FILE" --no-strict \
       --render-timers "$SYSTEMD_DIR" >/dev/null 2>&1; then
    echo "  ✅ timer 周期片段已按 app.yaml 生成（$SYSTEMD_DIR/*.timer.d/）"
    dry "systemctl daemon-reload"
    # 重新 enable/start timer，使新周期生效
    for tm in "$SYSTEMD_SRC"/*.timer; do
      [ -f "$tm" ] || continue
      dry "systemctl restart $(basename "$tm")"
    done
  else
    warn "timer 周期渲染失败，使用单元文件中的默认周期"
  fi
fi

# ── 9. Nginx 配置 ──────────────────────────────────────────────────────
log "安装 Nginx 配置..."
NGINX_SRC="$PACKAGE_DIR/nginx"

for conf in "$NGINX_SRC"/*.conf; do
  [ -f "$conf" ] || continue
  CONF_NAME="$(basename "$conf")"
  TARGET="$NGINX_AVAIL_DIR/$CONF_NAME"
  LINK="$NGINX_ENABLED_DIR/$CONF_NAME"

  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}[DRY-RUN]${NC} 安装: $CONF_NAME"
  else
    mkdir -p "$NGINX_AVAIL_DIR" "$NGINX_ENABLED_DIR"
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
    err "Nginx 配置语法错误，请检查 $NGINX_AVAIL_DIR/"
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
  "8090:数据看板"
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
      if curl -sf "http://localhost:${port}/healthz" > /dev/null 2>&1 || \
         curl -sf "http://localhost:${port}/health" > /dev/null 2>&1 || \
         curl -sf "http://localhost:${port}/ping" > /dev/null 2>&1; then
        echo -e " ${GREEN}✅${NC}"
        break
      fi
      sleep 2
    done
    if ! curl -sf "http://localhost:${port}/healthz" > /dev/null 2>&1 && \
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
  # 优先用统一自检引擎（结构化 + 逐一修复建议）；回退到 verify.sh
  SELFCHECK="$INSTALL_ROOT/tools/selfcheck.py"
  if [ -f "$SELFCHECK" ] && [ -x "$VENV_DIR/bin/python3" ]; then
    "$VENV_DIR/bin/python3" "$SELFCHECK" --config "$APP_YAML" \
      --no-external --report "$LOG_DIR/selfcheck.json" \
      || warn "自检发现失败项（详情看表，或 $LOG_DIR/selfcheck.json）"
  else
    warn "未找到 tools/selfcheck.py，回退到 verify.sh"
    VERIFY_SCRIPT="$PACKAGE_DIR/verify.sh"
    if [ -f "$VERIFY_SCRIPT" ]; then
      bash "$VERIFY_SCRIPT" || warn "自检发现警告，请查看详情"
    fi
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
