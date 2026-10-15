"""`/dev/shm` 프레임 전송 — 게이트웨이·camerad·LeRobot 플러그인이 공유하는 계약.

`piper_bus` 와 짝이다: **버스는 제어·메타데이터, shm 은 픽셀만** 나른다
(refactor/camera-transport.md "버스와의 역할 분담").
"""

from piper_shm.arm import (
    JOINTS,
    ActionReader,
    ActionWriter,
    ArmSegmentError,
    StateReader,
    StateWriter,
)
from piper_shm.frames import Publisher, Subscriber
from piper_shm.segment import (
    Layout,
    SegmentError,
    clear_segment_name,
    clear_segment_names,
    dead_segments,
    has_readers,
    list_segments,
    owner_pid,
    rule_segment_name,
    segment_for_camera,
    segment_name,
    segment_overrides,
    segment_path,
    set_segment_name,
    unlink,
)

__all__ = [
    "Publisher", "Subscriber", "Layout", "SegmentError",
    "list_segments", "segment_for_camera", "segment_name", "segment_path", "unlink",
    # 이름 지정 · 발행자 pid (feature/stream-based-cameras.md §5-4)
    "rule_segment_name", "set_segment_name", "clear_segment_name", "clear_segment_names",
    "segment_overrides", "owner_pid", "dead_segments", "has_readers",
    # 팔 — 카메라와 달리 **양방향**이라 이름이 방향을 말한다
    "StateWriter", "StateReader", "ActionWriter", "ActionReader",
    "ArmSegmentError", "JOINTS",
]
