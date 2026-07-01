# Piper Web

LeRobot 웹 인터페이스 — 로봇 모방학습 프레임워크를 웹에서 제어.

## 실행 방법

### 요구사항

- Node.js 20+ (nvm use 24 권장)
- Python 3.11+
- LeRobot 0.5+ (`pip install lerobot`)
### 설치

```bash
# 프론트엔드
cd frontend && npm install

# 백엔드 (pyproject.toml 기반, 의존성 자동 설치)
cd backend && pip install -e ".[dev]"

# matplotlib 분석 도구 (선택)
pip install -e ".[analysis]"
```

### 개발 서버 (백엔드 + 프론트엔드 동시)

```bash
./dev.sh
```

브라우저에서 http://localhost:5173 접속.

### 개별 실행

```bash
# 백엔드
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 프론트엔드 (별도 터미널)
cd frontend
npm run dev
```

### 빌드

```bash
cd frontend
npm run build       # 프로덕션 빌드
npx tsc --noEmit    # 타입 체크
```

## Docker 배포

전체 스택(백엔드 + LeRobot + Piper 플러그인 + 프론트엔드)을 컨테이너로 제공한다.
호스트 환경(Python 3.13 / torch 2.11.0+cu130 / lerobot 0.5.0)을 그대로 재현하며,
**로봇이 물리적으로 연결된 호스트**에서만 실행한다 (하드웨어 직접 접근이 필요).

### 사전 요구사항 (호스트)

- Docker + Docker Compose v2
- [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) (GPU 패스스루)
- CAN 인터페이스(`can_follower1` 등) 호스트에서 구성/기동 — 커널/네트워크 레벨이라 컨테이너 밖
- `v4l2loopback` 커널 모듈 호스트에 로드 — 커널 모듈은 컨테이너에서 삽입 불가
- RealSense udev 규칙 설치: `sudo cp backend/udev/99-realsense-libusb.rules /etc/udev/rules.d/ && sudo udevadm control --reload`

### 내부 패키지 (vendor/)

`lerobot_robot_piper`, `wego_piper` 는 공개 PyPI에 없어 `vendor/` 에 스냅샷으로 포함되어 있다.
소스가 바뀌면 [vendor/README.md](vendor/README.md) 의 갱신 방법을 따라 다시 떠야 한다.

### 실행

```bash
docker compose up -d --build      # 빌드 + 기동
docker compose logs -f backend    # 백엔드 로그
docker compose down               # 정지
```

브라우저에서 `http://<호스트IP>/` 접속 (nginx `:80` → backend `:8000` 프록시).

### 볼륨 (영속화)

| 호스트 경로 | 컨테이너 | 용도 |
|-------------|----------|------|
| `~/.cache/huggingface` | `/root/.cache/huggingface` | 모델·데이터셋·lerobot 캐시 |
| `~/.config/piper-web` | `/root/.config/piper-web` | `model_paths.json` 등 사용자 설정 |
| `./backend/data` | `/app/backend/data` | 로그/평가 데이터 |
| `./backend/outputs` | `/app/backend/outputs` | 학습 체크포인트 |

> GPU가 안 잡히면 compose 의 `deploy.resources...` 대신 `runtime: nvidia` 를 사용한다.
> 호스트 `:80`/`:8000` 이 이미 사용 중이면 충돌하므로 비워둔다 (host network).

## 추론 로그

### CSV 로깅

gRPC 추론 실행 시 매 스텝 자동 기록됩니다.

- **파일 위치**: `/tmp/piper_inference_YYYYMMDD_HHMMSS.csv`
- **종료 시** WebSocket으로 파일 경로 알림

#### 기록 컬럼

| 구분 | 컬럼 | 설명 |
|------|------|------|
| 메타 | `timestamp`, `step`, `fps`, `queue_size` | 시간, 스텝, FPS, 액션 큐 잔량 |
| 정책 출력 | `target_{joint}.pos` | 필터 적용 전 원본 액션 |
| 전송값 | `filtered_{joint}.pos` | 속도 제한 + 저역통과 등 필터 후 실제 전송 |
| 로봇 위치 | `actual_{joint}.pos` | 로봇이 보고한 현재 관절 위치 |
| 기타 | `task`, `paused` | 태스크 문자열, 일시정지 여부 |

### API

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /api/logs` | CSV 로그 파일 목록 |
| `GET /api/logs/download/{filename}` | CSV 파일 다운로드 |

### 분석 도구

`tools/analyze_inference_log.py` — pandas + matplotlib 기반 시각화.

```bash
# 통계 + 그래프 (GUI)
python tools/analyze_inference_log.py /tmp/piper_inference_20260404_213500.csv

# 특정 관절만
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --joint joint1

# PNG 저장 (GUI 없이, 서버 환경)
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --save

# 통계만 출력
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --no-plot
```

#### 출력 예시

```
============================================================
Duration: 45.2s | Steps: 1356 | Avg FPS: 30.0
Queue empty: 226/1356 (16.7%)
============================================================
Joint               |target-actual| mean        max        std
------------------------------------------------------------
joint1.pos                         1.234      5.678      0.891
...

Joint               |target-filtered| mean        max
----------------------------------------------------
joint1.pos                           0.456      2.345
...
```

#### 생성 파일 (`--save` 옵션)

| 파일 | 내용 |
|------|------|
| `*_overview.png` | 관절별 target / filtered / actual 비교 |
| `*_error.png` | 관절별 추적 오차 (target - actual) |
| `*_fps_queue.png` | FPS + 액션 큐 사이즈 시계열 |
