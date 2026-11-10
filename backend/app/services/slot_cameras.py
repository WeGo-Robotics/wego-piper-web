"""함께 올린 정책이 지금 어느 카메라를 읽나 (feature/multi-policy.md §3-2).

이름이 같으면 같은 카메라다. 첫 정책에 없는 이름은 사람이 고른다 — 같은 카메라를 다른 이름으로 학습했으면
매핑에 있는 그 카메라의 이름(`cameras[이름] = 매핑 이름`), 정말 다른 카메라면 자기 이름 그대로 두고 매핑에 줄을 더한다.
판정은 시작(routers/models.py)과 화면 검증(routers/inference.py)이 같이 쓴다. 이은 이름은 wrapper 가
그 정책에만 단다(`--extra-policies` 의 `cameras`).
"""

from __future__ import annotations


def reads(tags: list[str], cameras: dict[str, str]) -> dict[str, str]:
    """`{정책이 학습 때 부른 카메라 이름: 매핑 이름}` — 잇지 않은 이름은 자기 이름이다. 정책이 안 쓰는 이름은 버린다."""
    return {c: (cameras.get(c) or c) for c in tags}


def aliases(read: dict[str, str]) -> dict[str, str]:
    """wrapper 에 실을 것 — 이름이 다른 것만."""
    return {c: live for c, live in read.items() if live != c}


def problem(name: str, read: dict[str, str], mapped: dict[str, str] | None) -> str | None:
    """같이 돌릴 수 없는 이유, 없으면 None. `mapped` 가 None 이면 매핑은 보지 않는다(매핑 없는 미리보기)."""
    by_live: dict[str, list[str]] = {}
    for c, live in read.items():
        by_live.setdefault(live, []).append(c)
    for live, tags in by_live.items():
        if len(tags) > 1:
            return (f"정책 {name} 의 카메라 {', '.join(tags)} 가 매핑의 한 카메라({live})를 같이 봅니다 — "
                    f"학습 때 서로 다른 카메라였으니 따로 이으세요")
    if mapped is None:
        return None
    missing = [c if live == c else f"{c}→{live}" for c, live in read.items() if live not in mapped]
    if missing:
        return (f"정책 {name} 이 쓰는 카메라({', '.join(missing)})가 매핑에 없습니다 — "
                f"같은 카메라를 다른 이름으로 학습했으면 그 카메라로 잇고, 다른 카메라면 매핑에 더하세요")
    return None


def live_view(read: dict[str, str], live: dict[str, dict]) -> dict[str, dict]:
    """학습 카메라 대조용 — 지금 카메라 정보를 그 정책이 부르는 이름으로 옮긴다."""
    return {c: live[m] for c, m in read.items() if m in live}
