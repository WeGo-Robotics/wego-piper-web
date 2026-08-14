# LeRobot 웹 인터페이스 설계 문서

> 작성일: 2026-04-03
> 목적: LeRobot을 터미널에서 실행하고 웹에서 통제하기 위한 아키텍처 설계 및 기술 검토

---

## 1. 아키텍처 선택

### 1.1 설계 원칙

- **유지보수 최소화**: LeRobot 새 릴리즈 시 코드 수정 범위를 최소한으로 제한
- LeRobot의 내부 Python API는 릴리즈마다 자주 변경되지만, **CLI 인터페이스는 상대적으로 안정적**
- 직접 import 방식은 breaking change에 매번 코드 수정 필요

### 1.2 채택 방식: CLI 래핑 (subprocess)

```
[Web UI (브라우저)]
     ↕ WebSocket / REST
[FastAPI 서버]
     ├→ subprocess.Popen  → LeRobot CLI 실행
     ├→ evdev UInput      → 가상 키보드 입력 주입
     ├→ stdout 파싱       → 상태/로그 웹으로 전송
     ├→ ZMQ PUSH          → 실시간 파라미터 변경 (래퍼 경유)
     └→ /dev/video11      → 카메라 스트리밍 (독립)
```

- LeRobot 업데이트 시 **CLI 인자 매핑 테이블 + 키 매핑 테이블**만 관리
- 웹 서버 코드 자체는 건드릴 필요 없음
- 웹 프레임워크는 **FastAPI + 가벼운 프론트엔드**가 가장 보수적인 선택

### 1.3 비교: 다른 접근 방식

| 방식 | 장점 | 단점 |
|------|------|------|
| **NiceGUI 직접 import** | 코드가 간결, WebSocket 자동 처리 | API 변경 시 전면 수정, NiceGUI 자체 업데이트 주기 빠름 |
| **FastAPI + subprocess** (채택) | LeRobot과 완전 분리, 크래시 격리 | 초기 구축 비용 약간 높음 |
| **Gradio** | 빠른 프로토타입 | 커스터마이징 제한, 실시간 제어 부적합 |

---

## 2. 카메라 멀티프로세스 접근 문제

### 2.1 문제

- V4L2 디바이스는 일반적으로 **한 프로세스만 독점** (동시 접근 시 `EBUSY`)
- LeRobot이 카메라를 잡고 있으면 웹 스트리밍에서 동시에 접근 불가

### 2.2 해결: v4l2loopback 가상 디바이스 분기

```
/dev/video0 (물리 카메라)
      ↓ ffmpeg / gstreamer
      ├→ /dev/video10 (가상) → LeRobot이 사용
      └→ /dev/video11 (가상) → Web 스트리밍이 사용
```

```bash
# 가상 디바이스 2개 생성
sudo modprobe v4l2loopback devices=2 \
  video_nr=10,11 \
  card_label="LeRobot","WebStream" \
  exclusive_caps=1

# ffmpeg로 물리 카메라 → 가상 디바이스 2개로 tee
ffmpeg -f v4l2 -i /dev/video0 \
  -f v4l2 /dev/video10 \
  -f v4l2 /dev/video11
```

### 2.3 GStreamer 대안 (낮은 지연)

```bash
gst-launch-1.0 v4l2src device=/dev/video0 ! tee name=t \
  t. ! queue ! v4l2sink device=/dev/video10 \
  t. ! queue ! v4l2sink device=/dev/video11
```

- PiPER처럼 실시간성이 중요한 경우 GStreamer tee가 더 적합
- systemd 서비스로 등록하면 부팅 시 자동 실행

### 2.4 카메라 프록시 방식 (비채택)

- 한 프로세스가 카메라를 잡고 MJPEG/shared memory로 배포
- LeRobot이 `VideoCapture`로 `/dev/videoN`을 기대하므로 LeRobot 코드 수정 필요
- 유지보수 원칙에 위배

---

## 3. 키보드 입력 주입

### 3.1 문제

- LeRobot은 `stdin`이 아니라 **키보드 디바이스를 직접 읽음** (`pynput`, `keyboard` 라이브러리가 `/dev/input/` 직접 접근)
- `subprocess.Popen(stdin=PIPE)` → `proc.stdin.write(b'\n')` 은 무시됨

