"""학습 파라미터 스키마가 정책 목록과 어긋나지 않는지.

원래 프론트 `POLICY_TRAIN_SCHEMAS` 가 **두 번째 목록**이었고 실제로 어긋났다 —
`pi0_fast`/`tdmpc`/`vqbet` 은 학습 가능한데 스키마가 없어서 **선택해도 화면이
전혀 안 바뀌었다.**

이제 둘 다 `policies/*.yaml` 한 곳에서 온다(feature/policy-ui-spec.md).
그래도 검사는 남긴다 — 파일을 새로 추가할 때 `train.fields` 를 빼먹는 것은
여전히 가능하고, 증상이 예전과 똑같다.
"""

import re
from pathlib import Path

from app.core import policies

_PAGE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "TrainingPage.tsx"
_RAW = _PAGE.read_text()
# ⚠ **주석을 걷고 본다.** 이 저장소는 "왜 이렇게 됐는지"를 코드 옆에 적으므로,
# 옛 조건을 설명하는 주석이 흔하다. 원문으로 검사하면 그 설명이 걸려서
# "고쳤는데 테스트가 실패"한다 — 이번 세션에만 여러 번 겪었다.
_SRC = re.sub(r"\{?/\*[\s\S]*?\*/\}?|//.*", "", _RAW)


def _schema_keys() -> set[str]:
    """`train.fields` 를 가진 정책들."""
    return {n for n, s in policies.SPECS.items() if s.train.fields}


def _fields(name: str):
    return policies.SPECS[name].train.fields


def test_every_trainable_policy_has_a_schema():
    """스키마가 없으면 그 정책을 골라도 UI 가 아무 반응이 없다."""
    missing = set(policies.trainable()) - _schema_keys()
    assert not missing, f"학습 가능한데 프론트 스키마가 없다: {sorted(missing)}"


def test_schemas_only_cover_known_policies():
    """레지스트리에 없는 정책의 스키마는 죽은 코드다.

    ⚠ **미지원(`supported=False`) 정책의 스키마는 남겨둔다** — 지금은 ACT·SmolVLA 만
    켜져 있지만, 되살릴 때 스키마까지 다시 쓰지 않아도 되게 한다.
    """
    from app.core.policies import POLICIES

    unknown = _schema_keys() - set(POLICIES)
    assert not unknown, f"레지스트리에 없는 정책의 스키마: {sorted(unknown)}"


def test_architecture_fields_are_marked():
    """`arch` 표시가 있어야 pretrained 일 때 무엇을 가릴지 판단할 수 있다.

    예전에는 패널 **전체**를 숨겨서 `freeze_vision_encoder` 처럼 파인튜닝에서 가장
    중요한 학습 스위치까지 사라졌다.
    """
    assert any(f.arch for n in _schema_keys() for f in _fields(n)), \
        "아키텍처 값을 하나도 표시하지 않았다"
    assert "pretrainedPath && f.arch" in _SRC, "pretrained 일 때 arch 만 가리지 않는다"


def test_finetuning_switches_stay_visible():
    """파인튜닝의 핵심 스위치는 pretrained 여도 보여야 한다.

    이 키들이 `arch` 로 표시되면 체크포인트 학습에서 통째로 숨어버린다 —
    정작 파인튜닝에서 제일 자주 만지는 값들이다.
    """
    switches = {"freeze_vision_encoder", "train_expert_only", "load_vlm_weights"}
    for name in _schema_keys():
        for f in _fields(name):
            if f.key in switches:
                assert not f.arch, f"{name}.{f.key} 를 아키텍처로 표시했다"


def test_architecture_fields_actually_exist():
    """반대 방향 — 모델 구조 값은 반드시 `arch` 로 표시돼야 pretrained 때 가려진다."""
    for key in ("chunk_size", "dim_model", "n_obs_steps", "use_vae"):
        found = [(n, f) for n in _schema_keys() for f in _fields(n) if f.key == key]
        assert found, f"{key} 필드가 어느 정책에도 없다"
        for name, f in found:
            assert f.arch, f"{name}.{key} 는 구조 값인데 arch 표시가 없다"


