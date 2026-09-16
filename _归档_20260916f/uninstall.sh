#!/usr/bin/env bash
# ============================================================================
#  个性化饮食干预课题 — 卸载脚本
#  默认只停用服务 + 移除 systemd/Nginx 配置，保留数据目录。
#  加 --purge 才删除安装目录（含数据库）。
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'
NC='\033[0m'
log()  { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}==>${NC} $*"; }
err()  { echo -e "${RED}❌ $*${NC}" >&2; }

INSTALL_ROOT="${INSTALL_ROOT:-/opt/gutbrainaxis}"
PURGE="${PURGE:-false}"

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE="true"; shift ;;
    -h|--help)
      echo "用法: sudo bash uninstall.sh [--purge]"
      echo "  --purge  同时删除安装目录（含数据库，不可恢复）"
      exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  die "需要 root 权限"
fi

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   卸载安装包                                    ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""

# ── 1. 停用并移除 systemd 单元 ──────────────────────────────────────────
log "停用并移除 systemd 单元..."
for svc in /etc/systemd/system/research-*.service; do
  [ -f "$svc" ] || continue
  name="$(basename "$svc")"
  echo "  停止: $name"
  systemctl stop "$name" 2>/dev/null || true
  systemctl disable "$name" 2>/dev/null || true
  rm -f "$svc"
  echo "  ✅ 已移除 $name"
done

for tm in /etc/systemd/system/research-*.timer; do
  [ -f "$tm" ] || continue
  name="$(basename "$tm")"
  systemctl stop "$name" 2>/dev/null || true
  systemctl disable "$name" 2>/dev/null || true
  rm -f "$tm"
  echo "  ✅ 已移除 $name"
done

systemctl daemon-reload
echo ""

# ── 2. 移除 Nginx 站点 ─────────────────────────────────────────────────
log "移除 Nginx 站点配置..."
for conf in /etc/nginx/sites-available/research-*.conf; do
  [ -f "$conf" ] || continue
  name="$(basename "$conf")"
  rm -f "/etc/nginx/sites-enabled/$name"
  rm -f "$conf"
  echo "  ✅ 已移除 $name"
done

if nginx -t 2>&1; then
  systemctl reload nginx 2>/dev/null || true
  echo "  ✅ Nginx 已 reload"
fi
echo ""

# ── 3. 可选删除安装目录 ────────────────────────────────────────────────
if [ "$PURGE" = "true" ]; then
  warn "⚠️  --purge 模式：将删除整个安装目录（含数据库）"
  echo -n "确认删除 ${INSTALL_ROOT}？(输入 YES 确认): "
  read -r confirm
  if [ "$confirm" = "YES" ]; then
    rm -rf "$INSTALL_ROOT"
    rm -f /etc/research-app/env
    echo "  ✅ 已删除: $INSTALL_ROOT"
  else
    warn "取消删除数据目录"
  fi
else
  echo ""
  echo -e "${YELLOW}⚠️  数据目录保留: ${INSTALL_ROOT}${NC}"
  echo "  如需删除，运行: sudo bash $0 --purge"
  echo "  建议先备份: tar czf backup-$(date +%Y%m%d).tar.gz $INSTALL_ROOT"
fi

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   ✅ 卸载完成                                 ║"
echo "╚═══════════════════════════════════════════════╝"
