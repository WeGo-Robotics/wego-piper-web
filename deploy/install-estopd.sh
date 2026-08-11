#!/usr/bin/env bash
# E-stop 워치독을 systemd 사용자 유닛으로 설치한다.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_DIR"
sed "s|@REPO@|$REPO|g; s|@PY@|$PY|g" \
  "$REPO/deploy/systemd/piper-estopd.service" > "$UNIT_DIR/piper-estopd.service"

systemctl --user daemon-reload
systemctl --user enable --now piper-estopd

if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  echo
  echo "⚠ linger 가 꺼져 있습니다 — 로그아웃 시 워치독이 함께 죽습니다."
  echo "   sudo loginctl enable-linger $USER"
fi
systemctl --user --no-pager status piper-estopd | head -5
