#!/usr/bin/env bash
# 학습 이미지의 부팅 스크립트 — slim 이면 스택을 깔고, full 이면 바로 끝난다. **멱등이다.**
#
# 부르는 곳:
#   - Vast:      vastai create instance … --onstart-cmd "/opt/piper/bootstrap.sh"
#   - docker run: 이미지 CMD 가 부른다
# 두 곳에서 동시에 불려도 flock 으로 한 번만 돈다.
#
# 끝나면 /opt/piper/.ready 에 한 줄을 남긴다. SSHRunner(W1)는 이 파일을 보고 학습을
# 시작한다 — 없으면 "아직 설치 중" 이지 "고장" 이 아니다. 로그는 /opt/piper/bootstrap.log.
set -uo pipefail

READY=/opt/piper/.ready
LOG=/opt/piper/bootstrap.log
LOCK=/opt/piper/.bootstrap.lock

exec 9>"$LOCK"
flock 9

if [ -f "$READY" ]; then
    echo "학습 스택 준비됨: $(cat "$READY")"
    exit 0
fi

exec > >(tee -a "$LOG") 2>&1
echo "== 학습 스택 설치 시작 $(date -Is)  (TORCH_CUDA=${TORCH_CUDA:-cu126})"
t0=$(date +%s)
if TORCH_CUDA="${TORCH_CUDA:-cu126}" bash /opt/piper/install-stack.sh; then
    took=$(( $(date +%s) - t0 ))
    echo "$(date -Is) slim (bootstrap ${took}s)" > "$READY"
    echo "== 됐다: ${took}초"
else
    echo "== 실패 — $LOG 를 보라. 다시 돌리면 이어서 시도한다."
    exit 1
fi
