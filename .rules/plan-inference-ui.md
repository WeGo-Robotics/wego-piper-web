# 추론 UI 구현 기획

> 목표: LeRobot 추론(inference)을 웹에서 제어하고 시각화하는 UI 완성
> 전제: REF.md Phase 1(안전) + Phase 2(데이터 수집)의 기반 위에 구축

---

## 단계 0: 프로젝트 스캐폴딩

프론트엔드와 백엔드의 기본 뼈대를 세운다. 추론 UI뿐 아니라 전체 프로젝트의 기초.

### 0-1. 백엔드 초기화
- [ ] FastAPI 프로젝트 생성 (`backend/app/main.py`)
- [ ] uvicorn 실행 스크립트, CORS 설정
- [ ] 환경 변수 관리 (`backend/app/core/config.py` + `.env`)
- [ ] 헬스체크 엔드포인트 (`GET /health`)

### 0-2. 프론트엔드 초기화
- [ ] Vite + React + TypeScript 프로젝트 생성 (`frontend/`)
- [ ] Tailwind CSS 설정
- [ ] API 클라이언트 기본 설정 (`frontend/src/services/api.ts`)
- [ ] 라우팅 설정 (React Router) — 페이지: 대시보드, 추론, 데이터수집

### 0-3. 개발 환경
- [ ] 백엔드/프론트엔드 동시 실행 스크립트
- [ ] 프록시 설정 (Vite → FastAPI)

---

## 단계 1: 프로세스 관리 기반

LeRobot CLI를 subprocess로 실행하고 상태를 추적하는 핵심 레이어.

> **아키텍처 노트: subprocess(단계 1)와 ZMQ(단계 3)의 관계**
> 둘은 대안이 아니라 **레이어가 다른 협력 관계**이다.
> - subprocess = 프로세스 생명주기 관리 (시작/종료/크래시 감지/stdout 수집)
> - ZMQ = 실행 중인 프로세스 내부와 실시간 통신 (파라미터 변경)
>
> subprocess만으로는 실행 중인 Python 객체를 수정할 수 없고, ZMQ만으로는 프로세스를 시작/종료할 수 없다.
> 단계 1에서 프로세스를 띄울 수 있어야 단계 3에서 그 프로세스와 ZMQ로 통신할 수 있다.

### 1-1. subprocess 매니저
- [ ] `backend/app/services/process_manager.py`
  - LeRobot CLI를 `subprocess.Popen`으로 실행
  - stdout/stderr 실시간 파싱 (비동기 스트림)
  - 프로세스 상태 추적 (idle / running / error)
  - graceful shutdown (SIGTERM → SIGKILL fallback)
- [ ] CLI 인자 매핑 테이블 (`backend/app/core/cli_mapping.py`)
  - 웹 UI 파라미터 → LeRobot CLI 인자 변환
  - LeRobot 업데이트 시 이 파일만 수정

### 1-2. WebSocket 로그 스트리밍
- [ ] `backend/app/routers/ws.py` — WebSocket 엔드포인트
  - subprocess stdout → WebSocket으로 실시간 전송
  - 프로세스 상태 변경 이벤트 전송
- [ ] `frontend/src/hooks/useWebSocket.ts` — WebSocket 커스텀 훅
  - 자동 재연결 로직
  - 메시지 타입별 디스패치

---

## 단계 2: E-stop 안전 경로

추론 실행 전 반드시 완성해야 하는 안전 장치.

### 2-1. Watchdog 프로세스
- [ ] `backend/app/services/estop_watchdog.py`
  - 독립 프로세스로 실행 (웹서버와 분리)
  - heartbeat 주기 감시 (기본 500ms)
  - 타임아웃 시 LeRobot 프로세스 강제 종료
  - WebSocket 끊김 감지 → 즉시 안전 정지

### 2-2. E-stop UI
- [ ] `frontend/src/components/EStopButton.tsx`
  - 화면 어디서든 접근 가능한 고정 위치 (position: fixed)
  - 크고 빨간 버튼, 키보드 단축키 (Escape)
  - 연결 상태 표시 (heartbeat alive/dead)

---

## 단계 3: ZMQ IPC 파이프라인

실시간 파라미터 변경의 핵심 통로.

