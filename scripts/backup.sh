#!/usr/bin/env bash
# ============================================================================
#  备份脚本 — 保留最近 N 份备份，定时清理旧备份
# ============================================================================
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/opt/gutbrainaxis}"
BACKUP_DIR="${BACKUP_DIR:-$INSTALL_ROOT/backups}"
RETENTION="${RETENTION:-14}"   # 保留最近 14 份
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_NAME="backup_${TIMESTAMP}"
LOG_DIR="${LOG_DIR:-$INSTALL_ROOT/logs}"

mkdir -p "$BACKUP_DIR" "$LOG_DIR"

echo "============================================"
echo "备份开始: $(date)"
echo "目标: $BACKUP_DIR/$BACKUP_NAME"
echo "============================================"

# ── 1. 备份数据库 ─────────────────────────────────────────────────────────
echo "[1/4] 备份数据库..."
DB_DIRS=(
  "$INSTALL_ROOT/sjtu_survey_pro"
  "$INSTALL_ROOT/diet_survey"
  "$INSTALL_ROOT/exercise_survey"
)
for dbdir in "${DB_DIRS[@]}"; do
  for dbfile in "$dbdir"/*.db; do
    [ -f "$dbfile" ] || continue
    cp "$dbfile" "$BACKUP_DIR/${BACKUP_NAME}_$(basename "$dbfile")"
    echo "  ✅ 备份: $(basename "$dbfile")"
  done
done

# ── 2. 备份配置文件 ───────────────────────────────────────────────────────
echo "[2/4] 备份配置..."
CONF_BACKUP_DIR="$BACKUP_DIR/${BACKUP_NAME}_config"
mkdir -p "$CONF_BACKUP_DIR"

if [ -f /etc/research-app/env ]; then
  cp /etc/research-app/env "$CONF_BACKUP_DIR/"
  echo "  ✅ 备份: env"
fi

if [ -f /root/.config/rclone/rclone.conf ]; then
  cp /root/.config/rclone/rclone.conf "$CONF_BACKUP_DIR/"
  echo "  ✅ 备份: rclone.conf"
fi

# 备份 systemd 配置
for f in /etc/systemd/system/research-*.service /etc/systemd/system/research-*.timer; do
  [ -f "$f" ] || continue
  cp "$f" "$CONF_BACKUP_DIR/"
done
echo "  ✅ 备份: systemd 配置"

# ── 3. 打包 ───────────────────────────────────────────────────────────────
echo "[3/4] 打包..."
cd "$BACKUP_DIR"
tar czf "${BACKUP_NAME}.tar.gz" "${BACKUP_NAME}_"*.db "${BACKUP_NAME}_config"
rm -rf "${BACKUP_NAME}_"*.db "${BACKUP_NAME}_config"
echo "  ✅ 打包完成: ${BACKUP_NAME}.tar.gz ($(du -sh "${BACKUP_NAME}.tar.gz" | cut -f1))"

# ── 4. 清理旧备份（保留 N 份） ────────────────────────────────────────────
echo "[4/4] 清理旧备份（保留最近 ${RETENTION} 份）..."
ls -t "$BACKUP_DIR"/backup_*.tar.gz 2>/dev/null | tail -n +$((RETENTION + 1)) | while read -r old; do
  rm -f "$old"
  echo "  删除旧备份: $(basename "$old")"
done

echo ""
echo "✅ 备份完成: ${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
echo "   保留周期: ${RETENTION} 份"
echo "============================================"
