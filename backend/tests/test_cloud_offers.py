"""RENT 탭이 읽는 오퍼 — 가격·경고·질의 (feature/vast-training.md §9-2).

⚠ **이 파일이 지키는 것은 가격이다.** Vast 가 주는 `dph_total` 을 그대로 화면에 쓰면
예산 상한과 "최대 N시간" 이 전부 낙관적으로 틀린다 — 거기 섞인 디스크는 검색
기본값(약 5GB)인데 우리 템플릿은 40GB 를 쓰기 때문이다. 틀린 값은 화면에서
멀쩡해 보이고, 증상은 크레딧이 예상보다 빨리 마를 때에야 나타난다.

⚠ **fixture 는 실물이다.** 2026-09-16 에 받은 오퍼 13개(필드 100개)를 그대로 뒀다.
손으로 흉내 낸 표본은 필드명을 틀리고, 그 틀림이 곧 파서의 0 이 된다.
"""

import json
import subprocess
from pathlib import Path

import pytest

from app.services.cloud.providers import vast
from app.services.cloud.providers.base import (
    CC_MAX, CC_MIN, HOURS_PER_MONTH, KNOWN_CC, MIN_CPU_CORES, MIN_CUDA,
    OfferFilter, gpu_support, warnings_for,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "vast_offers.json").read_text())
DISK = 40.0


def test_fixture_has_the_fields_we_price_from():
    """⚠ fixture 를 새로 뜨다가 필드가 빠지면 파서는 조용히 0 을 읽는다."""
    needed = {"id", "dph_base", "dph_total", "storage_cost", "storage_total_cost",
              "cpu_cores_effective", "cuda_max_good", "reliability2", "disk_space",
              "dlperf_per_dphtotal", "inet_down_cost", "inet_up_cost", "duration"}
    assert FIXTURE, "fixture 가 비었다"
    for raw in FIXTURE:
        assert needed <= set(raw), f"id={raw.get('id')} 에 빠진 필드: {needed - set(raw)}"


def test_vast_price_identity_holds():
    """`dph_total == dph_base + storage_total_cost` — 우리 계산식의 근거다.

    이게 깨지면 `dph_total` 의 구성이 바뀐 것이고, 그때는 §9-2 의 계산식을 다시
    유도해야 한다. 표본 13/13 에서 오차 0 이었다.
    """
    for raw in FIXTURE:
        assert raw["dph_base"] + raw["storage_total_cost"] == pytest.approx(raw["dph_total"])


def test_hourly_is_recomputed_for_our_disk_not_taken_from_dph_total():
    """★ 이 저장소가 이 기능에서 가장 틀리기 쉬운 곳."""
    for raw in FIXTURE:
        o = vast.parse_offer(raw, DISK)
        expected = raw["dph_base"] + raw["storage_cost"] * DISK / HOURS_PER_MONTH
        assert o.hourly == pytest.approx(expected)
        # 40GB 는 검색 기본값(~5GB)보다 크므로 **언제나 표시가보다 비싸다**
        assert o.hourly > o.dph_total


def test_hourly_follows_the_disk_slider():
    """디스크는 필터이자 **가격 입력**이다 — 바꾸면 표 전체가 다시 계산돼야 한다."""
    raw = FIXTURE[0]
    small = vast.parse_offer(raw, 40.0)
    big = vast.parse_offer(raw, 200.0)
    assert big.hourly > small.hourly
    assert big.storage_hourly == pytest.approx(small.storage_hourly * 5)
    assert big.dph_base == small.dph_base          # GPU 몫은 그대로


def test_a_flat_markup_cannot_replace_the_calculation():
    """⚠ "대충 12% 더하면 되지" 가 안 되는 이유.

    호스트마다 `storage_cost` 가 크게 벌어진다. 보정률이 오퍼마다 다르므로 일률적인
    곱셈으로는 못 맞춘다 — 오퍼별로 계산해야 한다.
    """
    rates = [vast.parse_offer(r, DISK).hourly / r["dph_total"] for r in FIXTURE]
    assert max(rates) / min(rates) > 1.1, f"보정률 분포가 너무 좁다: {min(rates):.3f}~{max(rates):.3f}"


