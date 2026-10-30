"""시나리오 설계기 — 계획 생성기 (feature/scenario-designer.md §5·§7·§16 인수 기준).

지키는 것:

1. **고르다** — 종류 × 칸 목표 횟수의 최대−최소가 1 이하, 블록마다도. 모든 등록 템플릿이
   자기 기본값으로 같은 검사를 통과한다(새 템플릿이 균형을 깨면 여기서 잡힌다).
2. **정책이 지시문을 안 보면 지시문 모드를 막는다** — ACT 에 같은 장면·다른 목표를 주면 모순 데이터.
3. **연쇄의 상태가 이어진다** — 끼워 넣은 시도(빈손·바로잡기)가 있어도 다음 시도의 시작 상태가
   앞 시도가 끝난 상태와 같다. 안내 화면의 "옮길 것만" 이 이걸 믿는다.
4. 같은 입력이면 같은 계획 — 계획은 사이드카에 실려 재현돼야 한다.
"""

import pytest

from app.services.scenario import ScenarioError
from app.services.scenario.board import Board
from app.services.scenario.plan import generate
from app.services.scenario.sort import _after
from app.services.scenario.templates import load_templates

BOARD = {
    "cols": 7, "rows": 4, "cell_mm": 80, "blocked": ["G3", "G4"],
    "zones": [
        {"id": "bin_left", "label": "L", "cells": ["A1", "A2"]},
        {"id": "bin_right", "label": "R", "cells": ["G1", "G2"]},
        {"id": "bin_small", "label": "S", "cells": ["A3", "A4"]},
    ],
}
CATALOG = [
    {"id": "red", "label": "red apple", "count": 3},
    {"id": "yellow", "label": "yellow apple", "count": 3},
    {"id": "small", "label": "small apple", "count": 2},
]
RULES = [
    {"obj": "red", "dest": "bin_left", "task": "put the red apple in the left box"},
    {"obj": "yellow", "dest": "bin_right", "task": "put the yellow apple in the right box"},
    {"obj": "small", "dest": "bin_small", "task": "put the small apple in the lower box"},
]

# 템플릿마다 "기본값으로 돌려 볼" 최소 입력. 템플릿을 추가하면 여기도 추가해야 한다 —
# 안 하면 `test_every_template_has_a_fixture` 가 깨진다.
FIXTURES = {
    "sort": {"board": BOARD, "catalog": CATALOG, "values": {"rules": RULES}},
}


def _gen(values=None, seed=42, template="sort"):
    fx = FIXTURES[template]
    return generate(template, fx["board"], fx["catalog"], {**fx["values"], **(values or {})}, seed)


def _assert_balanced(plan):
    s = plan["summary"]
    assert all(v <= 1 for v in s["spread"].values()), s["spread"]
    assert all(v <= 1 for v in s["block_spread"].values()), s["block_spread"]


# ── 1. 고르다 ──

def test_every_template_has_a_fixture():
    assert set(load_templates()) <= set(FIXTURES)


@pytest.mark.parametrize("template", sorted(FIXTURES))
def test_every_template_balances_with_its_defaults(template):
    plan = _gen({"episodes": 120}, template=template)
    assert len(plan["episodes"]) == 120
    _assert_balanced(plan)


@pytest.mark.parametrize("values", [
    {},                                                       # 하나씩 · 연쇄 · 지시문 (기본)
    {"episode_unit": "all"},                                  # 여러 개 연속 분류
    {"round_style": "restore"},                               # 같은 배치에서 목표만 바꾸기
    {"target_by": "rule", "order_rule": "nearest"},
    {"target_by": "rule", "order_rule": "type_priority"},
])
@pytest.mark.parametrize("seed", range(8))
def test_targets_spread_evenly_overall_and_per_block(values, seed):
    """칸당 평균이 정수로 떨어지는 경우(160 ÷ 20)도 포함 — 한 칸만 넘쳐도 편차 2 가 된다."""
    plan = _gen({"episodes": 120, **values}, seed=seed)
    assert len(plan["episodes"]) == 120
    _assert_balanced(plan)


def test_type_totals_are_equal_when_asked():
    totals = _gen({"episodes": 120})["summary"]["type_totals"]
    assert max(totals.values()) - min(totals.values()) <= 1


