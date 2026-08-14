"""정책 UI 스펙 — `policies/*.yaml` (feature/policy-ui-spec.md).

여기서 잠그는 것:

1. **이관 전후 동일** — 공개 함수 결과가 안 바뀌었는가 (골든)
2. **상류 대조** — `from_lerobot` 기본값이 지금 LeRobot 값과 같은가
3. **표현 계층 금지** — 스펙이 UI 프레임워크로 자라지 않는가
4. **깨진 파일 격리** — 하나가 망가져도 나머지가 도는가
5. **TSX 에 정책 이름이 안 박혀 있는가** — 오늘 사고가 그 종류였다
"""

import ast
import importlib
import re
from pathlib import Path

import pytest
import yaml

from app.core import policies as P
from app.core import policy_spec as S

_REPO = Path(__file__).resolve().parents[2]
_POLICY_DIR = _REPO / "policies"
_SPEC_FILES = sorted(p for p in _POLICY_DIR.glob("*.yaml") if not p.name.startswith("_"))


def test_every_policy_has_a_file():
    assert _SPEC_FILES, "policies/*.yaml 이 하나도 없다"
    assert {p.stem for p in _SPEC_FILES} == set(P.SPECS)


# ── 1. 이관 전후 동일 ──────────────────────────────────────────────────────

def test_registry_output_is_unchanged():
    """**골든 테스트.** dict → YAML 이관에서 공개 함수 결과가 바뀌면 안 된다.

    이 값들은 이관 **직전에** 실행해 받아둔 것이다. 여기가 깨지면 스펙이 아니라
    이관이 틀린 것이다.
    """
    assert P.supported() == ["smolvla", "act"]
    assert P.trainable() == ["smolvla", "act"]
    assert P.inferable() == ["smolvla", "act"]
    assert P.rtc_policies() == ["smolvla"]
    assert P.encoder_probe_policies() == {"smolvla", "act"}
    # 긴 이름이 먼저 — "pi0" 가 앞이면 "pi05_base" 가 pi0 로 잡힌다
    assert P.TAG_MATCH_ORDER.index("pi05") < P.TAG_MATCH_ORDER.index("pi0")
    assert P.guess_from_name("lerobot/pi05_base") == "pi05"


def test_frontend_spec_keeps_its_shape():
    keys = {"type", "label", "train", "infer", "rtc", "language",
            "encoder_probe", "policy_base"}
    for row in P.spec_for_frontend():
        assert set(row) == keys, f"{row.get('type')}: 응답 모양이 바뀌었다"


def test_display_order_is_not_filename_order():
    """파일명 알파벳순이면 ACT 가 SmolVLA 앞으로 가서 목록이 뒤집힌다."""
    assert list(P.POLICIES)[:2] == ["smolvla", "act"]


# ── 2. 상류(LeRobot) 대조 ──────────────────────────────────────────────────

def _upstream(config_path):
    import dataclasses

    module_name, class_name = config_path
    cls = getattr(importlib.import_module(module_name), class_name)
    out = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            out[f.name] = f.default
    return out


@pytest.mark.parametrize("path", _SPEC_FILES, ids=lambda p: p.stem)
def test_defaults_match_lerobot(path):
    """`from_lerobot` 필드는 상류와 같아야 한다.

    지금 값들은 원래 사람이 LeRobot config 를 보고 TSX 에 옮긴 것이었다.
    YAML 로 형식만 바꾸면 드리프트가 한 층 위로 올라갈 뿐이라, 여기서 대조한다.
    **LeRobot 을 올렸을 때 기본값이 바뀌면 이 테스트가 알려준다.**
    """
    pytest.importorskip("lerobot")
    data = yaml.safe_load(path.read_text())
    cfg = (data.get("runtime") or {}).get("config")
    if not cfg:
        pytest.skip("runtime.config 없음")
    upstream = _upstream(cfg)

    for f in (data.get("train") or {}).get("fields", []):
        if not f.get("from_lerobot"):
            continue
        key = f["key"]
        assert key in upstream, (
            f"{path.stem}.{key}: LeRobot 에 그런 필드가 없다 — 화면에 띄우면 "
            f"학습 시작에서 알 수 없는 설정 키로 죽는다")
        assert f.get("default") == upstream[key], (
            f"{path.stem}.{key}: 스펙 {f.get('default')!r} vs LeRobot {upstream[key]!r} — "
            f"`python tools/gen_policy_spec.py --write` 로 맞춘다")


@pytest.mark.parametrize("path", _SPEC_FILES, ids=lambda p: p.stem)
def test_overrides_state_a_reason(path):
    """**이유 없는 이탈은 이탈이 아니라 오타다.**"""
    data = yaml.safe_load(path.read_text())
    for f in (data.get("train") or {}).get("fields", []):
        if (ov := f.get("override")) is not None:
            assert ov.get("reason"), f"{path.stem}.{f['key']}: override 에 reason 이 없다"
            assert ov.get("value") != f.get("default"), (
                f"{path.stem}.{f['key']}: override 값이 상류와 같다 — 이탈을 지워라")


def test_the_known_deviation_is_still_deliberate():
    """SmolVLA `load_vlm_weights` — 상류 false, 우리 true. 회귀 방지."""
    spec = P.SPECS["smolvla"]
    f = next(f for f in spec.train.fields if f.key == "load_vlm_weights")
    assert f.default is False and f.resolved_default() is True
    assert "freeze" in f.override.reason


