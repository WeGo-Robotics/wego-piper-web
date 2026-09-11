#!/usr/bin/env bash
# piper-web 제거 — 설치(piper-install.sh → apply.sh)가 만든 것을 되돌린다.
#
# ⚠ **데이터는 안 지운다.** 설치가 그렇듯 제거도 데이터를 건드리지 않는다 — /srv/piper-data
#   (데이터셋·모델·로그), ~/.cache/huggingface/lerobot(녹화한 데이터셋), ~/.config/piper-web(설정)
#   은 `--purge-data` 를 **명시**해야 지운다.
# ⚠ **sudo 는 직접 쓰지 않는다.** 설치와 같다 — root 로 놓은 것(udev 규칙 등)은 명령을 찍어 준다.
# ⚠ 이 호스트의 사정(docker-compose.override.yml)은 ~/override.keep.yml 로 빼 둔다 — 다시 깔면
#   apply.sh 가 알아서 되돌린다.
#
# 사용:  ./piper-uninstall.sh                 # 유닛·컨테이너·이미지·venv·배포 디렉토리
#        ./piper-uninstall.sh --keep-images   # 이미지는 둔다 (다시 깔 때 3.5GB 안 받게)
#        ./piper-uninstall.sh --purge-data    # ⚠ 데이터까지
#        ./piper-uninstall.sh --dry-run       # 무엇을 지울지만 보여 준다
set -euo pipefail

KEEP_IMAGES=0; PURGE=0; DRY=0
for a in "$@"; do
  case "$a" in
    --keep-images) KEEP_IMAGES=1 ;;
    --purge-data)  PURGE=1 ;;
    --dry-run)     DRY=1 ;;
    -h|--help)     sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "모르는 옵션: $a  (--keep-images | --purge-data | --dry-run)"; exit 1 ;;
  esac
done

# apply.sh 와 같은 자리들 — 여기와 거기가 어긋나면 지우는 척만 한다
WORK="${PIPER_WORK:-$HOME/piper-web-deploy}"
SRC="$WORK/current"
VENV="$HOME/.venvs/piper-daemons"
DATA="${PIPER_DATA_ROOT:-/srv/piper-data}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

