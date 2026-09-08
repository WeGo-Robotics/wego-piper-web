"""SO-101 캘리브레이션 — LeRobot 의 JSON 을 **파일로만** 읽는다 (feature/so101d.md §3).

3D 프린트 + 수조립이라 팔마다 범위가 다르다. `lerobot-calibrate` 가 만드는
`~/.cache/huggingface/lerobot/calibration/{robots|teleoperators}/<타입>/<이름>.json`
을 그대로 읽어 raw(틱 0..4095) ↔ 정규화(관절 -100..100, 그리퍼 0..100)를 한다 —
LeRobot 네이티브 경로와 숫자가 일치해야 데이터셋이 호환된다.

## homing_offset 은 여기서 적용하지 않는다

lerobot-calibrate 가 서보의 `Homing_Offset` **레지스터(EEPROM)에 직접 굽는다**
(lerobot feetech.py — `self.write("Homing_Offset", ...)`). 그래서 서보가
보고하는 Present_Position 이 이미 보정된 값이고, 소프트웨어 쪽은 range_min/max
(+drive_mode 부호)만 쓴다 — LeRobot `MotorsBus._normalize` 와 같은 수식.

## 모르는 포맷이면 읽지 않는다

필드가 빠졌거나 낯설면 **명확히 실패**한다. 조용히 기본값으로 떨어지면
"캘리브레이션이 됐는데 팔이 이상하다"가 되고, 그건 여기서 제일 비싼 오독이다.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from piper_so101.joints import MOTOR_IDS, SO101_JOINTS

#: 그리퍼만 0..100, 나머지는 -100..100 — piper_robot.joints 와 같은 구분.
_ZERO_TO_100 = frozenset({"gripper"})

_REQUIRED_FIELDS = ("id", "drive_mode", "homing_offset", "range_min", "range_max")


class CalibrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MotorCal:
    id: int
    drive_mode: int
    range_min: int
    range_max: int


def _search_dirs() -> list[Path]:
    """캘리브레이션을 찾는 곳. 테스트·비표준 배치는 env 로 바꾼다."""
    override = os.environ.get("PIPER_SO101_CALIB_DIR")
    if override:
        return [Path(override)]
    base = Path.home() / ".cache" / "huggingface" / "lerobot" / "calibration"
    # 리더가 1차 사용처라 teleoperators 를 먼저 본다 — 같은 이름이 양쪽에 있으면
    # 그건 사람이 정리할 일이고, 먼저 찾은 쪽을 쓰되 로그로 남긴다(hub 몫).
    return [base / "teleoperators" / "so101_leader",
            base / "robots" / "so101_follower"]


def find_calibration(name: str) -> Path | None:
    for d in _search_dirs():
        p = d / f"{name}.json"
        if p.exists():
            return p
    return None


def load_calibration(path: Path) -> dict[str, MotorCal]:
    """캘리브레이션 파일 → 모터별 MotorCal. 포맷이 낯설면 CalibrationError."""
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        raise CalibrationError(f"캘리브레이션을 읽지 못했습니다 ({path}): {exc}")
    if not isinstance(data, dict):
        raise CalibrationError(f"캘리브레이션 최상위가 dict 가 아닙니다: {path}")

    out: dict[str, MotorCal] = {}
    for name in SO101_JOINTS:
        entry = data.get(name)
        if entry is None:
            raise CalibrationError(
                f"캘리브레이션에 {name} 이 없습니다 ({path}) — "
                "SO-101 용 파일이 맞는지, lerobot-calibrate 버전이 바뀌었는지 보세요")
        missing = [f for f in _REQUIRED_FIELDS if f not in entry]
        if missing:
            raise CalibrationError(
                f"캘리브레이션 {name} 에 {missing} 필드가 없습니다 ({path}) — "
                "포맷이 바뀐 것 같습니다. 오독보다 거부가 낫습니다")
        if int(entry["id"]) != MOTOR_IDS[name]:
            raise CalibrationError(
                f"{name} 의 모터 ID 가 다릅니다: 파일 {entry['id']} vs 규약 "
                f"{MOTOR_IDS[name]} ({path}) — 배선이 바뀐 팔입니다")
        if int(entry["range_min"]) == int(entry["range_max"]):
            raise CalibrationError(f"{name} 의 range_min == range_max ({path})")
        out[name] = MotorCal(id=int(entry["id"]), drive_mode=int(entry["drive_mode"]),
                             range_min=int(entry["range_min"]),
                             range_max=int(entry["range_max"]))
    return out


def default_calibration() -> dict[str, MotorCal]:
    """파일이 없을 때의 전범위(0..4095) 폴백 — **미캘리브레이션 표시와 함께만**
    쓴다. 손으로 움직여 발행을 확인하는 용도이지, 이 값으로 등록·수집하면
    그 팔 전용 쓰레기 데이터가 된다 (게이트웨이가 등록을 막는 근거)."""
    return {name: MotorCal(id=MOTOR_IDS[name], drive_mode=0,
                           range_min=0, range_max=4095)
            for name in SO101_JOINTS}


def save_calibration(name: str, homing: dict[str, int],
                     range_min: dict[str, int], range_max: dict[str, int]) -> Path:
    """위저드 결과를 **LeRobot 과 같은 포맷·같은 자리**에 쓴다 — 어느 쪽
    (웹 위저드 / lerobot-calibrate)으로 만들었든 두 경로 모두 읽을 수 있다.
    drive_mode 는 SO-101 전 관절 0 (LeRobot so101 설정과 동일)."""
    d = _search_dirs()[0]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    data = {j: {"id": MOTOR_IDS[j], "drive_mode": 0,
                "homing_offset": int(homing[j]),
                "range_min": int(range_min[j]), "range_max": int(range_max[j])}
            for j in SO101_JOINTS}
    path.write_text(json.dumps(data, indent=4))
    return path


def normalize(ticks: dict[str, int], cal: dict[str, MotorCal]) -> dict[str, float]:
    """틱 → 정규화. LeRobot `_normalize` 와 같은 수식 (범위 클램프 포함)."""
    out: dict[str, float] = {}
    for name, raw in ticks.items():
        c = cal[name]
        bounded = min(c.range_max, max(c.range_min, int(raw)))
        ratio = (bounded - c.range_min) / (c.range_max - c.range_min)
        if name in _ZERO_TO_100:
            v = ratio * 100.0
            out[name] = 100.0 - v if c.drive_mode else v
        else:
            v = ratio * 200.0 - 100.0
            out[name] = -v if c.drive_mode else v
    return out


def denormalize(norm: dict[str, float], cal: dict[str, MotorCal]) -> dict[str, int]:
    """정규화 → 틱. `normalize` 의 역함수 (클램프 포함)."""
    out: dict[str, int] = {}
    for name, value in norm.items():
        c = cal[name]
        if name in _ZERO_TO_100:
            v = 100.0 - value if c.drive_mode else value
            ratio = min(100.0, max(0.0, v)) / 100.0
        else:
            v = -value if c.drive_mode else value
            ratio = (min(100.0, max(-100.0, v)) + 100.0) / 200.0
        out[name] = int(round(c.range_min + ratio * (c.range_max - c.range_min)))
    return out
