"""camerad(v4l2)의 하드웨어 계층.

게이트웨이는 이 패키지를 import 하지 않는다 — 버스 RPC 로만 이야기한다.
`piper_rs` 와 같은 구조·같은 메서드 이름이라 게이트웨이가 균일하게 분기한다.
"""

from piper_cam.hub import V4l2Hub

__all__ = ["V4l2Hub"]
