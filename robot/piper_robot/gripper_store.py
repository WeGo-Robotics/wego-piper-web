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

## 열림 끝 — 리더 핸들은 그리퍼 모델이 아니다 (2026-10-08)

70/100 은 **달린 그리퍼 모델**의 행정이다. 리더(마스터)는 그리퍼가 아니라 **티칭 핸들**이고, 그 핸들이
내는 지령은 모델과 무관한 자기 끝에서 멈춘다 — 실기: 리더를 끝까지 열어도 `0x159` 가 최대 82860µm,
같은 순간 팔로워(대형)는 99500µm 까지 열릴 수 있었다. 정규화는 비율이라 팔로워가 82% 에서 멈췄다.
그래서 팔마다 **측정한 끝**을 상한으로 쓸 수 있다(`set_end`) — 리더를 끝까지 열고 그 값을 저장하면
리더의 "100" 이 리더의 끝이 되어 팔로워도 끝까지 열린다. 행정(모델)을 바꾸면 이 값은 버린다 —
다른 그리퍼에서 잰 끝이 새 그리퍼에 남으면 안 된다.
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
_ends: dict[str, int] = {}                   # 팔별 측정한 열림 끝(µm) — 있으면 표 값보다 앞선다

#: 측정한 끝으로 받아들이는 범위(µm). 아래는 **덜 열린 채 눌렀다**는 뜻이다 — 상한이 작아지면
#: 정규화가 폭주해(작은 움직임이 100 이 된다) 팔로워가 튄다. 위는 그리퍼가 낼 수 없는 값이다.
END_MIN_UM, END_MAX_UM = 20000, 120000


def _load() -> dict[str, float]:
    global _cache, _strokes, _ends
    if _cache is None:
        try:
            data = json.loads(PATH.read_text())
            _cache = {k: clamp(v) for k, v in data.get("arms", {}).items()}
            # 모르는 값은 버린다 — 손으로 고친 파일이 팔을 엉뚱한 상한으로 보내면 안 된다
            _strokes = {k: int(v) for k, v in data.get("strokes", {}).items()
                        if v in GRIPPER_STROKES_MM}
            _ends = {k: int(v) for k, v in data.get("ends", {}).items()
                     if isinstance(v, (int, float)) and END_MIN_UM <= v <= END_MAX_UM}
        except FileNotFoundError:
            _cache, _strokes, _ends = {}, {}, {}
        except Exception as exc:
            logger.warning("gripper.json 을 못 읽었다 — 기본값(%.1f N·m·%dmm)으로 간다: %s",
                           DEFAULT_NM, DEFAULT_GRIPPER_STROKE_MM, exc)
            _cache, _strokes, _ends = {}, {}, {}
    return _cache


def _write() -> None:
    """파일 전체를 쓴다 — 힘·행정·열림 끝이 **같이** 나간다 (머리말)."""
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps({"arms": _cache, "strokes": _strokes, "ends": _ends}, indent=2))
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


def end_um(iface: str) -> int | None:
    """측정해 저장한 열림 끝(µm). 없으면 None — 표 값을 쓴다."""
    with _lock:
        _load()
        return _ends.get(iface)


def raw_max_um(iface: str) -> int:
    """이 팔 그리퍼의 raw 상한(µm). raw ↔ 정규화 변환이 **프레임마다** 부른다(메모리 조회뿐).

    측정한 끝이 있으면 그것이 먼저다 — 리더 핸들처럼 모델 표로는 못 맞추는 끝이 있다."""
    with _lock:
        _load()
        return _ends.get(iface) or GRIPPER_RAW_MAX[_strokes.get(iface, DEFAULT_GRIPPER_STROKE_MM)]


def set_end(iface: str, raw_um) -> int:
    """지금 열림(raw µm)을 이 팔의 끝으로 저장하고 **저장된 값**을 돌려준다.

    범위 밖은 거절한다(클램프가 아니다) — 덜 열린 채 눌렀을 때 가까운 값으로 맞춰 저장하면
    그 팔의 "100" 이 조용히 틀린 자리가 된다."""
    try:
        v = int(round(float(raw_um)))
    except (TypeError, ValueError):
        raise ValueError(f"열림 값을 읽지 못했습니다: {raw_um!r}") from None
    if not END_MIN_UM <= v <= END_MAX_UM:
        raise ValueError(
            f"지금 열림이 {v / 1000:.1f}mm 입니다 — 끝까지 연 상태에서 눌러 주세요 "
            f"({END_MIN_UM // 1000}~{END_MAX_UM // 1000}mm 만 받습니다)")
    with _lock:
        _load()
        _ends[iface] = v
        _write()
    logger.info("그리퍼 열림 끝 %s: %d µm (%.1f mm)", iface, v, v / 1000)
    return v


def clear_end(iface: str) -> None:
    """측정한 끝을 버리고 행정(70/100) 표 값으로 돌아간다."""
    with _lock:
        _load()
        if _ends.pop(iface, None) is not None:
            _write()
            logger.info("그리퍼 열림 끝 %s: 초기화", iface)


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
        # 그리퍼가 바뀌었을 수 있다 — 옛 그리퍼에서 잰 끝이 새 그리퍼에 남으면 안 된다
        _ends.pop(iface, None)
        _write()
    logger.info("그리퍼 행정 %s: %d mm (raw 상한 %d)", iface, v, GRIPPER_RAW_MAX[v])
    return v


def stroke_dict(iface: str) -> dict:
    mm = stroke_mm(iface)
    return {"iface": iface, "stroke_mm": mm,
            "options_mm": list(GRIPPER_STROKES_MM), "default_mm": DEFAULT_GRIPPER_STROKE_MM,
            # end_um: 직접 맞춘 열림 끝(없으면 null) / raw_max_um: 지금 변환에 쓰는 상한
            # / table_raw_max_um: 맞춤을 지우면 돌아갈 표 값
            "end_um": end_um(iface), "raw_max_um": raw_max_um(iface),
            "table_raw_max_um": GRIPPER_RAW_MAX[mm]}


def as_dict(iface: str) -> dict:
    return {"iface": iface, "effort_nm": effort_nm(iface),
            "min_nm": MIN_NM, "max_nm": MAX_NM, "default_nm": DEFAULT_NM}