def test_warnings_catch_a_starved_cpu_and_an_old_driver():
    """실측에서 실제로 걸리는 둘. GPU 만 보고 고르면 이걸 집는다."""
    assert warnings_for(cpu_cores=0, cuda_max_good=13.0,
                        disk_space_gb=200, disk_gb=DISK)[0].startswith("CPU")
    assert warnings_for(cpu_cores=32, cuda_max_good=12.2,
                        disk_space_gb=200, disk_gb=DISK)[0].startswith("CUDA")
    assert warnings_for(cpu_cores=32, cuda_max_good=13.0,
                        disk_space_gb=20, disk_gb=DISK)[0].startswith("디스크")
    # 멀쩡한 기계에는 아무것도 안 붙는다 — 표가 시끄러우면 아무도 안 본다
    assert warnings_for(cpu_cores=32, cuda_max_good=13.0,
                        disk_space_gb=200, disk_gb=DISK) == ()


def test_fixture_actually_contains_both_warning_cases():
    """⚠ 경고 코드를 지켜 주는 건 **그 경고에 걸리는 표본**이다.

    fixture 를 다시 뜰 때 걸리는 오퍼를 빼면, 경고가 죽어도 테스트는 초록이 된다.
    """
    flagged = [vast.parse_offer(r, DISK) for r in FIXTURE]
    assert any(o.cpu_cores < MIN_CPU_CORES for o in flagged), "CPU 굶는 표본이 없다"
    assert any(o.cuda_max_good < MIN_CUDA for o in flagged), "CUDA 낮은 표본이 없다"


def test_verification_and_duration_are_deliberately_not_warnings():
    """⚠ 일부러 안 만든 것. 표본 60/60 이 `verified` 고 `duration` 은 최소 5일이다.

    짐작으로 배지를 만들면 아무도 안 보는 열이 둘 는다. 되살리려면 먼저 실측으로
    그게 걸리는 오퍼가 있음을 보여야 한다.
    """
    text = " ".join(warnings_for(cpu_cores=0, cuda_max_good=0, disk_space_gb=0, disk_gb=DISK))
    assert "verified" not in text and "검증" not in text
    assert "잔여" not in text and "duration" not in text


def test_default_query_is_the_verified_filter():
    """§2 에서 실물로 확인된 필터 그대로여야 한다. 기본 GPU 는 여전히 4090 이다."""
    q = vast.build_query(OfferFilter())
    for token in ("gpu_name=RTX_4090", "cuda_vers>=12.4", "reliability>0.98",
                  "inet_down>200", "disk_space>=40", "rentable=true"):
        assert token in q, f"{token} 가 빠졌다: {q}"


@pytest.mark.parametrize("bad", [
    "RTX_4090 rentable=false",       # 토큰을 하나 더 밀어 넣기
    "RTX_4090; rm -rf /",
    "RTX_4090\nnum_gpus=8",
    "RTX_4090,RTX_3060]",            # 목록 문법의 대괄호를 닫고 나오기
    "A" * 41,
])
def test_query_builder_refuses_anything_that_is_not_a_gpu_name(bad):
    """⚠ **여기가 주입 경계다.** 값이 질의 문자열로 연결되므로 모양을 먼저 본다.

    다중 선택이 생기면서 이름이 `gpu_name in [A,B]` 의 대괄호 **안**으로도 들어간다.
    한 이름만 검사를 빠져나가도 목록 전체가 뚫리므로 **모든 이름**을 본다.
    """
    with pytest.raises(ValueError):
        vast.build_query(OfferFilter(gpu_names=(bad,)))
    # 멀쩡한 이름 뒤에 숨겨 넣어도 막혀야 한다
    with pytest.raises(ValueError):
        vast.build_query(OfferFilter(gpu_names=("RTX 4090", bad)))


def test_no_gpu_names_means_everything():
    """⚠ 사용자가 "싹 다 보여줘" 라고 했다. 제약을 아예 안 거는 길이 있어야 한다.

    실측: gpu_name 토큰 없이 기본 필터만으로 241개·56종이 나온다.
    """
    for empty in ((), ("",), ("   ",)):
        q = vast.build_query(OfferFilter(gpu_names=empty))
        assert "gpu_name" not in q, f"{empty!r} 인데 GPU 제약이 붙었다: {q}"
        assert "rentable=true" in q, "나머지 필터까지 사라지면 안 된다"


def test_several_gpus_use_the_list_syntax():
    """실측: `gpu_name in [RTX_4090,RTX_3060]` 가 91개(4090 51 + 3060 40)를 준다."""
    q = vast.build_query(OfferFilter(gpu_names=("RTX 4090", "RTX 3060")))
    assert "gpu_name in [RTX_4090,RTX_3060]" in q
    # 하나면 목록 문법을 쓰지 않는다 — 읽기 어려워질 뿐이다
    assert "gpu_name=RTX_3060" in vast.build_query(OfferFilter(gpu_names=("RTX 3060",)))


