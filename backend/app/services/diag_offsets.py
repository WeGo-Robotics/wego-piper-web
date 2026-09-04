"""검사 중심 오프셋 — **팔마다 사람이 정한다.**

## 왜 코드에 기본값을 안 두나

걸리는 것은 팔에 붙은 **기구물**이지 관절이 아니다. 실기에서 마스터 암에만 달린
기구물 때문에 joint5 가 아래에서 걸렸는데, 한때 이걸 `{"joint5": 30}` 상수로
박아서 그 기구물이 없는 팔까지 중심을 옮길 뻔했다. 무엇이 달려 있는지는 코드가
알 수 없고, 사람만 안다.

## 왜 게이트웨이인가

`hub.py` 가 정해 둔 경계다 — **사람이 정하는 것**은 게이트웨이가 갖고 robotd 는
인자로 받는다. 파킹 자세를 robotd 가 자기 파일에서 읽다가 저장 위치가 갈라져
"저장은 되는데 안 간다" 가 났다. 같은 실수를 반복하지 않는다.
"""

from __future__ import annotations

import json
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

ROOT = settings.config_dir / "diag_offsets"

#: 오프셋 한계 (도). 이보다 크게 옮기려는 것은 오타에 가깝다 — 관절 가동범위가
#: 가장 넓은 것도 ±150° 이고, 검사는 지금 자세 근처에서 흔드는 일이다.
LIMIT_DEG = 90.0


def _path(iface: str):
    # ⚠ 인터페이스 이름이 경로를 벗어나면 안 된다
    safe = "".join(c for c in iface if c.isalnum() or c in "-_")
    return ROOT / f"{safe or 'unknown'}.json"


def load(iface: str) -> dict[str, float]:
    """저장된 오프셋. 없거나 깨졌으면 **빈 값** — 안 옮기는 쪽이 안전하다."""
    path = _path(iface)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        logger.warning("검사 오프셋을 못 읽었습니다 (%s) — 빈 값으로 갑니다", iface)
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, (int, float))}


def save(iface: str, offsets: dict[str, float]) -> dict[str, float]:
    """0 은 지운다 — 안 쓰는 값이 파일에 남으면 나중에 왜 있는지 모른다."""
    from piper_robot.kinematics import ARM_JOINTS

    clean = {
        j: round(max(-LIMIT_DEG, min(LIMIT_DEG, float(v))), 2)
        for j, v in offsets.items()
        if j in ARM_JOINTS and isinstance(v, (int, float)) and abs(float(v)) >= 0.01
    }
    ROOT.mkdir(parents=True, exist_ok=True)
    _path(iface).write_text(json.dumps(clean, indent=2))
    logger.info("검사 오프셋 저장 (%s): %s", iface, clean)
    return clean
