# 문서 지도와 정리 계획

2026-09-23 전수 조사. **어디에 무엇이 있나**를 먼저 적고, 그다음에 **무엇이 문제인가**,
마지막에 **어떻게 정리할까**를 둔다. 정리가 끝나면 3·4절은 지우고 1·2절만 남는다 —
그때 이 문서는 "문서 지도"가 된다.

## 0. 숫자

| | |
|---|---|
| 전체 `.md` | **84** |
| git 이 추적 | 75 (vendor 제외 **72**) |
| 추적 안 함 | 9 — `.pytest_cache/README.md` ×4(스스로 무시), `.hostside/CHANGELOG.md`(gitignore됨), vendor 2, 이번 세션 미커밋 2 |

⚠ 도구 산출물은 **문제가 아니다.** `.pytest_cache` 는 자기 안에 `.gitignore` 를 갖고
있고 `.hostside` 는 `.gitignore` 에 있다 — 조사 중에 확인했다. 정리 대상에서 뺀다.

## 1. 어디에 무엇이 있나

| 자리 | 개수 | 맡은 것 | 상태 |
|---|---|---|---|
| **루트** | 7 | 들어오는 문 · 이력 · 고지 | 셋은 명확, 셋은 애매(§3-6) |
| **`feature/`** | 35 | 신기능 **기획**(만들기 전 생각 + 만든 뒤 실측) | 인덱스가 **22/34** 만 덮는다(§3-2) |
| **`refactor/`** | 21 | 구조 개편 작업 목록 | **14개가 제목에 ☑완료**, 전부 2026-08(§3-3) |
| **`docs/`** | 8 | 쓰는 사람 · 고치는 사람용 안내 | 역할이 가장 흐리다 |
| **`.rules/`** | 2 | — | **고아**. 아무도 안 가리킨다(§3-4) |
| **`deploy/`** | 1 | 배포 절차·실기 기록 | 살아 있다(506줄, 최신) |
| `vendor/` · `robot/` · `frontend/` | 6 | 서드파티 · 데이터 · 템플릿 | 손대지 않는다(§3-9 하나만 예외) |

### 루트

| 문서 | 줄 | 최종 | 무엇 |
|---|---|---|---|
| `README.md` | 275 | 09-21 | 밖에 보여주는 첫 화면. 설치는 스크립트 하나 |
| `CLAUDE.md` | 151 | 09-02 | AI·새 사람이 먼저 읽는 규칙·아키텍처 요약 |
| `CHANGELOG.md` | 652 | 09-21 | 버전마다 무엇이 달라졌나 |
| `ROADMAP.md` | 459 | 09-01 | 리팩터·신기능 구현 **순서** |
| `REF.md` | 454 | 08-15 | "LeRobot 웹 인터페이스 설계 문서" |
| `THIRD-PARTY-NOTICES.md` | 244 | 09-10 | 라이선스 고지 |
| `PiPER_AI_데모_시나리오_정리.md` | 241 | 08-12 | 데모 시나리오 원본 |

### `docs/` — 쓰는 사람·고치는 사람

| 문서 | 줄 | 최종 | 무엇 |
|---|---|---|---|
| `install-troubleshooting.md` | 221 | 09-15 | 설치가 멈추거나 죽을 때, **증상에서 찾는다** |
| `camera-troubleshooting.md` | 110 | 미커밋 | 카메라가 어느 단계에서 끊겼나 |
| `data-preprocessing.md` | 444 | 08-11 | 학습 데이터 전처리 파이프라인 |
| `robot-daemon-contract.md` | 77 | 09-09 | 외부 로봇을 웹에 붙이는 계약 |
| `qna.md` | 71 | 09-11 | 자주 묻는 것 |
| `inference-logs.md` | 69 | 09-11 | 추론 CSV 로그 형식 |
| `vibration_reduction.md` | 55 | **04-14** | 추론 중 진동 감소 방법론 |
| `doc-map.md` | — | 오늘 | 이 문서 |
| `architecture-c4.drawio` | — | — | C4 3계층 다이어그램(md 아님) |

### `feature/` — 기능마다 하나

`README.md` 가 인덱스다(표 + 구조 개편과의 관계).

