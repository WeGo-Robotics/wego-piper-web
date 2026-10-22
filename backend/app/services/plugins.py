"""플러그인 — 사용자 파이썬 스크립트를 폴더 하나로 얹어 웹에서 켜고 끈다 (feature/plugins.md).

여기가 하는 일 넷:

1. **레지스트리** — 플러그인 폴더(`plugin.yaml` + `main.py`)를 읽고 매니페스트를 검증한다.
   사용자 폴더(`settings.plugins_dir`)와 저장소 번들(`<repo>/plugins/`) 둘을 본다.
2. **소유자** — 플러그인마다 `make_process("piper-plugin-<name>")`. 검출기·정책 서버와 같은
   자리라 유닛이면 journald 로그, 못 쓰면 자식 프로세스 폴백.
3. **실행 토큰** — 시작할 때 무작위 토큰을 만들어 환경으로 넘기고, 매니페스트의
   `permissions` 범위 안에서만 `/api/*` 를 통과시킨다 (`core/authgate.py` 가 여기에 묻는다).
   끝나면 토큰도 죽는다. 파일에 적지 않는다 — 재부착이 없으므로(§3-9).
4. **E-stop** — 도는 플러그인의 PID 를 활동 `plugin:<name>` 으로 버스에 올린다. estopd 는
   키를 가리지 않고 전부 죽이므로 데몬 수정이 없다 (§3-7).

⚠ 게이트웨이가 재시작되면 플러그인은 **수동으로 다시 시작**한다. 기동 시 남아 있는
`piper-plugin-*` 유닛은 정지시킨다 — 토큰이 죽은 채 도는 고아를 두지 않는다.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from app.core import plugin_scopes
from app.core.config import settings
from app.services.process_manager import ProcessState

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]
BUNDLED_DIR = REPO / "plugins"
UNIT_PREFIX = "piper-plugin-"


# ── 매니페스트 — 모양은 SDK 가 정본이다 (`piper_plugin.manifest`, `piper-plugin check` 와 같은 모델) ──

from piper_plugin.manifest import (  # noqa: E402  (재노출 — 라우터·테스트가 여기서 가져간다)
    MANIFEST, NAME_RE, CameraSlot, ConfigField, Manifest, ManifestError, Outputs, Resources, load_manifest,
)


@dataclass
class PluginInfo:
    name: str
    dir: Path
    bundled: bool
    manifest: Manifest | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "dir": str(self.dir), "bundled": self.bundled, "error": self.error}
        if self.manifest:
            d["manifest"] = self.manifest.model_dump()
            d["scopes"] = plugin_scopes.describe(self.manifest.permissions)
        return d


def read_manifest(folder: Path) -> PluginInfo:
    """폴더 하나 → PluginInfo. 깨진 매니페스트는 **줄 번호와 함께** 사유를 남긴다.

    구조는 SDK 가 보고, **권한 범위 이름은 여기서만** 본다 — 표(`core/plugin_scopes.py`)는
    게이트웨이 것이다.
    """
    bundled = folder.parent == BUNDLED_DIR
    info = PluginInfo(name=folder.name, dir=folder, bundled=bundled)
    try:
        m = load_manifest(folder)
    except ManifestError as e:
        info.error = str(e)
        return info
    bad = [sc for sc in m.permissions if not plugin_scopes.known(sc)]
    if bad:
        info.error = f"모르는 권한 범위: {bad} (core/plugin_scopes.py 의 표)"
        return info
    info.manifest = m
    return info


# ── 실행 ──────────────────────────────────────────────────────────────────────


@dataclass
class Grant:
    """실행 토큰 하나의 권한. 프로세스와 수명이 같다."""
    name: str
    scopes: frozenset[str]
    issued_at: float = field(default_factory=time.time)


@dataclass
class Run:
    token: str
    config: dict
    cameras: dict[str, str]
    gpu: bool
    run_id: str = ""
    mission: bool = False
    started_at: float = field(default_factory=time.time)


def _idle(state: ProcessState) -> bool:
    return state in (ProcessState.IDLE, ProcessState.ERROR)


class PluginRegistry:
    def __init__(self) -> None:
        self._owners: dict[str, object] = {}
        self._grants: dict[str, Grant] = {}
        self._runs: dict[str, Run] = {}
        # 이 세션에서 한 번이라도 띄운 이름 — 멈춘 뒤 버스의 활동 PID 를 지우려면 알아야 한다
        self._seen: set[str] = set()

    # ── 디렉토리 ──

    def dirs(self) -> list[tuple[Path, bool]]:
        """(폴더, 번들인가). 사용자 폴더가 먼저라 같은 이름이면 사용자 것이 이긴다."""
        return [(settings.plugins_dir, False), (BUNDLED_DIR, True)]

    def runtime_dir(self, name: str) -> Path:
        d = settings.plugins_dir / ".runtime" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def scan(self) -> list[PluginInfo]:
        out: dict[str, PluginInfo] = {}
        for root, _bundled in self.dirs():
            if not root.is_dir():
                continue
            for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
                if folder.name in out:
                    continue
                out[folder.name] = read_manifest(folder)
        return list(out.values())

    def get(self, name: str) -> PluginInfo:
        for info in self.scan():
            if info.name == name:
                return info
        raise HTTPException(404, f"플러그인이 없습니다: {name}")

    # ── 소유자 ──

    def owner(self, name: str):
        pm = self._owners.get(name)
        if pm is None:
            from app.services.systemd_process import make_process

            pm = make_process(f"{UNIT_PREFIX}{name}")
            pm.set_state_callback(lambda st, n=name: self._on_state(n, st))
            self._owners[name] = pm
        return pm

    def _on_state(self, name: str, state: ProcessState) -> None:
        if _idle(state):
            run = self._runs.pop(name, None)
            if run:
                self._grants.pop(run.token, None)
        try:
            from app.services.estop_bridge import estop_bridge

            estop_bridge.sync_activities()
        except Exception as e:   # 상태 콜백에서 죽으면 소유자가 멈춘다
            logger.debug("플러그인 활동 동기화 실패: %s", e)

    def state(self, name: str) -> ProcessState:
        pm = self._owners.get(name)
        return pm.state if pm else ProcessState.IDLE

    def is_running(self, name: str) -> bool:
        return not _idle(self.state(name))

    def running(self) -> dict[str, int | None]:
        return {n: pm.pid for n, pm in self._owners.items() if not _idle(pm.state)}

    def any_running(self, gpu_only: bool = False) -> bool:
        for n in self.running():
            run = self._runs.get(n)
            if not gpu_only or (run and run.gpu):
                return True
        return False

    # ── 설정 저장 ──

    def saved_config(self, name: str) -> dict:
        p = self.runtime_dir(name) / "config.json"
        if not p.is_file():
            return {"config": {}, "cameras": {}}
        try:
            d = json.loads(p.read_text())
            return {"config": d.get("config", {}), "cameras": d.get("cameras", {})}
        except (OSError, ValueError):
            return {"config": {}, "cameras": {}}

    def save_config(self, name: str, config: dict, cameras: dict[str, str]) -> None:
        (self.runtime_dir(name) / "config.json").write_text(
            json.dumps({"config": config, "cameras": cameras}, ensure_ascii=False, indent=2))

    # ── 시작·정지 ──

    def _python_for(self, m: Manifest, name: str) -> str:
        if m.python == "system":
            return settings.grpc_python
        venv_py = self.runtime_dir(name) / "venv" / "bin" / "python"
        if venv_py.is_file():
            return str(venv_py)
        raise HTTPException(400, f"`python: {m.python}` 은 의존 설치가 먼저입니다 (아직 지원하지 않는다 — 6단계)")

    def _check_cameras(self, m: Manifest, cameras: dict[str, str]) -> None:
        from piper_shm import list_segments

        live = set(list_segments())
        for slot in m.cameras:
            stream = cameras.get(slot.key)
            if not stream:
                if slot.required:
                    raise HTTPException(400, f"카메라 '{slot.label or slot.key}' 를 고르지 않았습니다")
                continue
            if stream not in live:
                raise HTTPException(409, f"카메라 '{stream}' 이 연결돼 있지 않습니다")

    async def start(self, name: str, config: dict | None = None,
                    cameras: dict[str, str] | None = None) -> dict:
        info = self.get(name)
        if info.manifest is None:
            raise HTTPException(400, f"매니페스트가 깨져 있습니다: {info.error}")
        m = info.manifest
        if self.is_running(name):
            raise HTTPException(409, f"플러그인 '{name}' 은 이미 돌고 있습니다")

        from app.services import exclusivity

        if m.resources.gpu:
            exclusivity.require_idle(exclusivity.Activity.PLUGIN_GPU)

        config = {**m.defaults(), **(config or {})}
        cameras = dict(cameras or {})
        self._check_cameras(m, cameras)
        python = self._python_for(m, name)
        self.save_config(name, config, cameras)

        token = secrets.token_urlsafe(32)
        # 저널 id 는 게이트웨이가 정한다 — 외부 API 가 미션을 이 id 로 조회하므로 시작 응답에 실려야 한다
        run_id = time.strftime("run_%Y%m%d_%H%M%S")
        env = {
            "PIPER_PLUGIN_NAME": name,
            "PIPER_PLUGIN_DIR": str(info.dir),
            "PIPER_PLUGIN_RUNTIME_DIR": str(self.runtime_dir(name)),
            "PIPER_PLUGIN_CONFIG": json.dumps(config, ensure_ascii=False),
            "PIPER_PLUGIN_CAMERAS": json.dumps(cameras),
            "PIPER_PLUGIN_HEARTBEAT": "1" if m.resources.heartbeat else "0",
            "PIPER_API_BASE": f"http://127.0.0.1:{settings.port}",
            "PIPER_API_TOKEN": token,
            "PIPER_PLUGIN_RUN_ID": run_id,
        }
        cmd = [python, "-u", str(info.dir / m.entry)]

        pm = self.owner(name)
        self._grants[token] = Grant(name=name, scopes=frozenset(m.permissions))
        self._runs[name] = Run(token=token, config=config, cameras=cameras, gpu=m.resources.gpu,
                               run_id=run_id, mission=m.mission)
        self._seen.add(name)
        try:
            await pm.start(cmd, env_extra=env)
        except Exception as e:
            self._grants.pop(token, None)
            self._runs.pop(name, None)
            raise HTTPException(500, f"플러그인 시작 실패: {e}")
        self._on_state(name, pm.state)
        return self.status(name)

    async def stop(self, name: str) -> dict:
        pm = self._owners.get(name)
        if pm is None or _idle(pm.state):
            return self.status(name)
        await pm.stop()
        self._on_state(name, pm.state)
        return self.status(name)

    async def kill_all(self) -> None:
        """E-stop — 도는 플러그인 전부 즉시 kill."""
        for name, pm in list(self._owners.items()):
            if _idle(pm.state):
                continue
            try:
                await pm.kill()
            except Exception as e:
                logger.error("E-stop: 플러그인 %s kill 실패 — %s", name, e)
            self._on_state(name, pm.state)

    # ── 조회 ──

    def command(self, name: str, command: str, args: dict | None = None) -> None:
        """화면·ext API 의 버튼 → 플러그인 `on_command`. 매니페스트 `commands` 에 있는 것만."""
        info = self.get(name)
        if info.manifest is None or command not in info.manifest.commands:
            raise HTTPException(400, f"플러그인 '{name}' 에 그런 명령이 없습니다: {command}")
        if not self.is_running(name):
            raise HTTPException(409, f"플러그인 '{name}' 이 돌고 있지 않습니다")
        from piper_bus.client import Bus

        Bus().push_plugin_command(name, {"name": command, "args": args or {}})

    def grant_for(self, token: str) -> Grant | None:
        return self._grants.get(token)

    def estop_pids(self) -> dict[str, int | None]:
        """버스 활동 키 → PID. 멈춘 플러그인은 None 으로 **지운다**."""
        running = self.running()
        return {f"plugin:{n}": running.get(n) for n in self._seen | set(running)}

    def status(self, name: str) -> dict:
        pm = self._owners.get(name)
        state = pm.state if pm else ProcessState.IDLE
        run = self._runs.get(name)
        out: dict = {"name": name, "state": state.value, "pid": pm.pid if pm and not _idle(state) else None,
                     "started_at": run.started_at if run else None, "run_id": run.run_id if run else None,
                     "config": run.config if run else None, "cameras": run.cameras if run else None}
        if pm is not None and _idle(state):
            out["log_tail"] = pm.recent_log(20)
        # 플러그인이 올린 상태 KV(`phase`·`episode`·…). 이름이 `live` 가 아닌 이유: 외부 API 의
        # 미션 응답이 `live`(돌고 있나) 를 따로 쓴다 — 같은 키가 덮어쓰는 사고가 실제로 났다
        try:
            from piper_bus.client import Bus

            out["kv"] = Bus().get_plugin_state(name)
        except Exception:
            out["kv"] = None
        return out

    # ── 저널 (미션 회차 기록, feature/plugins.md §12-1) ──

    def runs(self, name: str | None = None) -> list[dict]:
        """저널 목록, 최신 먼저. `name` 이 없으면 모든 플러그인."""
        root = settings.plugins_dir / ".runtime"
        out = []
        if not root.is_dir():
            return out
        for d in sorted(root.iterdir()):
            if name and d.name != name:
                continue
            for p in (d / "runs").glob("run_*.jsonl") if (d / "runs").is_dir() else []:
                st = p.stat()
                out.append({"id": p.stem, "plugin": d.name, "bytes": st.st_size, "mtime": st.st_mtime,
                            "live": self._live_run(d.name) == p.stem})
        out.sort(key=lambda r: r["id"], reverse=True)
        return out

    def _live_run(self, name: str) -> str | None:
        run = self._runs.get(name)
        return run.run_id if run and self.is_running(name) else None

    def find_live_run(self, run_id: str) -> str | None:
        """이 저널 id 로 **지금 도는** 플러그인 이름 — 없으면 None."""
        for name in list(self._runs):
            if self._live_run(name) == run_id:
                return name
        return None

    def run_events(self, run_id: str, name: str | None = None) -> tuple[str, list[dict]] | None:
        """끝난 저널의 줄들. `(플러그인, 기록들)` — 강제 종료로 잘린 마지막 줄은 버린다."""
        for r in self.runs(name):
            if r["id"] != run_id:
                continue
            path = settings.plugins_dir / ".runtime" / r["plugin"] / "runs" / f"{run_id}.jsonl"
            events = []
            for line in path.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            return r["plugin"], events
        return None

    def live_mission(self) -> str | None:
        """도는 미션 플러그인(`mission: true`) 이름 — 동시 1개(외부 API 계약)."""
        for name, run in self._runs.items():
            if run.mission and self.is_running(name):
                return name
        return None

    # ── 설치·삭제 ──

    def install_from_path(self, src: Path) -> PluginInfo:
        src = src.expanduser().resolve()
        if not src.is_dir():
            raise HTTPException(400, f"폴더가 아닙니다: {src}")
        probe = read_manifest(src)
        if probe.manifest is None:
            raise HTTPException(400, probe.error or "매니페스트를 읽을 수 없습니다")
        dst = settings.plugins_dir / probe.manifest.name
        if dst.exists():
            raise HTTPException(409, f"이미 설치돼 있습니다: {probe.manifest.name}")
        settings.plugins_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", ".runtime"))
        return read_manifest(dst)

    def remove(self, name: str) -> None:
        info = self.get(name)
        if info.bundled:
            raise HTTPException(400, "번들 플러그인은 지울 수 없습니다")
        if self.is_running(name):
            raise HTTPException(409, "도는 중에는 지울 수 없습니다 — 먼저 정지하세요")
        shutil.rmtree(info.dir)
        shutil.rmtree(settings.plugins_dir / ".runtime" / name, ignore_errors=True)
        self._owners.pop(name, None)

    # ── 기동 시 고아 정리 ──

    def cleanup_orphans(self) -> list[str]:
        """게이트웨이가 다시 뜨면 토큰이 없다 — 남은 `piper-plugin-*` 유닛은 **정지**한다 (§3-9).

        우리 접두사만 본다. 다른 유닛(학습·정책 서버·데몬)은 이름을 알아도 건드리지 않는다.
        """
        if settings.process_runner != "systemd" or not shutil.which("systemctl"):
            return []
        try:
            out = subprocess.run(["systemctl", "--user", "list-units", "--plain", "--no-legend",
                                  "--all", f"{UNIT_PREFIX}*"],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception as e:
            logger.warning("플러그인 유닛 목록 실패: %s", e)
            return []
        stopped = []
        for line in out.splitlines():
            unit = line.split()[0] if line.split() else ""
            if not unit.startswith(UNIT_PREFIX):
                continue
            try:
                subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=30)
                stopped.append(unit)
            except Exception as e:
                logger.warning("고아 플러그인 유닛 정지 실패 (%s): %s", unit, e)
        if stopped:
            logger.warning("게이트웨이 재시작 — 남아 있던 플러그인을 정지했습니다 (수동 재시작): %s",
                           ", ".join(stopped))
        return stopped


registry = PluginRegistry()
