"""Vast API 키 — **넣는 길은 있고, 나오는 길은 없다** (feature/vast-training.md §9-1).

## 왜 별도 파일인가

지금까지는 `vastai` CLI 가 제 파일(`~/.config/vastai/vast_api_key`)을 읽었다. 개발
머신에서는 사람이 손으로 `vastai set api-key` 를 한 적이 있어 그게 있었지만, **배포판은
컨테이너라 그 파일이 없다.** 그래서 .120·.44 에서는 준비도가 `api_key ✗` 로 막혔고,
설정할 화면도 없었다 — 넣을 방법이 아예 없는 상태였다.

## ⚠ 키는 **돌려주지 않는다**

게이트웨이는 `/api/ext/v1` 말고는 인증이 없다. LAN 에서 이 포트에 닿는 누구나 응답을
읽으므로, "설정돼 있나" 와 "끝 네 자리" 까지만 말한다. 전체를 보여 주는 화면은 만들지
않는다 — 한 번 보여 주면 그 화면이 곧 유출 경로다.

## ⚠ 저장 자리는 데이터 볼륨이다

`config_dir` 은 재설치를 넘어 남는다(SSH 키가 이미 거기 있다). 이미지 안에 넣으면
릴리스마다 사라지고, 무엇보다 **이미지에 비밀이 실린다.**

## ⚠ `.env` 가 있으면 그쪽이 이긴다

`VAST_API_KEY` 환경변수는 운영자가 배포 수단으로 심는 값이다. 화면에서 저장한 것이
그걸 덮으면, 운영자는 자기가 심은 키가 왜 안 먹는지 알 길이 없다.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: 저장 파일. SSH 키와 같은 디렉토리 아래 — 한 자리에서 백업·이관된다.
FILENAME = "vast_api_key"


def path() -> Path:
    from app.core.config import settings

    return Path(settings.config_dir) / "cloud" / FILENAME


def from_env() -> str:
    """운영자가 심은 키. **이게 있으면 이긴다.**"""
    return (os.environ.get("VAST_API_KEY") or "").strip()


def load() -> str:
    """지금 쓸 키. 없으면 빈 문자열 — 그때는 CLI 가 제 파일을 볼 것이다."""
    env = from_env()
    if env:
        return env
    try:
        p = path()
        return p.read_text().strip() if p.exists() else ""
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("저장된 Vast 키를 못 읽었습니다: %s", exc)
        return ""


def save(key: str) -> None:
    """키를 저장한다. **0600 으로.**

    ⚠ 검증은 호출부가 먼저 한다 — 틀린 키를 저장해 두면 "설정됨" 이라 표시되는데 아무것도
    안 되는, 제일 헷갈리는 상태가 된다.
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("빈 키는 저장하지 않습니다")
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # ⚠ 먼저 만들고 권한을 준 뒤 쓴다 — 쓰고 나서 chmod 하면 그 사이에 읽힐 수 있다.
    p.touch(mode=0o600, exist_ok=True)
    p.chmod(0o600)
    p.write_text(key + "\n")


def clear() -> bool:
    p = path()
    if not p.exists():
        return False
    p.unlink()
    return True


def tail(key: str = "") -> str:
    """끝 네 자리. **이것만 화면으로 나간다.**"""
    k = (key or load()).strip()
    return k[-4:] if len(k) >= 4 else ""


def status() -> dict:
    """설정돼 있나 — 키 자체는 절대 넣지 않는다."""
    env = from_env()
    stored = path().exists()
    return {
        "configured": bool(env or stored),
        # ⚠ 어디서 온 키인지 말해 준다. 화면에서 지웠는데 `.env` 의 것이 남아 계속
        #   되는 상황을 사람이 이해할 수 있어야 한다.
        "source": "env" if env else ("file" if stored else None),
        "tail": tail(),
        "env_wins": bool(env and stored),
    }