⚠ **문서만 보고는 "이게 구현됐나" 를 알 수 없다.** 이 조사에서 분류를 시도했다가
그만뒀다 — 근거가 없으면 지어내는 것이기 때문이다. 실제 예: `llm-integration.md` 는
2026-08-16 에서 멈춰 기획처럼 읽히지만, `CLAUDE.md` 는 판단 LLM 이 **돌고 있다**고
적는다(`piper-ollama` 유닛 · `PIPER_LLM_PROVIDER` · `llm_client`). 문서와 코드를 한 쌍씩
대조해야 알 수 있고, 그 일 자체가 §4 의 1단계다.

말할 수 있는 것은 **언제 마지막으로 손댔나**뿐이다:

| 시기 | 문서 |
|---|---|
| 9월 (14개) | `gateway-auth`(미커밋) `vast-training` `so101-flipped` `readme-rewrite` `lighting-watch` `web-leader` `sim-scene-editor` `sim-env` `gray-card-calibration` `version-update` `so101d` `services` `act-delta` `joint-diagnostics` `alignment-check` `hf-account` `cloud-training` `camera-profiles` `act-aux` |
| 8월에서 멈춤 (15개) | `teleoperation` `episode-orchestrator` `service-restart` `depth-background-mask` `yolo-training` `llm-integration` `external-api` `episode-editor` `bimanual` `01-phase-annotation` `layout-redesign` `policy-ui-spec` `parameter-presets` `manual-control` `demo-scenario-gaps` |

⚠ **8월 = 낡음이 아니다.** 끝나서 안 건드리는 것과 방치된 것이 섞여 있고, 그 둘을 가르는
표시가 문서에 없다. 그게 §3-1 이다.

### `refactor/` — 구조 개편

`README.md` 가 목록이다. `01`~`13` 은 번호로 묶인 개별 항목(A·B·C급), 그 밖에
`camera-transport` · `robot-transport` · `daemon-split` · `daemon-inventory` ·
`robotd-safety` · `HARDWARE-CHECKLIST` 가 있다. **제목에 ☑완료가 붙은 것이 14개**이고
전부 2026-08 에 멈춰 있다.

## 2. 규칙이 어디에도 안 적혀 있다 — 이게 뿌리다

`README.md` 와 `CLAUDE.md` 를 다 뒤져도 **`docs/` · `feature/` · `refactor/` 를 어떻게
나누는지 적힌 곳이 없다**(조사로 확인). 그래서:

- 새 문서를 어디에 둘지 매번 감으로 정한다 — 이번 세션에도 카메라 트러블슈팅을
  `docs/` 에 둘지 `feature/` 에 둘지 근거 없이 골랐다
- `feature/services.md`(기능) 와 `feature/service-restart.md`(기능) 와
  `docs/install-troubleshooting.md`(안내) 가 같은 주제를 셋으로 나눠 갖는다
- 끝난 기획과 살아 있는 참조가 같은 폴더에 섞여, 어느 것이 지금 사실인지 알 수 없다

**정리보다 규칙이 먼저다.** 규칙 없이 옮기면 3개월 뒤 같은 상태로 돌아온다.

## 3. 찾은 문제 (근거와 함께)

| # | 문제 | 근거 |
|---|---|---|
| 3-1 | **문서가 자기 상태를 안 말한다** — 기획인지, 끝난 것인지, 지금 사실인지 | 위 분류를 조사자가 새로 매겨야 했다 |
| 3-2 | **`feature/README` 인덱스가 22/34** | 빠진 12: `act-delta` `alignment-check` `episode-editor` `external-api` `gateway-auth` `hf-account` `joint-diagnostics` `readme-rewrite` `services` `version-update` `web-leader` `yolo-training` |
| 3-3 | **`refactor/` 14개가 끝난 일** | 제목에 `☑ 완료`, 최종 수정 전부 2026-08 |
| 3-4 | **`.rules/` 2개가 고아** | 아무 문서·코드도 안 가리킨다. `plan-inference-ui.md` 는 **2026-04-04** |
| 3-5 | **이름 규칙이 갈린다** | `vibration_reduction.md`(snake) · `PiPER_AI_데모_시나리오_정리.md`(한글·루트) · `feature/01-phase-annotation.md`(feature 중 유일한 번호) |
| 3-6 | **`REF.md` 의 자리가 애매하다** | 454줄 "설계 문서". 가리키는 것은 `refactor/`·`.rules/` 같은 옛 문서뿐. `CLAUDE.md` 와 역할이 겹칠 수 있다 |
| 3-7 | **`ROADMAP.md` 가 3주 뒤처졌다** | 최종 09-01. 그 뒤 v0.5.2~v0.5.7 이 나갔다 |
| 3-8 | **`vast-training.md` 2355줄** | 세션 기록 `§12-1`~`§12-28` 이 누적. 설계와 일지가 한 파일에 |
| 3-9 | **`frontend/README.md` 가 Vite 기본 템플릿** | 2026-04-04, 내용은 `React + TypeScript + Vite` |
| 3-10 | **`docs/vibration_reduction.md` 가 5개월 방치** | 2026-04-14. 지금 코드와 맞는지 아무도 모른다 |

