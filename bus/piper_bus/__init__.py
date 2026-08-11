"""piper_bus — 게이트웨이와 데몬이 공유하는 버스 계약.

backend 안이 아니라 최상위에 두는 이유: 데몬이 백엔드를 import 하면 안 된다
(크래시 격리 + 다른 인터프리터).
"""

from piper_bus import contract
from piper_bus.client import Bus, connect, url

__all__ = ["Bus", "connect", "contract", "url"]
