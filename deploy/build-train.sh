#!/usr/bin/env bash
# 학습 전용 이미지를 굽는다 — full(스택 포함)·slim(부팅 때 설치) 두 변종
# (feature/vast-training.md §4). 빌드 머신에서 돈다.
#
# 사용:  ./deploy/build-train.sh [--only full|slim] [--cuda cu126] [--push] [--date YYYYMMDD]
#
#   태그:  <레지스트리>/piper-train:<변종>-<lerobot>-<cuda>-<날짜>   (고정 — job 레코드에 적는다)
#          <레지스트리>/piper-train:<변종>-<cuda>                     (움직이는 태그 — 손으로 쓸 때)
#
# ⚠ **push 는 `--push` 를 줄 때만.** 릴리스와 같은 규칙 — 레지스트리에 올리는 건 사람이 정한다.
#   레지스트리는 PIPER_REGISTRY (기본 ghcr.io/wego-robotics). Vast 가 인증 없이 pull 하려면
#   GHCR 패키지가 **공개**여야 한다 — 처음 push 한 뒤 GitHub 에서 visibility 를 public 으로.
#
# ⚠ CUDA 빌드(--cuda)는 태그에 들어간다. cu126 은 드라이버 12.6 이상 호스트, RTX 50 계열은
#   cu128 이 필요하다. 인스턴스 안의 env-check.sh 가 어느 쪽인지 말해 준다.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

REGISTRY="${PIPER_REGISTRY:-ghcr.io/wego-robotics}"
NAME="piper-train"
CUDA="cu126"; ONLY=""; PUSH=0; DATE="$(date +%Y%m%d)"
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift ;;
    --cuda) CUDA="$2"; shift ;;
    --date) DATE="$2"; shift ;;
    --push) PUSH=1 ;;
    *) echo "사용: $0 [--only full|slim] [--cuda cu126] [--push] [--date YYYYMMDD]"; exit 1 ;;
  esac
  shift
done

# 핀은 install-stack.sh 한 곳에 있다 — 태그의 lerobot 버전도 거기서 읽는다.
LEROBOT="$(sed -n 's/^LEROBOT="${LEROBOT:-\(.*\)}"$/\1/p' deploy/train/install-stack.sh)"
[ -n "$LEROBOT" ] || { echo "✗ install-stack.sh 에서 LEROBOT 핀을 못 읽었다"; exit 1; }

VARIANTS=(full slim)
[ -n "$ONLY" ] && VARIANTS=("$ONLY")

BUILT=()
for v in "${VARIANTS[@]}"; do
  case "$v" in full|slim) ;; *) echo "✗ 변종은 full 또는 slim: $v"; exit 1 ;; esac
  TAG="$REGISTRY/$NAME:$v-$LEROBOT-$CUDA-$DATE"
  MOVING="$REGISTRY/$NAME:$v-$CUDA"
  echo "· 굽는다: $TAG  ($v, $CUDA)"
  t0=$(date +%s)
  docker build -f deploy/train/Dockerfile \
      --build-arg "VARIANT=$v" --build-arg "TORCH_CUDA=$CUDA" --build-arg "PIPER_TRAIN_IMAGE=$TAG" \
      -t "$TAG" -t "$MOVING" .
  # ⚠ 도커가 보고하는 크기는 저장소 방식에 따라 들쭉날쭉하다(containerd 스토어에서 같은 이미지가
  #   1.0GB 로도 0.3GB 로도 나왔다). pull 량은 `docker save | gzip | wc -c` 실측 — env-check.sh 의 기본값.
  size="$(docker image inspect -f '{{.Size}}' "$TAG" | awk '{printf "%.1fGB", $1/1e9}')"
  echo "  됐다: $TAG  (도커 보고 크기 $size, $(( $(date +%s) - t0 ))초)"
  BUILT+=("$TAG")
  if [ $PUSH = 1 ]; then
    docker push -q "$TAG"
    docker push -q "$MOVING"
    echo "  → push: $TAG (+ $MOVING)"
  fi
done

echo
echo "만든 이미지:"
for t in "${BUILT[@]}"; do echo "  $t"; done
[ $PUSH = 1 ] || echo "(push 안 함 — 올리려면 --push)"
echo
echo "인스턴스에서 고르기: 아무 이미지로 한 번 띄운 뒤  /opt/piper/env-check.sh  가 회선을 재서 full/slim 을 추천한다."
echo "  vastai create instance <offer> --image ${BUILT[0]} --disk 40 --ssh --direct --label piper-v0 --onstart-cmd '/opt/piper/bootstrap.sh'"
