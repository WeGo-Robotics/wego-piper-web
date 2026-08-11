# 1. 작업 단계(phase) 라벨 — 상태 슬롯 추가 + 자동 라벨러 + 편집 UI

> **◐ 1~2단계 완료.** 라벨러는 [phase/piper_phase/](../phase/piper_phase/) 설치 패키지로,
> 분석 배치는 [phase_labeler.py](../backend/app/services/phase_labeler.py) 로 만들었다.
> 문서의 실측값이 **정확히 재현됐다** — ep0 830프레임, 갭 min/mean −35.0/−13.3,
> **50 에피소드 전부 3사이클**.
>
> 문서와 다르게 간 것 둘:
> - `done_still` 20 → **5**. 실측 꼬리가 5~25프레임이라 20이면 절반이 `DONE` 을 못 받는다.
> - 라벨러 위치를 `wrapper/phase_fsm.py` 가 아니라 **설치 패키지**(`pip install -e phase/`)로.
>   백엔드가 `wrapper/` 를 import 하려면 `sys.path` 조작이 필요한데,
>   `bus/` 와 같은 방식이면 양쪽이 깔끔하게 같은 코드를 쓴다.
>
> 착수 중 확인된 것: **임계값은 태스크마다 다르다.** `min_cube` 기본값(`hold_gap=-15`)으로
> `yeonwonju` 를 돌리면 0사이클이 16개인데, `-8` 로 낮추면 2개가 된다.
> 사이드카에 `params` 를 함께 저장하는 설계가 맞았다.

`observation.state`에 슬롯 하나를 추가해 "지금 로봇이 무슨 단계를 수행 중인지"를 넣는다.
그 값을 자동으로 채워주는 후처리 도구와, 결과를 영상 편집기처럼 확인·수정하는 UI를 만든다.

이 문서의 수치는 전부 실제 데이터셋 `wego-hansu/min_cube_071410`
(50 에피소드 / 31,349 프레임 / 15fps / cam: top·hand)에서 측정한 값이다.

---

## 0. 요약

| 구성 | 내용 |
|---|---|
| 데이터 형식 | `observation.state` `[7]` → `[8]`, 8번째 = 페이즈 코드 (0~6) |
| 자동 라벨러 | 관절 속도 + **그리퍼 지령/실측 갭** + 손목 카메라 변화율 → 인과적 FSM |
| 저장 | 원본을 건드리지 않고 사이드카(`meta/phase_labels.json`)에 먼저 저장 |
| 편집 UI | `/phase-editor` — 프레임 스크러버 + 신호 그래프 + 드래그 가능한 구간 바 |
| 굽기(export) | 확정 후 **새 데이터셋으로 내보내기** (비디오는 하드링크, 원본 보존) |
| 추론 | 오프라인 라벨러의 인과 코어를 wrapper에서 그대로 재사용해 실시간 추정 |

가장 중요한 설계 제약 두 가지를 먼저 적는다. 나머지는 전부 이 둘에서 파생된다.

1. **오프라인 라벨러와 온라인 추정기는 같은 코드여야 한다.**
   학습 데이터의 8번째 값이 추론 시에도 채워져야 하므로, 라벨러는 "미래를 보지 않고"
   (past-only) 계산 가능한 부분과 오프라인 전용 보정을 명확히 분리한다. (§3.4)
2. **원본 데이터셋은 절대 in-place로 고치지 않는다.**
   라벨은 사이드카에, 최종 결과는 새 데이터셋에. 잘못 구운 라벨로 50 에피소드를
   날리는 사고를 원천 차단한다. (§5)

---

## 1. 현재 데이터 형식 (실측)

LeRobot **v3.0**. 에피소드가 파일별로 나뉘지 않고 **하나의 parquet에 전부 concat**되어 있다.

```
wego-hansu/min_cube_071410/
├── meta/
│   ├── info.json                      # features, fps, total_episodes
│   ├── stats.json                     # 전역 정규화 통계
│   ├── tasks.parquet
│   └── episodes/chunk-000/file-000.parquet   # 에피소드별 length, from/to index, stats/*
├── data/chunk-000/file-000.parquet    # 31,349행 전부
└── videos/observation.images.{top,hand}/chunk-000/file-000.mp4   # 전 에피소드 연결
```

`data/.../file-000.parquet` 스키마:

