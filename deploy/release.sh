#!/usr/bin/env bash
# 릴리스 번들 하나를 만든다. 빌드 머신에서 돈다.
#
# ## 왜 이게 필요한가
#
# 배포 이력(RELEASE-CHECKLIST)이 말해 준다 — 15회 중 **12회가 이미지만**이었고
# 세 레이어 전부는 3회였다. 그런데 절차는 매번 사람이 "이번엔 어느 레이어가
# 필요한가"를 판단하게 했다. `v0.3.4 wheel(cam·rs) + backend` 같은 줄이 그
# 판단의 흔적이고, 판단은 틀릴 수 있다 — 빠뜨리면 **호스트에서 옛 코드가 돈다.**
#
# 그래서 여기서는 **직전 태그와의 diff 로 정한다.** 사람은 버전만 준다.
#
# 사용:  ./deploy/release.sh v0.3.9 [--dry-run]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VERSION="${1:-}"
DRY=0; [ "${2:-}" = "--dry-run" ] && DRY=1
[ -n "$VERSION" ] || { echo "사용: $0 vX.Y.Z [--dry-run]"; exit 1; }

PREV="$(git tag --sort=-v:refname | head -1)"
[ -n "$PREV" ] || { echo "✗ 직전 태그가 없습니다 — 첫 배포는 --all 로 하세요"; exit 1; }

# ── 무엇이 바뀌었나 ───────────────────────────────────────────────────────
# 경로 → 레이어. 겹치는 것이 있다(`bus/`·`shm/`·`robot/` 은 이미지에도 들어가고
# 데몬 wheel 로도 나간다) — 겹치는 게 맞다. 같은 코드를 두 곳이 쓴다.
# ⚠ **테스트와 문서는 레이어를 건드리지 않는다.** 이미지에 들어가긴 하지만
#   도는 것을 바꾸지 않는데, 그것 때문에 11GB 를 다시 굽고 3.4GB 를 전송하는 건
#   낭비다. 실측: v0.3.5 는 `backend/tests/test_system_messages.py` 하나 때문에
#   backend 이미지가 필요하다고 판정됐는데, 그 릴리스는 frontend 만 올렸다.
CHANGED="$(git diff --name-only "$PREV" HEAD \
  | grep -vE '(^|/)tests?/|_test\.py$|\.md$|^(refactor|feature|docs)/')"
