# syntax=docker/dockerfile:1
#
# 학습 전용 이미지 — 임대 GPU(Vast.ai 등)에서 `lerobot-train` 만 돌린다
# (feature/vast-training.md §4). 로봇·카메라 코드는 없다: 학습은 데이터셋만 본다.
#
# ## 변종이 둘이다 — 회선이 정한다
#
#   full  스택(torch·lerobot) 포함. pull 이 크다(수 GB). 부팅 즉시 학습 가능.
#   slim  스택 없음. pull 은 작고, 첫 부팅 때 bootstrap.sh 가 PyPI/PyTorch 에서 받아 깐다.
#
# 어느 쪽이 빠른지는 그 호스트의 회선에 달렸다 — GHCR 에서 이미지를 당기는 속도와
# PyPI/PyTorch 에서 wheel 을 받는 속도가 다르고, 호스트마다 다르다. 그래서 둘 다 굽고
# `env-check.sh` 가 인스턴스 안에서 재서 고른다. 두 변종은 **같은 install-stack.sh**
# 로 같은 버전을 깐다 — 다른 것은 "언제" 뿐이다.
#
# ## 레이어 순서 (backend/Dockerfile 과 같은 규칙)
#
#   안 바뀌는 것(apt · venv · 스택 5GB)이 위, 자주 바뀌는 것(act_aux · 스크립트 · 버전 ENV)이
#   아래. 스택 RUN 이 act_aux COPY 보다 **앞**이라 정책 코드 한 줄에 5GB 를 다시 굽지 않는다.
#   버전 ARG/ENV 는 모든 RUN 뒤 — 앞에 두면 태그마다 전부 캐시 미스다 (v0.5.0 의 교훈).
#
# 빌드:  ./deploy/build-train.sh            (full·slim 둘 다, 기본 cu126)
#        ./deploy/build-train.sh --cuda cu128  (RTX 50 계열 호스트용)

# ⚠ 베이스는 backend/Dockerfile.base 와 **같은 다이제스트**다 — 추론 기계와 파이썬·
#   glibc 가 같아야 환경 차이를 하나라도 줄인다. 올릴 때는 둘을 같이 올린다.
FROM python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tmux           : SSHRunner 의 전제 — 원격 세션이 곧 학습의 소유자다
# ffmpeg         : torchcodec 데이터셋 디코딩
# git            : lerobot 이 일부 경로에서 부른다
# openssh-server : Vast 의 ssh 접속 모드가 컨테이너 안 sshd 를 쓴다 (없으면 시작 때 깔려고
#                  하는데, 그 실패 모드를 5MB 로 없앤다 — V0 에서 실제 필요 여부 확인)
# rsync          : 체크포인트 회수 보험(scp/rsync) — feature/vast-training.md §5
# curl · procps  : env-check.sh (회선 측정 · 프로세스 확인)
# build-essential · linux-libc-dev : lerobot 의존성 중 py3.13 wheel 이 없는 sdist(evdev) 가
#                  gcc 와 `linux/input.h` 를 요구한다 — 없으면 full 은 굽다가, slim 은
#                  부팅 때 "Failed building wheel for evdev" 로 죽는다 (실제로 그랬다)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tmux ffmpeg git openssh-server rsync curl ca-certificates procps \
        build-essential linux-libc-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel

# ── 학습 스택 (full 만) ──
# 스크립트는 이 RUN 이 쓰는 것 하나만 먼저 복사한다 — 나머지 스크립트가 바뀌어도
# 5GB 레이어는 그대로다.
COPY deploy/train/install-stack.sh /opt/piper/install-stack.sh
ARG TORCH_CUDA=cu126
ARG VARIANT=full
RUN case "$VARIANT" in \
      full) TORCH_CUDA="$TORCH_CUDA" bash /opt/piper/install-stack.sh \
            && echo "$(date -Is) full (build)" > /opt/piper/.ready ;; \
      slim) : ;; \
      *)    echo "VARIANT 는 full 또는 slim 이어야 합니다: $VARIANT" >&2; exit 1 ;; \
    esac

# ── 회사 코드 — act_aux 하나뿐 ──
# ⚠ **act_aux 를 원격에서 학습하려면 필수** — lerobot-train 이 `lerobot_policy_` 접두사로
#   자동 import 한다. 빼먹으면 "unknown policy type" 으로 죽는다.
#   `--no-deps`: 의존성(lerobot)은 full 이면 위에 있고 slim 이면 부팅 때 온다.
COPY act_aux/ /opt/piper/act_aux/
RUN pip install --no-deps --no-build-isolation /opt/piper/act_aux

COPY deploy/train/bootstrap.sh deploy/train/env-check.sh /opt/piper/
RUN chmod +x /opt/piper/*.sh

# 부팅 스크립트가 어느 CUDA 빌드를 깔지 알아야 한다 (slim). full 에서는 기록용.
ENV TORCH_CUDA=${TORCH_CUDA} \
    PIPER_TRAIN_VARIANT=${VARIANT}

# 이미지 태그를 안에 박는다 — job 레코드가 "어느 환경에서 학습했나"를 여기서 읽는다.
# ⚠ RUN 이 전부 끝난 뒤다.
ARG PIPER_TRAIN_IMAGE=unknown
ENV PIPER_TRAIN_IMAGE=${PIPER_TRAIN_IMAGE}
LABEL org.opencontainers.image.source="https://github.com/WeGo-Robotics/wego-piper-web" \
      org.opencontainers.image.description="Piper Studio 학습 전용 이미지 (${VARIANT}, ${TORCH_CUDA})"

WORKDIR /workspace
# Vast 의 ssh 모드는 자체 진입점을 쓰고 `--onstart-cmd` 로 부팅 스크립트를 부른다.
# 그 밖(docker run)에서는 이 CMD 가 같은 일을 한다: slim 이면 스택을 깔고, 살아 있는다.
CMD ["bash", "-c", "/opt/piper/bootstrap.sh && exec sleep infinity"]
