"""시나리오 사이드카 — 계획과 에피소드 결과 (feature/scenario-designer.md §11-1, §8-3).

    <dataset>/meta/piper_scenario.json        계획 (wrapper 가 첫 저장 때 한 번 쓴다)
    <dataset>/meta/piper_scenario_log.jsonl   바인딩·결과 (wrapper) + 결정 (게이트웨이)

쓰는 쪽이 둘이라 파일이 둘이다. 로그는 덧붙이기만 한다 — 한 줄이 한 사건이고, 같은
에피소드는 **나중 줄의 필드가 이긴다**. 결정을 뒤집어도 앞 줄은 남아 이력이 된다.

    {"plan": "…", "trial": "b2-r7-3", "episode_index": 42, "outcome": "success", "at": "…"}
    {"plan": "…", "trial": "b2-r7-1", "episode_index": 43, "outcome": "failed", "decision": "pending", "at": "…"}
    {"episode_index": 43, "decision": "exclude", "by": "training-start", "at": "…"}

wrapper 쪽 쓰기는 [wrapper/scenario_cursor.py](../../../../wrapper/scenario_cursor.py), 에피소드를
지울 때의 번호 당기기는 [phase/piper_phase/sidecar.py](../../../../phase/piper_phase/sidecar.py).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PLAN_FILE = "piper_scenario.json"
LOG_FILE = "piper_scenario_log.jsonl"
DECISIONS = ("include", "exclude")


def plan_id(plan: dict) -> str:
    """같은 입력이면 같은 계획이다(생성기가 결정적) — 입력의 지문이 곧 계획의 이름."""
    keys = ("template", "seed", "board", "catalog", "values")
    body = json.dumps({k: plan.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(body.encode()).hexdigest()[:12]


def read_plan(root: Path) -> dict | None:
    path = Path(root) / "meta" / PLAN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_log(root: Path) -> list[dict]:
    """줄 목록. 깨진 줄은 건너뛴다 — 저장 도중 끊긴 마지막 줄이 나머지를 막으면 안 된다."""
    path = Path(root) / "meta" / LOG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict) and isinstance(line.get("episode_index"), int):
            out.append(line)
    return out


def episodes(root: Path) -> dict[int, dict]:
    """에피소드별로 접은 결과 — 나중 줄의 필드가 이긴다."""
    out: dict[int, dict] = {}
    for line in read_log(root):
        out.setdefault(line["episode_index"], {}).update(line)
    return out


def done_trials(root: Path, pid: str) -> set[str]:
    """이 계획에서 성공으로 저장된 시도 — 이어 찍을 때 큐에서 뺀다."""
    return {e["trial"] for e in episodes(root).values()
            if e.get("plan") == pid and e.get("trial") and e.get("outcome") == "success"}


def pending(root: Path) -> list[int]:
    return sorted(i for i, e in episodes(root).items() if e.get("decision") == "pending")


def excluded(root: Path) -> list[int]:
    return sorted(i for i, e in episodes(root).items() if e.get("decision") == "exclude")


def total_episodes(root: Path) -> int | None:
    try:
        info = json.loads((Path(root) / "meta" / "info.json").read_text(encoding="utf-8"))
        return int(info["total_episodes"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def kept_episodes(root: Path) -> list[int] | None:
    """학습에 넣을 에피소드 번호 — 제외가 없으면 None (거를 것이 없다, 인자를 안 붙인다)."""
    out = set(excluded(root))
    total = total_episodes(root)
    if not out or total is None:
        return None
    return [i for i in range(total) if i not in out]


def summary(root: Path) -> dict | None:
    """데이터셋 목록 배지용. 로그가 없으면 None — 시나리오 없이 찍은 데이터셋이다."""
    eps = episodes(root)
    if not eps:
        return None
    return {
        "bound": sum(1 for e in eps.values() if e.get("trial")),
        "failed": sum(1 for e in eps.values() if e.get("outcome") == "failed"),
        "pending": sum(1 for e in eps.values() if e.get("decision") == "pending"),
        "excluded": sum(1 for e in eps.values() if e.get("decision") == "exclude"),
    }


def decide(root: Path, indices: list[int], decision: str, by: str) -> list[int]:
    """포함·제외를 덧붙인다. 실패 표시가 있는 에피소드만 — 바뀐 번호 목록."""
    if decision not in DECISIONS:
        raise ValueError(decision)
    eps = episodes(root)
    targets = sorted({int(i) for i in indices if eps.get(int(i), {}).get("outcome") == "failed"})
    if not targets:
        return []
    at = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    path = Path(root) / "meta" / LOG_FILE
    with open(path, "a", encoding="utf-8") as f:
        for i in targets:
            f.write(json.dumps({"episode_index": i, "decision": decision, "by": by, "at": at},
                               ensure_ascii=False) + "\n")
    logger.info("시나리오 결정 %s: %s 에피소드 %s (%s)", decision, Path(root).name, targets, by)
    return targets
