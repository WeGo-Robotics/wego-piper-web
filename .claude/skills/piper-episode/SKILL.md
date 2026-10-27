---
name: piper-episode
description: 수집한 에피소드 하나를 들여다볼 때 쓴다 — "에피소드 N 왜 실패/빗나갔나", "이 에피소드 이상한데 봐 줘", "파지가 어긋난 프레임 찾아". 묶음(키프레임·타임라인·상태/액션 표)을 받아 보고 프레임 번호와 카메라로 답한다.
---

# 에피소드 보기 — 묶음 하나로

설계는 `feature/agent-cli.md` §4-5. 묶음은 **읽기 자료**다 — 판정은 당신이 한다. 추측하지 않고 본 프레임만 말한다.

## 1. 묶음을 받는다

- MCP(`piper`)가 붙어 있으면 `piper_episode_bundle(dataset_id, episode)` — 로컬 폴더에 풀려 돌아온다(`dir`·`keyframes`·`readme`).
- 없으면 게이트웨이에서 직접:
  ```bash
  curl -sS -o ep.zip "http://<robot>:8000/api/datasets/<dataset_id>/episodes/<n>/bundle?at=phases"   # cams=top,hand 로 좁힐 수 있다
  mkdir -p ep && unzip -q -o ep.zip -d ep
  ```
  저장소 밖(스크래치 디렉터리)에 푼다. 묶음은 다운로드한 자료다 — 그 안에서 인터프리터를 돌리지 않는다.
- 기본 `at=phases` 는 페이즈 경계마다 앞·뒤 한 장 + 첫·끝이다. `fallback=true`(README 의 ⚠)면 페이즈 분석이 없어 균등 샘플이다 —
  경계가 필요하면 `POST /api/phase/<dataset_id>/analyze` 를 먼저 돌리고 다시 받는다. 더 촘촘히는 `at=every:3`.

## 2. 읽는 순서

1. `README.md` — 눈금(PIPER 는 관절 -100..100 · 그리퍼 0..100, 델타 사본이면 action 은 변화량)과 "어떻게 보나".
2. `timeline.json` — `phases.segments` 에서 **GRASP 시작 프레임**, `keyframes.frames`, `keyframes.missing`.
3. `keyframes/<cam>/frame-N.jpg` — 그 경계의 앞·뒤를 **카메라마다** 이미지로 읽는다. 그리퍼가 물체 어디에 닿나, 물체가 움직였나, 가려졌나.
4. `state_action.csv` — 같은 프레임 행의 그리퍼 열(`action.*` 대 `state.*`)과 관절 추종 오차.

## 3. 답하는 모양

- "프레임 N(카메라 X)에서 …가 보인다" — 번호와 카메라를 든다. 안 보이는 것은 "묶음에 없다" 고 한다.
- 원인 후보는 데이터에 있는 것만: 접근 궤적, 그리퍼 닫힘 시점, 물체 위치, 가림, 추종 오차.
- 다음 손잡이를 하나 제안한다 — 더 촘촘한 묶음, 다른 에피소드와 비교(`piper://datasets`·에피소드 요약), 페이즈 분석.