### 3-1. LeRobot 래퍼
- [ ] `wrapper/lerobot_wrapper.py`
  - LeRobot 추론 루프를 실행하되, ZMQ PULL 수신 스레드 추가
  - 수신한 파라미터를 policy 객체에 직접 반영
  - Safe/Unsafe 파라미터 분류에 따른 검증 로직
  - **⚠️ CLI 래핑 원칙의 유일한 예외**: 이 래퍼는 LeRobot을 직접 import한다.
    policy 객체 속성을 런타임에 수정해야 하므로 subprocess CLI 래핑으로는 불가능.
    import 범위를 `load_policy` + `run_inference` 수준으로 최소화하여
    LeRobot 업데이트 시 영향 범위를 제한한다.

### 3-2. FastAPI ↔ ZMQ 브릿지
- [ ] `backend/app/services/zmq_bridge.py`
  - ZMQ PUSH 소켓으로 래퍼에 파라미터 전송
  - 소켓 재연결 처리
- [ ] `POST /api/params` 엔드포인트
  - 파라미터 범위 검증 (min/max 클램핑)
  - Unsafe 파라미터 변경 시 재시작 필요 경고 응답

---

## 단계 4: 로컬 모델·데이터셋 관리

로컬에 저장된 모델(체크포인트)과 데이터셋을 열람·관리하는 기능.
추론 시작(모델 선택 → 배포)도 이 단계에서 처리한다.

### 4-1. 로컬 모델 관리 API
- [ ] `backend/app/routers/models.py`
  - `GET /api/models` — 로컬 모델 목록 (경로, 크기, 날짜, policy 종류)
  - `GET /api/models/{model_id}` — 모델 상세 (config.json, 학습 파라미터, 파일 구조)
  - `DELETE /api/models/{model_id}` — 모델 삭제 (확인 절차 포함)
  - `POST /api/inference/start` — 모델 선택 → LeRobot 추론 CLI 실행
  - `POST /api/inference/stop` — 추론 중지
- [ ] `backend/app/services/model_scanner.py`
  - 로컬 체크포인트 디렉토리 스캔 (경로 설정 가능)
  - config.json 파싱하여 policy 종류, 학습 step, 하이퍼파라미터 추출
  - 디스크 사용량 집계

### 4-2. 로컬 데이터셋 관리 API
- [ ] `backend/app/routers/datasets.py`
  - `GET /api/datasets` — 로컬 데이터셋 목록 (경로, 크기, 에피소드 수, feature 구조)
  - `GET /api/datasets/{dataset_id}` — 데이터셋 상세 (에피소드 목록, 통계, feature 구조)
  - `GET /api/datasets/{dataset_id}/episodes/{ep_idx}` — 에피소드 상세 (길이, 타임스탬프, 썸네일)
  - `DELETE /api/datasets/{dataset_id}` — 데이터셋 삭제
  - `POST /api/datasets/{dataset_id}/edit` — `lerobot-edit-dataset` CLI 래핑
    - 에피소드 삭제 (`delete_episodes`)
    - 데이터셋 분할 (`split`)
    - 데이터셋 병합 (`merge`)
    - 피처 제거 (`remove_features`)
    - 데이터셋 정보 조회 (`info`)
- [ ] `backend/app/services/dataset_scanner.py`
  - 로컬 데이터셋 디렉토리 스캔
  - 메타데이터 파싱 (에피소드 수, 평균 길이, feature 구조, task description)
  - 디스크 사용량 집계 + 임계치 경고 (REF.md 9.4 — 카메라 2대 30fps → 시간당 수 GB)

### 4-3. 모델 관리 UI
- [ ] `frontend/src/pages/ModelsPage.tsx` — 모델 관리 페이지
- [ ] `frontend/src/components/ModelList.tsx`
  - 모델 카드형 목록 (이름, policy 종류, 크기, 날짜, 학습 step)
  - 정렬 (날짜/이름/크기) + 필터 (policy 종류)
  - "추론 시작" 버튼 → 원클릭 배포
  - "삭제" 버튼 (확인 다이얼로그)
- [ ] `frontend/src/components/ModelDetail.tsx`
  - config 정보 테이블 (policy 종류, 학습 파라미터, 아키텍처)
  - 파일 구조 트리뷰 + 개별 파일 크기
  - 디스크 사용량 표시

### 4-4. 데이터셋 관리 UI
- [ ] `frontend/src/pages/DatasetsPage.tsx` — 데이터셋 관리 페이지
- [ ] `frontend/src/components/DatasetList.tsx`
  - 데이터셋 카드형 목록 (이름, 에피소드 수, 크기, feature 요약)
  - 정렬 + 필터
  - "삭제" 버튼
