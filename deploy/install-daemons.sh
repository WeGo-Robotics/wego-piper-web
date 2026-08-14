#!/usr/bin/env bash
# 데몬 넷을 systemd 사용자 유닛으로 설치한다.
#
# 왜 유닛이어야 하나: 수동으로 띄우면 **죽어도 아무도 모른다.**
# 실제로 robotd 가 조용히 죽었고, 화면은 팔이 연결됐다고 하는데 추론만
# "세그먼트가 없습니다"로 죽었다. 왜 죽었는지는 끝내 못 찾았다 —
# 로그가 어디에도 안 남았기 때문이다. 유닛이면 되살아나고 journald 에 남는다.
#
# 사용법:
#   deploy/install-daemons.sh              # 넷 다
#   deploy/install-daemons.sh estopd rsd   # 고른 것만
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# ⚠ `command -v python3` 이 아니라 **지금 쉘의 python3** 을 그대로 쓴다.
# conda·venv 안에서 돌리면 그쪽 인터프리터여야 piper_bus·piper_rs 가 보인다.
# `/usr/bin/python3` 로 굳혀두면 이 저장소에서는 import 부터 실패한다.
PY="$(command -v python3)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

DAEMONS=("$@")
if [ ${#DAEMONS[@]} -eq 0 ]; then
  DAEMONS=(estopd robotd camerad rsd)
fi

mkdir -p "$UNIT_DIR"
for d in "${DAEMONS[@]}"; do
  src="$REPO/deploy/systemd/piper-$d.service"
  [ -f "$src" ] || { echo "✗ 유닛 파일이 없습니다: $src" >&2; exit 1; }
  sed "s|@REPO@|$REPO|g; s|@PY@|$PY|g" "$src" > "$UNIT_DIR/piper-$d.service"
  echo "· piper-$d.service → $UNIT_DIR"
done

systemctl --user daemon-reload

# ⚠ **먼저 수동으로 띄운 것을 죽인다.** 안 그러면 장치를 두 프로세스가 다툰다 —
# 소유가 하나여야 한다는 게 데몬을 나눈 전제다.
#
# ⚠ 유닛이 이미 돌고 있으면 건너뛴다. `pgrep` 은 유닛이 띄운 자식도 똑같이 잡으므로,
# 재설치할 때마다 자기가 관리하는 프로세스를 죽이게 된다(재시작되긴 하지만
# 그 사이 장치가 끊긴다 — 녹화 중이면 프레임이 빈다).
for d in "${DAEMONS[@]}"; do
  if systemctl --user is-active --quiet "piper-$d" 2>/dev/null; then
    continue
  fi
  if pids=$(pgrep -f "daemons/$d\.py$" 2>/dev/null); then
    echo "· 수동 실행 중인 $d 종료: $pids"
    kill $pids 2>/dev/null || true
  fi
done
sleep 1

for d in "${DAEMONS[@]}"; do
  systemctl --user enable --now "piper-$d"
done

if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  echo
  echo "⚠ linger 가 꺼져 있습니다 — 로그아웃 시 데몬이 함께 죽습니다."
  echo "   sudo loginctl enable-linger $USER"
fi

echo
for d in "${DAEMONS[@]}"; do
  printf '%-22s %s\n' "piper-$d" "$(systemctl --user is-active "piper-$d")"
done
echo
echo "로그:  journalctl --user -u piper-robotd -f"