def test_stopping_after_any_block_is_still_balanced():
    """블록마다 따로 맞춘다 — 80% 찍고 멈춰도 고르다 (§7-4)."""
    plan = _gen({"episodes": 120, "block_size": 20})
    for b in range(1, 7):
        counts: dict = {}
        for ep in plan["episodes"]:
            if ep["block"] == b:
                for p in ep["picks"]:
                    if p.get("cell"):
                        counts[(p["obj"], p["cell"])] = counts.get((p["obj"], p["cell"]), 0) + 1
        for t in ("red", "yellow", "small"):
            vals = [counts.get((t, c), 0) for c in Board.from_dict(BOARD).work_cells]
            assert max(vals) - min(vals) <= 1


def test_same_seed_same_plan_and_different_seed_different_plan():
    a, b, c = _gen(seed=1), _gen(seed=1), _gen(seed=2)
    assert a["episodes"] == b["episodes"]
    assert a["episodes"] != c["episodes"]


def test_plan_carries_its_inputs():
    """사이드카에 실리는 계획은 혼자서 재현돼야 한다 — 입력이 다 들어 있다."""
    plan = _gen({"episodes": 40})
    again = generate(plan["template"], plan["board"], plan["catalog"],
                     {k: v for k, v in plan["values"].items()}, plan["seed"])
    assert again["episodes"] == plan["episodes"]


# ── 2. 정책 ──

def test_policy_that_ignores_task_blocks_instruction_targets():
    with pytest.raises(ScenarioError, match="task 문구를 보지 않습니다"):
        _gen({"policy_type": "act"})
    # 순서 규칙이면 ACT 도 된다
    _gen({"policy_type": "act", "target_by": "rule"})
    # 지시문을 보는 정책은 그대로
    _gen({"policy_type": "smolvla"})


def test_all_unit_always_uses_the_rule_even_for_act():
    plan = _gen({"policy_type": "act", "episode_unit": "all"})
    assert all(len(ep["picks"]) >= 1 for ep in plan["episodes"])


def test_restore_needs_instruction_targets():
    with pytest.raises(ScenarioError, match="같은 배치 반복"):
        _gen({"round_style": "restore", "target_by": "rule"})


# ── 3. 상태 ──

def _rounds(plan):
    out: dict = {}
    for ep in plan["episodes"]:
        out.setdefault(ep["round"], []).append(ep)
    return out.values()


def _state(ep):
    return (sorted((p["obj"], p["cell"]) for p in ep["layout"]),
            sorted((p["obj"], p["zone"]) for p in ep["in_zones"]))


@pytest.mark.parametrize("seed", range(5))
def test_chain_state_carries_over_through_inserted_episodes(seed):
    plan = _gen({"episodes": 120, "variants": {"empty_target": 0.1, "wrong_bin_fix": 0.1}}, seed=seed)
    kinds = {ep["variant"] for ep in plan["episodes"]}
    assert {"empty_target", "wrong_bin_fix"} <= kinds
    for eps in _rounds(plan):
        assert eps[0]["reset"]
        for prev, nxt in zip(eps, eps[1:]):
            layout, zones = _after(prev)
            if nxt["variant"] == "wrong_bin_fix":
                # 시작 전에 사람이 하나를 다른 구역으로 옮겨 둔다 — 그 하나만 다르다
                h = nxt["hint"]
                zones[zones.index((h["obj"], h["from"]))] = (h["obj"], h["to"])
            assert _state(nxt) == (sorted(layout), sorted(zones))


def test_empty_target_asks_for_something_not_on_the_board():
    plan = _gen({"episodes": 120, "variants": {"empty_target": 0.2}})
    empties = [ep for ep in plan["episodes"] if ep["variant"] == "empty_target"]
    assert empties
    for ep in empties:
        assert ep["picks"][0]["obj"] not in {p["obj"] for p in ep["layout"]}
        assert ep["layout"], "빈 판에서 '없다' 는 너무 쉽다 — 남은 물체가 있는 자리에만"