def test_optional_filters_appear_only_when_asked():
    plain = vast.build_query(OfferFilter())
    assert "cpu_cores_effective" not in plain and "dph_total" not in plain
    rich = vast.build_query(OfferFilter(min_cpu_cores=8, max_price=0.5))
    assert "cpu_cores_effective>=8" in rich and "dph_total<=0.5" in rich


def test_template_variant_comes_from_the_tag():
    """이름은 사람이 고칠 수 있지만 태그는 이미지가 정한다."""
    t = vast.parse_template({"id": 1, "name": "아무 이름", "image": "ghcr.io/x/y",
                             "tag": "slim-0.5.0-cu126-20260914",
                             "recommended_disk_space": 40.0, "desc": ""})
    assert t.variant == "slim" and t.disk_gb == 40.0


class _FakeProvider(vast.VastProvider):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.calls: list[list[str]] = []

    def _raw(self, args):
        self.calls.append(args)
        return self.payload


def test_whoami_reports_credit_not_balance():
    """⚠ 실측 계정이 `balance: 0, credit: 25.0` 이었다.

    `balance` 를 읽으면 멀쩡한 계정이 "$0 · 최대 0시간" 이 되어 화면이 전부 막힌다.
    """
    got = _FakeProvider({"id": 1, "email": "a@b", "balance": 0, "credit": 25.0}).whoami()
    assert got["credit"] == 25.0


def test_templates_query_asks_for_private_ones():
    """⚠ `private=true` 가 없으면 공개 2048개가 오고 **우리 건 하나도 없다**."""
    p = _FakeProvider([])
    p.templates(refresh=True)
    assert "private=true" in p.calls[0]


def test_search_sorts_by_value_not_by_lowest_price():
    """기본 정렬은 **가성비**다 — 최저가 정렬은 CUDA 가 모자란 기계를 맨 위로 올린다.

    ⚠ 실물 fixture 에서는 가성비 1위가 마침 최저가이기도 해서, "최저가가 아니다" 로는
    둘을 못 가른다. 그래서 **갈리는 표본을 만들어** 확인한다 — 싸지만 느린 기계와
    비싸지만 빠른 기계.
    """
    cheap_slow = {**FIXTURE[0], "id": 1, "dph_base": 0.10,
                  "dlperf": 1.0, "dlperf_per_dphtotal": 10.0}
    dear_fast = {**FIXTURE[0], "id": 2, "dph_base": 0.90,
                 "dlperf": 9.0, "dlperf_per_dphtotal": 90.0}

    offers = _FakeProvider([cheap_slow, dear_fast]).search(OfferFilter(), refresh=True)
    assert [o.id for o in offers] == [2, 1], "최저가 정렬로 뒤집혔다"
    assert offers[0].hourly > offers[1].hourly    # 1등이 더 비싸다 — 가성비 정렬의 증거

    # 실물 표본에서도 내림차순은 지켜진다
    real = _FakeProvider(FIXTURE).search(OfferFilter(), refresh=True)
    assert [o.dlperf_per_dph for o in real] == sorted(
        (o.dlperf_per_dph for o in real), reverse=True)


