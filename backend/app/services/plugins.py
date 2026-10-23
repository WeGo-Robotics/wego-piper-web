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

import io
import json
import logging
import secrets
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections import deque
from collections.abc import Callable
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

# ── 인스턴스 (feature/plugins.md §7-4) ──
#
# 인스턴스 id 는 `<폴더 이름>` (기본 인스턴스) 또는 `<폴더 이름>:<라벨>`. 소유자·토큰·실행 기록·런타임
# 디렉토리·버스 키·유닛 이름이 전부 이 id 를 쓴다 — 기본 인스턴스의 id 가 폴더 이름 그대로라
# 인스턴스를 모르던 호출자(호환층·외부 API)는 그대로 돈다. systemd 유닛 이름에 `:` 는 허용 문자다.
import re as _re

INSTANCE_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")


def split_instance(iid: str) -> tuple[str, str]:
    """`detector:cam2` → (`detector`, `cam2`), `detector` → (`detector`, ``)."""
    base, _, label = iid.partition(":")
    return base, label


def instance_id(base: str, label: str) -> str:
    return f"{base}:{label}" if label else base


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
    inputs: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)


def _idle(state: ProcessState) -> bool:
    return state in (ProcessState.IDLE, ProcessState.ERROR)


class PluginRegistry:
    def __init__(self) -> None:
        self._owners: dict[str, object] = {}
        self._deps_owners: dict[str, object] = {}
        self._grants: dict[str, Grant] = {}
        self._runs: dict[str, Run] = {}
        # 이 세션에서 한 번이라도 띄운 이름 — 멈춘 뒤 버스의 활동 PID 를 지우려면 알아야 한다
        self._seen: set[str] = set()
        # 화면(WS)으로 흘릴 로그·상태 — ws.py 가 꽂는다. 도는 동안의 로그 꼬리도 들고 있다
        self._log_cb: Callable[[str, str], None] | None = None
        self._state_cb: Callable[[str, ProcessState], None] | None = None
        self._logs: dict[str, deque[str]] = {}
        # 시작 중인 플러그인 — 그 사이의 `idle` 전이는 소유자의 상태 조회가 튄 것이다 (아래 `_on_state`)
        self._starting: set[str] = set()

    # ── 디렉토리 ──

    def dirs(self) -> list[tuple[Path, bool]]:
        """(폴더, 번들인가). 사용자 폴더가 먼저라 같은 이름이면 사용자 것이 이긴다."""
        return [(settings.plugins_dir, False), (BUNDLED_DIR, True)]

    def runtime_dir(self, name: str) -> Path:
        d = settings.plugins_dir / ".runtime" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def ui_socket(self, name: str) -> Path:
        """`ui: server` 플러그인이 HTTP 서버를 띄우는 유닉스 소켓 (feature/plugins.md §8-2)."""
        return self.runtime_dir(name) / "ui.sock"

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
        """플러그인 정보 — 인스턴스 id(`detector:cam2`)를 줘도 그 폴더의 정보다."""
        base, _ = split_instance(name)
        for info in self.scan():
            if info.name == base:
                return info
        raise HTTPException(404, f"플러그인이 없습니다: {base}")

    def instances(self, base: str) -> list[str]:
        """이 폴더의 인스턴스 id 들 — 기본 인스턴스가 먼저, 그 뒤 라벨 순. 저장 설정·런타임 디렉토리·소유자에서 모은다."""
        labels: set[str] = set()
        root = settings.plugins_dir / ".runtime"
        if root.is_dir():
            for d in root.iterdir():
                b, label = split_instance(d.name)
                if b == base and label:
                    labels.add(label)
        for iid in list(self._owners) + list(self._runs):
            b, label = split_instance(iid)
            if b == base and label:
                labels.add(label)
        return [base] + [instance_id(base, l) for l in sorted(labels)]

    def create_instance(self, base: str, label: str) -> dict:
        info = self.get(base)
        if info.manifest is None:
            raise HTTPException(400, f"매니페스트가 깨져 있습니다: {info.error}")
        if not info.manifest.multi_instance:
            raise HTTPException(400, f"'{base}' 은 인스턴스를 여럿 띄울 수 없습니다 (plugin.yaml 의 multi_instance: true)")
        if not INSTANCE_RE.match(label or ""):
            raise HTTPException(400, "인스턴스 라벨은 영문자·숫자·_·- 만, 첫 글자는 영숫자, 1~20자")
        iid = instance_id(base, label)
        if iid in self.instances(base):
            raise HTTPException(409, f"이미 있는 인스턴스입니다: {iid}")
        saved = self.saved_config(base)             # 기본 인스턴스의 설정을 출발점으로
        self.save_config(iid, saved["config"], saved["cameras"], saved.get("inputs"))
        return self.status(iid)

    # ── 소유자 ──

    def owner(self, name: str):
        pm = self._owners.get(name)
        if pm is None:
            from app.services.systemd_process import make_process

            pm = make_process(f"{UNIT_PREFIX}{name}")
            pm.set_state_callback(lambda st, n=name: self._on_state(n, st))
            pm.set_log_callback(lambda line, n=name: self._on_log(n, line))
            self._owners[name] = pm
        return pm

    def set_log_callback(self, cb: Callable[[str, str], None] | None) -> None:
        self._log_cb = cb

    def set_state_callback(self, cb: Callable[[str, ProcessState], None] | None) -> None:
        self._state_cb = cb

    def _on_log(self, name: str, line: str) -> None:
        self._logs.setdefault(name, deque(maxlen=300)).append(line)
        if self._log_cb:
            try:
                self._log_cb(name, line)
            except Exception as e:
                logger.debug("플러그인 로그 콜백 실패: %s", e)

    def recent_logs(self, name: str, n: int = 50) -> list[str]:
        return list(self._logs.get(name, ()))[-n:]

    def _on_state(self, name: str, state: ProcessState) -> None:
        # ⚠ 유닛 소유자(`SystemdProcess`)는 상태를 **읽을 때마다 systemd 에 묻고**, 유닛이 뜨는
        #   사이에 묻히면 `inactive` 를 보고 idle 콜백을 쏜다. 그걸 그대로 믿고 실행 기록·토큰을
        #   지웠더니 **플러그인이 뜨자마자 토큰이 죽어 첫 호출부터 401** 이었다(실기 2026-10-22).
        #   시작이 끝나기 전의 idle 은 무시하고, 끝난 뒤의 idle 만 "멈췄다" 로 읽는다.
        if _idle(state) and name in self._starting:
            logger.debug("플러그인 %s 시작 중 idle 전이 무시 (%s)", name, state.value)
            return
        if _idle(state):
            run = self._runs.pop(name, None)
            if run:
                self._grants.pop(run.token, None)
        if self._state_cb:
            try:
                self._state_cb(name, state)
            except Exception as e:
                logger.debug("플러그인 상태 콜백 실패: %s", e)
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
        empty = {"config": {}, "cameras": {}, "inputs": {}}
        if not p.is_file():
            return empty
        try:
            d = json.loads(p.read_text())
            return {"config": d.get("config", {}), "cameras": d.get("cameras", {}), "inputs": d.get("inputs", {})}
        except (OSError, ValueError):
            return empty

    def save_config(self, name: str, config: dict, cameras: dict[str, str], inputs: dict[str, str] | None = None) -> None:
        (self.runtime_dir(name) / "config.json").write_text(
            json.dumps({"config": config, "cameras": cameras, "inputs": inputs or {}}, ensure_ascii=False, indent=2))

    # ── 플러그인 간 입출력 (feature/plugins.md §7-3) ──

    def sources(self) -> dict:
        """지금 살아 있는 소스 — 검출 이름(발행자 포함)과 토픽. 입력 슬롯 선택기의 재료."""
        try:
            from piper_bus.client import Bus

            bus = Bus()
            dets = {}
            for n in bus.detection_names():
                d = bus.get_detections(n) or {}
                dets[n] = {"publisher": d.get("publisher"), "ts": d.get("ts")}
            topics = {}
            for n in bus.topic_names():
                tp = bus.get_topic(n) or {}
                topics[n] = {"publisher": tp.get("publisher"), "ts": tp.get("ts"), "seq": tp.get("seq")}
            return {"detections": dets, "topics": topics}
        except Exception:
            return {"detections": {}, "topics": {}}

    def resolve_inputs(self, m: Manifest, inputs: dict[str, str] | None) -> dict[str, str]:
        """고른 소스 위에 매니페스트 기본 `source` 를 채운다."""
        out = {}
        for slot in m.inputs:
            v = (inputs or {}).get(slot.key) or slot.source
            if v:
                out[slot.key] = v
        return out

    def inputs_live(self, m: Manifest, inputs: dict[str, str]) -> dict[str, bool]:
        """입력마다 소스가 지금 살아 있나 — 막지 않고 **말만** 한다(§3-8 과 같은 태도)."""
        src = self.sources()
        out = {}
        for slot in m.inputs:
            v = inputs.get(slot.key)
            pool = src["detections"] if slot.kind == "detections" else src["topics"]
            out[slot.key] = bool(v) and v in pool
        return out

    # ── 시작·정지 ──

    # ── 파이썬 환경 (`python: venv | venv-isolated`, feature/plugins.md §6) ──

    def venv_dir(self, name: str) -> Path:
        # venv 는 **폴더(코드)에 딸린 것**이라 인스턴스가 아니라 기본 이름 아래 하나다
        return self.runtime_dir(split_instance(name)[0]) / "venv"

    def deps(self, name: str, m: Manifest | None = None) -> dict:
        """의존 상태 — 화면의 [의존 설치] 버튼 재료."""
        info = self.get(name) if m is None else None
        m = m or (info.manifest if info else None)
        venv = self.venv_dir(name)
        req = None
        try:
            d = self.get(name).dir
            req = (d / "requirements.txt").is_file()
        except HTTPException:
            pass
        pm = self._deps_owners.get(split_instance(name)[0])
        return {
            "python": m.python if m else "system",
            "venv_ready": (venv / ".deps-ok").is_file() and (venv / "bin" / "python").is_file(),
            "requirements": bool(req),
            "installing": bool(pm and not _idle(pm.state)),
            "log_tail": self.recent_logs(f"{split_instance(name)[0]}#deps", 30),
        }

    def _python_for(self, m: Manifest, name: str) -> str:
        if m.python == "system":
            return settings.grpc_python
        venv_py = self.venv_dir(name) / "bin" / "python"
        if venv_py.is_file() and (self.venv_dir(name) / ".deps-ok").is_file():
            return str(venv_py)
        raise HTTPException(409, f"`python: {m.python}` 은 의존 설치가 먼저입니다 — 상세의 [의존 설치]")

    async def install_deps(self, name: str) -> dict:
        """venv 를 만들고 `requirements.txt` 를 깐다 — 별도 프로세스(유닛)로, 로그는 플러그인 로그 채널로 흐른다.

        `venv` 는 `--system-site-packages`(torch·transformers 는 상속, 고유 의존만 추가),
        `venv-isolated` 는 상속 없음(게이트웨이의 것과 충돌하는 의존 — 수 GB 와 시간을 먹는다).
        끝에 `.deps-ok` 표지를 쓴다 — 중간에 죽은 venv 를 다 된 것으로 읽지 않는다.
        """
        name = split_instance(name)[0]
        info = self.get(name)
        if info.manifest is None:
            raise HTTPException(400, f"매니페스트가 깨져 있습니다: {info.error}")
        m = info.manifest
        if m.python == "system":
            raise HTTPException(400, "`python: system` 은 의존을 따로 깔지 않습니다 (게이트웨이 파이썬을 쓴다)")
        if any(self.is_running(i) for i in self.instances(name)):
            raise HTTPException(409, "도는 중에는 의존을 바꿀 수 없습니다 — 먼저 정지하세요")
        pm = self._deps_owner(name)
        if not _idle(pm.state):
            raise HTTPException(409, "이미 설치 중입니다")
        venv = self.venv_dir(name)
        (venv / ".deps-ok").unlink(missing_ok=True)
        req = info.dir / "requirements.txt"
        flag = "" if m.python == "venv-isolated" else "--system-site-packages"
        steps = [f"{settings.grpc_python} -m venv {flag} {venv}", f"{venv}/bin/pip install -q -U pip"]
        if req.is_file():
            steps.append(f"{venv}/bin/pip install -r {req}")
        else:
            steps.append("echo 'no requirements.txt - venv only'")   # 셸 로그 — 화면 문구가 아니다
        steps.append(f"touch {venv}/.deps-ok && echo 'deps installed'")
        await pm.start(["bash", "-c", " && ".join(steps)])
        return self.deps(name, m)

    def _deps_owner(self, name: str):
        pm = self._deps_owners.get(name)
        if pm is None:
            from app.services.systemd_process import make_process

            pm = make_process(f"{UNIT_PREFIX}deps-{name}")
            # 설치 로그는 두 채널로 — 의존 패널의 꼬리(`<name>#deps`)와 플러그인 로그 탭(WS)
            pm.set_log_callback(lambda line, n=name: (self._on_log(f"{n}#deps", line), self._on_log(n, f"[deps] {line}")))
            self._deps_owners[name] = pm
        return pm

    def remove_deps(self, name: str) -> None:
        name = split_instance(name)[0]
        if any(self.is_running(i) for i in self.instances(name)):
            raise HTTPException(409, "도는 중에는 의존을 지울 수 없습니다")
        shutil.rmtree(self.venv_dir(name), ignore_errors=True)

    def _limits(self, m: Manifest) -> dict[str, str]:
        props = {}
        if m.resources.memory:
            props["MemoryMax"] = m.resources.memory
        if m.resources.cpu:
            props["CPUQuota"] = m.resources.cpu
        return props

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
                    cameras: dict[str, str] | None = None, inputs: dict[str, str] | None = None) -> dict:
        base, label = split_instance(name)
        info = self.get(base)
        if info.manifest is None:
            raise HTTPException(400, f"매니페스트가 깨져 있습니다: {info.error}")
        m = info.manifest
        if label and not m.multi_instance:
            raise HTTPException(400, f"'{base}' 은 인스턴스를 여럿 띄울 수 없습니다 (plugin.yaml 의 multi_instance: true)")
        if label and not INSTANCE_RE.match(label):
            raise HTTPException(400, "인스턴스 라벨은 영문자·숫자·_·- 만, 첫 글자는 영숫자, 1~20자")
        if self.is_running(name):
            raise HTTPException(409, f"플러그인 '{name}' 은 이미 돌고 있습니다")

        from app.services import exclusivity

        if m.resources.gpu:
            # ⚠ `require_idle(PLUGIN_GPU)` 를 쓰면 **GPU 플러그인이 하나 돌고 있다는 것만으로** 둘째가
            #   막힌다(활동이 자기 자신에게 막히는 규칙). GPU 플러그인끼리는 경고만 하기로 했다(§3-8) —
            #   막는 것은 학습처럼 **다른** 활동이 쥐고 있을 때뿐이다.
            blockers = [a for a in exclusivity.blocking(exclusivity.Activity.PLUGIN_GPU)
                        if a is not exclusivity.Activity.PLUGIN_GPU]
            if blockers:
                names = ", ".join(exclusivity.LABELS[a] for a in blockers)
                raise HTTPException(409, f"{names} 중에는 GPU 플러그인을 시작할 수 없습니다")
            if self.any_running(gpu_only=True):
                logger.warning("GPU 플러그인이 이미 돌고 있는데 %s 를 더 켠다 — 메모리를 다툴 수 있다 (막지 않는다)", name)

        config = {**m.defaults(), **(config or {})}
        cameras = dict(cameras or {})
        self._check_cameras(m, cameras)
        inputs = self.resolve_inputs(m, inputs)
        missing = [s.key for s in m.inputs if s.required and not inputs.get(s.key)]
        if missing:
            raise HTTPException(400, f"입력을 고르지 않았습니다: {missing}")
        python = self._python_for(m, base)
        self.save_config(name, config, cameras, inputs)

        token = secrets.token_urlsafe(32)
        # 저널 id 는 게이트웨이가 정한다 — 외부 API 가 미션을 이 id 로 조회하므로 시작 응답에 실려야 한다
        run_id = time.strftime("run_%Y%m%d_%H%M%S")
        env = {
            "PIPER_PLUGIN_NAME": name,                     # 인스턴스 id — SDK 의 self.name, 버스 키
            "PIPER_PLUGIN_BASE_NAME": base,
            "PIPER_PLUGIN_INSTANCE": label,
            "PIPER_PLUGIN_DIR": str(info.dir),
            "PIPER_PLUGIN_RUNTIME_DIR": str(self.runtime_dir(name)),
            "PIPER_PLUGIN_CONFIG": json.dumps(config, ensure_ascii=False),
            "PIPER_PLUGIN_CAMERAS": json.dumps(cameras),
            "PIPER_PLUGIN_INPUTS": json.dumps(inputs),
            "PIPER_PLUGIN_HEARTBEAT": "1" if m.resources.heartbeat else "0",
            "PIPER_API_BASE": f"http://127.0.0.1:{settings.port}",
            "PIPER_API_TOKEN": token,
            "PIPER_PLUGIN_RUN_ID": run_id,
        }
        if m.ui == "server":
            sock = self.ui_socket(name)
            sock.unlink(missing_ok=True)          # 지난 실행이 남긴 소켓 파일 — 있으면 프록시가 "켜져 있다" 고 착각한다
            env["PIPER_PLUGIN_UI_SOCKET"] = str(sock)
        cmd = [python, "-u", str(info.dir / m.entry)]

        pm = self.owner(name)
        # 토큰은 **프로세스가 뜨기 전에** 유효해야 한다 — 첫 호출이 환경을 읽는 즉시 온다
        self._grants[token] = Grant(name=name, scopes=frozenset(m.permissions))
        self._runs[name] = Run(token=token, config=config, cameras=cameras, gpu=m.resources.gpu,
                               run_id=run_id, mission=m.mission, inputs=inputs)
        self._seen.add(name)
        self._starting.add(name)
        limits = self._limits(m)
        try:
            if limits and hasattr(pm, "unit"):           # 유닛 소유자만 자원 제한을 걸 수 있다
                await pm.start(cmd, env_extra=env, properties=limits)
            else:
                if limits:
                    logger.warning("플러그인 %s 의 자원 제한(%s)은 자식 프로세스 폴백에서는 걸리지 않습니다", name, limits)
                await pm.start(cmd, env_extra=env)
        except Exception as e:
            self._grants.pop(token, None)
            self._runs.pop(name, None)
            raise HTTPException(500, f"플러그인 시작 실패: {e}")
        finally:
            self._starting.discard(name)
        if not pm.is_running:
            # 정말 바로 죽었다(entry 가 즉사 등) — 그때는 기록을 지우는 게 맞다
            self._on_state(name, pm.state)
        else:
            try:
                from app.services.estop_bridge import estop_bridge

                estop_bridge.sync_activities()
            except Exception as e:
                logger.debug("플러그인 활동 동기화 실패: %s", e)
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
        base, label = split_instance(name)
        out: dict = {"name": name, "plugin": base, "instance": label or None,
                     "state": state.value, "pid": pm.pid if pm and not _idle(state) else None,
                     "limits_applied": bool(pm is not None and hasattr(pm, "unit")),
                     "started_at": run.started_at if run else None, "run_id": run.run_id if run else None,
                     "config": run.config if run else None, "cameras": run.cameras if run else None,
                     "inputs": run.inputs if run else None}
        # 입력 소스가 살아 있나 — 도는 중이면 지금 쓰는 입력, 아니면 저장된 입력 기준
        try:
            info = self.get(name)
            if info.manifest and info.manifest.inputs:
                chosen = run.inputs if run else self.resolve_inputs(info.manifest, self.saved_config(name).get("inputs"))
                out["inputs_live"] = self.inputs_live(info.manifest, chosen)
        except HTTPException:
            pass
        # 로그 꼬리: 도는 중에는 우리 링버퍼(WS 로 흘린 것과 같은 줄), 멈췄으면 소유자가 쥔 마지막 줄
        # (죽은 이유가 거기 있다 — 유닛은 `--collect` 라 journald 밖에 흔적이 없다)
        if pm is not None and _idle(state):
            out["log_tail"] = pm.recent_log(20) or self.recent_logs(name, 20)
        elif pm is not None:
            out["log_tail"] = self.recent_logs(name, 50)
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
            if name and d.name != name and split_instance(d.name)[0] != name:
                continue
            base, label = split_instance(d.name)
            for p in (d / "runs").glob("run_*.jsonl") if (d / "runs").is_dir() else []:
                st = p.stat()
                out.append({"id": p.stem, "plugin": d.name, "base": base, "instance": label or None,
                            "bytes": st.st_size, "mtime": st.st_mtime, "live": self._live_run(d.name) == p.stem})
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

    def install_from_zip(self, data: bytes) -> PluginInfo:
        """zip 한 장 → 설치. 루트에 `plugin.yaml` 이 있거나, 폴더 하나가 그걸 품고 있어야 한다.

        경로 조작(`../`, 절대 경로)이 든 항목은 거절한다 — 압축 파일은 사용자 입력이다.
        """
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise HTTPException(400, "zip 파일이 아닙니다")
        names = [n for n in zf.namelist() if not n.endswith("/")]
        for n in names:
            parts = Path(n).parts
            if Path(n).is_absolute() or ".." in parts:
                raise HTTPException(400, f"zip 안에 허용되지 않는 경로가 있습니다: {n}")
        tops = {Path(n).parts[0] for n in names}
        with tempfile.TemporaryDirectory(prefix="piper-plugin-zip-") as tmp:
            zf.extractall(tmp)
            root = Path(tmp)
            if (root / MANIFEST).is_file():
                src = root
            elif len(tops) == 1 and (root / next(iter(tops)) / MANIFEST).is_file():
                src = root / next(iter(tops))
            else:
                raise HTTPException(400, f"zip 루트(또는 폴더 하나 안)에 {MANIFEST} 이 있어야 합니다")
            # zip 안 폴더 이름은 아무것이든 될 수 있다 — 매니페스트의 `name` 으로 설치한다.
            # 그래서 폴더 이름 검사(`load_manifest`)를 거치지 않고 이름만 먼저 읽는다
            import yaml

            try:
                raw = yaml.safe_load((src / MANIFEST).read_text(encoding="utf-8")) or {}
                name = Manifest(**raw).name if isinstance(raw, dict) else None
            except Exception as e:
                raise HTTPException(400, f"{MANIFEST}: {e}")
            if not name:
                raise HTTPException(400, f"{MANIFEST} 에 name 이 없습니다")
            # ⚠ 스테이징은 **압축을 푼 곳 바깥**에 둔다 — 평평한 zip 이면 `src` 가 루트라 그 안에
            #   만들면 copytree 가 자기 자신을 끝없이 복사한다(RecursionError, 테스트에서 났다)
            with tempfile.TemporaryDirectory(prefix="piper-plugin-stage-") as stage_root:
                staged = Path(stage_root) / name
                shutil.copytree(src, staged, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", ".runtime"))
                return self.install_from_path(staged)

    def remove(self, name: str) -> None:
        base, label = split_instance(name)
        info = self.get(base)
        if label:
            # 인스턴스만 지운다 — 코드(폴더)는 기본 인스턴스의 것이다
            if self.is_running(name):
                raise HTTPException(409, "도는 중에는 지울 수 없습니다 — 먼저 정지하세요")
            shutil.rmtree(settings.plugins_dir / ".runtime" / name, ignore_errors=True)
            self._owners.pop(name, None)
            return
        if info.bundled:
            raise HTTPException(400, "번들 플러그인은 지울 수 없습니다")
        if any(self.is_running(i) for i in self.instances(base)):
            raise HTTPException(409, "도는 중에는 지울 수 없습니다 — 먼저 정지하세요")
        shutil.rmtree(info.dir)
        for iid in self.instances(base):
            shutil.rmtree(settings.plugins_dir / ".runtime" / iid, ignore_errors=True)
            self._owners.pop(iid, None)

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
