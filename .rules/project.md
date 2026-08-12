# 프로젝트 규칙

## 디렉토리 구조

```
wego-piper-web/
├── backend/                  # FastAPI 서버
│   ├── app/
│   │   ├── main.py           # FastAPI 앱 진입점
│   │   ├── routers/          # API 라우터 모듈
│   │   ├── services/         # 비즈니스 로직 (프로세스 관리, 버스 브리지 등)
│   │   ├── models/           # Pydantic 스키마
│   │   └── core/             # 설정, 상수, 유틸리티
│   ├── requirements.txt
│   └── pyproject.toml
├── frontend/                 # 웹 UI
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/            # 페이지 컴포넌트
│   │   ├── components/       # 재사용 UI 컴포넌트
│   │   ├── hooks/            # 커스텀 훅 (WebSocket, 상태 등)
│   │   ├── services/         # API 호출 래퍼
│   │   └── types/            # TypeScript 타입 정의
│   ├── package.json
│   └── vite.config.ts
├── wrapper/                  # LeRobot 래퍼 (버스 수신 + CLI 실행)
│   └── lerobot_wrapper.py
├── REF.md                    # 아키텍처 설계 문서 (원본 참조)
├── CLAUDE.md
└── .rules/
```

## 기술 스택

- **백엔드**: Python 3.11+, FastAPI, uvicorn, redis, evdev
- **프론트엔드**: TypeScript, React, Vite, Tailwind CSS
- **IPC**: Redis (`piper_bus` 공유 계약 패키지) — `redis://127.0.0.1:6379/0`
- **스트리밍**: WebSocket (상태/로그), WebRTC (카메라)

## 아키텍처 원칙

1. **LeRobot과 완전 분리**: subprocess CLI 래핑만 사용. LeRobot을 직접 import하지 않음
2. **CLI 인자 매핑 테이블**: LeRobot CLI 인자 변경 시 매핑 테이블만 수정. 웹 서버 코드는 건드리지 않음
3. **크래시 격리**: 웹 서버 ↔ LeRobot 프로세스 ↔ E-stop watchdog 은 각각 독립 프로세스
4. **안전 우선**: E-stop은 반드시 독립 watchdog 프로세스. 파라미터 슬라이더에 min/max 클램핑 필수

## 코딩 규칙

- 백엔드: FastAPI 라우터별 모듈 분리. Pydantic v2 모델로 요청/응답 검증
- 프론트엔드: 페이지 단위 컴포넌트 + 재사용 컴포넌트 분리. 커스텀 훅으로 WebSocket/상태 관리 추상화
- 한글 주석 허용. 변수/함수명은 영문
- `.env`로 환경별 설정 관리 (포트, 버스 주소, 카메라 디바이스 경로 등)
