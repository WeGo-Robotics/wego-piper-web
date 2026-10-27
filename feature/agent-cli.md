# 로컬 AI CLI 협력 — 코딩 에이전트가 로봇 옆에서 일하는 다섯 자리

> **☑ 1~5단계 + 설정 탭(4-7) + 웹 채팅(4-6, 사용자 결정) (2026-10-27).** 이 머신의 로컬 AI CLI 는 Claude Code 2.1.307 하나다(codex·gemini·aider 없음).
> 게이트웨이가 쓰는 로컬 LLM(`piper-ollama`, qwen2.5:7b)은 별개다 — 그쪽은 [llm-integration](llm-integration.md),
> 여기는 **사람 옆에서 터미널을 쥔 에이전트**와의 협력이다.

## 0. 요지

발판은 이미 둘이다. 플러그인은 "코딩 에이전트가 첫 사용자" 로 설계됐고([plugins §7-2](plugins.md)),
외부 API 문서는 MCP 서버를 "요구가 생기면 REST 위에 어댑터 하나" 로 미뤄 두었다([external-api §4](external-api.md)).
그 위에 다섯 자리를 얹는다. 값 대비 비용 순이고, 앞의 둘은 **어느 CLI 에나** 붙는다.

| # | 자리 | 한 줄 | 비용 |
|---|---|---|---|
| 1 | 에이전트용 컨텍스트 꾸러미 | 실패가 뜬 자리에서 [에이전트용 컨텍스트 복사] — 버전·유닛·활동·E-stop·저널·미션 저널을 한 장으로 | 반나절 |
| 2 | 플러그인 루프를 스킬·훅으로 | "이 플러그인을 X 로 바꿔 줘" 가 new → check → dry-run → install 로 사람 개입 없이 돈다 | 몇 시간 |
| 3 | MCP 서버 `piper-mcp` | ext v1 을 도구로. **stdio** — 에이전트가 죽으면 서버가 죽고 heartbeat 가 끊겨 팔이 선다 | 하루 이틀 |
| 4 | ext v1 읽기 전용 확장 | 데이터셋·에피소드·평가·학습 상태·진단을 GET 으로 — MCP 리소스의 재료 | 반나절 |
| 5 | 에피소드 묶음 | 페이즈 경계 키프레임 + 타임라인 + 상태·액션 표를 zip 하나로 — 이미지를 읽는 CLI 가 "왜 빗나갔나" 를 본다 | 하루 |
| — | (보류) 웹에서 에이전트 작업 | 플러그인 상세의 [에이전트에게 고치기] — 헤드리스 CLI 를 호스트 유닛으로 | 1~5 뒤 수요 확인 |

## 1. 왜

세 가지 반복이 있다.

- **디버깅 재료가 흩어져 있다.** "추론이 왜 죽었나" 를 묻기 위해 사람이 버전·유닛 상태·저널·미션 저널을
  네 화면에서 모아 붙여넣는다. 재료는 전부 API 에 있는데 모으는 손만 사람이다.
- **플러그인은 에이전트가 짜라고 만들었는데, 순서는 산문에만 있다.** [plugins/README.md](../plugins/README.md) 의
  "검증 순서" 를 세션마다 다시 설명하고, `check` 의 결과는 `✓`/`✗` 문자열이라 에이전트가 파싱한다.
- **에이전트가 로봇을 움직이려면 데드맨을 설명해야 한다.** ext v1 은 curl 로 쓸 수 있지만 heartbeat 를
  **별도 루프**로 돌려야 하고([external-api §3](external-api.md) 최소 시퀀스), LLM 의 턴은 500ms 주기를
  못 지킨다. "에이전트가 heartbeat 도구를 부르는" 설계는 처음부터 틀렸다 — 프로세스가 펌프해야 한다.

## 2. 지금 이런 상태다 — 재료는 다 있다