def test_cli_errors_ride_in_the_body_with_returncode_zero(monkeypatch):
    """⚠ 실측: 키가 틀리면 `vastai` 는 **0 으로 끝내고** 본문에 오류를 싣는다.

    반환코드만 보면 그 오류가 빈 목록으로 둔갑해 "오퍼가 없습니다" 가 된다.
    """
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(
            a[0], 0, '{"error": true, "status_code": 404, "msg": "Invalid user key"}', "")

    monkeypatch.setattr(vast.shutil, "which", lambda _: "/usr/bin/vastai")
    monkeypatch.setattr(vast.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Invalid user key"):
        vast.VastProvider()._raw(["show", "user"])


def test_api_key_goes_in_the_env_never_in_argv():
    """⚠ argv 는 같은 호스트의 다른 프로세스가 `ps` 로 읽는다."""
    p = vast.VastProvider(api_key="secret-key-value")
    assert p._env()["VAST_API_KEY"] == "secret-key-value"
    q = vast.build_query(OfferFilter())
    assert "secret-key-value" not in q


# ─────────────────────────────────────────────────────────────────────────────
# 우리 이미지가 돌릴 수 있는 GPU 세대 (§9-2 · 2026-09-16 실측)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gpu, cc, want", [
    # ✓ 돈다 — 이미지가 담은 커널은 sm_50·60·70·75·80·86·90 (major 5·6·7·8·9)
    ("GTX TITAN X", 520, "ok"),       # sm_52 ← sm_50 큐빈 (같은 major)
    ("Tesla P100", 600, "ok"),
    ("GTX 1080 Ti", 610, "ok"),       # sm_61 ← sm_60 큐빈
    ("Tesla V100", 700, "ok"),
    ("GTX 1660", 750, "ok"),
    ("A100 PCIE", 800, "ok"),
    ("RTX 3060", 860, "ok"),          # ← 사용자가 쓰겠다는 바로 그 GPU
    ("RTX 4090", 890, "ok"),          # sm_89 ← sm_86 큐빈
    ("H100 SXM", 900, "ok"),
    # ✗ 너무 신형 — cu128 이상이 필요하다
    ("B200", 1000, "too_new"),
    ("B300", 1030, "too_new"),
    ("RTX 5090", 1200, "too_new"),
    # ? 모르는 값 — 단정하지 않는다
    ("Quadro P2000", 140, "unknown"),
    ("Quadro P4000", 243, "unknown"),
    ("값 없음", None, "unknown"),
    ("0 이면 없는 것", 0, "unknown"),
])
def test_gpu_support_matches_what_the_image_itself_prints(gpu, cc, want):
    """⚠ 판정 근거는 **이미지 자신이 찍는 문장**이다.

    이 머신의 RTX 5090 에 실제 이미지를 물려 받은 출력(2026-09-16):

        NVIDIA GeForce RTX 5090 with CUDA capability sm_120 is not compatible ...
        The current PyTorch install supports CUDA capabilities
            sm_50 sm_60 sm_70 sm_75 sm_80 sm_86 sm_90

    ⚠ **로컬 파이썬의 torch 로 확인하면 틀린다.** 이 머신 torch 는 2.10+cu128 이라
    arch list 가 sm_70~sm_120 이다 — 맥스웰·파스칼이 빠지고 Blackwell 이 들어 있는
    **다른 집합**이다. 처음에 그 값으로 하한을 700 으로 잡았다가 구형 10종을 없는
    죄로 막을 뻔했다. 근거는 이미지 안에서 떠야 한다.

    ⚠ `cuda_max_good` 으로는 이걸 못 본다 — Blackwell 호스트는 12.8~13.3 을 보고해
    `MIN_CUDA` 검사를 여유롭게 통과한다.
    """
    assert gpu_support(cc) == want, f"{gpu}(cc={cc})"


def test_unknown_compute_caps_are_not_called_old():
    """⚠ Vast 의 `compute_cap` 에는 실재하지 않는 값이 섞여 있다 — P2000=140, P4000=243.

    실물은 둘 다 파스칼(cc 6.1)이라 이미지의 sm_60 커널로 **돈다**. 구간만 보고
    "너무 구형" 이라 하면 틀린 답으로 사용자를 막는다. 모르면 모른다고 한다.
    """
    for bogus in (140, 243, 999, 1111):
        assert gpu_support(bogus) == "unknown"
    assert CC_MIN == 500, "이미지가 sm_50 을 담고 있다"
    assert CC_MAX == 1000
    assert 610 in KNOWN_CC and 1200 in KNOWN_CC


def _catalog_provider(rows_with_cuda, rows_without_cuda):
    """A(필터 그대로) / B(cuda_vers 뺀 것) 두 질의를 흉내 낸다."""
    class _P(vast.VastProvider):
        def _raw(self, args, *, timeout=None):
            q = args[2]
            return rows_without_cuda if "cuda_vers" not in q else rows_with_cuda
    return _P()


def _row(name, cc, **kw):
    return {"id": 1, "gpu_name": name, "compute_cap": cc, "gpu_ram": 12288,
            "dph_base": kw.get("base", 0.1), "storage_cost": 0.5,
            "dph_total": kw.get("base", 0.1)}


def test_catalog_uses_the_same_filter_as_the_table():
    """⚠ 카탈로그가 자기 조건으로 만들어지면 **"선택지엔 있는데 표는 0행"** 이 된다.

    그래서 `build_query` 하나를 공유하고 GPU 이름 항만 뺀다.
    """
    seen = []

    class _P(vast.VastProvider):
        def _raw(self, args, *, timeout=None):
            seen.append(args[2])
            return []

    f = OfferFilter(min_reliability=0.5, min_inet_down=10, disk_gb=123, num_gpus=2)
    _P().catalog(f, refresh=True)
    assert len(seen) == 2, "A·B 두 질의여야 한다"
    for q in seen:
        assert "gpu_name" not in q, q                 # 이름만 뺀다
        assert "reliability>0.5" in q and "disk_space>=123" in q and "num_gpus=2" in q
    assert sum("cuda_vers" in q for q in seen) == 1, "한쪽만 cuda_vers 를 가져야 한다"