### 3.2 해결: evdev 가상 키보드 주입

```python
from evdev import UInput, ecodes

ui = UInput()

def send_key(key_code):
    ui.write(ecodes.EV_KEY, key_code, 1)  # press
    ui.write(ecodes.EV_KEY, key_code, 0)  # release
    ui.syn()

# 웹에서 "다음 에피소드" 버튼 → 엔터 키 주입
send_key(ecodes.KEY_ENTER)

# 녹화 중지 → 's' 키 주입
send_key(ecodes.KEY_S)
```

### 3.3 요구사항

- `uinput` 커널 모듈 필요: `sudo modprobe uinput`
- 권한: `sudo` 또는 `/dev/uinput`에 유저 퍼미션 추가
- RDK X5 등 임베디드 보드에서도 리눅스 커널이면 동일 동작

---

## 4. 모방학습/VLA 웹 인터페이스 필수 요소

### 4.1 데이터 수집 (가장 시간 많이 쓰는 단계)

- **카메라 멀티뷰 실시간 피드**: wrist cam, scene cam 동시 표시, 프레임 드랍 감지 경고
- **에피소드 컨트롤**: 녹화 시작/중지/폐기(discard), 에피소드 번호 표시
- **관절 상태 오버레이**: 현재 joint position/torque 실시간 그래프, 이상치 발생 시 해당 에피소드 마킹
- **태스크 프롬프트 입력**: VLA language instruction 에피소드별 텍스트 태깅 UI
- **에피소드 카운터/목표**: "47/100 episodes" 진행률 + 세션 타이머

### 4.2 데이터 검수

- **에피소드 리플레이 플레이어**: 영상 + 관절 궤적 동기화 재생, 배속 조절
- **불량 에피소드 삭제/필터**: 그리퍼 미스, 충돌 등 실패 에피소드 걸러내기
- **데이터셋 통계 대시보드**: 에피소드 수, 평균 길이, action 분포, 이미지 밝기 분포

### 4.3 학습

- **Config 편집기**: policy 종류(ACT, Diffusion, VLA), 하이퍼파라미터 수정 (YAML 직접 편집 대신 폼 UI)
- **학습 모니터링**: loss curve 실시간, GPU 사용률, ETA
- **체크포인트 관리**: 저장된 체크포인트 목록, 비교, 삭제

### 4.4 추론/평가 (가장 중요한 화면)

- **원클릭 배포**: 체크포인트 선택 → 로봇에 로드 → 실행
- **실시간 추론 시각화**: 모델 예측 action trajectory를 카메라 위에 오버레이 (predicted vs actual)
- **성공/실패 로깅**: 시도마다 성공 여부 기록, 자동 성공률 집계
- **긴급 정지 버튼**: 화면 어디서든 접근 가능, 크고 빨갛게

### 4.5 빼먹기 쉬운 요소

- 로봇 홈 포지션 리셋 (에피소드 사이마다 필요)
- 캘리브레이션 위저드 (카메라 extrinsic, 그리퍼 오프셋)
- 네트워크 상태 표시 (특히 무선 환경에서 지연/드랍 모니터링)
- 세션 자동저장 (브라우저 닫혀도 녹화 중인 데이터 안 날아가게)

---

## 5. LeRobot 에피소드 편집 기능

### 5.1 v0.4.0 이후 공식 지원 (`lerobot-edit-dataset` CLI)

| 기능 | CLI 명령 | 비고 |
|------|----------|------|
| 에피소드 삭제 | `--operation.type delete_episodes --operation.episode_indices "[0,2,5]"` | 원본 보존 옵션 (`--new_repo_id`) |
| 데이터셋 분할 | `--operation.type split --operation.splits '{"train":0.8,"test":0.2}'` | fraction 또는 episode index 기준 |
| 데이터셋 병합 | `--operation.type merge --operation.repo_ids "[...]"` | 동일 feature 구조 필수 |
| 피처 제거 | `--operation.type remove_features` | |
| 데이터셋 정보 | `--operation.type info` | |

### 5.2 미지원 기능 (2026년 4월 기준)

