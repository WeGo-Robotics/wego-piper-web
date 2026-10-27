"""웹 채팅 (feature/agent-cli.md §4-6) — 게이트웨이가 CLI 를 한 턴씩 띄운다. 가드가 요점이다:
코드 수정 도구 없음, 토큰은 명령줄에 안 보임, 턴은 하나씩, 못 쓰는 환경은 이유와 함께 503."""

import asyncio
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import agent_chat as AC
from app.services import agent_chat_prompt as P
from piper_bus import agent_clis as A


def _fake_claude(tmp_path, lines: list[dict], rc: int = 0, sleep: float = 0.0) -> str:
    """stream-json 을 흉내 내는 실행 파일. 인자를 파일로 남겨 무엇이 넘어왔는지 본다."""
    script = tmp_path / "claude"
    body = "\n".join(json.dumps(l, ensure_ascii=False) for l in lines)
    script.write_text(
        "#!/usr/bin/env python3\nimport sys, time, json\n"
        f"open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"open({str(tmp_path / 'env.json')!r}, 'w').write(json.dumps(dict(__import__('os').environ)))\n"
        f"time.sleep({sleep})\n"
        f"sys.stdout.write({body!r} + '\\n')\nsys.stdout.flush()\nsys.exit({rc})\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


STREAM = [
    {"type": "system", "subtype": "init", "session_id": "sess-1", "mcp_servers": [{"name": "piper", "status": "connected"}]},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "추론이 "}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "죽은 이유는"}}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__piper__piper_context", "input": {"since": "10 minutes ago"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "# PIPER 에이전트용 컨텍스트 …", "is_error": False}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "추론이 죽은 이유는 카메라 top 세그먼트가 사라져서입니다."}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "…", "total_cost_usd": 0.0123, "duration_ms": 4321, "session_id": "sess-1"},
]


def test_argv_restricts_tools_by_mode_and_keeps_the_token_out(tmp_path):
    argv = AC.build_argv("claude", "/x/claude", "왜 죽었나", mode="observe", mcp_config=tmp_path / "mcp.json")
    joined = " ".join(argv)
    assert argv[:3] == ["/x/claude", "-p", "왜 죽었나"] and "--output-format" in argv and "stream-json" in argv
    assert "--allowedTools" in argv and "mcp__piper__piper_status" in argv and "mcp__piper__piper_mission_start" not in argv
    assert "--disallowedTools" in argv and "Edit" in argv and "Bash" in argv
    assert "--mcp-config" in argv and "--strict-mcp-config" in argv and "--max-budget-usd" in argv and "--resume" not in argv
    assert "PIPER_API_TOKEN" not in joined.replace("${PIPER_API_TOKEN}", "")
    ctl = AC.build_argv("claude", "/x/claude", "미션 시작", mode="control", session_id="sess-9")
    assert "mcp__piper__piper_mission_start" in ctl and "mcp__piper__piper_estop" in ctl and ctl[-2:] == ["--resume", "sess-9"]
    assert set(P.DISALLOWED_TOOLS).isdisjoint(P.CONTROL_TOOLS)          # 관찰·조종에는 코드 수정 도구가 없다
    # 개발 모드 — plugins/ 아래만, piper-plugin 과 git 읽기만. 거부 목록에서 Edit·Write·Bash 를 빼야 경로 허용이 산다
    dev = AC.build_argv("claude", "/x/claude", "플러그인 만들어", mode="develop")
    assert "Edit(plugins/**)" in dev and "Write(plugins/**)" in dev and "Bash(piper-plugin run:*)" in dev
    assert "Edit" not in dev[dev.index("--disallowedTools"):dev.index("--append-system-prompt")]
    assert "Bash" not in dev[dev.index("--disallowedTools"):dev.index("--append-system-prompt")]
    assert "plugins/ 아래만" in dev[dev.index("--append-system-prompt") + 1]
    assert not any(a == "Edit" or a == "Bash" or a == "Write" for a in dev[dev.index("--allowedTools"):dev.index("--disallowedTools")])
    assert AC.build_argv("gemini", "/x/gemini", "안녕") == ["/x/gemini", "-p", "안녕"]
    with pytest.raises(ValueError):
        AC.build_argv("aider", "/x/aider", "안녕")