| 재료 | 어디 | 비고 |
|---|---|---|
| 미션 수준 외부 API | [routers/external.py](../backend/app/routers/external.py) — 미션 제출·조회·취소, 플러그인 시작·정지, 상태, 검출, detectord, heartbeat, E-stop | Bearer `PIPER_API_TOKEN`, 미설정 503. `test_auth_covers_every_route` 가 모든 라우트의 인증을 구조적으로 강제 |
| 플러그인 터미널 루프 | [piper_plugin/__main__.py](../plugin/piper_plugin/__main__.py) — `new`·`check`·`run`(`--dry-run`, `--api`) | 게이트웨이가 켜는 것과 같은 진입점 |
| 에이전트용 문서 | [plugins/README.md](../plugins/README.md) — 계약 한 장·하지 말 것·검증 순서 | §7-2 가 정한 "에이전트가 읽는 파일" |
| 저널 | `GET /api/system/logs?unit&level&since&format=text` — 유닛 목록은 `log_unit_options()` 계약, 컨테이너에서는 unitd 가 읽어 준다 | 같은 메시지는 `count` 로 접힌다 |
| 버전·서비스·활동·E-stop | `GET /api/system/version`·`/services`(낡은 코드 표식)·`GET /api/activity`·`estop_bridge.status()` | 상태바·서비스 패널이 이미 쓴다 |
| 미션 저널 | `<runtime>/runs/<run_id>.jsonl`, `GET /api/ext/v1/missions/{id}` | 회차별 실패 원인(검출/LLM/VLA) |
| 디버그 런 | `GET /api/debug/runs/*` — JSONL + jpg + zip 내려받기 | 추론 디버그 레코더 |
| 에피소드 프레임·페이즈 | `GET /api/datasets/{id}/episodes/{ep}/frames/{cam}/{i}`, `GET /api/phase/{id}/signals/{ep}`·`/labels` | 뷰어가 쓰는 디코딩 캐시 |
| 시스템 메시지 | [SystemMessages.tsx](../frontend/src/components/SystemMessages.tsx) — 알림함 `history`(id·level·text·source·at) | 페이지 무관, 같은 자리 |
| 호스트 유닛 제어 | [daemons/unitd.py](../daemons/unitd.py) — `UNIT_CATALOG` 의 유닛만 start/stop/restart/enable/disable | 컨테이너 게이트웨이에는 `systemctl` 이 없다 |
| 로컬 CLI | Claude Code 2.1.307 — `-p`, `--output-format stream-json`, `--json-schema`, `--allowedTools`, `--add-dir`, `--mcp-config`, `--max-budget-usd`, `claude mcp add -t stdio\|http -s project` | 저장소 `.claude/` 는 gitignore, 안에는 권한 허용 목록만. 스킬·훅·`.mcp.json` 없음 |
| MCP SDK | 어느 venv 에도 없다 | 3단계의 선택 의존 |

## 3. 원칙

1. **에이전트는 외부 호출자다.** [external-api §1](external-api.md) 그대로 — 미션 수준만, 장치 수준(추론 시작·
   조그·캘리브레이션) 없음, heartbeat 의무 이전, E-stop 은 늘 열림.
2. **heartbeat 는 프로세스가 펌프한다, 턴이 아니라.** 그리고 **자기가 시작한 미션이 살아 있을 때만.** 상시 펌프는
   다른 운영자(브라우저)의 죽음을 가린다 — 브라우저가 죽었는데 MCP 서버가 박동을 이어 주면 데드맨이 거짓이 된다.
3. **개발 시점 협력이 먼저다.** 런타임 LLM 플래너 폐기([plugins §15](plugins.md) 결정 3)는 유지한다. 에이전트는
   플러그인을 **짜고**, 미션을 **제출·감시** 한다. 스텝 시퀀스를 런타임에 짓지 않는다.
4. **어느 CLI 에나 붙는 것부터.** 복사·붙여넣기(4-1)와 파일 묶음(4-5)은 CLI 를 가리지 않는다. Claude Code
   전용(스킬·훅·MCP 등록)은 그 다음이고, 설치된 CLI 가 늘면 그때 넓힌다.
5. **비밀은 꾸러미에 안 들어간다.** 토큰·API 키·`.env` 값은 어느 출력에도 없다. 구현이 `.env` 를 읽지 않고
   상태 API 만 모으는 것으로 구조에서 막고, 테스트가 토큰 문자열을 찾아 실패한다.

## 4. 설계

### 4-1. 에이전트용 컨텍스트 꾸러미

`GET /api/system/agent-context` → `text/markdown`. 내부 API(세션 쿠키·LAN) — 외부 토큰 경로는 4-4 에서
같은 본문을 낸다.

| 인자 | 기본 | 뜻 |
|---|---|---|
| `since` | `30 minutes ago` | 저널 범위 (journald 문법, `/api/system/logs` 와 같다) |
| `unit` | `all` | 유닛 하나로 좁힐 때 |
| `lines` | 200 | 유닛당 저널 상한 — 붙여넣기 한도 안에 두기 위해 |
| `format` | `markdown` | `json` 이면 같은 내용을 구조로 |

본문 순서 — 위가 "지금", 아래가 "왜":

1. **머리** — 버전(`version.collect`), 시각, 배포형(컨테이너/소스), 머신 이름
2. **유닛** — `/services` 의 상태 + **낡은 코드** 표식. "고쳤는데 왜 안 되지" 의 첫 답
3. **활동** — `exclusivity.snapshot()`, 도는 정책(`inference_state.get()`: 종류·체크포인트), 라이브 미션(`registry.live_mission()`)
4. **E-stop** — `armed`, `last_trigger`(reason 포함), `timeout_ms`
5. **장치** — 등록 로봇·카메라 중 연결된 것과 사라진 것 (`device_watch` 가 이미 갖고 있다)
6. **저널** — `since` 안의 **warning 이상**, 유닛별, `count` 접힘 그대로 (`syslog.fetch`)
7. **미션 저널 꼬리** — 라이브 또는 마지막 run 의 끝 20줄
8. **읽을 곳** — CLAUDE.md, [services.md](services.md), 활동에 해당하는 feature 문서 경로, 더 볼 때의
   URL(`/api/system/logs?...&format=text`)

**시스템 메시지 알림함은 서버가 모른다** — 프론트 상태다. 그래서 복사 버튼은 서버 본문 **앞에** 알림함(`history`)을
붙여 클립보드에 넣는다. 서버 꾸러미만 받는 curl 경로는 그 절이 빈다 — 그게 맞다, 저널에 같은 사건이 있다.