def test_perturb_moves_to_a_free_cell():
    plan = _gen({"episodes": 120, "variants": {"perturb": 0.3}})
    work = set(Board.from_dict(BOARD).work_cells)
    moved = [ep for ep in plan["episodes"] if ep["variant"] == "perturb"]
    assert moved
    for ep in moved:
        to = ep["hint"]["to"]
        assert to in work and to not in {p["cell"] for p in ep["layout"]}


def test_variant_counts_follow_ratios():
    s = _gen({"episodes": 120})["summary"]["variant_counts"]
    assert s["recover_missed_grasp"] == 12 and s["recover_dropped"] == 6
    assert s["perturb"] == 6 and s["empty_target"] == 6


def test_variants_that_do_not_apply_are_reported_not_used():
    s = _gen({"episodes": 60, "target_by": "rule"})["summary"]
    assert "empty_target" in s["variants_off"]
    assert "empty_target" not in s["variant_counts"]


def test_rule_order_is_nearest_first_then_left():
    plan = _gen({"episodes": 60, "target_by": "rule", "order_rule": "nearest", "variants": {}})
    board = Board.from_dict(BOARD)
    for ep in plan["episodes"]:
        key = [(board.dist_to_robot(p["cell"]), board.parse(p["cell"])[0]) for p in ep["layout"]]
        cell = ep["picks"][0]["cell"]
        assert (board.dist_to_robot(cell), board.parse(cell)[0]) == min(key)


def test_all_unit_picks_follow_type_priority():
    plan = _gen({"episodes": 30, "episode_unit": "all"})
    order = {"red": 0, "yellow": 1, "small": 2}
    for ep in plan["episodes"]:
        ranks = [order[p["obj"]] for p in ep["picks"]]
        assert ranks == sorted(ranks)
        assert len(ep["picks"]) == len(ep["layout"])


def test_hard_rounds_put_two_objects_side_by_side():
    plan = _gen({"episodes": 120, "variants": {"hard_layout": 0.5}})
    board = Board.from_dict(BOARD)
    hard = [r for r in plan["rounds"] if r["hard"]]
    assert hard
    for r in hard:
        a, b = r["placements"][0]["cell"], r["placements"][1]["cell"]
        assert board.chebyshev(a, b) == 1


def test_min_spacing_is_kept():
    # 어려운 배치는 일부러 둘을 붙인다 — 간격 조건의 유일한 예외라 여기서는 끈다
    plan = _gen({"episodes": 60, "min_spacing": 2, "round_sizes": "3", "variants": {"hard_layout": 0}})
    board = Board.from_dict(BOARD)
    for r in plan["rounds"]:
        cells = [p["cell"] for p in r["placements"]]
        assert all(board.chebyshev(a, b) >= 2 for i, a in enumerate(cells) for b in cells[i + 1:])


# ── 물체 구성 (§5-3) ──

def test_fixed_composition_is_used_every_full_round():
    plan = _gen({"episodes": 100, "composition": "fixed",
                 "fixed_composition": {"red": 2, "yellow": 2, "small": 1}, "variants": {}})
    full = [r for r in plan["rounds"] if len(r["placements"]) == 5]
    assert full
    for r in full:
        objs = sorted(p["obj"] for p in r["placements"])
        assert objs == ["red", "red", "small", "yellow", "yellow"]


def test_rounds_never_use_more_objects_than_exist():
    for comp in ("equal", "proportional"):
        plan = _gen({"episodes": 120, "composition": comp})
        for r in plan["rounds"]:
            objs = [p["obj"] for p in r["placements"]]
            assert objs.count("small") <= 2 and objs.count("red") <= 3


def test_nuisance_levels_cross_with_target_types():
    s = _gen({"episodes": 120, "nuisance": [{"name": "operator", "levels": ["A", "B"]}]})["summary"]
    tab = s["crosstab"]["operator"]
    for t in ("red", "yellow", "small"):
        assert abs(tab["A"][t] - tab["B"][t]) <= 4


# ── 거절 ──

