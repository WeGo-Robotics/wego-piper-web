"""웹 채팅 에이전트의 시스템 프롬프트와 도구 허용 목록 (feature/agent-cli.md §4-6).

에이전트가 읽는 글이라 번역하지 않는다 (`tools/i18n.py` 의 `PY_SKIP_FILES`).
도구 목록은 **여기 한 곳**이다 — 관찰 모드는 읽기와 로봇 상태 조회, 조종 모드는 거기에 미션·플러그인·
검출기·E-stop 을 더한다. 코드를 고치는 도구(Edit·Write·Bash …)는 어느 모드에도 없다 — 웹 버튼이
저장소를 바꾸는 권한은 열지 않는다(§4-6 보류 사유 그대로). 그건 터미널의 일이다.
"""

MCP = "mcp__piper__"

#: 관찰 — 저장소 읽기 + 로봇 상태 조회. 묶음 도구는 로컬 임시 폴더에 쓰지만 로봇을 건드리지 않는다
OBSERVE_TOOLS = (
    "Read", "Grep", "Glob",
    MCP + "piper_status", MCP + "piper_context", MCP + "piper_missions", MCP + "piper_mission_get",
    MCP + "piper_plugins", MCP + "piper_detections", MCP + "piper_episode_bundle",
)
#: 조종 — 미션(dry_run 기본)·플러그인·검출기·E-stop 까지. 장치 수준은 MCP 자체에 없다
CONTROL_TOOLS = OBSERVE_TOOLS + (
    MCP + "piper_mission_start", MCP + "piper_mission_cancel",
    MCP + "piper_plugin_start", MCP + "piper_plugin_stop",
    MCP + "piper_vision_start", MCP + "piper_vision_stop",
    MCP + "piper_estop",
)
#: 개발 — 관찰 + **plugins/ 아래만** 고치기 + piper-plugin 루프(new → check → run --dry-run) + git 읽기.
#: 경로·명령 접두사는 Claude Code 의 권한 규칙 문법이다. 셸은 이 접두사 밖이면 비대화 모드에서 거절된다.
#: `piper-plugin run` 은 CLI 쪽 하드 가드(PIPER_AGENT_CHAT=1 이면 --api/--token 거절)로 게이트웨이에 못 붙는다.
DEVELOP_TOOLS = OBSERVE_TOOLS + (
    "Edit(plugins/**)", "Write(plugins/**)", "Skill",
    "Bash(piper-plugin new:*)", "Bash(piper-plugin check:*)", "Bash(piper-plugin run:*)",
    "Bash(python -m piper_plugin new:*)", "Bash(python -m piper_plugin check:*)", "Bash(python -m piper_plugin run:*)",
    "Bash(python3 -m piper_plugin new:*)", "Bash(python3 -m piper_plugin check:*)", "Bash(python3 -m piper_plugin run:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(ls:*)",
)
#: 관찰·조종에는 없다 — 이중 안전장치(allowedTools 밖은 어차피 거절되지만, 이름으로도 막는다)
DISALLOWED_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task", "WebFetch", "WebSearch")
#: 개발 모드에서도 없다 — 거부 규칙이 허용보다 세므로 Edit·Write·Bash 는 여기서 빼고 경로·접두사 허용으로만 연다
DISALLOWED_TOOLS_DEVELOP = ("MultiEdit", "NotebookEdit", "Task", "WebFetch", "WebSearch")

SYSTEM_PROMPT = """당신은 PIPER 로봇 게이트웨이 웹 화면의 우측 채팅에서 돈다. 상대는 이 로봇을 운용하는 사람이다.

규칙:
- 이 채팅은 코드를 고치지 않는다. 파일 수정·셸 도구는 없다. 고쳐야 할 것이 보이면 어디를 어떻게 고칠지 말하고, 터미널의 Claude Code 로 하라고 안내한다.
- 로봇은 미션 수준으로만 다룬다(mcp__piper__* 도구). 장치 준비(정책 배포·카메라·캘리브레이션)는 사람 몫이고, 전제가 안 되면 409 가 온다 — 장치를 손대려 하지 말고 사람에게 말한다.
- "왜 죽었나 / 왜 안 되나" 는 먼저 piper_context(진단 꾸러미)를 읽는다. 에피소드 질문은 piper_episode_bundle 로 묶음을 받아 키프레임을 본다.
- 미션 시작은 dry_run 이 기본이다. 실물로 팔을 움직이는 것은 사람이 이 채팅에서 명시적으로 허락한 뒤에만, 그리고 조종 모드일 때만 한다. 위험하면 piper_estop.
- 짧게, 한국어로, 본 것만 말한다. 추측은 추측이라고 한다.
"""

DEVELOP_PROMPT = """
개발 모드다. 이 채팅에서는 **plugins/ 아래만** 고칠 수 있고, 셸은 piper-plugin 과 git diff/status 뿐이다.
- 먼저 plugins/README.md 를 읽는다(계약·하지 말 것·검증 순서). 참고 예제는 plugins/detect-and-start, plugins/recycling.
- 순서: piper-plugin new plugins/<name> --template loop|mission|detector → 코드 → piper-plugin check --json plugins/<name>
  → piper-plugin run plugins/<name> --dry-run (게이트웨이 없이 — --api 는 여기서 거절된다).
- 설치와 실물 실행은 사람이 플러그인 페이지에서 한다. 끝나면 폴더 경로와 바꾼 것, 드라이런 결과를 세 줄로 보고한다.
- 번들 예제를 고칠 때는 사본으로 새 플러그인을 만든다 — 예제는 참고 구현이다.
"""

#: 모드 이름 → 도구 목록
TOOLS_BY_MODE = {"observe": OBSERVE_TOOLS, "control": CONTROL_TOOLS, "develop": DEVELOP_TOOLS}
DISALLOWED_BY_MODE = {"observe": DISALLOWED_TOOLS, "control": DISALLOWED_TOOLS, "develop": DISALLOWED_TOOLS_DEVELOP}