**화면** — 둘, 둘 다 `t('에이전트용 컨텍스트 복사')`:
- [SystemMessages](../frontend/src/components/SystemMessages.tsx) 알림함 머리 — 실패를 **본 자리**에서 바로
- [SystemLogPanel](../frontend/src/components/SystemLogPanel.tsx) 의 [내려받기] 옆 — 로그를 보다가

**터미널** — `curl -s http://<robot>:8000/api/system/agent-context | claude -p "추론이 왜 죽었나"`. 로그인을
켰으면 세션 쿠키를 `-b` 로 준다 — 기본은 꺼져 있다([gateway-auth](gateway-auth.md)).

**크기** — 유닛 열 개 × 200줄이 상한이라 수십 KB. 붙여넣기 한도 안이고, 넘치면 `since` 를 좁히라고 머리에 적는다.

**비밀 가드** — 서비스는 `settings` 의 토큰·키 필드를 **읽지 않는다**(상태 API 결과만 모은다). 테스트는
`api_token`·`anthropic_api_key`·`llm_api_key` 에 더미를 넣고 본문에 그 문자열이 없음을 확인한다. 저널에 비밀이
찍히는 것은 이 기능의 책임 밖이지만, 생기면 그 데몬의 버그다.

**☑ 구현 (2026-10-27)** — [services/agent_context.py](../backend/app/services/agent_context.py) +
`GET`/`POST /api/system/agent-context`, 테스트 [test_agent_context.py](../backend/tests/test_agent_context.py).
설계와 다른 점 둘: 알림함은 클라이언트가 앞에 붙이는 대신 **POST 본문 `inbox`** 로 보내고 서버가 절 0 으로 앉힌다
(배치는 서버가 소유). 지움은 설정값 + 흔한 토큰 모양(`Bearer …`·`hf_…`·`sk-ant-…`) 둘 다. 모르는 유닛·형식은 400.
이 파일의 한국어는 화면 문구가 아니라 `tools/i18n.py` 의 `PY_SKIP_FILES` 로 번역에서 뺐다.

### 4-2. 플러그인 루프를 스킬·훅으로

지금도 되는 길을 **일급**으로 만드는 일이다. 세 조각.

**스킬** `.claude/skills/piper-plugin/SKILL.md` — 트리거는 "플러그인 만들어/고쳐/이걸 X 로 바꿔". 내용은
(a) 먼저 읽을 것: [plugins/README.md](../plugins/README.md) 와 참고 예제 `recycling`·`detector`,
(b) 순서: `new` → 코드 → `check --json` → `run --dry-run` 저널 1회차 확인 → `install replace: true` → 게이트웨이 `start`,
(c) 하지 말 것 요약(장치 직접 열기·`/api/auth`·`time.sleep`·금지 단어·브랜드 이름),
(d) 끝내는 기준: `check` ok, 드라이런 저널에 회차 1개, 화면 문구는 한국어 원문.
스킬은 README 를 **반복하지 않는다** — 경로와 순서만 든다. 정본은 README.

**`piper-plugin check --json`** — `{"ok": bool, "name", "version", "entry", "permissions": [], "cameras": [], "arms": [],
"scopes_checked": bool, "errors": [{"where", "msg"}]}`. 에이전트가 `✓` 를 찾지 않게. 사람용 출력은 그대로.

**훅** — `.claude/settings.json` 의 `PostToolUse`(`Edit|Write`) → `tools/hooks/plugin-check.sh`: 바뀐 `file_path` 가
`plugins/<name>/` 아래면 그 폴더에 `check --json` 을 돌려 `ok: false` 를 stderr 로 돌려준다. 고친 직후 틀린
매니페스트를 바로 안다.

**다른 CLI** — `AGENTS.md` 를 CLAUDE.md 로 심링크. 지금 설치된 CLI 는 하나라 비용 0 의 대비일 뿐이다.

⚠ `.claude/` 가 gitignore 다(`.gitignore:35`). 스킬·훅을 저장소가 들려면 `settings.local.json` 만 빼고 열어야 하고,
지금 `settings.json` 에 있는 개인 경로 허용 목록은 `settings.local.json` 으로 옮긴다 — §8 결정 1.

**사용자에게 배포** — 플러그인 패키지에 스킬 파일을 실어 `piper-plugin skill install` 로 `~/.claude/skills/` 에
복사하는 길이 있다. 2단계를 저장소 안에서 써 본 뒤 결정한다.

**☑ 구현 (2026-10-27)** — 스킬 `.claude/skills/piper-plugin/SKILL.md`, 훅 `tools/hooks/plugin-check.sh`
(`.claude/settings.json` PostToolUse — 폴더 밖·SDK 미설치는 조용히 0, 실패는 exit 2 로 에이전트에게 되돌린다),
`piper-plugin check --json`(사람용 출력과 같은 판정), `AGENTS.md → CLAUDE.md`. `.gitignore` 는 `.claude/*` 를 막고
`settings.json`·`skills/` 만 연다 — 개인 허용 목록은 `settings.local.json` 으로 옮겼다(§8 결정 1 반영).
사용자 배포(`skill install`)는 아직이다.

