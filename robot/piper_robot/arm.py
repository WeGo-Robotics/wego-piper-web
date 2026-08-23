"""팔 하나 — CAN 을 실제로 만지는 층. robotd 가 소유한다.

`backend/app/services/robot_manager.py` 의 `ArmInfo` 에서 **장치 부분만** 옮겨왔다.
역할(leader/follower)·슬롯·등록 여부·카메라 매핑 같은 **설정**은 게이트웨이에 남는다 —
그건 사람이 정하는 것이고 CAN 과 무관하다. 카메라에서 `CameraInfo` 가 게이트웨이
쪽 기록으로 남고 장치는 데몬이 가진 것과 같은 경계다.

여기 남은 것은 전부 "팔에 물어봐야 알 수 있는 것"이다:
연결·제어모드·마스터/슬레이브·관절·에러·토크·파킹.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from piper_robot.can import (
    DEFAULT_BITRATE,
    _read_can_rx,
    init_can_interface,
)
from piper_robot.joints import denormalize_all, normalize_all

logger = logging.getLogger(__name__)

# 커스텀 파킹 위치 저장 경로. robotd 는 게이트웨이 설정을 못 읽으므로
# **환경변수로 받는다** — 기기별 상태의 단일 경계(`settings.config_dir`)와 같은 곳이다.
CONFIG_DIR = Path(os.environ.get("PIPER_CONFIG_DIR", "~/.piper")).expanduser()


@dataclass
class Arm:
    iface: str
    bus_info: str
    state: str = "DOWN"
    connected: bool = False
    ctrl_mode: str = ""
    is_master: bool | None = None  # 하드웨어 마스터(示教输入)/슬레이브(运动输出) 모드. None=미확인
    firmware: str = ""
    # 내부
    _piper: object = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict:
        """**장치가 아는 것만.** 역할·슬롯·등록 여부·카메라 매핑은 게이트웨이가 붙인다."""
        return {
            "iface": self.iface,
            "bus_info": self.bus_info,
            "state": self.state,
            "connected": self.connected,
            "ctrl_mode": self.ctrl_mode,
            "master_slave": (None if self.is_master is None
                             else "master" if self.is_master else "slave"),
            "firmware": self.firmware,
        }

    def connect(self, bitrate: int = DEFAULT_BITRATE) -> tuple[bool, str]:
        ok, msg = init_can_interface(self.iface, bitrate)
        if not ok:
            return False, msg
        try:
            from piper_sdk import C_PiperInterface_V2
        except ImportError:
            return False, "piper_sdk not installed"
        try:
            piper = C_PiperInterface_V2(self.iface, judge_flag=False, can_auto_init=False)
            piper.CreateCanBus(self.iface)
            piper.ConnectPort(piper_init=False, start_thread=True)
            try:
                piper.SearchPiperFirmwareVersion()
            except Exception:
                pass
            for _ in range(5):
                time.sleep(0.2)
                fw = piper.GetPiperFirmwareVersion()
                if isinstance(fw, str):
                    self.firmware = fw
                    break
            with self._lock:
                self._piper = piper
            mode_int = self.refresh_ctrl_mode()
            if mode_int is None:
                self.ctrl_mode = "?"
            self._classify_master(mode_int)  # RX 유무로 마스터/슬레이브 판별
            # 역할 기본값은 **마스터/슬레이브 판별 결과를 따른다.**
            #
            # 예전에는 `ctrl_mode == 0x06` 만 보고 role 을 정했는데, 마스터가 되는 길은
            # 둘이다 — 0x06 을 보고하거나, RX 가 없거나(피드백을 송신하지 않는 팔).
            # 전원을 껐다 켜면 마스터 설정이 풀려 Standby(0x00)를 보고하므로
            # **RX 규칙으로만 마스터로 잡히고 role 은 follower 로 남았다.**
            # 사용자가 원하면 `set_role()` 로 언제든 바꿀 수 있다.
            # ⚠ **역할을 여기서 정하지 않는다.** `is_master` 는 팔에 물어본 사실이고,
            # 그걸 leader/follower 로 읽는 것은 게이트웨이의 해석이다 —
            # 사용자가 `set_role()` 로 뒤집을 수 있으므로 장치가 소유할 값이 아니다.
            self.connected = True
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def refresh_ctrl_mode(self) -> int | None:
        """GetArmStatus로 현재 제어 모드를 다시 읽어 ctrl_mode 텍스트 갱신.

        반환: mode_int (읽기 실패 시 None — 이 경우 기존 값 유지)
        """
        with self._lock:
            if not self._piper:
                return None
            try:
                ctrl_mode = self._piper.GetArmStatus().arm_status.ctrl_mode
                mode_int = ctrl_mode.value if hasattr(ctrl_mode, "value") else int(ctrl_mode)
            except Exception:
                return None
        mode_names = {
            0x00: "Standby", 0x01: "CAN ctrl", 0x02: "Teaching",
            0x03: "Ethernet", 0x04: "WiFi", 0x05: "Remote ctrl",
            0x06: "Linkage teaching", 0x07: "Offline trajectory",
        }
        self.ctrl_mode = mode_names.get(mode_int, f"0x{mode_int:02X}")
        return mode_int

    def _classify_master(self, mode_int: int | None = None, rx_interval: float = 0.3) -> None:
        """마스터/슬레이브 판별 (사용자 지정 규칙).

        - RX 데이터가 있으면 슬레이브: 슬레이브 팔은 주기 피드백(0x2Ax)을 계속 송신한다.
        - RX 데이터가 없으면 마스터: 마스터(示教输入臂)는 피드백을 송신하지 않아 RX가 비어있다.
        - ctrl_mode 0x06(연동 示教입력)이면 명시적 마스터로 본다.
        """
        rx1 = _read_can_rx(self.iface)
        time.sleep(rx_interval)
        rx2 = _read_can_rx(self.iface)
        self.is_master = (mode_int == 0x06) or (rx2 == rx1)

    def refresh_mode(self) -> None:
        """ctrl_mode 텍스트 + 마스터/슬레이브를 라이브 갱신.

        역할(leader/follower)은 게이트웨이가 해석하므로 여기서 다루지 않는다 —
        `/robots/current` 폴링마다 불리는 함수라, 장치가 역할을 덮으면 사용자가
        고른 값이 조용히 되돌아간다.
        """
        mode_int = self.refresh_ctrl_mode()
        self._classify_master(mode_int)

    def set_master_slave(self, master: bool) -> tuple[bool, str]:
        """팔을 마스터(示教输入臂, 0xFA) 또는 슬레이브(运动输出臂, 0xFC)로 설정.

        설정 후 마스터는 ctrl_mode 0x06(Linkage teaching)을 보고하며 제어지령(0x15x)을
        송신한다. 전원 재투입 시 풀릴 수 있으므로 재설정이 필요할 수 있다.
        """
        with self._lock:
            if not self._piper:
                return False, "연결되지 않음"
            try:
                self._piper.MasterSlaveConfig(0xFA if master else 0xFC, 0, 0, 0)
            except Exception as e:
                return False, str(e)
        time.sleep(0.4)  # 모드 전환 반영 대기
        self.refresh_mode()
        # 사용자가 명시적으로 바꿨으므로 역할도 따라간다 — 마스터로 바꿔놓고
        # 화면에 follower 로 남아 있으면 슬롯 배정에서 그대로 어긋난다.
        return True, "OK"

    def disconnect(self) -> None:
        with self._lock:
            if self._piper:
                try:
                    self._piper.DisconnectPort()
                except Exception:
                    pass
                self._piper = None
            self.connected = False
            self.is_master = None
            self.ctrl_mode = ""

    def read_joints_raw(self) -> list[int] | None:
        with self._lock:
            if not self._piper:
                return None
            try:
                j = self._piper.GetArmJointCtrl().joint_ctrl
                ctrl = [j.joint_1, j.joint_2, j.joint_3, j.joint_4, j.joint_5, j.joint_6]
                if any(v != 0 for v in ctrl):
                    return ctrl
            except Exception:
                pass
            try:
                j = self._piper.GetArmJointMsgs().joint_state
                return [j.joint_1, j.joint_2, j.joint_3, j.joint_4, j.joint_5, j.joint_6]
            except Exception:
                return None

    # 피드백보다 제어지령이 이만큼 신선하면 지령을 상태로 쓴다. 마스터(연동 示教입력)
    # 팔은 피드백(0x2A5~7)을 송신하지 않아 GetArmJointMsgs 가 모드 전환 시점 값으로
    # 얼어붙는다 — 지령(0x155~7·0x159)이 그 팔의 실제 관절 위치다. 슬레이브는
    # 피드백이 항상 이보다 신선하므로 이 분기를 타지 않는다.
    _CTRL_FRESHER_S = 0.5

    def read_joints_normalized(self) -> dict[str, float] | None:
        """정규화된 관절 위치 읽기 (joint1~6 + gripper)."""
        with self._lock:
            if not self._piper:
                return None
            try:
                jm = self._piper.GetArmJointMsgs()
                gm = self._piper.GetArmGripperMsgs()
                j = jm.joint_state
                gripper = float(gm.gripper_state.grippers_angle)
                jc = self._piper.GetArmJointCtrl()
                if jc.time_stamp - jm.time_stamp > self._CTRL_FRESHER_S:
                    j = jc.joint_ctrl
                    gc = self._piper.GetArmGripperCtrl()
                    if gc.time_stamp - gm.time_stamp > self._CTRL_FRESHER_S:
                        gripper = float(gc.gripper_ctrl.grippers_angle)
                raw = {
                    "joint1": float(j.joint_1), "joint2": float(j.joint_2),
                    "joint3": float(j.joint_3), "joint4": float(j.joint_4),
                    "joint5": float(j.joint_5), "joint6": float(j.joint_6),
                    "gripper": gripper,
                }
                return normalize_all(raw)
            except Exception as e:
                logger.debug("read_joints_normalized error: %s", e)
                return None

    def go_parking(self) -> bool:
        """파킹 위치로 이동."""
        with self._lock:
            if not self._piper:
                return False
            try:
                self._piper.EnablePiper()
                time.sleep(0.3)
                # 커스텀 파킹 위치 또는 기본값
                parking_pos = _load_custom_parking(self.iface)
                from lerobot_robot_piper.motors.tables import INITIALIZE_POSITION
                target = parking_pos or INITIALIZE_POSITION
                # set_action 직접 호출 (정규화 값 → raw 변환)
                raw = denormalize_all(target)

                self._piper.ModeCtrl(0x01, 0x01, 30, 0x00)
                self._piper.JointCtrl(
                    raw["joint1"], raw["joint2"], raw["joint3"],
                    raw["joint4"], raw["joint5"], raw["joint6"],
                )
                self._piper.GripperCtrl(abs(raw["gripper"]), 1000, 0x03, 0)
                return True
            except Exception as e:
                logger.error("go_parking error: %s", e)
                return False

    # ── 명령 반응으로 마스터/슬레이브 가리기 ──

    # 어느 관절을 건드리나. **손목(joint6)** 이다 — 질량이 가장 작고 팔의 도달
    # 범위가 안 바뀌어서, 주변에 뭐가 있어도 부딪힐 일이 가장 적다.
    PROBE_JOINT = "joint6"
    # 얼마나 움직이나. 정규화 4 = 그 관절 가동범위의 4% 다. 눈에 보일 만큼은
    # 되면서 위험하지 않은 크기.
    PROBE_DELTA_NORM = 4.0
    # 명령이 반영될 시간. 짧으면 슬레이브를 마스터로 오판한다.
    PROBE_SETTLE_S = 1.5

    def probe_command_response(self) -> dict:
        """**이동 명령에 반응하는가**로 마스터/슬레이브를 가린다.

        마스터(示教输入臂)는 외부 제어 명령을 무시하고 피드백도 안 보낸다 —
        명령을 넣어도 움직이지 않고 관절값도 그대로다. 슬레이브는 움직이고
        관절값이 따라온다. 그 차이가 이 판정의 전부다.

        ⚠ **팔이 실제로 움직인다.** 호출부가 사람이 옆에 있는지, 다른 것이 돌고
        있지 않은지를 책임진다(게이트웨이의 배타 가드).

        ⚠ 끝나면 **원래 자세로 되돌린다.** 판별하려고 팔을 옮겨놓고 두면 다음
        작업이 그 자세에서 시작한다.
        """
        before = self.read_joints_raw()
        if before is None:
            return {"ok": False, "error": "관절값을 읽지 못했습니다"}

        from piper_robot.joints import JOINT_CALIBRATION, denormalize_joint

        idx = int(self.PROBE_JOINT[-1]) - 1
        lo, hi = JOINT_CALIBRATION[self.PROBE_JOINT]
        span = abs(denormalize_joint(self.PROBE_JOINT, 100)
                   - denormalize_joint(self.PROBE_JOINT, 0))
        delta = int(span * self.PROBE_DELTA_NORM / 100)
        # 가동범위 끝에 있으면 반대로 민다 — 끝에서 밀면 안 움직이고,
        # 그걸 "반응 없음"으로 읽으면 슬레이브를 마스터라고 한다.
        target = list(before)
        headroom = max(lo, hi) - before[idx]
        target[idx] += delta if headroom > delta else -delta

        with self._lock:
            if not self._piper:
                return {"ok": False, "error": "연결되지 않음"}
            try:
                self._piper.EnablePiper()
                time.sleep(0.3)
                self._piper.ModeCtrl(0x01, 0x01, 20, 0x00)
                self._piper.JointCtrl(*target)
            except Exception as e:
                return {"ok": False, "error": f"명령 전송 실패: {e}"}

        time.sleep(self.PROBE_SETTLE_S)
        after = self.read_joints_raw()

        # 되돌린다. 마스터면 어차피 무시하므로 해로울 게 없다.
        with self._lock:
            if self._piper:
                try:
                    self._piper.JointCtrl(*before)
                except Exception as e:
                    logger.warning("%s: 원위치 복귀 실패: %s", self.iface, e)

        if after is None:
            return {"ok": False, "error": "명령 후 관절값을 읽지 못했습니다"}
        moved = max(abs(a - b) for a, b in zip(after, before))
        # 명령한 것의 절반은 움직여야 반응으로 본다. 잡음(수십 raw)보다 훨씬 크고,
        # 부하로 목표에 못 미쳐도 걸린다.
        is_master = moved < delta // 2
        return {"ok": True, "is_master": is_master, "moved_raw": moved,
                "commanded_raw": delta, "joint": self.PROBE_JOINT}

    def enable_torque(self) -> bool:
        with self._lock:
            if not self._piper:
                return False
            try:
                self._piper.EnablePiper()
                return True
            except Exception:
                return False

    def disable_torque(self) -> bool:
        with self._lock:
            if not self._piper:
                return False
            try:
                self._piper.DisablePiper()
                return True
            except Exception:
                return False

    # ── 에러 플래그 ──

    # err_code 비트 → 의미 (arm_feedback_status.ErrStatus 참조)
    _ERR_BITS = {
        0: "joint1_comm", 1: "joint2_comm", 2: "joint3_comm",
        3: "joint4_comm", 4: "joint5_comm", 5: "joint6_comm",
        8: "joint1_angle_limit", 9: "joint2_angle_limit", 10: "joint3_angle_limit",
        11: "joint4_angle_limit", 12: "joint5_angle_limit", 13: "joint6_angle_limit",
    }

    def read_error(self) -> dict | None:
        """현재 팔의 에러 코드/플래그를 읽는다. 연결 안 됐거나 실패 시 None."""
        with self._lock:
            if not self._piper:
                return None
            try:
                err_code = int(self._piper.GetArmStatus().arm_status.err_code)
            except Exception as e:
                logger.debug("read_error failed on %s: %s", self.iface, e)
                return None
        flags = [name for bit, name in self._ERR_BITS.items() if err_code & (1 << bit)]
        return {"err_code": err_code, "flags": flags}

    def clear_error(self) -> bool:
        """팔의 에러를 무조건 클리어한다 (급정지/일시정지 해제 + 전 관절 에러코드 클리어).

        공식 reset 데모(piper_ctrl_reset.py)의 MotionCtrl_1(0x02) '恢复'와
        JointConfig clear_err=0xAE(전 관절 에러코드 클리어)를 함께 보낸다.
        """
        with self._lock:
            if not self._piper:
                return False
            try:
                self._piper.MotionCtrl_1(0x02, 0, 0)        # 급정지/일시정지 해제(恢复)
                time.sleep(0.01)
                self._piper.JointConfig(joint_num=7, clear_err=0xAE)  # 전 관절 에러코드 클리어
                return True
            except Exception as e:
                logger.error("clear_error failed on %s: %s", self.iface, e)
                return False


# ── 세션/파킹 저장 경로 ──
SESSION_DIR = CONFIG_DIR
PARKING_DIR = SESSION_DIR / "parking"
ROBOT_SESSION_PATH = SESSION_DIR / "robot_session.json"


def _load_custom_parking(iface: str) -> dict | None:
    path = PARKING_DIR / f"{iface}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _save_custom_parking(iface: str, positions: dict) -> None:
    PARKING_DIR.mkdir(parents=True, exist_ok=True)
    (PARKING_DIR / f"{iface}.json").write_text(json.dumps(positions, indent=2))
    logger.info("Saved custom parking for %s: %s", iface, positions)


# ── Robot Manager ──
