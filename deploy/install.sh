#!/usr/bin/env bash
# 이 저장소를 새 머신에 설치한다.
#
# ⚠ **README 의 두 줄로는 안 된다.** `pip install -e backend/` 는 로컬 패키지
#   11개 중 하나도 안 딸려온다 — `backend/pyproject.toml` 이 그것들에 의존하지
#   않기 때문이다. 순서도 있다(`piper-robot` 이 `piper-shm`·`piper-bus` 를 쓴다).
#
# 하는 일만 한다. 시스템 패키지 설치와 그룹 추가는 sudo 가 필요하므로
# **명령을 찍어 주고 사람이 실행**한다 — 스크립트가 몰래 sudo 를 쓰면
# 무엇이 바뀌었는지 아무도 모른다.
#
# 사용:  ./deploy/install.sh [--check]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }

# ── 1. 시스템 전제 ────────────────────────────────────────────────────────
echo "1. 시스템 전제"
MISSING_SYS=()
command -v redis-server >/dev/null || MISSING_SYS+=("redis-server")
command -v node >/dev/null        || MISSING_SYS+=("nodejs")
command -v npm >/dev/null         || MISSING_SYS+=("npm")
command -v ip >/dev/null          || MISSING_SYS+=("iproute2")
if [ ${#MISSING_SYS[@]} -gt 0 ]; then
  bad "없는 것: ${MISSING_SYS[*]}"
  echo "     sudo apt install ${MISSING_SYS[*]}"
else
  ok "redis-server · node · npm · ip"
fi

# ⚠ Redis 는 **버스 전체**다. 없으면 데몬끼리 말을 못 하고 웹은 "데몬 없음"만 띄운다.
if redis-cli ping >/dev/null 2>&1; then ok "redis 응답"; else bad "redis 가 안 떠 있다 — sudo systemctl enable --now redis-server"; fi

# ── 2. 그룹 ───────────────────────────────────────────────────────────────
echo "2. 그룹 (재로그인해야 반영된다)"
for g in video dialout; do
  # ⚠ `video` 가 없으면 `/dev/video*` 를 못 열어 RealSense 스캔이 0개가 된다.
  #   `dialout` 은 USB-CAN 어댑터용이다.
  if id -nG | tr ' ' '\n' | grep -qx "$g"; then ok "$g"; else bad "$g 없음 — sudo usermod -aG $g $USER"; fi
done
for g in adm systemd-journal; do
  # 커널 로그(USB 분리·CAN 오류)를 읽으려면 필요하다. 없어도 돌지만 진단이 막힌다.
  id -nG | tr ' ' '\n' | grep -qx "$g" && ok "$g (커널 로그)" || warn "$g 없음 — 진단용, sudo usermod -aG $g $USER"
done

# ── 3. 서브모듈 ───────────────────────────────────────────────────────────
echo "3. 서브모듈"
# URDF 는 지오메트리를 **다시 구울 때만** 필요하다 — 구운 npz 는 저장소에 있다.
if [ -f "$REPO/vendor/agx_arm_urdf/piper/urdf/piper_description.urdf" ]; then
  ok "vendor/agx_arm_urdf"
else
  warn "vendor/agx_arm_urdf 없음 (지오메트리 재빌드에만 필요)"
  [ $CHECK_ONLY -eq 0 ] && git -C "$REPO" submodule update --init --recursive && ok "받았다"
fi

# ── 4. 파이썬 패키지 — **순서가 있다** ────────────────────────────────────
echo "4. 파이썬 패키지"
# 의존 순서: bus·shm → robot·cam → rs → phase → vendor → backend
PKGS=(bus shm robot cam rs phase act_aux
      vendor/wego_piper vendor/lerobot_robot_piper
      vendor/lerobot_robot_pipershm vendor/lerobot_camera_pipershm
      backend)
if [ $CHECK_ONLY -eq 1 ]; then
  for p in "${PKGS[@]}"; do
    n=$(grep -m1 '^name' "$REPO/$p/pyproject.toml" | cut -d'"' -f2 | tr '_' '-')
    python3 -m pip show "$n" >/dev/null 2>&1 && ok "$p" || bad "$p 미설치"
  done
else
  for p in "${PKGS[@]}"; do
    python3 -m pip install -q -e "$REPO/$p" && ok "$p"
  done
  python3 -m pip install -q -e "$REPO/backend[dev]" && ok "backend[dev]"
fi

# ── 5. 설정 ───────────────────────────────────────────────────────────────
echo "5. backend/.env"
# ⚠ **코드 기본값은 `direct` 다.** 그러면 LeRobot subprocess 가 CAN·카메라를
#   직접 열어 robotd 의 안전 필터와 카메라 공유가 통째로 빠진다.
if [ -f "$REPO/backend/.env" ]; then
  grep -q "PIPER_ROBOT_TRANSPORT=shm"  "$REPO/backend/.env" && ok "PIPER_ROBOT_TRANSPORT=shm"  || warn "PIPER_ROBOT_TRANSPORT 가 shm 이 아니다 — 안전 필터가 안 걸린다"
  grep -q "PIPER_CAMERA_TRANSPORT=shm" "$REPO/backend/.env" && ok "PIPER_CAMERA_TRANSPORT=shm" || warn "PIPER_CAMERA_TRANSPORT 가 shm 이 아니다"
else
  bad "backend/.env 없음 — deploy/env.example 을 복사하세요"
fi

# ── 6. udev ───────────────────────────────────────────────────────────────
echo "6. udev 규칙"
for r in "$REPO/deploy/udev/99-piper-can.rules" "$REPO/backend/udev/99-realsense-libusb.rules"; do
  n=$(basename "$r")
  [ -f "/etc/udev/rules.d/$n" ] && ok "$n" || bad "$n 미설치 — sudo cp $r /etc/udev/rules.d/"
done

# ── 7. 프론트엔드 ─────────────────────────────────────────────────────────
echo "7. 프론트엔드"
if [ -d "$REPO/frontend/node_modules" ]; then ok "node_modules"; else
  bad "npm install 필요"
  [ $CHECK_ONLY -eq 0 ] && (cd "$REPO/frontend" && npm install --silent) && ok "설치됨"
fi

# ── 8. 데몬 ───────────────────────────────────────────────────────────────
echo "8. systemd 사용자 유닛"
for d in estopd robotd camerad rsd gateway frontend; do
  systemctl --user list-unit-files "piper-$d.service" >/dev/null 2>&1 \
    && [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/piper-$d.service" ] \
    && ok "piper-$d" || bad "piper-$d 미설치"
done
[ $CHECK_ONLY -eq 0 ] && "$REPO/deploy/install-daemons.sh" estopd robotd camerad rsd gateway frontend

# ⚠ linger 가 꺼져 있으면 **로그아웃할 때 데몬이 통째로 죽는다** — 학습·녹화까지.
loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes" \
  && ok "linger" || bad "linger 꺼짐 — sudo loginctl enable-linger $USER"

echo
echo "확인만 하려면: ./deploy/install.sh --check"
