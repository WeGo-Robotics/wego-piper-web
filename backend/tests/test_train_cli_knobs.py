"""학습 화면의 칸이 **실제로 학습에 닿는가** — Learning Rate · 이미지 증강 · CLI 편집.

2026-10-30 이어 학습에서 셋 다 걸렸다:

- Learning Rate 칸은 `--optimizer.lr` 로 나가는데, 프리셋이 켜져 있으면 LeRobot 이 optimizer 를
  정책 config 의 `optimizer_lr` 로 다시 만들어 그 값을 **말없이 버린다**.
- 이미지 증강은 화면에 없었다. LeRobot 기본 묶음은 색조를 흔들어 빨강/노랑 분류에 해롭다.
- 화면의 CLI 편집은 공백으로 인자를 자르는데 rename_map 이 들여쓴 JSON 이라 조각났다.
"""

import json

import pytest

from app.core.cli_mapping import build_train_args

BASE = {"dataset_repo_id": "org/name", "policy_type": "smolvla_s", "batch_size": 16, "steps": 40000}
PRETTY_RENAME = json.dumps({
    "observation.images.left_top": "observation.images.camera1",
    "observation.images.left_hand": "observation.images.camera2",
}, indent=2)


def _flag(args, prefix):
    return [a for a in args if a.startswith(prefix)]


# ── Learning Rate ──

def test_lr_with_preset_goes_to_the_policy_config():
    args = build_train_args({**BASE, "learning_rate": 2e-5})
    assert _flag(args, "--policy.optimizer_lr=") == ["--policy.optimizer_lr=2e-05"]
    assert not _flag(args, "--optimizer.lr"), "프리셋이 버리는 자리로 보내면 안 된다"


def test_lr_with_pretrained_checkpoint_too():
    """이어 학습(`--policy.path`)에서도 정책 쪽 덮어쓰기는 먹는다 — 그게 이번에 필요했던 경우다."""
    args = build_train_args({**BASE, "pretrained_path": "/ckpt/pretrained_model", "learning_rate": 2e-5})
    assert "--policy.optimizer_lr=2e-05" in args


def test_lr_without_preset_stays_on_the_optimizer():
    args = build_train_args({**BASE, "learning_rate": 2e-5, "use_policy_training_preset": False})
    assert "--optimizer.lr=2e-05" in args
    assert not _flag(args, "--policy.optimizer_lr")


def test_no_lr_means_no_override():
    args = build_train_args(BASE)
    assert not _flag(args, "--optimizer.lr") and not _flag(args, "--policy.optimizer_lr")


# ── 이미지 증강 ──

def test_aug_off_by_default():
    assert not _flag(build_train_args(BASE), "--dataset.image_transforms")


def test_photometric_aug_leaves_colour_alone():
    args = build_train_args({**BASE, "image_aug": "photometric"})
    assert "--dataset.image_transforms.enable=true" in args
    [tfs] = _flag(args, "--dataset.image_transforms.tfs=")
    tfs = json.loads(tfs.split("=", 1)[1])
    assert set(tfs) == {"brightness", "contrast", "sharpness"}
    assert not {"hue", "saturation", "affine"} & set(tfs)


def test_full_aug_is_lerobot_default_set():
    args = build_train_args({**BASE, "image_aug": "full"})
    assert "--dataset.image_transforms.enable=true" in args
    assert not _flag(args, "--dataset.image_transforms.tfs"), "기본 묶음은 LeRobot 이 정한다"


def test_unknown_aug_is_refused():
    with pytest.raises(ValueError):
        build_train_args({**BASE, "image_aug": "strong"})


def test_lerobot_parses_the_photometric_set():
    """점 경로(`tfs.hue.weight=0`)는 draccus 가 거부했다 — 통째로 넘긴 dict 가 실제로 먹는지."""
    draccus = pytest.importorskip("draccus")
    default = pytest.importorskip("lerobot.configs.default")
    args = build_train_args({**BASE, "image_aug": "photometric"})
    ds_args = ["--repo_id=x"] + [a.replace("--dataset.", "--", 1) for a in _flag(args, "--dataset.image_transforms")]
    cfg = draccus.parse(default.DatasetConfig, args=ds_args)
    assert cfg.image_transforms.enable
    assert set(cfg.image_transforms.tfs) == {"brightness", "contrast", "sharpness"}


# ── CLI 편집 ──

def test_rename_map_is_compacted_without_changing_it():
    args = build_train_args({**BASE, "rename_map": PRETTY_RENAME})
    [rm] = _flag(args, "--rename_map=")
    assert json.loads(rm.split("=", 1)[1]) == json.loads(PRETTY_RENAME)


def test_no_argument_has_whitespace():
    """화면의 CLI 편집은 `split(/\\s+/)` 로 자른다 — 인자 안에 공백이 있으면 고쳐서 시작할 때 갈라진다."""
    args = build_train_args({
        **BASE, "pretrained_path": "/ckpt/pretrained_model", "rename_map": PRETTY_RENAME,
        "learning_rate": 2e-5, "image_aug": "photometric",
    })
    assert [a for a in args if any(c.isspace() for c in a)] == []