- [ ] `frontend/src/components/DatasetDetail.tsx`
  - 에피소드 목록 테이블 (인덱스, 길이, 타임스탬프, task description)
  - 에피소드 선택 → 삭제 (체크박스 다중 선택 → `lerobot-edit-dataset` 호출)
  - 데이터셋 통계 (에피소드 수, 평균 길이, action 분포)
  - feature 구조 표시
- [ ] `frontend/src/components/DiskUsageBar.tsx`
  - 모델 + 데이터셋 전체 디스크 사용량 표시
  - 임계치 초과 시 경고

> **REF.md 5절 연동**: 데이터셋 편집은 `lerobot-edit-dataset` CLI를 그대로 래핑한다.
> 에피소드 삭제, 분할, 병합, 피처 제거 등 CLI가 지원하는 기능만 노출.
> 미지원 기능(task description 수정, feature 이름 변경)은 UI에서 제외.

---

## 단계 5: HuggingFace Hub 열람 및 가져오기

LeRobot의 모델(체크포인트)과 데이터셋은 HuggingFace Hub에 호스팅된다.
웹 UI에서 Hub을 탐색하고 로컬로 다운로드하는 기능.

### 5-1. Hub API 서비스
- [ ] `backend/app/services/hub_client.py`
  - `huggingface_hub` 라이브러리로 Hub API 호출
  - 모델 검색: `lerobot/` org 하위 모델 목록 조회, 태그/policy 종류 필터
  - 데이터셋 검색: `lerobot/` org 하위 데이터셋 목록 조회, task/로봇 종류 필터
  - 모델 카드(README) 및 메타데이터(config.json) 파싱
  - 데이터셋 카드 및 통계 정보(에피소드 수, feature 구조) 파싱

### 5-2. Hub API 엔드포인트
- [ ] `backend/app/routers/hub.py`
  - `GET /api/hub/models` — 모델 목록 (검색어, 태그 필터, 페이지네이션)
  - `GET /api/hub/models/{repo_id}` — 모델 상세 (카드, config, 파일 목록, 용량)
  - `GET /api/hub/datasets` — 데이터셋 목록 (검색어, task 필터, 페이지네이션)
  - `GET /api/hub/datasets/{repo_id}` — 데이터셋 상세 (카드, 에피소드 수, feature 구조)
  - `POST /api/hub/download` — 모델 또는 데이터셋 다운로드 시작
  - `GET /api/hub/download/status` — 다운로드 진행률 (WebSocket으로도 전송)

### 5-3. Hub 브라우저 UI
- [ ] `frontend/src/components/HubBrowser.tsx`
  - 탭: 모델 / 데이터셋
  - 검색바 + 태그 필터 (policy 종류, 로봇 종류, task 등)
  - 카드형 목록 (이름, 설명, 다운로드 수, 최근 업데이트)
- [ ] `frontend/src/components/HubModelDetail.tsx`
  - 모델 카드(README) 렌더링
  - config 정보 (policy 종류, 학습 파라미터)
  - 파일 목록 및 총 용량
  - "다운로드" 버튼 → 로컬 체크포인트로 저장
- [ ] `frontend/src/components/HubDatasetDetail.tsx`
  - 데이터셋 카드 렌더링
  - 통계 (에피소드 수, 평균 길이, feature 구조)
  - "다운로드" 버튼 → 로컬 데이터셋으로 저장
- [ ] `frontend/src/components/DownloadProgress.tsx`
  - 다운로드 진행률 바
  - 다운로드 큐 관리 (여러 항목 동시 다운로드)

> **단계 4와의 연결**: Hub에서 다운로드한 모델은 로컬 체크포인트 목록(단계 4)에 자동으로 나타나며,
> 바로 추론에 사용할 수 있다. Hub 브라우저 → 다운로드 → 체크포인트 선택 → 추론 시작의 흐름.

---

## 단계 6: RTC 파라미터 실시간 튜닝

추론 중 파라미터를 실시간으로 조절하는 핵심 UI.

### 6-1. 파라미터 슬라이더 컴포넌트
- [ ] `frontend/src/components/ParamSlider.tsx`
  - 범위 제한 슬라이더 (min/max/step 설정)
  - 현재 값 숫자 입력 지원
  - debounce로 과도한 전송 방지 (200ms)
