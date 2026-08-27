"""act_aux ↔ 저장소 계약 (feature/act-aux.md).

정책 패키지는 `piper_phase` 를 import 하지 않는다 (학습 머신엔 phase 패키지가 없을 수 있다).
대신 **여기서** 두 이름 목록을 대조한다 — 어긋나면 굽기가 쓴 subtask 이름과 정책의 클래스
인덱스가 조용히 달라진다.
"""

import importlib
import inspect
from pathlib import Path

import pytest

from conftest import code_only

from app.core import policies as P


def test_default_stage_names_match_piper_phase():
    cfg_mod = pytest.importorskip("lerobot_policy_act_aux.configuration_act_aux")
    phase = pytest.importorskip("piper_phase")
    assert tuple(cfg_mod.DEFAULT_STAGE_NAMES) == tuple(phase.PHASE_NAMES)


def test_spec_runtime_paths_are_importable():
    """yaml 의 runtime 경로가 실제 클래스를 가리키는가 — wrapper 가 이 경로로 import 한다."""
    pytest.importorskip("lerobot_policy_act_aux")
    spec = P.SPECS["act_aux"]
    for mod, cls in (spec.runtime.model, spec.runtime.config):
        assert hasattr(importlib.import_module(mod), cls), f"{mod}.{cls}"


def test_act_aux_is_a_supported_sibling_of_act():
    assert P.POLICIES["act_aux"]["supported"]
    order = list(P.POLICIES)
    assert order.index("act_aux") == order.index("act") + 1, "목록에서 ACT 바로 뒤여야 한다"
    assert P.guess_from_name("org/act_aux_cube") == "act_aux"
    assert P.guess_from_name("org/act_cube") == "act"


def test_baked_info_detects_stale_and_missing_source(tmp_path):
    """구운 사본의 `stale`/`source_missing` — 원본 라벨이 bake 뒤 바뀌면 재굽기 배지가 떠야 한다."""
    import hashlib
    import json

    from app.services.dataset_scanner import baked_info

    src = tmp_path / "org" / "orig"
    (src / "meta").mkdir(parents=True)
    labels = src / "meta" / "phase_labels.json"
    labels.write_text('{"episodes": {}}')

    dst = tmp_path / "org" / "orig_stage"
    (dst / "meta").mkdir(parents=True)
    meta = {"source": str(src), "source_repo_id": "org/orig", "stage_names": ["A", "B"],
            "source_labels_sha256": hashlib.sha256(labels.read_bytes()).hexdigest()}
    (dst / "meta" / "act_aux.json").write_text(json.dumps(meta))

    assert baked_info(src) is None                      # 원본은 구운 사본이 아니다
    info = baked_info(dst)
    assert info == {"source": "org/orig", "stale": False, "source_missing": False,
                    "stage_names": ["A", "B"], "class_counts": {}}

    labels.write_text('{"episodes": {"0": {}}}')         # 원본 라벨을 고쳤다
    assert baked_info(dst)["stale"] is True

    labels.unlink()
    info = baked_info(dst)
    assert info["source_missing"] is True and info["stale"] is False


# ── wrapper 추론 배선 (feature/act-aux.md §6.1) ────────────────────────────────
#
# 정책이 stage 를 내도 wrapper 가 안 읽으면 계산만 하고 버린다. 실제로 그 상태로
# 한동안 있었다 — 모델은 완성이고 배선이 없었다. 아래가 그 배선을 잡아 둔다.

WRAPPER = Path(__file__).resolve().parents[2] / "wrapper" / "lerobot_wrapper.py"


def _wrapper_src() -> str:
    return WRAPPER.read_text()


def test_wrapper_reads_the_stage_the_policy_produces():
    """모델이 내는 키와 wrapper 가 읽는 키가 같아야 한다.

    `last_aux` 의 키 이름이 바뀌면 wrapper 는 조용히 `None` 을 받고 단계 표시가
    사라진다 — 예외가 없으므로 화면을 안 보면 모른다.
    """
    mod = pytest.importorskip("lerobot_policy_act_aux.modeling_act_aux")
    src = inspect.getsource(mod.ActAuxPolicy._aux_from)
    for key in ("stage", "stage_p"):
        assert f'"{key}"' in src, f"모델이 더는 {key} 를 내지 않는다"
        assert f'"{key}"' in _wrapper_src(), f"wrapper 가 {key} 를 안 읽는다"


def test_wrapper_guards_the_attribute_for_other_policies():
    """바닐라 정책엔 `last_aux` 가 없다 — 직접 접근하면 추론 스레드가 죽는다."""
    src = _wrapper_src()
    assert 'getattr(policy, "last_aux", None)' in src, "guarded 접근이 아니다"
    assert "policy.last_aux" not in code_only(src), "직접 접근이 남아 있다"


def test_stage_names_come_from_the_checkpoint():
    """이름표는 체크포인트 config 에서 온다 — wrapper 가 piper_phase 를 import 하면 안 된다.

    학습 머신과 달리 추론 머신엔 phase 패키지가 없을 수 있고, 다른 태스크로 구운
    모델은 이름 자체가 다르다.
    """
    src = _wrapper_src()
    assert 'getattr(policy.config, "stage_names"' in src
    assert "piper_phase" not in code_only(src)


def test_stage_is_published_under_the_same_lock_as_the_actions():
    """낡은 세대 결과가 폐기될 때 stage 도 함께 폐기돼야 한다.

    락 밖에서 공표하면 버려진 추론의 단계가 화면에 남는다.
    """
    src = _wrapper_src()
    lock_body = src[src.index("with _action_lock:\n                    # 연산 도중"):]
    lock_body = lock_body[: lock_body.index("_inference_ms_shared =")]
    assert "_latest_aux = aux" in lock_body
