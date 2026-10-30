"""`generate()` — 템플릿 생성기를 부르고 균형 요약을 붙인다 (§7).

모든 템플릿이 같이 쓰는 값(회수·블록·물체 구성·간격·성가신 변수·변형 비율)은 여기서
검증하고, 템플릿 고유 필드는 템플릿 YAML 이 검증한다. 생성기는 검증된 `Context` 만 받는다.

같은 입력(템플릿·값·시드)이면 같은 계획이다. 계획은 녹화 때 데이터셋 사이드카에 그대로
실린다(2단계) — 그래서 입력도 계획 안에 같이 담는다.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.services.scenario import ScenarioError
from app.services.scenario.balance import spread
from app.services.scenario.board import Board
from app.services.scenario.templates import (
    TemplateSpec, generator_of, get_template, resolve_fields,
)

_ITEM_ID = re.compile(r"^[A-Za-z][\w-]{0,31}$")

COMPOSITIONS = ("equal", "proportional", "fixed")
MAX_NUISANCE = 4


@dataclass(frozen=True)
class Item:
    id: str
    label: str
    color: str
    size_mm: float
    count: int


@dataclass
class Common:
    """모든 템플릿이 같이 쓰는 값."""
    episodes: int = 120
    block_size: int = 20
    episode_unit: str = ""
    # equal — 종류 총량 같게, proportional — 실물 비율대로, fixed — 라운드 구성 고정 (§5-3)
    composition: str = "equal"
    fixed_composition: dict[str, int] = field(default_factory=dict)
    # 물체 사이 최소 칸 거리(체비셰프). 1 = 이웃 칸 허용, 2 = 한 칸 띄움
    min_spacing: int = 1
    # 물체 가장자리 사이에 남길 틈(mm) — 그리퍼 손가락이 들어갈 자리. 0 이면 겹치지만 않게.
    # 물체 크기는 칸과 상관없이 늘 지킨다(`Board.fits`·`sort._apart`). 어려운 배치의 짝은 이 틈을 푼다.
    clearance_mm: float = 0.0
    episode_s: float = 20.0
    reset_s: float = 15.0
    nuisance: list[dict] = field(default_factory=list)
    # 쓸 정책 — 지시문을 보는지(`capabilities.language`)를 여기서 읽는다
    policy_type: str = ""
    variants: dict[str, float] = field(default_factory=dict)


@dataclass
class Context:
    board: Board
    catalog: list[Item]
    fields: dict[str, Any]
    common: Common
    # 이 설정에서 켜진 변형과 비율 (조건 안 맞는 것·0 은 빠져 있다)
    variants: dict[str, float]
    rng: random.Random
    # 정책이 지시문을 보나 — 정책을 안 골랐거나 모르는 정책이면 None
    policy_language: bool | None = None

    def item(self, iid: str) -> Item:
        return next(i for i in self.catalog if i.id == iid)


@dataclass
class Generated:
    episodes: list[dict]
    rounds: list[dict]
    warnings: list[dict] = field(default_factory=list)


# ── 검증 ──

def _num(v: Any, name: str, lo: float, hi: float, *, integer: bool = False) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ScenarioError(f"{name} 는 숫자여야 합니다")
    if integer and not x.is_integer():
        raise ScenarioError(f"{name} 는 정수여야 합니다")
    if not lo <= x <= hi:
        raise ScenarioError(f"{name} 는 {lo:g}~{hi:g} 사이여야 합니다")
    return int(x) if integer else x


def parse_catalog(raw: list[dict]) -> list[Item]:
    if not raw:
        raise ScenarioError("물체 목록이 비어 있습니다")
    if len(raw) > 12:
        raise ScenarioError("물체 종류는 12개까지입니다")
    items: list[Item] = []
    for d in raw:
        iid = str(d.get("id", ""))
        if not _ITEM_ID.match(iid):
            raise ScenarioError(f"물체 id 는 영문으로 시작하는 32자 이내여야 합니다: {iid!r}")
        if any(i.id == iid for i in items):
            raise ScenarioError(f"물체 id 가 겹칩니다: {iid}")
        items.append(Item(
            id=iid,
            label=str(d.get("label", "")) or iid,
            color=str(d.get("color", "#888888")),
            size_mm=_num(d.get("size_mm", 70), "size_mm", 5, 500),
            count=_num(d.get("count", 1), "count", 1, 50, integer=True),
        ))
    return items


def parse_common(raw: dict, spec: TemplateSpec, catalog: list[Item]) -> Common:
    c = Common()
    c.episodes = _num(raw.get("episodes", c.episodes), "episodes", 1, 5000, integer=True)
    c.block_size = _num(raw.get("block_size", c.block_size), "block_size", 1, 500, integer=True)
    c.episode_unit = str(raw.get("episode_unit") or spec.episode_units[0])
    if c.episode_unit not in spec.episode_units:
        raise ScenarioError(f"이 템플릿의 에피소드 단위는 {', '.join(spec.episode_units)} 중 하나입니다")
    c.composition = str(raw.get("composition", c.composition))
    if c.composition not in COMPOSITIONS:
        raise ScenarioError(f"composition 은 {', '.join(COMPOSITIONS)} 중 하나여야 합니다")
    ids = {i.id for i in catalog}
    fixed = raw.get("fixed_composition") or {}
    c.fixed_composition = {}
    for k, v in fixed.items():
        if k not in ids:
            raise ScenarioError(f"고정 구성에 물체 목록에 없는 종류가 있습니다: {k}")
        n = _num(v, k, 0, 50, integer=True)
        if n > next(i.count for i in catalog if i.id == k):
            raise ScenarioError(f"고정 구성의 {k} 가 실물 개수보다 많습니다")
        if n:
            c.fixed_composition[k] = n
    if c.composition == "fixed" and not c.fixed_composition:
        raise ScenarioError("고정 구성을 고르면 라운드마다 놓을 개수를 적어야 합니다")
    c.min_spacing = _num(raw.get("min_spacing", c.min_spacing), "min_spacing", 1, 3, integer=True)
    c.clearance_mm = _num(raw.get("clearance_mm", c.clearance_mm), "clearance_mm", 0, 100)
    c.episode_s = _num(raw.get("episode_s", c.episode_s), "episode_s", 1, 600)
    c.reset_s = _num(raw.get("reset_s", c.reset_s), "reset_s", 0, 600)
    c.policy_type = str(raw.get("policy_type", "") or "")

    nuisance = raw.get("nuisance") or []
    if len(nuisance) > MAX_NUISANCE:
        raise ScenarioError(f"성가신 변수는 {MAX_NUISANCE}개까지입니다")
    c.nuisance = []
    for n in nuisance:
        name = str(n.get("name", "")).strip()
        levels = [str(x).strip() for x in n.get("levels", []) if str(x).strip()]
        if not name:
            raise ScenarioError("성가신 변수에 이름이 없습니다")
        if len(set(levels)) < 2:
            raise ScenarioError(f"성가신 변수 {name} 에는 서로 다른 값이 둘 이상 있어야 합니다")
        c.nuisance.append({"name": name, "levels": list(dict.fromkeys(levels))})

    c.variants = {}
    for k, v in (raw.get("variants") or {}).items():
        if k not in spec.variants:
            raise ScenarioError(f"이 템플릿에 없는 변형입니다: {k}")
        c.variants[k] = _num(v, k, 0, 0.5)
    return c


def _policy_language(policy_type: str) -> bool | None:
    if not policy_type:
        return None
    from app.core.policy_spec import load_specs

    spec = load_specs().get(policy_type)
    return spec.capabilities.language if spec else None


def resolve_variants(spec: TemplateSpec, common: Common, fields: dict) -> tuple[dict, list[str]]:
    """켜진 변형과 비율, 조건이 안 맞아 꺼진 변형 이름."""
    values = {**fields, "episode_unit": common.episode_unit}
    on: dict[str, float] = {}
    off: list[str] = []
    for k, v in spec.variants.items():
        ratio = common.variants.get(k, v.default)
        if ratio <= 0:
            continue
        if any(str(values.get(rk)) != rv for rk, rv in v.requires.items()):
            off.append(k)
            continue
        on[k] = ratio
    return on, off


# ── 생성 ──

def generate(template: str, board: dict, catalog: list[dict], values: dict, seed: int) -> dict:
    spec = get_template(template)
    b = Board.from_dict(board)
    items = parse_catalog(catalog)
    known = set(spec.fields) | set(Common.__dataclass_fields__)
    unknown = sorted(set(values) - known)
    if unknown:
        raise ScenarioError(f"모르는 값입니다: {', '.join(unknown)}")
    fields = resolve_fields(spec, values)
    common = parse_common(values, spec, items)
    variants, off = resolve_variants(spec, common, fields)

    ctx = Context(
        board=b, catalog=items, fields=fields, common=common, variants=variants,
        rng=random.Random(seed), policy_language=_policy_language(common.policy_type),
    )
    out = generator_of(spec).generate(ctx)
    if len(out.episodes) != common.episodes:
        # 생성기 계약 위반 — 사용자 탓이 아니다. 조용히 다른 개수를 돌려주면 진행률이 거짓말한다.
        raise RuntimeError(f"생성기 {spec.generator}: 시도 {len(out.episodes)}개 ≠ 요청 {common.episodes}")

    resolved = {**fields, **{k: getattr(common, k) for k in Common.__dataclass_fields__}}
    return {
        "version": 1,
        "template": spec.type,
        "seed": seed,
        "board": b.to_dict(),
        "catalog": [i.__dict__ for i in items],
        "values": resolved,
        "episodes": out.episodes,
        "rounds": out.rounds,
        "summary": summarize(ctx, out, off),
    }


def summarize(ctx: Context, out: Generated, variants_off: list[str]) -> dict:
    """생성 직후 보여 줄 것 (§7-5) — 히트맵·총량·변형·교차표·예상 시간·경고."""
    work = ctx.board.work_cells
    ids = [i.id for i in ctx.catalog]
    # 크기 때문에 그 종류가 못 쓰는 칸은 0 이 정상이다 — 고른지는 쓸 수 있는 칸에서만 본다
    fit = {i.id: [c for c in work if ctx.board.fits(c, i.size_mm)] for i in ctx.catalog}

    def per_cell() -> dict[str, dict[str, int]]:
        return {t: {c: 0 for c in work} for t in ids}

    targets, placed = per_cell(), per_cell()
    blocks: dict[int, dict[str, dict[str, int]]] = {}
    for ep in out.episodes:
        for p in ep["picks"]:
            if p.get("cell"):
                targets[p["obj"]][p["cell"]] += 1
                blocks.setdefault(ep["block"], per_cell())[p["obj"]][p["cell"]] += 1
    for r in out.rounds:
        for p in r["placements"]:
            placed[p["obj"]][p["cell"]] += 1

    type_totals = {t: sum(targets[t].values()) for t in ids}
    crosstab: dict[str, dict[str, dict[str, int]]] = {}
    for n in ctx.common.nuisance:
        tab = {lv: {t: 0 for t in ids} for lv in n["levels"]}
        for ep in out.episodes:
            lv = ep["nuisance"].get(n["name"])
            for p in ep["picks"]:
                if lv is not None and p.get("cell"):
                    tab[lv][p["obj"]] += 1
        crosstab[n["name"]] = tab

    resets = sum(1 for ep in out.episodes if ep["reset"])
    warnings = list(out.warnings)
    picks_on_board = sum(type_totals.values())
    combos = sum(len(fit[t]) for t in ids)
    if picks_on_board < combos:
        warnings.append({"code": "combos_short", "combos": combos, "targets": picks_on_board})
    for i in ctx.catalog:
        if i.size_mm > ctx.board.cell_mm:
            warnings.append({"code": "object_over_cell", "obj": i.id, "size_mm": i.size_mm,
                             "cell_mm": ctx.board.cell_mm})
        off = [c for c in work if c not in fit[i.id]]
        if off:
            warnings.append({"code": "size_keeps_off", "obj": i.id, "cells": off})
    if ctx.common.composition == "equal" and len(ids) > 1:
        mean = picks_on_board / len(ids)
        if spread(type_totals.values()) > max(1, round(mean * 0.1)):
            warnings.append({"code": "uneven_types", "totals": type_totals})

    return {
        "episodes": len(out.episodes),
        "rounds": len(out.rounds),
        "blocks": len({ep["block"] for ep in out.episodes}),
        "work_cells": work,
        "target_counts": targets,
        "placed_counts": placed,
        "type_totals": type_totals,
        "spread": {t: spread([targets[t][c] for c in fit[t]]) for t in ids},
        "block_spread": {
            t: max((spread([b[t][c] for c in fit[t]]) for b in blocks.values()), default=0) for t in ids
        },
        "variant_counts": dict(Counter(ep["variant"] for ep in out.episodes)),
        "variants_off": variants_off,
        "round_sizes": {str(k): v for k, v in sorted(Counter(len(r["placements"]) for r in out.rounds).items())},
        "crosstab": crosstab,
        "resets": resets,
        "est_s": round(len(out.episodes) * ctx.common.episode_s + resets * ctx.common.reset_s),
        "warnings": warnings,
    }