### 4-3. MCP 서버 `piper-mcp` — REST 위의 어댑터, stdio

**위치** — `plugin/piper_plugin/mcp.py`, 콘솔 스크립트 `piper-mcp`, 선택 의존 `piper_plugin[mcp]`(`mcp` SDK).
게이트웨이 이미지에는 넣지 않는다 — 서버는 **에이전트가 도는 기계**(개발 PC·노트북)에서 돌고 로봇과는 REST 로
말한다. SDK 가 `lerobot` 을 import 하지 않는 규칙도 그대로다.

**등록** — `claude mcp add -s project -e PIPER_API_TOKEN=... piper -- piper-mcp --api http://<robot>:8000`.
저장소에는 `.mcp.json` 예시를 둔다(토큰은 환경변수 참조, 값은 없다).

**전송은 stdio 다 — 데드맨 때문에.** stdio 서버는 CLI 의 **자식**이다. 에이전트가 죽으면(창을 닫든, 크래시든)
stdin 이 닫혀 서버가 끝나고 heartbeat 가 끊기며 estopd 가 2.5초 안에 세운다. 데드맨이 프로세스 수명에 묶인다 —
[external-api §1](external-api.md) "외부 시스템이 죽으면 로봇이 선다" 가 추가 코드 없이 성립한다.
HTTP(게이트웨이에 마운트)는 클라이언트 사망을 못 보고 게이트웨이에 의존성을 늘린다. 클라이언트 쪽에 파이썬을
못 까는 날이 오면 그때 HTTP 를 **덧붙인다**(§5).

**도구** — ext v1 과 1:1. 이름은 `piper_` 접두사.

| 도구 | REST | 비고 |
|---|---|---|
| `piper_status` | `GET /status` | 활동·E-stop·플러그인·미션·detectord 한 장 |
| `piper_missions` / `piper_mission_get(id)` | `GET /missions`, `GET /missions/{id}` | 끝난 미션도 저널에서 |
| `piper_mission_start(type, config, dry_run=True)` | `POST /missions` | **`dry_run` 기본 true** — 실물은 에이전트가 명시해야 한다 |
| `piper_mission_cancel(id)` | `POST /missions/{id}/cancel` | |
| `piper_plugins` / `piper_plugin_start(name, config, cameras, arms)` / `piper_plugin_stop(name)` | `GET/POST /plugins…` | 비미션 플러그인(검출기 등) |
| `piper_detections` | `GET /detections` | |
| `piper_vision_start(cams, model, conf)` / `piper_vision_stop` | `POST /vision/…` | |
| `piper_estop` | `POST /estop` | 항상 노출. 설명 첫 줄이 "지금 멈춘다" |

**heartbeat 는 도구가 아니다.** 서버가 펌프한다 — 규칙 셋:
- `piper_mission_start` 가 성공하면 그 id 로 0.5초 주기 스레드를 켠다.
- `GET /missions/{id}` 가 끝남·취소·실패를 보이면 끈다. 서버 종료(stdin EOF·SIGTERM)에도 끈다 — 그게 곧 "끊김" 이다.
- **자기가 시작하지 않은 미션에는 펌프하지 않는다.** 브라우저가 시작한 추론을 MCP 가 살려 두면 안 된다(§3 원칙 2).

플러그인이 `resources.heartbeat: true`(무인 운용)면 플러그인 프로세스가 스스로 박동한다 — 그 미션은 에이전트가
죽어도 선 채로 돈다. 매니페스트의 선택이고 MCP 가 뒤집지 않는다. 번들 `recycling` 은 `false` 다.

**리소스** — 4-4 의 읽기 전용 라우트를 MCP 리소스로 낸다(`piper://context`, `piper://datasets`, …). 도구 없이
"읽기" 로 붙는다.

**테스트** — `httpx.MockTransport` 로 가짜 게이트웨이([test_llm_client](../backend/tests/test_llm_client.py) 패턴):
미션 시작 → 펌프 시작, 끝남 → 펌프 종료, 시작 안 한 미션 → 펌프 없음, `dry_run` 기본값. 실기 인수: Claude Code 에서
`status → mission(dry_run) → cancel` 왕복, 그리고 **CLI 를 kill 했을 때 estopd 저널에 `heartbeat_timeout` 이
2.5초 안에** 남는 것.

**☑ 구현 (2026-10-27)** — [piper_plugin/mcp.py](../plugin/piper_plugin/mcp.py), 콘솔 `piper-mcp`, 선택 의존
`piper_plugin[mcp]`(`mcp>=1.2` — v1 FastMCP·v2 MCPServer 둘 다 받는다), 저장소 루트 `.mcp.json`(토큰은
`${PIPER_API_TOKEN:-}` 로 환경변수 참조). 핵심(`PiperMcp`·`HeartbeatPump`)은 SDK 없이 import 되고 테스트된다 —
[test_piper_mcp.py](../backend/tests/test_piper_mcp.py). 설계와 같은 점: 도구 12개 = ext v1 1:1, heartbeat 는 도구가
아니라 펌프(자기 run 에만, 끝남·취소·404 에 멈춤), `dry_run` 기본 true, 게이트웨이 오류는 상태·detail·다음 할 일을 든
ToolError 로. 끝까지 확인한 것: 가짜 게이트웨이에 실제 stdio 서버 프로세스를 띄워 미션을 시작하고 **클라이언트를
닫으면(부모가 죽은 상황) 박동이 멈춘다**. 남은 인수: 실기에서 CLI 를 kill 하고 estopd 저널의 `heartbeat_timeout` 을 보는 것.