@pytest.mark.parametrize("values, match", [
    ({"nope": 1}, "모르는 값"),
    ({"rules": RULES[:2]}, "담을 곳이 정해지지 않은"),
    ({"rules": [*RULES[:2], {"obj": "small", "dest": "bin_nowhere", "task": "x"}]}, "작업판에 없는 구역 bin_nowhere"),
    ({"round_sizes": "9"}, "실물 개수 합"),
    ({"round_sizes": "a,b"}, "쉼표로 나눈 정수"),
    ({"composition": "fixed"}, "고정 구성"),
    ({"episode_unit": "both"}, "에피소드 단위"),
    ({"variants": {"teleport": 0.1}}, "없는 변형"),
])
def test_bad_values_are_refused(values, match):
    with pytest.raises(ScenarioError, match=match):
        _gen(values)


def test_too_tight_spacing_is_refused():
    with pytest.raises(ScenarioError, match="자리가 모자랍니다"):
        _gen({"round_sizes": "5", "min_spacing": 3})


def test_board_validation():
    with pytest.raises(ScenarioError, match="겹칩니다"):
        Board.from_dict({**BOARD, "zones": [{"id": "a", "cells": ["A1"]}, {"id": "b", "cells": ["A1"]}]})
    with pytest.raises(ScenarioError, match="없는 칸"):
        Board.from_dict({**BOARD, "blocked": ["Z9"]})
    with pytest.raises(ScenarioError, match="놓을 칸이 없습니다"):
        Board.from_dict({"cols": 1, "rows": 1, "cell_mm": 80, "blocked": ["A1"]})


def test_nearest_rule_ties_are_symmetric_cells():
    """좌우 대칭 칸은 거리가 **정확히** 같아야 "같으면 왼쪽부터" 가 걸린다."""
    b = Board.from_dict(BOARD)
    assert b.dist_to_robot("B1") == b.dist_to_robot("F1")


# ── API ──

def test_api_roundtrip():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    types = {t["type"] for t in c.get("/api/scenario/templates").json()}
    assert "sort" in types
    r = c.post("/api/scenario/generate", json={
        "template": "sort", "seed": 3, "board": BOARD, "catalog": CATALOG,
        "values": {"rules": RULES, "episodes": 40}})
    assert r.status_code == 200 and len(r.json()["episodes"]) == 40
    r = c.post("/api/scenario/generate", json={
        "template": "sort", "board": BOARD, "catalog": CATALOG, "values": {"rules": RULES[:1]}})
    assert r.status_code == 400 and "담을 곳" in r.json()["detail"]


# ── 로봇 쪽 · 삼면 상자 ──

# 화면 기본값과 같은 모양 — 로봇은 아래, 상자는 왼쪽·위쪽·오른쪽 변 가운데, 그 줄의 나머지는 금지
THREE_SIDES = {
    "cols": 7, "rows": 5, "cell_mm": 80, "robot_side": "bottom",
    "blocked": ["A1", "A5", "B1", "F1", "G1", "G5"],
    "zones": [
        {"id": "bin_left", "cells": ["A2", "A3", "A4"]},
        {"id": "bin_top", "cells": ["C1", "D1", "E1"]},
        {"id": "bin_right", "cells": ["G2", "G3", "G4"]},
    ],
}
THREE_RULES = [
    {"obj": "red", "dest": "bin_left", "task": "put the red apple in the left box"},
    {"obj": "yellow", "dest": "bin_right", "task": "put the yellow apple in the right box"},
    {"obj": "small", "dest": "bin_top", "task": "put the small apple in the top box"},
]


def test_robot_side_defaults_to_top_for_old_settings():
    """예전에 저장한 설정에는 `robot_side` 가 없다 — 그때의 뜻(위)대로 읽는다."""
    assert Board.from_dict(BOARD).robot_side == "top"
    with pytest.raises(ScenarioError, match="로봇 쪽"):
        Board.from_dict({**BOARD, "robot_side": "front"})


def test_distance_follows_the_robot_side():
    b = Board.from_dict(THREE_SIDES)
    assert b.dist_to_robot("D5") < b.dist_to_robot("D2")          # 아래 로봇 — 아래 줄이 가깝다
    left = Board.from_dict({**BOARD, "robot_side": "left"})
    assert left.dist_to_robot("B2") < left.dist_to_robot("F2")
    # 왼쪽 로봇이면 대칭 칸은 위·아래 짝이고, 같으면 위쪽부터
    assert left.dist_to_robot("C1") == left.dist_to_robot("C4")
    assert left.tie_key("C1") < left.tie_key("C4")


