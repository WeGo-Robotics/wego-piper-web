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
    sniff_can_ids,
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

    def _classify_master(self, mode_int: int | None = None, rx_interval: float = 0.35) -> None:
        """마스터/슬레이브 판별 — **어떤 프레임이 오는지**로 가른다.

        ⚠ 예전에는 RX **개수**만 셌다: 0.35초 동안 안 늘면 마스터, 늘면 슬레이브.
          그런데 마스터는 사람이 팔을 움직이는 동안 제어지령(0x15x)을 **송신한다** —
          그게 마스터의 정의다. 그 프레임이 호스트 RX 에 잡히므로, **수집하는 내내
          리더가 슬레이브로 표시됐다.** 팔은 멀쩡한데 화면만 뒤집힌 것이라
          토크가 실제로 풀렸을 때와 증상이 같아서 구분이 안 됐다.

        대역이 다르므로 세지 말고 읽는다 (`can.sniff_can_ids`):

          0x2A1~0x2A8  슬레이브의 **주기** 피드백 — 가만히 있어도 계속 온다
          0x150~0x15F  마스터가 움직일 때 보내는 제어지령
          0x2B1~0x2C8  마스터의 오프셋 피드백 (offset 을 준 경우)

        판정 순서가 중요하다. 슬레이브 피드백은 **조건 없이 계속** 오므로 그게
        보이면 슬레이브다. 안 보이면 마스터다 — 조작 중이든 아니든 같은 답이 나온다.

        ⚠ 아무것도 안 올 때도 마스터로 본다. 조용한 마스터와 죽은 팔은 버스만
          봐서는 구분이 안 되는데, 이쪽으로 틀리면 **명령을 안 보내는** 쪽으로
          안전하게 실패한다(`_require_commandable`).

        `ctrl_mode 0x06` 은 그대로 두되 **믿고 기대지 않는다** — 실측하면 마스터가
        `Standby` 를 보고한다. 그래서 예전 규칙이 통째로 RX 개수에 얹혀 있었다.
        """
        if mode_int == 0x06:
            self.is_master = True
            return
        seen = sniff_can_ids(self.iface, duration=rx_interval)
        if seen.get("error"):
            # 버스를 못 들으면 판정하지 않는다 — 추측이 라벨로 굳는 것보다 낫다
            self.is_master = None
            return
        self.is_master = seen["groups"]["slave_fb"] == 0

    def refresh_mode(self, classify: bool = False) -> None:
        """ctrl_mode 텍스트를 라이브 갱신. 마스터/슬레이브는 **요청할 때만.**

        ⚠ **마스터/슬레이브는 폴링으로 확인할 값이 아니다.** 저절로 바뀌지 않는다 —
          우리가 `set_master_slave` 로 세우거나 팔 전원이 끊길 때만 바뀐다. 그런데
          `/robots/current` 가 1초마다 불리면서 매번 다시 판별했고, 판별은 버스를
          0.35초 듣는 일이라 **팔 2대면 매 초의 0.7초를 여기에 쓴다.**

          비싸기만 한 게 아니라 위험하다: 판별이 틀릴 창이 폴링 횟수만큼 열린다.
          실제로 옛 규칙에서 조작 중인 리더가 매 폴링마다 슬레이브로 뒤집혔다.

        그래서 판별은 **연결할 때와 우리가 모드를 세운 직후**에만 한다.
        전원이 끊기면 CAN 도 끊겨 재연결을 타므로 그때 다시 판별된다.

        역할(leader/follower)은 여기서 안 건드린다 — 게이트웨이의 해석이고,
        장치가 덮으면 사용자가 고른 값이 조용히 되돌아간다.
        """
        mode_int = self.refresh_ctrl_mode()
        if classify:
            self._classify_master(mode_int)

    #: 마스터/슬레이브 설정 재시도 횟수. 프레임 하나가 떨어져도 조용히 실패하면
    #: 안 되고, 그렇다고 무한히 시도하면 사람이 화면 앞에서 하염없이 기다린다.
    MS_ATTEMPTS = 3

    def set_master_slave(self, master: bool) -> tuple[bool, str]:
        """팔을 마스터(示教输入臂, 0xFA) 또는 슬레이브(运动输出臂, 0xFC)로 설정.

        설정 후 마스터는 ctrl_mode 0x06(Linkage teaching)을 보고하며 제어지령(0x15x)을
        송신한다. 전원 재투입 시 풀릴 수 있으므로 재설정이 필요할 수 있다.
        """
        want = "마스터" if master else "슬레이브"
        last = ""
        # ⚠ **한 번 보내고 믿지 않는다.** 프레임이 조용히 떨어진 적이 있다 —
        #   2026-09-03 로그에 `MasterSlaveConfig send failed: SEND_MESSAGE_FAILED`
        #   가 아홉 번 찍혔는데 화면에는 전부 성공으로 보였다. 보낸 뒤 팔이 실제로
        #   그 모드인지 보고, 아니면 다시 보낸다.
        for attempt in range(self.MS_ATTEMPTS):
            with self._lock:
                if not self._piper:
                    return False, "연결되지 않음"
                try:
                    self._piper.MasterSlaveConfig(0xFA if master else 0xFC, 0, 0, 0)
                except Exception as e:
                    last = str(e)
                    continue
            time.sleep(0.4)  # 모드 전환 반영 대기
            self.refresh_mode(classify=True)   # 방금 바꿨다 — 여기서는 다시 본다
            # 사용자가 명시적으로 바꿨으므로 역할도 따라간다 — 마스터로 바꿔놓고
            # 화면에 follower 로 남아 있으면 슬롯 배정에서 그대로 어긋난다.
            if self.is_master is master:
                if attempt:
                    logger.warning("%s 모드 설정이 %d번째에 먹었습니다 (%s)",
                                   self.iface, attempt + 1, want)
                return True, "OK"
            last = (f"팔이 {want} 로 바뀌지 않았습니다 "
                    f"(지금 {'마스터' if self.is_master else '슬레이브'})")
        logger.warning("%s 모드 설정 실패 (%s): %s", self.iface, want, last)
        return False, (f"{last}. 명령은 보냈지만 팔이 따라오지 않았습니다 — "
                       f"CAN 연결과 팔 전원을 확인하세요. "
                       f"(전원을 껐다 켜면 이 설정은 풀립니다)")

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
        """관절 raw. **피드백이 기본이고, 지령은 그보다 신선할 때만** 쓴다.

        ⚠ 예전에는 `any(v != 0)` 이기만 하면 지령을 돌려줬다 — 나이를 안 봤다.
        지령 캐시는 한 번 차면 갱신이 없어도 그 값이 남아 있어서, **얼어붙은
        지령이 살아 있는 피드백을 가렸다.** 실측: can0 의 raw 읽기가 joint2·
        joint3 을 0 으로 내면서 나머지는 실제값을 내놨다.

        판정 규칙은 바로 아래 `read_joints_normalized` 의 것과 같아야 한다 —
        같은 사실을 두 함수가 다르게 답하면 어느 쪽이 맞는지 알 길이 없다.
        """
        with self._lock:
            if not self._piper:
                return None
            fb = self._feedback_joints_locked()
            try:
                jm = self._piper.GetArmJointMsgs()
                jc = self._piper.GetArmJointCtrl()
                if fb is None or jc.time_stamp - jm.time_stamp > self._CTRL_FRESHER_S:
                    j = jc.joint_ctrl
                    return [j.joint_1, j.joint_2, j.joint_3,
                            j.joint_4, j.joint_5, j.joint_6]
            except Exception:
                pass
            return fb

    def read_joints_feedback(self) -> list[int] | None:
        """피드백(0x2A5~7)만. **지령 폴백 없음.**

        ⚠ 마스터/슬레이브 판별이 쓴다. 폴백이 존재하는 이유가 "마스터는 피드백을
        안 보내서 얼어붙기 때문" 인데, 판별이 가리려는 것이 정확히 그 조건이다 —
        피드백이 없을 때 지령으로 갈아타면 **재려던 신호가 사라진다.**
        """
        with self._lock:
            if not self._piper:
                return None
            return self._feedback_joints_locked()

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

    # ── 팔이 말하는 운동 상태 ──
    #
    # ⚠ **팔이 IK 실패를 직접 보고한다.** `GetArmStatus()` (CAN 0x2A1) 의
    #   `arm_status` 가 그 자리인데, 우리는 지금까지 같은 메시지에서
    #   `ctrl_mode` 와 `err_code` 만 읽고 이걸 안 봤다.
    #
    #   그래서 말단 명령이 안 먹을 때 이유를 우리가 **추측**했다. 팔은 알고 있다.

    #: `arm_status` 코드 → 사람 말. SDK 문서(0x2A1)에서 그대로 옮겼다.
    MOTION_STATUS = {
        0x00: "정상",
        0x01: "급정지",
        0x02: "IK 해가 없습니다",          # 无解
        0x03: "특이점입니다",              # 奇异点
        0x04: "목표 각도가 관절 한계를 넘습니다",
        0x05: "관절 통신 이상",
        0x06: "관절 브레이크가 안 풀렸습니다",
        0x07: "충돌이 감지됐습니다",
        0x08: "교시 중 과속",
        0x09: "관절 상태 이상",
        0x0A: "기타 이상",
        0x0B: "교시 기록 중", 0x0C: "교시 실행 중", 0x0D: "교시 일시정지",
        0x0E: "주제어 NTC 과열", 0x0F: "방전저항 NTC 과열",
    }
    #: 말단 명령이 **실패한** 상태들. 나머지는 정상이거나 다른 이야기다.
    MOTION_BAD = (0x02, 0x03, 0x04, 0x07)

    def read_motion_status(self) -> dict | None:
        """팔이 보고하는 운동 상태. `None` 이면 못 읽었다."""
        with self._lock:
            if not self._piper:
                return None
            try:
                st = self._piper.GetArmStatus().arm_status
                code = int(st.arm_status)
                return {
                    "code": code,
                    "text": self.MOTION_STATUS.get(code, f"알 수 없음(0x{code:02X})"),
                    "bad": code in self.MOTION_BAD,
                    "mode_feed": int(st.mode_feed),   # 0 = MOVE P, 1 = MOVE J
                }
            except Exception as exc:
                logger.debug("read_motion_status 실패 (%s): %s", self.iface, exc)
                return None

    # ── 하드웨어 영점 ──
    #
    # ⚠ **소프트웨어 영점과 전혀 다른 물건이다. 헷갈리면 팔을 망친다.**
    #
    #   소프트웨어 영점  `joints.JOINT_CALIBRATION` — raw 엔코더 범위를 정규화
    #                    -100..100 으로 옮기는 **우리 파일 안의 표**다. 고쳐도
    #                    팔은 아무것도 모른다. 파킹 자세(`~/.piper/parking/*.json`)
    #                    도 이쪽이다.
    #
    #   하드웨어 영점    `JointConfig(set_zero=0xAE)` — CAN 0x475 로 모터
    #                    드라이버에 지금 위치를 0 으로 **플래시에 굽는다.**
    #                    전원을 꺼도 남고, 되돌리는 명령이 없다.
    #                    raw 값의 의미 자체가 바뀐다.
    #
    # 그래서 이걸 한 번 누르면 **위쪽 소프트웨어 표가 통째로 어긋난다** —
    # 정규화·FK·바닥 필터·이미 녹화한 데이터셋의 뜻까지. 부르는 쪽이 그걸
    # 사용자에게 말해야 한다.

    #: 관절 이름 → 모터 번호 (SDK 규약: 1~6 관절, 7 그리퍼)
    ZERO_MOTOR = {"joint1": 1, "joint2": 2, "joint3": 3,
                  "joint4": 4, "joint5": 5, "joint6": 6, "gripper": 7}

    #: 마스터 팔이 외부 명령을 무시한다는 안내. 두 곳(토크·영점)이 같은 말을
    #: 해야 하므로 한 곳에 둔다.
    MASTER_IGNORES = (
        "이 팔은 마스터(示教输入臂) 모드입니다 — 외부 제어 명령을 **전부 무시**하므로 "
        "토크 조작도 영점 굽기도 먹지 않습니다. 먼저 슬레이브로 바꾸세요. "
        "(실측: 마스터 팔에 EnablePiper·ModeCtrl·DisableArm 을 보내도 상태가 안 바뀐다)"
    )

    #: 설정 명령 사이의 간격 (초). 공식 예제(`piper_set_joint_zero_cpv.py`)가
    #: 모드 설정 사이에 두는 것과 같은 값이다 — 컨트롤러가 앞 프레임을 처리하기
    #: 전에 다음 것이 오면 조용히 흘린다.
    CONFIG_GAP_S = 0.1

    #: 굽은 뒤 이만큼(raw, 0.001°) 안이면 영점이 옮겨진 것으로 본다.
    #: 실측 성공 사례는 대부분 정확히 0 이었고 가장 큰 것이 281 이었다.
    ZERO_APPLIED_RAW = 1000

    def _settled_raw(self, joint: str, timeout_s: float = 2.0) -> int | None:
        """피드백이 자리 잡을 때까지 기다렸다가 읽는다.

        ⚠ 한 번만 읽고 판단하면 **아직 안 온 갱신을 실패로 읽는다.** 굽기는
        되돌릴 수 없는 조작이라, 성공을 실패라 부르는 쪽도 값이 비싸다.
        """
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            with self._lock:
                v = self._raw_of(joint)
            if v is not None and last is not None and abs(v - last) < 50:
                return v                       # 두 번 연속 같으면 자리 잡았다
            last = v
            time.sleep(0.2)
        return last

    def set_hardware_zero(self, joint: str) -> dict:
        """지금 위치를 그 관절의 **하드웨어 영점**으로 굽는다.

        되돌릴 수 없다. SDK 에 "영점 해제" 명령이 없다 — 되돌리려면 팔을 원래
        자세로 되돌려 놓고 다시 굽는 수밖에 없는데, 그 "원래 자세"를 아무도
        기록해 두지 않았다면 못 찾는다.

        성공 판정은 **팔이 보내는 응답**으로 한다(`is_set_zero_successfully`).
        보내고 성공했다고 치면, CAN 이 반쯤 죽었을 때 조용히 실패한다.
        """
        motor = self.ZERO_MOTOR.get(joint)
        if motor is None:
            return {"ok": False, "error": f"모르는 관절입니다: {joint}"}
        # ⚠ **마스터 팔에는 굽기가 안 먹는다.** 그런데 팔은 성공이라 응답하므로
        #   보내고 나서는 구분이 안 된다 — 보내기 전에 막아야 이유를 말할 수 있다.
        if self.is_master:
            return {"ok": False, "joint": joint, "error": self.MASTER_IGNORES}
        with self._lock:
            if not self._piper:
                return {"ok": False, "error": "연결되지 않음"}
            before = self._raw_of(joint)
            try:
                # ⚠ 먼저 지운다. 안 지우면 **이전 명령의 성공 응답**을 읽고
                #   이번 것도 성공했다고 보고한다.
                # ⚠ 설정 명령을 **몰아 보내지 않는다.** 공식 예제도 설정 사이에
                #   100ms 를 둔다 — 컨트롤러가 앞 프레임을 처리하기 전에 다음
                #   프레임이 오면 조용히 흘린다.
                self._piper.ClearRespSetInstruction()
                time.sleep(self.CONFIG_GAP_S)
                if joint == "gripper":
                    self._piper.GripperCtrl(0, 1000, 0x01, 0xAE)
                else:
                    self._piper.JointConfig(joint_num=motor, set_zero=0xAE)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        time.sleep(0.5)          # 응답이 올 시간. 락 밖에서 쉰다
        with self._lock:
            try:
                resp = self._piper.GetRespInstruction()
                flag = int(resp.instruction_response.is_set_zero_successfully)
            except Exception as exc:
                return {"ok": False, "error": f"응답을 읽지 못했습니다: {exc}"}
        after = self._settled_raw(joint)
        if flag == 1:
            # ⚠ **팔의 "성공" 을 그대로 믿으면 안 된다.** 굽혔다고 응답해 놓고
            #   실제로는 안 굽는 경우가 있다 — 2026-09-03 에 16번을 눌러 전부
            #   `ok` 를 받았는데 하나도 안 굽혔고, 화면에는 아무 신호도 없었다.
            #   영점이 옮겨졌으면 그 관절은 **0 을 보고해야 한다.** 확인할 근거가
            #   바로 옆에 있는데 안 보는 것은 성공을 지어내는 것이다.
            if after is not None and abs(after) > self.ZERO_APPLIED_RAW:
                logger.warning("영점이 안 먹었다: %s %s (모터 %d) raw %s → %s "
                               "(0 이 되어야 한다)", self.iface, joint, motor,
                               before, after)
                return {"ok": False, "joint": joint, "motor": motor,
                        "raw_before": before, "raw_after": after,
                        "error": "팔은 성공이라 응답했지만 값이 0 이 되지 않았습니다 "
                                 f"({before} → {after}). 영점이 실제로는 안 굽혔습니다."}
            logger.warning("하드웨어 영점 설정: %s %s (모터 %d) raw %s → %s",
                           self.iface, joint, motor, before, after)
            return {"ok": True, "joint": joint, "motor": motor,
                    "raw_before": before, "raw_after": after}
        if flag == 0:
            return {"ok": False, "error": "팔이 실패로 응답했습니다", "joint": joint}
        # -1 = 응답 없음. **성공으로 치지 않는다** — 명령이 나갔는지도 모른다.
        return {"ok": False, "joint": joint,
                "error": "팔이 응답하지 않았습니다 — 설정됐는지 확인할 수 없습니다. "
                         "raw 값을 보고 판단하세요.",
                "raw_before": before, "raw_after": after}

    def set_motor_enabled(self, joint: str, enabled: bool) -> dict:
        """그 관절 모터의 토크를 켜고 끈다.

        ⚠ **끄면 그 관절이 중력으로 주저앉는다.** SDK 공식 예제도 영점 굽기 전에
        이걸 하면서 "请保护好机械臂"(팔을 보호하라)라고 적어 둔다. 부르는 쪽이
        사람에게 그 사실을 먼저 말해야 한다.

        영점 굽기에 필요한 절차다 — 모터가 자세를 붙들고 있는 채로 굽기를 보내면
        팔은 성공이라 응답하면서 실제로는 안 굽는다 (2026-09-03 실측 16/16 실패).
        """
        motor = self.ZERO_MOTOR.get(joint)
        if motor is None or joint == "gripper":
            return {"ok": False, "error": f"모터가 없는 관절입니다: {joint}"}
        if self.is_master:
            return {"ok": False, "joint": joint, "error": self.MASTER_IGNORES}
        with self._lock:
            if not self._piper:
                return {"ok": False, "error": "연결되지 않음"}
            try:
                if enabled:
                    self._piper.EnableArm(motor)
                else:
                    # ⚠ **끊기 전에 팔을 CAN 제어 모드로 세운다.** 공식 예제는 절차
                    #   전체를 그 모드에서 한다(`EnablePiper` → `ModeCtrl(0x01,0x01,…)`
                    #   → 대상 모터만 `DisableArm`). Standby 인 팔에 굽기를 보내면
                    #   팔은 성공이라 응답하면서 아무 일도 안 한다 — 2026-09-03 실측.
                    #
                    #   ⚠ 예제의 `JointCtrl(0,0,0,0,0,0)` 은 **따라하지 않는다.**
                    #     그건 팔을 홈 자세로 **움직이라는 명령**이다. 영점을 맞추려
                    #     세워둔 자세가 그 순간 사라진다.
                    self._piper.EnablePiper()
                    time.sleep(self.CONFIG_GAP_S)
                    self._piper.ModeCtrl(0x01, 0x01, 30, 0x00)
                    time.sleep(self.CONFIG_GAP_S)
                    self._piper.DisableArm(motor)
                time.sleep(self.CONFIG_GAP_S)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        logger.warning("모터 토크 %s: %s %s (모터 %d)",
                       "켬" if enabled else "끔", self.iface, joint, motor)
        return {"ok": True, "joint": joint, "motor": motor, "enabled": enabled}

    def motor_enabled(self) -> dict[str, bool]:
        """모터별 **실제** 활성 상태. 기억이 아니라 팔이 말하는 값이다.

        ⚠ 우리가 보낸 명령을 기억해 두면, 팔이 스스로 실능된 경우(에러·전원)를
        놓친다 — 화면은 "토크 켜짐" 인데 관절은 힘이 없는 상태가 된다.
        """
        with self._lock:
            if not self._piper:
                return {}
            try:
                info = self._piper.GetArmLowSpdInfoMsgs()
            except Exception:
                return {}
            out = {}
            for joint, motor in self.ZERO_MOTOR.items():
                m = getattr(info, f"motor_{motor}", None)
                if m is None:
                    continue
                out[joint] = bool(m.foc_status.driver_enable_status)
            return out

    def read_raw_all(self) -> dict[str, int | None]:
        """관절 + 그리퍼의 raw 값. **영점 창이 보는 숫자다.**

        정규화가 아니라 raw 인 이유: 정규화는 `JOINT_CALIBRATION` 을 거친 것이라
        하드웨어 영점을 옮기면 같이 흔들린다. 무엇을 굽는지 보려면 팔이 직접
        말하는 숫자여야 한다.
        """
        with self._lock:
            if not self._piper:
                return {}
            return {j: self._raw_of(j) for j in self.ZERO_MOTOR}

    def _raw_of(self, joint: str) -> int | None:
        """그 관절의 raw 값. 영점이 실제로 옮겨졌는지 눈으로 확인하는 근거다."""
        try:
            if joint == "gripper":
                return int(self._piper.GetArmGripperMsgs().gripper_state.grippers_angle)
            j = self._piper.GetArmJointMsgs().joint_state
            return int(getattr(j, f"joint_{self.ZERO_MOTOR[joint]}"))
        except Exception:
            return None

    # ── 말단 자세 (온보드 IK) ──

    def read_end_pose(self) -> dict[str, int] | None:
        """지금 말단 자세. **SDK 단위**(0.001mm / 0.001도)."""
        with self._lock:
            if not self._piper:
                return None
            try:
                e = self._piper.GetArmEndPoseMsgs().end_pose
                return {"x": e.X_axis, "y": e.Y_axis, "z": e.Z_axis,
                        "rx": e.RX_axis, "ry": e.RY_axis, "rz": e.RZ_axis}
            except Exception:
                return None

    # 말단 이동 속도. **낮게 고정한다** — 관절 필터가 안 걸리는 모드라
    # 속도가 사람이 반응할 수 있는 범위를 넘으면 안 된다.
    END_POSE_SPEED = 20

    #: `EnablePiper()` 를 다시 부르기까지의 최소 간격 (초).
    #
    # ⚠ **매번 부르면 조그가 답답해진다.** 그 뒤에 반영 대기 200ms 가 붙어서
    #   버튼 한 번에 최소 200ms 가 깔린다 — 사람이 연타하는 조작에서 그건
    #   "반응이 느리다"로 느껴진다. 한 번 켜 두면 계속 켜져 있으므로 매번
    #   부를 이유가 없다.
    ENABLE_TTL_S = 5.0

    def _ensure_enabled(self) -> None:
        """최근에 안 켰으면 켠다. 락 안에서 부른다."""
        now = time.monotonic()
        if now - getattr(self, "_enabled_at", 0.0) < self.ENABLE_TTL_S:
            return
        self._piper.EnablePiper()
        time.sleep(0.2)          # 반영 대기 — 켤 때만 낸다
        self._enabled_at = now

    def move_end_pose(self, target: dict[str, int]) -> tuple[bool, str]:
        """말단을 목표로 보낸다. **관절은 팔의 온보드 IK 가 정한다.**

        ⚠ 이 경로는 `safety.filter_goal` 을 **타지 않는다** — 우리가 관절을 안
        정하기 때문이다. 범위 판단은 호출부가 `endpose.step_target` 으로 끝내고
        와야 한다.
        """
        with self._lock:
            if not self._piper:
                return False, "연결되지 않음"
            try:
                self._ensure_enabled()
                # MOVE P = 점대점 말단 제어. 관절 모드(MOVE J)와 다른 모드다.
                self._piper.ModeCtrl(0x01, 0x00, self.END_POSE_SPEED, 0x00)
                self._piper.EndPoseCtrl(target["x"], target["y"], target["z"],
                                        target["rx"], target["ry"], target["rz"])
            except Exception as e:
                return False, f"말단 명령 실패: {e}"
        return True, "OK"

    def stream_end_pose(self, target: dict[str, int]) -> tuple[bool, str]:
        """`move_end_pose` 의 **스트리밍용**. 텔레오퍼레이션 POSE 모드가 쓴다.

        ⚠ `EnablePiper()` 와 그 뒤 200ms 대기를 **빼는 것이 요점이다.** 한 걸음
          조그에서는 그게 맞지만 초당 수십 번 보내는 경로에서는 그 200ms 가
          주기를 통째로 잡아먹는다 — 30Hz 로 보내려는데 한 번에 200ms 를 쉬면
          5Hz 도 안 나온다. 토크는 세션을 열 때 한 번 켠다.

        ⚠ 이 경로도 `safety.filter_goal` 을 **타지 않는다** — 관절을 팔의 온보드
          IK 가 정하기 때문이다. 막는 것은 전부 호출부(작업공간 상자·걸음 상한·
          짐벌락)에 있고, 그게 통과한 뒤에야 명령이 나간다.
        """
        with self._lock:
            if not self._piper:
                return False, "연결되지 않음"
            try:
                # MOVE P = 점대점 말단 제어. 매번 세우는 이유는 관절 명령이
                # 중간에 끼면 모드가 MOVE J 로 바뀌어 있기 때문이다.
                self._piper.ModeCtrl(0x01, 0x00, self.END_POSE_SPEED, 0x00)
                self._piper.EndPoseCtrl(target["x"], target["y"], target["z"],
                                        target["rx"], target["ry"], target["rz"])
            except Exception as e:
                return False, f"말단 명령 실패: {e}"
        return True, "OK"

    # ── 명령 반응으로 마스터/슬레이브 가리기 ──

    # 어느 관절을 건드리나. **손목(joint6)** 이다 — 질량이 가장 작고 팔의 도달
    # 범위가 안 바뀌어서, 주변에 뭐가 있어도 부딪힐 일이 가장 적다.
    PROBE_JOINT = "joint6"
    # 얼마나 움직이나. 정규화 4 = 그 관절 가동범위의 4% 다. 눈에 보일 만큼은
    # 되면서 위험하지 않은 크기.
    PROBE_DELTA_NORM = 4.0
    # 명령이 반영될 시간. 짧으면 슬레이브를 마스터로 오판한다.
    PROBE_SETTLE_S = 1.5

    def probe_command_response(self, on_step=None) -> dict:
        """**이동 명령에 반응하는가**로 마스터/슬레이브를 가린다.

        마스터(示教输入臂)는 외부 제어 명령을 무시하고 피드백도 안 보낸다 —
        명령을 넣어도 움직이지 않고 관절값도 그대로다. 슬레이브는 움직이고
        관절값이 따라온다. 그 차이가 이 판정의 전부다.

        ⚠ **팔이 실제로 움직인다.** 호출부가 사람이 옆에 있는지, 다른 것이 돌고
        있지 않은지를 책임진다(게이트웨이의 배타 가드).

        ⚠ 끝나면 **원래 자세로 되돌린다.** 판별하려고 팔을 옮겨놓고 두면 다음
        작업이 그 자세에서 시작한다.

        `on_step(설명, 남은초)` 를 주면 단계마다 부른다. 이 함수는 몇 초를 조용히
        보내는데, 화면에 아무 변화가 없으면 **멈춘 것과 구분이 안 된다.**
        """
        def step(text: str, remaining: float = 0.0) -> None:
            if on_step:
                on_step(text, remaining)

        step("기준 관절값 읽는 중")
        # ⚠ **피드백만** 본다 — `read_joints_raw` 는 지령이 신선하면 그걸 주는데,
        #   우리가 방금 쓴 지령을 되읽으면 팔이 뭐든 같은 답이 나온다.
        before = self.read_joints_feedback()
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

        step("이동 명령 보내는 중")
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

        # 반응을 기다린다. **남은 시간을 계속 알린다** — 여기가 가장 긴 침묵이고,
        # 사용자가 "지금 뭘 기다리는 건가"를 알아야 하는 구간이다.
        deadline = time.monotonic() + self.PROBE_SETTLE_S
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            step("반응 기다리는 중", round(left, 1))
            time.sleep(0.1)

        step("관절값 다시 읽는 중")
        after = self.read_joints_feedback()

        # 되돌린다. 마스터면 어차피 무시하므로 해로울 게 없다.
        step("원위치로 되돌리는 중")
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

    # 리셋 전후 보고값 차이가 이보다 크면 슬립으로 친다. raw 단위 0.001° —
    # 2000 = 2.0°. 정지 상태의 잡음·양자화는 수십 단위라 여유가 크다.
    SLIP_WARN_RAW = 2000

    def _feedback_joints_locked(self) -> list[int] | None:
        """피드백(0x2A5~7)의 관절 raw. **지령 폴백 없이 피드백만** 본다.

        슬립 계측은 "피드백이 리셋 전에 거짓말하고 리셋 후 실제값에 재동기화되는"
        그 간극을 재는 것이다 — `read_joints_raw` 처럼 지령(0x155~7)을 섞으면
        계측 자체가 무너진다. 호출자가 락을 쥔 상태여야 한다.
        """
        try:
            j = self._piper.GetArmJointMsgs().joint_state
            return [j.joint_1, j.joint_2, j.joint_3, j.joint_4, j.joint_5, j.joint_6]
        except Exception:
            return None

    def clear_error(self) -> dict:
        """팔의 에러를 무조건 클리어한다 (급정지/일시정지 해제 + 전 관절 에러코드 클리어).

        공식 reset 데모(piper_ctrl_reset.py)의 MotionCtrl_1(0x02) '恢复'와
        JointConfig clear_err=0xAE(전 관절 에러코드 클리어)를 함께 보낸다.

        ## 리셋은 슬립 센서다 (piper_sdk #120)

        과부하로 로터↔출력축이 미끄러지면 피드백은 명령값을 따라가며 **조용히
        거짓말한다** — fault 도 없다. 0x150(0x02) 리셋만이 출력축 절대값을 다시
        읽어 보고 프레임을 실제에 재동기화한다. 그래서 **리셋 직전/직후 피드백의
        차이가 곧 그동안 쌓인 슬립**이고, 여기서 그걸 재서 돌려준다(`slip_raw`,
        0.001° 단위). 소프트웨어가 평소에는 볼 수 없는 것을 이 순간에는 본다.
        """
        with self._lock:
            if not self._piper:
                return {"ok": False, "slip_raw": None}
            before = self._feedback_joints_locked()
            try:
                self._piper.MotionCtrl_1(0x02, 0, 0)        # 급정지/일시정지 해제(恢复)
                time.sleep(0.01)
                self._piper.JointConfig(joint_num=7, clear_err=0xAE)  # 전 관절 에러코드 클리어
            except Exception as e:
                logger.error("clear_error failed on %s: %s", self.iface, e)
                return {"ok": False, "slip_raw": None}
        time.sleep(0.3)          # 재동기화된 피드백이 올 시간. 락 밖에서 쉰다
        with self._lock:
            after = self._feedback_joints_locked() if self._piper else None
        slip = [a - b for a, b in zip(after, before)] if before and after else None
        if slip and max(abs(v) for v in slip) >= self.SLIP_WARN_RAW:
            logger.warning("리셋 재동기화 %s: 관절 슬립 감지 raw %s (0.001°) — "
                           "직전까지의 피드백은 실제 자세와 어긋나 있었다", self.iface, slip)
        return {"ok": True, "slip_raw": slip}


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
