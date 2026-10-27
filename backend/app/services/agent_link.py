"""설정 → 에이전트 탭의 재료 — 깔린 AI CLI, 고른 CLI, 붙이는 레시피 (feature/agent-cli.md §4-7).

감지 표와 레시피는 `piper_bus.agent_clis` 에 있다 — 호스트의 unitd 도 같은 표로 본다. 여기는 둘을 합치고
(게이트웨이 기계에서 본 것 + unitd 가 호스트에서 본 것) 사람의 선택을 파일로 기억한다.

게이트웨이는 CLI 를 돌리지 않는다. 선택은 **안내를 바꾸는 것**이지 실행 대상을 정하는 것이 아니다.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from piper_bus import agent_clis

from app.core.config import settings

logger = logging.getLogger(__name__)

PREF_FILE = "agent_cli.json"


def _pref_path() -> Path:
    return Path(settings.config_dir) / PREF_FILE


def load_preferred() -> str | None:
    try:
        v = json.loads(_pref_path().read_text()).get("preferred")
        return v if v in agent_clis.known_ids() else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("에이전트 CLI 선택 파일을 읽지 못했다 (%s): %s", _pref_path(), exc)
        return None


def save_preferred(cli_id: str) -> dict:
    if cli_id not in agent_clis.known_ids():
        raise ValueError(f"모르는 CLI 입니다: {cli_id} — {', '.join(agent_clis.known_ids())} 중 하나")
    p = _pref_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    out = {"preferred": cli_id, "updated_at": time.time()}
    p.write_text(json.dumps(out))
    return out


def _host_clis() -> tuple[list[dict], bool]:
    """unitd 가 호스트에서 본 것. 옛 unitd(clis 를 모르는)면 (빈 목록, False)."""
    from app.services import units

    if not units.unitd_available():
        return [], False
    try:
        from piper_bus import contract as C

        h = units._bus().rpc_call(C.UNITD, "host_info", [], timeout=10) or {}
    except Exception as exc:
        logger.debug("unitd host_info 실패: %s", exc)
        return [], False
    if "clis" not in h:
        return [], False
    return list(h.get("clis") or []), True


def collect() -> dict:
    local = agent_clis.detect()
    host, host_supported = _host_clis()
    by_host = {c.get("id"): c for c in host}
    merged = []
    for c in local:
        entry = {**c, "where": "gateway" if c.get("installed") else None}
        hc = by_host.get(c["id"])
        if not c.get("installed") and hc and hc.get("installed"):
            entry.update(installed=True, path=hc.get("path"), version=hc.get("version"), where="host")
        merged.append(entry)
    saved = load_preferred()
    return {
        "clis": merged,
        "preferred": saved or agent_clis.default_preferred(merged),
        "preferred_saved": saved is not None,
        "recipes": agent_clis.recipes(),
        "gateway_in_container": Path("/.dockerenv").exists(),
        "host_supported": host_supported,
    }
