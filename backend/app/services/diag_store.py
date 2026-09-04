"""검사 결과 보관 — 저장·조회·비교 (feature/joint-diagnostics.md).

⚠ **행까지 저장한다.** 요약만 남기면 나중에 파형을 못 본다 — "그때는 어땠지" 를
보려고 저장하는 것인데 정작 그림이 없다. 대신 목록은 요약만 실어 보낸다:
한 회차가 1300행 × 120열이라 목록에 다 담으면 화면이 못 연다.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

ROOT: Path = settings.config_dir / "diagnostics"
_NAME = re.compile(r"^[A-Za-z0-9가-힣 ._-]{1,60}$")


def _check(name: str) -> None:
    if not _NAME.match(name or ""):
        raise ValueError("이름은 한글·영문·숫자와 -_. 만, 60자 이내입니다")


def _path(name: str) -> Path:
    _check(name)
    return ROOT / f"{name}.json"


def save(name: str, payload: dict) -> dict:
    """한 회차를 저장한다. 같은 이름이면 덮어쓴다."""
    _check(name)
    ROOT.mkdir(parents=True, exist_ok=True)
    rec = {**payload, "name": name, "saved_at": time.time()}
    _path(name).write_text(json.dumps(rec, ensure_ascii=False))
    logger.info("검사 결과 저장: %s (%d행)", name, len(rec.get("rows") or []))
    return meta(rec)


def meta(rec: dict) -> dict:
    """목록·비교에 쓰는 가벼운 부분. **행은 뺀다.**"""
    return {k: v for k, v in rec.items() if k != "rows"} | {
        "rows": len(rec.get("rows") or [])}


def get(name: str) -> dict | None:
    p = _path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as exc:                          # noqa: BLE001
        logger.warning("검사 결과를 읽지 못했습니다 (%s): %s", name, exc)
        return None


def list_saved() -> list[dict]:
    """저장된 회차들 — **최근 것부터.**"""
    if not ROOT.exists():
        return []
    out = []
    for p in ROOT.glob("*.json"):
        try:
            out.append(meta(json.loads(p.read_text())))
        except Exception:
            continue                                  # 깨진 파일 하나가 목록을 막지 않는다
    return sorted(out, key=lambda r: r.get("saved_at", 0), reverse=True)


def delete(name: str) -> bool:
    p = _path(name)
    if not p.exists():
        return False
    p.unlink()
    return True


def compare(a: dict, b: dict) -> dict:
    """두 회차의 관절별 차이. `b − a`.

    ⚠ **모션이 다르면 비교가 거짓이다.** 진폭이나 방향이 다른 두 회차의 전류를
    나란히 놓으면 관절이 아니라 계획의 차이를 보는 것이다. 그래서 계획이 다르면
    숫자를 내되 **다르다는 사실을 함께** 낸다 — 감추지도, 막지도 않는다.
    """
    ja = (a.get("summary") or {}).get("joints") or {}
    jb = (b.get("summary") or {}).get("joints") or {}
    keys = ("err_max_deg", "err_rms_deg", "current_max_a", "current_mean_a",
            "effort_max_nm", "temp_rise_c")
    joints = {}
    for j in sorted(set(ja) | set(jb)):
        row = {}
        for k in keys:
            va, vb = (ja.get(j) or {}).get(k), (jb.get(j) or {}).get(k)
            row[k] = {"a": va, "b": vb,
                      "delta": None if (va is None or vb is None) else round(vb - va, 4),
                      "ratio": None if not va or vb is None else round(vb / va, 3)}
        joints[j] = row
    return {"joints": joints, "keys": list(keys),
            "plan_differs": _plan_differs(a.get("plan"), b.get("plan"))}


def _plan_differs(pa: dict | None, pb: dict | None) -> list[str]:
    """계획이 어떻게 다른가 — 사람이 읽을 문장으로."""
    if not pa or not pb:
        return ["한쪽에 계획 정보가 없습니다"]
    out = []
    if pa.get("intensity") != pb.get("intensity"):
        out.append(f"강도 {pa.get('intensity')} → {pb.get('intensity')}")
    amps_a = {j["joint"]: (j["amplitude_deg"], j.get("direction", 0))
              for j in pa.get("joints", [])}
    amps_b = {j["joint"]: (j["amplitude_deg"], j.get("direction", 0))
              for j in pb.get("joints", [])}
    for j in sorted(set(amps_a) | set(amps_b)):
        if amps_a.get(j) != amps_b.get(j):
            out.append(f"{j} 모션 {amps_a.get(j)} → {amps_b.get(j)}")
    return out
