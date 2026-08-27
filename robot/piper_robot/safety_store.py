"""바닥 필터 설정의 저장/적용.

## 왜 `safety.py` 가 아닌가

그 파일은 **순수 함수**라고 머리말이 못박고 있다 — 하드웨어 없이 부를 수 있고
부작용이 없다. 그게 데이터셋 리플레이와 단위 테스트를 가능하게 하는 성질이다.
파일을 읽고 쓰는 코드를 거기 넣으면 그 약속이 깨진다.

## ⚠ 이건 "오버라이드 리스"가 아니다

refactor/robotd-safety.md 는 **녹화 중 임시 해제**를 TTL 리스로 설계했다 —
만료되고, robotd 재시작에서 **살아남지 않아야** 한다. 여기 있는 것은 그것과 다른
물건이다: 설치별 **설정**이고, 재시작을 넘어 유지되는 것이 맞다
(안 그러면 robotd 를 올릴 때마다 다시 맞춰야 한다).

리스는 아직 없다. 만들 때 이 파일을 재사용하지 말고 따로 두어야 한다 —
"영구 설정"과 "임시 해제"가 한 값을 공유하면 만료가 설정을 덮어쓴다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from piper_robot.arm import CONFIG_DIR
from piper_robot.safety import FloorConfig

logger = logging.getLogger(__name__)

PATH = CONFIG_DIR / "safety.json"

# 사람이 고칠 수 있는 값만 노출한다. `sweep_steps`·`allow_escape` 는 UI 에 두면
# 안 되는 것들이다 — 전자는 성능/정확도 맞바꿈이고 후자를 끄면 **팔이 바닥에
# 박힌 채 굳어 복구가 불가능해진다.**
EDITABLE = ("enabled", "min_z")

# 한계 높이의 허용 범위 (m). 밖의 값은 잘라서 받는다.
#
#  아래쪽: -0.30 보다 낮으면 필터가 **사실상 꺼진 것과 같다.** 그럴 거면 `enabled`
#          를 꺼야 한다 — 화면에는 켜져 있는데 아무것도 안 막는 상태가 제일 나쁘다.
#
#  위쪽:   팔에는 **구조적 최저점**이 있다. link1 은 자세와 무관하게 +7.6cm 에
#          고정돼 있어서(실측: 관절 범위 안 무작위 4000자세에서 변동 0.00cm),
#          한계를 그보다 위로 올리면 **어떤 자세도 통과 못 하고 팔이 통째로 굳는다.**
#          +5cm 로 두어 2.6cm 를 남긴다. `test_the_ceiling_still_lets_the_arm_exist`
#          가 지오메트리를 다시 재서 이 여유를 지킨다.
MIN_Z_FLOOR = -0.30
MIN_Z_CEIL = 0.05


def clamp_min_z(v: float) -> float:
    return max(MIN_Z_FLOOR, min(MIN_Z_CEIL, float(v)))


def load() -> FloorConfig:
    """저장된 설정. 없거나 깨졌으면 **기본값** — 실측으로 정한 안전한 쪽이다."""
    base = FloorConfig()
    try:
        raw = json.loads(PATH.read_text()).get("floor", {})
    except FileNotFoundError:
        return base
    except Exception as exc:
        logger.warning("안전 설정을 읽지 못했습니다 (%s): %s — 기본값을 씁니다", PATH, exc)
        return base
    return _apply(base, raw)


def save(cfg: FloorConfig) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(
        {"floor": {k: getattr(cfg, k) for k in EDITABLE}}, indent=2))
    logger.info("안전 설정 저장: enabled=%s min_z=%.3fm", cfg.enabled, cfg.min_z)


def _apply(base: FloorConfig, patch: dict) -> FloorConfig:
    """`EDITABLE` 만 반영한다. 모르는 키는 조용히 버린다 —
    UI 가 보낸 오타가 안전 파라미터를 바꾸면 안 된다."""
    out = {}
    if "enabled" in patch:
        out["enabled"] = bool(patch["enabled"])
    if "min_z" in patch:
        try:
            out["min_z"] = clamp_min_z(patch["min_z"])
        except (TypeError, ValueError):
            logger.warning("min_z 값이 숫자가 아닙니다: %r — 무시", patch["min_z"])
    return replace(base, **out) if out else base


def as_dict(cfg: FloorConfig) -> dict:
    """UI 용. 단위는 **cm** 로 낸다 — 사람이 미터로 생각하지 않는다."""
    return {
        "enabled": cfg.enabled,
        "min_z_cm": round(cfg.min_z * 100, 1),
        "range_cm": [MIN_Z_FLOOR * 100, MIN_Z_CEIL * 100],
        "default_cm": round(FloorConfig().min_z * 100, 1),
    }
