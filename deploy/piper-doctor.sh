#!/usr/bin/env bash
# [Piper Studio 진단] — 웹이 안 뜰 때 원인을 한 장으로 (feature/field-deployment.md §3 ② · §5-3).
#
# 브라우저가 "연결할 수 없음" 만 보여 주는 층(프론트 컨테이너·docker 자체)은 웹이 말해 줄 수 없다 —
# 그래서 이건 웹 **밖**에서 돈다. 설치가 이미 아는 것(apply.sh --check)과 실행 상태(compose·유닛·저널)를
# 모아 찍는다. **아무것도 안 바꾼다.**
#
# ⚠ 진단은 죽으면 안 된다. 명령이 없거나 실패하는 것 자체가 정보라 `set -e` 를 안 쓰고, 오래 걸릴 수
#   있는 것(docker·apply.sh --check)은 시간을 제한한다.
# ⚠ 처방 문장은 새로 짓지 않는다 — apply.sh --check 가 찍는 ✗·! 줄을 그대로 싣는다
#   (docs/install-troubleshooting.md 의 표와 같은 문장이다).
#
# 사용:  piper-doctor.sh            # 터미널에 텍스트
#        piper-doctor.sh --html     # HTML 을 표준 출력으로
#        piper-doctor.sh --open     # HTML 보고서를 만들어 브라우저로 연다 (아이콘이 이걸 부른다)
#   환경: PIPER_BROWSER   보고서를 열 명령 (기본 xdg-open). PIPER_WORK: 배포 디렉토리(기본 ~/piper-web-deploy)
set -uo pipefail
MODE="${1:-text}"
WORK="${PIPER_WORK:-$HOME/piper-web-deploy}"
SRC="$WORK/current"
DAEMONS="estopd robotd camerad rsd unitd"