@pytest.mark.parametrize("seed", range(4))
def test_three_side_boxes_balance_and_nearest_rule(seed):
    plan = generate("sort", THREE_SIDES, CATALOG,
                    {"rules": THREE_RULES, "episodes": 120, "target_by": "rule", "order_rule": "nearest"}, seed)
    _assert_balanced(plan)
    b = Board.from_dict(THREE_SIDES)
    for ep in plan["episodes"]:
        key = [(b.dist_to_robot(p["cell"]), b.tie_key(p["cell"])) for p in ep["layout"]]
        cell = ep["picks"][0]["cell"]
        assert (b.dist_to_robot(cell), b.tie_key(cell)) == min(key)
    assert {ep["picks"][0]["dest"] for ep in plan["episodes"]} == {"bin_left", "bin_right", "bin_top"}


# ── 격자 바깥 구역 ──

OUTSIDE = {
    "cols": 5, "rows": 4, "cell_mm": 80, "robot_side": "bottom",
    "zones": [
        {"id": "bin_left", "outside": {"side": "left", "start": 1, "end": 2, "depth_mm": 120, "gap_mm": 10}},
        {"id": "bin_top", "outside": {"side": "top", "start": 1, "end": 3, "depth_mm": 120, "gap_mm": 10}},
        {"id": "bin_right", "cells": ["E1"]},                    # 안·바깥 섞어도 된다
    ],
}


def test_outside_zones_keep_every_cell_for_objects():
    b = Board.from_dict(OUTSIDE)
    assert len(b.work_cells) == 5 * 4 - 1                      # 안쪽 구역 E1 만 빠진다
    assert b.zone_rect(b.zone("bin_top")) == (80.0, -130.0, 240.0, 120.0)
    assert b.zone_rect(b.zone("bin_left")) == (-130.0, 80.0, 120.0, 160.0)
    assert b.zone_rect(b.zone("bin_right")) is None
    assert b.frame == (-130.0, -130.0, 400.0, 320.0)


def test_outside_zones_survive_the_round_trip():
    """계획에 실린 작업판으로 다시 만들면 같은 계획 — 바깥 구역도 빠짐없이 실린다."""
    rules = [{"obj": "red", "dest": "bin_left", "task": "r"}, {"obj": "yellow", "dest": "bin_top", "task": "y"},
             {"obj": "small", "dest": "bin_right", "task": "s"}]
    plan = generate("sort", OUTSIDE, CATALOG, {"rules": rules, "episodes": 60}, 5)
    _assert_balanced(plan)
    again = generate("sort", plan["board"], plan["catalog"], dict(plan["values"]), plan["seed"])
    assert again["episodes"] == plan["episodes"]
    assert {ep["picks"][0]["dest"] for ep in plan["episodes"] if ep["picks"][0]["dest"]} == {
        "bin_left", "bin_top", "bin_right"}


@pytest.mark.parametrize("outside, match", [
    ({"side": "front", "start": 0, "end": 1}, "변은"),
    ({"side": "top", "start": 3, "end": 9}, "벗어납니다"),
    ({"side": "top", "start": 0, "end": 1, "depth_mm": 5}, "깊이"),
])
def test_bad_outside_zones_are_refused(outside, match):
    with pytest.raises(ScenarioError, match=match):
        Board.from_dict({**BOARD, "zones": [{"id": "x", "outside": outside}]})


def test_outside_zones_on_one_side_must_not_overlap():
    with pytest.raises(ScenarioError, match="겹칩니다"):
        Board.from_dict({**BOARD, "zones": [
            {"id": "a", "outside": {"side": "top", "start": 0, "end": 2}},
            {"id": "b", "outside": {"side": "top", "start": 2, "end": 3}},
        ]})


# ── 물체 크기 — 실제로 놓을 수 있어야 한다 ──
#
# 칸 60mm 에 사과 75mm. 칸 이름만 보면 이웃 칸 둘이 비어 보여도 판 위에서는 겹친다.
# 상자(A·G 열)·금지 칸 바로 옆에 두면 사과가 상자에 걸친다.