- 에피소드별 task description 수정 (오타 교정, 상세 설명 추가)
- feature 이름 변경 (rename)
- 서로 다른 feature 구조의 데이터셋 병합
- 프레임 단위 트리밍 → 서드파티 `lerobot-dataset-editor`가 지원

### 5.3 웹 UI 연동

`lerobot-edit-dataset` CLI를 그대로 래핑:
에피소드 리플레이 → 불량 체크 → 삭제 인덱스 선택 → CLI 호출

---

## 6. 비디오 인코딩 성능

### 6.1 기존 병목 (v0.4 이하)

- `save_episode()` 호출 시 30~40초 대기 (15초 에피소드 기준)
- 원인: `encode_episode_videos()` 75% + `compute_episode_stats()` 25%
- 순수 CPU 소프트웨어 인코딩, 임시 PNG `compress_level=6` 기본값으로 1080p에서 최대 10배 느림

### 6.2 v0.5.0 개선사항

- **Streaming video encoding**: 프레임 캡처 즉시 실시간 인코딩, 에피소드 간 대기 제거
- **하드웨어 인코더 자동 감지**: GPU 가속 자동 사용
- **Parallel encoding**: non-streaming에서도 기본 적용, 3배 빠른 인코딩

### 6.3 사용법

```bash
lerobot-record \
  --dataset.streaming_encoding=true \
  --dataset.vcodec=auto              # HW 인코더 자동 감지
  # 명시적 지정:
  # --dataset.vcodec=h264_nvenc        # NVIDIA GPU
  # --dataset.vcodec=h264_videotoolbox # macOS
  # --dataset.vcodec=h264_qsv          # Intel QSV
  --dataset.encoder_threads=2
```

### 6.4 임베디드 환경 (RDK X5 / Jetson)

| 환경 | 권장 설정 |
|------|----------|
| Jetson (NVENC) | `--dataset.vcodec=h264_v4l2m2m` |
| RDK X5 | HW 인코더 ffmpeg 노출 확인 필요. 미지원 시 `streaming_encoding=true` + `libx264 ultrafast` + 480p 해상도 |
| 일반 PC (NVIDIA) | `--dataset.vcodec=h264_nvenc` |

---

## 7. 추론 진동 문제와 해결

### 7.1 원인

- 대형 VLA 모델은 action chunk (예: 50스텝)를 한 번에 예측
- 모델 추론 시간 > chunk 실행 시간 → chunk 경계에서 일시정지, 급격한 방향 전환, 진동 발생
- SmolVLA에서 주기적 re-inference 시 trajectory 불연속 → 로봇 암 진동

### 7.2 해결: Real-Time Chunking (RTC) — v0.5.0 내장

- 현재 chunk 실행 중 비동기로 다음 chunk를 미리 생성
- overlap 구간을 inpainting으로 처리하여 자연스러운 전환
- **재학습 불필요**, 추론 시간에만 적용
- Pi0, Pi0.5, SmolVLA, Diffusion 정책에 호환

```yaml
rtc_config:
  enabled: true
  execution_horizon: 10          # overlap 스텝 수
  max_guidance_weight: 10.0      # SmolVLA/Pi0/Pi0.5 최적값
  prefix_attention_schedule: EXP  # 지수 감쇠 (권장)
```

### 7.3 ACT 계열 (별도)

- RTC 대상이 아님 (flow-matching 기반이 아니므로)
- 자체 temporal ensemble 사용

```python
cfg = ACTConfig(
    temporal_ensemble_coeff=0.01,  # 지수 가중 평균 스무딩
    n_action_steps=1               # temporal ensemble 시 필수
)
```

### 7.4 디버그 시각화

LeRobot 내장 `RTCDebugVisualizer`로 denoising step 및 correction 시각화 가능:

```python
policy_cfg.rtc_config.debug = True
policy_cfg.rtc_config.debug_maxlen = 100

debug_data = policy.rtc_processor.get_debug_data()

from lerobot.policies.rtc.debug_visualizer import RTCDebugVisualizer
visualizer = RTCDebugVisualizer()
```

---

## 8. 실시간 파라미터 변경

### 8.1 문제