### 4-4. ext v1 읽기 전용 확장

MCP 리소스의 재료이자 외부 집계 경로다. 전부 `GET`, 깨는 변경 없음, `test_auth_covers_every_route` 가 자동으로 덮는다.

| 경로 | 내용 | 안의 재료 |
|---|---|---|
| `GET /api/ext/v1/diag/context` | 4-1 과 같은 본문 (`format` 인자 포함) | 같은 서비스 |
| `GET /api/ext/v1/datasets` | 로컬 데이터셋 목록 — 이름·에피소드 수·fps·카메라·구운 사본 여부 | `dataset_scanner` |
| `GET /api/ext/v1/datasets/{id}/episodes/{ep}` | 에피소드 요약 — 길이·task·페이즈 라벨·노트 | 페이즈 사이드카·`notes_sidecar` |
| `GET /api/ext/v1/eval/stats` | 평가 통계 | `eval_log` |
| `GET /api/ext/v1/training` | 학습 상태·진행률·러너 | `training.manager` |

[external-api §4](external-api.md) 의 "안 여는 것" 표는 그대로다 — 추론 시작·장치 관리는 여전히 없다. 읽기만 늘었다.

**☑ 구현 (2026-10-27)** — [external.py](../backend/app/routers/external.py) 끝의 "읽기 전용" 절. 다섯 라우트 전부 GET,
인증은 라우터 의존성이 걸고 `test_auth_covers_every_route` 가 대조한다(경로 자리표시자 치환을 데이터셋·에피소드까지
넓혔다). `diag/context` 는 `/api/system/agent-context` 와 같은 서비스·같은 인자 검사(`check_agent_context_args`)를 쓴다.
데이터셋 목록은 `features` 전체와 경로를 **빼고** 카메라 이름만 뽑아 낸다. 에피소드 요약은 `episode_meta` + 페이즈
사이드카(구간을 번호가 아니라 이름으로) + 노트 + 구운 사본 표식이고, 분석 전이면 `phases` 가 null 이다.
MCP 쪽은 리소스 7개 + 에피소드 템플릿(`piper://datasets/{+dataset_id}/episodes/{episode}` — `/` 가 든 id 를 그대로
받는다, `{+}` 를 모르는 SDK 면 `{}` 로 물러난다). 설계와 다른 점 하나: 꾸러미는 리소스(`piper://context`)에 **더해**
도구 `piper_context` 로도 낸다 — 클라이언트가 리소스보다 도구를 훨씬 잘 찾고, "왜 죽었나" 의 첫 손잡이가 바로 그것이기
때문이다. 도구는 13개가 됐다.

### 4-5. 에피소드 묶음 — 보는 자료

`GET /api/datasets/{id}/episodes/{ep}/bundle?cams=top,hand&at=phases` → zip.

| 들어가는 것 | 출처 |
|---|---|
| `keyframes/<cam>/<frame>.jpg` — `at=phases` 면 페이즈 경계마다 앞·뒤 한 장씩, `at=every:30` 이면 균등 | 프레임 캐시 라우트와 같은 디코더 |
| `timeline.json` — 페이즈 신호·라벨·task·노트·fps·카메라 | `/api/phase/.../signals`·`labels`, 노트 사이드카 |
| `state_action.csv` — 프레임별 `observation.state`·`action`(정규화 눈금, 델타 사본이면 그렇다고 머리에) | 데이터셋 parquet |
| `README.md` — 무엇이 들었나, 눈금, 키프레임이 가리키는 프레임 번호 | 생성 |

페이즈 분석이 없으면 `at=phases` 는 400 이 아니라 `every:` 로 떨어지고 README 에 그렇게 적는다 — 에이전트가
다음 질문을 하게.

디버그 런 zip(`GET /api/debug/runs/{id}/download`)에도 같은 모양의 `keyframes/` 를 넣는다 — 수집 데이터와 추론
디버그 런을 **같은 눈으로** 본다.

쓰는 모양: 스킬이 "이 에피소드 왜 실패했나" 를 받으면 묶음을 받아 풀고 키프레임을 읽는다. 로컬 7b 가 못 하고
이미지를 읽는 CLI 가 잘하는 질문이 바로 이것이다.

