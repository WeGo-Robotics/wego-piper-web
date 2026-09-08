"""Feetech STS3215 버스 IO — scservo_sdk 를 얇게 감싼다.

락 하나로 모든 송수신을 직렬화한다 — 상태 스레드와 명령 스레드가 같은
시리얼 포트를 나눠 쓰기 때문이다 (robotd 의 `arm._lock` 과 같은 이유).

레지스터 주소는 STS 시리즈 (lerobot feetech tables 와 대조):
Torque_Enable(40,1) · Goal_Position(42,2) · Present_Position(56,2).
"""

import logging
import threading

logger = logging.getLogger(__name__)

BAUD = 1_000_000
ADDR_TORQUE_ENABLE = 40
ADDR_GOAL_POSITION = 42
ADDR_PRESENT_POSITION = 56

#: STS3215 의 model number — ping 응답 대조용 (실측 777).
STS3215_MODEL = 777


class FeetechError(RuntimeError):
    pass


class FeetechBus:
    """포트 하나 = 팔 하나. 열기/핑/일괄읽기/토크/목표."""

    def __init__(self, port: str) -> None:
        self.port_name = port
        self._lock = threading.Lock()
        import scservo_sdk as scs
        self._scs = scs
        self._port = scs.PortHandler(port)
        self._pkt = scs.PacketHandler(0)          # STS/SMS 프로토콜
        if not self._port.openPort():
            raise FeetechError(f"포트를 열지 못했습니다: {port}")
        if not self._port.setBaudRate(BAUD):
            self._port.closePort()
            raise FeetechError(f"{BAUD}bps 설정 실패: {port}")
        self._sync = scs.GroupSyncRead(self._port, self._pkt,
                                       ADDR_PRESENT_POSITION, 2)

    def close(self) -> None:
        with self._lock:
            try:
                self._port.closePort()
            except Exception:
                pass

    def ping(self, motor_id: int) -> int | None:
        """응답하면 model number, 아니면 None."""
        with self._lock:
            model, res, _ = self._pkt.ping(self._port, motor_id)
        return int(model) if res == self._scs.COMM_SUCCESS else None

    def ping_all(self, ids: list[int]) -> dict[int, int | None]:
        return {mid: self.ping(mid) for mid in ids}

    def setup_sync(self, ids: list[int]) -> None:
        with self._lock:
            self._sync.clearParam()
            for mid in ids:
                self._sync.addParam(mid)
        self._sync_ids = list(ids)

    def sync_read_positions(self) -> dict[int, int] | None:
        """등록된 모터 전부의 현재 위치(틱). 통신 실패면 None — 부분 성공을
        절반의 자세로 발행하면 찢어진 레코드와 같은 병이 된다."""
        with self._lock:
            if self._sync.txRxPacket() != self._scs.COMM_SUCCESS:
                return None
            out: dict[int, int] = {}
            for mid in self._sync_ids:
                if not self._sync.isAvailable(mid, ADDR_PRESENT_POSITION, 2):
                    return None
                out[mid] = int(self._sync.getData(mid, ADDR_PRESENT_POSITION, 2))
        return out

    def set_torque(self, motor_id: int, on: bool) -> bool:
        with self._lock:
            res, err = self._pkt.write1ByteTxRx(
                self._port, motor_id, ADDR_TORQUE_ENABLE, 1 if on else 0)
        return res == self._scs.COMM_SUCCESS and err == 0

    def write_goal(self, motor_id: int, ticks: int) -> bool:
        ticks = max(0, min(4095, int(ticks)))
        with self._lock:
            res, err = self._pkt.write2ByteTxRx(
                self._port, motor_id, ADDR_GOAL_POSITION, ticks)
        return res == self._scs.COMM_SUCCESS and err == 0