# ── 3. 표현 계층 금지 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", _SPEC_FILES, ids=lambda p: p.stem)
def test_no_presentation_keys(path):
    """스펙이 *무엇을* 그릴지만 말하고 *어떻게* 는 화면이 정한다.

    `component`/`layout`/`order` 가 들어오기 시작하면 타입 검사도 디버깅도 안 되는
    JSX 를 YAML 로 쓰게 된다. 그 문이 열리는 것을 여기서 막는다.
    """
    assert S._forbidden(yaml.safe_load(path.read_text())) is None


def test_loader_rejects_presentation_keys(tmp_path, monkeypatch, caplog):
    (tmp_path / "bad.yaml").write_text(
        "spec_version: 1\ntype: bad\nlabel: Bad\ntrain:\n  fields:\n"
        "    - { key: x, kind: number, component: Slider }\n")
    monkeypatch.setattr(S, "BUILTIN_DIR", tmp_path)
    monkeypatch.setattr(S, "OVERLAY_DIR", tmp_path / "none")
    assert S.load_specs() == {}


# ── 4. 깨진 파일 하나가 전체를 죽이지 않는다 ────────────────────────────────

def test_a_broken_file_only_removes_itself(tmp_path, monkeypatch):
    (tmp_path / "good.yaml").write_text("spec_version: 1\ntype: good\nlabel: Good\n")
    (tmp_path / "broken.yaml").write_text("spec_version: 1\ntype: [unclosed\n")
    (tmp_path / "future.yaml").write_text("spec_version: 99\ntype: future\nlabel: F\n")
    (tmp_path / "invalid.yaml").write_text(
        "spec_version: 1\ntype: invalid\nlabel: I\ncapabilities: { nope: true }\n")
    monkeypatch.setattr(S, "BUILTIN_DIR", tmp_path)
    monkeypatch.setattr(S, "OVERLAY_DIR", tmp_path / "none")
    assert set(S.load_specs()) == {"good"}


def test_device_overlay_merges_deeply(tmp_path, monkeypatch):
    """기기별 파일은 한 값만 덮어써도 나머지가 살아 있어야 한다."""
    builtin, overlay = tmp_path / "b", tmp_path / "o"
    builtin.mkdir(); overlay.mkdir()
    (builtin / "act.yaml").write_text(
        "spec_version: 1\ntype: act\nlabel: ACT\nsupported: true\n"
        "capabilities: { train: true, infer: true }\n"
        "train:\n  defaults: { batch_size: 8, steps: 100000 }\n")
    (overlay / "act.yaml").write_text(
        "spec_version: 1\ntype: act\ntrain:\n  defaults: { steps: 500 }\n")
    monkeypatch.setattr(S, "BUILTIN_DIR", builtin)
    monkeypatch.setattr(S, "OVERLAY_DIR", overlay)
    spec = S.load_specs()["act"]
    assert spec.train.defaults == {"batch_size": 8, "steps": 500}
    assert spec.label == "ACT" and spec.capabilities.train is True


def test_ui_spec_for_unknown_policy_is_empty_not_an_error():
    """스펙은 **편의 계층**이지 안전 계층이 아니다 — 모르면 비우고 막지 않는다."""
    ui = P.ui_spec("nope")
    assert ui["train"]["fields"] == [] and ui["train"]["warnings"] == []


# ── 5. 화면에 정책 이름이 박혀 있지 않은가 ──────────────────────────────────

def test_training_page_has_no_policy_names_left():
    """**회귀** — `POLICY_TRAIN_SCHEMAS` 가 백엔드와 갈라져 `pi0_fast`·`tdmpc`·
    `vqbet` 은 골라도 화면이 안 바뀌었다. 같은 일이 다시 생기지 않게 잠근다."""
    src = (_REPO / "frontend/src/pages/TrainingPage.tsx").read_text()
    # 주석은 빼고 코드만 본다 — 주석에 정책 이름이 나오는 건 설명이다
    code = re.sub(r"\{?/\*[\s\S]*?\*/\}?|//.*", "", src)
    hits = re.findall(r"policyType\s*===\s*'(\w+)'", code)
    assert not hits, f"TrainingPage 에 정책 이름이 박혀 있다: {hits}"
    assert "POLICY_TRAIN_SCHEMAS" not in code
    assert "SCRATCH_WEIGHTS" not in code


def test_spec_fields_component_does_not_know_policies():
    """공용 폼이 정책을 알면 스펙을 만든 의미가 없다."""
    src = (_REPO / "frontend/src/components/SpecFields.tsx").read_text()
    for name in P.SPECS:
        assert f"'{name}'" not in src, f"SpecFields 가 {name} 을 안다"


def test_warning_condition_grammar_stays_small():
    """조건 문법이 자라면 YAML 안에 프로그램이 생긴다.

    `Condition` 이 받는 키는 `field` 와 `is` 뿐이어야 한다 — 늘리고 싶어지면
    그건 화면에 남길 진짜 로직이라는 신호다.
    """
    tree = ast.parse((_REPO / "backend/app/core/policy_spec.py").read_text())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Condition")
    names = {t.target.id for t in cls.body if isinstance(t, ast.AnnAssign)}
    assert names == {"field", "is_"}, f"조건 문법이 자랐다: {names}"