- [ ] `frontend/src/components/InferenceControls.tsx`
  - Policy 종류별 슬라이더 그룹 표시
    - Flow-matching (Pi0, SmolVLA): `max_guidance_weight`, `execution_horizon`
    - ACT: `temporal_ensemble_coeff`, `n_action_steps`
  - Safe/Unsafe 파라미터 시각적 구분 (Unsafe는 잠금 + 경고)

### 6-2. 파라미터 프리셋
- [ ] 프리셋 저장/불러오기 (로컬 JSON)
- [ ] 기본 프리셋 제공 (보수적 / 공격적 / 기본값)

---

## 단계 7: 추론 상태 모니터링

실행 중인 추론의 상태를 실시간으로 확인.

### 7-1. 상태 대시보드
- [ ] `frontend/src/components/InferenceStatus.tsx`
  - 프로세스 상태: idle / loading / running / error
  - 현재 로드된 체크포인트 정보
  - 추론 루프 FPS
  - GPU 메모리 사용량

### 7-2. 로그 뷰어
- [ ] `frontend/src/components/LogViewer.tsx`
  - WebSocket으로 수신한 stdout 실시간 표시
  - 자동 스크롤 + 검색
  - 에러 라인 하이라이트

---

## 단계 8: 추론 시각화

모델의 예측과 실제 동작을 비교하는 시각화.

### 8-1. 카메라 피드 (추론 UI 이전에 카메라 스트리밍 인프라 필요)
- [ ] `frontend/src/components/CameraFeed.tsx`
  - WebRTC 연결로 카메라 스트림 표시
  - 멀티뷰 지원 (wrist cam + scene cam)

### 8-2. Trajectory 오버레이
- [ ] `backend/app/routers/inference.py`
  - `GET /api/inference/trajectory` (WebSocket) — predicted/actual action trajectory 데이터 전송
- [ ] `frontend/src/components/TrajectoryOverlay.tsx`
  - 카메라 피드 위에 Canvas 오버레이
  - 예측 궤적 (점선) vs 실제 궤적 (실선)
  - 색상으로 오차 크기 표현

---

## 단계 9: 성공/실패 로깅

추론 시도별 결과를 기록하고 집계.

### 9-1. 로깅 API
- [ ] `backend/app/routers/eval_log.py`
  - `POST /api/eval/log` — 시도 결과 기록 (성공/실패, 메모, 타임스탬프)
  - `GET /api/eval/stats` — 성공률, 최근 N회 결과, 체크포인트별 비교
  - 결과를 JSON 파일로 저장 (`data/eval_logs/`)

### 9-2. 평가 UI
- [ ] `frontend/src/components/EvalPanel.tsx`
  - 성공/실패 버튼 (추론 실행 후 수동 기록)
  - 간단한 메모 입력
  - 성공률 표시 (전체 / 최근 10회 / 체크포인트별)
  - 시도 히스토리 테이블

---

## 구현 순서 요약

```
단계 0  프로젝트 스캐폴딩              ← 모든 것의 기반
  ↓
단계 1  프로세스 관리 기반              ← subprocess + WebSocket 핵심 인프라
  ↓
단계 2  E-stop 안전 경로               ← 추론 실행 전 반드시 완성
  ↓
단계 3  ZMQ IPC 파이프라인             ← 실시간 제어의 핵심 통로
  ↓
단계 4  로컬 모델·데이터셋 관리          ← 로컬 자산 열람·편집·삭제 + 추론 시작
  ↓
단계 5  HuggingFace Hub 열람/가져오기  ← 모델·데이터셋 탐색 및 다운로드
  ↓
단계 6  RTC 파라미터 실시간 튜닝        ← 추론 중 제어
  ↓
단계 7  추론 상태 모니터링              ← 실행 상태 확인
  ↓
단계 8  추론 시각화                    ← predicted vs actual (카메라 인프라 필요)
  ↓
단계 9  성공/실패 로깅                 ← 평가 결과 집계
```

> 단계 0~2는 추론 UI 외에도 전체 프로젝트에서 공유하는 기반이므로 가장 먼저 구축한다.
> 단계 5(Hub)는 단계 4(로컬 체크포인트)와 연결 — Hub에서 다운로드한 모델이 로컬 목록에 나타남.
> 단계 8(카메라 스트리밍)은 Phase 2 데이터 수집 UI와 인프라를 공유하므로 병행 개발 가능.
