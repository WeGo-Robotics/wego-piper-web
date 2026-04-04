# Piper Web

LeRobot 웹 인터페이스 — 로봇 모방학습 프레임워크를 웹에서 제어.

## 실행 방법

### 요구사항

- Node.js 20+ (nvm use 24 권장)
- Python 3.11+
- LeRobot 0.5+ (`pip install lerobot`)
- 백엔드 의존성: `pip install fastapi uvicorn pydantic-settings pyzmq websockets huggingface-hub`

### 설치

```bash
# 프론트엔드
cd frontend && npm install

# 백엔드 (pyproject.toml 기반)
cd backend && pip install -e ".[dev]"
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
