"""분류 템플릿 생성기 (feature/scenario-designer.md §5).

라운드의 모양은 셋이다:

- `one` + `chain`   — 물체 n 개를 놓고 하나씩 담는다. 담고 나면 **그 자체가 다음 배치**라
  라운드 하나가 에피소드 n 개다. 남은 물체가 다음 시도의 방해물이 된다.
- `one` + `restore` — 같은 배치에서 목표만 바꿔 k 번. 담은 것은 되돌려 놓는다 (알약 교훈 —
  한 장면에 정답이 하나면 모델이 목표 입력을 무시한다).
- `all`             — 라운드 전체를 순서 규칙대로 분류하는 긴 에피소드 하나.

시도마다 담는 것은 `picks` 다 — `one` 이면 하나, `all` 이면 규칙 순서대로 여럿.

변형은 두 가지 방식으로 들어간다:

- **끼워 넣기**(`empty_target`·`wrong_bin_fix`) — 물체를 더 쓰지 않는 시도를 라운드 중간에 넣는다.
  연쇄 도중의 상태가 곧 그 시도의 시작 상태다.
- **바꾸기**(`recover_*`·`perturb`) — 정상 시도의 시작을 바꾼다. 시도 수는 그대로다.
"""

from __future__ import annotations

import math

from app.services.scenario import ScenarioError
from app.services.scenario.balance import Tally, apportion, pick_least
from app.services.scenario.plan import Context, Generated

INSERTED = ("empty_target", "wrong_bin_fix")
MODIFIERS = ("recover_missed_grasp", "recover_dropped", "perturb")
_HINTS = {"recover_missed_grasp": "missed_grasp", "recover_dropped": "dropped", "perturb": "perturb"}


# ── 입력 ──

def _rules(ctx: Context) -> dict[str, dict]:
    """물체 종류 → {dest, task}. 종류마다 정확히 하나."""
    raw = ctx.fields.get("rules") or []
    need_task = ctx.common.episode_unit == "one"
    out: dict[str, dict] = {}
    for r in raw:
        obj, dest, task = str(r.get("obj", "")), str(r.get("dest", "")), str(r.get("task", "")).strip()
        if not any(i.id == obj for i in ctx.catalog):
            raise ScenarioError(f"규칙에 물체 목록에 없는 종류가 있습니다: {obj}")
        if obj in out:
            raise ScenarioError(f"규칙이 겹칩니다: {obj}")
        if ctx.board.zone(dest) is None:
            # 상자를 지우면 규칙이 옛 id 를 들고 남는다 — 무엇을 고치면 되는지까지 말한다
            raise ScenarioError(f"분류 규칙의 {obj} 가 작업판에 없는 구역 {dest} 를 가리킵니다 — 담을 상자를 다시 고르세요")
        if need_task and not task:
            raise ScenarioError(f"{obj} 의 task 문구가 비어 있습니다")
        out[obj] = {"dest": dest, "task": task}
    missing = [i.id for i in ctx.catalog if i.id not in out]
    if missing:
        raise ScenarioError(f"담을 곳이 정해지지 않은 물체가 있습니다: {', '.join(missing)}")
    return out


def _sizes(ctx: Context) -> list[int]:
    if ctx.common.composition == "fixed":
        return [sum(ctx.common.fixed_composition.values())]
    try:
        sizes = sorted({int(s) for s in str(ctx.fields["round_sizes"]).replace(" ", "").split(",") if s})
    except ValueError:
        raise ScenarioError("라운드 물체 수는 쉼표로 나눈 정수여야 합니다 (예: 3,5)")
    if not sizes or sizes[0] < 1:
        raise ScenarioError("라운드 물체 수는 1 이상이어야 합니다")
    physical = sum(i.count for i in ctx.catalog)
    if sizes[-1] > physical:
        raise ScenarioError(f"라운드 물체 수 {sizes[-1]} 가 실물 개수 합 {physical} 보다 많습니다")
    return sizes


# ── 생성 ──

