# 외부 제어 API — 미션 수준으로 조종한다

외부 시스템(스크립트·상위 플래너·다른 머신의 오케스트레이션)이 이 로봇으로
**프로젝트(미션)를 수행**하기 위한 창구. `/api/ext/v1` 프리픽스.

> **◐ v1 구현됨** — [routers/external.py](../backend/app/routers/external.py).
> 미션(분리수거 루프) 제출·조회·취소, 시스템 상태, heartbeat, E-stop,
> yolod 제어, 검출 조회. 인증은 `PIPER_API_TOKEN` Bearer.

---

## 1. 원칙

### 미션 수준이다 — 장치 수준이 아니다

외부에 여는 것은 **"무엇을 해라"** 이지 "어느 관절을 몇 도로" 가 아니다.
준비(정책 배포·로봇/카메라 연결·캘리브레이션)는 로컬 운영자와 화면의 몫으로
남는다 — 그 경로들은 하드웨어 상태에 대한 사람 판단이 끼는 자리라서다.
외부가 하는 일: 미션 제출 → 진행 감시 → 중단. 내부 `/api/*` 전체를 외부에
그대로 여는 것은 **하지 않는다** — 외부 계약은 좁을수록 오래 산다.

### 안전 계약은 바뀌지 않는다 — 외부 호출자가 곧 운영자다

- **heartbeat 의무가 외부로 이전된다.** 브라우저가 하던 생존 신호를 외부
  클라이언트가 `POST /api/ext/v1/heartbeat` 로 보내야 한다 (500ms~1s 주기 권장).
  안 보내면 estopd 가 **2.5초 안에 팔을 세운다** — 이것은 버그가 아니라
  데드맨 설계다. 외부 시스템이 죽으면 로봇이 선다.
- E-stop 은 외부에서도 즉시 가능: `POST /api/ext/v1/estop`.
- 배타 가드(exclusivity)·estopd 감시는 내부 경로와 동일하게 적용된다 —
  외부라고 우회 경로가 생기지 않는다.

### 인증 — 기본 잠김

- `PIPER_API_TOKEN` (기기별 `backend/.env`). **미설정이면 `/api/ext/*` 전체가
  503** — 켜는 것이 명시적 행위다.
- 요청마다 `Authorization: Bearer <token>`. 실패는 401.
- 내부 `/api/*`(웹 UI)는 지금처럼 LAN 신뢰로 남는다. 게이트웨이를 공인망에
  내놓는 순간 이 전제가 깨지므로, 그때는 리버스 프록시에서 `/api/ext` 만
  내보내는 것이 배포 규칙이다.

### 버전을 못 박는다

내부 API 는 UI 와 함께 움직이지만 외부 계약은 소비자를 모른다.
`/api/ext/v1` — 깨는 변경은 v2 를 만든다.

---

## 2. 표면 (v1)

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/api/ext/v1/missions` | 미션 시작. body `{"type": "recycling", ...설정}` → `{"id"}`. 동시 1개 (오케스트레이터 싱글턴) |
| `GET` | `/api/ext/v1/missions` | 미션 이력 (저널 파일 목록, 최신 먼저) |
| `GET` | `/api/ext/v1/missions/{id}` | 진행 중이면 라이브 상태, 끝났으면 저널에서 회차 기록 |
| `POST` | `/api/ext/v1/missions/{id}/cancel` | 진행 중 미션 중단 |
| `GET` | `/api/ext/v1/status` | 종합 상태 — 실행 중 활동·E-stop·오케스트레이터·yolod·검출 카메라 |
| `GET` | `/api/ext/v1/detections` | 살아 있는 검출 (yolod 페이로드 그대로 — `text` 포함) |
| `POST` | `/api/ext/v1/vision/start` | yolod 기동 (`{"cams": {...}, "model", "conf"}`) |
| `POST` | `/api/ext/v1/vision/stop` | yolod 정지 |
| `POST` | `/api/ext/v1/heartbeat` | 데드맨 생존 신호 — **미션 중 의무** |
| `POST` | `/api/ext/v1/estop` | 긴급 정지 |

미션 타입은 지금 `recycling` 하나다 (설정 = `OrchestratorConfig`: 회차 수·대기·
규칙·프로바이더/모델·dry_run). **오케스트레이터 2단계(YAML 시나리오 스펙)가 오면
미션 = 스펙 제출**이 되고, 이 표면은 그대로 그 위에 얹힌다.

미션 `id` = 저널 run id (`run_YYYYmmdd_HHMMSS`). 끝난 미션도 같은 id 로
저널에서 회차 기록을 돌려준다 — 외부 시스템의 사후 집계 경로다.

## 3. 최소 클라이언트 시퀀스

```bash
T="Authorization: Bearer $PIPER_API_TOKEN"
B="http://<robot>:8000/api/ext/v1"

curl -H "$T" -X POST $B/vision/start -d '{"cams":{"top":"rs_..._color"}}'   # 인식 켬
while :; do curl -s -H "$T" -X POST $B/heartbeat; sleep 0.5; done &          # 데드맨 (별도 루프)
MID=$(curl -s -H "$T" -X POST $B/missions -d '{"type":"recycling","max_episodes":10}' | jq -r .id)
watch curl -s -H "$T" $B/missions/$MID                                       # 진행 감시
curl -H "$T" -X POST $B/missions/$MID/cancel                                 # 필요 시 중단
```

## 4. v1 에 없는 것 (의도적)

| 안 여는 것 | 이유 |
|---|---|
| 정책 배포·추론 시작 | 로봇·카메라 준비에 사람 판단이 낌. 미션 전제 조건으로 검사만 한다 (없으면 409 + 사유) |
| 장치 관리 (스캔·연결·캘리브레이션) | 하드웨어 상태는 현장 몫 |
| 녹화·학습 미션 | 다음 후보 — 미션 타입 추가로 들어온다 |
| WS 스트림 | v1 은 폴링 (`GET /missions/{id}`). 필요해지면 `/api/ext/v1/events` SSE |
| MCP 서버 | 외부 LLM 에이전트가 조종하려면 이 REST 위에 어댑터 하나 — 요구가 생기면 |
