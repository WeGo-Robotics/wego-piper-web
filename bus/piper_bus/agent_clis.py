"""로컬 AI CLI — 무엇이 깔려 있나, 어떻게 붙이나 (feature/agent-cli.md §4-7).

게이트웨이(소스로 돌 때)와 호스트의 unitd 가 **같은 표**로 같은 판정을 한다 — 컨테이너 게이트웨이는
호스트의 CLI 를 못 보므로 unitd 의 `host_info` 가 이 표로 대신 본다. 그래서 `piper_bus` 에 있고 표준
라이브러리만 쓴다.

표는 두 가지를 든다.

- **감지** — 실행 파일 이름. PATH 에 더해 사용자 디렉터리(~/.local/bin·npm·nvm·cargo·go)를 본다.
  유닛으로 뜬 프로세스의 PATH 는 좁아서, 거기만 보면 깔린 것을 "없다" 고 한다 (실제로 `claude` 는
  `~/.local/bin` 에 있다). 버전은 `--version` 한 번 — 찾은 것만, 짧은 시간 제한으로.
- **레시피** — MCP 등록 명령·설정 파일 조각·꾸러미를 묻는 한 줄. `{origin}` 은 게이트웨이 주소(브라우저가
  채운다 — 에이전트 기계에서 닿는 주소가 그것이다), `{question}` 은 질문, `<TOKEN>` 은 사람이 게이트웨이
  기계의 backend/.env 에서 옮기는 값. **여기엔 절대 값이 없다.**

⚠ 등록 **명령**은 CLI 버전에 따라 바뀔 수 있다 — 설정 **파일** 조각이 더 오래 간다. 둘 다 준다.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from pathlib import Path

_MCP_JSON = (
    '{\n  "mcpServers": {\n    "piper": {\n      "command": "piper-mcp",\n'
    '      "args": ["--api", "{origin}"],\n      "env": { "PIPER_API_TOKEN": "<TOKEN>" }\n    }\n  }\n}'
)

#: 순서가 곧 기본 선택의 우선순위다 — 저장한 선택이 없으면 깔린 것 중 첫째
KNOWN: tuple[dict, ...] = (
    {"id": "claude", "name": "Claude Code", "bin": "claude", "vendor": "Anthropic",
     "mcp": {"add": "claude mcp add -s project -e PIPER_API_TOKEN=<TOKEN> piper -- piper-mcp --api {origin}",
             "config_path": ".mcp.json (프로젝트) 또는 ~/.claude.json",
             "config": ('{\n  "mcpServers": {\n    "piper": {\n      "type": "stdio",\n      "command": "piper-mcp",\n'
                        '      "args": ["--api", "{origin}"],\n      "env": { "PIPER_API_TOKEN": "${PIPER_API_TOKEN:-}" }\n'
                        '    }\n  }\n}')},
     "ask": 'curl -s {origin}/api/system/agent-context | claude -p "{question}"',
     "notes": ["저장소 안에서 열면 .mcp.json 과 스킬(piper-plugin · piper-episode)이 이미 있다 — 환경변수 PIPER_API_TOKEN 만 두면 된다.",
               "plugins/ 아래 파일을 저장하면 훅이 매니페스트를 바로 검사한다."]},
    {"id": "codex", "name": "Codex CLI", "bin": "codex", "vendor": "OpenAI",
     "mcp": {"add": "codex mcp add piper --env PIPER_API_TOKEN=<TOKEN> -- piper-mcp --api {origin}",
             "config_path": "~/.codex/config.toml",
             "config": '[mcp_servers.piper]\ncommand = "piper-mcp"\nargs = ["--api", "{origin}"]\nenv = { PIPER_API_TOKEN = "<TOKEN>" }'},
     "ask": 'codex exec "$(curl -s {origin}/api/system/agent-context)\n\n{question}"',
     "notes": ["AGENTS.md(CLAUDE.md 로 이어진다)를 읽는다. 스킬·훅은 Claude Code 전용이라 plugins/README.md 의 순서를 직접 따른다.",
               "등록 명령은 버전에 따라 다를 수 있다 — config.toml 조각이 정본이다."]},
    {"id": "gemini", "name": "Gemini CLI", "bin": "gemini", "vendor": "Google",
     "mcp": {"add": "gemini mcp add -s project -e PIPER_API_TOKEN=<TOKEN> piper piper-mcp --api {origin}",
             "config_path": ".gemini/settings.json (프로젝트) 또는 ~/.gemini/settings.json",
             "config": _MCP_JSON},
     "ask": 'curl -s {origin}/api/system/agent-context | gemini -p "{question}"',
     "notes": ["GEMINI.md 를 읽는다 — contextFileName 설정으로 AGENTS.md 를 읽게 할 수 있다."]},
    {"id": "qwen", "name": "Qwen Code", "bin": "qwen", "vendor": "Alibaba",
     "mcp": {"add": "qwen mcp add -s project -e PIPER_API_TOKEN=<TOKEN> piper piper-mcp --api {origin}",
             "config_path": ".qwen/settings.json (프로젝트) 또는 ~/.qwen/settings.json",
             "config": _MCP_JSON},
     "ask": 'curl -s {origin}/api/system/agent-context | qwen -p "{question}"',
     "notes": ["Gemini CLI 와 같은 모양이다 — QWEN.md 를 읽는다."]},
    {"id": "opencode", "name": "OpenCode", "bin": "opencode", "vendor": "SST",
     "mcp": {"add": None,
             "config_path": "opencode.json (프로젝트) 또는 ~/.config/opencode/opencode.json",
             "config": ('{\n  "$schema": "https://opencode.ai/config.json",\n  "mcp": {\n    "piper": {\n      "type": "local",\n'
                        '      "command": ["piper-mcp", "--api", "{origin}"],\n      "environment": { "PIPER_API_TOKEN": "<TOKEN>" },\n'
                        '      "enabled": true\n    }\n  }\n}')},
     "ask": 'opencode run "$(curl -s {origin}/api/system/agent-context)\n\n{question}"',
     "notes": ["AGENTS.md 를 읽는다."]},
    {"id": "cursor", "name": "Cursor Agent", "bin": "cursor-agent", "vendor": "Cursor",
     "mcp": {"add": None,
             "config_path": ".cursor/mcp.json (프로젝트) 또는 ~/.cursor/mcp.json",
             "config": _MCP_JSON},
     "ask": 'cursor-agent -p "$(curl -s {origin}/api/system/agent-context)\n\n{question}"',
     "notes": ["AGENTS.md 또는 .cursor/rules 를 읽는다."]},
    {"id": "copilot", "name": "GitHub Copilot CLI", "bin": "copilot", "vendor": "GitHub",
     "mcp": {"add": None,
             "config_path": "~/.copilot/mcp-config.json",
             "config": ('{\n  "mcpServers": {\n    "piper": {\n      "type": "local",\n      "command": "piper-mcp",\n'
                        '      "args": ["--api", "{origin}"],\n      "env": { "PIPER_API_TOKEN": "<TOKEN>" },\n'
                        '      "tools": ["*"]\n    }\n  }\n}')},
     "ask": 'copilot -p "$(curl -s {origin}/api/system/agent-context)\n\n{question}"',
     "notes": ["대화 중 /mcp add 로도 붙일 수 있다."]},
    {"id": "goose", "name": "Goose", "bin": "goose", "vendor": "Block",
     "mcp": {"add": "goose configure",
             "config_path": "~/.config/goose/config.yaml",
             "config": ('extensions:\n  piper:\n    enabled: true\n    type: stdio\n    name: piper\n    cmd: piper-mcp\n'
                        '    args: ["--api", "{origin}"]\n    envs: { PIPER_API_TOKEN: "<TOKEN>" }\n    timeout: 300')},
     "ask": 'goose run -t "$(curl -s {origin}/api/system/agent-context)\n\n{question}"',
     "notes": ["goose configure 에서 Add Extension → Command-line Extension 으로 piper-mcp 를 고른다 — 조각을 직접 적어도 같다."]},
    {"id": "aider", "name": "Aider", "bin": "aider", "vendor": "Aider",
     "mcp": None,
     "ask": 'curl -s {origin}/api/system/agent-context > piper-context.md && aider --read piper-context.md --message "{question}"',
     "notes": ["MCP 가 없다 — 꾸러미와 에피소드 묶음(/api/datasets/<id>/episodes/<n>/bundle)을 파일로 넘긴다."]},
)

EXTRA_DIRS = ("~/.local/bin", "~/bin", "~/.npm-global/bin", "~/.cargo/bin", "~/go/bin",
              "/usr/local/bin", "/opt/homebrew/bin")
VERSION_TIMEOUT_S = 2.0
_cache: dict = {"at": 0.0, "items": None}


#: 웹 채팅이 CLI 를 **한 턴씩** 띄우는 모양 (feature/agent-cli.md §4-6). `{bin}` 은 찾은 실행 파일,
#: `{prompt}` 는 사람의 글. 도구 제한·MCP 주입·세션 이어가기는 Claude Code 만 안다(format 이 그걸 말한다) —
#: 나머지는 텍스트 한 덩이로 돌려받고 턴마다 새로 시작한다. 없는 것(aider)은 웹 채팅이 안 된다.
HEADLESS: dict[str, dict | None] = {
    "claude": {"format": "claude-stream-json", "multi_turn": True,
               "argv": ["{bin}", "-p", "{prompt}", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]},
    "codex": {"format": "text", "multi_turn": False, "argv": ["{bin}", "exec", "{prompt}"]},
    "gemini": {"format": "text", "multi_turn": False, "argv": ["{bin}", "-p", "{prompt}"]},
    "qwen": {"format": "text", "multi_turn": False, "argv": ["{bin}", "-p", "{prompt}"]},
    "opencode": {"format": "text", "multi_turn": False, "argv": ["{bin}", "run", "{prompt}"]},
    "cursor": {"format": "text", "multi_turn": False, "argv": ["{bin}", "-p", "{prompt}"]},
    "copilot": {"format": "text", "multi_turn": False, "argv": ["{bin}", "-p", "{prompt}"]},
    "goose": {"format": "text", "multi_turn": False, "argv": ["{bin}", "run", "-t", "{prompt}"]},
    "aider": None,
}


def known_ids() -> list[str]:
    return [k["id"] for k in KNOWN]


def headless(cli_id: str) -> dict | None:
    return HEADLESS.get(cli_id)


def search_dirs() -> list[str]:
    """PATH + 사용자 디렉터리 + nvm 의 node bin — 중복 없이, PATH 순서 먼저."""
    out: list[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and d not in out:
            out.append(d)
    for d in EXTRA_DIRS:
        d = os.path.expanduser(d)
        if d not in out:
            out.append(d)
    for d in sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True):
        if d not in out:
            out.append(d)
    return out


def find(bin_name: str) -> str | None:
    for d in search_dirs():
        p = Path(d) / bin_name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return shutil.which(bin_name)


def _version(path: str) -> str | None:
    """`--version` 의 첫 줄. 못 읽으면 None — 깔린 것은 맞으니 감지는 유지한다."""
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S,
                           stdin=subprocess.DEVNULL, env=env)
    except Exception:
        return None
    for line in (r.stdout or "").splitlines() + (r.stderr or "").splitlines():
        line = line.strip()
        if line:
            return line[:80]
    return None


def detect(ttl: float = 60.0) -> list[dict]:
    """알려진 CLI 전부 — `installed`·`path`·`version`. 결과는 ttl 초 캐시 (프로세스를 매번 띄우지 않는다)."""
    now = time.monotonic()
    if _cache["items"] is not None and now - _cache["at"] < ttl:
        return [dict(i) for i in _cache["items"]]
    items = []
    for k in KNOWN:
        path = find(k["bin"])
        items.append({"id": k["id"], "name": k["name"], "vendor": k["vendor"], "bin": k["bin"],
                      "installed": path is not None, "path": path,
                      "version": _version(path) if path else None})
    _cache.update(at=now, items=items)
    return [dict(i) for i in items]


def reset_cache() -> None:
    _cache.update(at=0.0, items=None)


def recipes() -> dict[str, dict]:
    """id → {name, vendor, mcp, ask, notes}. 자리표시자는 그대로 — 채우는 쪽은 화면이다."""
    return {k["id"]: {"name": k["name"], "vendor": k["vendor"], "mcp": k["mcp"], "ask": k["ask"], "notes": list(k["notes"])}
            for k in KNOWN}


def default_preferred(detected: list[dict]) -> str:
    """저장한 선택이 없을 때 — 깔린 것 중 표의 첫째, 아무것도 없으면 claude(저장소가 그 스킬·훅을 들고 있다)."""
    installed = {d["id"] for d in detected if d.get("installed")}
    for k in KNOWN:
        if k["id"] in installed:
            return k["id"]
    return "claude"
