"""정렬 검사 — 태그로 "얼마나 틀어졌나" 재기 (feature/alignment-check.md).

⚠ **이건 재기만 한다.** 틀어진 것을 고치는 건 영점·0x150 리셋의 일이고, 무엇을
고쳐야 하는지는 사람이 판단한다. 여기서 자동으로 보정하면, 슬립을 영점으로
오진해 굽는 그 최악의 실패를 자동화하는 셈이 된다.
"""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException

logger = logging.getLogger(__name__)

DOMAIN = "alignment"

# 자세로 이동한 뒤 정착을 기다리는 시간. 움직이는 중에 찍으면 그 흔들림이
# 그대로 "틀어짐" 으로 기록된다.
SETTLE_S = 1.5

# 목표에 이만큼(정규화) 안까지 오면 도착으로 본다. 관절 하나가 부하로 조금 덜
# 가는 일은 흔하고, 그걸 실패로 만들면 검사가 늘 실패한다.
REACH_TOL = 1.5

# 이동을 포기하는 시간. 못 가면 **재지 않는다** — 엉뚱한 자세에서 잰 값은
# 기준과 비교할 수 없고, 그걸 "틀어졌다" 로 읽으면 오진이다.
MOVE_TIMEOUT_S = 12.0


def _require_arm_idle() -> None:
    """⚠ 팔이 움직이는 중이면 거절한다 — `/zero` 와 같은 판단이다."""
    from app.services.exclusivity import LABELS, Activity, running

    busy = [a for a in (Activity.INFERENCE, Activity.RECORDING, Activity.TELEOP)
            if a in running()]
    if busy:
        names = " · ".join(LABELS[a] for a in busy)
        raise HTTPException(409, f"{names} 실행 중입니다 — 정렬 검사는 팔을 "
                                 f"움직입니다. 먼저 멈추세요.")


def intrinsics_for(cam_id: str):
    """이 카메라의 내부 파라미터. 없으면 None — **지어내지 않는다.**"""
    from app.services.camera_manager import camera_manager
    from app.services.realsense_manager import realsense_hub
    from piper_cam.tags import Intrinsics

    cam = camera_manager.cameras.get(cam_id)
    if cam is None or not cam.connected:
        return None
    raw = realsense_hub._call("intrinsics", cam_id, default=None)
    if not raw:
        return None
    return Intrinsics(fx=raw["fx"], fy=raw["fy"], cx=raw["cx"], cy=raw["cy"],
                      coeffs=tuple(raw.get("coeffs") or ()),
                      model=str(raw.get("model") or ""))


def _frame(cam_id: str):
    """이 카메라의 최신 프레임. shm 에서 읽는다 — 장치를 안 만진다."""
    from piper_shm import Subscriber, segment_for_camera

    sub = Subscriber(segment_for_camera(cam_id))
    try:
        got = sub.read()
    finally:
        sub.close()
    return None if got is None else got[0]


def move_to(iface: str, pose: dict[str, float]) -> None:
    """팔을 이 관절 자세로 보내고 도착을 기다린다.

    ⚠ **`JogSession` 을 거친다.** 그래야 robotd 의 `filter_goal`(바닥·범위·
    변화율·데드맨)이 전부 걸린다. `arm.JointCtrl` 로 직접 보내면 그게 다 빠진다.
    """
    from app.services.jog import jog_session
    from app.services.robot_manager import robot_manager

    arm = robot_manager.arms.get(iface)
    if arm is None or not arm.connected:
        raise HTTPException(404, f"{iface} 가 연결되어 있지 않습니다")
    now = arm.read_joints_normalized()
    if not now:
        raise HTTPException(409, f"{iface} 의 관절값을 읽지 못했습니다")

    jog_session.start(iface, now)
    try:
        jog_session.set_goal(pose)
        deadline = time.monotonic() + MOVE_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(0.2)
            cur = arm.read_joints_normalized() or {}
            gap = max((abs(cur.get(j, 0.0) - v) for j, v in pose.items()),
                      default=99.0)
            if gap <= REACH_TOL:
                time.sleep(SETTLE_S)      # 흔들림이 "틀어짐" 으로 기록되지 않게
                return
        raise HTTPException(409,
            f"{iface} 가 목표 자세에 도달하지 못했습니다 (최대 오차 {gap:.1f}). "
            f"엉뚱한 자세에서 잰 값은 기준과 비교할 수 없어 측정하지 않습니다.")
    finally:
        jog_session.stop()


def observe(check: dict):
    """자세로 이동 → 태그 관찰. `TagPose` 또는 사유가 담긴 HTTPException."""
    from piper_cam.tags import detect

    _require_arm_idle()
    cam_id = check["camera_id"]
    intr = intrinsics_for(cam_id)
    if intr is None:
        raise HTTPException(409,
            f"{cam_id} 의 내부 파라미터를 알 수 없습니다 — 카메라를 연결하세요. "
            f"초점거리를 추측하면 mm 단위 답이 그럴듯한 모양으로 틀립니다.")

    move_to(check["iface"], check["pose"])

    frame = _frame(cam_id)
    if frame is None:
        raise HTTPException(409, f"{cam_id} 의 프레임을 읽지 못했습니다")
    found = detect(frame, intr, float(check["tag_mm"]), check.get("family") or "36h11")
    want = int(check["tag_id"])
    for p in found:
        if p.tag_id == want:
            return p
    seen = ", ".join(str(p.tag_id) for p in found) or "없음"
    raise HTTPException(409,
        f"태그 {want} 가 안 보입니다 (보인 태그: {seen}). 카메라 시야와 조명을 "
        f"확인하세요 — 이 자세에서 태그가 보여야 검사가 됩니다.")