# ── 파인튜닝 후보 목록 ───────────────────────────────────────────────────────

def test_non_policy_models_are_flagged():
    """**회귀** — models 디렉토리에는 정책이 아닌 것도 있다.

    `HuggingFaceTB/SmolVLM2-500M-Video-Instruct` 는 smolvla 의 비전-언어 백본이라
    반드시 거기 있어야 하는데, 파인튜닝 목록에 떠서 고르면 학습이 깨졌다.
    LeRobot 정책은 config.json 에 판별자 `type` 을 반드시 갖는다 — 그게 기준이다.
    """
    from app.services.model_scanner import _policy_type_of

    assert _policy_type_of({"type": "act"}) == ("act", True)
    # 실제로 목록을 오염시킨 그 config 의 모양
    assert _policy_type_of({"model_type": "smolvlm",
                            "architectures": ["SmolVLMForConditionalGeneration"]})[1] is False
    assert _policy_type_of({})[1] is False


def test_legacy_target_form_is_still_recognised():
    """옛 `_target_` 형식을 못 알아보면 **멀쩡한 체크포인트가 목록에서 사라진다.**

    판정이 이제 숨기는 방향으로 쓰이므로 거짓 음성이 더 위험하다.
    """
    from app.services.model_scanner import _policy_type_of

    assert _policy_type_of({"_target_": "lerobot.policies.smolvla.SmolVLAConfig"}) == ("smolvla", True)
    assert _policy_type_of({"_target_": "x.PI0FastConfig"}) == ("pi0_fast", True)
    assert _policy_type_of({"type": "ACTConfig"}) == ("act", True)


def test_finetune_list_is_filtered_by_policy():
    """다른 정책의 체크포인트로는 이어서 학습할 수 없다 — 목록에 뜨면 안 된다."""
    assert "finetuneCandidates" in _SRC, "파인튜닝 후보를 거르지 않는다"
    assert "m.policy_type === policyType" in _SRC, "정책이 같은 것만 남기지 않는다"
    assert "m.is_policy !== false" in _SRC, "정책이 아닌 모델을 걸러내지 않는다"
    # 드롭다운이 걸러진 목록을 쓰는지 (원본 `models` 를 그대로 쓰면 의미가 없다)
    dd = _SRC.split('<option value="">처음부터 학습</option>', 1)[1].split("</select>", 1)[0]
    assert "finetuneCandidates.map" in dd, f"드롭다운이 전체 목록을 쓴다: {dd.strip()[:80]}"


def test_mismatched_selection_is_cleared_on_policy_change():
    """화면에서 사라진 선택이 CLI 인자에는 남으면 "왜 다른 모델로 학습되지"가 된다."""
    handler = _SRC.split("const handlePolicyChange", 1)[1].split("const setPolicyParam", 1)[0]
    assert "setPretrainedPath('')" in handler, "정책을 바꿔도 안 맞는 체크포인트가 남는다"


def test_vla_policies_recommend_a_base_checkpoint():
    """목록이 비면 "처음부터 학습"밖에 못 고른다 — VLA 는 그러면 사실상 못 쓴다.

    필터를 넣은 뒤 실제로 이 상태가 됐다. 무엇을 받아야 하는지 알려주고
    바로 받을 수 있어야 막다른 길이 아니다.
    """
    # 목록은 백엔드 레지스트리 하나에서 온다 — 프론트가 또 만들면 갈라진다
    assert "BASE_CHECKPOINTS" not in _SRC, "프론트가 베이스 목록을 따로 들고 있다"
    assert "policyBase(policyType)" in _SRC, "백엔드가 준 권장 시작점을 안 쓴다"
    assert "hub/download" in _SRC, "받기 버튼이 Hub 다운로드를 부르지 않는다"

    from app.core.policies import POLICIES, spec_for_frontend

    assert POLICIES["smolvla"]["policy_base"] == "lerobot/smolvla_base"
    assert all("policy_base" in s for s in spec_for_frontend()), "API 가 안 내려준다"


