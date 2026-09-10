"""데몬 저널을 웹으로 — "무슨 오류가 났나" 를 SSH 없이 본다.

컨테이너 게이트웨이는 호스트 저널을 못 읽는다 — unitd 가 읽어 준다. 소스로 도는
기계에서 unitd 가 없으면 여기서 직접 journalctl 을 부른다(같은 파서 — 표는 하나).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def fetch(unit: str, lines: int, level: str, since: str | None) -> dict:
    from app.services import units
    from piper_bus import contract as C

    if units.unitd_available():
        try:
            out = units._bus().rpc_call(C.UNITD, "logs", [unit, lines, level, since], timeout=30)
            return {**out, "via": "unitd"}
        except Exception as exc:
            raise RuntimeError(str(exc))
    from daemons.unitd import fetch_logs          # 같은 파서·같은 허용 목록
    try:
        return {**fetch_logs(unit, lines, level, since, docker_container=None), "via": "local"}
    except FileNotFoundError:
        raise RuntimeError("저널을 읽을 수 없습니다 — 서비스 관리 데몬(piper-unitd)이 없고 journalctl 도 없습니다")


def as_text(entries: list[dict]) -> str:
    from datetime import datetime

    rows = []
    for e in entries:
        ts = datetime.fromtimestamp(e["t"]).strftime("%m-%d %H:%M:%S") if e.get("t") else "--:--:--"
        rows.append(f"{ts} {e.get('unit', ''):8s} {e.get('level', ''):7s} {e.get('msg', '')}")
    return "\n".join(rows) + ("\n" if rows else "")
