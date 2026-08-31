# syntax=docker/dockerfile:1
#
# 백엔드 이미지 — **회사 코드만.** 서드파티 스택은 베이스에 있다.
# 빌드 컨텍스트는 레포 루트(docker-compose 의 context: .)라서 backend/ wrapper/ vendor/ 를 모두 볼 수 있다.
#
# ⚠ 베이스가 먼저 있어야 한다. 없으면 `./deploy/build-base.sh` 가 만든다 —
#   `release.sh` 는 알아서 확인한다.
# ⚠ **아래 기본값은 `backend/BASE_VERSION` 과 같아야 한다.** 테스트가 강제한다
#   (`test_release.py::test_the_base_tag_is_single_sourced`).
ARG BASE_TAG=cu130-1
FROM piper-web-base:${BASE_TAG}

# ── 버스·shm 계약 패키지 ──
# 게이트웨이와 데몬이 **공유하는 계약**이다. 데몬 자체(rsd/camerad/robotd)는
# 호스트 systemd 로 뜨지만, 이 컨테이너 안의 LeRobot 프로세스가 같은 포맷으로
# 세그먼트를 읽고 같은 큐로 말해야 하므로 계약 패키지는 여기에도 들어간다.
COPY bus/ /tmp/pkg/bus/
COPY shm/ /tmp/pkg/shm/
COPY robot/ /tmp/pkg/robot/
COPY phase/ /tmp/pkg/phase/
# act_aux: 배포명 `lerobot_policy_act_aux` — lerobot-train 이 접두사로 자동 import 한다 (feature/act-aux.md §2)
COPY act_aux/ /tmp/pkg/act_aux/
RUN pip install --no-deps /tmp/pkg/bus /tmp/pkg/shm /tmp/pkg/robot /tmp/pkg/phase /tmp/pkg/act_aux \
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
# 정책 스펙 — `core/policy_spec.py` 가 저장소 루트(/app)의 `policies/` 에서 읽는다.
# 빠뜨리면 정책 목록이 **통째로 비고**, 학습·추론 화면이 아무것도 못 고른다.
COPY policies/ /app/policies/

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