def test_catalog_says_why_a_gpu_has_no_machines():
    """⚠ **조용히 빈 목록을 주지 않는다.**

    실측: `cuda_vers>=12.4` 하나가 Blackwell 전체를 걸러낸다(RTX_5090 은 46대 → 0대).
    그래서 사용자가 RTX 5090 을 고르면 시장에 수십 대가 있는데도 표가 0행이 되는데,
    이유를 아무 데도 안 적어 주면 화면이 고장난 줄 안다.
    """
    a = [_row("RTX 3060", 860, base=0.05)]                    # 필터 통과
    b = a + [_row("RTX 5090", 1200, base=0.40),               # cuda_vers 가 숨기던 것
             _row("Quadro P4000", 243, base=0.04)]
    by = {g.name: g for g in _catalog_provider(a, b).catalog(OfferFilter(), refresh=True)}

    assert by["RTX 3060"].available is True and by["RTX 3060"].reason is None
    assert by["RTX 5090"].available is False
    assert "cu128" in by["RTX 5090"].reason, "왜 없는지를 세대로 설명해야 한다"
    # 세대를 모르는 것은 세대 탓으로 돌리지 않는다
    assert by["Quadro P4000"].support == "unknown"
    assert "cu128" not in (by["Quadro P4000"].reason or "")

    # ⚠ 못 고르는 것도 **목록에는 남는다** — 사용자가 "싹 다 보여줘" 라고 했다
    assert len(by) == 3


def test_catalog_puts_the_usable_ones_first():
    a = [_row("RTX 4090", 890, base=0.30), _row("RTX 3060", 860, base=0.05)]
    b = a + [_row("RTX 5090", 1200, base=0.01)]     # 제일 싸지만 못 쓴다
    got = _catalog_provider(a, b).catalog(OfferFilter(), refresh=True)
    assert [g.name for g in got] == ["RTX 3060", "RTX 4090", "RTX 5090"]
    assert got[-1].available is False, "못 쓰는 것이 가격만으로 맨 위에 오면 안 된다"


def test_catalog_counts_are_labelled_as_a_sample_not_a_fact():
    """⚠ 오퍼 수는 새로고침마다 흔들린다(실측: 같은 질의 3회에 행 234~238, 한 기종
    18행 중 10행만 같은 id). 필드 이름이 그렇게 말해야 화면도 그렇게 적는다."""
    from app.services.cloud.providers.base import GpuModel
    assert "offers_seen" in GpuModel.__dataclass_fields__
    assert "offers" not in GpuModel.__dataclass_fields__


def test_catalog_prices_use_the_same_basis_as_the_table():
    """⚠ 여기만 `dph_total` 을 쓰면 **고를 땐 싸 보이고 표에선 비싸지는** 화면이 된다."""
    row = {"id": 1, "gpu_name": "RTX 3060", "compute_cap": 860, "gpu_ram": 12288,
           "dph_base": 0.100, "storage_cost": 0.8666666, "dph_total": 0.106}
    g = _catalog_provider([row], [row]).catalog(OfferFilter(), refresh=True)[0]
    assert g.min_hourly == pytest.approx(0.100 + 0.8666666 * 40 / HOURS_PER_MONTH)
    assert g.min_hourly > 0.106, "dph_total 을 그대로 썼다"


def test_catalog_is_cached_with_its_own_ttl():
    """⚠ TTL 을 offers 와 공유하면 카탈로그를 위해 늘릴 때 **가격표까지 같이 늙는다**."""
    calls = []

    class _P(vast.VastProvider):
        def _raw(self, args, *, timeout=None):
            calls.append(1)
            return [_row("RTX 3060", 860)]

    p = _P()
    p.catalog(OfferFilter(), refresh=True)
    n = len(calls)
    p.catalog(OfferFilter())
    assert len(calls) == n, "캐시가 안 먹었다"
    assert vast.CATALOG_TTL != vast.CACHE_TTL, "TTL 이 갈라져 있어야 한다"


def test_the_cache_does_not_grow_without_bound():
    """⚠ 키가 질의 문자열이라 사용자가 칸을 만질 때마다 새 항목이 생긴다."""
    class _P(vast.VastProvider):
        def _raw(self, args, *, timeout=None):
            return []

    p = _P()
    for i in range(vast.CACHE_MAX + 20):
        p._cached(f"k{i}", lambda: i)
    assert len(vast._cache) <= vast.CACHE_MAX


