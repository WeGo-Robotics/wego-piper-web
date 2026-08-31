#!/usr/bin/env bash
# piper-web 설치·업데이트. **사용자가 받는 유일한 파일이다.**
#
# 나머지는 전부 이미지 안에 있다 — 데몬·wheel·udev·compose·apply.sh 까지.
# 파일을 여러 개 주면 하나를 빠뜨리고, 빠뜨린 것은 늘 나중에 엉뚱한 증상으로
# 드러난다. 그래서 여기서 받아 꺼내고 넘긴다.
#
# 사용:
#   ./piper-install.sh              # 최신
#   ./piper-install.sh v0.3.10      # 특정 버전
#   PIPER_IMAGE=<주소>/piper-web-backend ./piper-install.sh   # 다른 곳에서 받기
set -euo pipefail

# ⚠ **아직 정해지지 않았다.** 공개 레지스트리 네임스페이스가 정해지면 이 줄을
#   바꾼다. 그 전에는 `PIPER_IMAGE` 로 넘겨야 한다.
IMAGE="${PIPER_IMAGE:-wego/piper-web-backend}"
VERSION="${1:-latest}"
WORK="${PIPER_WORK:-$HOME/piper-web-deploy}"

ok()  { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad() { printf "  \033[31m✗\033[0m %s\n" "$1"; }
say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

echo "piper-web 설치  ($IMAGE:$VERSION)"

# ── 0. 도커 ───────────────────────────────────────────────────────────────
# ⚠ **이 검사만 여기 있다.** 나머지 전제(compose·venv·redis·GPU·udev…)는 전부
#   `apply.sh` 가 본다 — 한 곳에 있어야 갈리지 않는다. 그런데 도커는 그것을
#   *꺼내오는 수단*이라 여기서 먼저 봐야 한다. 없으면 아무것도 시작 못 한다.
say "0. 도커"
if ! command -v docker >/dev/null; then
  bad "docker 가 없습니다"
  echo "     sudo apt update && sudo apt install -y docker.io"
  echo "     sudo usermod -aG docker \$USER   # 그 뒤 다시 로그인"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  bad "docker 데몬에 못 붙습니다"
  if systemctl is-active --quiet docker 2>/dev/null; then
    echo "     sudo usermod -aG docker $USER   # 그 뒤 **다시 로그인**해야 반영됩니다"
  else
    echo "     sudo systemctl enable --now docker"
  fi
  exit 1
fi
ok "docker"

# ── 1. 받기 ───────────────────────────────────────────────────────────────
say "1. 이미지 받기"
# ⚠ 이미 있는 레이어는 안 받는다. 두 번째 설치부터는 우리 코드만 온다.
if ! docker pull "$IMAGE:$VERSION"; then
  bad "받지 못했습니다: $IMAGE:$VERSION"
  echo "     다른 곳에서 받으려면:  PIPER_IMAGE=<주소>/piper-web-backend $0 $VERSION"
  exit 1
fi
ok "$IMAGE:$VERSION"

# ── 2. 호스트 코드 꺼내기 ─────────────────────────────────────────────────
say "2. 호스트 코드 꺼내기"
# ⚠ `docker create` 는 **컨테이너를 실행하지 않는다.** 받은 이미지를 돌려보지
#   않고 파일만 꺼내려는 것이다 — 설치 전에 남의 코드를 실행할 이유가 없다.
DEST="$WORK/${VERSION}"
mkdir -p "$DEST"
cid="$(docker create "$IMAGE:$VERSION")"
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
docker cp "$cid:/opt/piper-host/." "$DEST/"
docker rm -f "$cid" >/dev/null; trap - EXIT
[ -f "$DEST/apply.sh" ] || { bad "이미지에 호스트 코드가 없습니다 (/opt/piper-host)"; exit 1; }
ok "$DEST"

# ── 3. 넘긴다 ─────────────────────────────────────────────────────────────
# 여기서부터는 `apply.sh` 가 전부 한다. 전제 확인·udev·wheel·데몬·컨테이너.
say "3. 설치"
exec "$DEST/apply.sh" "${@:2}"
