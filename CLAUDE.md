# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

LeRobot(로봇 모방학습 프레임워크)을 원격으로 제어하는 웹 인터페이스. 터미널에서 LeRobot을 실행하고 웹에서 통제한다.

주요 기능: 데이터 수집(에피소드 녹화/관리), 추론/평가(체크포인트 배포, 실시간 파라미터 튜닝), 학습 모니터링, E-stop 안전 정지.

## 빌드 및 실행

```bash
# 프론트엔드 의존성 설치 (Node 20+ 필요, nvm use 24 권장)
cd frontend && npm install

# 프론트엔드 빌드
cd frontend && npm run build

# 프론트엔드 타입 체크
cd frontend && npx tsc --noEmit

# 백엔드 의존성 설치
cd backend && pip install -e ".[dev]"

# 백엔드 + 프론트엔드 동시 개발 서버
./dev.sh

# 백엔드만 실행
cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 프론트엔드만 실행 (Vite dev server, :5173 → :8000 프록시)
cd frontend && npm run dev
```

## 아키텍처

### 핵심 설계 원칙
- **CLI 래핑 (subprocess)**: LeRobot을 직접 import하지 않고 subprocess로 CLI를 실행. LeRobot 업데이트 시 `backend/app/core/cli_mapping.py`만 수정
- **유일한 예외**: `wrapper/lerobot_wrapper.py`는 policy 객체를 런타임에 수정해야 하므로 LeRobot을 직접 import. import 범위를 `load_policy` + `run_inference`로 최소화
- **크래시 격리**: 웹 서버 ↔ LeRobot 프로세스 ↔ E-stop watchdog 각각 독립 프로세스

### 시스템 구조
```
[Web UI (React + Vite)]
     ↕ WebSocket / REST
[FastAPI 서버 (backend/)]
     ├→ subprocess.Popen  → LeRobot CLI 실행 (process_manager.py)
     ├→ Redis 큐          → 실시간 파라미터 변경 (param_bridge.py → wrapper/)
     ├→ stdout 파싱       → WebSocket으로 로그 전송 (ws.py)
     └→ estop_watchdog    → heartbeat 감시, 타임아웃 시 강제 종료
```

### subprocess(프로세스 관리)와 버스(파라미터 변경)의 관계
둘은 대안이 아니라 레이어가 다른 **협력 관계**:
- subprocess = 프로세스 시작/종료/크래시 감지/stdout 수집
- 버스(Redis) = 실행 중인 프로세스 내부와 실시간 통신 (policy 객체 속성 변경)

### 파라미터 안전 분류
- **Safe (실시간 변경 가능)**: `max_guidance_weight`, `execution_horizon`, `temporal_ensemble_coeff`, `n_action_steps`(chunk_size 이하일 때)
- **Unsafe (재시작 필요)**: `chunk_size`, `dim_model`, `n_obs_steps`, `use_vae`, 모델 아키텍처 관련 전부

## 기술 스택

| 구성요소 | 기술 |
|----------|------|
| 프론트엔드 | React + TypeScript + Vite + Tailwind CSS |
| 백엔드 | Python 3.11+ / FastAPI + uvicorn |
| IPC | Redis (piper_bus) — 파라미터·녹화제어 큐, 프리뷰 TTL 키, E-stop heartbeat |
| 카메라 분배 | v4l2loopback + GStreamer |
| 키 입력 주입 | evdev UInput |
| 추론 스무딩 | RTC (flow-matching) / Temporal Ensemble (ACT) |

## API 엔드포인트

| 경로 | 설명 |
|------|------|
| `GET /health` | 헬스체크 |
| `WS /ws` | WebSocket (로그 스트리밍, 프로세스 상태) |
| `POST /api/estop/trigger` | 긴급 정지 |
| `POST /api/estop/heartbeat` | heartbeat |
| `POST /api/params` | 실시간 파라미터 변경 (버스) |
| `GET /api/models` | 로컬 모델 목록 |
| `POST /api/models/inference/start` | 추론 시작 |
| `POST /api/models/inference/stop` | 추론 정지 |
| `GET /api/datasets` | 로컬 데이터셋 목록 |
| `POST /api/datasets/{id}/edit` | 데이터셋 편집 (CLI 래핑) |
| `GET /api/hub/models` | HuggingFace Hub 모델 검색 |
| `GET /api/hub/datasets` | HuggingFace Hub 데이터셋 검색 |
| `POST /api/hub/download` | Hub에서 다운로드 |
| `POST /api/eval/log` | 평가 결과 기록 |
| `GET /api/eval/stats` | 평가 통계 |

## 안전 관련 주의사항

- E-stop은 웹서버와 **반드시 분리된 독립 watchdog 프로세스**로 구현. WebSocket 끊김 = 즉시 안전 정지
- 웹 슬라이더에 min/max 클램핑 필수 (`backend/app/services/param_bridge.py`의 SAFE_PARAMS)
- ⚠ **녹화는 브라우저가 닫히면 죽는다.** CLI 래핑 자체는 에피소드를 독립적으로 완결할 수
  있지만, refactor #10 이 E-stop 대상에 녹화를 넣은 뒤로는 heartbeat 가 끊기면 estopd 가
  **SIGKILL** 한다 (실측 2.5초, `reason=heartbeat_timeout`). 인코딩 도중에 끊기면 그
  에피소드는 잃는다. 의도한 안전 동작인지 재검토 대상인지는 아직 결정 안 됨 —
  `exclusivity.ESTOP_TARGETS` 가 그 결정의 자리다
- GPU 메모리 경합: 학습과 추론 동시 실행 시 OOM 위험 → 모드를 배타적으로 관리

## 환경 변수

`backend/.env`에서 `PIPER_` 접두사로 설정:
- `PIPER_PORT`: 서버 포트 (기본 8000)
- `PIPER_MODELS_DIR`: 모델 디렉토리
- `PIPER_DATASETS_DIR`: 데이터셋 디렉토리
- `PIPER_REDIS_URL`: 버스 주소 (기본 redis://127.0.0.1:6379/0). `ProcessManager` 가 모든 자식에게 넘긴다
- `PIPER_DISK_WARNING_THRESHOLD_GB`: 디스크 경고 임계치 (기본 10GB)