**☑ 구현 (2026-10-27)** — [services/episode_bundle.py](../backend/app/services/episode_bundle.py) + 내부/ext 라우트
`…/episodes/{ep}/bundle` + 디버그 런 zip 의 `?keyframes=every:N` + MCP 도구 `piper_episode_bundle`(zip 을 **에이전트 기계의
로컬 폴더**에 풀어 키프레임 경로를 돌려준다 — 경로 이탈 항목이 있으면 묶음 전체 거절) + 스킬 `.claude/skills/piper-episode`.
설계보다 더 한 것 하나: 프레임은 디코딩 캐시가 없으면 **mp4 에서 바로 뽑는다**(cv2, 에피소드 레코드의 `from_timestamp`·chunk/file,
긴 변 800 으로 줄여 묶음 크기를 묶는다) — 이 기계의 데이터셋 51개 중 캐시가 있는 것은 둘뿐이라 캐시를 요구하면 아무도 안 쓴다.
`state_action.csv` 의 열 이름은 features 의 names, 페이즈 열은 구간에서 채운다. 키프레임 상한은 카메라당 60장(고르게 덜어낸다).
남은 인수: 실제 빗나간 에피소드를 묶음만으로 짚는 것 — 묶음 쪽 재료는 실기 데이터셋에서 확인했다.

### 4-7. 설정 → 에이전트 탭 — 붙이는 안내, 채팅창이 아니다 (2026-10-27 추가)

"로컬 CLI 설정하는 탭이나 채팅하는 건 없나" 에 대한 답. 채팅은 4-6 이고 보류다. 탭은 게이트웨이가 CLI 를 돌리지
않고도 보여 줄 수 있는 셋만 보여 준다 — [AgentLinkPanel.tsx](../frontend/src/components/AgentLinkPanel.tsx),
재료는 `GET /api/system/agent-link`:

| 보여 주는 것 | 재료 | 안 하는 것 |
|---|---|---|
| 전제 — 외부 API 토큰이 켜져 있나 (꺼졌으면 주황 상자: `.env` 에 넣고 재시작) | `settings.api_token` 의 **유무만** | 토큰 값 |
| 붙었나 — 마지막으로 **통과한** 외부 호출의 시각·메서드·경로 | `external._require_token` 이 성공할 때 기록(메모리, 재시작하면 빈다 — 그것도 맞는 답) | 틀린 토큰의 시도 |
| **어떤 CLI 가 깔려 있나** — Claude Code·Codex·Gemini·Qwen·OpenCode·Cursor·Copilot·Goose·Aider, 버전·어디서 봤나 | [piper_bus/agent_clis.py](../bus/piper_bus/agent_clis.py) 의 표 하나로 **게이트웨이 기계**(PATH + `~/.local/bin`·npm·nvm·cargo·go — 유닛의 PATH 는 좁다)와 **호스트**(unitd `host_info` 의 `clis`, 컨테이너 게이트웨이가 못 보는 것을 대신 본다)를 같은 판정으로 | 설치·실행 |
| **쓸 CLI 고르기** — 라디오 하나, 저장은 `PUT /api/system/agent-link` → `config_dir/agent_cli.json`. 저장이 없으면 깔린 것 중 표의 첫째 | 선택은 **안내를 바꾸는 것**이다 — 실행 대상이 아니다 | |
| 붙이는 법 — 고른 CLI 의 설치 한 줄 · MCP 등록 명령(버전에 따라 다를 수 있다) · **설정 파일 조각**(더 오래 간다) · 꾸러미를 묻는 한 줄 · 메모(AGENTS.md 등). Aider 처럼 MCP 가 없는 CLI 는 파일로 넘기는 길 | 레시피도 같은 표. `{origin}` 은 `window.location.origin`(에이전트 기계에서 닿는 주소), `{question}` 은 화면이, `<TOKEN>` 은 사람이 | 토큰 값·CLI 실행 |
| MCP 가 주는 것 — 도구·리소스 이름, 안전 한 줄(dry_run 기본·자기 미션에만 heartbeat) | `piper_plugin.mcp` 의 이름 상수(SDK 없이 import) | |

### 4-6. 웹 채팅 — 우측 사이드바 (2026-10-27 구현, 사용자 결정)

보류 사유를 두 번 말한 뒤 사용자가 "우측 사이드바에서 채팅하게 만들자" 고 정했다. 그래서 만들되 사유를 **가드**로
녹였다 — [services/agent_chat.py](../backend/app/services/agent_chat.py) ·
[AgentChatDrawer.tsx](../frontend/src/components/AgentChatDrawer.tsx) · WS 타입 `agent_chat`.

