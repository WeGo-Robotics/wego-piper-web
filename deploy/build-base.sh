#!/usr/bin/env bash
# 베이스 이미지(서드파티 스택)를 굽는다.
#
# ## 왜 따로인가
#
# 이 구간이 7.3GB 이고 릴리스마다 바뀌지 않는다. 앱 이미지와 한 파일로 묶여
# 있으면 우리 코드 한 줄에 이것까지 다시 구울 위험이 있고, 실제로 그랬다 —
# v0.3.8 과 v0.3.9 는 **레이어 24개 중 24개가 달랐다**(뜬 베이스 태그 탓).
#
# 릴리스마다 부를 일이 아니다. `release.sh` 가 없을 때만 알아서 부른다.
#
# 사용:  ./deploy/build-base.sh [--force]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="$(tr -d '[:space:]' < "$REPO/backend/BASE_VERSION")"
IMAGE="piper-web-base:$TAG"
DF="$REPO/backend/Dockerfile.base"

# ⚠ **태그만 보고 건너뛰면 낡은 베이스가 남는다.** `Dockerfile.base` 를 고치고
#   `BASE_VERSION` 을 안 올리면 아무도 눈치채지 못한 채 옛 스택 위에 앱이 얹힌다.
#   그래서 내용 해시를 라벨로 박아두고 그것까지 대조한다.
SHA="$(sha256sum "$DF" | cut -c1-12)"
HAVE="$(docker image inspect -f '{{index .Config.Labels "piper.base.sha"}}' "$IMAGE" 2>/dev/null || true)"

if [ "${1:-}" != "--force" ] && [ "$HAVE" = "$SHA" ]; then
  echo "베이스 그대로: $IMAGE ($SHA)"
  exit 0
fi
[ -n "$HAVE" ] && [ "$HAVE" != "$SHA" ] && echo "⚠ $IMAGE 이 낡았다 ($HAVE → $SHA) — 다시 굽는다"

# ⚠ **컨텍스트 없이 굽는다**(`-` = Dockerfile 만 stdin 으로). 베이스에 `COPY` 가
#   생기면 여기서 바로 실패한다 — 회사 코드가 섞이는 것을 빌드가 막아 준다.
#   이 이미지에 우리 코드가 없어야 공개 레지스트리에 올릴 수 있다.
echo "굽는다: $IMAGE  (서드파티 ~7GB, 20~40분)"
docker build --label "piper.base.sha=$SHA" -t "$IMAGE" - < "$DF"
echo "됐다: $IMAGE"
