"""서비스 상태와 재시작 — **낡은 코드로 도는 것을 찾아낸다.**

## 왜 만들었나

유닛은 **기동 시점의 코드로 돈다.** 파일을 고쳐도 재시작 전에는 아무 일도
일어나지 않는데, 화면에는 그 사실이 어디에도 안 보인다. 실제로 겪은 것들:

- rsd 가 **이틀 전** 코드로 돌아, 고친 깊이 단위 버그가 그대로 재현됐다
- 게이트웨이가 새 라우트를 모른 채로 떠 있어 404 만 돌려줬다

둘 다 "고쳤는데 왜 안 되지"로 한참을 썼다. 코드가 유닛보다 **새것인지**는
기계가 답할 수 있는 질문이다.

## 어떻게 판정하나

유닛이 뜬 시각과, **그 유닛이 실제로 읽는 소스**의 최신 수정 시각을 견준다.
전부를 보지 않는다 — 프론트엔드를 고쳤다고 robotd 가 낡았다고 하면 경고가
의미를 잃는다.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]

# 이 프로세스가 뜬 시각. **import 시점을 쓴다** — `/proc/<pid>/stat` 의 mtime 은
# 기동 시각이 아니라 계속 갱신되는 값이라 늘 "방금"으로 나온다(실제로 그랬다).
_STARTED = time.time()

# 유닛 → 그 유닛이 읽는 소스. 없는 유닛은 아래 기본값을 쓴다.
#
# ⚠ 넓게 잡으면 경고가 늘 켜져 있어 아무도 안 본다. 좁게 잡으면 진짜를 놓친다.
#   데몬이 실제로 import 하는 패키지만 적는다.
_SOURCES: dict[str, tuple[str, ...]] = {
    "piper-rsd": ("daemons/rsd.py", "rs", "cam", "shm", "bus"),
    "piper-camerad": ("daemons/camerad.py", "cam", "shm", "bus"),
    "piper-robotd": ("daemons/robotd.py", "robot", "shm", "bus"),
    "piper-estopd": ("daemons/estopd.py", "bus"),
    "piper-gateway": ("backend/app", "wrapper", "bus", "shm"),
    # ⚠ **프론트는 판정하지 않는다.** vite dev 로 도는 동안에는 소스를 고치면
    #   그 자리에서 반영된다 — "낡았다"가 성립하지 않는다. 빌드본을 서빙하도록
    #   바꾼다면 그때 `frontend/src` 를 여기 넣어야 한다.
}
_DEFAULT_SOURCES = ("daemons", "bus", "shm")

# 게이트웨이가 읽는 것. 유닛이 아니라 이 프로세스 자신이다.
_GATEWAY_SOURCES = ("backend/app", "wrapper", "bus", "shm")

# 파일 시각과 유닛 기동 시각 사이의 여유. 배포가 파일을 쓰고 유닛을 띄우는 사이에
# 몇 초가 흐르는데, 그걸 "낡았다"로 읽으면 배포 직후마다 거짓 경고가 뜬다.
_GRACE_S = 5.0


def _newest_mtime(rel_paths) -> float:
    """이 경로들 아래에서 가장 최근에 고쳐진 `.py` 의 시각."""
    newest = 0.0
    for rel in rel_paths:
        p = REPO / rel
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
            continue
        if not p.is_dir():
            continue
        for f in p.rglob("*.py"):
            # 캐시는 소스가 아니다 — import 만 해도 갱신돼 늘 낡은 것처럼 보인다
            if "__pycache__" in f.parts:
                continue
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
    return newest


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, timeout=10)


def _show(unit: str, *props: str) -> dict[str, str]:
    out = _systemctl("show", unit, *[f"--property={p}" for p in props]).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


@dataclass
class Unit:
    name: str
    active: bool
    since: float          # epoch. 0 이면 모른다
    stale: bool           # 이 유닛보다 새 코드가 있다
    code_mtime: float
    pid: int | None
    description: str = ""
    restartable: bool = True   # 컨테이너에서 버스로만 보이는 유닛은 재시작 불가

    def to_dict(self) -> dict:
        return {
            "name": self.name, "active": self.active,
            "since": self.since or None, "stale": self.stale,
            "code_mtime": self.code_mtime or None, "pid": self.pid,
            "description": self.description,
            "restartable": self.restartable,
            "age_s": round(time.time() - self.since) if self.since else None,
        }


def _parse_since(value: str) -> float:
    """systemd 의 `ActiveEnterTimestamp` → epoch. 못 읽으면 0."""
    value = (value or "").strip()
    if not value or value == "n/a":
        return 0.0
    try:
        # `Thu 2026-08-20 13:50:24 KST` — 요일과 타임존을 떼고 읽는다
        parts = value.split()
        from datetime import datetime
        return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def list_units() -> list[Unit]:
    """`piper-*` 사용자 유닛. systemd 가 안 보이면 버스 생존 키로 폴백."""
    try:
        out = _systemctl("list-units", "piper-*", "--all",
                         "--no-pager", "--no-legend", "--plain").stdout
    except Exception as exc:
        logger.debug("유닛 목록 조회 실패: %s", exc)
        return _units_from_bus()

    units: list[Unit] = []
    for line in out.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("piper-"):
            continue
        name = parts[0].removesuffix(".service")
        props = _show(parts[0], "ActiveState", "ActiveEnterTimestamp",
                      "MainPID", "Description")
        since = _parse_since(props.get("ActiveEnterTimestamp", ""))
        # ⚠ 우리가 소스를 아는 유닛만 판정한다. `piper-ollama` 처럼 남의 서비스를
        #   감싼 유닛까지 "낡았다"고 하면 경고가 늘 켜져 있어 아무도 안 본다.
        known = name in _SOURCES
        code = _newest_mtime(_SOURCES[name]) if known else 0.0
        try:
            pid = int(props.get("MainPID", "0")) or None
        except ValueError:
            pid = None
        units.append(Unit(
            name=name,
            active=props.get("ActiveState") == "active",
            since=since,
            # 기동 시각을 모르면 낡았다고 하지 않는다 — 모르는 것과 낡은 것은 다르다
            stale=bool(known and since and code and code > since + _GRACE_S),
            code_mtime=code, pid=pid,
            description=props.get("Description", ""),
        ))
    if not units:
        return _units_from_bus()
    return sorted(units, key=lambda u: u.name)


# 배포 대상(.120)의 게이트웨이는 컨테이너 안이라 호스트 systemd 가 안 보인다.
# 데몬들이 생존 키(TTL 3초)에 실어 보내는 자기 보고(pid·기동 시각·소스 mtime)가
# 거기서 보이는 전부다 — stale 판정도 그 보고로 한다. 구버전 데몬은 "1"만 쓰므로
# 생존 여부만 알 수 있다.
#
# 재시작은 데몬에게 `restart` RPC 를 보낸다 — 데몬이 스스로 죽으면 유닛의
# Restart=always 가 되살린다. estopd 는 RPC 창구가 없다: 안전장치에 원격 종료
# 경로를 다는 것은 별개의 결정이라, 여기서는 재시작 불가로 남긴다.
_BUS_DAEMONS: tuple[tuple[str, str, bool], ...] = (
    ("piper-camerad", "Piper v4l2 camera daemon", True),
    ("piper-estopd", "Piper E-stop watchdog", False),
    ("piper-robotd", "Piper robot daemon", True),
    ("piper-rsd", "Piper RealSense daemon", True),
)

_bus_singleton = None


def _bus():
    global _bus_singleton
    if _bus_singleton is None:
        from piper_bus.client import Bus
        _bus_singleton = Bus()
    return _bus_singleton


def _units_from_bus() -> list[Unit]:
    units: list[Unit] = []
    try:
        for name, desc, restartable in _BUS_DAEMONS:
            info = _bus().daemon_info(name.removeprefix("piper-"))
            alive, info = info is not None, info or {}
            since = float(info.get("started") or 0)
            code = float(info.get("code_mtime") or 0)
            units.append(Unit(
                name=name, active=alive, since=since,
                stale=bool(since and code and code > since + _GRACE_S),
                code_mtime=code,
                pid=int(info["pid"]) if info.get("pid") else None,
                description=desc,
                restartable=restartable and alive,
            ))
    except Exception as exc:
        logger.debug("버스 생존 키 조회 실패: %s", exc)
        return []
    return units


def _restart_via_bus(name: str) -> tuple[bool, str]:
    if not any(n == name and ok for n, _, ok in _BUS_DAEMONS):
        return False, f"여기서는 재시작할 수 없습니다: {name}"
    try:
        _bus().rpc_call(name.removeprefix("piper-"), "restart", timeout=5)
    except Exception as exc:
        return False, f"재시작 실패: {exc}"
    logger.warning("버스로 재시작 요청: %s", name)
    return True, "OK"


def gateway_status() -> dict:
    """이 프로세스 자신. 유닛이 아니라 재시작 방식이 다르다."""
    started = _STARTED
    code = _newest_mtime(_GATEWAY_SOURCES)
    return {
        "pid": os.getpid(), "since": started or None,
        "code_mtime": code or None,
        "stale": bool(started and code and code > started + _GRACE_S),
        "age_s": round(time.time() - started) if started else None,
        "restartable": _can_self_restart(),
    }


def respawn_argv() -> list[str] | None:
    """스스로를 다시 띄울 명령줄. 못 만들면 None.

    ⚠ **`sys.argv` 를 쓰면 안 된다.** `python -m uvicorn …` 으로 뜬 경우
    `sys.argv[0]` 은 `site-packages/uvicorn/__main__.py` 인데, 그 파일을 직접
    실행하면 **그 디렉토리가 `sys.path[0]` 이 되어** `uvicorn/logging.py` 가
    표준 라이브러리 `logging` 을 가린다. 실기에서 이렇게 죽었다:

        AttributeError: module 'logging' has no attribute 'Formatter'

    `sys.orig_argv` 는 `-m uvicorn` 을 **그대로** 담고 있다 — 원래 명령줄이다.
    """
    argv = list(getattr(sys, "orig_argv", []) or [])
    if len(argv) < 2 or not Path(sys.executable).exists():
        return None
    return [sys.executable, *argv[1:]]


def _can_self_restart() -> bool:
    return respawn_argv() is not None


def restart_unit(name: str) -> tuple[bool, str]:
    """유닛 재시작. **`piper-` 접두사만** 허용한다.

    임의 유닛을 재시작하는 창구가 되면 안 된다 — 웹에서 남의 서비스를 만질 수
    있게 되고, 그건 이 기능이 하려는 일이 아니다.
    """
    if not name.startswith("piper-"):
        return False, f"우리 유닛이 아닙니다: {name}"
    if "/" in name or ".." in name:
        return False, f"이상한 이름입니다: {name}"
    try:
        r = _systemctl("restart", f"{name}.service")
    except FileNotFoundError:
        # 컨테이너 — systemctl 자체가 없다. 데몬에게 직접 청한다.
        return _restart_via_bus(name)
    except Exception as exc:
        return False, f"재시작 실패: {exc}"
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "Failed to connect to bus" in err:
            # systemctl 은 있는데 systemd 가 없는 환경(일부 이미지) — 같은 폴백
            return _restart_via_bus(name)
        return False, err or f"종료 코드 {r.returncode}"
    logger.warning("유닛 재시작: %s", name)
    return True, "OK"
