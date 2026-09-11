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
# ⚠ **일곱 개 전부 만든다.** 어느 것이 바뀌었는지 따지지 않는다 — 이미지가 곧
#   배포 단위이므로 부분만 담으면 호스트에 옛 wheel 이 남는다. 다 합쳐 200KB 안쪽이다.
# ⚠ 전부 `py3-none-any`(순수 파이썬)라 호스트 파이썬 버전과 무관하다. 확인:
#     ls .hostside/wheels   → piper_*-0.1.0-py3-none-any.whl
#   so101·sim 의 바깥 의존(feetech-servo-sdk·mujoco)은 wheel 에 없다 — apply.sh 가
#   PyPI 에서 깐다 (mujoco 는 플랫폼 wheel 이라 여기서 못 싣는다).
# ⚠ **wheel 버전에 릴리스 태그를 도장 찍는다.** 저장소의 pyproject 는 전부 `0.1.0` 이라
#   호스트 venv 에 어느 릴리스의 wheel 이 깔렸는지 아무도 모른다 — 데몬 자기 보고가
#   `piper-robot 0.4.5` 라고 말해야 화면의 [버전] 이 게이트웨이와 대조할 수 있다
#   (feature/version-update.md §2). 저장소는 안 건드린다 — 굽는 사본만 바꾼다.
STAMP="${PIPER_VERSION#v}"
TMPB="$(mktemp -d)"; trap 'rm -rf "$TMPB"' EXIT
for p in bus shm robot cam rs so101 sim; do
  rm -rf "$TMPB/$p"; cp -r "./$p" "$TMPB/$p"; rm -rf "$TMPB/$p/build" "$TMPB/$p"/*.egg-info
  if [ -n "$STAMP" ]; then
    sed -i "s/^version = \"[^\"]*\"/version = \"$STAMP\"/" "$TMPB/$p/pyproject.toml"
  fi
  python3 -m pip wheel --no-deps -q -w "$OUT/wheels" "$TMPB/$p"
done

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
# 웹 업데이트가 쓰는 둘 (feature/version-update.md): 받기 스크립트, 그리고 받은 뒤
# "무엇이 달라졌나"를 보여 줄 변경 이력 — `.dockerignore` 가 `*.md` 를 빼므로 여기서 싣는다
cp deploy/piper-install.sh "$OUT/"
cp deploy/piper-uninstall.sh "$OUT/"   # 제거도 번들에서 — 설치가 만든 것을 되돌린다 (데이터는 남긴다)
cp deploy/pull-progress.py "$OUT/"
cp CHANGELOG.md "$OUT/"

# ── compose · env 예시 ────────────────────────────────────────────────────
cp docker-compose.yml "$OUT/"
cp docker-compose.nogpu.yml "$OUT/"    # GPU 없는 호스트 조각 — apply.sh 3c 절이 COMPOSE_FILE 로 끼운다
cp deploy/env.example "$OUT/backend.env.example"

# ── udev ──────────────────────────────────────────────────────────────────
# ⚠ CAN 규칙 파일이 아니라 **만드는 도구**를 싣는다 (release.sh 의 같은 주석 참고)
cp deploy/udev/list-can-adapters.py "$OUT/udev/"
cp backend/udev/99-realsense-libusb.rules "$OUT/udev/"

# `install-daemons.sh` 는 이제 tarball 안이라 여기서 못 만진다 — 실행 비트는
# tar 가 보존하고, 푸는 쪽(`apply.sh` 3절)이 그대로 쓴다.
chmod +x "$OUT/apply.sh"
echo "· 호스트 코드 준비: $(du -sh "$OUT" | cut -f1)  ($OUT)"
