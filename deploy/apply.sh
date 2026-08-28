#!/usr/bin/env bash
# 번들을 호스트에 적용한다. **첫 설치와 업데이트가 같은 명령이다.**
#
# ## 왜 하나인가
#
# 절차가 갈리면 "업데이트인 줄 알았는데 첫 설치였다"가 생긴다 — 그때 빠뜨리는
# 것은 늘 sudo 가 필요한 쪽(redis 소켓·linger·udev)이고, 증상은 "웹은 뜨는데
# 카메라도 팔도 안 보인다"라 원인을 찾기 어렵다.
#
# 그래서 **없는 것만 한다.** 이미 돼 있으면 건너뛴다. 두 번 돌려도 같다.
#
# 사용:  ./apply.sh [--check]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
say()  { printf "\n\033[1m%s\033[0m\n" "$1"; }

[ -f "$HERE/manifest.txt" ] || { echo "manifest.txt 가 없습니다 — 번들 안에서 실행하세요"; exit 1; }
# shellcheck disable=SC1090
source "$HERE/manifest.txt"
echo "piper-web $version  (직전 $prev, 빌드 $built_at)"

VENV="$HOME/.venvs/piper-daemons"
DATA="${PIPER_DATA_ROOT:-/srv/piper-data}"

# ── 0. 호스트 전제 — sudo 가 필요한 것은 **찍어만 준다** ──────────────────
say "0. 호스트 전제"
NEED_SUDO=()
[ -d "$DATA" ] && ok "데이터 루트 $DATA" || { bad "$DATA 없음"; NEED_SUDO+=("mkdir -p $DATA && chown $USER $DATA"); }
# ⚠ 컨테이너는 유닉스 소켓으로만 버스에 붙는다. 설정만 하고 redis 를 재시작 안 하면
#   소켓 파일이 없어 backend 가 "E-stop 버스에 연결할 수 없습니다" 로 뜬다(실제 사고).
[ -S /run/redis/redis-server.sock ] && ok "redis 유닉스 소켓" || {
  bad "redis 소켓 없음"
  NEED_SUDO+=("sed -i 's|^# *unixsocket |unixsocket |; s|^# *unixsocketperm .*|unixsocketperm 770|' /etc/redis/redis.conf")
  NEED_SUDO+=("systemctl restart redis-server")
}
loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes" && ok "linger" || {
  bad "linger 꺼짐 — 로그아웃하면 데몬이 통째로 죽는다"
  NEED_SUDO+=("loginctl enable-linger $USER")
}
if [ ${#NEED_SUDO[@]} -gt 0 ]; then
  echo
  echo "  아래를 먼저 실행하세요 (이 스크립트는 sudo 를 직접 쓰지 않습니다):"
  for c in "${NEED_SUDO[@]}"; do echo "    sudo $c"; done
  [ $CHECK = 0 ] && exit 1
fi

# ── 1. 이미지 ─────────────────────────────────────────────────────────────
if [ -n "${images:-}" ]; then
  say "1. 이미지 ($images)"
  if [ $CHECK = 1 ]; then
    for s in $images; do
      docker image inspect "piper-web-$s:$version" >/dev/null 2>&1 \
        && ok "piper-web-$s:$version" || bad "piper-web-$s:$version 미적용"
    done
  else
    gunzip -c "$HERE/images.tar.gz" | docker load
    # ⚠ compose 는 `image: piper-web-backend` (태그 생략=latest) 로 참조한다.
    #   `:latest` 로도 달아두지 않으면 compose 가 **다시 빌드하려 든다.**
    for s in $images; do
      docker tag "piper-web-$s:$version" "piper-web-$s:latest"
      ok "piper-web-$s:$version → :latest"
    done
  fi
else
  say "1. 이미지 — 이번 릴리스에 없음"
fi

# ── 2. 데몬 라이브러리 ────────────────────────────────────────────────────
if [ -n "${wheels:-}" ]; then
  say "2. 데몬 wheel ($wheels)"
  if [ ! -d "$VENV" ]; then
    if [ $CHECK = 1 ]; then bad "$VENV 없음"; else
      # ⚠ `--system-site-packages` 로 만든다 — numpy·opencv·piper-sdk 를 다시 안 깐다.
      python3 -m venv --system-site-packages "$VENV" && ok "venv 생성"
      "$VENV/bin/pip" install -q redis pyrealsense2 && ok "PyPI 의존(redis·pyrealsense2)"
    fi
  fi
  if [ $CHECK = 1 ]; then
    for p in $wheels; do
      "$VENV/bin/pip" show "piper-${p}" >/dev/null 2>&1 || "$VENV/bin/pip" show "piper_${p}" >/dev/null 2>&1 \
        && ok "piper-$p" || bad "piper-$p 미설치"
    done
  else
    "$VENV/bin/pip" install -q --no-deps --force-reinstall "$HERE"/wheels/*.whl
    ok "wheel $(ls "$HERE"/wheels | wc -l) 개"
  fi
else
  say "2. 데몬 wheel — 이번 릴리스에 없음"
fi

# ── 3. 데몬 소스 + 유닛 ───────────────────────────────────────────────────
SRC="$HOME/piper-daemons"
if [ -n "${daemons:-}" ]; then
  say "3. 데몬 소스·유닛"
  if [ $CHECK = 1 ]; then
    [ -d "$SRC/daemons" ] && ok "$SRC" || bad "$SRC 없음"
  else
    mkdir -p "$SRC" && tar xzf "$HERE/daemons.tar.gz" -C "$SRC" && ok "풀었다: $SRC"
    # ⚠ 설치 스크립트는 **지금 셸의 python3** 를 유닛에 박는다. venv 를 켜고 불러야
    #   데몬이 wheel 을 볼 수 있다 (deploy/install-daemons.sh 참고).
    ( . "$VENV/bin/activate" && "$SRC/deploy/install-daemons.sh" estopd robotd camerad rsd )
    ok "유닛 설치·기동"
  fi
else
  say "3. 데몬 소스·유닛 — 이번 릴리스에 없음"
fi

# ── 4. 기동 ───────────────────────────────────────────────────────────────
say "4. 기동"
if [ $CHECK = 0 ]; then
  # 데몬이 먼저다 — 컨테이너는 세그먼트와 버스가 있어야 뭔가 보인다.
  for d in estopd robotd camerad rsd; do systemctl --user restart "piper-$d" 2>/dev/null || true; done
  ok "데몬 재시작"
  ( cd "$SRC" 2>/dev/null || cd "$HOME"; docker compose up -d 2>/dev/null ) || \
    warn "docker compose 는 compose 파일이 있는 곳에서 직접 실행하세요"
fi
for d in estopd robotd camerad rsd; do
  systemctl --user is-active --quiet "piper-$d" && ok "piper-$d" || bad "piper-$d 안 돎"
done

say "확인"
echo "  ./apply.sh --check      # 적용 상태만 다시 본다"
echo "  docker compose logs -f backend"
