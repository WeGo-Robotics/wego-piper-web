#!/usr/bin/env bash
# [Piper Studio] 아이콘이 부르는 것 — 웹을 연다. 안 떠 있으면 진단 보고서를 대신 연다.
#
# ⚠ "연결할 수 없음" 흰 화면을 일반 사용자에게 보여 주지 않는다 (feature/field-deployment.md §3 ②).
#   프론트(nginx)가 응답하면 브라우저를 열고, 응답이 없으면 piper-doctor.sh 가 만든 보고서를 연다 —
#   게이트웨이만 죽은 경우는 웹이 뜨고 화면이 말하는 층이다.
# ⚠ 포트는 apply.sh 5절(접속)과 같은 순서로 찾는다 — compose 에 묻고, 못 물으면 .env 의
#   PIPER_WEB_PORT, 그것도 없으면 80. override 로 옮긴 포트도 compose 가 안다.
#
# 사용:  piper-studio.sh [--kiosk]
#   환경: PIPER_BROWSER   URL 하나를 받아 여는 명령. 기본: google-chrome/chromium 앱 창 → xdg-open
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${PIPER_SRC:-$HERE}"      # apply.sh 가 current/ 에 같이 둔다
KIOSK=0; [ "${1:-}" = "--kiosk" ] && KIOSK=1

web_port() {
  local p=""
  if [ -f "$SRC/docker-compose.yml" ] && command -v docker >/dev/null; then
    p="$( (cd "$SRC" && timeout 5 docker compose port frontend 80) 2>/dev/null | head -n1 | sed 's/.*://' || true)"
  fi
  if [ -z "$p" ] && [ -f "$SRC/.env" ]; then p="$(sed -n 's/^PIPER_WEB_PORT=//p' "$SRC/.env" | tail -n1)"; fi
  case "$p" in ''|*[!0-9]*) p=80 ;; esac
  echo "$p"
}

# 프론트가 **응답하나** — 상태 코드는 안 본다. 로그인이 켜져 있어도 nginx 는 정적 화면을 준다.
web_up() {   # $1 포트
  if command -v curl >/dev/null; then
    curl -s -o /dev/null -m 3 "http://127.0.0.1:$1/"
  else
    timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/$1" 2>/dev/null
  fi
}

open_url() {   # $1 URL
  if [ -n "${PIPER_BROWSER:-}" ]; then exec $PIPER_BROWSER "$1"; fi
  local b
  for b in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$b" >/dev/null; then
      if [ $KIOSK = 1 ]; then exec "$b" --kiosk "$1"; else exec "$b" --app="$1"; fi
    fi
  done
  exec xdg-open "$1"
}

port="$(web_port)"
url="http://localhost$([ "$port" != 80 ] && echo ":$port")/"
if web_up "$port"; then
  open_url "$url"
else
  exec "$HERE/piper-doctor.sh" --open
fi
