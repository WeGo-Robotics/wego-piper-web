"""`teleop.type = "so101_leader_shm"` 등록 — SO-101 리더로 Piper 를 녹화한다.

Piper끼리는 마스터가 CAN 으로 직접 지령을 쏘지만, SO-101 리더는 관절 구성이
달라 **매핑**이 필요하다. 릴레이(게이트웨이)가 도는 동안은 녹화가 배타로
막히므로(TELEOP ↔ RECORDING), 녹화 중에는 이 텔레오퍼레이터가 LeRobot
프로세스 안에서 같은 매핑(`piper_so101.relay_map`)을 돌린다 — 쓰는 쪽은
로봇 클래스 하나뿐이라 세그먼트 라이터가 둘이 되지 않는다.
"""

from dataclasses import dataclass

from lerobot.teleoperators.config import TeleoperatorConfig


@TeleoperatorConfig.register_subclass("so101_leader_shm")
@dataclass(kw_only=True)
class So101ShmLeaderConfig(TeleoperatorConfig):
    #: so101d 가 발행하는 팔 이름 (= 세그먼트 이름, 예: so101_leader1)
    port: str
    #: 정합 앵커를 읽을 Piper 팔로워 iface (예: can0) — 녹화 로봇과 같은 팔
    follower: str
    #: 캘리브레이션 이름. 비우면 port 이름으로 찾는다 (위저드가 그 이름으로 저장)
    calib: str = ""

    @property
    def type(self) -> str:
        return self.get_choice_name(self.__class__)
