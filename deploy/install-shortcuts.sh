#!/usr/bin/env bash
# 바탕화면·앱 메뉴 바로가기 — [Piper Studio] 와 [Piper Studio 진단] (feature/field-deployment.md §5).
#
# 일반 사용자는 주소를 외우지 않는다. 설치(apply.sh 5절)가 이걸 불러 아이콘 둘을 만든다:
#   [Piper Studio]       웹을 연다. 웹이 안 떠 있으면 진단 보고서를 대신 연다   (piper-studio.sh)
#   [Piper Studio 진단]  보고서만 만든다 — 웹이 안 뜰 때 원인을 한 장으로        (piper-doctor.sh)
#
# ⚠ 그래픽 세션이 없는 기계(SSH 로만 쓰는 NUC)에서도 죽지 않는다 — 앱 메뉴 항목은 늘 만들고,
#   바탕화면 사본은 그 디렉토리가 **있을 때만** 둔다. `xdg-user-dir` 는 바탕화면이 없으면 $HOME 을
#   돌려주는데, 거기에 .desktop 을 두면 홈 디렉토리에 파일이 굴러다닌다 — 그래서 $HOME 이면 없는 것으로 친다.
# ⚠ GNOME(우분투 22.04+)은 바탕화면의 .desktop 을 "신뢰할 수 없음" 으로 막는다 — 실행 비트와 gio 의
#   `metadata::trusted` 를 미리 준다. gio 가 세션 버스를 못 찾으면(SSH) 건너뛰고 **말한다**
#   (docs/install-troubleshooting.md §11 — 우클릭 [실행 허용]).
# ⚠ 두 번 돌려도 같다. 파일을 덮어쓸 뿐이다.
#
# 사용:  install-shortcuts.sh            # 만든다
#        install-shortcuts.sh --check    # 있는지만 본다 (apply.sh --check)
#        install-shortcuts.sh --remove   # 지운다 (piper-uninstall.sh 도 같은 자리를 지운다)
#   환경: PIPER_SRC    실행 스크립트(piper-studio.sh·piper-doctor.sh)가 있는 자리. 기본 ~/piper-web-deploy/current
#         PIPER_ICON   아이콘 원본(svg). 기본: 이 스크립트 옆의 piper-studio.svg
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-install}"
case "$MODE" in install|--check|--remove) ;; *) echo "사용: $0 [--check | --remove]"; exit 2 ;; esac

SRC="${PIPER_SRC:-${PIPER_WORK:-$HOME/piper-web-deploy}/current}"
ICON_SRC="${PIPER_ICON:-$HERE/piper-studio.svg}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$DATA/applications"
ICON="$DATA/icons/piper-studio.svg"
DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
DESKTOP="${DESKTOP:-$HOME/Desktop}"
have_desktop() { [ "$DESKTOP" != "$HOME" ] && [ -d "$DESKTOP" ]; }

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }

# 이름 → (영문 이름, 한글 이름, 영문 설명, 한글 설명, 실행 명령)
entry_fields() {
  case "$1" in
    piper-studio)
      printf '%s\n' "Piper Studio" "Piper Studio" \
        "Open Piper Studio in the browser (shows a diagnosis when the web is down)" \
        "브라우저에서 Piper Studio 를 연다 (웹이 안 떠 있으면 진단 보고서를 연다)" \
        "\"$SRC/piper-studio.sh\"" ;;
    piper-studio-doctor)
      printf '%s\n' "Piper Studio Diagnostics" "Piper Studio 진단" \
        "Why is the web not coming up? One-page report" \
        "웹이 안 뜰 때 원인을 한 장으로" \
        "\"$SRC/piper-doctor.sh\" --open" ;;
  esac
}

write_entry() {   # $1 파일, $2 이름 키
  local name name_ko comment comment_ko exec_
  { read -r name; read -r name_ko; read -r comment; read -r comment_ko; read -r exec_; } < <(entry_fields "$2")
  cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$name
Name[ko]=$name_ko
Comment=$comment
Comment[ko]=$comment_ko
Exec=$exec_
Icon=$ICON
Terminal=false
Categories=Development;Science;
StartupNotify=false
EOF
}

ENTRIES="piper-studio piper-studio-doctor"

case "$MODE" in
  --check)
    for e in $ENTRIES; do
      [ -f "$APPS/$e.desktop" ] && ok "앱 메뉴: $e" || bad "앱 메뉴 항목 없음: $e — 적용하면 만든다"
    done
    [ -f "$ICON" ] && ok "아이콘" || bad "아이콘 없음 ($ICON)"
    if have_desktop; then
      for e in $ENTRIES; do
        [ -x "$DESKTOP/$e.desktop" ] && ok "바탕화면: $e" || bad "바탕화면 사본 없음(또는 실행 비트 없음): $e"
      done
    else
      warn "바탕화면 디렉토리 없음 — 앱 메뉴 항목만 (SSH 만 쓰는 기계면 정상)"
    fi
    exit 0 ;;
  --remove)
    n=0
    for f in "$APPS/piper-studio.desktop" "$APPS/piper-studio-doctor.desktop" \
             "$DESKTOP/piper-studio.desktop" "$DESKTOP/piper-studio-doctor.desktop" "$ICON"; do
      [ -e "$f" ] || continue
      rm -f "$f" && ok "$f"; n=$((n + 1))
    done
    [ $n -gt 0 ] || echo "  바로가기 없음"
    command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
    exit 0 ;;
esac

# ── 만든다 ────────────────────────────────────────────────────────────────
for s in piper-studio.sh piper-doctor.sh; do
  [ -x "$SRC/$s" ] || warn "$SRC/$s 가 없거나 실행 비트가 없다 — 아이콘을 눌러도 안 열린다 (apply.sh 5절이 깐다)"
done
mkdir -p "$APPS" "$(dirname "$ICON")"
if [ -f "$ICON_SRC" ]; then cp "$ICON_SRC" "$ICON"; else warn "아이콘 원본 없음 ($ICON_SRC) — 기본 아이콘으로 보인다"; fi
for e in $ENTRIES; do
  write_entry "$APPS/$e.desktop" "$e"
done
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
ok "앱 메뉴: Piper Studio · Piper Studio 진단 ($APPS)"

if have_desktop; then
  trusted=1
  for e in $ENTRIES; do
    cp "$APPS/$e.desktop" "$DESKTOP/$e.desktop"
    chmod +x "$DESKTOP/$e.desktop"
    # GNOME 의 "실행 허용" 을 미리 준다. 세션 버스가 없으면(SSH) gio 가 실패한다 — 건너뛰고 아래서 말한다.
    if command -v gio >/dev/null; then
      gio set "$DESKTOP/$e.desktop" metadata::trusted true 2>/dev/null || trusted=0
    else
      trusted=0
    fi
  done
  if [ $trusted = 1 ]; then
    ok "바탕화면: Piper Studio · Piper Studio 진단 ($DESKTOP)"
  else
    ok "바탕화면: Piper Studio · Piper Studio 진단 ($DESKTOP)"
    warn "\"실행 허용\" 표시를 미리 못 줬다(세션 없음) — 아이콘이 잠겨 보이면 우클릭 → [실행 허용] 한 번"
  fi
else
  echo "  바탕화면 디렉토리가 없어 앱 메뉴 항목만 만들었다 (SSH 만 쓰는 기계면 정상)"
fi
