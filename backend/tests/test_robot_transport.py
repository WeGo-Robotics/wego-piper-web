"""로봇 shm 전송 — 프록시 드라이버 계약 (refactor/robot-transport.md 3단계).

여기서 지키는 것은 하나다: **프록시로 만든 관측·행동이 직접 드라이버와 같아야 한다.**
어긋나면 프록시로 녹화한 데이터셋과 직접 드라이버로 학습한 정책이 조용히 갈린다 —
에러가 아니라 성능 저하로만 나타나서 원인을 찾기 어렵다.
"""

import ast
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("piper_shm")
pytest.importorskip("lerobot")
lerobot_robot_pipershm = pytest.importorskip("lerobot_robot_pipershm")

from piper_shm import JOINTS, StateWriter  # noqa: E402
from piper_shm import arm as A  # noqa: E402

IFACE = "pytest-can-rt"
POSE = {j: float(i) for i, j in enumerate(JOINTS)}


def _installed_piper_source(filename: str) -> str:
    """**설치된** `lerobot_robot_piper` 의 소스. 리포의 `vendor/` 사본이 아니다.

    ⚠ `vendor/lerobot_robot_piper/` 는 별도 저장소의 사본이고 **설치되지 않는다.**
    거기를 읽으면 실제로 도는 코드와 다른 것을 검사하게 된다.
    """
    spec = importlib.util.find_spec("lerobot_robot_piper")
    return (Path(spec.origin).parent / filename).read_text()


