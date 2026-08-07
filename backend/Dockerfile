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
RUN pip install lerobot==0.5.0 \
    && pip uninstall -y torch torchvision torchcodec \
    && pip freeze | grep -E '^nvidia-[a-z0-9_-]+-cu12' | cut -d= -f1 | xargs -r pip uninstall -y \
    && pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch==2.11.0 torchvision==0.26.0 torchcodec==0.11.0

# ── Piper 하드웨어 스택 ──
RUN pip install piper_sdk==0.6.1 python-can==4.6.1

# ── 내부 패키지 (vendor/) : 이미 설치된 의존성 재해석 방지로 --no-deps ──
COPY vendor/ /tmp/vendor/
RUN pip install --no-deps /tmp/vendor/wego_piper \
    && pip install --no-deps /tmp/vendor/lerobot_robot_piper \
    && rm -rf /tmp/vendor

# ── 앱 소스: cli_mapping 의 parents[3] 상대경로(/app/backend ↔ /app/wrapper) 유지 ──
WORKDIR /app
COPY backend/ /app/backend/
COPY wrapper/ /app/wrapper/

# 백엔드 의존성 설치 (fastapi/uvicorn/pyzmq/pandas/plotly + pyrealsense2)
RUN pip install -e "/app/backend[realsense]"

# 두 python 진입점 모두 이 venv 를 가리키게 함 (compose 에서 override 가능)
ENV PIPER_LOCAL_PYTHON=/opt/venv/bin/python \
    PIPER_GRPC_PYTHON=/opt/venv/bin/python

WORKDIR /app/backend
EXPOSE 8000

# subprocess/ZMQ 상태가 인메모리라 단일 워커 고정, --reload 없음
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
