"""로봇 설정 조립 — 로컬(lerobot_wrapper)·gRPC(grpc_wrapper) 공용.

한 곳에 두는 이유: 양팔 조립이 grpc_wrapper 안에 즉석으로 있던 시절,
`lerobot_features` 를 왼팔 기준으로만 서버에 보내는 결함이 그 즉석 코드에
숨어 있었다 (feature/bimanual.md 결함 ①). 조립이 Robot 클래스(bi_piper_*)로
내려간 지금, wrapper 는 설정만 만들고 나머지는 클래스가 한다.

- 포트가 2개면 `bi_` 타입으로 승격한다 (`piper_follower_shm` → `bi_piper_follower_shm`).
- 카메라는 이름 접두사로 팔에 배정한다: `left_*`→왼팔, `right_*`→오른팔,
  무접두사(공용)→왼팔. bi 클래스가 관측 키에 팔 접두사를 도로 붙이므로
  `left_hand`→왼팔 `hand`→관측 `left_hand` 로 왕복이 맞는다.
  backend/recording.py 의 `_split_camera_mapping` 과 같은 규약이다.
"""

import logging
import typing

logger = logging.getLogger(__name__)


def _cam_configs(cameras_cfg: dict) -> dict:
    """`{이름: {type, ...}}` JSON dict → CameraConfig 객체 dict."""
    from lerobot.cameras.configs import CameraConfig

    # 카메라 서브클래스 등록을 위한 import
    try:
        from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig  # noqa: F401
    except ImportError:
        pass
    try:
        from lerobot.cameras.realsense.configuration_realsense import RealSenseCameraConfig  # noqa: F401
    except ImportError:
        pass

    out = {}
    for name, params in cameras_cfg.items():
        p = dict(params)
        cam_type = p.pop("type", "opencv")
        # OpenCV 카메라: V4L2 백엔드 강제 (ANY 사용 시 GStreamer 등에서 실패하는 경우 방지)
        if cam_type == "opencv" and "backend" not in p:
            p["backend"] = 200  # Cv2Backends.V4L2
        out[name] = CameraConfig.get_choice_class(cam_type)(**p)
    return out


def split_cameras_by_arm(cameras_cfg: dict) -> tuple[dict, dict]:
    left, right = {}, {}
    for name, params in cameras_cfg.items():
        if name.startswith("right_"):
            right[name.removeprefix("right_")] = params
        elif name.startswith("left_"):
            left[name.removeprefix("left_")] = params
        else:
            left[name] = params
    return left, right


def build_robot_config(robot_type: str, robot_port: str,
                       robot_ports=None, cameras_cfg: dict | None = None):
    """(RobotConfig 인스턴스, 실제 robot_type) 을 돌려준다.

    `robot_ports` 는 리스트 또는 "left,right" 콤마 문자열 — [왼팔, 오른팔] 순서.
    2개 미만이면 단팔이고 `robot_port` 를 쓴다.
    """
    from lerobot.robots.config import RobotConfig

    if isinstance(robot_ports, str):
        ports = [p.strip() for p in robot_ports.split(",") if p.strip()]
    else:
        ports = list(robot_ports or [])
    cameras_cfg = cameras_cfg or {}

    if len(ports) >= 2:
        bi_type = robot_type if robot_type.startswith("bi_") else f"bi_{robot_type}"
        BiCfg = RobotConfig.get_choice_class(bi_type)
        # 중첩 팔 설정 클래스는 bi 설정의 필드 타입이 정본이다 — 여기서 하드코딩하면
        # shm/direct 를 이 함수가 다시 구분해야 한다
        ArmCfg = typing.get_type_hints(BiCfg)["left_arm_config"]
        left_cams, right_cams = split_cameras_by_arm(cameras_cfg)
        cfg = BiCfg(
            left_arm_config=ArmCfg(port=ports[0], cameras=_cam_configs(left_cams)),
            right_arm_config=ArmCfg(port=ports[1], cameras=_cam_configs(right_cams)),
        )
        logger.info("Bimanual robot: %s (left=%s, right=%s)", bi_type, ports[0], ports[1])
        return cfg, bi_type

    Cfg = RobotConfig.get_choice_class(robot_type)
    cfg = Cfg(port=robot_port)
    if cameras_cfg and hasattr(cfg, "cameras"):
        cfg.cameras = _cam_configs(cameras_cfg)
    return cfg, robot_type