def test_no_hardcoded_seed_list_in_the_provider():
    """⚠ 박아 둔 목록이 썩은 것이 이번 사건의 원인이다 — 같은 실수를 서버에 심지 않는다."""
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "cloud"
           / "providers" / "vast.py").read_text()
    assert "SEED" not in src and "FALLBACK_GPUS" not in src


# ─────────────────────────────────────────────────────────────────────────────
# 라우터 — ⚠ 이 구멍이 실제로 뚫렸다
#
# `OfferFilter.gpu_name` 을 `gpu_names` 로 바꿨을 때 라우터는 옛 이름을 계속 넘겼다.
# 위의 단위 테스트는 전부 초록인데 엔드포인트는 500 이었다 — 아무 테스트도 라우터를
# 지나가지 않았기 때문이다. 계약이 바뀌는 자리는 **양쪽 끝에서** 잡아야 한다.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import cloud as router

    class _Fake(vast.VastProvider):
        def _raw(self, args, *, timeout=None):
            if "templates" in args:
                return [{"id": 1, "name": "piper-train full cu126", "image": "ghcr.io/x/y",
                         "tag": "full-0.5.0-cu126", "recommended_disk_space": 40.0, "desc": ""}]
            if "user" in args:
                return {"id": 1, "email": "a@b", "balance": 0, "credit": 25.0}
            return FIXTURE

    monkeypatch.setattr(router, "_provider", _Fake)
    vast._cache.clear()
    return TestClient(app)


@pytest.mark.parametrize("params, expect_in_query, expect_out", [
    ({}, "gpu_name=RTX_4090", None),                                   # 기본
    ({"gpu": "RTX 3060"}, "gpu_name=RTX_3060", None),                  # 하나
    ({"gpu": ["RTX 4090", "RTX 3060"]},                                # 여럿
     "gpu_name in [RTX_4090,RTX_3060]", None),
    ({"gpu": ""}, None, "gpu_name"),                                   # 전체 — 제약 없음
])
def test_offers_endpoint_accepts_one_many_or_all(client, params, expect_in_query, expect_out):
    """사용자가 "싹 다 보여줘" 라고 했다 — 계약이 그걸 표현할 수 있어야 한다."""
    r = client.get("/api/cloud/vast/offers", params={**params, "limit": 3})
    assert r.status_code == 200, r.text
    q = r.json()["query"]
    if expect_in_query:
        assert expect_in_query in q, q
    if expect_out:
        assert expect_out not in q, q


def test_offers_endpoint_rejects_a_bad_gpu_name(client):
    """주입 경계가 HTTP 끝까지 살아 있는가 — 400 이지 500 이 아니다."""
    r = client.get("/api/cloud/vast/offers", params={"gpu": "RTX_4090; rm -rf /"})
    assert r.status_code == 400


def test_gpus_endpoint_returns_the_catalog_with_support(client):
    r = client.get("/api/cloud/gpus")
    assert r.status_code == 200, r.text
    gpus = r.json()["gpus"]
    assert gpus and all(
        {"name", "support", "vram_gb", "offers_seen", "min_hourly", "available", "reason"}
        <= set(g) for g in gpus)


def test_gpus_endpoint_degrades_to_an_empty_list_instead_of_failing(client, monkeypatch):
    """⚠ 카탈로그가 죽어도 화면이 죽으면 안 된다 — 지금 오퍼에 보이는 기종과 합쳐 쓴다."""
    from app.routers import cloud as router

    class _Dead(vast.VastProvider):
        def _raw(self, args, *, timeout=None):
            raise RuntimeError("vast 가 응답하지 않음")

    monkeypatch.setattr(router, "_provider", _Dead)
    vast._cache.clear()
    r = client.get("/api/cloud/gpus")
    assert r.status_code == 200 and r.json()["gpus"] == []
    assert r.json()["detail"], "왜 비었는지 말해야 한다"


# ─────────────────────────────────────────────────────────────────────────────
# bf16 — **죽지 않아서** 위험한 경우 (2026-09-17)
# ─────────────────────────────────────────────────────────────────────────────

