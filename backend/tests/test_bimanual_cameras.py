"""양팔 녹화의 카메라 인자.

⚠ **실제로 났던 고장이다.** 양팔 설정에는 최상위 `cameras` 필드가 없다 —
카메라는 팔별(`left_arm_config.cameras`)로 들어간다. 그런데 요청 본문에 남아
있던 `robot_cameras` 를 양팔 분기가 안 지워서 `--robot.cameras={}` 가 나갔고,
draccus 가 이렇게 죽었다:

    `robot`: The fields `cameras` are not valid for BiPiperShmFollowerConfig

**카메라를 하나도 안 붙여도 그랬다** — 빈 dict 라도 실렸기 때문이다.
"""

import pytest

from app.core.cli_mapping import BIMANUAL_ROBOT_TYPES, build_record_args
from app.routers.recording import _apply_arm_params

BASE = {"repo_id": "a/b", "single_task": "t"}


def _args(params: dict) -> list[str]:
    return build_record_args(_apply_arm_params(dict(params), cam_w=640, cam_h=480, cam_fps=30))


def _bimanual(**over) -> dict:
    return {**BASE, "robot_type": "bi_piper_follower",
            "robot_ports": ["can0", "can1"], "teleop_ports": ["can2", "can3"],
            "robot_cameras": {}, "camera_mapping": {}, **over}


# ── 양팔 ────────────────────────────────────────────────────────────────────

def test_bimanual_never_gets_a_top_level_cameras():
    """카메라가 없어도, 있어도 최상위로는 안 나간다."""
    for mapping in ({}, {"left_top": "/dev/video0", "right_top": "/dev/video2"}):
        args = _args(_bimanual(camera_mapping=mapping))
        assert not [a for a in args if a.startswith("--robot.cameras")], \
            f"camera_mapping={mapping} 에서 최상위 cameras 가 나갔다"


def test_bimanual_cameras_go_per_arm():
    args = _args(_bimanual(camera_mapping={"left_top": "/dev/video0",
                                           "right_top": "/dev/video2"}))
    assert any(a.startswith("--robot.left_arm_config.cameras=") for a in args)
    assert any(a.startswith("--robot.right_arm_config.cameras=") for a in args)


def test_a_stale_robot_cameras_in_the_body_is_dropped():
    """⚠ 프론트가 단팔로 쓰다 양팔로 바꾸면 그 값이 본문에 남아 온다.
    양팔 분기가 지우지 않으면 그대로 CLI 로 나간다 — 원래 고장이 이거다."""
    args = _args(_bimanual(robot_cameras={"top": {"type": "opencv"}}))
    assert not [a for a in args if a.startswith("--robot.cameras")]


# ── 단팔은 그대로 ───────────────────────────────────────────────────────────

def test_single_arm_still_gets_the_top_level_cameras():
    args = _args({**BASE, "robot_type": "piper_follower", "robot_port": "can0",
                  "robot_cameras": {}, "camera_mapping": {"top": "/dev/video0"}})
    assert any(a.startswith("--robot.cameras=") for a in args)


def test_an_empty_dict_is_not_sent_at_all():
    """⚠ LeRobot 쪽 기본값이 어차피 `{}` 라 뜻이 같은데, 실으면 그 필드가 없는
    설정에서 "필드가 유효하지 않다" 로 죽는다."""
    args = _args({**BASE, "robot_type": "piper_follower", "robot_port": "can0",
                  "robot_cameras": {}, "camera_mapping": {}})
    assert not [a for a in args if "cameras" in a]


# ── 타입과 포트 수 ──────────────────────────────────────────────────────────

def test_the_shm_variants_are_bimanual_too():
    """전송 방식을 바꿔도 양팔 판정이 따라와야 한다 — 안 그러면 `_shm` 타입에서
    단팔 인자가 조립된다."""
    assert "bi_piper_follower_shm" in BIMANUAL_ROBOT_TYPES
    assert "bi_piper_follower" in BIMANUAL_ROBOT_TYPES


def test_two_ports_means_two_arm_ports():
    args = _args(_bimanual())
    assert any(a.startswith("--robot.left_arm_config.port=") for a in args)
    assert any(a.startswith("--robot.right_arm_config.port=") for a in args)
    assert not [a for a in args if a.startswith("--robot.port=")]
