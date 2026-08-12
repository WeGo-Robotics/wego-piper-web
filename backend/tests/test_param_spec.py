"""추론 파라미터 단일 소스 (refactor/01-inference-params.md 단계 2).

원래 문제: 파라미터 하나가 **네 곳**에 따로 적혀 있었다 —
프론트 기본값 / 프론트 슬라이더 / `SAFE_PARAMS` / `override_keys`.
하나라도 빠지면 **에러 없이 조용히 값이 유실**됐고, 실제로 세 건이 어긋나 있었다.
"""

import re
from pathlib import Path

import pytest

from app.core import inference_params as P
from app.core.cli_mapping import OVERRIDE_KEYS
from app.services.param_bridge import BOOL_PARAMS, SAFE_PARAMS, UNSAFE_PARAMS

_REPO = Path(__file__).resolve().parents[2]
_INFERENCE_PAGE = _REPO / "frontend" / "src" / "pages" / "InferencePage.tsx"


# ── 파생이 실제로 파생인가 ──

def test_safe_params_derive_from_spec():
    assert SAFE_PARAMS == P.bounds()


def test_bool_params_derive_from_spec():
    assert BOOL_PARAMS == P.bool_params()


def test_override_keys_derive_from_spec():
    assert OVERRIDE_KEYS == P.start_params()


def test_unsafe_params_are_not_in_spec():
    """재시작이 필요한 모델 아키텍처 값은 실시간 스펙에 있으면 안 된다."""
    assert not (UNSAFE_PARAMS & set(P.PARAM_SPEC))


# ── 스펙 자체의 불변식 ──

def test_realtime_params_are_sent_at_start():
    """실시간 변경되는데 시작값이 안 가면 "슬라이더를 움직여야 적용되는 값"이 생긴다.

    `fps` 는 `--fps` CLI 인자로 따로 가므로 예외.
    """
    missing = P.realtime_params() - P.start_params() - {"fps"}
    assert not missing, f"실시간인데 시작 시 전달 안 됨: {missing}"


@pytest.mark.parametrize("key", sorted(P.PARAM_SPEC))
def test_every_param_has_label_and_default(key):
    spec = P.PARAM_SPEC[key]
    assert spec.get("label"), f"{key} 에 label 이 없다"
    assert "default" in spec, f"{key} 에 default 가 없다"


@pytest.mark.parametrize(
    "key", sorted(k for k, v in P.PARAM_SPEC.items() if v.get("kind") != "bool")
)
def test_numeric_defaults_are_inside_range(key):
    """기본값이 범위를 벗어나면 로드 즉시 클램프돼 사용자가 이유를 모른다."""
    s = P.PARAM_SPEC[key]
    assert s["min"] <= s["default"] <= s["max"], f"{key}: {s['default']} ∉ [{s['min']},{s['max']}]"


def test_policy_scoped_params_reference_real_policies():
    """`policies` 에 없는 정책 이름을 적으면 그 파라미터가 영영 안 보인다."""
    from app.core.policies import POLICIES

    for key, spec in P.PARAM_SPEC.items():
        for name in spec.get("policies", []):
            assert name in POLICIES, f"{key}: 알 수 없는 정책 {name!r}"


# ── 프론트가 스펙을 쓰는가 ──

def test_frontend_sliders_use_the_spec():
    """드리프트 (2) 재발 방지 — 프론트에 min/max 를 손으로 적으면 백엔드와 갈린다."""
    src = _INFERENCE_PAGE.read_text()
    hardcoded = re.findall(r"<ParamSlider[^>]*?\bmin=\{[\d.]+\}", src, re.S)
    assert not hardcoded, (
        f"슬라이더 {len(hardcoded)}개가 범위를 하드코딩하고 있다 — rangeOf() 를 쓸 것"
    )
    assert "useParamSpec" in src


def test_frontend_has_no_local_defaults_block():
    """기본값도 서버에서 온다 — 프론트 defaults 는 스펙 도착 전 임시값일 뿐이다."""
    src = _INFERENCE_PAGE.read_text()
    assert "specDefaults" in src, "스펙 기본값을 병합하지 않고 있다"


def test_spec_endpoint_shape():
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/api/params/spec").json()
    assert set(r) == {"params", "defaults"}
    assert len(r["params"]) == len(P.PARAM_SPEC)
    for p in r["params"]:
        assert {"key", "label", "kind", "default", "group", "policies"} <= set(p)
