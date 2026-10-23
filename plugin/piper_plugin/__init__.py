"""PIPER 플러그인 SDK (feature/plugins.md §7).

세 층이다 — 자기 수준에 맞는 층을 고른다:

1. `Plugin.loop()`      반복 호출되는 함수 하나. 안에서 뭘 하든 자유
2. `MissionPlugin`      관찰 → 판단 → 실행 → 완료 → 기록 → 리셋 뼈대. 훅만 채운다
3. `self.robot.*` 등    추론 시작·정지, `task`, 판단 LLM, 검출 읽기, 저널 — 위 두 층의 재료

SDK 는 선택이다. 매니페스트의 `entry` 가 그냥 스크립트여도 환경변수(`PIPER_PLUGIN_*`,
`PIPER_API_*`, `PIPER_REDIS_URL`)만 읽으면 돈다 — `plugins/hello` 가 그 실례.

lerobot 을 import 하지 않는다. 의존은 `piper_bus`·`piper_shm`·numpy·pyyaml·pydantic 뿐이다.
"""

from piper_plugin.env import Env
from piper_plugin.api import Api, ApiUnavailable, Response
from piper_plugin.camera import Camera
from piper_plugin.plugin import Plugin, PluginStop, Topic
from piper_plugin.mission import Decision, MissionPlugin

__all__ = ["Env", "Api", "ApiUnavailable", "Response", "Camera",
           "Plugin", "PluginStop", "Topic", "Decision", "MissionPlugin"]
