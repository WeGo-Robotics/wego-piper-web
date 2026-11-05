"""여러 정책을 올려 두고 바꿔 쓴다 — 버스 `policy`·`hold` (feature/multi-policy.md).

추론 프로세스 하나에 체크포인트를 이름 붙여 여럿 올리고, 지금 쓸 것을 바꾼다. 끄고 켜서 바꾸면 정지할 때마다
팔이 원점에 파킹하고 토크가 꺼진다 — 물건을 든 채로는 못 바꾼다.

- `policy: "<이름>"` — 그 정책으로 바꾸고 돈다. 유지도 풀린다. 지금 이름이면 처음부터 다시(큐를 비운다).
- `hold: true` — 새 추론을 띄우지 않고 마지막 액션을 계속 보낸다. 유지하다 끄면 지금 정책을 처음부터 다시.

⚠ 유지는 일시정지가 아니다. 일시정지는 명령을 안 보내서 robotd 데드맨이 **측정한 자세**를 세운다 — 물체에 막혀
벌어진 그리퍼 폭이 목표가 되면 쥐는 힘이 빠질 수 있다. 유지는 쥐라는 마지막 지령을 그대로 보낸다.

갈아끼우는 일은 래퍼 메인 루프가 한다(`pending` → `done`). 이 모듈은 요청과 상태만 든다.
torch 를 import 하지 않는다 — 테스트가 그냥 돈다.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 이름은 로그·텔레메트리·REST 에 그대로 나간다 — 플러그인 이름과 같은 글자만
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ACTION_SPACES = ("absolute", "delta_v1")


def parse_extra(raw: str, first: str) -> list[dict]:
    """`--extra-policies` → `[{name, path, action_space, delta_spec, target_spec}]`. 틀리면 ValueError.

    `first` 는 첫 정책(`--policy-name`)의 이름이다 — 겹치면 어느 쪽으로 바꾸라는 것인지 모른다."""
    if not raw:
        return []
    items = json.loads(raw)
    if not isinstance(items, list):
        raise ValueError("배열이어야 한다")
    seen = {first}
    out = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError(f"정책 항목이 객체가 아니다: {it!r}")
        name, path = str(it.get("name") or ""), str(it.get("path") or "")
        if not NAME_RE.match(name):
            raise ValueError(f"정책 이름이 틀렸다: {name!r} (영숫자·_·- 40자까지)")
        if name in seen:
            raise ValueError(f"정책 이름이 겹친다: {name}")
        if not path:
            raise ValueError(f"정책 {name} 의 경로가 없다")
        space = it.get("action_space") or "absolute"
        if space not in ACTION_SPACES:
            raise ValueError(f"정책 {name} 의 액션 공간을 모른다: {space}")
        seen.add(name)
        out.append({"name": name, "path": path, "action_space": space,
                    "delta_spec": it.get("delta_spec") or {}, "target_spec": it.get("target_spec") or None})
    return out


def action_dim(path: str | Path) -> int:
    """체크포인트 config.json 의 액션 길이. 못 읽으면 0 — 모르는 것은 막지 않는다(허브 repo id 등)."""
    try:
        cfg = json.loads((Path(path) / "config.json").read_text())
    except (OSError, ValueError):
        return 0
    shape = ((cfg.get("output_features") or {}).get("action") or {}).get("shape") or []
    return int(shape[0]) if len(shape) == 1 else 0


class PolicySwitch:
    """버스 요청과 지금 상태. 파라미터 리스너 스레드가 쓰고, 메인 루프가 읽고 갈아끼운다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.names: list[str] = []
        self.active = ""
        self._pending: str | None = None
        self._holding = False

    def attach(self, names: list[str], active: str) -> None:
        with self._lock:
            self.names, self.active = list(names), active

    def update_param(self, key: str, value) -> bool:
        """`policy`·`hold` 면 받아 들고 True. 모르는 이름도 True — 버리되 다른 키로 흘려보내지 않는다."""
        if key == "policy":
            name = str(value)
            with self._lock:
                if name not in self.names:
                    logger.warning("올리지 않은 정책이라 버린다: %s (있는 것: %s)", name, ", ".join(self.names))
                    return True
                self._pending, self._holding = name, False
            return True
        if key == "hold":
            with self._lock:
                was, self._holding = self._holding, bool(value)
                # 유지가 풀리면 지금 정책을 처음부터 — 유지하는 동안 쌓인 결과는 낡았다
                if was and not self._holding and self._pending is None:
                    self._pending = self.active
            return True
        return False

    def pending(self) -> str | None:
        with self._lock:
            return self._pending

    def done(self, name: str) -> None:
        """메인 루프가 갈아끼웠다. 그 사이 다른 이름이 왔으면 그건 다음 스텝에 한다."""
        with self._lock:
            self.active = name
            if self._pending == name:
                self._pending = None

    @property
    def holding(self) -> bool:
        return self._holding

    def blocks(self) -> bool:
        """새 추론을 띄우면 안 되는가 — 갈아끼울 것이 남았거나 유지 중이다."""
        with self._lock:
            return self._holding or self._pending is not None
