"""녹화 중 진행 — 지금 시도와 다음 시도, 그 사이에 옮길 것 (feature/scenario-designer.md §9-2).

wrapper 의 커서 상태(버스 진행 키)와 계획을 합친다. 커서는 "어느 시도" 만 알고, 판을 어떻게
바꿔야 하는지는 여기서 계획끼리 대조해 낸다:

- 앞 시도가 **성공했다고 보고** 그 직후의 판(배치 − 집은 것)과 다음 시도의 배치를 비교한다.
  연쇄면 차이가 없고("그대로"), 되돌리기면 집은 것 하나를 도로 놓는 것, 새 라운드면 거의 전부다.
- 앞 시도를 모르면(세션 첫 시도, 실패 표시, 미루기로 순서가 바뀐 자리) 차이를 못 낸다 —
  `changes=None`, 판 전체를 그림대로 놓는다(`relayout`).
- ← 재녹화면 다음은 같은 시도다. 방금 시도가 성공했다고 보고 그 판을 도로 되돌리는 차이를 낸다.
"""

from __future__ import annotations

from collections import Counter


def initial_state(payload: dict) -> dict:
    """wrapper 가 아직 진행을 안 올렸을 때(유닛 기동 중) — 시작 때 넘긴 것으로 같은 모양을 만든다."""
    done = set(payload.get("done") or ())
    eps = payload["plan"].get("episodes") or []
    queue = [ep["id"] for ep in eps if ep["id"] not in done]
    return {
        "plan_id": payload["plan_id"], "current": None, "current_failed": False, "rerecord": False,
        "next": queue[0] if queue else None, "prev": None, "queue": queue,
        "done": len(eps) - len(queue), "total": len(eps),
        "session": {"saved": 0, "failed": 0, "deferred": 0}, "last": None, "finished": not queue,
    }


def view(payload: dict, state: dict | None) -> dict:
    plan = payload["plan"]
    st = state if state and state.get("plan_id") == payload["plan_id"] else initial_state(payload)
    by_id = {ep["id"]: ep for ep in plan.get("episodes") or []}
    cur = by_id.get(st.get("current") or "")
    nxt = by_id.get(st.get("next") or "")
    prev = by_id.get(st.get("prev") or "")

    if st.get("rerecord"):
        next_pred = cur
    elif cur is not None:
        next_pred = None if st.get("current_failed") else cur
    else:
        next_pred = prev

    blocks = max((ep.get("block") or 0 for ep in by_id.values()), default=0)
    here = cur or nxt
    return {
        "plan_id": payload["plan_id"],
        "template": plan.get("template"),
        "board": plan.get("board"),
        "catalog": plan.get("catalog"),
        "total": st.get("total", len(by_id)),
        "done": st.get("done", 0),
        "remaining": len(st.get("queue") or []) + (1 if cur is not None else 0),
        "block": {"now": here.get("block") if here else None, "total": blocks},
        "current": _trial(cur, prev),
        "next": _trial(nxt, next_pred),
        "current_failed": bool(st.get("current_failed")),
        "rerecord": bool(st.get("rerecord")),
        "finished": bool(st.get("finished")),
        "session": st.get("session") or {},
        "last": st.get("last"),
    }


def _trial(ep: dict | None, pred: dict | None) -> dict | None:
    if ep is None:
        return None
    changes = None if pred is None else diff(after(pred), ep.get("layout") or [])
    return {**ep, "relayout": changes is None, "changes": changes}


def after(ep: dict) -> Counter:
    """이 시도가 성공한 직후의 판 — 배치에서 칸에서 집은 것을 뺀다."""
    board = Counter((p["obj"], p["cell"]) for p in ep.get("layout") or [])
    for p in ep.get("picks") or []:
        if p.get("cell") and board[(p["obj"], p["cell"])] > 0:
            board[(p["obj"], p["cell"])] -= 1
    return +board


def diff(before: Counter, layout: list[dict]) -> list[dict]:
    """`before` 판을 `layout` 으로 만드는 일 — 같은 물체는 치우기+놓기를 옮기기로 묶는다."""
    target = Counter((p["obj"], p["cell"]) for p in layout)
    gone = sorted((before - target).elements())
    new = sorted((target - before).elements())
    out: list[dict] = []
    for obj in sorted({o for o, _ in gone} | {o for o, _ in new}):
        src = [c for o, c in gone if o == obj]
        dst = [c for o, c in new if o == obj]
        for i in range(max(len(src), len(dst))):
            out.append({"obj": obj, "from": src[i] if i < len(src) else None,
                        "to": dst[i] if i < len(dst) else None})
    return out