need_backend=0 need_frontend=0 need_wheels=0 need_daemons=0
WHEEL_PKGS=()
while read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    backend/*|wrapper/*|policies/*|act_aux/*|phase/*|vendor/*) need_backend=1 ;;
  esac
  case "$f" in
    frontend/*) need_frontend=1 ;;
    daemons/*|deploy/systemd/*|deploy/install-daemons.sh) need_daemons=1 ;;
  esac
  # `bus/ shm/ robot/` 은 **양쪽**이다 — 이미지 안에도 들어가고 호스트 venv 에도 깔린다
  case "$f" in
    bus/*)   need_backend=1; need_wheels=1; WHEEL_PKGS+=(bus) ;;
    shm/*)   need_backend=1; need_wheels=1; WHEEL_PKGS+=(shm) ;;
    robot/*) need_backend=1; need_wheels=1; WHEEL_PKGS+=(robot) ;;
    cam/*)   need_wheels=1; WHEEL_PKGS+=(cam) ;;   # 호스트 전용 — 이미지엔 없다
    rs/*)    need_wheels=1; WHEEL_PKGS+=(rs)  ;;
  esac
done <<< "$CHANGED"
# 중복 제거
if [ ${#WHEEL_PKGS[@]} -gt 0 ]; then
  mapfile -t WHEEL_PKGS < <(printf '%s\n' "${WHEEL_PKGS[@]}" | sort -u)
fi

echo "릴리스 $VERSION  (직전 $PREV, 파일 $(echo "$CHANGED" | grep -c .) 개 변경)"
echo "  backend 이미지 : $([ $need_backend  = 1 ] && echo 예 || echo 아니오)"
echo "  frontend 이미지: $([ $need_frontend = 1 ] && echo 예 || echo 아니오)"
echo "  데몬 wheel     : $([ $need_wheels   = 1 ] && echo "${WHEEL_PKGS[*]}" || echo 아니오)"
echo "  데몬 소스·유닛 : $([ $need_daemons  = 1 ] && echo 예 || echo 아니오)"
[ $DRY = 1 ] && exit 0

if [ $((need_backend + need_frontend + need_wheels + need_daemons)) -eq 0 ]; then
  echo "✗ 바뀐 것이 없습니다 — 배포할 이유가 없습니다"; exit 1
fi

OUT="$REPO/dist/$VERSION"
rm -rf "$OUT"; mkdir -p "$OUT"

# ── 이미지 ────────────────────────────────────────────────────────────────
IMAGES=()
[ $need_backend  = 1 ] && IMAGES+=(backend)
[ $need_frontend = 1 ] && IMAGES+=(frontend)
if [ ${#IMAGES[@]} -gt 0 ]; then
  echo "· 이미지 빌드: ${IMAGES[*]}"
  docker compose build "${IMAGES[@]}"
  TAGS=()
  for s in "${IMAGES[@]}"; do
    docker tag "piper-web-$s:latest" "piper-web-$s:$VERSION"
    TAGS+=("piper-web-$s:$VERSION")
  done
  echo "· 이미지 저장 (몇 GB, 몇 분)"
  docker save "${TAGS[@]}" | gzip > "$OUT/images.tar.gz"
fi

# ── 데몬 wheel ────────────────────────────────────────────────────────────
if [ $need_wheels = 1 ]; then
  echo "· wheel 빌드: ${WHEEL_PKGS[*]}"
  mkdir -p "$OUT/wheels"
  for p in "${WHEEL_PKGS[@]}"; do
    python3 -m pip wheel --no-deps -q -w "$OUT/wheels" "./$p"
  done
  # ⚠ 저장소 안에서 빌드하면 `<pkg>/build/` 가 남는다. git 에는 안 잡히지만 지저분하다.
  rm -rf "${WHEEL_PKGS[@]/%//build}" 2>/dev/null || true
fi

# ── 데몬 소스 + 유닛 ──────────────────────────────────────────────────────
# ⚠ 유닛·설치 스크립트는 **wheel 이 아니라 파일 그대로** 간다 — daemons/ 는
#   패키지가 아니라 엔트리포인트다.
if [ $need_daemons = 1 ]; then
  echo "· 데몬 소스 묶기"
  tar czf "$OUT/daemons.tar.gz" daemons deploy/systemd deploy/install-daemons.sh
fi

# ── compose + env 예시 — **항상 넣는다** ──────────────────────────────────
# ⚠ 이게 없으면 호스트가 컨테이너를 띄울 수가 없다. 레이어 판정과 무관하게
#   늘 필요하고 몇 KB 라, "바뀌었을 때만" 으로 아낄 이유가 없다.
#
# ⚠ `docker-compose.override.yml` 은 **안 보낸다.** 그건 그 호스트의 사정이다 —
#   192.168.0.120 은 :80 을 WMS 가, :8080 을 다른 node 앱이 쓰고 있어 8081 로
#   빼 두었다. 번들이 덮으면 그 설정이 조용히 사라지고 포트 충돌로 안 뜬다.
cp docker-compose.yml "$OUT/"
cp deploy/env.example "$OUT/backend.env.example"

# ── 적용 스크립트와 매니페스트 ────────────────────────────────────────────
cp "$REPO/deploy/apply.sh" "$OUT/apply.sh"; chmod +x "$OUT/apply.sh"
cat > "$OUT/manifest.txt" <<EOF
version=$VERSION
prev=$PREV
built_at=$(date -Is)
images=$([ ${#IMAGES[@]} -gt 0 ] && echo "${IMAGES[*]}" || echo "")
wheels=$([ $need_wheels = 1 ] && echo "${WHEEL_PKGS[*]}" || echo "")
daemons=$([ $need_daemons = 1 ] && echo yes || echo "")
EOF

BUNDLE="$REPO/dist/piper-web-$VERSION.tar.gz"
tar czf "$BUNDLE" -C "$REPO/dist" "$VERSION"
echo
echo "번들: $BUNDLE  ($(du -h "$BUNDLE" | cut -f1))"
echo
echo "호스트에서:"
echo "  scp $BUNDLE <호스트>:~/"
echo "  tar xzf piper-web-$VERSION.tar.gz && ./$VERSION/apply.sh"
