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
