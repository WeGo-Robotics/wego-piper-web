"""학습이 **받아 둔 데이터셋을 실제로 찾는가**.

⚠ `--dataset.repo_id` 만 주면 lerobot 은 `HF_LEROBOT_HOME/<repo_id>`
(`~/.cache/huggingface/lerobot/…`) 를 본다. 그런데 우리가 받는 자리는 HF **허브 캐시**
(`~/.cache/huggingface/hub/datasets--…`) 다. 못 찾으면 lerobot 은 허브에 코드베이스
**태그**(`v3.0`)를 물으러 가고, 태그 없는 저장소에서는 거기서 죽는다.

실기(2026-09-23, `wego-mink/sim_two_box_3_120`): 그 저장소는 태그가 없어서

    RevisionNotFoundError → TypeError: HfHubHTTPError.__init__() missing 'response'

로 끝났다 — 진짜 사유(`meta/info.json` 을 못 찾음)가 두 겹 뒤에 묻혔다.

`root` 에 `meta/info.json` 이 있으면 lerobot 은 **허브를 아예 안 탄다**(실측).
"""

from pathlib import Path

from app.core.cli_mapping import TRAIN_ARGS_MAP, build_train_args
from app.routers.training import add_local_dataset_root

REPO = "org/name"


def test_the_flag_exists_at_all():
    assert TRAIN_ARGS_MAP.get("dataset_root") == "--dataset.root"


def test_local_training_is_told_where_the_dataset_actually_is(monkeypatch, tmp_path):
    monkeypatch.setattr("app.routers.training.find_dataset_path", lambda _id: tmp_path)
    p = add_local_dataset_root({"dataset_repo_id": REPO}, remote=False)
    assert p["dataset_root"] == str(tmp_path)
    assert f"--dataset.root={tmp_path}" in build_train_args(p)


def test_remote_training_is_not(monkeypatch, tmp_path):
    """⚠ 그 경로는 임대 서버에 **없다.** 거기서는 이미지가 제 손으로 받는다."""
    monkeypatch.setattr("app.routers.training.find_dataset_path", lambda _id: tmp_path)
    p = add_local_dataset_root({"dataset_repo_id": REPO}, remote=True)
    assert "dataset_root" not in p


def test_a_dataset_we_cannot_find_is_not_faked(monkeypatch):
    """못 찾았으면 아무 경로나 지어내지 않는다 — lerobot 이 제 규칙으로 찾게 둔다."""
    monkeypatch.setattr("app.routers.training.find_dataset_path", lambda _id: None)
    assert "dataset_root" not in add_local_dataset_root({"dataset_repo_id": REPO}, remote=False)


def test_the_preview_shows_the_same_command_that_runs():
    """⚠ 처음 고쳤을 때 `/start` 에만 넣어서 **미리보기가 실제와 달랐다.** 미리보기가
    실행과 다르면 그건 미리보기가 아니라 거짓말이다."""
    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[1]
                     / "app/routers/training.py").read_text())
    assert src.count("add_local_dataset_root(") >= 3, \
        "정의 + /start + /preview 셋이 아니면 한쪽이 빠진 것이다"