def test_pre_ampere_cards_are_flagged_for_bf16():
    """⚠ 학습 기본 AMP 가 bf16 인데(`routers/training.py`·`RentRequest.amp`) 하드웨어
    bf16 은 암페어(cc 8.0)부터다.

    ⚠ **이게 경고인 이유는 안 죽기 때문이다.** torch 의
    `is_bf16_supported(including_emulation=True)` 가 기본값이라(실측: torch 소스에서
    `major >= 8` 이 아니면 에뮬레이션 경로로 떨어져 True 를 돌려준다) 학습은 멀쩡히
    시작하고 **조용히 느려진다.** 죽었으면 사람이 바로 알았을 텐데, 그냥 몇 시간을
    더 낸다.
    """
    def warn(cc):
        return warnings_for(cpu_cores=8, cuda_max_good=12.6, disk_space_gb=100,
                            disk_gb=40, compute_cap=cc)

    for cc in (700, 750, 610):        # V100 · 튜링(RTX 20xx·T4·GTX 16xx) · 파스칼
        assert any("bf16" in w for w in warn(cc)), f"cc {cc} 에 bf16 경고가 없다"
    for cc in (800, 860, 890, 900):   # 암페어 이상 — 하드웨어 bf16 이 있다
        assert not any("bf16" in w for w in warn(cc)), f"cc {cc} 에 쓸데없는 bf16 경고"


def test_the_bf16_warning_says_what_to_do_instead():
    """⚠ 튜링은 fp16 텐서코어는 있고 bf16 만 없다 — "느립니다" 로 끝내면 사람이
    할 수 있는 것이 없다."""
    (w,) = [w for w in warnings_for(cpu_cores=8, cuda_max_good=12.6, disk_space_gb=100,
                                    disk_gb=40, compute_cap=750) if "bf16" in w]
    assert "fp16" in w and ("느려" in w or "느립" in w)


def test_an_unknown_generation_is_not_accused_of_anything():
    """⚠ 모르는 것을 경고로 만들면 표가 시끄러워지고, 시끄러운 표는 아무도 안 읽는다."""
    assert not any("bf16" in w for w in warnings_for(
        cpu_cores=8, cuda_max_good=12.6, disk_space_gb=100, disk_gb=40, compute_cap=None))


def test_bf16_and_generation_warnings_do_not_overlap():
    """⚠ 판정이 겹치면 한 행에 같은 말이 두 줄로 뜬다.

    cc 500~790 은 **이미지로는 멀쩡히 도는데** bf16 만 없는 구간이다 — 세대 경고와는
    다른 이야기다. 반대로 커널이 아예 없는 세대(`too_new`)는 bf16 을 따질 일이 없다.
    """
    def warn(cc):
        return warnings_for(cpu_cores=8, cuda_max_good=12.6, disk_space_gb=100,
                            disk_gb=40, compute_cap=cc)

    turing = warn(750)
    assert any("bf16" in w for w in turing)
    assert not any("커널" in w for w in turing), "돌아가는 기계에 세대 경고를 붙였다"

    blackwell = warn(1200)                   # too_new — 이미지에 커널이 없다
    assert any("커널" in w for w in blackwell)
    assert not any("bf16" in w for w in blackwell), "못 도는 기계에 bf16 까지 얹었다"


def test_an_unknown_compute_cap_is_left_alone_even_though_it_may_be_old():
    """⚠ 실측의 `140`·`243` 은 실물이 파스칼이라 bf16 이 **없다.** 그런데도 경고를 안
    붙인다 — 이 파일의 규칙이 "알 수 없는 값을 단정하지 않는다" 이고(`gpu_support`),
    한쪽만 예외로 두면 두 판정이 서로 다른 말을 하게 된다. 세대를 모른다는 사실 자체는
    `support='unknown'` 으로 이미 화면에 간다.
    """
    from app.services.cloud.providers.base import gpu_support

    for cc in (140, 243):
        assert gpu_support(cc) == "unknown"
        assert not any("bf16" in w for w in warnings_for(
            cpu_cores=8, cuda_max_good=12.6, disk_space_gb=100, disk_gb=40,
            compute_cap=cc))


# ─────────────────────────────────────────────────────────────────────────────
# 판정이 **고른 템플릿**을 따라간다 (2026-09-17)
# ─────────────────────────────────────────────────────────────────────────────

def test_cu128_is_not_a_superset_of_cu126():
    """⚠ **실측 2026-09-17** — 두 이미지를 띄워 arch list 를 직접 찍었다:

        cu126: sm_50 sm_60 sm_70 sm_75 sm_80 sm_86 sm_90
        cu128: sm_75 sm_80 sm_86 sm_90 sm_100 sm_120

    cu128 은 Blackwell 을 얻는 대신 맥스웰·파스칼 **그리고 V100(sm_70)** 을 잃는다.
    코드 주석이 한때 "cu128 은 sm_70~sm_120" 이라 적고 있었는데, 그건 이 머신의
    torch **2.10** 기준이었다 — 2.11 의 cu128 은 튜링(7.5)부터다.
    """
    from app.services.cloud.providers.base import gpu_support

    assert gpu_support(700, "cu126") == "ok", "V100 은 cu126 에서 돈다"
    assert gpu_support(700, "cu128") == "too_old", "V100 은 cu128 에서 빠진다"
    assert gpu_support(1200, "cu126") == "too_new", "5090 은 cu126 에 커널이 없다"
    assert gpu_support(1200, "cu128") == "ok", "5090 은 cu128 에서 돈다"
    # 어느 쪽도 전부를 덮지 못한다 — 그게 "상위집합이 아니다" 의 뜻이다
    assert gpu_support(500, "cu126") == "ok" and gpu_support(500, "cu128") == "too_old"