```
action:            fixed_size_list<float>[7]
observation.state: fixed_size_list<float>[7]
timestamp: float / frame_index: int64 / episode_index: int64 / index: int64 / task_index: int64
```

`observation.state`의 7채널 = `joint1..6.pos` + `gripper.pos`
([info.json의 `features.observation.state.names`](../backend/app/services/dataset_scanner.py#L226-L229)).

에피소드 경계는 `meta/episodes/*.parquet`의 `dataset_from_index` / `dataset_to_index`,
비디오 경계는 같은 파일의 `videos/{key}/from_timestamp` / `to_timestamp`로 찾는다.

> 이 구조 때문에 "에피소드 하나만 고친다"가 불가능하다. 라벨을 구우려면 **parquet 전체를
> 다시 쓴다**. 다행히 비디오는 그대로라 하드링크로 재사용할 수 있다 (§5.2).

---

## 2. 데이터 형식 변경 — 8번째 슬롯

### 2.1 페이즈 정의

실측에서 나온 가장 중요한 사실: **한 에피소드 안에서 집기가 3번 반복된다.**
(min_cube_071410의 태스크가 "빨강·파랑·초록 큐브 순서대로 쌓기"라서 큐브 3개 = 3사이클)

```
ep | frames | 집기 사이클 | 물체 든 프레임 | 끝 정지구간
 0 |    830 |     3      |      331      |     25
 1 |    852 |     3      |      357      |     21
 2 |    724 |     3      |      266      |      6
 ... 10개 에피소드 전부 3사이클
```

따라서 페이즈는 **단조 증가가 아니라 순환**한다. 이걸 놓치면 라벨러가 첫 사이클 이후
전부 "완료"로 칠해버린다.

| 코드 | 이름 | 의미 | 다음 |
|---|---|---|---|
| 0 | `IDLE` | 정지·대기 (에피소드 시작 등) | 1 |
| 1 | `APPROACH` | 목표로 이동 중 (큰 관절 속도) | 2 |
| 2 | `ALIGN` | 목표 미세 접근 중 (감속, 손목 시야 변화 유지) | 3 |
| 3 | `GRASP` | 집는 중 (그리퍼 닫히는 중) | 4 / 1 |
| 4 | `HOLD` | 집기 완료 — 물체를 든 채 이동·배치 | 5 |
| 5 | `RELEASE` | 놓는 중 (그리퍼 열림) | **1** ← 다음 사이클 |
| 6 | `DONE` | 미션 완료 (마지막 정지 구간) | — |

- `3 → 1`은 **집기 실패**(그리퍼가 끝까지 닫힘 = 빈손)일 때의 복귀 경로다.
- `RELEASE → APPROACH`가 순환 고리. `DONE`은 에피소드 끝에서 한 번만 나온다.
- 페이즈 개수·이름은 설정으로 뺀다. 태스크마다 다르다 (핸드오버 태스크엔 `RELEASE`가 없음).

### 2.2 인코딩

8번째 값 = 페이즈 코드를 `float32`로 그대로 저장 (`0.0` ~ `6.0`).

`features.observation.state`를 이렇게 바꾼다:

```json
{ "dtype": "float32", "shape": [8],
  "names": ["joint1.pos", ..., "gripper.pos", "phase"] }
```

**정규화 주의**: LeRobot은 채널별 mean/std로 정규화한다. 페이즈는 에피소드 안에서 0~6을
오가므로 std가 0이 되진 않지만, 단일 페이즈만 존재하는 데이터셋을 굽는 경우
`std ≈ 0 → 0으로 나눔`이 된다. **굽는 단계에서 `std < 1e-6`이면 `1.0`으로 치환**한다. (§5.1)

### 2.3 `action`에는 넣지 않는다 (결정)

고려한 대안: `action`에도 슬롯을 추가해 정책이 다음 페이즈를 스스로 예측하게 하면
추론 시 온라인 추정기가 아예 필요 없어진다 (자기 예측을 되먹임).

채택하지 않은 이유:
- `action[7]`이 로봇으로 그대로 나가면 안 되므로 wrapper의 액션 경로 전부에서 잘라내야 한다
  (`_send_action`, 액션 필터, 파킹, 텔레메트리 — 누락 시 조용히 관절 하나가 잘못 움직인다).
- 되먹임 루프라 한 번 틀리면 회복 경로가 없다. 온라인 추정기는 매 프레임 센서에서 다시 계산한다.

`observation.state`에만 넣고, 추론 시엔 §6의 온라인 추정기가 채운다.

### 2.4 정직한 위험 — privileged information

학습 때의 페이즈는 **그리퍼 지령/실측 갭**처럼 "이미 일어난 일"에서 뽑은 값이라 정확하다.
추론 때는 같은 신호를 실시간으로 보고 추정하므로 몇 프레임 늦고 가끔 틀린다.
정책이 페이즈 채널에만 의존하도록 학습되면 (causal confusion) 추론에서 성능이 무너진다.

완화책 — **학습 시 페이즈 채널 드롭아웃**: 프레임의 10~20%에서 페이즈 값을 `-1`(unknown)로
바꿔 학습한다. 정책이 페이즈 없이도 동작하도록 강제하고, 온라인 추정기가 헷갈릴 때
`-1`을 내보내면 그 상황이 학습 분포 안에 들어온다.

굽기 옵션으로 `--phase-dropout 0.15`를 넣고, 기본값은 켜 둔다.

---

## 3. 자동 라벨러

### 3.1 신호 (전부 실측 검증됨)

**(a) 관절 속도** — `state[0:6]`의 프레임간 차분 L2 노름 × fps (deg/s).
비디오 디코딩 없이 parquet만으로 계산. ep0 기준 `p50=5.9`, `p90=42.8`, `max=119.7`.

**(b) 그리퍼 지령/실측 갭 — 가장 강력한 신호.**
`gap = action[6] - state[6]`. 그리퍼에 **지령은 0(완전 닫힘)인데 실측은 34에서 멈춰 있으면
물체가 물려 있다는 뜻**이다. ep0 실측:

```
gripper cmd  : 100 100 100  85   0   0   0   0  14  90 100 ...
gripper state: 100 100 100  97  34  34  34  34  34  79 100 ...
                              ↑ 갭 -35 지속 = 물체 파지 중
```

`gap`의 최소/평균 = `-35.0` / `-13.3`. 빈손으로 닫으면 실측이 0 근처까지 내려가 갭이
사라진다. **비전 없이 "집기 성공"을 판정할 수 있는 신호**라 FSM의 척추로 쓴다.

판정: `hold = (gap < -15) and (cmd < 20)`. 이 조건의 상승 에지 개수가 곧 집기 사이클 수이고,
10개 에피소드 전부에서 정확히 3이 나왔다 (§2.1 표).

**(c) 손목 카메라 변화율** — `hand` 프레임을 80×60 그레이스케일로 줄여 연속 프레임
절대차 평균. ep0: `p10=0.09`, `p50=1.23`, `p90=6.62`, `max=28.0`. 이동 중 6~8, 파지 중 0.1~0.5.

**(d) 근접도 비율 `(c)/(a)`** — 물체에 가까울수록 같은 관절 이동이 만드는 시야 변화가
커진다(시차). 집기 직전 3초 구간에서 실측:

| | 관절속도 중앙값 | 손목 변화율 | **비율** |
|---|---|---|---|
| 집기 직전 (3회 평균) | 0.0 ~ 6.7 | 0.84 ~ 2.58 | **0.153 ~ 0.308** |
| 이동 중 (속도 > 30) | 44.0 | 5.41 | 0.114 |
| 전체 | 5.9 | 1.23 | 0.106 |

**신호는 살아 있지만 약하다.** 3회 중 한 번은 이동 중 대비 1.3배밖에 안 됐다.
따라서 (d)는 `ALIGN` 판정의 **보조**로만 쓰고, 단독 근거로 삼지 않는다.
(a)+(b)가 주 신호, (c)+(d)가 보조.

### 3.2 FSM

프레임마다 아래 규칙을 순서대로 본다. 모든 임계값은 설정 파일로 뺀다 (§3.5).

```
공통: still = speed < 2.0 (deg/s), moving = speed > 20.0
      hold  = (cmd_gripper - state_gripper < -15) and (cmd_gripper < 20)
      closing = d(state_gripper)/dt < -20   (열림 100 → 닫힘 0 방향)
      opening = d(state_gripper)/dt > +20

IDLE(0)     → APPROACH  : moving 이 6프레임(0.4s) 연속
APPROACH(1) → ALIGN     : speed < 12 가 8프레임 연속  and  그리퍼 열림 상태
                          (보조: 근접도 비율 > 0.14 면 전이 확신도 up)
            → GRASP     : closing 감지 (ALIGN 을 건너뜀 — 빠른 집기)
ALIGN(2)    → GRASP     : closing 감지
            → APPROACH  : moving 이 8프레임 연속 (재접근)
GRASP(3)    → HOLD      : hold 가 5프레임(0.33s) 연속        ← 집기 성공
            → APPROACH  : 그리퍼가 닫힘 완료했는데 hold=False (빈손) → 재시도
HOLD(4)     → RELEASE   : opening 감지
RELEASE(5)  → APPROACH  : 그리퍼 열림 완료 and moving
            → DONE      : 이후 에피소드 끝까지 still 이 지속
어느 상태든 → DONE      : still 이 20프레임 이상이고 남은 프레임에 hold/closing 이 없음
```

**끝 정지구간 = 미션 완료**의 함정: 실측에서 에피소드 중간에도 `speed=0`인 순간이 흔하다
(트레이스에 0.0이 산재). 반면 **꼬리 정지구간은 5~25프레임(0.3~1.7초)으로 짧다.**
그래서 `DONE`은 "정지"만으로 판정하면 안 되고 **"정지 + 에피소드 끝까지 아무 일도 없음"**으로
판정한다. 이건 미래를 보는 조건이라 오프라인 전용이다 (§3.4).

히스테리시스: 모든 전이에 최소 연속 프레임 수를 두고, 각 구간에 **최소 길이 4프레임**을
강제한다. 짧은 구간은 앞 구간에 흡수한다.

### 3.3 사이드카 저장 형식

원본 데이터셋 폴더 안에 두되 LeRobot이 무시하는 파일명을 쓴다.

`meta/phase_labels.json`:
```json
{
  "version": 1,
  "phases": ["IDLE", "APPROACH", "ALIGN", "GRASP", "HOLD", "RELEASE", "DONE"],
  "params": { "still_speed": 2.0, "moving_speed": 20.0, "hold_gap": -15.0, "...": 0 },
  "episodes": {
    "0": { "segments": [[0, 42, 0], [43, 187, 1], [188, 231, 2], ...],
           "reviewed": true, "edited_by": "auto+manual", "note": "" },
    "1": { "segments": [...], "reviewed": false, "edited_by": "auto" }
  }
}
```
`segments`는 `[start_frame, end_frame(포함), phase_code]`. 에피소드 내 상대 프레임 번호.
구간 리스트로 저장하는 이유: 31,349개 값 배열보다 작고, UI에서 경계 드래그가 곧 편집이 된다.

`meta/phase_signals.parquet` (그래프용 캐시):
```
episode_index:int32, frame_index:int32, speed:float32,
gripper_gap:float32, wrist_diff:float32, proximity:float32
```
손목 변화율은 비디오 디코딩이 필요해 매번 계산할 수 없다. 분석 1회에 같이 떨궈 UI가 즉시 읽는다.
(31k 프레임 80×60 디코딩 = 수십 초 수준.)

### 3.4 인과성 — 오프라인/온라인 분리

라벨러를 두 층으로 나눈다. 이걸 지키지 않으면 §2.4의 위험이 현실이 된다.

| 층 | 내용 | 온라인 사용 |
|---|---|---|
| **인과 코어** | §3.2의 전이 규칙 전부. 과거 N프레임만 참조 | ✅ 그대로 재사용 |
| 오프라인 보정 | `DONE` 판정(미래 참조), 최소 구간 길이 흡수, 사이클 수 검증 | ❌ |

온라인에서는 `DONE`을 내보내지 않는다 (추론 중엔 미션 종료를 알 수 없다).
`RELEASE` 후 정지는 그냥 `IDLE`로 둔다.
학습 데이터의 `DONE`(6) 구간이 온라인에 안 나타나는 비대칭은 남지만, 어차피 에피소드
꼬리 1초짜리라 영향이 작다. 대안으로 **굽기 옵션 `--merge-done-into-idle`**을 제공한다.

`ALIGN`이 가장 취약하다. "집기 N프레임 전"처럼 미래를 보고 정의하면 온라인에서 재현이
불가능하므로, §3.2처럼 **감속 + 그리퍼 열림 + (보조)근접도**라는 인과 조건으로만 정의했다.
그 대가로 오탐이 는다 — 그래서 UI 수정이 필수다.

### 3.5 파라미터 튜닝

임계값은 로봇·태스크마다 다르다. `params`를 사이드카에 같이 저장하고, UI에서 슬라이더로
바꾸면 **선택한 에피소드 하나만 즉시 재분석**해 미리보기 한다 (신호는 캐시되어 있으므로 빠름).
만족하면 "전체 재분석".

---

## 4. 편집 UI

### 4.1 위치

`frontend/src/config/pages.ts`에 한 줄 추가한다
([pages.ts:79-84](../frontend/src/config/pages.ts#L79-L84) 형식).
Plotly를 쓰므로 `DebugLogsPage`처럼 **lazy import**해 메인 번들에서 분리한다
([pages.ts:19](../frontend/src/config/pages.ts#L19)).

```ts
const PhaseEditorPage = lazy(() => import('../pages/PhaseEditorPage'))
{ path: '/phase-editor', label: '구간 라벨',
  description: '에피소드 작업 단계 자동 분석·수정', component: PhaseEditorPage, card: true }
```

### 4.2 레이아웃

```
┌─ 데이터셋 [min_cube_071410 ▾]   [분석 실행] [파라미터 ▾]  50 ep · 12 확정 ──┐
├───────────┬───────────────────────────────────────────────────────────────┤
│ 에피소드   │  ┌ top ─────────┐  ┌ hand ────────┐   frame 312/830  20.8s   │
│  0 ✔      │  │              │  │              │   phase: HOLD (4)         │
│  1 ✔      │  │              │  │              │   사이클 2/3               │
│  2 ⚠ 2사이클│  └──────────────┘  └──────────────┘                          │
│  3        │  ◀◀  ◀  ▶(재생)  ▶  ▶▶     [━━━━━━━━●━━━━━━━━━━━━━━]          │
│  ...      ├───────────────────────────────────────────────────────────────┤
│           │ 페이즈 ▐0▐■■■1■■■▐2▐3▐■■■■4■■■■▐5▐■■1■■▐2▐3▐■4■▐5▐■1■▐3▐4▐6▐ │
│           │ 관절속도  ╱╲__╱╲______╱╲___╱╲____╱╲__                          │
│           │ 그리퍼   ▔▔▔╲___/▔▔▔╲___/▔▔▔╲___/▔  (cmd 실선/state 점선, 갭 음영)│
│           │ 손목변화 ▁▄█▂▁▁▁▂▄█▂▁▁▁▂▄█▂▁▁                                  │
│           │           ┆ ← 재생헤드(모든 트랙 공유)                          │
└───────────┴───────────────────────────────────────────────────────────────┘
```

- **페이즈 트랙**: 색칠된 구간 바. 경계를 드래그하면 인접 두 구간이 같이 늘고 준다.
  구간 클릭 후 숫자키 `0`~`6`으로 페이즈 변경, `S`=현재 재생헤드에서 분할, `M`=앞 구간과 병합.
- **그래프 3종**: 기존 [PlotlyChart](../frontend/src/components/PlotlyChart.tsx)를 그대로 쓴다.
  `markerX`가 이미 세로 재생헤드 선을 그리므로([PlotlyChart.tsx:89-92](../frontend/src/components/PlotlyChart.tsx#L89-L92))
  프레임 인덱스만 넘기면 동기화가 끝난다. 박스 줌/휠 줌/`hovermode: x unified`도 이미 있다.
- **프레임 스크러버**: [DebugLogsPage.tsx:224-240](../frontend/src/pages/DebugLogsPage.tsx#L224-L240)의
  `<input type="range">` + `<img src=...>` 패턴을 재사용. 데이터셋용 프레임 서빙 엔드포인트만
  새로 필요하다 (§5.3).
- **에피소드 리스트**: `✔` 확정 / `⚠` 이상 감지(사이클 수가 중앙값과 다름, `DONE` 없음,
  구간 4개 미만 등) / 무표시 = 미검토. **⚠ 먼저 보게 정렬**하는 게 이 툴의 핵심 가치다.
  50개를 다 볼 필요 없이 이상한 것만 고치면 된다.

### 4.3 단축키

| 키 | 동작 | | 키 | 동작 |
|---|---|---|---|---|
| `Space` | 재생/정지 | | `0`~`6` | 선택 구간 페이즈 지정 |
| `←` `→` | 1프레임 | | `S` | 재생헤드에서 분할 |
| `Shift+←→` | 10프레임 | | `M` | 앞 구간과 병합 |
| `,` `.` | 이전/다음 구간 경계 | | `Ctrl+Z` | 되돌리기 |
| `J` `K` | 이전/다음 에피소드 | | `Enter` | 이 에피소드 확정(`reviewed`) |

재생은 `requestAnimationFrame` + 프레임 이미지 프리페치. mp4를 `<video>`로 직접 재생하는
방법도 있으나, 에피소드가 하나의 mp4에 concat되어 있어 `from_timestamp` 오프셋 계산과
프레임 정확도 문제가 생긴다. **디코딩 캐시 기반 이미지 시퀀스가 프레임 단위 편집에 맞다.**
([decode-cache 엔드포인트](../backend/app/routers/datasets.py#L205)가 이미
`images/{key}/episode-{ep:06d}/frame-{n:06d}.png`를 만든다 — 그대로 쓴다.)

> 주의: 현재 decode-cache는 PNG로 저장해 용량이 크다. 이 툴용으로는 **JPEG(q=85)
> + 긴 변 320px 축소** 옵션을 추가한다. 31k 프레임 × 2캠 기준 PNG 수 GB → JPEG 수백 MB.

---

## 5. 굽기(export) — 새 데이터셋 생성

### 5.1 무엇을 고쳐야 하나

`{org}/{name}` → `{org}/{name}_phase`로 **복제하며 변환**한다. 고칠 곳 전부:

| 파일 | 변경 |
|---|---|
| `data/chunk-*/file-*.parquet` | `observation.state` `[7]`→`[8]`, 8번째에 페이즈 코드. 전체 재작성 |
| `meta/info.json` | `features.observation.state.shape=[8]`, `names += ["phase"]` |
| `meta/stats.json` | `observation.state`의 `min/max/mean/std/q01..q99` 배열 7→8개 |
| `meta/episodes/*.parquet` | `stats/observation.state/*` 컬럼 전부 7→8개 |
| `videos/**` | **변경 없음 → 하드링크** |
| `meta/tasks.parquet` | 그대로 복사 |
| `meta/phase_labels.json` | 근거 보존용으로 함께 복사 |

- `std < 1e-6` → `1.0` 치환 (§2.2).
- `--phase-dropout` 적용 시 해당 프레임은 `-1.0`으로 쓰고, **stats는 dropout 적용 후 값으로 계산**한다.
- 에피소드 stats를 놓치면 LeRobot이 로드 시점이나 정규화에서 shape 불일치로 터진다.
  **이게 가장 빼먹기 쉬운 부분이다.**

### 5.2 비디오는 하드링크

비디오가 데이터셋 용량의 대부분이다. 내용이 안 바뀌므로 `os.link()`로 건다.
같은 파일시스템이 아니면 `EXDEV`가 나므로 심볼릭 링크 → 복사 순으로 폴백한다.

```python
try: os.link(src, dst)
except OSError:
    try: os.symlink(src, dst)
    except OSError: shutil.copy2(src, dst)
```

하드링크라 **원본 데이터셋을 지우면 링크만 남고 실체가 사라지지 않는다**(inode 공유).
반대로 `_phase` 쪽을 지워도 원본은 안전하다. 다만 UI에 "비디오는 원본과 공유됨" 표시를 한다.

### 5.3 실행 방식

기존 [decode-cache](../backend/app/routers/datasets.py#L205)와 같은 패턴 —
`settings.grpc_python`으로 별도 프로세스를 띄우고 stdout을 WebSocket으로 흘린다.
분석·굽기 모두 수 분 걸릴 수 있으므로 요청 스레드에서 절대 돌리지 않는다.

> 현재 decode-cache가 쓰는 `_upload_pm`은 Hub 업로드와 공유되는 단일 ProcessManager라
> `409 다른 작업이 진행 중`으로 서로를 막는다. 페이즈 분석/굽기는 **전용 ProcessManager**를
> 새로 둔다 (업로드 중에도 분석은 돌 수 있어야 한다).

---

## 6. 추론 경로 (필수 — 이거 없으면 학습해도 못 쓴다)

### 6.1 로컬 추론 (`wrapper/lerobot_wrapper.py`)

지금 코드는 로봇 상태가 모델 기대 차원보다 **길면 잘라내는** 로직만 있다
([lerobot_wrapper.py:435-441](../wrapper/lerobot_wrapper.py#L435-L441)):

```python
_expected_state_len = _state_dim.shape[0] if hasattr(_state_dim, 'shape') else 0
if _expected_state_len > 0:
    obs_state = lerobot_obs.get("observation.state")
    if obs_state is not None and len(obs_state) > _expected_state_len:
        lerobot_obs["observation.state"] = obs_state[:_expected_state_len]   # 자르기만 함
```

8차원 정책 + 7채널 로봇이면 **짧은** 경우라 이 분기가 안 걸리고, 그대로 정책에 들어가
shape 불일치로 죽는다. `_prepare_observation` 안에서 **추정기 값을 append**해야 한다:

```python
if _expected_state_len == len(obs_state) + 1 and _phase_estimator is not None:
    phase = _phase_estimator.update(obs_state, last_action, wrist_frame)
    lerobot_obs["observation.state"] = np.append(obs_state, np.float32(phase))
```

추정기는 §3.4의 **인과 코어를 그대로 import**한다. 두 벌로 나뉘면 반드시 어긋난다 —
`wrapper/phase_fsm.py` 한 파일에 두고 백엔드 라벨러가 같은 파일을 import한다.
(`refactor/` 문서들이 지적하는 "같은 사실 두 곳" 문제를 처음부터 만들지 않는다.)

필요한 입력이 전부 이미 있다: 관절/그리퍼 실측은 `raw_obs`, 그리퍼 지령은 직전 액션,
손목 프레임은 카메라 딕셔너리.

### 6.2 원격 추론 (`wrapper/grpc_wrapper.py`)

`grpc_wrapper`는 로봇 obs 딕셔너리를 그대로 정책 서버에 보내고 서버가 `build_dataset_frame`을
돌린다. 페이즈 키를 **`gripper.pos` 뒤에 오도록** 딕셔너리에 넣어야 채널 순서가 맞는다
(`hw_to_dataset_features`가 키 순서로 채널을 만든다). 순서가 어긋나면 에러 없이
**조용히 잘못된 채널로 학습/추론된다.** 굽기 시 `names` 배열과 대조 검증을 넣는다.

### 6.3 UI 표시

추론 중 현재 추정 페이즈를 텔레메트리에 실어 InferencePage에 표시한다. 추정기가 헷갈리는
지점을 사람이 바로 볼 수 있어야 §2.4의 위험을 조기에 발견한다.

---

## 7. 백엔드 API

기존 `/api/datasets` 라우터에 붙인다 ([datasets.py](../backend/app/routers/datasets.py)).

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/api/datasets/{id}/phase/analyze` | 자동 분석 실행 (신호 계산 + FSM). body: `params`, `episodes?` |
| `GET` | `/api/datasets/{id}/phase/labels` | 사이드카 전체 (에피소드별 구간 + 검토 상태) |
| `PUT` | `/api/datasets/{id}/phase/labels/{ep}` | 한 에피소드 구간 저장 (수동 편집) |
| `GET` | `/api/datasets/{id}/phase/signals/{ep}` | 그래프용 신호 배열 (캐시에서) |
| `POST` | `/api/datasets/{id}/phase/preview/{ep}` | 파라미터만 바꿔 한 에피소드 재분석 (캐시 재사용, 빠름) |
| `GET` | `/api/datasets/{id}/episodes/{ep}/frames/{cam}/{idx}` | 프레임 이미지 (디코딩 캐시) |
| `POST` | `/api/datasets/{id}/phase/export` | 새 데이터셋으로 굽기. body: `target_name`, `dropout`, `merge_done` |
| `GET` | `/api/datasets/phase/status` | 분석/굽기 진행 상태 |

프레임 서빙은 [debug_logs.py:150](../backend/app/routers/debug_logs.py#L150)의
`FileResponse` 패턴을 그대로 따른다. 캐시가 없으면 404 + "디코딩 캐시를 먼저 생성하세요"를
UI에 띄우고 버튼을 노출한다.

> 라우트 순서 주의: 기존 `@router.get("/{dataset_id:path}")`가 catch-all이라
> ([datasets.py:49](../backend/app/routers/datasets.py#L49)) 그 **위에** 등록하지 않으면
> 새 경로가 전부 데이터셋 상세로 먹힌다. `/upload-status`가 이미 같은 문제를 안고 있다.

---

## 8. 구현 순서

각 단계가 끝나면 그 자체로 쓸모가 있게 자른다.

| # | 단계 | 산출물 | 검증 |
|---|---|---|---|
| 1 | ☑ [phase/piper_phase/fsm.py](../phase/piper_phase/fsm.py) — 신호 + 인과 FSM | numpy만. `pip install -e phase/` | ☑ **50 에피소드 전부 3사이클** |
| 2 | ☑ [phase_labeler.py](../backend/app/services/phase_labeler.py) | `phase_labels.json`, `phase_signals.parquet` | ☑ 4개 데이터셋 전수 + 이상 목록 |
| 3 | API (분석/조회/저장/프레임) | 라우터 + 전용 ProcessManager | curl 라운드트립 |
| 4 | `PhaseEditorPage` — 스크러버 + 그래프 + 구간 바 | 페이지 1개 | `npm run build` |
| 5 | 편집 인터랙션 (드래그/분할/병합/단축키/undo) | | 실제로 50개 검토해보기 |
| 6 | 굽기 → `{name}_phase` | 새 데이터셋 | `LeRobotDataset`로 로드, `state.shape==(8,)` |
| 7 | 학습 1회 (짧게) | 체크포인트 | 정규화 통계 에러 없이 스텝 진행 |
| 8 | wrapper 추론 경로 (로컬 → 원격) | append 로직 + 온라인 추정기 | 실기 추론, UI에 페이즈 표시 |

1~2단계만 해도 "에피소드 품질 검사"(사이클 수가 다른 이상 에피소드 찾기) 도구로 쓸 수 있다.

---

## 9. 검증

- **프론트엔드는 반드시 `cd frontend && npm run build`.**
  `npx tsc --noEmit`은 루트 tsconfig가 참조 전용이라 no-op이라 에러를 놓친다.
- **굽기 라운드트립**: 내보낸 데이터셋을 `LeRobotDataset`으로 열어
  `ds[0]["observation.state"].shape == (8,)`, `ds.meta.info["features"]["observation.state"]["names"][7] == "phase"`,
  `len(ds) == 원본 total_frames` 확인.
- **하드링크 확인**: `stat -c %h videos/.../file-000.mp4`가 2 이상.
- **채널 순서**: 구운 데이터셋에서 무작위 프레임 100개를 뽑아 `state[0:7]`이 원본과
  bit-identical한지 대조 (§6.2의 조용한 오류 방지).
- **인과성 회귀 테스트**: 오프라인 라벨러를 "프레임 t까지만 보이게" 잘라 돌린 결과와
  온라인 추정기 결과가 일치하는지. 어긋나면 미래를 보는 조건이 인과 코어에 섞인 것이다.

---

## 10. 결정이 필요한 것

1. **페이즈 개수** — 초안은 7개(`RELEASE` 포함). 큐브 쌓기엔 필요하지만 단순 픽앤플레이스엔
   과할 수 있다. 실제 태스크 목록을 보고 확정.
2. **`DONE` 처리** — 온라인에 안 나타나는 비대칭을 감수할지, `IDLE`로 병합할지.
   초안은 옵션으로 두고 기본은 분리 유지.
3. **한 슬롯이 맞나** — 페이즈 코드 대신 "현재 페이즈 내 진행도(0~1)"를 한 칸 더 주면
   정책이 "곧 집는다"를 알 수 있지만, 진행도는 미래를 알아야 계산되므로 온라인에서 추정
   불가에 가깝다. **한 칸 유지를 권장.**
4. **기존 데이터셋 소급 적용 범위** — 50 에피소드짜리 하나로 시작해 파라미터를 잡은 뒤
   나머지에 적용. 태스크가 다르면 파라미터도 다시 잡아야 한다.
5. **페이즈 채널 드롭아웃 비율** — 초안 0.15. §2.4 위험 대비 효과는 실제 학습해봐야 안다.
