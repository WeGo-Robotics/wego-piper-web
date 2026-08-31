#!/usr/bin/env bash
# 이미지에 실을 **호스트 쪽 코드**를 `.hostside/` 에 모은다.
#
# ## 왜 이미지 안에 넣나
#
# 사용자에게 파일을 여러 개 주면 하나를 빠뜨린다. 스크립트 하나만 주고 나머지는
# 전부 `docker pull` 로 오게 하려면, 도커 **바깥**에서 도는 것들(데몬·wheel·udev·
# compose·apply.sh)도 이미지 안에 있어야 한다. 설치할 때 꺼내 쓴다.
#
# 비용은 없다시피 하다 — 실측 **207KB**, 앱 이미지 3.7GB 의 0.0057% 다.
# 맨 마지막 레이어에 얹으므로 베이스도 앱 레이어도 안 건드린다.
#
# 사용:  ./deploy/stage-hostside.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/.hostside"
cd "$REPO"

rm -rf "$OUT"; mkdir -p "$OUT/wheels" "$OUT/udev"

# ── 데몬 wheel ────────────────────────────────────────────────────────────
# ⚠ **다섯 개 전부 만든다.** 어느 것이 바뀌었는지 따지지 않는다 — 이미지가 곧
#   배포 단위이므로 부분만 담으면 호스트에 옛 wheel 이 남는다. 다 합쳐 155KB 다.
# ⚠ 전부 `py3-none-any`(순수 파이썬)라 호스트 파이썬 버전과 무관하다. 확인:
#     ls .hostside/wheels   → piper_*-0.1.0-py3-none-any.whl
for p in bus shm robot cam rs; do
  python3 -m pip wheel --no-deps -q -w "$OUT/wheels" "./$p"
done
rm -rf bus/build shm/build robot/build cam/build rs/build 2>/dev/null || true

# ── 데몬 소스·유닛·설치 스크립트 ──────────────────────────────────────────
# ⚠ `__pycache__` 는 뺀다. 빌드 머신과 호스트의 파이썬이 달라 쓰이지도 않는다.
tar cf - --exclude='__pycache__' --exclude='*.pyc' daemons | (cd "$OUT" && tar xf -)
cp -r deploy/systemd "$OUT/systemd"
cp deploy/install-daemons.sh deploy/apply.sh "$OUT/"

# ── compose · env 예시 ────────────────────────────────────────────────────
cp docker-compose.yml "$OUT/"
cp deploy/env.example "$OUT/backend.env.example"

# ── udev ──────────────────────────────────────────────────────────────────
cp deploy/udev/99-piper-can.rules deploy/udev/list-can-adapters.py "$OUT/udev/"
cp backend/udev/99-realsense-libusb.rules "$OUT/udev/"

chmod +x "$OUT/apply.sh" "$OUT/install-daemons.sh"
echo "· 호스트 코드 준비: $(du -sh "$OUT" | cut -f1)  ($OUT)"