| 보류 사유였던 것 | 가드 |
|---|---|
| 웹 버튼이 코드를 바꾼다 | **코드 수정 도구가 없다.** Claude Code 에는 `--allowedTools`(Read·Grep·Glob + 로봇 MCP)만 주고 Edit·Write·Bash 등은 `--disallowedTools` 로 이름으로도 막는다. 시스템 프롬프트가 "고칠 것은 터미널로" 라고 말한다 ([agent_chat_prompt.py](../backend/app/services/agent_chat_prompt.py)) |
| 컨테이너 게이트웨이에 CLI 가 없다 | 게이트웨이 **자기 기계**에 깔린 CLI(설정 → 에이전트에서 고른 것)만 띄운다. 컨테이너·CLI 없음·채팅 미지원(aider)은 `available=false` + 이유 — 화면이 그대로 말하고 입력을 막는다. 호스트에서 대신 돌리는 길(unitd 일시 유닛)은 **아직 없다** |
| 과금 | 턴마다 `--max-budget-usd 2`, 300초 상한, 동시에 한 턴. 턴이 끝나면 비용·시간을 보여 준다 |
| 호스트 CLI 로그인 | 게이트웨이 사용자의 로그인을 그대로 쓴다. 없으면 CLI 의 오류가 말풍선으로 온다 |
| 로봇 안전 | 모드 셋 — **관찰**(읽기 + 상태 조회)·**조종**(미션 dry_run 기본·플러그인·검출기·E-stop)·**개발**(아래). 장치 수준은 MCP 자체에 없다. heartbeat 는 piper-mcp 가 자기 미션에만(§4-3). 드로어는 모달이 아니라 레이아웃의 오른쪽 열이라 E-stop heartbeat 를 막지 않는다 |
| 웹 버튼이 코드를 돌린다 (개발 모드, 2026-10-27 사용자 요청) | **개발 모드**는 `Edit(plugins/**)`·`Write(plugins/**)` 와 `Bash(piper-plugin …:*)`·`git status/diff`·`ls` 접두사만 연다 — 저장소의 다른 곳은 못 고치고 다른 셸은 비대화 모드라 거절된다. 에이전트가 쓴 코드의 드라이런은 이 기계에서 돌지만 **게이트웨이에는 못 붙는다**: 채팅이 띄운 셸에는 `PIPER_AGENT_CHAT=1` 이 있고 `piper-plugin run` 이 그걸 보면 `--api/--token` 을 거절한다. 설치·실물은 사람이 플러그인 페이지에서. 모드를 켜면 보라색 안내가 뜬다 |

**어떻게 도나** — 한 턴 = CLI 프로세스 하나. Claude Code 는 `stream-json`(+partial)을 사건(init·delta·text·tool_use·
tool_result·result)으로 바꿔 WS 로 흘리고 `--resume <session_id>` 로 대화를 잇는다. 로봇 MCP 는 `--mcp-config`
파일(0600, 토큰은 `${PIPER_API_TOKEN}` 참조)과 `--strict-mcp-config` 로 게이트웨이가 **자기 토큰**으로 붙인다 —
명령줄에 토큰이 없다. 다른 CLI(Codex·Gemini·…)는 [agent_clis.HEADLESS](../bus/piper_bus/agent_clis.py) 의 텍스트
모드로 턴마다 새로 시작하고, 그 CLI 의 비대화 기본 샌드박스에 맡긴다(웹이 쓰기 권한을 주지 않는다).
에이전트 말풍선은 **마크다운(GFM)** 으로 그린다(`react-markdown` + `remark-gfm`, HTML 은 안 그린다 — 에이전트 출력은
신뢰하지 않는다). 사람의 글과 오류 말풍선은 그대로 둔다. 링크는 새 창이다 — 메인 탭이 이동하면 heartbeat 가 끊긴다.

### 4-6-1. (보류) 호스트에서 대신 돌리기

컨테이너 게이트웨이(.120 같은 배포 기계)에서 채팅을 쓰려면 호스트의 unitd 가 일시 유닛으로 CLI 를 돌려 주어야 한다.
플러그인 상세의 [에이전트에게 고치기](코드를 고치는 쪽)도 같은 길이다. **지금 하지 않는다.** 전제가 셋이다:

- 컨테이너 게이트웨이에는 CLI 가 없다. 호스트에서 돌려야 하고, unitd 는 **카탈로그 유닛만** 다룬다 —
  `piper-agent@<id>` 유닛 종류와 고정 명령 템플릿(예: `claude -p --output-format stream-json
  --permission-mode acceptEdits --add-dir <plugins>/<name> --allowedTools "Read,Edit,Write,Bash(piper-plugin *)"
  --max-budget-usd N`)을 카탈로그·unitd 에 더해야 한다.
- 호스트에 CLI **로그인**이 있어야 한다. `.120` 에는 없다. 과금은 운영자 계정이다.
- 코드를 고치는 기능이다. 범위는 `plugins/<name>/` 안, 설치는 사람의 버튼 — 그래도 "웹 버튼이 코드를 바꾼다" 는
  새 종류의 권한이다.

재검토 트리거: 1~5 를 쓰고 나서 "터미널 왕복이 귀찮다" 가 실제로 나올 때. 그때도 설정 `PIPER_AGENT_CLI` 가 비면
버튼이 없는 **옵트인**이다.

## 5. 안 하는 것

| 안 하는 것 | 이유 |
|---|---|
| `judge()` 프로바이더로 CLI | 회차 경계 예산이 수백 ms(qwen 7b 웜 326ms)인데 CLI 기동이 수 초. Anthropic 프로바이더가 이미 있다 |
| 런타임 플래너 부활 | [plugins §15](plugins.md) 결정 3. 시나리오 5장의 장기 계획이 돌아온다면 **에이전트가 MCP 로 미션을 하나씩** 부르는 형태다 — 스텝 스펙 생성이 아니다 |
| MCP 로 장치 수준 | 추론 시작·조그·캘리브레이션·녹화는 사람 판단이 끼는 자리 — [external-api §1](external-api.md) |
| 상시 heartbeat 펌프 | 다른 운영자의 죽음을 가린다 (§3 원칙 2) |
| 꾸러미에 `.env`·토큰 | 붙여넣기는 곧 유출이다. 구조로 막고 테스트로 잡는다 |
| 게이트웨이가 CLI 를 부른다 | 컨테이너에 없다. 호스트 유닛 경로는 4-6 보류 사유 그대로 |
| HTTP MCP 를 게이트웨이에 마운트 | 클라이언트 사망을 못 본다 + 의존성. 파이썬을 못 까는 클라이언트가 생기면 **덧붙인다** |
| ext v1 에 WS/SSE | 폴링으로 충분하다 — MCP 도구가 `mission_get` 을 부른다. SSE 는 external-api 의 기존 후보로 남긴다 |

