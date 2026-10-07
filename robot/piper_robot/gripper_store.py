"""그리퍼 힘(토크)의 저장·적용 — `load_store` 와 같은 자리, 같은 이유 (2026-10-07).

Piper 그리퍼 명령(`GripperCtrl`, CAN 0x159)은 **프레임마다** 위치와 함께 힘을 싣는다.
팔에 한 번 굽는 설정이 아니라 "이 힘으로 여기까지" 를 매번 말하는 방식이라, 여기 값만
바꾸면 **다음 명령 프레임부터** 바로 반영된다 — 추론 도중 슬라이더가 그래서 가능하다.

예전에는 두 곳(`publish.py`·`arm.go_parking`)에 `1000`(= 1.0 N·m)이 박혀 있었다.
미끄러운 물체는 놓치고 말랑한 물체는 찌그러뜨려도 바꿀 길이 없었다.

## 왜 robotd 가 저장하나

팔에 명령을 보내는 쪽이 들고 있어야 게이트웨이가 재시작돼도 값이 산다. 게이트웨이
세션에도 두면 저장이 두 곳이 되고, 어긋나면 화면의 힘과 실제 힘이 다르다 —
바닥 필터(`safety.json`)·부하 임계(`load.json`)와 같은 판단이다.

## 왜 팔마다인가

그리퍼는 팔마다 달려 있고, 양팔에서 한쪽만 다른 물건을 잡는 일이 흔하다.
"""

from __future__ import annotations

import json
import logging
import threading

from piper_robot.arm import CONFIG_DIR

logger = logging.getLogger(__name__)

PATH = CONFIG_DIR / "gripper.json"

#: SDK 범위(0~5000, 0.001 N·m 단위 → 0~5 N·m). 기본값은 예전에 박혀 있던 1.0 N·m 그대로 —
#: 이 기능을 넣었다고 이미 쓰던 팔의 잡는 힘이 바뀌면 안 된다.
MIN_NM, MAX_NM, DEFAULT_NM = 0.0, 5.0, 1.0

_lock = threading.Lock()
_cache: dict[str, float] | None = None


def _load() -> dict[str, float]:
    global _cache
    if _cache is None:
        try:
            raw = json.loads(PATH.read_text()).get("arms", {})
            _cache = {k: clamp(v) for k, v in raw.items()}
        except FileNotFoundError:
            _cache = {}
        except Exception as exc:
            logger.warning("gripper.json 을 못 읽었다 — 기본값(%.1f N·m)으로 간다: %s",
                           DEFAULT_NM, exc)
            _cache = {}
    return _cache


def clamp(v) -> float:
    return float(min(MAX_NM, max(MIN_NM, float(v))))


def effort_nm(iface: str) -> float:
    with _lock:
        return _load().get(iface, DEFAULT_NM)


def effort_mnm(iface: str) -> int:
    """`GripperCtrl` 이 받는 단위(0.001 N·m) — 명령 프레임마다 부른다(메모리 조회뿐)."""
    return int(round(effort_nm(iface) * 1000))


def set_effort(iface: str, nm) -> float:
    """저장하고 **적용된(클램프된) 값**을 돌려준다. 끝이 어디인지는 이 값이 말한다."""
    v = clamp(nm)
    with _lock:
        arms = _load()
        arms[iface] = v
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(json.dumps({"arms": arms}, indent=2))
        except Exception as exc:
            logger.warning("gripper.json 저장 실패 (값은 지금 프로세스에만 남는다): %s", exc)
    logger.info("그리퍼 힘 %s: %.2f N·m", iface, v)
    return v


def as_dict(iface: str) -> dict:
    return {"iface": iface, "effort_nm": effort_nm(iface),
            "min_nm": MIN_NM, "max_nm": MAX_NM, "default_nm": DEFAULT_NM}
