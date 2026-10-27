"""웹 채팅 — 우측 사이드바에서 로컬 AI CLI 와 한 턴씩 말한다 (feature/agent-cli.md §4-6).

## 무엇을 하고 무엇을 안 하나

게이트웨이가 **자기 기계에 깔린** CLI(설정 → 에이전트에서 고른 것)를 한 턴에 한 번 띄우고, 출력을 WS 로
흘린다. 보류했던 사유는 가드로 녹였다:

- 코드를 고치는 도구가 없다 — Claude Code 에는 allowedTools(읽기 + 로봇 MCP)만 주고 Edit·Write·Bash 는
  이름으로도 막는다. 다른 CLI 는 텍스트 한 덩이로 받으며 그 CLI 의 기본 샌드박스에 맡긴다(코드 수정 권한은
  여전히 웹이 주지 않는다 — cwd 는 저장소지만 쓰기는 각 CLI 의 비대화 모드 기본값대로 거절된다).
- 로봇 MCP 서버는 게이트웨이가 **자기 토큰**으로 붙인다 — `--mcp-config` 파일(0600)에 `${PIPER_API_TOKEN}`
  참조만 두고 값은 환경변수로 준다. 명령줄에 토큰이 안 보인다.
- 한 턴에 예산(USD)·시간 상한이 있고, 동시에 한 턴이다. [중지]는 프로세스를 죽인다.
- 컨테이너 게이트웨이는 CLI 를 못 돌린다 — `available=false` 와 이유를 돌려주고 화면은 그걸 그대로 말한다.
  호스트에서 대신 돌리는 길(unitd 일시 유닛)은 아직 없다.

## 세션

Claude Code 는 `--resume <session_id>` 로 대화를 잇는다 — init/result 사건의 session_id 를 기억해 다음 턴에
넘긴다. [새 대화]가 그걸 비운다. 나머지 CLI 는 턴마다 새로 시작한다(multi_turn=False).

## 스트림

Claude Code 의 `stream-json`(+partial) 을 사건으로 바꾼다: init(session·MCP 상태) → delta(글 조각) →
text/tool_use/tool_result → result(비용·시간). 텍스트 CLI 는 끝나고 한 번에 text. 전부 `agent_chat` 타입,
`turn` 번호로 묶인다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import time
from pathlib import Path

from piper_bus import agent_clis

from app.core.config import settings
from app.services import agent_chat_prompt as P

logger = logging.getLogger(__name__)

TURN_TIMEOUT_S = 300.0
BUDGET_USD = 2.0
MAX_MESSAGES = 200
TOOL_RESULT_CHARS = 400
MODES = ("observe", "control", "develop")
MCP_CONFIG_FILE = "agent_chat_mcp.json"


def mcp_config_path() -> Path:
    """Claude Code 에 줄 MCP 설정 파일 — piper 서버 하나, 토큰은 환경변수 참조. 매번 다시 쓴다(포트·파이썬이 바뀔 수 있다)."""
    p = Path(settings.config_dir) / MCP_CONFIG_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = {"mcpServers": {"piper": {
        "type": "stdio", "command": sys.executable,
        "args": ["-m", "piper_plugin.mcp", "--api", f"http://127.0.0.1:{settings.port}"],
        "env": {"PIPER_API_TOKEN": "${PIPER_API_TOKEN}"},
    }}}
    p.write_text(json.dumps(cfg, indent=2))
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def build_argv(cli_id: str, bin_path: str, prompt: str, *, mode: str = "observe", session_id: str | None = None,
               mcp_config: Path | None = None, budget_usd: float = BUDGET_USD) -> list[str]:
    """CLI 별 헤드리스 명령줄. **토큰은 절대 여기 안 들어간다** — 환경변수로 간다."""
    spec = agent_clis.headless(cli_id)
    if spec is None:
        raise ValueError(f"{cli_id} 는 웹 채팅을 지원하지 않습니다")
    argv = [a.replace("{bin}", bin_path).replace("{prompt}", prompt) for a in spec["argv"]]
    if spec["format"] == "claude-stream-json":
        tools = P.TOOLS_BY_MODE.get(mode, P.OBSERVE_TOOLS)
        denied = P.DISALLOWED_BY_MODE.get(mode, P.DISALLOWED_TOOLS)
        prompt_text = P.SYSTEM_PROMPT + (P.DEVELOP_PROMPT if mode == "develop" else "")
        argv += ["--allowedTools", *tools, "--disallowedTools", *denied,
                 "--append-system-prompt", prompt_text, "--max-budget-usd", f"{budget_usd:g}"]
        if mcp_config is not None:
            argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
        if session_id:
            argv += ["--resume", session_id]
    return argv


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    # 게이트웨이의 PATH 는 좁을 수 있다(유닛·tmux). piper-plugin·piper-mcp 콘솔 스크립트는 게이트웨이 파이썬 옆에 있다 —
    # 그 디렉터리를 앞에 둔다. 실기에서 개발 모드의 에이전트가 `piper-plugin: command not found` 로 막혔다 (2026-10-27).
    bin_dir = str(Path(sys.executable).resolve().parent)
    path = [d for d in env.get("PATH", "").split(os.pathsep) if d and d != bin_dir]
    path.insert(0, bin_dir)        # 늘 맨 앞 — 다른 파이썬의 piper-plugin 이 가리지 않게
    # PIPER_AGENT_CHAT=1 — 에이전트가 띄우는 `piper-plugin run` 이 게이트웨이에 붙는 것을 CLI 쪽이 거절한다 (개발 모드 가드)
    env.update(NO_COLOR="1", TERM="dumb", PATH=os.pathsep.join(path),
               PIPER_API_TOKEN=settings.api_token or "", PIPER_AGENT_CHAT="1")
    return env


class AgentChat:
    """채팅 하나(게이트웨이당 하나). 상태는 메모리 — 게이트웨이가 재시작하면 말풍선은 비고 세션 id 만 파일에 남는다."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.session_id: str | None = None
        self.mode: str = "observe"
        self.turn: int = 0
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.last: dict | None = None
        self._session_file = Path(settings.config_dir) / "agent_chat_session.json"
        try:
            d = json.loads(self._session_file.read_text())
            self.session_id, self.mode = d.get("session_id") or None, d.get("mode") if d.get("mode") in MODES else "observe"
        except Exception:
            pass

    # ── 상태 ──

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def availability(self) -> dict:
        """지금 이 게이트웨이가 채팅을 띄울 수 있나 — 못 하면 **이유**를 말한다. 지어내지 않는다."""
        from app.services import agent_link

        link = agent_link.collect()
        cli_id = link["preferred"]
        cli = next((c for c in link["clis"] if c["id"] == cli_id), None)
        spec = agent_clis.headless(cli_id)
        out = {"cli": cli_id, "cli_name": cli["name"] if cli else cli_id, "version": cli.get("version") if cli else None,
               "multi_turn": bool(spec and spec.get("multi_turn")), "format": spec["format"] if spec else None,
               "ext_enabled": bool(settings.api_token), "available": False, "reason": None, "path": None}
        if link.get("gateway_in_container"):
            out["reason"] = "컨테이너 게이트웨이는 호스트의 CLI 를 돌릴 수 없습니다 — 소스로 도는 게이트웨이에서만 됩니다. 터미널에서 쓰세요."
        elif spec is None:
            out["reason"] = f"{out['cli_name']} 은(는) 웹 채팅을 지원하지 않습니다 — 설정 → 에이전트에서 다른 CLI 를 고르거나 터미널에서 쓰세요."
        elif not cli or not cli.get("installed") or cli.get("where") != "gateway":
            out["reason"] = f"{out['cli_name']} 이(가) 게이트웨이 기계에 없습니다 — 설정 → 에이전트에서 깔린 CLI 를 고르세요."
        else:
            out.update(available=True, path=cli.get("path"))
        return out

    def status(self) -> dict:
        return {**self.availability(), "mode": self.mode, "session_id": self.session_id, "running": self.running,
                "turn": self.turn, "messages": self.messages[-MAX_MESSAGES:], "last": self.last,
                "budget_usd": BUDGET_USD, "timeout_s": TURN_TIMEOUT_S}

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"모르는 모드입니다: {mode} — observe · control · develop 중 하나")
        self.mode = mode
        self._persist()

    def reset(self) -> None:
        self.session_id = None
        self.messages = []
        self.last = None
        self._persist()

    def _persist(self) -> None:
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_file.write_text(json.dumps({"session_id": self.session_id, "mode": self.mode}))
        except OSError as exc:
            logger.warning("채팅 세션 저장 실패: %s", exc)

    # ── 턴 ──

    async def start_turn(self, text: str) -> int:
        text = (text or "").strip()
        if not text:
            raise ValueError("빈 글입니다")
        if self.running:
            raise RuntimeError("이미 답하는 중입니다 — 끝나거나 [중지]한 뒤에")
        avail = self.availability()
        if not avail["available"]:
            raise LookupError(avail["reason"])
        self.turn += 1
        turn = self.turn
        self._add({"role": "user", "text": text, "turn": turn})
        spec = agent_clis.headless(avail["cli"]) or {}
        argv = build_argv(avail["cli"], avail["path"], text, mode=self.mode,
                          session_id=self.session_id if spec.get("multi_turn") else None,
                          mcp_config=mcp_config_path() if spec.get("format") == "claude-stream-json" else None)
        self.task = asyncio.create_task(self._run(turn, argv, spec.get("format") or "text"))
        return turn

    async def stop(self) -> bool:
        if not self.running or self.proc is None:
            return False
        try:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3.0)
            except asyncio.TimeoutError:
                self.proc.kill()
        except ProcessLookupError:
            pass
        return True

    async def _run(self, turn: int, argv: list[str], fmt: str) -> None:
        from app.routers.ws import broadcast_agent_chat

        async def emit(kind: str, **data) -> None:
            await broadcast_agent_chat({"turn": turn, "kind": kind, **data})

        t0 = time.monotonic()
        await emit("status", running=True)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(_repo_root()), env=child_env(), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except Exception as exc:
            self._add({"role": "assistant", "text": f"CLI 를 띄우지 못했습니다: {exc}", "turn": turn, "is_error": True})
            await emit("error", text=f"CLI 를 띄우지 못했습니다: {exc}")
            await emit("status", running=False)
            return
        stderr_task = asyncio.create_task(self.proc.stderr.read())
        try:
            if fmt == "claude-stream-json":
                await asyncio.wait_for(self._read_claude(turn, emit), TURN_TIMEOUT_S)
            else:
                await asyncio.wait_for(self._read_text(turn, emit), TURN_TIMEOUT_S)
            rc = await asyncio.wait_for(self.proc.wait(), 10.0)
        except asyncio.TimeoutError:
            await self.stop()
            rc = -1
            self._add({"role": "assistant", "text": f"시간 상한({TURN_TIMEOUT_S:g}초)을 넘겨 중지했습니다", "turn": turn, "is_error": True})
            await emit("error", text=f"시간 상한({TURN_TIMEOUT_S:g}초)을 넘겨 중지했습니다")
        stderr = (await stderr_task).decode("utf-8", "replace").strip()
        if rc not in (0, None) and not any(m.get("turn") == turn and m.get("role") == "assistant" for m in self.messages):
            tail = "\n".join(stderr.splitlines()[-8:]) if stderr else f"종료 코드 {rc}"
            self._add({"role": "assistant", "text": f"CLI 가 실패했습니다:\n{tail}", "turn": turn, "is_error": True})
            await emit("error", text=f"CLI 가 실패했습니다:\n{tail}")
        self.last = {"turn": turn, "rc": rc, "duration_ms": int((time.monotonic() - t0) * 1000),
                     "stderr_tail": "\n".join(stderr.splitlines()[-3:]) if stderr else ""}
        self.proc = None
        await emit("status", running=False)

    async def _read_text(self, turn: int, emit) -> None:
        out = (await self.proc.stdout.read()).decode("utf-8", "replace").strip()
        if out:
            self._add({"role": "assistant", "text": out, "turn": turn})
            await emit("text", text=out)

    async def _read_claude(self, turn: int, emit) -> None:
        partial: list[str] = []
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id") or self.session_id
                self._persist()
                mcp = [{"name": m.get("name"), "status": m.get("status")} for m in ev.get("mcp_servers") or []]
                await emit("init", session_id=self.session_id, mcp=mcp)
            elif kind == "stream_event":
                e = ev.get("event") or {}
                if e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                    piece = e["delta"].get("text") or ""
                    partial.append(piece)
                    await emit("delta", text=piece)
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        partial.clear()
                        self._add({"role": "assistant", "text": block["text"], "turn": turn})
                        await emit("text", text=block["text"])
                    elif block.get("type") == "tool_use":
                        self._add({"role": "tool", "name": block.get("name"), "input": block.get("input"), "turn": turn})
                        await emit("tool_use", name=block.get("name"), input=block.get("input"))
            elif kind == "user":
                for block in (ev.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        text = _tool_result_text(block)
                        is_err = bool(block.get("is_error"))
                        self._add({"role": "tool_result", "text": text, "turn": turn, "is_error": is_err})
                        await emit("tool_result", text=text, is_error=is_err)
            elif kind == "result":
                self.session_id = ev.get("session_id") or self.session_id
                self._persist()
                if ev.get("is_error") and ev.get("result"):
                    self._add({"role": "assistant", "text": str(ev["result"]), "turn": turn, "is_error": True})
                    await emit("error", text=str(ev["result"]))
                await emit("result", session_id=self.session_id, cost_usd=ev.get("total_cost_usd"),
                           duration_ms=ev.get("duration_ms"), is_error=bool(ev.get("is_error")),
                           text=str(ev.get("result") or "")[:2000])
        if partial:   # 최종 assistant 메시지 없이 끝났다 — 조각이라도 남긴다
            self._add({"role": "assistant", "text": "".join(partial), "turn": turn})

    def _add(self, m: dict) -> None:
        m.setdefault("at", time.time())
        self.messages.append(m)
        if len(self.messages) > MAX_MESSAGES:
            del self.messages[: len(self.messages) - MAX_MESSAGES]


def _tool_result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        c = " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in c)
    s = str(c or "")
    return s if len(s) <= TOOL_RESULT_CHARS else s[:TOOL_RESULT_CHARS] + "…"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def shell_preview(argv: list[str]) -> str:
    """로그·테스트용 — 사람이 읽는 명령줄."""
    return " ".join(shlex.quote(a) for a in argv)


chat = AgentChat()