SMALL_CELLS = {**BOARD, "cell_mm": 60}
BIG_APPLES = [
    {"id": "red", "label": "red apple", "count": 3, "size_mm": 75},
    {"id": "yellow", "label": "yellow apple", "count": 3, "size_mm": 75},
    {"id": "small", "label": "small apple", "count": 2, "size_mm": 55},
]


def _sized(values=None, seed=42, board=SMALL_CELLS, catalog=BIG_APPLES):
    return generate("sort", board, catalog, {"rules": RULES, **(values or {})}, seed)


def _layouts(plan):
    """시작 배치마다 [(종류, 칸)] — 라운드 배치와 perturb 로 옮긴 뒤의 배치."""
    for r in plan["rounds"]:
        yield r["hard"], [(p["obj"], p["cell"]) for p in r["placements"]]
    for ep in plan["episodes"]:
        h = ep["hint"]
        if h and h["kind"] == "perturb":
            moved = [(p["obj"], h["to"] if p["cell"] == h["cell"] else p["cell"]) for p in ep["layout"]]
            yield False, moved


def _check_physical(plan, clearance=0.0):
    b = Board.from_dict(plan["board"])
    size = {i["id"]: i["size_mm"] for i in plan["catalog"]}
    for hard, layout in _layouts(plan):
        for t, c in layout:
            assert b.fits(c, size[t]), f"{t}({size[t]}mm) 가 {c} 에서 상자·금지 칸에 걸친다"
        for i, (t, c) in enumerate(layout):
            for j, (u_t, u) in enumerate(layout[:i]):
                pair = hard and {i, j} == {0, 1}
                need = (size[t] + size[u_t]) / 2 + (0 if pair else clearance)
                assert b.dist_mm(c, u) >= need - 1e-6, f"{t}@{c} 와 {u_t}@{u} 가 겹친다 ({b.dist_mm(c, u):.0f} < {need})"


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_objects_never_overlap_on_the_board(seed):
    plan = _sized({"variants": {"perturb": 0.2}}, seed=seed)
    _check_physical(plan)
    _assert_balanced(plan)


def test_clearance_leaves_room_for_the_fingers():
    plan = _sized({"clearance_mm": 20, "round_sizes": "3"})
    _check_physical(plan, clearance=20)


def test_big_objects_stay_off_cells_that_would_touch_a_box():
    """사과(반지름 37.5)는 상자 칸과 맞닿은 칸(중심까지 30mm)에 못 간다 — 균형은 갈 수 있는 칸 안에서."""
    plan = _sized()
    s = plan["summary"]
    red_off = next(w["cells"] for w in s["warnings"] if w["code"] == "size_keeps_off" and w["obj"] == "red")
    assert {"B1", "B4", "F1", "F4"} <= set(red_off)
    assert all(s["target_counts"]["red"][c] == 0 for c in red_off)
    assert not any(w["code"] == "size_keeps_off" and w["obj"] == "small" for w in s["warnings"]), \
        "작은 사과(55mm)는 60mm 칸 어디든 들어간다"
    assert {"code": "object_over_cell", "obj": "red", "size_mm": 75.0, "cell_mm": 60.0} in s["warnings"]


def test_hard_pair_sits_as_close_as_it_physically_can():
    """바로 옆(60mm)에는 75mm 사과 둘이 안 들어간다 — 대각선(85mm)이면 들어간다. 겹치게 붙이지 않는다."""
    plan = _sized({"variants": {"hard_layout": 0.5}, "round_sizes": "3"})
    b = Board.from_dict(plan["board"])
    hard = [r for r in plan["rounds"] if r["hard"]]
    assert hard
    for r in hard:
        (t0, c0), (t1, c1) = [(p["obj"], p["cell"]) for p in r["placements"][:2]]
        assert b.chebyshev(c0, c1) == 1
        if {t0, t1} <= {"red", "yellow"}:
            c, r0 = b.parse(c0)
            cc, rr = b.parse(c1)
            assert c != cc and r0 != rr, "75mm 둘을 60mm 이웃 칸에 붙였다 — 겹친다"
    _check_physical(plan)


def test_an_object_that_fits_nowhere_is_refused():
    huge = [*BIG_APPLES[:2], {"id": "small", "label": "melon", "count": 2, "size_mm": 320}]
    with pytest.raises(ScenarioError, match="melon"):
        _sized(catalog=huge)