say()  { printf "\n%s\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
skip() { printf "  \033[90m-\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
# --dry-run 이면 하지 않고 무엇을 할지만 찍는다. runq 는 조용한 판.
run()  { if [ $DRY = 1 ]; then printf "  \033[90m(dry-run)\033[0m %s\n" "$*"; else "$@"; fi; }
runq() { if [ $DRY = 1 ]; then printf "  \033[90m(dry-run)\033[0m %s\n" "$*"; else "$@" >/dev/null 2>&1; fi; }

echo "piper-web 제거$([ $DRY = 1 ] && echo ' (dry-run — 아무것도 안 바꾼다)' || true)"

have_docker() { command -v docker >/dev/null && docker info >/dev/null 2>&1; }

# ── 1. 컨테이너 ───────────────────────────────────────────────────────────
say "1. 컨테이너"
if have_docker; then
  if [ -f "$SRC/docker-compose.yml" ]; then
    # compose 가 .env 의 COMPOSE_FILE(nogpu 조각·override)을 그대로 읽는다 — 띄운 그 조합으로 내린다
    run bash -c "cd '$SRC' && docker compose down --remove-orphans" && ok "docker compose down"
  fi
  # compose 파일이 없거나 조합이 달라도 남은 컨테이너는 이름으로 잡는다
  for c in piper-web-backend piper-web-frontend; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then runq docker rm -f "$c" && ok "컨테이너 $c"; fi
  done
else
  skip "docker 없음/안 돎 — 컨테이너·이미지는 건너뛴다"
fi

# ── 2. 데몬 유닛 ─────────────────────────────────────────────────────────
say "2. 데몬 유닛"
units="$(ls "$UNIT_DIR"/piper-*.service 2>/dev/null | xargs -rn1 basename || true)"
if [ -n "$units" ]; then
  for u in $units; do
    runq systemctl --user disable --now "$u" || true
    run rm -f "$UNIT_DIR/$u"
    ok "$u"
  done
  runq systemctl --user daemon-reload || true
  runq systemctl --user reset-failed || true
else
  skip "유닛 없음 ($UNIT_DIR/piper-*.service)"
fi
# 데몬이 남긴 공유 메모리 세그먼트 — 유닛이 멈춘 뒤엔 쓰는 곳이 없다
if ls /dev/shm/piper.* >/dev/null 2>&1; then run rm -f /dev/shm/piper.* && ok "/dev/shm/piper.*"; fi

# ── 3. 이미지 ─────────────────────────────────────────────────────────────
say "3. 이미지"
if [ $KEEP_IMAGES = 1 ]; then
  skip "--keep-images — 이미지는 둔다"
elif have_docker; then
  # 로컬 이름·레지스트리 이름·버전 태그 전부 (piper-web-backend:v0.4.16, ghcr.io/…/piper-web-frontend:latest …)
  imgs="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '(^|/)piper-web-(backend|frontend):' || true)"
  if [ -n "$imgs" ]; then
    for i in $imgs; do runq docker image rm -f "$i" || true; done
    runq docker image prune -f || true          # 태그가 다 떨어진 레이어
    ok "이미지 $(echo "$imgs" | wc -l) 개 (backend·frontend, 태그 전부)"
  else
    skip "이미지 없음"
  fi
fi

# ── 4. venv · 배포 디렉토리 ────────────────────────────────────────────────
say "4. venv · 배포 디렉토리"
if [ -d "$VENV" ]; then run rm -rf "$VENV" && ok "$VENV"; else skip "venv 없음"; fi
if [ -d "$WORK" ]; then
  # ⚠ override 는 이 호스트의 사정(포트 충돌 회피)이다 — 빼 두면 다시 깔 때 apply.sh 가 되돌린다
  if [ -f "$SRC/docker-compose.override.yml" ]; then
    run cp "$SRC/docker-compose.override.yml" "$HOME/override.keep.yml" \
      && ok "override → ~/override.keep.yml (다시 깔면 apply.sh 가 되돌린다)"
  fi
  if [ -f "$SRC/.env" ] && grep -q '^PIPER_WEB_PORT=' "$SRC/.env"; then
    port="$(grep '^PIPER_WEB_PORT=' "$SRC/.env" | tail -n1)"
    warn "$port 였다 — 다시 깔 때 같은 값을 앞에 붙이세요:  $port ./piper-install.sh"
  fi
  run rm -rf "$WORK" && ok "$WORK"
else
  skip "배포 디렉토리 없음"
fi

# ── 5. 데이터 ─────────────────────────────────────────────────────────────
say "5. 데이터"
DATA_DIRS=("$DATA" "$HOME/.cache/huggingface/lerobot" "$HOME/.config/piper-web")
if [ $PURGE = 1 ]; then
  for d in "${DATA_DIRS[@]}"; do
    [ -e "$d" ] || continue
    if run rm -rf "$d" 2>/dev/null; then ok "$d 지움"; else warn "$d 는 못 지웠다 — sudo rm -rf $d"; fi
  done
else
  for d in "${DATA_DIRS[@]}"; do
    [ -e "$d" ] && skip "$d 남김 ($(du -sh "$d" 2>/dev/null | cut -f1 || echo '?')) — 지우려면 --purge-data" || true
  done
fi

# ── 6. root 로 놓은 것 — 명령만 찍는다 ───────────────────────────────────
say "6. 호스트에 남긴 것 (sudo 필요 — 이 스크립트는 직접 안 한다)"
echo "  지워도 되는 것 — 다른 것이 안 쓴다면 (CAN 규칙은 이 배선의 시리얼이 든 것이라 다시 쓸 거면 둔다):"
echo "    sudo rm -f /etc/udev/rules.d/99-realsense-libusb.rules /etc/udev/rules.d/99-piper-can.rules && sudo udevadm control --reload-rules"
echo "  그대로 두는 것 — 공용 설정이라 손대지 않는다: docker 그룹, linger, redis-server(유닉스 소켓 설정), docker.io·python3-venv 패키지"

say "끝"
echo "  다시 깔려면:  ./piper-install.sh   (~/override.keep.yml 이 있으면 override 를 되돌린다)"
