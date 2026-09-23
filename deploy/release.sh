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
# 사용:  ./deploy/release.sh v0.3.10 [--dry-run] [--offline]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VERSION="${1:-}"
DRY=0; OFFLINE=0; REGISTRY=""

# ⚠ **어느 레지스트리에 올릴지는 스크립트가 안다** — 부르는 사람이 매번 기억할 일이 아니다.
#
#   v0.5.5 를 `PIPER_REGISTRY=piper-build:5000` 으로만 올렸더니 GHCR 에는 안 갔고,
#   거기서 받는 호스트(.120·.44)의 "새 버전 확인" 이 계속 v0.5.4 를 최신이라 답했다.
#   화면은 맞는 말을 하고 있었다 — 틀린 것은 릴리스였다. 환경변수 하나에 결과가 갈리는데
#   그 선택이 스크립트 **밖에** 있었던 것이 원인이다.
#
#   그래서 기본은 **둘 다**다. 맨 앞이 주 레지스트리이고, 매니페스트의 `registry=` 로
#   들어가 호스트가 업데이트를 물어볼 곳이 된다.
#
# ⚠ 하나만 올리고 싶으면 `PIPER_REGISTRY` 로 덮는다(예전 사용법 그대로). 다만 그러면
#   나머지 레지스트리에서 받는 호스트는 이 버전을 **영영 못 본다.**
# ⚠ 배열은 **여기서** 만든다. 뒤의 확인 블록이 `set -u` 아래에서 이걸 읽으므로,
#   push 분기 안에서만 만들면 오프라인 경로에서 "unbound variable" 로 죽는다.
PUSHED=(); SKIPPED=()
DEFAULT_REGISTRIES="ghcr.io/wego-robotics piper-build:5000"
if [ -n "${PIPER_REGISTRY:-}" ]; then
  REGISTRIES="$PIPER_REGISTRY"
else
  REGISTRIES="${PIPER_REGISTRIES:-$DEFAULT_REGISTRIES}"
fi
for a in "${@:2}"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    # ⚠ 현장 USB 배포용. 레지스트리가 떠 있어도 tar 를 만든다.
    --offline) OFFLINE=1 ;;
  esac
done
[ -n "$VERSION" ] || { echo "사용: $0 vX.Y.Z [--dry-run] [--offline]"; exit 1; }

# ⚠ **지금 굽는 버전은 빼고 고른다.** 절차가 "태그 먼저, 그다음 빌드" 인데,
#   그냥 최신 태그를 잡으면 **방금 찍은 그 태그**가 직전 버전이 된다 — diff 가
#   비고 "바뀐 것이 없다" 가 된다. 실제로 v0.4.0 에서 그렇게 막혔다.
PREV="$(git tag --sort=-v:refname | grep -vFx "$VERSION" | head -1)"
[ -n "$PREV" ] || { echo "✗ 직전 태그가 없습니다 — 첫 배포는 --all 로 하세요"; exit 1; }

# ── 이 번호가 이미 나갔나 (R10) ───────────────────────────────────────────
# ⚠ **적용한 호스트가 있으면 같은 번호를 다시 쓰면 안 된다.** 호스트의 "새 버전 확인"은
#   버전 **문자열**을 견준다 — 같은 번호로 다시 끊으면 `v0.5.5 == v0.5.5` 라 "최신입니다"
#   라고 답하고, 그 기계는 고친 것을 **스스로는 영영 못 받는다**. 사람이 그 호스트마다
#   손으로 다시 깔아야 한다. 실제로 v0.5.5 에서 그랬다(.120 이 이미 받은 뒤 3차 발행).
#   아직 아무도 안 받았으면 다시 끊어도 된다 — 그래서 막지 않고 **말만 한다**.
if [ $DRY = 0 ] && [ $OFFLINE = 0 ]; then
  for reg_ in $REGISTRIES; do
    case "$reg_" in *:[0-9]*) continue ;; esac      # 사설은 건너뛴다(사람이 받는 곳이 아니다)
    if docker manifest inspect "$reg_/piper-web-backend:$VERSION" >/dev/null 2>&1; then
      echo "⚠ $VERSION 는 $reg_ 에 **이미 있다** — 다시 끊는 것이다."
      echo "  이미 이 번호를 적용한 호스트는 '최신입니다' 를 보고 **스스로 안 받는다.**"
      echo "  그 기계마다 ./piper-install.sh 를 손으로 돌려야 한다."
      echo "  아직 아무도 안 받았다면 그대로 진행해도 된다. 받은 곳이 있으면 번호를 올려라."
      break
    fi
  done