def test_random_vlm_init_is_warned():
    """`load_vlm_weights=false` 면 LeRobot 이 VLM 을 **랜덤 초기화**한다.

    LeRobot 기본값이 false 라 그냥 두면 조용히 그렇게 된다 —
    이 저장소가 실제로 당했던 문제다(커밋 ce768bc, "silently using a random vision encoder").
    """
    warns = policies.SPECS["smolvla"].train.warnings
    assert any(w.when.field == "load_vlm_weights" and w.when.is_ is False and w.level == "error"
               for w in warns), "랜덤 초기화 상태를 경고하지 않는다"
    # 화면이 조건을 다시 적지 않는다 — 갈리면 한쪽만 뜬다
    assert "load_vlm_weights === false" not in _SRC, "화면이 경고 조건을 또 적고 있다"


def test_resnet_backbone_is_not_left_random():
    """**회귀** — Diffusion/VQ-BeT 의 `pretrained_backbone_weights` 기본값은 `None` 이다.

    `get_model(..., weights=None)` 은 ResNet18 을 랜덤 초기화한다 —
    smolvla 의 `load_vlm_weights=False` 와 정확히 같은 함정이다.
    ACT 만 자체 기본값이 ImageNet 이라 손댈 필요가 없다(그래서 받을 베이스도 없다).
    """
    from app.core.cli_mapping import build_train_args

    def backbone_args(policy: str, **extra) -> list[str]:
        args = build_train_args({"policy_type": policy, "dataset_repo_id": "x/y",
                                 "output_dir": "/tmp/o", **extra})
        return [a for a in args if "pretrained_backbone_weights" in a]

    for policy in ("diffusion", "vqbet"):
        assert backbone_args(policy) == [
            "--policy.pretrained_backbone_weights=ResNet18_Weights.IMAGENET1K_V1"
        ], f"{policy} 가 랜덤 백본으로 학습된다"

    assert backbone_args("act") == [], "ACT 는 자체 기본값이 ImageNet 이라 건드리면 안 된다"

    # 사용자가 직접 지정하면 그 값을 쓴다 — 안전망이 덮어쓰면 안 된다
    assert backbone_args("diffusion", policy_params={"pretrained_backbone_weights": "x"}) == [
        "--policy.pretrained_backbone_weights=x"
    ]
    # 체크포인트에서 이어 학습하면 가중치가 거기서 오므로 아무것도 안 붙인다
    assert backbone_args("diffusion", pretrained_path="/some/ckpt") == []


def test_only_act_and_smolvla_are_supported_for_now():
    """지금 지원 범위 — 나머지는 **정의를 남긴 채** 꺼져 있다.

    스키마·권장 베이스·백본 안전망이 이미 붙어 있으므로,
    되살릴 때는 `"supported": True` 한 줄이면 된다.
    """
    from app.core.policies import POLICIES

    assert set(policies.supported()) == {"act", "smolvla"}
    # 어느 목록에도 미지원 정책이 새지 않는다
    for names in (policies.trainable(), policies.inferable(),
                  policies.rtc_policies(), sorted(policies.encoder_probe_policies())):
        assert set(names) <= {"act", "smolvla"}, f"미지원 정책이 노출된다: {names}"
    assert {s["type"] for s in policies.spec_for_frontend()} == {"act", "smolvla"}

    # 꺼둔 것들의 정의는 지우지 않았다 — 되살릴 때 다시 쓰지 않게
    assert {"diffusion", "pi0", "pi05", "pi0_fast", "tdmpc", "vqbet"} <= set(POLICIES)


def test_disabled_policies_still_tag_hub_models():
    """`guess_from_name` 은 지원 여부와 무관해야 한다.

    이미 받아둔 pi0 체크포인트가 "정체 불명"이 되면 모델 목록이 이상해진다 —
    **고를 수 없는 것과 알아보지 못하는 것은 다르다.**
    """
    assert policies.guess_from_name("lerobot/pi05_base") == "pi05"
    assert policies.guess_from_name("someone/act_pick") == "act"