def test_an_unknown_build_is_never_guessed():
    """⚠ 모르는 빌드에 cu126 범위를 빌려 쓰면 **그럴듯한 거짓말**이 되고, 그 대가는
    빌린 뒤에 드러난다."""
    from app.services.cloud.providers.base import gpu_support

    for cc in (500, 750, 890, 1200):
        assert gpu_support(cc, "cu999") == "unknown"


def test_the_warning_names_the_build_you_picked():
    """⚠ 판정이 고정이던 시절엔 문구도 고정이라, cu128 을 고른 사람에게 "cu128 로
    구우세요" 라고 말했다."""
    w126 = warnings_for(cpu_cores=8, cuda_max_good=12.8, disk_space_gb=100, disk_gb=40,
                        compute_cap=1200, cuda="cu126")
    assert any("cu126" in x and "cu128" in x for x in w126)
    # cu128 에서는 5090 이 멀쩡하므로 커널 경고 자체가 없다
    w128 = warnings_for(cpu_cores=8, cuda_max_good=12.8, disk_space_gb=100, disk_gb=40,
                        compute_cap=1200, cuda="cu128")
    assert not any("커널" in x for x in w128)


def test_bf16_does_not_follow_the_build_because_it_is_hardware():
    """⚠ cu128 로 다시 구워도 튜링에 bf16 텐서코어가 생기지는 않는다."""
    for build in ("cu126", "cu128"):
        w = warnings_for(cpu_cores=8, cuda_max_good=12.8, disk_space_gb=100, disk_gb=40,
                         compute_cap=750, cuda=build)
        assert any("bf16" in x for x in w), f"{build} 에서 bf16 경고가 사라졌다"


def test_the_cache_is_keyed_by_build_or_switching_templates_lies(monkeypatch):
    """⚠ 판정이 빌드를 타는데 캐시 키가 그대로면, 템플릿을 cu128 로 바꿔도 **cu126
    판정이 그대로 나온다** — 화면은 바뀐 줄 아는데 내용은 안 바뀐다."""
    import inspect

    from app.services.cloud.providers import vast

    for fn in (vast.VastProvider.search, vast.VastProvider.catalog):
        src = inspect.getsource(fn)
        i = src.index("key = ")
        assert "cuda_build" in src[i:i + 200], f"{fn.__name__} 캐시 키에 빌드가 없다"


def test_the_endpoints_take_the_build_and_pass_it_down():
    """⚠ 파라미터만 받고 안 넘기면 아무 일도 안 일어난다 — 조용히."""
    import inspect

    from app.routers import cloud as router

    for fn in (router.vast_offers, router.cloud_gpus):
        sig = inspect.signature(fn)
        assert "cuda" in sig.parameters, f"{fn.__name__} 이 빌드를 안 받는다"
        assert "cuda_build=cuda" in inspect.getsource(fn), \
            f"{fn.__name__} 이 받은 빌드를 프로바이더로 안 넘긴다"


def test_the_build_parameter_is_a_whitelist():
    """⚠ 이 값은 판정에 쓰이고 캐시 키에도 들어간다. 자유 문자열로 두면 키가 무한히
    늘고, 형태 검사 없이 받는 습관이 이 파일의 규칙(주입 경계)과 어긋난다."""
    import inspect

    from app.routers import cloud as router

    src = inspect.getsource(router.vast_offers)
    assert r'pattern=r"^(cu\d{3})?$"' in src, "형태 검사가 없다"


def test_the_rent_tab_sends_the_build_and_refetches_when_it_changes():
    """⚠ 템플릿을 바꿨는데 다시 안 물어보면 표는 옛 판정을 그대로 보여 준다."""
    from pathlib import Path

    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[2] / "frontend" / "src"
                     / "components" / "CloudRentTab.tsx").read_text())
    assert src.count("p.set('cuda', template.cuda)") == 2, "오퍼·기종 중 한쪽만 보낸다"
    assert src.count("template?.cuda])") == 2, "템플릿이 바뀌어도 다시 안 부른다"
