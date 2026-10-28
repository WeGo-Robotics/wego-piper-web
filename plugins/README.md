# 플러그인 — 폴더 하나로 로봇에 얹는 파이썬

사람도 코딩 에이전트(Claude Code 등)도 이 파일을 읽고 플러그인을 쓴다. 설계 전체는
[feature/plugins.md](../feature/plugins.md). 여기는 **계약 요약 · 하지 말 것 · 검증 순서**다.

## 계약 — 한 장

```
plugins/<name>/
  plugin.yaml      이름·설정 항목·카메라·팔 슬롯·권한·자원  (모양: piper_plugin/manifest.py)
  main.py          entry. SDK 를 써도, 환경변수만 읽어도 된다 (plugins/hello 가 후자)
  requirements.txt (선택) python: venv 일 때 깐다
```

게이트웨이가 프로세스를 띄우며 **환경변수**를 준다: `PIPER_PLUGIN_NAME` · `PIPER_PLUGIN_DIR` ·
`PIPER_PLUGIN_RUNTIME_DIR` · `PIPER_PLUGIN_CONFIG`(JSON) · `PIPER_PLUGIN_CAMERAS`(JSON) · `PIPER_PLUGIN_ARMS`(JSON) ·
`PIPER_API_BASE` · `PIPER_API_TOKEN`(이번 실행의 범위 토큰) · `PIPER_REDIS_URL`.

| 하고 싶은 것 | SDK | 뒤에서 |
|---|---|---|
| 카메라 프레임 | `self.camera("slot").read_new()` → `(frame BGR, seq, ns)` | `/dev/shm` 세그먼트 구독. 카메라를 열지 않는다 |
| 팔 상태 (읽기 전용) | `self.arm("slot").read()` → `.joints` `.gripper` `.age_s` / `.read_new()` | `/dev/shm` 상태 세그먼트 구독. 값은 정규화(관절 -100..100, 그리퍼 0..100) — `observation.state` 와 같은 눈금. 매니페스트 `arms: [{key, label}]` |
| 말단 자세 | `self.arm("slot").end_pose()` → `x y z`(mm) `rx ry rz`(도) `inside` | REST, 권한 `robot.read`. 회차 경계·저속에만 — 제어 루프 속도로 부르지 않는다 |
| 검출 모델 | `self.detector(model)` → `.predict(frame, conf=…)[0]` | RT-DETR(Apache). ultralytics(.pt) 는 **거절** |
| 검출 결과 올리기 | `self.publish_detections(name, shape, result)` | 버스 `piper:vision:<name>` — 화면·다른 플러그인이 읽는다 |
| 그림 올리기 | `self.publish_preview(name, frame)` | `/api/vision/preview/<name>` |
| 상태 보이기 | `self.state(phase="…", fps=…)` | 버스 KV, 화면 카드 |
| 다른 검출기 읽기 | `self.detections(name, fresh_s=1.0)` | `.objects` `.count(label)` `.text` |
| 다른 플러그인에 넘기기 | `self.publish("grades", data)` / 받기 `self.latest("슬롯")` → `.data` `.seq` `.publisher` | 버스 토픽. 매니페스트 `outputs.topics`·`inputs` 가 배선을 선언한다 (§7-3) |
| 입력 슬롯 풀기 | `self.input("슬롯")` / `self.input_detections("슬롯")` | 시작할 때 고른 검출 이름·토픽 — 코드는 소스 이름을 모른다 |
| 추론 시작·정지 | `self.robot.start_inference(ckpt, self.camera_mapping())` / `.stop_inference()` | REST, 권한 `inference.start`/`inference.stop` |
| 지시문(task) | `self.robot.set_task("…")` | 권한 `params.write`. 언어 정책만 읽는다 |
| 일시정지·재개·리셋 | `self.robot.pause()` `.resume()` `.reset()` | 권한 `params.write` |
| LLM 판단 | `self.judge(user_text, system=rules)` → 슬롯 dict | 권한 `vision.judge` |
| 평가 기록 | `self.robot.eval_log(success=…)` | 권한 `eval.write` |
| 긍급 정지·박동 | `self.robot.estop()` / `resources.heartbeat: true` | 열린 경로, 권한 불필요 |
| 기다리기 | `self.sleep(s)` | 정지 신호에 바로 깬다 — `time.sleep` 대신 |
| 끝내기 | `self.stop("사유")` | `teardown()` 이 불린다 |

`multi_instance: true` 면 같은 코드를 인스턴스(`<name>:<label>`)로 여럿 띄운다 — `self.name` 이 인스턴스 id 라 버스 키가 자동으로 갈린다.

세 층: `Plugin.loop()`(뭐든) · `MissionPlugin`(관찰→판단→실행→완료→기록→리셋, 훅만 채운다) ·
`self.robot.*`(원시 연산). 미션의 공통 설정 `max_episodes`·`episode_timeout_s`·`settle_s`·`dry_run` 은
자동으로 붙는다.

권한 범위 이름의 정본은 `GET /api/plugins/scopes` (코드: `backend/app/core/plugin_scopes.py`).
범위 밖 호출은 **403** 이고 본문 `detail` 이 무엇을 더해야 하는지 말한다. `"*"` 도 `/api/auth/*` 는 못 간다.

## 하지 말 것

