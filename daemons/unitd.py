#!/usr/bin/env python3
"""piper-unitd — `piper-*` 유닛을 켜고 끄는 데몬 (feature/services.md).

## 왜 따로인가

배포 대상의 게이트웨이는 **컨테이너**라 `systemctl` 이 없다. 데몬에게 `restart`
RPC 를 보내는 게 지금까지의 전부였는데, 그건 "끄기"가 못 된다 — 스스로 죽어도
`Restart=always` 가 되살린다. "부팅 시 시작"(enable/disable)은 아예 길이 없다.

호스트 systemd 소켓을 컨테이너에 마운트하는 길도 있지만, 이 compose 는 특권과
`/dev` 를 **일부러** 뺐다 (docker-compose.yml). 그 결정을 되돌리지 않고, 호스트에
아주 작은 데몬 하나를 두어 버스로 부탁을 받는다. 소스로 도는 기계도 같은 길을
쓴다 — 통로가 하나여야 어느 환경에서 되고 어느 환경에서 안 되는 일이 없다.

## 무엇을 하는가 — 그리고 안 하는가

- 허용 목록은 `piper_bus.contract.UNIT_CATALOG` 뿐이다. 남의 유닛은 이름을
  알아도 못 만진다.
- **estopd 는 읽기 전용**(`UNIT_READONLY`). 안전장치에 원격 종료 경로를 다는 것은
  별개의 결정이다.
- **자기 자신은 끄지 않는다**(`UNIT_SELF`) — 끄면 이 화면의 켜기/끄기가 같이 죽는다.
- 활동(녹화·추론) 중에 robotd 를 끄면 에피소드가 깨진다 — 그 판단은 **게이트웨이**가
  한다(exclusivity). 여기는 활동을 모른다.

환경변수: `PIPER_REDIS_URL`
"""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from piper_bus import contract as C
from piper_bus.client import Bus, self_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] unitd: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("unitd")

REPO = Path(__file__).resolve().parents[1]

_running = True

ACTIONS = ("start", "stop", "restart", "enable", "disable")


def unit_name(name: str) -> str:
    """`simd` / `piper-simd` / `piper-simd.service` → `piper-simd.service`. 카탈로그에
    없으면 ValueError — 남의 유닛은 이름을 알아도 못 만진다."""
    short = name.removesuffix(".service").removeprefix("piper-")
    if short not in C.UNIT_CATALOG or "/" in short or ".." in short:
        raise ValueError(f"우리 유닛이 아닙니다: {name}")
    return f"piper-{short}.service"


def check_action(name: str, action: str) -> str:
    """허용되는 조합만 통과시킨다. 반환은 `piper-*.service` 이름."""
    unit = unit_name(name)
    short = unit.removeprefix("piper-").removesuffix(".service")
    if action not in ACTIONS:
        raise ValueError(f"모르는 동작입니다: {action}")
    if short in C.UNIT_READONLY:
        raise ValueError(f"{short} 는 안전장치라 웹에서 손대지 않습니다")
    if short == C.UNIT_SELF and action in ("stop", "disable", "restart"):
        raise ValueError(f"{short} 는 이 기능 자체입니다 — 끄면 켜기/끄기가 같이 죽습니다")
    return unit


def _systemctl(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, timeout=timeout)


def _parse_since(value: str) -> float:
    value = (value or "").strip()
    if not value or value == "n/a":
        return 0.0
    try:
        from datetime import datetime
        parts = value.split()
        return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


class UnitHub:
    def list(self) -> list[dict]:
        """카탈로그의 유닛 전부 — **설치 안 된 것도** 낸다(`installed: False`).
        화면이 "왜 simd 가 없지"를 보게 하려면 없는 것도 줄로 보여야 한다."""
        out: list[dict] = []
        for short, (desc, kind) in C.UNIT_CATALOG.items():
            unit = f"piper-{short}.service"
            r = _systemctl("show", unit, "--property=ActiveState",
                           "--property=UnitFileState", "--property=ActiveEnterTimestamp",
                           "--property=MainPID", "--property=LoadState")
            props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
            load = props.get("LoadState", "")
            installed = load not in ("not-found", "") and props.get("UnitFileState", "") != ""
            ufs = props.get("UnitFileState", "")
            try:
                pid = int(props.get("MainPID", "0")) or None
            except ValueError:
                pid = None
            out.append({
                "name": f"piper-{short}", "short": short, "description": desc, "kind": kind,
                "installed": installed,
                "active": props.get("ActiveState") == "active",
                "state": props.get("ActiveState", "unknown"),
                # enabled/disabled 외(static·masked·indirect)는 "모른다" — 체크박스를 안 그린다
                "enabled": True if ufs == "enabled" else False if ufs == "disabled" else None,
                "since": _parse_since(props.get("ActiveEnterTimestamp", "")),
                "pid": pid,
                "readonly": short in C.UNIT_READONLY,
                "self": short == C.UNIT_SELF,
            })
        return out

    def control(self, name: str, action: str) -> dict:
        unit = check_action(name, action)
        r = _systemctl(action, unit, timeout=30)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip() or f"종료 코드 {r.returncode}"
            raise RuntimeError(f"{action} {unit} 실패: {err}")
        logger.warning("%s %s", action, unit)
        return {"unit": unit, "action": action, "ok": True}


_METHODS = {"list", "control"}


def serve(bus: Bus, hub: UnitHub) -> None:
    global _running
    logger.info("유닛 관리 데몬 시작")
    last_beat = 0.0
    while _running:
        now = time.monotonic()
        if now - last_beat > C.DAEMON_ALIVE_TTL_MS / 3000:
            try:
                bus.mark_alive(C.UNITD, info=self_report(REPO, C.DAEMON_SOURCES[C.UNITD]))
            except Exception as exc:
                logger.warning("생존 표시 실패: %s", exc)
            last_beat = now
        try:
            req = bus.rpc_next_request(C.UNITD, timeout=1)
        except Exception as exc:
            logger.warning("요청 수신 오류: %s", exc)
            time.sleep(0.5)
            continue
        if req is None:
            continue
        rid, method, args = req.get("id", ""), req.get("method", ""), req.get("args", [])
        if method == "restart":
            bus.rpc_reply(rid, True, result="restarting")
            logger.warning("재시작 요청 — 종료")
            _running = False
            continue
        if method not in _METHODS:
            bus.rpc_reply(rid, False, error=f"알 수 없는 메서드: {method}")
            continue
        try:
            bus.rpc_reply(rid, True, result=getattr(hub, method)(*args))
        except Exception as exc:
            logger.warning("%s 실패: %s", method, exc)
            bus.rpc_reply(rid, False, error=str(exc))


def main() -> int:
    def _bye(signum, _frame):
        global _running
        _running = False
        logger.info("신호 %s — 종료", signum)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    bus = Bus()
    if not bus.ping():
        logger.error("Redis 에 연결할 수 없습니다 (%s)", os.environ.get("PIPER_REDIS_URL", "기본값"))
        return 1
    if _systemctl("--version").returncode != 0:
        logger.error("사용자 systemd 에 접속할 수 없습니다 — 이 데몬은 호스트에서만 뜻이 있다")
        return 1
    serve(bus, UnitHub())
    logger.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