## 4. 정리 계획

순서가 중요하다 — **규칙(1)이 없으면 나머지가 다시 흩어진다.**

### 1단계 — 규칙을 적는다 *(먼저)*

`CLAUDE.md` 에 다섯 줄을 넣는다. 예:

| 자리 | 넣는 것 | 안 넣는 것 |
|---|---|---|
| `feature/` | 기능 하나의 **기획·결정·실측**. 만들기 전 생각과 만든 뒤 사실 | 사용법 |
| `docs/` | **쓰는 사람·고치는 사람**을 위한 안내 — 증상에서 찾는 것, 형식, 계약 | 왜 그렇게 만들었나 |
| `refactor/` | 구조 개편 항목. **끝나면 닫고 옮긴다** | 신기능 |
| 루트 | 들어오는 문(README)·규칙(CLAUDE)·이력(CHANGELOG)·고지 | 그 밖의 것 |

그리고 **모든 문서 머리에 상태 한 줄**을 규칙으로 한다:
`> 상태: 기획 | 구현됨 | 참조 | 보관` + 마지막 확인 날짜.

### 2단계 — 인덱스를 완성한다

- `feature/README.md` 에 빠진 12개를 넣는다(§3-2). 표에 **상태 칸**을 더한다
- `docs/README.md` 를 만든다 — 지금은 인덱스가 없다
- `refactor/README.md` 의 ☑완료를 실제 파일 상태와 대조한다

### 3단계 — 끝난 것을 치운다

- `refactor/` 의 완료 14개 → `refactor/done/` 으로 옮기고 README 에서 한 줄로 묶는다
  (지우지 **않는다** — 왜 그렇게 고쳤는지가 거기 있다)
- `feature/` 의 "끝난 기획" 13개는 **옮기지 않는다.** 실측이 같이 들어 있어 참조로 계속
  쓰인다 — 대신 1단계의 상태 줄로 구분한다

### 4단계 — 고아와 중복을 정한다 *(판단 필요)*

| 대상 | 후보 |
|---|---|
| `.rules/plan-inference-ui.md`(04-04) | 지운다 / `refactor/done/` 로 |
| `.rules/project.md` | `CLAUDE.md` 에 흡수 |
| `REF.md` | `CLAUDE.md` 와 겹치는 부분을 확인하고, 남는 것만 `docs/` 로 |
| `frontend/README.md` | Vite 템플릿을 지우고 한 줄로(빌드는 루트 README 에 있다) |
| `PiPER_AI_데모_시나리오_정리.md` | `docs/demo-scenario.md` 로 이름·자리 정리 |
| `docs/vibration_reduction.md` | 코드와 대조 → 맞으면 `vibration-reduction.md` 로, 아니면 보관 |

⚠ **넷은 사람이 정해야 한다.** 특히 `REF.md` 는 454줄이라 겹침을 확인하지 않고 옮기면
살아 있는 내용을 잃는다.

### 5단계 — 큰 문서를 쪼갠다

`feature/vast-training.md`(2355줄)를 둘로:
- `feature/vast-training.md` — 설계·결정·지금 사실
- `feature/vast-training-log.md` — `§12-1`~`§12-28` 세션 기록

⚠ **기록을 지우지 않는다.** 이 저장소의 세션 기록은 "왜 그렇게 했나"의 유일한 근거이고,
실측 수치가 거기 있다.

### 6단계 — 안 흩어지게 못 박는다

`backend/tests/` 에 문서 규칙 테스트를 더한다(이미 `test_install_docs.py` 가 같은 일을
한다):

- `feature/*.md` 가 전부 `feature/README.md` 에 링크돼 있나
- 모든 문서에 상태 줄이 있나
- 깨진 상대 링크가 없나

**이 단계가 없으면 1~5단계는 한 번 쓰고 마는 청소다.**

## 5. 손대지 않는 것

`vendor/` · `robot/piper_robot/data/README.md` · `.pytest_cache` · `.hostside` ·
`THIRD-PARTY-NOTICES.md` · `CHANGELOG.md` · `deploy/RELEASE-CHECKLIST.md`.
