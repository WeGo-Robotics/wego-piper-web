"""녹화 래퍼의 저장 가드 (wrapper/start_record.py `_install_dataset_guards`).

첫 에피소드 전 준비 대기(기본 켬) 중에 정지하면 프레임이 한 장도 안 들어왔다. LeRobot 은 에피소드 버퍼를
`add_frame` 에서야 만들므로 그때 `episode_buffer` 는 `None` 이고, 그대로 `save_episode()` 를 부르면
`validate_episode_buffer` 가 `argument of type 'NoneType' is not iterable` 로 녹화 프로세스를 죽였다
(실기 2026-10-29 23:52, 저장된 것은 없어 데이터 손실은 없다).

`start_record.py` 는 import 하는 순간 녹화를 시작하므로 모듈을 불러올 수 없다 — 함수 소스만 꺼내
가짜 LeRobotDataset 에 건다.
"""

import ast
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "wrapper" / "start_record.py"


@pytest.fixture
def dataset_cls(monkeypatch):
    class FakeDataset:
        saved: list = []

        def __init__(self, buf):
            self.episode_buffer = buf
            self.meta = types.SimpleNamespace(total_episodes=0)

        def save_episode(self, *args, **kwargs):
            FakeDataset.saved.append((args, kwargs))
            return "saved"

        def push_to_hub(self, *args, **kwargs):
            return "pushed"

    FakeDataset.saved = []
    mod = types.ModuleType("lerobot.datasets.lerobot_dataset")
    mod.LeRobotDataset = FakeDataset
    for name in ("lerobot", "lerobot.datasets"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "lerobot.datasets.lerobot_dataset", mod)

    tree = ast.parse(SRC.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_install_dataset_guards")
    ns: dict = {}
    exec(compile(ast.Module([fn], []), str(SRC), "exec"), ns)
    ns["_install_dataset_guards"]()
    return FakeDataset


def test_stopping_before_the_first_frame_saves_nothing(dataset_cls):
    """⚠ 요점. 버퍼가 `None` 이어도 죽지 않고 건너뛴다."""
    assert dataset_cls(None).save_episode() is None
    assert dataset_cls.saved == [], "빈 버퍼로 원래 저장을 불렀다"


def test_an_empty_buffer_is_still_skipped(dataset_cls):
    assert dataset_cls({"size": 0}).save_episode() is None
    assert dataset_cls.saved == []


def test_a_buffer_with_frames_is_saved(dataset_cls):
    assert dataset_cls({"size": 3}).save_episode() == "saved"
    assert len(dataset_cls.saved) == 1


def test_explicit_episode_data_is_never_swallowed(dataset_cls):
    """호출자가 데이터를 직접 주면 가드는 건드리지 않는다."""
    assert dataset_cls(None).save_episode({"size": 0}) == "saved"
    assert len(dataset_cls.saved) == 1
