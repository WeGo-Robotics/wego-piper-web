# syntax=docker/dockerfile:1
#
# 백엔드 이미지: FastAPI + LeRobot + wrapper + Piper 플러그인.
# 호스트 환경(Python 3.13 / torch 2.11.0+cu130 / lerobot 0.5.0)을 그대로 재현한다.
# GPU 드라이버는 nvidia-container-toolkit가 런타임에 주입하므로 베이스는 순수 python 이미지.
# 빌드 컨텍스트는 레포 루트(docker-compose 의 context: .)라서 backend/ wrapper/ vendor/ 를 모두 볼 수 있다.

FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── 시스템 패키지 ──
#  build-essential : py3.13용 wheel 없는 sdist 대비
#  ffmpeg/libav    : torchcodec · 녹화 인코딩 런타임 라이브러리
#  libgl1/libglib  : opencv 런타임
#  v4l-utils       : v4l2-ctl (camera_manager)
#  iproute2/can-utils : CAN 인터페이스 관리 (ip link / candump)
#  ethtool         : CAN 어댑터 드라이버 식별 (robot_manager.scan_can_interfaces)
#  kmod            : modprobe gs_usb (호스트 /lib/modules 를 ro 마운트해야 실효)
#  libusb-1.0-0/usbutils : RealSense · piper_sdk USB
#  gstreamer*      : 카메라 분배 (v4l2loopback 커널 모듈은 호스트에 로드)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
        ffmpeg libgl1 libglib2.0-0 \
        v4l-utils iproute2 can-utils ethtool kmod \
        libusb-1.0-0 usbutils \
        gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/*

# ── 가상환경 ──
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel

# ── LeRobot + torch (호스트 버전 고정) ──
# lerobot 0.5.0 은 torch<2.11 을 요구하지만 호스트는 2.11.0+cu130 으로 override 되어 있다.
# 동일하게 맞추되, cu12 스택을 먼저 걷어내고 cu130 을 deps 포함으로 설치한다.
#   ※ --no-deps 로 설치하면 nvidia-*-cu13 런타임이 안 들어와서
#     libcudart.so.13 누락 → `import torch` 자체가 죽는다. (cu12 잔재만 남음)
#   ※ 한 RUN 으로 묶는 이유: 레이어는 누적이라 별도 RUN 에서 uninstall 해도
#     cu12 파일(~5GB)이 앞 레이어에 그대로 남아 이미지가 두 배가 된다.
# [smolvla] extra 필수: 이게 없으면 transformers 가 안 들어와
# lerobot.policies.smolvla.modeling_smolvla import 자체가 실패한다 (추론/학습/엔코더 프로브 전부).
# transformers 는 호스트와 동일하게 5.3.0 으로 고정한다. 5.14 에서는 PretrainedConfig 에
# 기본값 필드가 늘어 lerobot 의 groot config 데이터클래스가 깨지고(backbone_cfg follows
# problem_type), lerobot.policies.__init__ 이 groot 를 import 하므로 ACT 까지 전부 죽는다.
RUN pip install "lerobot[smolvla]==0.5.0" "transformers==5.3.0" \
    && pip uninstall -y torch torchvision torchcodec \
    && pip freeze | grep -E '^nvidia-[a-z0-9_-]+-cu12' | cut -d= -f1 | xargs -r pip uninstall -y \
    && pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch==2.11.0 torchvision==0.26.0 torchcodec==0.11.0

# ── Piper 하드웨어 스택 ──
RUN pip install piper_sdk==0.6.1 python-can==4.6.1

# ── 버스·shm 계약 패키지 ──
# 게이트웨이와 데몬이 **공유하는 계약**이다. 데몬 자체(rsd/camerad/robotd)는
# 호스트 systemd 로 뜨지만, 이 컨테이너 안의 LeRobot 프로세스가 같은 포맷으로
# 세그먼트를 읽고 같은 큐로 말해야 하므로 계약 패키지는 여기에도 들어간다.
COPY bus/ /tmp/pkg/bus/
COPY shm/ /tmp/pkg/shm/
COPY robot/ /tmp/pkg/robot/
COPY phase/ /tmp/pkg/phase/
RUN pip install --no-deps /tmp/pkg/bus /tmp/pkg/shm /tmp/pkg/robot /tmp/pkg/phase \
    && pip install redis \
    && rm -rf /tmp/pkg

# ── 내부 패키지 (vendor/) : 이미 설치된 의존성 재해석 방지로 --no-deps ──
# ⚠ **shm 플러그인 둘이 빠지면 `type: "shm"` 카메라·`piper_follower_shm` 로봇이
# 등록되지 않아** draccus 가 "unrecognized robot.type" 으로 죽는다.
COPY vendor/ /tmp/vendor/
RUN pip install --no-deps /tmp/vendor/wego_piper \
    && pip install --no-deps /tmp/vendor/lerobot_robot_piper \
    && pip install --no-deps /tmp/vendor/lerobot_camera_pipershm \
    && pip install --no-deps /tmp/vendor/lerobot_robot_pipershm \
    && rm -rf /tmp/vendor

# ── 앱 소스: cli_mapping 의 parents[3] 상대경로(/app/backend ↔ /app/wrapper) 유지 ──
WORKDIR /app
COPY backend/ /app/backend/
COPY wrapper/ /app/wrapper/

# ── 데이터 경로 계약 ──
# 컨테이너 내부 경로는 호스트와 무관한 고정 상수다. 호스트 디렉토리는 compose 가
# <DATA_ROOT>:/data 단일 마운트로 붙인다. 바꾸려면 compose environment 로 덮어쓴다.
# ※ Path.home() 은 컨테이너에서 /root 라 호스트와 갈리므로 경로 근거로 쓰지 않는다.
ENV HF_HOME=/data/hf \
    PIPER_MODELS_DIR=/data/hf/hub \
    PIPER_DATASETS_DIR=/data/hf/hub \
    PIPER_LEROBOT_DIR=/data/hf/lerobot \
    PIPER_CONFIG_DIR=/data/config \
    PIPER_ROBOT_CONFIG_PATH=/data/config/piper_config.json \
    PIPER_LOG_DIR=/data/logs \
    PIPER_DEBUG_DIR=/data/logs/debug \
    PIPER_MODEL_PATHS=/data/hf/hub:/data/outputs/train

# 백엔드 의존성 설치 (fastapi/uvicorn/pyzmq/pandas/plotly + pyrealsense2)
RUN pip install -e "/app/backend[realsense]"

# LeRobot train 의 기본 output_dir 은 CWD 상대(`outputs/train/...`)이고 우리가 바꿀 수
# 없다. CWD(/app/backend) 아래 심볼릭 링크를 걸어 체크포인트가 마운트로 떨어지게 한다.
# (.dockerignore 가 backend/outputs 를 제외하므로 이 자리는 비어 있다.
#  빌드 시점엔 /data 가 아직 없어 dangling 이므로 pip install 이후에 만든다)
RUN ln -s /data/outputs /app/backend/outputs

# 두 python 진입점 모두 이 venv 를 가리키게 함 (compose 에서 override 가능)
ENV PIPER_LOCAL_PYTHON=/opt/venv/bin/python \
    PIPER_GRPC_PYTHON=/opt/venv/bin/python

WORKDIR /app/backend
EXPOSE 8000

# /data 는 런타임 마운트라 빌드 시 mkdir 해봐야 가려진다. 기동 직전에 만든다.
# subprocess/ZMQ 상태가 인메모리라 단일 워커 고정, --reload 없음
CMD ["sh", "-c", "mkdir -p /data/hf /data/outputs/train /data/logs /data/config && \
exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