## 6. 단계

| 단계 | 내용 | 인수 기준 |
|---|---|---|
| 1 ☑ | 꾸러미 — `services/agent_context.py` + `GET /api/system/agent-context` + 버튼 둘 + 비밀 테스트 | 카메라를 일부러 뽑아 추론을 죽인 뒤, **꾸러미만 붙여넣은** 에이전트가 사망 이유를 저널 줄로 짚는다 |
| 2 ☑ | 스킬·훅·`check --json`·`AGENTS.md` + gitignore 조정 | 새 세션의 에이전트가 "`recycling` 을 사과 분류기로" 지시만으로 `new → check → dry-run` 을 사람 개입 없이 완주한다 |
| 3 ☑ | `piper-mcp`(stdio) + 도구 + heartbeat 규칙 + Mock 테스트 + `.mcp.json` 예시 | Claude Code 에서 `status → mission(dry_run) → cancel` 왕복. CLI 를 kill 하면 estopd 저널에 2.5초 안에 `heartbeat_timeout` |
| 4 ☑ | ext v1 읽기 전용 다섯 + MCP 리소스 | 인증 테스트 통과, MCP 에서 리소스가 보인다 |
| 5 ☑ | 에피소드 묶음 + 디버그 런 키프레임 | 에이전트가 묶음만 보고 파지가 빗나간 프레임 번호를 가리킨다 |
| — | 4-6 | 트리거가 올 때 |

1·2 가 먼저인 이유는 둘 다 **지금 손으로 하는 일**을 없애는 것이고 로봇 안전 표면을 안 건드리기 때문이다.
3 은 heartbeat 소유를 바꾸는 일이라 실기 인수가 필요하다.

## 7. 손대는 곳

| 곳 | 무엇 | 단계 |
|---|---|---|
| `backend/app/services/agent_context.py` (신규) | 상태 API 결과를 markdown/json 으로 모은다 | 1 |
| `backend/app/routers/system.py` | `GET /agent-context` | 1 |
| `frontend/src/components/SystemMessages.tsx`·`SystemLogPanel.tsx` | [에이전트용 컨텍스트 복사] | 1 |
| `backend/tests/test_agent_context.py` (신규) | 비밀 문자열 부재·절 순서 | 1 |
| `.claude/skills/piper-plugin/SKILL.md`·`.claude/settings.json`(hooks)·`tools/hooks/plugin-check.sh`·`.gitignore`·`AGENTS.md` | 스킬·훅 | 2 |
| `plugin/piper_plugin/__main__.py` | `check --json` | 2 |
| `plugin/piper_plugin/mcp.py` (신규)·`plugin/pyproject.toml`(`[mcp]` extra, `piper-mcp`)·`.mcp.json` | MCP 서버 | 3 |
| `backend/app/routers/external.py` | 읽기 전용 다섯 | 4 |
| `backend/app/routers/datasets.py`·`debug_logs.py` | `bundle`, 디버그 런 `keyframes/` | 5 |
| `feature/external-api.md` §2·§4 | 표면 표에 읽기 전용 추가, "MCP 서버" 행을 이 문서로 | 3·4 |
| `plugins/README.md` | 검증 순서에 `--json`·스킬 한 줄 | 2 |

## 8. 남은 결정

| # | 질문 | 권고 |
|---|---|---|
| 1 | `.claude/` gitignore — 스킬·훅을 저장소가 드나 | ☑ **든다.** `.claude/settings.local.json` 만 무시하고, 지금 `settings.json` 의 개인 경로 허용 목록은 local 로 옮긴다 |
| 2 | 꾸러미 기본 형식 | **markdown.** 붙여넣는 상대가 사람과 LLM 둘이다. `format=json` 을 같이 둔다 |
| 3 | MCP 서버 패키지 | **`piper_plugin[mcp]`** + 콘솔 `piper-mcp`. 별도 패키지는 설치가 한 번 늘 뿐 얻는 게 없다 |
| 4 | `dry_run` 기본 true 를 MCP 에서 강제 | **한다.** 실물은 `dry_run=false` 를 에이전트가 쓰게 — 저널에 의도가 남는다 |
| 5 | 스킬을 사용자에게 배포 | 2단계를 저장소 안에서 쓴 뒤. 패키지에 실어 `piper-plugin skill install` 이 후보 |
| 6 | 읽기 전용 라우트 범위 | 다섯으로 시작. 늘릴 땐 "에이전트가 실제로 물은 것" 만 |
