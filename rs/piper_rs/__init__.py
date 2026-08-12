"""RealSense 데몬(rsd)의 하드웨어 계층.

게이트웨이는 이 패키지를 import 하지 않는다 — 버스 RPC 로만 이야기한다.
"""

from piper_rs.hub import RealSenseHub, rs_available

__all__ = ["RealSenseHub", "rs_available"]