def generate(ctx: Context) -> Generated:
    board, rng, common = ctx.board, ctx.rng, ctx.common
    rules = _rules(ctx)
    unit = common.episode_unit
    style = ctx.fields["round_style"] if unit == "one" else "all"
    target_by = ctx.fields["target_by"] if unit == "one" else "rule"

    if target_by == "instruction" and ctx.policy_language is False:
        raise ScenarioError(
            f"{common.policy_type} 는 task 문구를 보지 않습니다 — 같은 장면에서 지시만 바꾸면 "
            "모순 데이터가 됩니다. 목표 고르기를 '순서 규칙'으로 바꾸세요")
    if style == "restore" and target_by == "rule":
        raise ScenarioError("같은 배치 반복은 지시문으로 목표를 고를 때만 됩니다 — 규칙이면 매번 같은 것을 집습니다")

    sizes = _sizes(ctx)
    work = board.work_cells
    if sizes[-1] > len(work):
        raise ScenarioError(f"라운드 물체 수 {sizes[-1]} 가 놓을 수 있는 칸 {len(work)} 보다 많습니다")
    k_targets = int(ctx.fields.get("targets_per_layout", 1))
    warnings: list[dict] = []

    def eps_of(size: int) -> int:
        if style == "chain":
            return size
        if style == "restore":
            return min(k_targets, size)
        return 1

    order_idx = {i.id: n for n, i in enumerate(ctx.catalog)}

    def rule_key(p: tuple[str, str]):
        t, c = p
        d = board.dist_to_robot(c)
        if ctx.fields["order_rule"] == "nearest":
            return (d, board.tie_key(c), order_idx[t])
        return (order_idx[t], d, board.tie_key(c))

    def pick(p: tuple[str, str]) -> dict:
        return {"obj": p[0], "cell": p[1], "dest": rules[p[0]]["dest"]}

    # 세는 것들
    place, target = Tally(), Tally()          # (종류, 칸)
    types, tgt_types = Tally(), Tally()       # 종류 — 놓은 것 / 목표가 된 것
    order = Tally()                           # (순번, 종류)
    size_t, perturb_t, empty_t, wrong_t = Tally(), Tally(), Tally(), Tally()
    nuis = {n["name"]: Tally() for n in common.nuisance}
    tallies = [place, target, types, tgt_types, order, size_t, perturb_t, empty_t, wrong_t, *nuis.values()]

    def compose(size: int) -> list[str]:
        if common.composition == "fixed":
            out = [t for t, n in common.fixed_composition.items() for _ in range(n)]
            rng.shuffle(out)
            return out[:size]                        # 블록 끝의 작은 라운드면 일부만
        if common.composition == "proportional":
            pool = [i.id for i in ctx.catalog for _ in range(i.count)]
            return rng.sample(pool, size)
        inr: dict[str, int] = {}
        out = []
        for _ in range(size):
            cands = [i.id for i in ctx.catalog if inr.get(i.id, 0) < i.count]
            t = pick_least(cands, lambda t: (types.block[t] + inr.get(t, 0),
                                             types.total[t] + inr.get(t, 0)), rng)
            inr[t] = inr.get(t, 0) + 1
            out.append(t)
        return out

    def targets_first(comp: list[str], n: int) -> list[str]:
        """같은 배치 반복 — 목표가 될 n 개를 먼저 고른다(목표 종류 횟수로). 나머지는 방해물."""
        pool, chosen = comp[:], []
        for _ in range(n):
            t = pick_least(sorted(set(pool)), lambda t: (tgt_types.block[t] + chosen.count(t),
                                                        tgt_types.total[t] + chosen.count(t)), rng)
            chosen.append(t)
            pool.remove(t)
        return chosen + pool

    def place_round(comp: list[str], hard: bool, n_targets: int | None = None) -> list[tuple[str, str]]:
        """칸을 고른다. 앞의 `n_targets` 개는 목표 — 목표 칸 횟수로, 나머지는 놓인 횟수로 고른다.

        한 번 놓아 보고 끝내지 않는다. 앞 물체가 가져간 칸 때문에 뒤 물체가 덜 쓴 칸을 못 받는
        일이 생기므로, 순서를 바꿔 여러 번 놓아 보고 점수 합이 가장 작은 배치를 쓴다.
        """
        spacing = common.min_spacing
        n_targets = len(comp) if n_targets is None else n_targets

        def score(t: str, c: str, is_target: bool) -> tuple:
            return (target.score((t, c)), place.score((t, c))) if is_target else ((0, 0), place.score((t, c)))

        best, best_cost = None, None
        tries = 0
        for _ in range(80):
            if tries >= 16:
                break
            tgt, rest = comp[:n_targets], comp[n_targets:]
            rng.shuffle(tgt)
            rng.shuffle(rest)
            seq = [(t, True) for t in tgt] + [(t, False) for t in rest]
            used: list[tuple[str, str]] = []
            cost = [0, 0, 0, 0]
            for i, (t, is_target) in enumerate(seq):
                taken = {c for _, c in used}
                # 어려운 배치: 두 번째 물체를 첫 번째 바로 옆에 붙인다 (간격 조건은 그 둘 사이만 푼다)
                pair = hard and i == 1
                cands = [
                    c for c in work if c not in taken
                    and all(board.chebyshev(c, u) >= spacing
                            for j, (_, u) in enumerate(used) if not (pair and j == 0))
                    and (not pair or board.chebyshev(c, used[0][1]) == 1)
                ]
                if not cands:
                    break
                c = pick_least(cands, lambda c: score(t, c, is_target), rng)
                s = score(t, c, is_target)
                cost = [cost[0] + s[0][0], cost[1] + s[0][1], cost[2] + s[1][0], cost[3] + s[1][1]]
                used.append((t, c))
            if len(used) < len(seq):
                continue
            tries += 1
            if best_cost is None or cost < best_cost:
                best, best_cost = used, cost
            if best_cost == [0, 0, 0, 0]:
                break
        if best is None:
            raise ScenarioError(
                f"물체 {len(comp)}개를 놓을 칸이 모자랍니다 — 칸을 늘리거나 최소 간격을 줄이세요")
        for i, p in enumerate(best):
            place.add(p)
            types.add(p[0])
            if i < n_targets:
                target.add(p)
                tgt_types.add(p[0])
        return best

    def repair(blocks: list[list[dict]]) -> None:
        """전체 목표 횟수의 편차를 1 로 — 넘치는 칸의 목표 하나를 모자란 칸으로 옮긴다.

        칸당 평균이 정수로 떨어지면(160 ÷ 20 = 8) 한 칸만 9 가 돼도 다른 칸이 7 이 된다. 탐욕
        배치는 그 마지막 하나를 못 맞추므로 모든 블록의 배치가 끝난 뒤 여기서 메운다. 옮길 물체는
        **어느 블록에서든** 고른다 — 마지막 블록만 보면 넘치는 칸이 하필 그 블록에서는 덜 쓴 칸이라
        옮기는 순간 블록 균형이 깨지는 일이 흔하다. 블록 안 편차가 1 을 넘게 되는 이동과 어려운
        배치의 짝(붙여 놓은 둘)은 건드리지 않는다.
        """
        def counts(rounds: list[dict], t: str) -> dict[str, int]:
            out = dict.fromkeys(work, 0)
            for r in rounds:
                for tt, c in r["placements"][:r["n_targets"]]:
                    if tt == t:
                        out[c] += 1
            return out

        for item in ctx.catalog:
            t = item.id
            per_block = [counts(rounds, t) for rounds in blocks]
            for _ in range(10 * len(work) * len(blocks)):
                totals = {c: sum(b[c] for b in per_block) for c in work}
                hi, lo = max(totals.values()), min(totals.values())
                if hi - lo <= 1:
                    break
                over = {c for c in work if totals[c] == hi}
                under = [c for c in work if totals[c] == lo]
                rng.shuffle(under)
                spots = [(bi, r, idx) for bi, rounds in enumerate(blocks) for r in rounds
                         for idx in range(r["n_targets"])
                         if r["placements"][idx][0] == t and r["placements"][idx][1] in over
                         and not (r["hard"] and idx < 2)]
                rng.shuffle(spots)
                done = False
                for bi, r, idx in spots:
                    c = r["placements"][idx][1]
                    others = [u for j, (_, u) in enumerate(r["placements"]) if j != idx]
                    b = per_block[bi]
                    for u in under:
                        if u in others or any(board.chebyshev(u, o) < common.min_spacing for o in others):
                            continue
                        vals = [b[x] - (x == c) + (x == u) for x in work]
                        if max(vals) - min(vals) > 1:
                            continue
                        r["placements"][idx] = (t, u)
                        b[c] -= 1
                        b[u] += 1
                        for tally in (target, place):
                            tally.total[(t, c)] -= 1
                            tally.total[(t, u)] += 1
                        done = True
                        break
                    if done:
                        break
                if not done:
                    break

    def recount_block(rounds: list[dict]) -> None:
        """블록 안 횟수를 이 블록의 배치로 다시 센다 — 3단계에서 칸을 더 고를 때 (1단계 카운터는
        마지막 블록 것이라 그대로 쓰면 다른 블록의 균형으로 고른다)."""
        for tally in (place, target, types, tgt_types):
            tally.new_block()
        for r in rounds:
            for i, (t, c) in enumerate(r["placements"]):
                place.block[(t, c)] += 1
                types.block[t] += 1
                if i < r["n_targets"]:
                    target.block[(t, c)] += 1
                    tgt_types.block[t] += 1

    def episode(layout, in_zones, picks, task, *, reset: bool) -> dict:
        return {
            "layout": [{"obj": t, "cell": c} for t, c in layout],
            "in_zones": [{"obj": t, "zone": z} for t, z in in_zones],
            "picks": picks, "task": task, "variant": "normal", "hint": None, "reset": reset,
        }

    def build_round(used: list[tuple[str, str]], n_eps: int) -> list[dict]:
        eps: list[dict] = []
        if style == "all":
            seq = sorted(used, key=rule_key)
            for pos, p in enumerate(seq):
                order.add((pos, p[0]))
            eps.append(episode(used, [], [pick(p) for p in seq], ctx.fields["all_task"], reset=True))
            return eps
        if style == "restore":
            # 목표는 앞의 n_eps 개다 (`targets_first` → `place_round`). 여기서는 순서만 고른다.
            chosen: list[tuple[str, str]] = []
            for pos in range(n_eps):
                cands = [p for p in used[:n_eps] if p not in chosen]
                p = pick_least(cands, lambda p: order.score((pos, p[0])), rng)
                chosen.append(p)
                order.add((pos, p[0]))
                eps.append(episode(used, [], [pick(p)], rules[p[0]]["task"], reset=True))
            return eps
        remaining = list(used)
        in_zones: list[tuple[str, str]] = []
        pos = 0
        while remaining:
            if target_by == "instruction":
                p = pick_least(remaining, lambda p: (order.score((pos, p[0])), target.score(p)), rng)
            else:
                p = min(remaining, key=rule_key)
            eps.append(episode(remaining, in_zones, [pick(p)], rules[p[0]]["task"], reset=pos == 0))
            order.add((pos, p[0]))
            remaining.remove(p)
            in_zones.append((p[0], rules[p[0]]["dest"]))
            pos += 1
        return eps

    def insert_empty(rounds: list[dict]) -> bool:
        """지시한 종류가 판에 없는 시도. 연쇄면 남은 물체가 있는 자리에만 넣는다."""
        spots = []
        for r in rounds:
            eps = r["episodes"]
            positions = range(1, len(eps)) if style == "chain" else range(1, len(eps) + 1)
            for j in positions:
                src = eps[j] if j < len(eps) else eps[-1]
                present = {p["obj"] for p in src["layout"]}
                absent = [i.id for i in ctx.catalog if i.id not in present]
                if absent:
                    spots.append((r, j, src, absent))
        if not spots:
            return False
        r, j, src, absent = rng.choice(spots)
        t = pick_least(absent, lambda t: empty_t.score(t), rng)
        empty_t.add(t)
        ep = episode([(p["obj"], p["cell"]) for p in src["layout"]],
                     [(p["obj"], p["zone"]) for p in src["in_zones"]],
                     [{"obj": t, "cell": None, "dest": None}], rules[t]["task"],
                     reset=style == "restore")
        ep["variant"] = "empty_target"
        ep["hint"] = {"kind": "empty_target", "obj": t}
        r["episodes"].insert(j, ep)
        return True

    def insert_wrong(rounds: list[dict]) -> bool:
        """이미 담은 것을 다른 구역에 옮겨 둔 상태에서 시작 → 바로잡기. 연쇄 전용."""
        if len(board.zones) < 2:
            return False
        spots = [(r, j) for r in rounds for j in range(1, len(r["episodes"]) + 1)]
        if not spots:
            return False
        r, j = rng.choice(spots)
        eps = r["episodes"]
        layout, zones_now = _after(eps[j - 1])
        if not zones_now:
            return False
        t, right = zones_now[rng.randrange(len(zones_now))]
        wrong = pick_least([z.id for z in board.zones if z.id != right], lambda z: wrong_t.score(z), rng)
        wrong_t.add(wrong)
        start_zones = list(zones_now)
        start_zones[start_zones.index((t, right))] = (t, wrong)
        ep = episode(layout, start_zones, [{"obj": t, "cell": None, "from_zone": wrong, "dest": right}],
                     rules[t]["task"], reset=False)
        ep["variant"] = "wrong_bin_fix"
        ep["hint"] = {"kind": "wrong_bin_fix", "obj": t, "from": right, "to": wrong}
        eps.insert(j, ep)
        return True

    def modify(rounds: list[dict], n_block: int) -> None:
        want = apportion(n_block, {k: ctx.variants.get(k, 0.0) for k in MODIFIERS})
        pool = [ep for r in rounds for ep in r["episodes"]
                if ep["variant"] == "normal" and ep["picks"] and ep["picks"][0].get("cell")]
        rng.shuffle(pool)
        for kind in MODIFIERS:
            done = 0
            for ep in list(pool):
                if done >= want.get(kind, 0):
                    break
                first = ep["picks"][0]
                hint = {"kind": _HINTS[kind], "obj": first["obj"], "cell": first["cell"]}
                if kind == "perturb":
                    occupied = {p["cell"] for p in ep["layout"]}
                    free = [c for c in work if c not in occupied]
                    far = [c for c in free if board.chebyshev(c, first["cell"]) >= 2] or free
                    if not far:
                        continue
                    to = pick_least(far, lambda c: perturb_t.score(c), rng)
                    perturb_t.add(to)
                    hint["to"] = to
                ep["variant"], ep["hint"] = kind, hint
                pool.remove(ep)
                done += 1
            if done < want.get(kind, 0):
                warnings.append({"code": "variant_short", "variant": kind,
                                 "missing": want[kind] - done})

    hard_ratio = ctx.variants.get("hard_layout", 0.0)

    def add_rounds(rounds: list[dict], count: int) -> None:
        """시도 `count` 개를 채울 만큼 라운드를 만들고 칸을 고른다."""
        made = 0
        while made < count:
            need = count - made
            fitting = [s for s in sizes if eps_of(s) <= need]
            if fitting:
                size = pick_least(fitting, lambda s: size_t.score(s), rng)
            elif style == "chain":
                size = need                          # 블록 끝 — 남은 수만큼 작은 라운드
                warnings.append({"code": "small_round", "size": size})
            else:
                size = sizes[0]
            size_t.add(size)
            n_eps = min(eps_of(size), need)
            n_hard = sum(r["hard"] for r in rounds)
            hard = size >= 2 and n_hard < math.floor(hard_ratio * (len(rounds) + 1) + 0.5)
            comp = compose(size)
            if style == "restore":
                comp = targets_first(comp, n_eps)
            n_t = n_eps if style == "restore" else size
            used = place_round(comp, hard, n_t)
            rounds.append({"placements": used, "hard": hard, "n_targets": n_t, "n_eps": n_eps})
            made += n_eps

    def can_empty(rounds: list[dict]) -> bool:
        """빈손 시도를 넣을 자리가 있나 — 판에 없는 종류가 있는 시작 상태."""
        if len(ctx.catalog) < 2:
            return False
        if style == "chain":
            return any(len(r["placements"]) >= 2 for r in rounds)   # 마지막 하나 남았을 때
        ids = {i.id for i in ctx.catalog}
        return any({t for t, _ in r["placements"]} != ids for r in rounds)

    # 1단계 — 블록마다 라운드의 모양과 배치
    plan_blocks: list[dict] = []
    left = common.episodes
    while left > 0:
        n_block = min(common.block_size, left)
        left -= n_block
        for t in tallies:
            t.new_block()

        ins = apportion(n_block, {k: ctx.variants.get(k, 0.0) for k in INSERTED})
        while sum(ins.values()) > n_block // 2:      # 끼워 넣은 시도가 블록의 절반을 넘지 않게
            ins[max(ins, key=ins.get)] -= 1
        n_core = n_block - sum(ins.values())

        rounds: list[dict] = []
        add_rounds(rounds, n_core)
        # 끼워 넣을 자리가 없으면 그 몫을 지금 정상 라운드로 채운다 — 3단계에서 알면 이미 편차를
        # 메운 뒤라 균형이 다시 깨진다
        if ins.get("empty_target") and not can_empty(rounds):
            warnings.append({"code": "variant_short", "variant": "empty_target",
                             "missing": ins["empty_target"]})
            add_rounds(rounds, ins.pop("empty_target"))
        plan_blocks.append({"n": n_block, "ins": ins, "rounds": rounds})

    # 2단계 — 전체 편차 메우기
    repair([b["rounds"] for b in plan_blocks])

    # 3단계 — 블록마다 에피소드를 조립하고, 변형을 넣고, 섞는다
    episodes_out: list[dict] = []
    rounds_out: list[dict] = []
    round_no = 0
    for block, pb in enumerate(plan_blocks, start=1):
        for t in (order, perturb_t, empty_t, wrong_t, *nuis.values()):
            t.new_block()
        rounds = pb["rounds"]
        for r in rounds:
            r["episodes"] = build_round(r["placements"], r["n_eps"])

        for kind, insert in (("empty_target", insert_empty), ("wrong_bin_fix", insert_wrong)):
            for _ in range(pb["ins"].get(kind, 0)):
                if not insert(rounds):
                    # 넣을 자리가 없다 — 시도 수를 지키려고 물체 하나짜리 라운드로 메운다
                    warnings.append({"code": "variant_short", "variant": kind, "missing": 1})
                    recount_block(rounds)
                    used = place_round(compose(1), False)
                    rounds.append({"placements": used, "hard": False, "n_targets": 1, "n_eps": 1,
                                   "episodes": build_round(used, 1)})

        modify(rounds, pb["n"])
        rng.shuffle(rounds)                          # 순서도 섞는다 (§7-2)

        for r in rounds:
            round_no += 1
            levels = {}
            for n in common.nuisance:
                lv = pick_least(n["levels"], lambda lv: nuis[n["name"]].score(lv), rng)
                nuis[n["name"]].add(lv)
                levels[n["name"]] = lv
            rounds_out.append({
                "round": round_no, "block": block, "hard": r["hard"], "nuisance": levels,
                "placements": [{"obj": t, "cell": c} for t, c in r["placements"]],
            })
            for step, ep in enumerate(r["episodes"], start=1):
                episodes_out.append({
                    "id": f"b{block}-r{round_no}-{step}", "block": block, "round": round_no,
                    "step": step, **ep, "hard": r["hard"], "nuisance": levels,
                })

    return Generated(episodes=episodes_out, rounds=rounds_out, warnings=_dedupe(warnings))


def _after(ep: dict) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """연쇄에서 이 시도가 끝난 직후의 (판, 구역) 상태 — 다음 시도의 시작 상태다."""
    layout = [(p["obj"], p["cell"]) for p in ep["layout"]]
    zones = [(p["obj"], p["zone"]) for p in ep["in_zones"]]
    for p in ep["picks"]:
        if p.get("cell"):                         # 판에서 집어 구역으로
            layout.remove((p["obj"], p["cell"]))
            zones.append((p["obj"], p["dest"]))
        elif p.get("from_zone"):                  # 잘못 담긴 것을 제 구역으로
            zones[zones.index((p["obj"], p["from_zone"]))] = (p["obj"], p["dest"])
        # 빈손 — 아무것도 안 바뀐다
    return layout, zones


def _dedupe(warnings: list[dict]) -> list[dict]:
    """같은 경고를 블록마다 되풀이하지 않는다 — 개수만 합친다."""
    out: dict[tuple, dict] = {}
    for w in warnings:
        key = (w["code"], w.get("variant"))
        if key in out and "missing" in w:
            out[key]["missing"] += w["missing"]
        elif key not in out:
            out[key] = dict(w)
    return list(out.values())