fi

# ── 무엇이 바뀌었나 ───────────────────────────────────────────────────────
# 경로 → 레이어. 겹치는 것이 있다(`bus/`·`shm/`·`robot/` 은 이미지에도 들어가고
# 데몬 wheel 로도 나간다) — 겹치는 게 맞다. 같은 코드를 두 곳이 쓴다.
# ⚠ **테스트와 문서는 레이어를 건드리지 않는다.** 이미지에 들어가긴 하지만
#   도는 것을 바꾸지 않는데, 그것 때문에 11GB 를 다시 굽고 3.4GB 를 전송하는 건
#   낭비다. 실측: v0.3.5 는 `backend/tests/test_system_messages.py` 하나 때문에
#   backend 이미지가 필요하다고 판정됐는데, 그 릴리스는 frontend 만 올렸다.
# ⚠ **`|| true` 가 없으면 조용히 죽는다.** 걸러낸 결과가 비면 `grep` 이 1 을
#   돌려주고, `set -e` 아래의 명령 치환은 그걸로 스크립트를 끝낸다 — 에러도
#   메시지도 없이. 실제로 v0.4.0 에서 아무 출력 없이 종료됐다.
CHANGED="$(git diff --name-only "$PREV" HEAD \
  | grep -vE '(^|/)tests?/|_test\.py$|\.md$|^(refactor|feature|docs)/' || true)"
