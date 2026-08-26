"""act_aux ↔ 저장소 계약 (feature/act-aux.md).

정책 패키지는 `piper_phase` 를 import 하지 않는다 (학습 머신엔 phase 패키지가 없을 수 있다).
대신 **여기서** 두 이름 목록을 대조한다 — 어긋나면 굽기가 쓴 subtask 이름과 정책의 클래스
인덱스가 조용히 달라진다.
"""

import importlib

import pytest

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
