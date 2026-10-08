"""PIPER 관절 캘리브레이션 — raw 엔코더 값 ↔ 정규화 값 변환의 단일 정의.

이전에는 같은 `cal` dict 와 서로 **역함수 관계인 변환식**이
[robot_manager.py] 안에 두 번 인라인으로 적혀 있었다
(`read_joints_normalized` / `go_parking`, refactor/05-joint-calibration.md).
한쪽 범위만 고치면 정규화/역정규화가 어긋나 **팔이 엉뚱한 위치로 간다** —
파킹 동작이라 바닥을 긁거나 관절 한계를 칠 수 있다.

⚠ **정본은 사실 여기가 아니다.** 같은 값이
`vendor/lerobot_robot_piper/.../piper_follower.py` 의 `PiperFollower.__init__` 에
`MotorCalibration(...)` 으로 들어 있고, 실제 추론·녹화는 그쪽을 쓴다.
vendor 는 외부 repo(`WeGo-Robotics/lerobot_robot_piper`) 스냅샷이라 여기서 고치면
다음 갱신에 덮이므로 **복제를 유지하되 어긋나면 테스트가 잡는다**
(`tests/test_joints.py`). 프로세스 경계를 넘는 중복이라
refactor/04-err-bits.md 와 같은 종류의 문제다.
"""

# 관절 순서 — 백엔드가 만드는 dict 순서이자 프론트가 인덱스로 매칭하는 순서다.
# 바꾸면 수동 제어 슬라이더가 엉뚱한 관절을 움직인다.
JOINT_ORDER: tuple[str, ...] = (
    "joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "gripper",
)

# raw 엔코더 범위 (min, max). AGILEX-M/S 기준 간이 선형 매핑.
#
# ⚠ **joint6 은 ±120 이 정본이다.** 한때 `(-100000, 130000)` 이었는데 그 값은
#   어느 공식 문서에도 없다 — piper_sdk 의 `PiperParamManager` 표도, AgileX 의
#   URDF(`vendor/agx_arm_urdf/`, 구형·신형 둘 다)도 `[-120.0, 120.0]` 이라고
#   말한다. 나머지 다섯이 SDK 표와 한 자리도 안 틀리게 맞는 것을 보면 이 표는
#   SDK 에서 옮겨 적은 것이고, j6 만 손이 미끄러진 자리다.
#
# ⚠ 비대칭이라는 게 왜 문제였나: 정규화 0 이 물리 0 이 아니라 **+15°** 였다.
#   조그로 0 을 보내도 안 돌아오고, 파킹 자세도 15° 틀어진 채 섰다. 게다가
#   정규화 +100 이 130° 라 **기구 한계 밖까지 명령할 수 있었다** — 펌웨어
#   소프트 한계는 ±170/±180 이라 이 표가 j6 을 막는 유일한 것이었다.
#
# ⚠ **바꾸면 옛 데이터의 뜻이 바뀐다.** 이 값을 고칠 때 기록된 데이터셋의 j6 도
#   같이 다시 매겨야 한다 (`n_new = n_old·(230000/240000) + 12.5`). 2026-09-04 에
#   25 개 데이터셋 339,845 프레임을 변환했다. 여기만 고치고 데이터를 두면
#   학습된 정책이 j6 에서 10~20° 어긋난다.
JOINT_CALIBRATION: dict[str, tuple[int, int]] = {
    "joint1": (-150000, 150000),
    "joint2": (0, 180000),
    "joint3": (-170000, 0),
    "joint4": (-100000, 100000),
    "joint5": (-65000, 65000),
    "joint6": (-120000, 120000),
    "gripper": (0, 68000),
}

# 정규화 스케일이 다른 관절 — 그리퍼만 0..100, 나머지는 -100..100.
# LeRobot 쪽 `MotorNormMode.RANGE_0_100` vs `RANGE_M100_100` 과 같은 구분이다.
# `wrapper/parking_controller.py` 도 이 스케일을 전제로 동작하므로 바꾸면 안 된다.
_ZERO_TO_100: frozenset[str] = frozenset({"gripper"})

# ── 그리퍼 행정은 **팔마다** 다르다 ──
#
# Piper 그리퍼는 두 가지다 — 소형 70mm, 대형 100mm (piper_sdk `max_range_config`:
# "Small gripper: 70 mm / Large gripper: 100 mm"). 리더·팔로워에 섞여 달릴 수 있다.
# 정규화 값(0..100)은 **행정에 대한 비율**이라 위쪽(슬라이더·정책·안전 필터·파킹)은 그대로
# 두고, raw ↔ 정규화 변환만 팔별 상한을 받는다 (`gripper_store.raw_max_um`).
#
# ⚠ `JOINT_CALIBRATION["gripper"]` 는 **소형(기본)** 이고 그대로 둔다 — vendor 드라이버와
#   대조되는 값이고(`tests/test_joints.py`), 팔별 설정이 없는 팔은 예전과 한 자리도
#   다르지 않아야 한다.
#
# ⚠ 대형의 상한은 **실측 끝(99500µm)보다 2mm 안쪽**(98000)이다. 실기(2026-10-08)에서 대형
#   팔로워가 raw 99500 까지 열렸다 — 공칭 100000 에 두면 "100" 이 끝보다 0.5mm 너머라 완전히
#   열 때마다 스톱을 힘 설정(기본 1 N·m)으로 누른다. 소형이 공칭 70 에 대해 68000 으로 2mm 를
#   남기는 것과 같은 여유다. (같은 때 리더는 104400 까지 읽혔다 — 티칭 핸들은 행정 계수가 따로라
#   기준이 아니다.) 더 열고 싶으면 이 표 한 줄만 고치면 된다.
GRIPPER_STROKES_MM: tuple[int, ...] = (70, 100)
DEFAULT_GRIPPER_STROKE_MM = 70
GRIPPER_RAW_MAX: dict[int, int] = {70: JOINT_CALIBRATION["gripper"][1], 100: 98000}


def _range(name: str, gripper_raw_max: int | None) -> tuple[int, int]:
    mn, mx = JOINT_CALIBRATION[name]
    if name == "gripper" and gripper_raw_max:
        mx = gripper_raw_max
    return mn, mx


def normalize_joint(name: str, raw: float, gripper_raw_max: int | None = None) -> float:
    """raw 엔코더 값 → 정규화 값 (관절 -100..100, 그리퍼 0..100).

    `gripper_raw_max`: 이 팔 그리퍼의 raw 상한(µm). 없으면 소형 기본값.
    """
    mn, mx = _range(name, gripper_raw_max)
    ratio = (raw - mn) / (mx - mn)
    if name in _ZERO_TO_100:
        return round(ratio * 100, 2)
    return round(ratio * 200 - 100, 2)


def denormalize_joint(name: str, norm: float, gripper_raw_max: int | None = None) -> int:
    """정규화 값 → raw 엔코더 값. `normalize_joint` 의 역함수."""
    mn, mx = _range(name, gripper_raw_max)
    if name in _ZERO_TO_100:
        return int(mn + (norm / 100) * (mx - mn))
    return int(mn + ((norm + 100) / 200) * (mx - mn))


def normalize_all(raw: dict[str, float], gripper_raw_max: int | None = None) -> dict[str, float]:
    return {name: normalize_joint(name, value, gripper_raw_max) for name, value in raw.items()}


def denormalize_all(norm: dict[str, float], gripper_raw_max: int | None = None) -> dict[str, int]:
    return {name: denormalize_joint(name, value, gripper_raw_max) for name, value in norm.items()}