need_backend=0 need_frontend=0 need_wheels=0 need_daemons=0
WHEEL_PKGS=()
while read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    backend/*|wrapper/*|policies/*|act_aux/*|phase/*|vendor/*) need_backend=1 ;;
  esac
  case "$f" in
    frontend/*) need_frontend=1 ;;
    # ⚠ `daemons/` 는 **양쪽**이다 — 호스트 유닛으로도 돌고, 컨테이너가
    #   `/app/daemons` 에서 subprocess 로도 실행한다(비전·학습). 한쪽만 올리면
    #   컨테이너 안 데몬이 옛 코드로 돈다.
    daemons/*) need_daemons=1; need_backend=1 ;;
    deploy/systemd/*|deploy/install-daemons.sh) need_daemons=1 ;;
  esac
  # ⚠ **호스트 코드는 이미지 안(/opt/piper-host)으로 간다.** `stage-hostside.sh` 가 싣는 것
  #   (apply.sh·piper-install.sh·compose 파일·udev·wheel 전부)이 바뀌면 backend 이미지를 다시
  #   구워야 온라인 설치(`piper-install.sh` → `docker pull`)가 새 것을 받는다. v0.4.14 에서
  #   apply.sh 와 compose 조각만 바뀌자 "바뀐 것이 없다"로 판정돼 릴리스가 막혔다. cam·rs·sim
  #   wheel 도 같은 이유 — 이미지의 매니페스트·wheel 이 옛것이면 온라인 경로는 새 wheel 을
  #   알지도 못한다(v0.4.13 은 backend 가 우연히 같이 바뀌어 괜찮았을 뿐이다).
  case "$f" in
    deploy/apply.sh|deploy/piper-install.sh|deploy/piper-uninstall.sh|deploy/pull-progress.py|deploy/install-shortcuts.sh|deploy/piper-studio.sh|deploy/piper-doctor.sh|frontend/public/favicon.svg|deploy/update-source.sh|deploy/stage-hostside.sh|deploy/env.example|deploy/udev/*|deploy/sudoers/*|docker-compose.yml|docker-compose.*.yml|cam/*|rs/*|sim/*) need_backend=1 ;;
  esac
  # `bus/ shm/ robot/` 은 **양쪽**이다 — 이미지 안에도 들어가고 호스트 venv 에도 깔린다
  case "$f" in
    bus/*)   need_backend=1; need_wheels=1; WHEEL_PKGS+=(bus) ;;
    shm/*)   need_backend=1; need_wheels=1; WHEEL_PKGS+=(shm) ;;
    robot/*) need_backend=1; need_wheels=1; WHEEL_PKGS+=(robot) ;;
    cam/*)   need_wheels=1; WHEEL_PKGS+=(cam) ;;   # 호스트 전용 — 이미지엔 없다
    rs/*)    need_wheels=1; WHEEL_PKGS+=(rs)  ;;
    # so101 도 **양쪽**이다 — 관절 매핑 표를 게이트웨이 릴레이와 컨테이너 안 녹화
    # 프로세스(so101_leader_shm)가 읽는다. sim 은 호스트 전용 (게이트웨이는 버스만 쓴다)
    so101/*) need_backend=1; need_wheels=1; WHEEL_PKGS+=(so101) ;;
    sim/*)   need_wheels=1; WHEEL_PKGS+=(sim) ;;
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

# ⚠ **값에 따옴표를 씌운다.** `apply.sh` 가 `source` 하는데, `images=backend frontend`
#   는 셸에서 "images=backend 를 환경으로 두고 frontend 를 실행" 이다 —
#   실제로 `frontend: command not found` 로 깨졌다.
# ⚠ 이미지 안과 번들에 **같은 것**이 들어가야 한다. 두 군데서 따로 만들면 갈린다.
write_manifest() {
  cat > "$1" <<EOF
version="$VERSION"
prev="$PREV"
built_at="$BUILT_AT"
images="$([ ${#IMAGES[@]} -gt 0 ] && echo "${IMAGES[*]}" || echo "")"
wheels="$([ $need_wheels = 1 ] && echo "${WHEEL_PKGS[*]}" || echo "")"
daemons="$([ $need_daemons = 1 ] && echo yes || echo "")"
registry="$REGISTRY"
EOF
}
BUILT_AT="$(date -Is)"

OUT="$REPO/dist/$VERSION"
rm -rf "$OUT"; mkdir -p "$OUT"

# ⚠ 매니페스트가 **이미지 안으로** 들어가므로, 어디서 받을지를 굽기 전에 정해야
#   한다. 나중에 정하면 이미지 안의 매니페스트가 비어 나간다.
# 매니페스트에 적히는 것은 **주** 레지스트리(목록의 맨 앞) — 호스트가 업데이트를 묻는 곳이다
PRIMARY="${REGISTRIES%% *}"
if [ $OFFLINE = 0 ]; then REGISTRY="$PRIMARY"; fi

# ── 이미지 ────────────────────────────────────────────────────────────────
IMAGES=()
[ $need_backend  = 1 ] && IMAGES+=(backend)
[ $need_frontend = 1 ] && IMAGES+=(frontend)
if [ ${#IMAGES[@]} -gt 0 ]; then
  # ⚠ 앱 이미지는 베이스 위에 얹힌다 — 없거나 낡으면 compose build 가 죽는다.
  #   릴리스마다 굽는 게 아니라, 없을 때만 굽는다(build-base.sh 가 판단).
  case " ${IMAGES[*]} " in *" backend "*) "$REPO/deploy/build-base.sh" ;; esac
  # ⚠ 호스트 코드는 **이미지 안에** 들어간다. 이미지를 굽기 전에 모아야 한다 —
  #   순서가 뒤집히면 옛 데몬이 실린 이미지가 나가는데 아무 에러가 안 난다.
  # 태그를 wheel 버전과 이미지 ENV 에 박는다 — 화면의 [버전] 이 그걸 읽는다
  PIPER_VERSION="$VERSION" "$REPO/deploy/stage-hostside.sh"
  write_manifest "$REPO/.hostside/manifest.txt"
  echo "· 이미지 빌드: ${IMAGES[*]}"
  # 태그를 이미지 ENV 로 — compose 의 build.args 가 환경에서 받는다 (feature/version-update.md)
  PIPER_VERSION="$VERSION" docker compose build "${IMAGES[@]}"
  TAGS=()
  for s in "${IMAGES[@]}"; do
    docker tag "piper-web-$s:latest" "piper-web-$s:$VERSION"
    TAGS+=("piper-web-$s:$VERSION")
  done
  # ── 레지스트리로 보낼 수 있으면 tar 를 안 만든다 ───────────────────────
  # ⚠ `docker save` 는 **부모 레이어를 전부 담는다**(측정: 부모 28MB → 한 줄 얹은
  #   자식 28MB). 그래서 베이스/앱을 갈라놔도 tar 인 한 매번 3.46GB 가 통째로 간다.
  #   레지스트리는 호스트에 없는 레이어만 준다 — 그게 이 분기의 전부다.
  #
  # ⚠ **오프라인 경로는 남긴다.** 현장에 USB 로 들고 가는 배포가 실재한다.
  #   `PIPER_REGISTRY` 가 비었거나 `--offline` 이면 예전처럼 tar 를 만든다.
  if [ $OFFLINE = 0 ]; then
    # ⚠ **주소가 둘인 데는 이유가 있다.** 도커는 `127.0.0.0/8` 만 기본으로 평문
    #   레지스트리로 인정한다. 빌드 머신이 자기 LAN IP 로 밀면
    #   "server gave HTTP response to HTTPS client" 로 거부당하므로, 미는 쪽은
    #   `localhost` 를 쓴다 — 그러면 빌드 머신에는 daemon.json 설정이 필요 없다.
    #   매니페스트에는 **호스트가 받을 주소**(LAN IP)를 적는다. 같은 레지스트리라
    #   다이제스트는 동일하다.
    # ⚠ **공개 레지스트리와 사설 평문 레지스트리는 다르게 다룬다.**
    #   `ghcr.io/...` 처럼 포트가 없으면 HTTPS 공개 레지스트리다 — `localhost`
    #   우회도, 평문 점검도 하면 안 된다(`localhost:ghcr.io/...` 라는 엉뚱한
    #   주소가 만들어진다). 살아 있는지는 push 가 말해 준다.
    for reg in $REGISTRIES; do
      if [[ "$reg" == *:[0-9]* ]]; then
        # 사설 평문: 도커가 `127.0.0.0/8` 만 기본으로 믿으므로 미는 쪽은 localhost
        push_to="${PIPER_REGISTRY_PUSH:-localhost:${reg##*:}}"
        if ! curl -fsS --max-time 3 "http://$push_to/v2/" >/dev/null 2>&1; then
          # ⚠ **여기서 죽지 않는다.** 사설 레지스트리는 현장망용이라 빌드 머신에서
          #   늘 떠 있지는 않다. 한쪽이 없다고 릴리스 전체를 멈추면, 그걸 피하려고
          #   사람이 다시 `PIPER_REGISTRY` 하나만 주게 되고 — 그게 이 사고의 원인이었다.
          #   대신 **건너뛴 것을 끝에서 크게 말한다.**
          echo "  ⚠ $reg 은 지금 없습니다 (push_to=$push_to) — 건너뜁니다"
          SKIPPED+=("$reg")
          continue
        fi
      else
        push_to="$reg"
      fi
      echo "· 레지스트리로 push: $push_to  (호스트가 받을 주소: $reg)"
      for s_ in "${IMAGES[@]}"; do
        docker tag "piper-web-$s_:$VERSION" "$push_to/piper-web-$s_:$VERSION"
        docker push -q "$push_to/piper-web-$s_:$VERSION"
        # ⚠ **`latest` 도 민다.** `piper-install.sh` 는 인자가 없으면 `latest` 를
        #   받는다 — README 의 기본 명령이 그것이다. 버전 태그만 밀면 사용자가
        #   `./piper-install.sh` 를 그냥 쳤을 때 **"not found" 로 끝난다.**
        #   실기에서 그렇게 막혔다.
        docker tag "piper-web-$s_:$VERSION" "$push_to/piper-web-$s_:latest"
        docker push -q "$push_to/piper-web-$s_:latest"
        echo "  → piper-web-$s_:$VERSION (+ latest)"
      done
      PUSHED+=("$reg")
    done
    [ ${#PUSHED[@]} -gt 0 ] || { echo "✗ 어느 레지스트리에도 못 올렸습니다"; exit 1; }
  else
    echo "· 이미지 저장 (몇 GB, 몇 분)"
    docker save "${TAGS[@]}" | gzip > "$OUT/images.tar.gz"
  fi
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
  # ⚠ `__pycache__` 는 빼고 묶는다. 빌드 머신은 py3.13, 호스트는 py3.12 라
  #   그 `.pyc` 는 호스트에서 쓰이지도 않는다 — 번들만 지저분해진다.
  # piper-install.sh 도 싣는다 — 호스트의 unitd 가 웹 [받기] 를 이걸로 돌린다
  # (`current/deploy/piper-install.sh --pull-only vX`, feature/version-update.md §4)
  tar czf "$OUT/daemons.tar.gz" --exclude='__pycache__' --exclude='*.pyc' \
      daemons deploy/systemd deploy/install-daemons.sh deploy/piper-install.sh deploy/update-source.sh
fi

# ── compose + env 예시 — **항상 넣는다** ──────────────────────────────────
# ⚠ 이게 없으면 호스트가 컨테이너를 띄울 수가 없다. 레이어 판정과 무관하게
#   늘 필요하고 몇 KB 라, "바뀌었을 때만" 으로 아낄 이유가 없다.
#
# ⚠ `docker-compose.override.yml` 은 **안 보낸다.** 그건 그 호스트의 사정이다 —
#   로봇 호스트 은 :80 을 WMS 가, :8080 을 다른 node 앱이 쓰고 있어 8081 로
#   빼 두었다. 번들이 덮으면 그 설정이 조용히 사라지고 포트 충돌로 안 뜬다.
cp docker-compose.yml "$OUT/"
cp docker-compose.nogpu.yml "$OUT/"    # GPU 없는 호스트 조각 — apply.sh 3c 절이 COMPOSE_FILE 로 끼운다
cp deploy/piper-uninstall.sh "$OUT/"   # 제거도 번들에서 — 설치가 만든 것을 되돌린다 (데이터는 남긴다)
# 바탕화면 바로가기 (feature/field-deployment.md §5) — 이미지 경로(stage-hostside.sh)와 같은 넷
cp deploy/install-shortcuts.sh "$OUT/"
cp deploy/piper-studio.sh "$OUT/"
cp deploy/piper-doctor.sh "$OUT/"
cp frontend/public/favicon.svg "$OUT/piper-studio.svg"
cp deploy/env.example "$OUT/backend.env.example"

# ── udev 규칙 — **항상 넣는다** ───────────────────────────────────────────
# ⚠ 이게 빠져 있었다. 새 머신에서는 RealSense 가 libusb 로 장치를 못 열어
#   **카메라가 0개**로 잡히고, CAN 은 `can0`/`can1` 로 붙어 **저장된 팔 등록이
#   반대 팔을 가리킨다.** 셋 합쳐 8KB 라 레이어 판정에서 뺄 이유가 없다.
#
# ⚠ CAN 규칙에는 **이 배선의 시리얼이 박혀 있다.** 어댑터가 다른 머신에 그대로
#   깔면 아무 줄도 매칭되지 않아 이름이 조용히 안 붙는다 — 그래서
#   `list-can-adapters.py` 를 같이 보내고 `apply.sh` 가 꽂힌 어댑터와 대조한다.
# ⚠ **CAN 규칙 파일은 안 보낸다.** 시리얼은 어댑터마다 다르므로 남의 시리얼이
#   든 규칙은 받는 머신에서 **아무 줄도 매칭되지 않는다** — 그런데 udev 는 그걸
#   에러로 안 쳐서 증상이 "팔 0개" 뿐이다. 대신 **만드는 도구**를 보낸다.
#   RealSense 규칙은 vendor/product ID 라 머신과 무관하므로 그대로 보낸다.
mkdir -p "$OUT/udev"
cp backend/udev/99-realsense-libusb.rules "$OUT/udev/"
cp deploy/udev/list-can-adapters.py       "$OUT/udev/"

# ── 적용 스크립트와 매니페스트 ────────────────────────────────────────────
cp "$REPO/deploy/apply.sh" "$OUT/apply.sh"; chmod +x "$OUT/apply.sh"

write_manifest "$OUT/manifest.txt"

BUNDLE="$REPO/dist/piper-web-$VERSION.tar.gz"
tar czf "$BUNDLE" -C "$REPO/dist" "$VERSION"
echo
echo "번들: $BUNDLE  ($(du -h "$BUNDLE" | cut -f1))"
echo
echo "호스트에서:"
echo "  scp $BUNDLE <호스트>:~/"
echo "  tar xzf piper-web-$VERSION.tar.gz && ./$VERSION/apply.sh"

# ── 올라갔는지 **확인한다** ───────────────────────────────────────────────
#
# ⚠ push 가 조용히 끝났다고 거기 있는 것은 아니다. 그리고 한쪽 레지스트리만 올라가면
#   그쪽에서 안 받는 호스트는 **"최신입니다" 를 계속 본다** — v0.5.5 에서 실제로
#   그랬고, 화면은 맞는 말을 하고 있었다. 사람이 나중에 알아채는 대신 여기서 말한다.
if [ $OFFLINE = 0 ] && [ ${#PUSHED[@]} -gt 0 ]; then
  echo
  echo "· 레지스트리 확인"
  miss=0
  for reg in "${PUSHED[@]}"; do
    for s_ in "${IMAGES[@]}"; do
      if [[ "$reg" == *:[0-9]* ]]; then
        url="http://${PIPER_REGISTRY_PUSH:-localhost:${reg##*:}}/v2/piper-web-$s_/manifests/$VERSION"
        code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json' "$url" || echo 000)"
      else
        # 공개 레지스트리는 토큰이 필요할 수 있다 — `docker manifest inspect` 가 로그인을 쓴다
        code="$(docker manifest inspect "$reg/piper-web-$s_:$VERSION" >/dev/null 2>&1 && echo 200 || echo 000)"
      fi
      if [ "$code" = 200 ]; then echo "  ☑ $reg/piper-web-$s_:$VERSION"
      else echo "  ✗ $reg/piper-web-$s_:$VERSION 가 안 보입니다 ($code)"; miss=1; fi
    done
  done
  [ $miss = 0 ] || { echo "✗ 올렸다고 했는데 없는 것이 있습니다"; exit 1; }
fi

# ⚠ 건너뛴 레지스트리는 **끝에서 크게 말한다.** 그 레지스트리에서 받는 호스트는 이
#   버전을 못 본다 — 업데이트 확인이 옛 버전을 "최신" 이라 답하는 그 상태가 된다.
if [ ${#SKIPPED[@]} -gt 0 ]; then
  echo
  echo "⚠ 안 올라간 레지스트리: ${SKIPPED[*]}"
  echo "   거기서 받는 호스트는 $VERSION 을 못 봅니다 — 띄운 뒤 이 스크립트를 다시 돌리세요"
  echo "   (사설 레지스트리면: PIPER_REGISTRY_BIND=0.0.0.0 ./deploy/registry.sh)"
fi
