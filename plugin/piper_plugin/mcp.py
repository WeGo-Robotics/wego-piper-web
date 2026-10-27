"""piper-mcp — 외부 제어 API(`/api/ext/v1`) 위의 MCP 어댑터, stdio (feature/agent-cli.md §4-3).

로컬 코딩 에이전트(Claude Code 등)가 로봇을 **미션 수준**으로 조종하는 창구다. 도구는 ext v1 과
1:1 이고 — 상태·미션·플러그인·검출·detectord·E-stop — 장치 수준(추론 시작·조그·캘리브레이션)은
없다. 외부 API 의 원칙 그대로다: 준비는 사람 몫이고 전제가 안 되면 409 가 온다.

## 왜 stdio 인가 — 데드맨

이 서버는 에이전트(CLI)의 **자식 프로세스**다. 에이전트가 죽으면(창을 닫든 크래시든) stdin 이
닫혀 서버가 끝나고 heartbeat 가 끊기며 estopd 가 2.5초 안에 팔을 세운다. 데드맨이 프로세스
수명에 묶인다 — "외부 시스템이 죽으면 로봇이 선다"(external-api.md §1)가 추가 코드 없이 성립한다.

## heartbeat 는 도구가 아니다 — 서버가 펌프한다, 자기 미션에만

LLM 의 턴은 500ms 주기를 못 지킨다. 그래서 `piper_mission_start` 가 성공하면 이 프로세스가
박동 스레드를 켜고, 그 미션이 끝남·취소·사라짐을 보이면 끈다. **자기가 시작하지 않은 미션에는
펌프하지 않는다** — 브라우저가 시작한 추론을 여기서 살려 두면 브라우저가 죽어도 로봇이 안 서는,
거짓 데드맨이 된다(agent-cli.md §3 원칙 2).

## 의존

`mcp` SDK 는 선택 의존이다(`pip install 'piper_plugin[mcp]'`). 핵심(`PiperMcp`·`HeartbeatPump`)은
SDK 없이 import 되고 테스트된다 — SDK 는 `build_server()`·`main()` 에서만 쓴다. v1(FastMCP)·
v2(MCPServer) 둘 다 받는다. REST 는 `piper_plugin.api.Api`(표준 라이브러리) 그대로다.

⚠ stdout 은 프로토콜 채널이다. 로그는 전부 stderr 로.
⚠ 이 파일의 한국어(도구 설명·오류)는 화면 문구가 아니라 에이전트가 읽는 글이다 —
   `tools/i18n.py` 의 `PY_SKIP_FILES` 가 이 파일을 뺀다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from piper_plugin.api import Api, Response

logger = logging.getLogger("piper-mcp")

EXT = "/api/ext/v1"
HEARTBEAT_S = 0.5      # external-api.md §1: 500ms~1s 권장
POLL_S = 2.0           # 미션이 끝났나 보는 주기
ESTOP_TIMEOUT_S = 2.5  # 실측값 — 설명에만 쓴다. 판정은 estopd 가 한다

#: 도구 이름의 정본 — `build_server()` 가 정확히 이것들을 등록한다 (테스트가 대조).
#: ext v1 의 제어 표면 12개 + 진단 꾸러미 `piper_context` 하나(읽기 전용 — 리소스로도 내지만, 클라이언트가
#: 도구를 훨씬 잘 찾으므로 "왜 죽었나" 의 첫 손잡이는 도구로도 둔다. agent-cli.md §4-4)
TOOL_NAMES = (
    "piper_status", "piper_context", "piper_episode_bundle",
    "piper_missions", "piper_mission_get", "piper_mission_start", "piper_mission_cancel",
    "piper_plugins", "piper_plugin_start", "piper_plugin_stop",
    "piper_detections", "piper_vision_start", "piper_vision_stop",
    "piper_estop",
)

#: 읽기 전용 리소스 — ext v1 §4-4 의 GET 들. 도구 없이 "읽기" 로 붙는다
RESOURCE_URIS = ("piper://context", "piper://status", "piper://missions", "piper://plugins",
                 "piper://datasets", "piper://eval/stats", "piper://training")
#: 에피소드 요약 템플릿 — `{+dataset_id}` 는 `user/name` 처럼 `/` 가 든 id 를 받는다 (RFC 6570 reserved expansion)
EPISODE_TEMPLATE = "piper://datasets/{+dataset_id}/episodes/{episode}"

INSTRUCTIONS = """PIPER 로봇 게이트웨이를 미션 수준으로 조종한다 (feature/agent-cli.md §4-3, feature/external-api.md).
- 먼저 piper_status 로 무엇이 돌고 있는지 본다. 미션은 동시에 하나다.
- piper_mission_start 는 dry_run 이 기본이다. 실물로 팔을 움직이는 것은 사람이 명시적으로 허락한 뒤에만 dry_run=false 로.
- 장치 준비(정책 배포·카메라·캘리브레이션)는 사람 몫이다. 전제가 안 되면 409 가 온다 — 장치를 손대려 하지 말고 사람에게 말한다.
- heartbeat 는 이 서버가 자기가 시작한 미션에만 자동으로 보낸다. 이 서버의 부모(당신)가 죽으면 로봇은 2.5초 안에 선다 — 그게 설계다.
- 위험하면 piper_estop. 즉시 멈춘다. 묻지 말고 누른다.
"""


class GatewayError(RuntimeError):
    """게이트웨이가 2xx 가 아니었다 — 상태 코드와 본문의 detail, 그리고 다음에 할 일을 든다."""

    HINTS = {
        0: "게이트웨이 주소가 맞고 떠 있는지 본다 (--api)",
        401: "토큰이 틀렸다 — PIPER_API_TOKEN 이 게이트웨이 backend/.env 의 값과 같아야 한다",
        403: "이 토큰으로는 못 하는 일이다",
        404: "그런 것이 없다",
        409: "전제가 안 됐다(다른 활동·미션·준비 안 된 장치) — 사람이 화면에서 정리해야 한다",
        503: "외부 API 가 꺼져 있다 — 게이트웨이 backend/.env 에 PIPER_API_TOKEN 을 설정해야 켜진다",
    }

    def __init__(self, status: int, path: str, detail: str, hint: str | None = None):
        self.status, self.path, self.detail = status, path, detail
        hint = hint if hint is not None else self.HINTS.get(status)
        super().__init__(f"게이트웨이 {status} {path}: {detail or '(본문 없음)'}" + (f" — {hint}" if hint else ""))


class HeartbeatPump:
    """자기가 시작한 미션이 살아 있는 동안만 박동 — 끝남을 보면 스스로 멈춘다.

    스레드는 daemon 이다: 서버 프로세스가 죽으면 같이 죽는다. 그게 곧 "끊김" 이고 estopd 가
    그걸 보고 세운다. 박동 실패(네트워크·5xx)는 세어 두고 계속 시도한다 — 여기서 포기하면
    일시적 끊김이 곧 정지가 되는데, 그 판정은 estopd 의 것이다.
    """

    def __init__(self, api: Api, run_id: str, *, interval_s: float = HEARTBEAT_S, poll_s: float = POLL_S):
        self.api, self.run_id = api, run_id
        self.interval_s, self.poll_s = interval_s, poll_s
        self.beats = 0
        self.failures = 0
        self.ended_reason: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"piper-mcp-heartbeat:{run_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, reason: str = "stopped") -> None:
        if self.ended_reason is None:
            self.ended_reason = reason
        self._stop.set()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _loop(self) -> None:
        last_poll = time.monotonic()
        while not self._stop.is_set():
            try:
                r = self.api.post(f"{EXT}/heartbeat", timeout_s=1.0)
                if r.ok:
                    self.beats += 1
                else:
                    self.failures += 1
                    logger.warning("heartbeat %s: %s", r.status, r.detail or r.text[:120])
            except Exception as exc:
                self.failures += 1
                logger.warning("heartbeat 실패: %s", exc)
            now = time.monotonic()
            if now - last_poll >= self.poll_s:
                last_poll = now
                if self._mission_over():
                    break
            self._stop.wait(self.interval_s)

    def _mission_over(self) -> bool:
        try:
            r = self.api.get(f"{EXT}/missions/{self.run_id}", timeout_s=3.0)
        except Exception as exc:
            logger.warning("미션 상태 조회 실패 — 계속 박동한다: %s", exc)
            return False
        if r.status == 404:
            self.stop("gone")
            return True
        if r.ok and not (r.json() or {}).get("live", True):
            self.stop("finished")
            return True
        return False


def extract_zip(data: bytes, dest: Path, path: str = "bundle") -> list[str]:
    """zip → dest. 경로 이탈(`..`·절대 경로) 항목이 하나라도 있으면 묶음 **전체**를 거절한다."""
    import io
    import zipfile

    base = dest.resolve()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise GatewayError(0, path, f"zip 이 아니다: {exc}", hint="게이트웨이가 묶음 대신 다른 것을 돌려줬다 — 주소·버전을 본다")
    out: list[str] = []
    with zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or not (base / rel).resolve().is_relative_to(base):
                raise GatewayError(0, path, f"묶음에 경로 이탈 항목이 있다: {name!r}",
                                   hint="게이트웨이가 만든 묶음이 아니다 — 버린다")
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            out.append(name)
    return sorted(out)


class PiperMcp:
    """도구의 본체 — SDK 없이 돈다. `build_server()` 가 이 메서드들을 MCP 도구로 감싼다."""

    def __init__(self, api: Api, *, heartbeat_s: float = HEARTBEAT_S, poll_s: float = POLL_S):
        self.api = api
        self.heartbeat_s, self.poll_s = heartbeat_s, poll_s
        self.pump: HeartbeatPump | None = None
        self.started: list[str] = []      # 이 서버가 시작한 run id — 펌프 자격의 전부

    # ── REST ──

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            r: Response = self.api.request(method, path, json_body=body)
        except Exception as exc:          # URLError 등 — 게이트웨이에 닿지 못했다
            raise GatewayError(0, path, f"연결 실패 ({self.api.base}): {exc}")
        if not r.ok:
            raise GatewayError(r.status, path, r.detail or r.text[:300])
        out = r.json()
        return out if isinstance(out, dict) else {"result": out}

    # ── 상태 ──

    def status(self) -> dict:
        out = self._call("GET", f"{EXT}/status")
        out["mcp"] = self.pump_status()
        return out

    def pump_status(self) -> dict:
        p = self.pump
        return {"heartbeat_for": p.run_id if p and p.alive else None,
                "beats": p.beats if p else 0, "failures": p.failures if p else 0,
                "last_ended": p.ended_reason if p else None,
                "started_here": list(self.started)}

    # ── 미션 ──

    def missions(self) -> dict:
        return self._call("GET", f"{EXT}/missions")

    def mission_get(self, mission_id: str) -> dict:
        return self._call("GET", f"{EXT}/missions/{mission_id}")

    def mission_start(self, type: str, config: dict | None = None, cameras: dict | None = None,
                      arms: dict | None = None, dry_run: bool = True) -> dict:
        body = {**(config or {}), "type": type, "dry_run": bool(dry_run),
                "cameras": cameras or {}, "arms": arms or {}}
        out = self._call("POST", f"{EXT}/missions", body)
        run_id = out.get("id")
        if run_id:
            self._pump_for(str(run_id))
            out["heartbeat"] = (f"이 서버가 run {run_id} 의 heartbeat 를 맡았다 — 이 프로세스가 죽으면 "
                                f"estopd 가 {ESTOP_TIMEOUT_S}초 안에 세운다")
        out["dry_run"] = bool(dry_run)
        return out

    def mission_cancel(self, mission_id: str) -> dict:
        out = self._call("POST", f"{EXT}/missions/{mission_id}/cancel")
        if self.pump and self.pump.run_id == mission_id:
            self.pump.stop("cancelled")
        return out

    def _pump_for(self, run_id: str) -> None:
        if self.pump and self.pump.alive:
            self.pump.stop("replaced")
        self.started.append(run_id)
        self.pump = HeartbeatPump(self.api, run_id, interval_s=self.heartbeat_s, poll_s=self.poll_s)
        self.pump.start()

    # ── 플러그인 (미션이 아닌 것 — 검출기 등) ──

    def plugins(self) -> dict:
        return self._call("GET", f"{EXT}/plugins")

    def plugin_start(self, name: str, config: dict | None = None, cameras: dict | None = None,
                     inputs: dict | None = None, arms: dict | None = None) -> dict:
        return self._call("POST", f"{EXT}/plugins/{name}/start",
                          {"config": config or {}, "cameras": cameras or {}, "inputs": inputs or {}, "arms": arms or {}})

    def plugin_stop(self, name: str) -> dict:
        return self._call("POST", f"{EXT}/plugins/{name}/stop")

    # ── 검출 ──

    def detections(self) -> dict:
        return self._call("GET", f"{EXT}/detections")

    def vision_start(self, cams: dict, model: str | None = None, conf: float | None = None) -> dict:
        body: dict[str, Any] = {"cams": cams}
        if model:
            body["model"] = model
        if conf is not None:
            body["conf"] = conf
        return self._call("POST", f"{EXT}/vision/start", body)

    def vision_stop(self) -> dict:
        return self._call("POST", f"{EXT}/vision/stop")

    # ── 읽기 전용 (ext v1 §4-4) — 리소스와 piper_context 의 재료 ──

    def _text(self, path: str) -> str:
        try:
            r: Response = self.api.request("GET", path)
        except Exception as exc:
            raise GatewayError(0, path, f"연결 실패 ({self.api.base}): {exc}")
        if not r.ok:
            raise GatewayError(r.status, path, r.detail or r.text[:300])
        return r.text

    def context(self, since: str | None = None, unit: str = "all", lines: int = 200) -> str:
        q: dict[str, str] = {"unit": unit or "all", "lines": str(int(lines or 200)), "format": "markdown"}
        if since:
            q["since"] = since
        return self._text(f"{EXT}/diag/context?{urlencode(q)}")

    def datasets(self) -> dict:
        return self._call("GET", f"{EXT}/datasets")

    def episode(self, dataset_id: str, episode: int) -> dict:
        return self._call("GET", f"{EXT}/datasets/{quote(dataset_id, safe='/')}/episodes/{int(episode)}")

    def eval_stats(self, last_n: int = 0) -> dict:
        return self._call("GET", f"{EXT}/eval/stats?last_n={int(last_n or 0)}")

    def training(self) -> dict:
        return self._call("GET", f"{EXT}/training")

    def _bytes(self, path: str) -> bytes:
        try:
            r: Response = self.api.request("GET", path, timeout_s=120.0)   # mp4 직접 디코딩은 수 초 걸린다
        except Exception as exc:
            raise GatewayError(0, path, f"연결 실패 ({self.api.base}): {exc}")
        if not r.ok:
            raise GatewayError(r.status, path, r.detail or r.text[:300])
        return r.body

    def episode_bundle(self, dataset_id: str, episode: int, cams=None, at: str = "phases",
                       out_dir: str | None = None) -> dict:
        """묶음 zip 을 받아 **로컬 폴더**에 푼다 — 이 서버는 에이전트가 도는 기계에서 돌기 때문에
        여기 푼 키프레임을 에이전트가 자기 파일 도구로 바로 읽는다 (agent-cli.md §4-5)."""
        import tempfile

        q: dict[str, str] = {"at": at or "phases"}
        if cams:
            q["cams"] = ",".join(cams) if isinstance(cams, (list, tuple)) else str(cams)
        path = f"{EXT}/datasets/{quote(dataset_id, safe='/')}/episodes/{int(episode)}/bundle?{urlencode(q)}"
        data = self._bytes(path)
        dest = Path(out_dir).expanduser() if out_dir else Path(tempfile.mkdtemp(prefix="piper-episode-"))
        dest.mkdir(parents=True, exist_ok=True)
        files = extract_zip(data, dest, path)
        readme_p, tl_p = dest / "README.md", dest / "timeline.json"
        readme = readme_p.read_text(encoding="utf-8") if readme_p.exists() else ""
        timeline: dict = {}
        if tl_p.exists():
            try:
                timeline = json.loads(tl_p.read_text(encoding="utf-8"))
            except ValueError:
                timeline = {}
        keyframes: dict[str, list[str]] = {}
        for f in files:
            parts = Path(f).parts
            if len(parts) == 3 and parts[0] == "keyframes":
                keyframes.setdefault(parts[1], []).append(str(dest / f))
        kf = timeline.get("keyframes") or {}
        return {"dir": str(dest), "files": files, "keyframes": {k: sorted(v) for k, v in keyframes.items()},
                "length": timeline.get("length"), "fps": timeline.get("fps"), "tasks": timeline.get("tasks"),
                "phases": timeline.get("phases"), "selected_frames": kf.get("frames"), "fallback": kf.get("fallback"),
                "missing": kf.get("missing"), "readme": readme}

    # ── 안전 ──

    def estop(self) -> dict:
        return self._call("POST", f"{EXT}/estop")

    def close(self) -> None:
        if self.pump:
            self.pump.stop("closed")


# ── MCP 서버 (SDK 가 있을 때만) ────────────────────────────────────────────────


def _sdk():
    """SDK v2(MCPServer) 또는 v1(FastMCP) — 둘 다 `tool()` 데코레이터와 `run("stdio")` 모양이 같다."""
    try:
        from mcp.server.mcpserver import MCPServer as Server
        from mcp.server.mcpserver.exceptions import ResourceError, ToolError
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server
            from mcp.server.fastmcp.exceptions import ResourceError, ToolError
        except ImportError as exc:
            raise SystemExit("mcp SDK 가 없다 — pip install 'piper_plugin[mcp]' (또는 pip install mcp)") from exc
    from mcp.types import ToolAnnotations

    return Server, ToolError, ResourceError, ToolAnnotations


def _ann(Ann, **camel):
    """ToolAnnotations — v1 은 camelCase 필드, v2 는 snake_case + alias. 둘 다 안 되면 없이 간다."""
    try:
        return Ann.model_validate(camel)
    except Exception:
        try:
            import re

            return Ann(**{re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), k): v for k, v in camel.items()})
        except Exception:
            return None


def build_server(core: PiperMcp):
    Server, ToolError, ResourceError, Ann = _sdk()
    server = Server("piper", instructions=INSTRUCTIONS)
    READ = _ann(Ann, readOnlyHint=True, openWorldHint=False)
    ACT = _ann(Ann, readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    STOP = _ann(Ann, readOnlyHint=False, destructiveHint=True, openWorldHint=False)

    def run(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except GatewayError as e:
            raise ToolError(str(e)) from e

    @server.tool(name="piper_status", annotations=READ,
                 description="종합 상태 한 장 — 도는 활동·E-stop·플러그인·라이브 미션·detectord, 그리고 이 서버의 heartbeat 상태(mcp). 먼저 부른다.")
    def piper_status() -> dict:
        return run(core.status)

    @server.tool(name="piper_context", annotations=READ,
                 description="진단 꾸러미 한 장(markdown) — 버전·유닛(낡은 코드)·활동·E-stop·장치·경고 이상 저널·미션 저널·읽을 문서. "
                             "\"왜 죽었나 / 왜 안 되나\" 는 이것부터 읽는다. since 는 journald 문법(기본 30 minutes ago), "
                             "unit 은 all 또는 유닛 id, lines 는 유닛당 저널 상한.")
    def piper_context(since: str | None = None, unit: str = "all", lines: int = 200) -> str:
        return run(core.context, since, unit, lines)

    @server.tool(name="piper_episode_bundle", annotations=ACT,
                 description="에피소드 묶음을 받아 로컬 폴더에 푼다 — keyframes/<cam>/frame-N.jpg(at=phases: 페이즈 경계 앞·뒤 + 첫·끝, "
                             "at=every:N: 균등), timeline.json(페이즈 구간·신호), state_action.csv, README.md. 돌아온 dir 의 키프레임을 "
                             "이미지로 읽고 README 의 '어떻게 보나' 를 따른다. \"이 에피소드 왜 빗나갔나\" 의 재료. 페이즈 분석이 없으면 "
                             "균등 샘플로 떨어진다(fallback=true). cams 는 카메라 이름 목록(기본 전부).")
    def piper_episode_bundle(dataset_id: str, episode: int, cams: list[str] | None = None, at: str = "phases",
                             out_dir: str | None = None) -> dict:
        return run(core.episode_bundle, dataset_id, episode, cams, at, out_dir)

    @server.tool(name="piper_missions", annotations=READ,
                 description="미션 이력 — 저널 목록, 최신 먼저. 끝난 미션도 id 로 piper_mission_get 할 수 있다.")
    def piper_missions() -> dict:
        return run(core.missions)

    @server.tool(name="piper_mission_get", annotations=READ,
                 description="미션 하나 — 진행 중이면 라이브 상태(live=true, 플러그인 상태 KV), 끝났으면 저널의 회차 기록(events).")
    def piper_mission_get(mission_id: str) -> dict:
        return run(core.mission_get, mission_id)

    @server.tool(name="piper_mission_start", annotations=ACT,
                 description="미션 시작. type 은 설치된 미션 플러그인 이름(예: recycling), config 는 그 설정(max_episodes·episode_timeout_s 등), "
                             "cameras/arms 는 슬롯→스트림/인터페이스. dry_run 기본 true — 실물은 사람이 허락한 뒤에만 false. "
                             "성공하면 이 서버가 heartbeat 를 맡는다(이 프로세스가 죽으면 로봇이 선다). 미션은 동시에 하나(409).")
    def piper_mission_start(type: str, config: dict[str, Any] | None = None, cameras: dict[str, str] | None = None,
                            arms: dict[str, str] | None = None, dry_run: bool = True) -> dict:
        return run(core.mission_start, type, config, cameras, arms, dry_run)

    @server.tool(name="piper_mission_cancel", annotations=STOP,
                 description="진행 중 미션을 중단한다. 이 서버가 맡은 heartbeat 도 함께 끝난다.")
    def piper_mission_cancel(mission_id: str) -> dict:
        return run(core.mission_cancel, mission_id)

    @server.tool(name="piper_plugins", annotations=READ,
                 description="설치된 플러그인과 상태. mission=true 인 것이 piper_mission_start 의 type 후보다.")
    def piper_plugins() -> dict:
        return run(core.plugins)

    @server.tool(name="piper_plugin_start", annotations=ACT,
                 description="미션이 아닌 플러그인(검출기 등)을 켠다. 비운 값은 화면에서 저장한 설정으로 돈다. 팔을 움직이는 길은 아니다.")
    def piper_plugin_start(name: str, config: dict[str, Any] | None = None, cameras: dict[str, str] | None = None,
                           inputs: dict[str, str] | None = None, arms: dict[str, str] | None = None) -> dict:
        return run(core.plugin_start, name, config, cameras, inputs, arms)

    @server.tool(name="piper_plugin_stop", annotations=STOP, description="플러그인을 끈다.")
    def piper_plugin_stop(name: str) -> dict:
        return run(core.plugin_stop, name)

    @server.tool(name="piper_detections", annotations=READ,
                 description="살아 있는 검출 결과 — detectord 페이로드 그대로(라벨·좌표·text).")
    def piper_detections() -> dict:
        return run(core.detections)

    @server.tool(name="piper_vision_start", annotations=ACT,
                 description="검출기(detectord)를 켠다. cams 는 별칭→스트림 이름(piper_status 의 vision 이나 사람에게 묻는다). GPU 를 쓴다.")
    def piper_vision_start(cams: dict[str, str], model: str | None = None, conf: float | None = None) -> dict:
        return run(core.vision_start, cams, model, conf)

    @server.tool(name="piper_vision_stop", annotations=STOP, description="검출기를 끈다.")
    def piper_vision_stop() -> dict:
        return run(core.vision_stop)

    @server.tool(name="piper_estop", annotations=STOP,
                 description="지금 멈춘다 — 긍급 정지. 돌고 있는 추론·녹화·플러그인을 즉시 세운다. 위험하면 묻지 말고 누른다.")
    def piper_estop() -> dict:
        return run(core.estop)

    # ── 리소스 — 읽기 전용 GET 을 "읽기" 로 붙인다 (agent-cli.md §4-4) ──

    def read(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except GatewayError as e:
            raise ResourceError(str(e)) from e

    def resource(uri: str, fn, *, name: str, description: str, mime: str = "application/json") -> None:
        """`{+var}` 템플릿을 모르는 SDK 면 `{var}` 로 물러난다 — `/` 가 든 id 는 %2F 로 넣어야 한다."""
        try:
            server.resource(uri, name=name, description=description, mime_type=mime)(fn)
        except (ValueError, TypeError):
            server.resource(uri.replace("{+", "{"), name=name, description=description, mime_type=mime)(fn)

    def res_context() -> str:
        return read(core.context)

    def res_status() -> dict:
        return read(core.status)

    def res_missions() -> dict:
        return read(core.missions)

    def res_plugins() -> dict:
        return read(core.plugins)

    def res_datasets() -> dict:
        return read(core.datasets)

    def res_eval() -> dict:
        return read(core.eval_stats)

    def res_training() -> dict:
        return read(core.training)

    def res_episode(dataset_id: str, episode: str) -> dict:
        return read(core.episode, dataset_id, int(episode))

    resource("piper://context", res_context, name="context", mime="text/markdown",
             description="진단 꾸러미 — 버전·유닛·활동·E-stop·장치·경고 이상 저널(최근 30분)·미션 저널·읽을 문서")
    resource("piper://status", res_status, name="status", description="종합 상태 한 장 + 이 서버의 heartbeat 상태")
    resource("piper://missions", res_missions, name="missions", description="미션 이력 — 저널 목록, 최신 먼저")
    resource("piper://plugins", res_plugins, name="plugins", description="설치된 플러그인과 상태 (mission=true 가 미션 타입)")
    resource("piper://datasets", res_datasets, name="datasets",
             description="로컬 데이터셋 목록 — 에피소드·프레임·fps·카메라·구운 사본·노트")
    resource("piper://eval/stats", res_eval, name="eval_stats", description="평가 통계 — 성공률·프리셋/체크포인트/장면별")
    resource("piper://training", res_training, name="training", description="학습 상태·진행률·러너 + job 목록")
    resource(EPISODE_TEMPLATE, res_episode, name="episode",
             description="에피소드 요약 — 길이·task·페이즈 구간·노트. 예: piper://datasets/user/apple/episodes/3")

    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="piper-mcp",
        description="PIPER 외부 제어 API 위의 MCP 서버 (stdio). 에이전트가 도는 기계에서 띄우고 로봇과는 REST 로 말한다.")
    p.add_argument("--api", default=os.environ.get("PIPER_API_BASE") or "http://127.0.0.1:8000",
                   help="게이트웨이 주소 (기본 PIPER_API_BASE 또는 http://127.0.0.1:8000)")
    p.add_argument("--token", default=os.environ.get("PIPER_API_TOKEN", ""),
                   help="외부 API 토큰 (기본 환경변수 PIPER_API_TOKEN) — 게이트웨이 backend/.env 의 값")
    p.add_argument("--heartbeat-s", type=float, default=HEARTBEAT_S, help="박동 주기(초). external-api 권장 0.5~1")
    p.add_argument("--poll-s", type=float, default=POLL_S, help="미션이 끝났나 보는 주기(초)")
    p.add_argument("--log-level", default="INFO")
    a = p.parse_args(argv)
    # ⚠ stdout 은 프로토콜 채널 — 로그는 stderr 로만
    logging.basicConfig(level=getattr(logging, a.log_level.upper(), logging.INFO), stream=sys.stderr,
                        format="%(asctime)s [%(levelname)s] piper-mcp: %(message)s")
    if not a.token:
        logger.warning("PIPER_API_TOKEN 이 비어 있다 — 외부 API 는 401/503 으로 거절한다")
    core = PiperMcp(Api(a.api, a.token), heartbeat_s=a.heartbeat_s, poll_s=a.poll_s)
    server = build_server(core)
    logger.info("stdio 서버 시작 — 게이트웨이 %s, heartbeat %.2fs", a.api, a.heartbeat_s)
    try:
        server.run("stdio")
    finally:
        core.close()
        logger.info("종료 — heartbeat 끊김")
    return 0


if __name__ == "__main__":
    sys.exit(main())
