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

## 행정(stroke) — 같은 파일, 같은 이유 (2026-10-08)

Piper 그리퍼는 소형 70mm / 대형 100mm 두 가지고, 리더·팔로워에 섞여 달린다. 정규화(0..100)는
**행정에 대한 비율**이라, 팔마다 raw 상한이 달라야 "100" 이 그 팔의 끝을 뜻한다. 대형에 소형
상한(68000)을 쓰면 팔로워가 68mm 에서 토크로 버텨 **끝까지 안 열린다**(실기에서 겪었다).

설정이 없는 팔은 소형(예전 값)이라 이 기능을 넣었다고 쓰던 팔이 바뀌지 않는다. 힘과 같은 파일에
`strokes` 로 둔다 — ⚠ 쓰는 곳은 `_write` 하나여야 한다. 두 곳이 파일 전체를 따로 쓰면 힘을
저장하는 순간 행정이 지워진다.
"""

from __future__ import annotations

import json
import logging
import threading

from piper_robot.arm import CONFIG_DIR
from piper_robot.joints import DEFAULT_GRIPPER_STROKE_MM, GRIPPER_RAW_MAX, GRIPPER_STROKES_MM

logger = logging.getLogger(__name__)

PATH = CONFIG_DIR / "gripper.json"

#: SDK 범위(0~5000, 0.001 N·m 단위 → 0~5 N·m). 기본값은 예전에 박혀 있던 1.0 N·m 그대로 —
#: 이 기능을 넣었다고 이미 쓰던 팔의 잡는 힘이 바뀌면 안 된다.
MIN_NM, MAX_NM, DEFAULT_NM = 0.0, 5.0, 1.0

_lock = threading.Lock()
_cache: dict[str, float] | None = None       # 팔별 힘 (None = 아직 안 읽음 → 행정도 같이 읽는다)
_strokes: dict[str, int] = {}                # 팔별 행정(mm)


def _load() -> dict[str, float]:
    global _cache, _strokes
    if _cache is None:
        try:
            data = json.loads(PATH.read_text())
            _cache = {k: clamp(v) for k, v in data.get("arms", {}).items()}
            # 모르는 값은 버린다 — 손으로 고친 파일이 팔을 엉뚱한 상한으로 보내면 안 된다
            _strokes = {k: int(v) for k, v in data.get("strokes", {}).items()
                        if v in GRIPPER_STROKES_MM}
        except FileNotFoundError:
            _cache, _strokes = {}, {}
        except Exception as exc:
            logger.warning("gripper.json 을 못 읽었다 — 기본값(%.1f N·m·%dmm)으로 간다: %s",
                           DEFAULT_NM, DEFAULT_GRIPPER_STROKE_MM, exc)
            _cache, _strokes = {}, {}
    return _cache


def _write() -> None:
    """파일 전체를 쓴다 — 힘·행정이 **같이** 나간다 (머리말)."""
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps({"arms": _cache, "strokes": _strokes}, indent=2))
    except Exception as exc:
        logger.warning("gripper.json 저장 실패 (값은 지금 프로세스에만 남는다): %s", exc)


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
        _write()
    logger.info("그리퍼 힘 %s: %.2f N·m", iface, v)
    return v


def stroke_mm(iface: str) -> int:
    with _lock:
        _load()
        return _strokes.get(iface, DEFAULT_GRIPPER_STROKE_MM)


def raw_max_um(iface: str) -> int:
    """이 팔 그리퍼의 raw 상한(µm). raw ↔ 정규화 변환이 **프레임마다** 부른다(메모리 조회뿐)."""
    return GRIPPER_RAW_MAX[stroke_mm(iface)]


def set_stroke(iface: str, mm) -> int:
    """저장하고 적용된 행정을 돌려준다. 모르는 행정은 **거절한다**(클램프가 아니다) —
    70 과 100 사이의 값은 존재하지 않는 그리퍼다."""
    try:
        v = int(mm)
    except (TypeError, ValueError):
        raise ValueError(f"행정은 {GRIPPER_STROKES_MM} mm 중 하나여야 합니다: {mm!r}") from None
    if v not in GRIPPER_STROKES_MM:
        raise ValueError(f"행정은 {GRIPPER_STROKES_MM} mm 중 하나여야 합니다: {mm!r}")
    with _lock:
        _load()
        _strokes[iface] = v
        _write()
    logger.info("그리퍼 행정 %s: %d mm (raw 상한 %d)", iface, v, GRIPPER_RAW_MAX[v])
    return v


def stroke_dict(iface: str) -> dict:
    return {"iface": iface, "stroke_mm": stroke_mm(iface),
            "options_mm": list(GRIPPER_STROKES_MM), "default_mm": DEFAULT_GRIPPER_STROKE_MM}


def as_dict(iface: str) -> dict:
    return {"iface": iface, "effort_nm": effort_nm(iface),
            "min_nm": MIN_NM, "max_nm": MAX_NM, "default_nm": DEFAULT_NM}