def test_mcp_config_file_references_the_token_by_env_only(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "SECRET-TOKEN-123")
    p = AC.mcp_config_path()
    text = p.read_text()
    assert "SECRET-TOKEN-123" not in text and "${PIPER_API_TOKEN}" in text and "piper_plugin.mcp" in text
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert AC.child_env()["PIPER_API_TOKEN"] == "SECRET-TOKEN-123" and "CLAUDECODE" not in AC.child_env()
    assert AC.child_env()["PIPER_AGENT_CHAT"] == "1"          # 개발 모드의 piper-plugin run 가드가 이걸 본다
    import sys
    from pathlib import Path as _P
    assert AC.child_env()["PATH"].split(":")[0] == str(_P(sys.executable).resolve().parent)   # piper-plugin 이 보여야 한다


@pytest.fixture
def ready(monkeypatch, tmp_path):
    """깔린 Claude Code 가 있는 소스 게이트웨이인 척 — 실제 실행 파일은 가짜다."""
    def avail(self):
        return {"cli": "claude", "cli_name": "Claude Code", "version": "fake", "multi_turn": True,
                "format": "claude-stream-json", "ext_enabled": True, "available": True, "reason": None,
                "path": str(tmp_path / "claude")}
    monkeypatch.setattr(AC.AgentChat, "availability", avail)
    monkeypatch.setattr(settings, "api_token", "tok-123")
    events: list[dict] = []

    async def fake_broadcast(message):
        events.append(message)

    from app.routers import ws
    monkeypatch.setattr(ws, "broadcast", fake_broadcast)
    chat = AC.AgentChat()
    chat.reset()
    return chat, events, tmp_path


def test_a_turn_streams_events_and_remembers_the_session(ready):
    chat, events, tmp_path = ready
    _fake_claude(tmp_path, STREAM)

    async def go():
        turn = await chat.start_turn("추론이 왜 죽었나")
        await chat.task
        return turn

    turn = asyncio.run(go())
    kinds = [e["data"]["kind"] for e in events if e["type"] == "agent_chat"]
    assert kinds[0] == "status" and kinds[-1] == "status" and "init" in kinds and "delta" in kinds
    assert "tool_use" in kinds and "tool_result" in kinds and "text" in kinds and "result" in kinds
    assert all(e["data"]["turn"] == turn for e in events)
    roles = [m["role"] for m in chat.messages]
    assert roles == ["user", "tool", "tool_result", "assistant"]
    assert chat.session_id == "sess-1" and not chat.running
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "--allowedTools" in argv and "tok-123" not in " ".join(argv)
    env = json.loads((tmp_path / "env.json").read_text())
    assert env["PIPER_API_TOKEN"] == "tok-123"
    # 다음 턴은 세션을 잇는다
    asyncio.run(go())
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[-2:] == ["--resume", "sess-1"]


def test_failure_and_timeout_become_error_bubbles_and_turns_are_one_at_a_time(ready, monkeypatch):
    chat, events, tmp_path = ready
    _fake_claude(tmp_path, [], rc=3)

    async def go(text):
        await chat.start_turn(text)
        await chat.task

    asyncio.run(go("hi"))
    assert chat.messages[-1]["role"] == "assistant" and chat.messages[-1]["is_error"] and "실패" in chat.messages[-1]["text"]
    # 시간 상한 — 느린 CLI 는 죽인다
    _fake_claude(tmp_path, STREAM, sleep=5.0)
    monkeypatch.setattr(AC, "TURN_TIMEOUT_S", 0.5)

    async def go_slow():
        await chat.start_turn("slow")
        with pytest.raises(RuntimeError):
            await chat.start_turn("again")        # 동시에 둘은 없다
        await chat.task

    asyncio.run(go_slow())
    assert "시간 상한" in chat.messages[-1]["text"] and not chat.running


def test_routes_report_availability_mode_and_reset(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(AC.AgentChat, "availability", lambda self: {
        "cli": "aider", "cli_name": "Aider", "version": None, "multi_turn": False, "format": None, "ext_enabled": False,
        "available": False, "reason": "Aider 은(는) 웹 채팅을 지원하지 않습니다", "path": None})
    st = client.get("/api/agent/chat").json()
    assert st["available"] is False and "Aider" in st["reason"] and st["mode"] in ("observe", "control")
    assert client.post("/api/agent/chat", json={"text": "hi"}).status_code == 503
    assert client.post("/api/agent/chat", json={"text": "  "}).status_code == 400
    assert client.put("/api/agent/chat/mode", json={"mode": "control"}).json() == {"mode": "control"}
    assert client.put("/api/agent/chat/mode", json={"mode": "develop"}).json() == {"mode": "develop"}
    assert client.put("/api/agent/chat/mode", json={"mode": "god-mode"}).status_code == 400
    assert client.post("/api/agent/chat/reset").json() == {"status": "reset"}
    assert client.get("/api/agent/chat").json()["session_id"] is None
    client.put("/api/agent/chat/mode", json={"mode": "observe"})
