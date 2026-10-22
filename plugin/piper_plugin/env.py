"""게이트웨이가 플러그인 프로세스에 넘기는 것 — 전부 환경변수다 (feature/plugins.md §6).

| 변수 | 뜻 |
|---|---|
| `PIPER_PLUGIN_NAME` | 플러그인 이름 (= 폴더 이름 = 유닛 이름의 꼬리) |
| `PIPER_PLUGIN_DIR` | 플러그인 폴더 |
| `PIPER_PLUGIN_RUNTIME_DIR` | 설정·저널·venv 가 사는 곳 (`<plugins_dir>/.runtime/<name>`) |
| `PIPER_PLUGIN_CONFIG` | 설정 JSON — 매니페스트 기본값이 채워져 온다 |
| `PIPER_PLUGIN_CAMERAS` | `{슬롯 키: 스트림 이름}` JSON |
| `PIPER_PLUGIN_HEARTBEAT` | `1` 이면 SDK 가 데드맨 박동을 돈다 (`resources.heartbeat`) |
| `PIPER_API_BASE` | 게이트웨이 주소 (`http://127.0.0.1:8000`) |
| `PIPER_API_TOKEN` | 이번 실행의 범위 토큰 — 끝나면 죽는다 |
| `PIPER_REDIS_URL` | 버스 (`injected_env` 가 모든 자식에게 준다) |
| `PIPER_PLUGIN_RUN_ID` | (미션) 저널 id — 외부 API 의 미션 id 와 같다 |

터미널에서 `python -m piper_plugin run <폴더>` 로 돌릴 때는 같은 변수를 그 명령이 채운다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Env:
    name: str
    dir: Path
    runtime_dir: Path
    config: dict = field(default_factory=dict)
    cameras: dict[str, str] = field(default_factory=dict)
    api_base: str = ""
    token: str = ""
    heartbeat: bool = False
    redis_url: str | None = None
    #: 이번 실행의 저널 id — 게이트웨이가 정해 준다(미션 조회가 이 id 로 온다). 없으면 플러그인이 만든다
    run_id: str = ""

    @classmethod
    def from_environ(cls, environ: dict | None = None) -> "Env":
        e = os.environ if environ is None else environ
        name = e.get("PIPER_PLUGIN_NAME") or Path.cwd().name
        d = Path(e.get("PIPER_PLUGIN_DIR") or Path.cwd())
        rt = Path(e.get("PIPER_PLUGIN_RUNTIME_DIR") or (d / ".runtime"))
        return cls(
            name=name, dir=d, runtime_dir=rt,
            config=_json(e.get("PIPER_PLUGIN_CONFIG")),
            cameras=_json(e.get("PIPER_PLUGIN_CAMERAS")),
            api_base=(e.get("PIPER_API_BASE") or "").rstrip("/"),
            token=e.get("PIPER_API_TOKEN") or "",
            heartbeat=e.get("PIPER_PLUGIN_HEARTBEAT") == "1",
            redis_url=e.get("PIPER_REDIS_URL"),
            run_id=e.get("PIPER_PLUGIN_RUN_ID") or "",
        )

    def to_environ(self) -> dict[str, str]:
        """`from_environ` 의 역 — `python -m piper_plugin run` 이 쓴다."""
        out = {
            "PIPER_PLUGIN_NAME": self.name,
            "PIPER_PLUGIN_DIR": str(self.dir),
            "PIPER_PLUGIN_RUNTIME_DIR": str(self.runtime_dir),
            "PIPER_PLUGIN_CONFIG": json.dumps(self.config, ensure_ascii=False),
            "PIPER_PLUGIN_CAMERAS": json.dumps(self.cameras),
            "PIPER_PLUGIN_HEARTBEAT": "1" if self.heartbeat else "0",
            "PIPER_API_BASE": self.api_base,
            "PIPER_API_TOKEN": self.token,
        }
        if self.redis_url:
            out["PIPER_REDIS_URL"] = self.redis_url
        if self.run_id:
            out["PIPER_PLUGIN_RUN_ID"] = self.run_id
        return out


def _json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}
