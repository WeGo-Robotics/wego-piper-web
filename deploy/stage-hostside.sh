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
# ⚠ **번들과 똑같은 모양으로 싣는다.** 예전에는 `daemons/`·`systemd/` 를 디렉토리로
#   풀어 놨는데, `apply.sh` 는 `daemons.tar.gz` 를 기대한다 — 이미지로 설치하면
#   3절이 "Cannot open: No such file or directory" 로 실패하고, 그런데도 **그 다음
#   줄이 옛 `$SRC` 의 스크립트로 유닛을 설치했다.** 즉 낡은 데몬이 깔렸다.
#   `/opt/piper-host` 가 곧 번들이면 `apply.sh` 는 한 경로만 알면 된다.
# ⚠ `__pycache__` 는 뺀다. 빌드 머신과 호스트의 파이썬이 달라 쓰이지도 않는다.
tar czf "$OUT/daemons.tar.gz" --exclude='__pycache__' --exclude='*.pyc' \
    daemons deploy/systemd deploy/install-daemons.sh
cp deploy/apply.sh "$OUT/"

# ── compose · env 예시 ────────────────────────────────────────────────────
cp docker-compose.yml "$OUT/"
cp deploy/env.example "$OUT/backend.env.example"

# ── udev ──────────────────────────────────────────────────────────────────
# ⚠ CAN 규칙 파일이 아니라 **만드는 도구**를 싣는다 (release.sh 의 같은 주석 참고)
cp deploy/udev/list-can-adapters.py "$OUT/udev/"
cp backend/udev/99-realsense-libusb.rules "$OUT/udev/"

# `install-daemons.sh` 는 이제 tarball 안이라 여기서 못 만진다 — 실행 비트는
# tar 가 보존하고, 푸는 쪽(`apply.sh` 3절)이 그대로 쓴다.
chmod +x "$OUT/apply.sh"
echo "· 호스트 코드 준비: $(du -sh "$OUT" | cut -f1)  ($OUT)"