def _dict_literal(src: str, fn_name: str) -> list[str]:
    """함수 안 dict 리터럴 **대입**의 키 순서."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            for n in ast.walk(node):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict):
                    return [k.value for k in n.value.keys]
    raise AssertionError(f"{fn_name} 의 dict 리터럴을 못 찾았다")


def _upstream_bus_kwargs() -> dict[str, dict[str, str]]:
    """상류 `PiperFollower.__init__` 이 `PiperMotorsBus` 에 넘기는 dict 인자들.

    대입이 아니라 **키워드 인자**로 들어가므로 `_dict_literal` 로는 못 잡는다.
    """
    tree = ast.parse(_installed_piper_source("piper_follower.py"))
    init = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    call = next(n for n in ast.walk(init)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "PiperMotorsBus")
    return {
        k.arg: {key.value: ast.unparse(val)
                for key, val in zip(k.value.keys, k.value.values, strict=True)}
        for k in call.keywords if isinstance(k.value, ast.Dict)
    }


def test_motor_specs_match_the_direct_driver():
    """**모터·캘리브레이션이 상류와 같아야 한다.**

    상류(`WeGo-Robotics/lerobot_robot_piper`)가 별도 저장소라 상수를 공유할 수 없어
    `motor_specs.py` 에 복사본을 뒀다. 그 복사가 썩지 않게 여기서 대조한다 —
    상류가 캘리브레이션을 바꾸면 이 테스트가 터진다.
    """
    from lerobot_robot_pipershm.motor_specs import CALIBRATION, MOTORS

    kw = _upstream_bus_kwargs()
    got_motors, got_cal = kw["motors"], kw["calibration"]

    assert list(got_motors) == list(MOTORS), "모터 이름·순서가 상류와 다르다"
    assert list(got_cal) == list(CALIBRATION), "캘리브레이션 키가 상류와 다르다"

    for name, expr in got_motors.items():
        m = MOTORS[name]
        assert expr == f"Motor({m.id}, '{m.model}', MotorNormMode.{m.norm_mode.name})", (
            f"{name} 모터 정의가 상류와 다르다: {expr}"
        )
    for name, expr in got_cal.items():
        c = CALIBRATION[name]
        assert expr == (
            f"MotorCalibration({c.id}, {c.drive_mode}, {c.homing_offset}, "
            f"{c.range_min}, {c.range_max})"
        ), f"{name} 캘리브레이션이 상류와 다르다: {expr}"


def test_joint_order_matches_the_motors_bus():
    """레코드에 이름표가 없으므로 **순서가 곧 의미다.**

    `get_action()` 이 만드는 키 순서와 어긋나면 joint4 자리에 joint5 값이 들어가고,
    아무 예외 없이 팔이 엉뚱한 자세로 간다.
    """
    keys = _dict_literal(_installed_piper_source("motors/piper_motors_bus.py"), "get_action")
    assert tuple(keys) == JOINTS, f"모터 버스와 순서가 다르다: {keys} != {JOINTS}"


def test_proxy_features_equal_the_direct_driver():
    """`observation_features`/`action_features` 가 직접 드라이버와 **완전히** 같아야 한다.

    직접 드라이버는 CAN 을 열어야 만들 수 있으므로 인스턴스를 만들 수 없다.
    대신 두 드라이버가 같은 `bus.motors` 에서 파생한다는 사실을 확인한다 —
    그게 성립하면 feature dict 는 자동으로 같다(그래서 `Robot` 이 아니라
    `MotorsBusBase` 에서 잘랐다).
    """
    from lerobot_robot_piper.piper_follower import PiperFollower
    from lerobot_robot_pipershm import PiperShmFollower
    from lerobot_robot_pipershm.motor_specs import MOTORS

    # 프록시는 **동작을 하나도 오버라이드하지 않는다.** 베껴오는 순간 계약이 갈린다.
    # `config_class`/`name` 은 동작이 아니라 플러그인 등록용 식별자라 예외다.
    IDENTITY = {"config_class", "name"}     # 플러그인 등록용 — 동작이 아니다
    overridden = {
        n for n, v in vars(PiperShmFollower).items()
        if (callable(v) or isinstance(v, property))
        and not n.startswith("__") and n not in IDENTITY
    }
    assert not overridden, f"동작을 오버라이드했다: {overridden}"
    assert "__init__" in vars(PiperShmFollower), "버스를 바꿔 끼우지 않았다"

    for attr in ("observation_features", "action_features", "_motors_ft"):
        assert getattr(PiperShmFollower, attr) is getattr(PiperFollower, attr), (
            f"{attr} 가 상속되지 않았다"
        )

    # feature 키는 `bus.motors` 에서 나온다 — 그 순서까지 같아야 한다
    upstream = list(_upstream_bus_kwargs()["motors"])
    assert [f"{m}.pos" for m in MOTORS] == [f"{k}.pos" for k in upstream]


def test_proxy_bus_covers_everything_the_follower_calls():
    """`PiperFollower` 가 부르는 버스 메서드를 프록시가 **전부** 가져야 한다.

    하나라도 빠지면 추론 도중 `AttributeError` 로 죽는다 — 팔이 움직이는 중에.
    """
    from lerobot_robot_pipershm import PiperShmMotorsBus

    src = _installed_piper_source("piper_follower.py")
    used = {
        node.attr
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute) and node.value.attr == "bus"
    }
    # `motors` 는 메서드가 아니라 `MotorsBusBase.__init__` 이 인스턴스에 두는 속성이라
    # 클래스에는 없다 — 인스턴스를 만들어 확인한다.
    from lerobot_robot_pipershm.motor_specs import CALIBRATION, MOTORS

    probe = PiperShmMotorsBus(id="t", port="pytest-can-probe", motors=dict(MOTORS),
                              calibration=dict(CALIBRATION))
    missing = {name for name in used if not hasattr(probe, name)}
    assert not missing, f"프록시 버스에 없는 것: {sorted(missing)}"


def test_proxy_never_opens_can():
    """프록시가 CAN 을 열면 robotd 와 **같은 버스를 두 프로세스가 만진다.**

    특히 `PiperFollower.__init__` 을 그대로 부르면 `C_PiperInterface_V2(port)` 가
    CAN 을 연다 — 그래서 조부모(`Robot`)를 직접 부른다.
    """
    import inspect

    from lerobot_robot_pipershm import PiperShmFollower

    # **주석이 아니라 import 를 본다** — 설명문에 SDK 이름이 나온다
    pkg = Path(inspect.getfile(PiperShmFollower)).parent
    for path in pkg.glob("*.py"):
        mods = {
            n.module or "" for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.ImportFrom)
        } | {
            a.name for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.Import) for a in n.names
        }
        assert not any(m.startswith("piper_sdk") for m in mods), f"{path.name} 이 CAN SDK 를 연다"

    # `PiperFollower.__init__` 을 부르면 `C_PiperInterface_V2(port)` 가 CAN 을 연다.
    # 조부모(`Robot`)를 직접 불러야 한다.
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(ast.parse(inspect.getsource(PiperShmFollower.__init__).lstrip()))
        if isinstance(n, ast.Call)
    }
    assert "Robot.__init__" in calls, "조부모를 직접 부르지 않는다"
    assert not any(c.startswith("super()") for c in calls), "super() 는 CAN 을 연다"


def test_proxy_reads_state_from_shm():
    """상태를 읽는 경로 전체. robotd 자리에 StateWriter 를 세워 대신 발행한다."""
    from lerobot_robot_pipershm import PiperShmMotorsBus
    from lerobot_robot_pipershm.motor_specs import CALIBRATION, MOTORS

    w = StateWriter(IFACE)
    bus = PiperShmMotorsBus(id="t", port=IFACE, motors=dict(MOTORS),
                            calibration=dict(CALIBRATION))
    try:
        w.publish(POSE, ctrl_mode=0x01)
        bus.connect()
        assert bus.is_connected
        assert bus.get_action() == pytest.approx(POSE)
        assert bus.sync_read("Present_Position") == pytest.approx(POSE)
        assert bus.read("Present_Position", "joint3") == pytest.approx(POSE["joint3"])
    finally:
        bus.disconnect()
        w.close()
        A.unlink(A.segment_name(IFACE, A.KIND_ACTION))


def test_proxy_writes_action_to_shm():
    from lerobot_robot_pipershm import PiperShmMotorsBus
    from lerobot_robot_pipershm.motor_specs import CALIBRATION, MOTORS
    from piper_shm import ActionReader

    w = StateWriter(IFACE)
    bus = PiperShmMotorsBus(id="t", port=IFACE, motors=dict(MOTORS),
                            calibration=dict(CALIBRATION), deadman_ms=123)
    try:
        w.publish(POSE)
        bus.connect()
        goal = {j: v + 5.0 for j, v in POSE.items()}
        bus.set_action(goal, is_conv=True)

        r = ActionReader(IFACE)
        try:
            got = r.read()
            assert got["values"] == pytest.approx(goal)
            assert r.deadman_ms == 123, "소비자가 선언한 데드맨이 안 실렸다"
        finally:
            r.close()
        assert bus.get_control() == pytest.approx(goal)
    finally:
        bus.disconnect()
        w.close()


def test_partial_action_keeps_the_other_joints():
    """일부 관절만 온 명령을 **0으로 채우지 않는다.**

    0은 정규화 좌표의 "가운데"라 그럴듯해 보이고, 그게 명령이 되면 팔이 튄다.
    """
    from lerobot_robot_pipershm import PiperShmMotorsBus
    from lerobot_robot_pipershm.motor_specs import CALIBRATION, MOTORS

    w = StateWriter(IFACE)
    bus = PiperShmMotorsBus(id="t", port=IFACE, motors=dict(MOTORS),
                            calibration=dict(CALIBRATION))
    try:
        w.publish(POSE)
        bus.connect()
        got = bus.set_action({"joint2": 99.0}, is_conv=True)
        assert got["joint2"] == pytest.approx(99.0)
        assert got["joint5"] == pytest.approx(POSE["joint5"]), "안 보낸 관절이 0으로 갔다"
    finally:
        bus.disconnect()
        w.close()
        A.unlink(A.segment_name(IFACE, A.KIND_ACTION))


def test_stale_state_is_an_error_not_a_frozen_pose():
    """robotd 가 죽으면 **읽기가 실패해야 한다.**

    마지막 자세를 계속 돌려주면 정책이 멈춘 팔을 움직이는 팔로 착각하고,
    관측과 실제가 어긋난 채로 계속 명령을 낸다.
    """
    import time

    from lerobot_robot_pipershm import shm_motors_bus as M
    from lerobot_robot_pipershm.motor_specs import CALIBRATION, MOTORS

    w = StateWriter(IFACE)
    bus = M.PiperShmMotorsBus(id="t", port=IFACE, motors=dict(MOTORS),
                              calibration=dict(CALIBRATION))
    old = M.STALE_STATE_S
    try:
        M.STALE_STATE_S = 0.05
        w.publish(POSE)
        bus.connect()
        assert bus.get_action() == pytest.approx(POSE)
        time.sleep(0.12)
        with pytest.raises(ConnectionError, match="묵었습니다"):
            bus.get_action()
    finally:
        M.STALE_STATE_S = old
        bus.disconnect()
        w.close()
        A.unlink(A.segment_name(IFACE, A.KIND_ACTION))


def test_connect_fails_loudly_without_robotd():
    """상태 세그먼트가 없으면 연결이 실패해야 한다.

    조용히 넘어가면 정책이 0 자세를 관측으로 받는다 — 그건 그럴듯해 보인다.
    """
    from lerobot_robot_pipershm import PiperShmMotorsBus
    from lerobot_robot_pipershm.motor_specs import CALIBRATION, MOTORS

    bus = PiperShmMotorsBus(id="t", port="pytest-can-absent", motors=dict(MOTORS),
                            calibration=dict(CALIBRATION))
    with pytest.raises(ConnectionError, match="robotd"):
        bus.connect()
    assert not bus.is_connected


# ── 전송 스위치 배선 (refactor/robot-transport.md 3단계) ──

def test_transport_switch_changes_the_robot_type():
    """스위치 하나로 드라이버가 바뀐다. 되돌리기도 값 하나여야 한다."""
    from app.core.cli_mapping import resolve_robot_type
    from app.core.config import settings

    old = settings.robot_transport
    try:
        settings.robot_transport = "direct"
        assert resolve_robot_type("piper_follower") == "piper_follower"

        settings.robot_transport = "shm"
        assert resolve_robot_type("piper_follower") == "piper_follower_shm"

        # 프록시가 없는 타입은 **그대로 둔다** — 조용히 바꾸면 있지도 않은
        # `robot.type` 으로 subprocess 가 죽는다
        assert resolve_robot_type("piper_leader") == "piper_leader"
        assert resolve_robot_type("so101_follower") == "so101_follower"
    finally:
        settings.robot_transport = old


def test_mapped_types_are_actually_registered():
    """매핑 대상이 **진짜 등록된 `robot.type`** 이어야 한다.

    오타가 있으면 subprocess 가 draccus 파싱에서 죽는데, 그때는 이미 카메라를
    붙잡고 팔을 연 뒤라 원인이 멀리 보인다.
    """
    from lerobot.robots import RobotConfig
    from lerobot.utils.import_utils import register_third_party_plugins

    from app.core.cli_mapping import SHM_ROBOT_TYPES

    register_third_party_plugins()
    known = set(RobotConfig.get_known_choices())
    for src, dst in SHM_ROBOT_TYPES.items():
        assert src in known, f"원본 타입이 등록돼 있지 않다: {src}"
        assert dst in known, f"프록시 타입이 등록돼 있지 않다: {dst}"


def test_preview_and_start_agree_on_the_robot_type():
    """**미리보기가 거짓말하면 안 된다.**

    화면에서 확인한 명령과 실제로 도는 명령이 다르면, 사용자가 검토한 의미가 없다.
    둘 다 같은 조립기(`_build_args_for` / `build_record_args`)를 타야 한다.
    """
    import ast
    import inspect

    from app.core import cli_mapping
    from app.routers import models

    for fn in (models._build_args_for, cli_mapping.build_record_args):
        calls = {
            ast.unparse(n.func)
            for n in ast.walk(ast.parse(inspect.getsource(fn).lstrip()))
            if isinstance(n, ast.Call)
        }
        assert "resolve_robot_type" in calls, f"{fn.__name__} 이 전송 방식을 안 본다"


def test_arms_are_prepared_before_the_args_are_built():
    """`shm` 에서는 게이트웨이가 CAN 을 쥔 채 상태를 발행해야 프록시가 붙는다.

    카메라와 같은 순서 요구다 — 준비가 인자 조립보다 **앞**이어야 한다.
    """
    import ast
    import inspect

    from app.routers import models

    body = ast.parse(inspect.getsource(models.start_inference).lstrip()).body[0]
    order = [
        ast.unparse(n.func)
        for n in ast.walk(body) if isinstance(n, ast.Call)
        and ast.unparse(n.func) in {"prepare_cameras", "prepare_arms", "_build_args_for"}
    ]
    # ast.walk 는 순서를 보장하지 않으므로 줄 번호로 본다
    lines = {
        ast.unparse(n.func): n.lineno
        for n in ast.walk(body) if isinstance(n, ast.Call)
        and ast.unparse(n.func) in {"prepare_cameras", "prepare_arms", "_build_args_for"}
    }
    assert set(lines) == {"prepare_cameras", "prepare_arms", "_build_args_for"}, order
    assert lines["prepare_cameras"] < lines["_build_args_for"], "카메라 준비가 늦다"
    assert lines["prepare_arms"] < lines["_build_args_for"], "팔 준비가 늦다"


# ── robotd 데몬 경계 (refactor/robot-transport.md 4단계) ──

def test_daemon_package_never_imports_the_gateway():
    """데몬이 백엔드에 의존하면 **분리한 의미가 없다.**

    rsd(`piper_rs`)·camerad(`piper_cam`)와 같은 규칙이다. 하나라도 `app.` 을
    import 하면 robotd 를 게이트웨이 없이 못 띄운다.
    """
    import ast
    import importlib.util
    from pathlib import Path

    pkg = Path(importlib.util.find_spec("piper_robot").origin).parent
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text())
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        mods |= {a.name for n in ast.walk(tree)
                 if isinstance(n, ast.Import) for a in n.names}
        leaked = {m for m in mods if m.split(".")[0] == "app"}
        assert not leaked, f"{path.name} 이 게이트웨이를 import 한다: {leaked}"


def test_device_and_config_stay_on_their_own_side():
    """장치는 데몬, 설정은 게이트웨이. **같은 사실이 두 프로세스에 있으면 어긋난다.**

    역할(leader/follower)·슬롯·등록 여부는 사람이 정하는 것이고 CAN 과 무관하다.
    `Arm` 이 그걸 들면 재시작할 때마다 게이트웨이 쪽 값과 갈린다.
    """
    from piper_robot.arm import Arm

    fields = set(Arm.__dataclass_fields__)
    assert not fields & {"role", "slot", "ready", "cameras", "gripper_open_pos"}, (
        f"장치 계층이 설정을 들고 있다: {fields}"
    )
    # 장치가 아는 것은 전부 "팔에 물어봐야 알 수 있는 것"이어야 한다
    assert {"connected", "ctrl_mode", "is_master", "firmware"} <= fields


def test_robotd_exposes_only_what_the_gateway_calls():
    """RPC 화이트리스트가 허브에 실제로 있는 메서드여야 한다.

    오타가 있으면 게이트웨이가 부를 때까지 모른다 — 그것도 팔을 쓰려는 순간에.
    """
    import ast
    from pathlib import Path

    from piper_robot.hub import RobotHub

    src = (Path(__file__).resolve().parents[2] / "daemons" / "robotd.py").read_text()
    methods = next(
        {e.value for e in n.value.elts}
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_METHODS"
    )
    missing = {m for m in methods if not hasattr(RobotHub, m)}
    assert not missing, f"허브에 없는 메서드를 노출한다: {sorted(missing)}"
