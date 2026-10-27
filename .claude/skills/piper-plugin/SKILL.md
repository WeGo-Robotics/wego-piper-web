---
name: piper-plugin
description: PIPER 플러그인을 만들거나 고칠 때 쓴다 — "플러그인 만들어", "recycling 을 사과 분류기로 바꿔", "플러그인이 안 돌아/안 깔려" 같은 요청. new → check --json → run --dry-run → install(replace) 순서, 하지 말 것, 끝내는 기준.
---

# PIPER 플러그인 개발 루프

정본은 `plugins/README.md` 다 — 계약 한 장·하지 말 것·검증 순서. 이 스킬은 **순서와 끝내는 기준**만 든다.
설계 전체는 `feature/plugins.md`, 이 스킬이 있는 이유는 `feature/agent-cli.md` §4-2.

## 먼저 읽는다

1. `plugins/README.md` 전체 (짧다)
2. 참고 예제 — 루프면 `plugins/detect-and-start/`, 미션(관찰→판단→실행→기록)이면 `plugins/recycling/`,
   검출기면 `plugins/detector/`, 플러그인 사이 배선이면 `plugins/size-grader/`

## 순서 — 전부 터미널, 사람 개입 없이 돈다

```bash
piper-plugin new  plugins/<name> --template loop|mission|detector|dashboard   # 틀. 고치는 거면 건너뛴다
#   … main.py · plugin.yaml 을 고친다 …
piper-plugin check --json plugins/<name> [--api http://127.0.0.1:8000]       # "ok": false 면 errors[].where/msg 를 고친다
piper-plugin run plugins/<name> --dry-run --camera <slot>=<stream> [--api http://127.0.0.1:8000]
#   미션이면 저널 plugins/<name>/.runtime/runs/*.jsonl 에 회차가 1개 이상 남아야 한다
curl -X POST localhost:8000/api/plugins/install -H 'content-type: application/json' \
     -d '{"path":"/abs/plugins/<name>","replace":true}'                      # 설치·업데이트 (도는 중이면 409 → 먼저 stop)
```

- 살아 있는 스트림 이름: `GET /api/vision/segments`. 권한 범위 이름: `GET /api/plugins/scopes`.
- `plugins/` 아래 파일을 저장하면 훅(`tools/hooks/plugin-check.sh`)이 `check --json` 을 자동으로 돌려
  깨진 매니페스트를 바로 알려 준다 — 그 글이 보이면 다음 저장 전에 고친다.
- 게이트웨이 없이도 `run` 은 돈다 (`--api` 를 빼면 `self.api` 호출만 `ApiUnavailable`).

## 하지 말 것 (README 의 목록 — 요약)

- 장치를 직접 열지 않는다(`/dev/video*`·CAN·RealSense). 카메라는 `self.camera()` 세그먼트로만
- 팔을 직접 움직이지 않는다. 추론(REST)으로만. 상태는 `self.arm()` 읽기만
- `/api/auth/*` 를 부르지 않는다. `time.sleep` 대신 `self.sleep`
- 특정 검출 모델의 브랜드 이름을 쓰지 않는다 — 대체어는 `detector`. ultralytics 를 번들에 넣지 않는다
- 화면 문구(`title`·`label`·`description`·로그)는 한국어 원문으로 — 화면이 번역한다

## 끝내는 기준

- `check --json` 이 `"ok": true`
- 드라이런이 끝까지 돌고(미션은 저널에 회차 1개 이상) 예외 없이 `teardown` 까지 간다
- 사람에게는 세 줄로 보고한다 — 무엇을 바꿨나, 어떤 설정으로 드라이런했나, 실물로 켤 때 고를 카메라·팔 슬롯
