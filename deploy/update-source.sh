#!/usr/bin/env bash
# 소스로 도는 기계의 업데이트 — 웹의 [적용] 이 unitd 를 통해 이걸 돌린다
# (feature/version-update.md §4, 4단계). 배포 기계는 piper-install.sh + apply.sh.
#
# 사용:  deploy/update-source.sh vX.Y.Z      # 그 태그로
#        deploy/update-source.sh             # origin/master 최신으로 (fast-forward 만)
#
# ⚠ 게이트웨이 유닛을 재시작하는 순간 이 스크립트를 부른 웹 요청은 끊긴다 —
#   그래서 unitd 의 일시 유닛으로 돈다(호출자와 수명이 다르다).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
TARGET="${1:-}"
echo "1. 소스"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "✗ 작업 트리에 커밋 안 된 변경이 있습니다 — 업데이트로 덮지 않습니다"; git status --short | head; exit 1
fi
git fetch -q --tags origin
if [ -n "$TARGET" ]; then
  git rev-parse -q --verify "refs/tags/$TARGET" >/dev/null || { echo "✗ 태그가 없습니다: $TARGET"; exit 1; }
  git checkout -q "$TARGET"
else
  git pull -q --ff-only origin master
fi
echo "  · $(git describe --tags --always)"
echo "2. 설치 (deploy/install.sh)"
"$REPO/deploy/install.sh"
echo "3. 재시작 — 유닛은 기동 시점의 코드로 돈다"
# 돌고 있는 piper-* 유닛만. 자기 자신(일시 유닛)은 이름이 다르다. estopd 는 마지막 —
# 이 순간은 활동이 없다(게이트웨이가 적용 전에 막는다).
units="$(systemctl --user list-units 'piper-*' --plain --no-legend 2>/dev/null | awk '{print $1}' | grep -v "piper-update" || true)"
for u in $units; do
  case "$u" in piper-estopd.service) continue ;; esac
  systemctl --user restart "$u" && echo "  · $u"
done
echo "$units" | grep -q "piper-estopd.service" && systemctl --user restart piper-estopd.service && echo "  · piper-estopd.service"
echo "끝: $(git describe --tags --always)"