- subprocess로 띄운 LeRobot 프로세스 내부의 Python 객체를 외부에서 수정해야 함
- 순수 CLI 래핑으로는 불가능

### 8.2 해결: 얇은 래퍼 + ZMQ IPC

```
[Web UI 슬라이더]
     ↓ WebSocket
[FastAPI 서버]
     ↓ ZMQ PUSH
[LeRobot 래퍼 프로세스]
     ├→ ZMQ 수신 스레드 → policy 객체 속성 직접 수정
     └→ LeRobot 추론 루프 (수정 없음)
```

### 8.3 래퍼 코드

```python
# lerobot_wrapper.py
import threading
import zmq

def param_listener(policy, zmq_addr="tcp://127.0.0.1:5555"):
    ctx = zmq.Context()
    sock = ctx.socket(zmq.PULL)
    sock.bind(zmq_addr)

    while True:
        msg = sock.recv_json()
        if "max_guidance_weight" in msg:
            policy.config.rtc_config.max_guidance_weight = msg["max_guidance_weight"]
        if "execution_horizon" in msg:
            policy.config.rtc_config.execution_horizon = msg["execution_horizon"]
        if "temporal_ensemble_coeff" in msg:
            policy.config.temporal_ensemble_coeff = msg["temporal_ensemble_coeff"]
        if "n_action_steps" in msg:
            policy.config.n_action_steps = msg["n_action_steps"]

policy = load_policy(...)
t = threading.Thread(target=param_listener, args=(policy,), daemon=True)
t.start()
run_inference(policy, ...)
```

### 8.4 FastAPI 측

```python
import zmq

ctx = zmq.Context()
sock = ctx.socket(zmq.PUSH)
sock.connect("tcp://127.0.0.1:5555")

@app.post("/params")
async def update_params(params: dict):
    sock.send_json(params)
    return {"status": "ok"}
```

### 8.5 IPC 방식 비교

| 방식 | 지연 | 장점 | 단점 |
|------|------|------|------|
| **ZMQ** (채택) | < 1ms | 의존성 하나(pyzmq), JSON 내장 | — |
| Shared memory | < 0.1ms | 최저 지연 | 구조체 정의, 동기화 직접 관리 |
| Redis | ~1ms | 풍부한 기능 | 외부 서비스 의존 |
| Named pipe | < 1ms | OS 내장 | 직렬화 직접 구현 |

### 8.6 파라미터 안전 분류

| Safe (실시간 변경 가능) | Unsafe (재시작 필요) |
|------------------------|---------------------|
| `max_guidance_weight` | `chunk_size` |
| `execution_horizon` | `dim_model` |
| `temporal_ensemble_coeff` | `n_obs_steps` |
| `n_action_steps`* | `use_vae` |
| guidance schedule | 모델 아키텍처 관련 전부 |

\* `n_action_steps`는 `chunk_size` 이하일 때만 safe

---

## 9. 추가 고려사항

### 9.1 안전

- **E-stop 경로를 웹서버와 분리**: 별도 watchdog 프로세스가 heartbeat 감시, 타임아웃 시 하드웨어 레벨 정지. WebSocket 끊김 = 즉시 안전 정지
- **파라미터 범위 제한**: 웹 슬라이더에 min/max 클램핑 필수. `n_action_steps`를 과도하게 올리면 로봇이 오래 눈 감고 달리는 격

### 9.2 프로세스 라이프사이클

- **크래시 복구**: supervisord 또는 systemd로 자동 재시작, 마지막 체크포인트 + RTC 파라미터 자동 복원, ZMQ 소켓 재연결 처리
- **GPU 메모리 경합**: 학습과 추론 동시 실행 시 OOM 위험 → 웹 UI에서 모드를 배타적으로 관리하거나 별도 GPU 지정

### 9.3 네트워크

- **원격 접속 시 지연**: 카메라 스트리밍이 수백ms 지연되면 텔레옵 데이터 품질 저하. MJPEG보다 WebRTC 권장, 화질-지연 트레이드오프 슬라이더 고려
- **동시 접속 제어**: 교육용 환경에서 학생 여러 명 접속 시 lock/unlock 메커니즘 필요 (G1 시분할 시스템과 유사)