web_port() {
  local p=""
  [ -f "$SRC/.env" ] && p="$(sed -n 's/^PIPER_WEB_PORT=//p' "$SRC/.env" | tail -n1)"
  case "$p" in ''|*[!0-9]*) p=80 ;; esac
  echo "$p"
}
http_code() {   # $1 URL → 상태 코드, 못 붙으면 "없음"
  if command -v curl >/dev/null; then
    local c; c="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$1" 2>/dev/null || true)"
    case "$c" in ''|000) echo "없음" ;; *) echo "$c" ;; esac
  else
    echo "(curl 없음)"
  fi
}
strip_ansi() { sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g'; }

report() {
  local port ip fe gw
  port="$(web_port)"
  echo "Piper Studio 진단 — $(date '+%Y-%m-%d %H:%M:%S') — $(hostname 2>/dev/null || echo '?')"
  echo "아무것도 바꾸지 않았다. 이 화면을 캡처해 문의하면 된다."
  echo

  echo "[1] 적용본"
  if [ -f "$SRC/VERSION" ]; then echo "  $(cat "$SRC/VERSION")  ($SRC)"
  else echo "  설치된 적이 없다 — $SRC/VERSION 없음. 설치는 ./piper-install.sh"; fi
  echo

  echo "[2] 웹 (포트 $port)"
  fe="$(http_code "http://127.0.0.1:$port/")"
  gw="$(http_code "http://127.0.0.1:$port/health")"
  echo "  프론트(nginx) : $fe"
  echo "  게이트웨이     : $gw   (/health)"
  if [ "$fe" = "없음" ]; then
    echo "  → 프론트 컨테이너가 안 떠 있다. 아래 [3] 컨테이너를 본다 — docker 가 안 돌거나 포트를 남이 쓴다(ss -ltnp)"
  elif [ "$gw" != "200" ]; then
    echo "  → 프론트는 뜨는데 게이트웨이가 안 떠 있거나 뜨는 중이다:  cd $SRC && docker compose logs --tail 100 backend"
  else
    echo "  → 웹은 살아 있다. 브라우저에서 http://localhost$([ "$port" != 80 ] && echo ":$port")/"
  fi
  echo

  echo "[3] 컨테이너"
  if ! command -v docker >/dev/null; then echo "  docker 없음 — sudo apt install docker.io docker-compose-v2"
  elif ! timeout 10 docker info >/dev/null 2>&1; then
    echo "  docker 데몬에 못 붙는다 — 안 돌거나(sudo systemctl enable --now docker) 이 사용자가 docker 그룹이 아니다(재로그인)"
  elif [ -f "$SRC/docker-compose.yml" ]; then
    (cd "$SRC" && timeout 20 docker compose ps 2>&1) | sed 's/^/  /' || echo "  docker compose ps 실패"
  else
    echo "  $SRC/docker-compose.yml 없음 — 설치가 끝까지 안 갔다"
  fi
  echo

  echo "[4] 호스트 데몬"
  if timeout 10 systemctl --user list-units 'piper-*' --all --no-pager --no-legend >/tmp/.piper-doctor.$$ 2>&1 && grep -q . /tmp/.piper-doctor.$$; then
    sed 's/^/  /' /tmp/.piper-doctor.$$
  else
    echo "  systemd 사용자 유닛을 못 읽었다 — 세션이 없거나(SSH: loginctl enable-linger) 유닛이 안 깔렸다"
  fi
  rm -f /tmp/.piper-doctor.$$
  echo

  echo "[5] 전제 점검 (apply.sh --check 의 ✗·! 줄)"
  if [ -f "$SRC/VERSION" ] && [ -x "$WORK/$(cat "$SRC/VERSION")/apply.sh" ]; then
    local lines
    lines="$(timeout 120 "$WORK/$(cat "$SRC/VERSION")/apply.sh" --check 2>&1 | strip_ansi | grep -E '^\s*[✗!]' || true)"
    if [ -n "$lines" ]; then echo "$lines" | sed 's/^\s*/  /'; else echo "  전부 ✓"; fi
  else
    echo "  점검 스크립트 없음 — 그 버전의 번들(~/piper-web-deploy/<버전>/apply.sh)이 없다"
  fi
  echo

  # ⚠ 이번 부팅(-b)만 본다 — 전체에서 15줄을 뽑으면 몇 주 전 경고가 "최근" 으로 올라와 사람을 엉뚱한 데로 보낸다
  echo "[6] 저널 — 이번 부팅, 경고 이상, 데몬마다 최근 15줄"
  local d
  for d in $DAEMONS; do
    echo "  · piper-$d"
    timeout 10 journalctl --user -b -u "piper-$d" -p warning -n 15 --no-pager -o short 2>/dev/null | sed 's/^/      /' || echo "      (읽지 못함)"
  done
  echo

  echo "[7] 접속 주소"
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  echo "  http://${ip:-<이 기계의 IP>}$([ "$port" != 80 ] && echo ":$port")/   (다른 기계에서)"
  echo
  echo "더 읽을 것: docs/install-troubleshooting.md — 증상에서 찾는다"
}

html_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
html() {
  cat <<'EOF'
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>Piper Studio 진단</title>
<style>body{margin:0;background:#171717;color:#e5e5e5;font:14px/1.5 ui-monospace,Menlo,Consolas,monospace}
h1{font:600 18px system-ui,sans-serif;margin:0;padding:16px 20px;background:#262626;border-bottom:1px solid #404040}
pre{white-space:pre-wrap;padding:20px;margin:0}</style></head><body>
<h1>Piper Studio 진단 — 웹이 안 떠서 이 보고서를 대신 열었다</h1>
<pre>
EOF
  report | html_escape
  cat <<'EOF'
</pre></body></html>
EOF
}

case "$MODE" in
  text|"") report ;;
  --html) html ;;
  --open)
    out="${XDG_CACHE_HOME:-$HOME/.cache}/piper-web/doctor.html"
    mkdir -p "$(dirname "$out")"
    html > "$out"
    echo "보고서: $out"
    if [ -n "${PIPER_BROWSER:-}" ]; then exec $PIPER_BROWSER "file://$out"; fi
    command -v xdg-open >/dev/null && exec xdg-open "file://$out"
    echo "브라우저를 못 열었다(xdg-open 없음) — 위 파일을 직접 연다" ;;
  *) echo "사용: $0 [--html | --open]"; exit 2 ;;
esac