- **장치를 직접 열지 않는다.** `/dev/video*`·CAN·RealSense 는 호스트 데몬이 독점한다. 카메라는 세그먼트로만.
- **팔을 직접 움직이지 않는다.** shm 명령 세그먼트·CAN 에 쓰지 않는다. 추론(REST)을 통해서만. 상태는 `self.arm()` 으로 읽기만 한다.
- **`/api/auth/*` 를 부르지 않는다.** 토큰이 `*` 라도 403 이다.
- **블로킹 대기를 쓰지 않는다.** `time.sleep` 대신 `self.sleep` — SIGTERM 에 바로 멈춰야 E-stop 뒤처리가 깨끗하다.
- **특정 검출 모델의 브랜드 이름을 쓰지 않는다** (파일·식별자·문구 전부 — `backend/tests/test_no_model_branding.py` 가 트리 전체를 막는다). 대체어는 `detector`.
- **ultralytics 를 번들 플러그인에 넣지 않는다** (AGPL). 사용자 플러그인은 사용자 몫이다.
- 화면 문구(`title`·`label`·`description`·로그)는 **한국어 원문**으로 쓴다 — 화면이 번역한다.

## 검증 순서 — 전부 터미널

```bash
piper-plugin new  plugins/my-thing --template loop|mission|detector|dashboard   # 틀 (dashboard = 정적 자기 화면 + E-stop 버튼)
piper-plugin check plugins/my-thing [--api http://127.0.0.1:8000] [--json]  # 매니페스트·entry·권한 범위. --json 은 기계가 읽는 결과(ok·errors[])
piper-plugin run  plugins/my-thing --camera watch=<스트림> [--arm follower=can0] \
    --api http://127.0.0.1:8000 [--config '{"conf":0.4}'] [--dry-run] # 게이트웨이가 켤 때와 같은 진입점
curl -X POST localhost:8000/api/plugins/install -H 'content-type: application/json' \
    -d '{"path":"/abs/plugins/my-thing","replace":true}'               # 설치 · 고친 뒤 다시 깔기(업데이트)
curl -X POST localhost:8000/api/plugins/my-thing/start -H 'content-type: application/json' \
    -d '{"config":{},"cameras":{"watch":"<스트림>"},"arms":{"follower":"can0"}}'   # 게이트웨이에서
```

- 살아 있는 스트림 이름: `GET /api/vision/segments`. 검출 결과 확인: `GET /api/vision/detections`.
  상태를 내는 팔: `GET /api/plugins/topics` 의 `arms`.
- **업데이트**는 `replace: true`(zip 은 `?replace=true`) — 폴더만 바뀌고 저장 설정·실행 기록·인스턴스·venv 는 남는다.
  `requirements.txt` 나 `python:` 이 바뀌었으면 [의존 설치] 를 다시 한다(응답 `updated.deps_reset`). 도는 중이면 409.
- `run` 에 `--api` 가 없으면 `self.api`/`self.robot` 호출은 `ApiUnavailable` — 카메라·버스·모델만 시험하는 모드다.
- 미션은 `--dry-run` 으로 로봇 없이 루프만 돌려 저널(`.runtime/runs/*.jsonl`)을 본다.
- 테스트는 `backend/tests/test_plugin_sdk.py`·`test_plugins.py` 가 기준이다 — 예제 매니페스트는 전부 거기서 읽힌다.
- **웹 채팅의 개발 모드**(🤖 → 개발)도 같은 순서를 돈다 — 단 `run` 은 `--api` 없이 드라이런만 되고(가드), 설치는 사람이 플러그인 페이지에서 한다.
- **코딩 에이전트는 스킬로 간다** — Claude Code 에서 "플러그인 만들어/고쳐" 라고 하면 `.claude/skills/piper-plugin` 이 이 순서를 안다.
  `plugins/` 아래 파일을 저장하면 훅(`tools/hooks/plugin-check.sh`)이 `check --json` 을 자동으로 돌려 깨진 매니페스트를
  바로 되돌려 준다 ([feature/agent-cli.md](../feature/agent-cli.md) §4-2).

## 번들 예제

| 폴더 | 층 | 보여 주는 것 |
|---|---|---|
| `hello/` | 없음(SDK 미사용) | 환경변수·토큰·상태 KV 의 최소 배선 |
| `detect-and-start/` | 1층 | 검출기 안에 두고, 보이면 추론 시작·사라지면 정지 |
| `zone-guard/` | 1층 | ROI 침입 → 일시정지/E-stop, 비면 재개 |
| `recycling/` | 2층 | 검출 → LLM 판단 → task → 저널. `MissionPlugin` 의 참고 구현. 외부 API 미션 타입 `recycling` |
| `detector/` | 1층 | 카메라 여러 대를 한 모델로 훑는 검출기. 검출 페이지의 뒷단. 둘 이상은 인스턴스(`detector:cam2`) |
| `ui-demo/` | 1층 | `ui: server` — 자기 FastAPI 서버(HTTP·WS)를 소켓에 띄워 게이트웨이 뒤로 프록시. 포트를 고르지 않는다 |
| `size-grader/` | 1층 | `detector` 의 검출을 **입력 슬롯**으로 받아 크기 등급을 토픽 `grades` 로 — 플러그인 배선의 실례 |