### 9.4 데이터 무결성

- ⚠ **녹화 중 브라우저 종료 — 지금은 에피소드를 잃는다.** CLI 래핑 자체는 독립 완결이
  가능하지만, refactor #10 이 E-stop 범위를 "로봇을 움직이는 것 전부"로 넓힌 뒤로는
  heartbeat 가 끊기면 estopd 가 **SIGKILL** 한다 (실측 2.5초). 비디오 인코딩 도중에
  끊기는 것이라 그 에피소드는 못 쓴다. 맞교환을 다시 볼 자리는 `ESTOP_TARGETS` 다
- **디스크 용량 모니터링**: 카메라 2대 30fps → 시간당 수 GB. 임계치 경고 필수

### 9.5 재현성

- **세션 로깅**: 체크포인트, RTC 파라미터, config를 자동 기록. 추론 결과와 대조 가능하게
- **config 스냅샷**: 파라미터 변경 이력을 타임스탬프와 함께 저장

### 9.6 임베디드 배포 (RDK X5 / Jetson)

- **리소스 예산**: 웹서버 + ZMQ + 카메라 스트리밍 + 추론이 한 보드에서 동작 시 CPU/메모리 빠듯. FastAPI uvicorn single worker, 카메라는 GStreamer로 CPU 부하 최소화
- **thermal throttling**: 장시간 추론 시 SoC 발열로 성능 저하 → 온도 모니터링 대시보드에 포함

### 9.7 교육 환경 특화

- **원클릭 환경 리셋**: 학생이 config 망가뜨려도 `git checkout`으로 초기 상태 복원
- **학생별 네임스페이스 분리**: `student01/dataset_name` 형태로 데이터셋 격리
- **5일 집중 교육 시나리오**: pre-built code + UI 중심 실습, 코드 직접 수정 최소화

---

## 10. MVP 우선순위

### Phase 1 (핵심 — 즉시 구현)

1. E-stop 독립 경로 (watchdog + heartbeat)
2. 프로세스 크래시 자동 복구 (systemd)
3. 파라미터 범위 제한 (min/max 클램핑)

### Phase 2 (데이터 수집 UI — ROI 최대)

4. 카메라 멀티뷰 스트리밍 (v4l2loopback + WebRTC)
5. 에피소드 녹화/중지/폐기 컨트롤
6. 에피소드 카운터 + 진행률 표시
7. evdev 가상 키보드 입력 주입

### Phase 3 (추론/평가)

8. 체크포인트 선택 → 원클릭 배포
9. RTC 파라미터 실시간 슬라이더 (ZMQ IPC)
10. 추론 시각화 (predicted vs actual trajectory)
11. 성공/실패 로깅 + 성공률 집계

### Phase 4 (데이터 관리)

12. 에피소드 리플레이 플레이어
13. 불량 에피소드 삭제 (lerobot-edit-dataset 래핑)
14. 데이터셋 통계 대시보드

### Phase 5 (학습 + 고급)

15. Config 편집기 (폼 UI)
16. 학습 모니터링 (loss curve, GPU 사용률)
17. 세션 로깅 + config 스냅샷
18. 원격 접속 최적화 (WebRTC, 동시 접속 제어)

---

## 부록: 기술 스택 요약

| 구성요소 | 기술 | 비고 |
|----------|------|------|
| 웹 백엔드 | FastAPI + uvicorn | 안정성 우선 |
| IPC | ZMQ (pyzmq) | 프로세스 간 < 1ms 지연 |
| 카메라 분배 | v4l2loopback + GStreamer | LeRobot 코드 수정 없음 |
| 키 입력 주입 | evdev UInput | 가상 키보드 |
| 비디오 인코딩 | streaming_encoding + vcodec=auto | v0.5.0+ |
| 추론 스무딩 | RTC (flow-matching) / Temporal Ensemble (ACT) | v0.5.0+ |
| 프로세스 관리 | systemd / supervisord | 자동 재시작 |
| 원격 접속 | RustDesk + Tailscale/ZeroTier | 기존 인프라 활용 |
| 카메라 스트리밍 | WebRTC (권장) / MJPEG (fallback) | 지연 최소화 |