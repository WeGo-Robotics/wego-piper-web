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

# ── Vast 에서 SSH 가 되게 만드는 두 가지 ────────────────────────────────────
#
# ⚠ **이게 없으면 이 이미지로 띄운 인스턴스에 아무도 접속할 수 없다.** 2026-09-16
# 실기에서 둘 다 실제로 막혔다(feature/vast-training.md §12-1).
#
# ① Vast 가 심는 authorized_keys 의 권한이 우리 sshd 를 통과하지 못한다. 이 이미지는
#    openssh-server 를 담고 Debian 기본 sshd_config(StrictModes yes)를 쓴다. 실기 로그:
#      Authentication refused: bad ownership or modes for file /root/.ssh/authorized_keys
#    키는 맞는데(sshd 가 지문까지 찍는다) 권한 때문에 거부된다.
#
# ② Vast 가 /root/.bashrc 에서 **모든 SSH 세션을 tmux 로 감싼다.** bash 는 sshd 로
#    불릴 때 비대화식이어도 .bashrc 를 읽으므로 `ssh host 'cmd'` 가 통째로 막힌다
#    (`duplicate session: ssh_tmux`). SSHRunner 는 그 형태로만 동작하므로, 끄지 않으면
#    원격 학습이 한 줄도 못 돈다. `.no_auto_tmux` 는 Vast 가 문서화한 공식 스위치다.
#
piper_fix_ssh() {
    mkdir -p /root/.ssh
    chown -R root:root /root/.ssh 2>/dev/null || true
    chmod 700 /root/.ssh 2>/dev/null || true
    [ -f /root/.ssh/authorized_keys ] && chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true
    : > /root/.no_auto_tmux 2>/dev/null || true
}
piper_fix_ssh

# ⚠ **키 주입 시점을 우리가 모른다** — onstart 가 주입보다 먼저 돌 수 있다. 그래서 한 번
#   고치고 끝내지 않고 3분 동안 5초마다 다시 본다. 멱등이라 몇 번 돌아도 무해하다.
#
# ⚠ **락 fd 를 물려주지 않는다 (`9>&-`).** 배경 루프가 fd 9 를 물려받으면 루프가 도는
#   3분 내내 `flock` 이 안 풀린다. bootstrap 은 두 곳에서 불릴 수 있고(Vast onstart +
#   이미지 CMD — 파일 머리말 참조) 그러면 두 번째가 정확히 그만큼 멈춘다. SSH 교정은
#   스택 설치와 아무 상관이 없으므로 락을 나눠 가질 이유가 없다.
nohup sh -c 'i=0; while [ "$i" -lt 36 ]; do
    mkdir -p /root/.ssh
    chown -R root:root /root/.ssh 2>/dev/null
    chmod 700 /root/.ssh 2>/dev/null
    [ -f /root/.ssh/authorized_keys ] && chmod 600 /root/.ssh/authorized_keys 2>/dev/null
    : > /root/.no_auto_tmux 2>/dev/null
    i=$((i+1)); sleep 5
done' 9>&- >/dev/null 2>&1 &

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
