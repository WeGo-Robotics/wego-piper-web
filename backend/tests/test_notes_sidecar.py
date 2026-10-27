"""이름·설명 사이드카 — LeRobot 구조에 없는 자리를 옆에 만든다.

`meta/info.json`(데이터셋)·`config.json`(체크포인트)에는 사람이 붙이는
이름·설명 자리가 없고, 거기 임의 키를 끼우면 LeRobot 도구가 재작성할 때
보존된다는 보장이 없다. 그래서 별도 파일이다 — `meta/piper_cameras.json`
(카메라 매핑)과 같은, 이 저장소의 확립된 사이드카 관례.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import notes_sidecar as ns


def _dataset(root: Path) -> Path:
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text("{}")
    return root


def test_roundtrip_for_datasets_and_models(tmp_path):
    ds = _dataset(tmp_path / "ds")
    ds.parent.mkdir(exist_ok=True)
    out = ns.write_notes(ds, kind="dataset", name="볼트 1차", description="주간 조명")
    assert out["updated_at"]
    got = ns.read_notes(ds, kind="dataset")
    assert got["name"] == "볼트 1차" and got["description"] == "주간 조명"
    # 파일 자리 자체가 계약이다 — LeRobot 이 안 건드리는 meta/ 아래
    assert (ds / "meta" / "piper_notes.json").exists()

    model = tmp_path / "ckpt"
    model.mkdir()
    ns.write_notes(model, kind="model", name="", description="lr 낮춘 재학습")
    assert ns.read_notes(model, kind="model")["description"] == "lr 낮춘 재학습"


def test_missing_or_broken_sidecar_reads_as_empty(tmp_path):
    """목록 스캔이 데이터셋마다 부른다 — 없거나 깨졌다고 시끄러우면 안 된다."""
    assert ns.read_notes(tmp_path, kind="dataset") == ns.EMPTY
    _dataset(tmp_path)
    (tmp_path / "meta" / "piper_notes.json").write_text("{깨진 json")
    assert ns.read_notes(tmp_path, kind="dataset")["name"] == ""


def test_dataset_write_refuses_where_there_is_no_meta(tmp_path):
    """meta/ 없는 곳에 만들면 데이터셋이 아닌 디렉토리에 흔적이 남는다
    (camera_sidecar 와 같은 규칙)."""
    with pytest.raises(FileNotFoundError):
        ns.write_notes(tmp_path, kind="dataset", name="x", description="")


def test_readme_is_created_once_and_never_clobbered(tmp_path):
    """업로드가 폴더 전체를 올리므로 README.md 가 곧 허브 카드다.
    ⚠ 이미 있으면 안 덮는다 — 사람이 다듬은 카드를 업로드가 지우면 안 된다."""
    _dataset(tmp_path)
    notes = ns.write_notes(tmp_path, kind="dataset", name="볼트 1차", description="주간")
    assert ns.ensure_readme(tmp_path, "org/ds", notes) is True
    text = (tmp_path / "README.md").read_text()
    assert "# 볼트 1차" in text and "주간" in text

    (tmp_path / "README.md").write_text("사람이 고친 카드")
    assert ns.ensure_readme(tmp_path, "org/ds", notes) is False
    assert (tmp_path / "README.md").read_text() == "사람이 고친 카드"

    # 이름도 설명도 없으면 빈 카드를 만들지 않는다
    empty = tmp_path / "empty"
    _dataset(empty)
    assert ns.ensure_readme(empty, "org/empty", ns.EMPTY) is False


def test_the_scan_carries_notes_so_lists_can_show_them(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.dataset_scanner import scan_datasets

    root = tmp_path / "lerobot" / "org" / "ds1"
    root.mkdir(parents=True)
    (root / "meta").mkdir()
    (root / "meta" / "info.json").write_text(json.dumps({"total_episodes": 3}))
    ns.write_notes(root, kind="dataset", name="이름", description="설명")
    monkeypatch.setattr(settings, "lerobot_dir", tmp_path / "lerobot")
    monkeypatch.setattr(settings, "datasets_dir", tmp_path / "none")
    found = [d for d in scan_datasets() if d["id"] == "org/ds1"]
    assert found and found[0]["notes"]["name"] == "이름"


def test_notes_api_reads_and_writes(tmp_path, monkeypatch):
    from app.main import app
    from app.routers import datasets as ds_router

    root = _dataset(tmp_path)
    monkeypatch.setattr(ds_router, "find_dataset_path", lambda _id: root)
    c = TestClient(app)
    r = c.put("/api/datasets/org/ds/notes", json={"name": "n", "description": "d"})
    assert r.status_code == 200
    assert c.get("/api/datasets/org/ds/notes").json()["name"] == "n"


def test_recording_carries_the_description_to_the_sidecar():
    """녹화 폼의 설명이 정지 시 사이드카가 된다 — 카메라 사이드카와 같은 시점.
    빈 값이면 안 쓴다: 기존 설명을 빈 값으로 덮는 사고 방지."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "recording.py").read_text()
    assert "description: str" in src
    stop = src.split("write_camera_sidecar(", 1)[1]
    assert "write_notes" in stop and "if repo_id and (title or desc)" in stop


def test_upload_makes_the_card_before_pushing():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "datasets.py").read_text()
    body = src.split("async def upload_to_hub", 1)[1].split("async def", 1)[0]
    assert "ensure_readme" in body, "업로드가 카드를 안 만든다 — 허브에 설명이 안 간다"


def test_recording_carries_the_title_as_the_sidecar_name():
    """제목 = 사이드카의 name. 수집 폼의 제목이 정지 시 설명과 **같은 시점**에
    같은 파일로 간다 — 이름을 따로 다른 곳에 두면 둘이 어긋난다."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "recording.py").read_text()
    assert "title: str" in src
    stop = src.split("write_camera_sidecar(", 1)[1]
    assert "name=title" in stop
    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "RecordingPage.tsx").read_text()
    assert "데이터셋 제목" in page and "title, description," in page


def test_training_writes_the_weight_notes_next_to_its_output(tmp_path, monkeypatch):
    """학습 폼의 제목·설명은 run 루트의 piper_notes.json 이 된다. CLI 인자 빌더에는
    흘리지 않는다.

    ⚠ **시작 직후에 쓰면 안 된다.** 폴더를 미리 만들면 LeRobot 이 validate 에서
    `FileExistsError` 로 죽는다(resume 이 아니면) — Output Dir 을 적은 학습이 제목을 넣는
    순간 몇 초 뒤 죽었다. 그리고 Output Dir 을 비우면(자동, 대부분이 이렇게 돈다) 경로를
    몰라 아예 안 남았다 — 목록에 이름이 하나도 안 뜬 이유다. 둘 다 LeRobot 이 자리를
    정했다고 찍은 뒤에 쓰는 것으로 푼다(`TrainManager._output_dir_reported`)."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "training.py").read_text()
    assert 'params.pop("title"' in src and 'params.pop("description"' in src, \
        "제목·설명이 인자 dict 에 남는다"
    start = src.split("async def start_training(", 1)[1].split("@router.post", 1)[0]
    assert "on_output_dir=leave_in_run" in start, "LeRobot 이 자리를 정하기 전에 쓴다"
    leave = start.split("def leave_in_run(", 1)[1].split("try:", 1)[0]
    assert "write_notes(" in leave and "inherit_to_run(" in leave
    assert "body.output_dir" not in leave, "자동 경로면 또 못 쓴다"
    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "TrainingPage.tsx").read_text()
    assert "가중치 제목" in page and "가중치 설명" in page
    assert "Output Dir 이 비어 있으면" not in page, "이제는 남는데 폼이 못 남긴다고 말한다"


class _Runner:
    """로그 한 줄을 흘려 넣을 수 있는 가짜 러너."""
    state = None
    is_running = True
    pid = None
    is_remote = False

    def __init__(self, workdir):
        self.workdir = workdir

    def set_log_callback(self, cb): self.feed = cb
    def set_state_callback(self, cb): pass
    async def start(self, spec): pass
    async def stop(self): pass
    def restore(self): return None


def _manager(runner):
    from app.services.training.jobs import JobRegistry
    from app.services.training.manager import TrainManager

    class DeadBus:          # 버스 없이도 돈다 — 레지스트리는 부가 기능이다
        def __getattr__(self, _name):
            def boom(*a, **k):
                raise ConnectionError("redis down")
            return boom

    return TrainManager(runner=runner, registry=JobRegistry(DeadBus()))


_OUT_LINE = "INFO 2026-10-26 18:20:11 ot_train.py:329 Output dir: outputs/train/2026-10-26/18-20-01_smolvla"


def test_run_files_wait_for_lerobot_to_pick_the_folder(tmp_path):
    import asyncio

    runner = _Runner(tmp_path)
    tm = _manager(runner)
    got: list[Path] = []
    asyncio.run(tm.start(["lerobot-train"], total_steps=10, output_dir="", on_output_dir=got.append))
    runner.feed("INFO cfg.validate() 전후의 다른 줄")
    assert got == [] and not (tmp_path / "outputs").exists(), "LeRobot 이 validate 하기 전에 폴더를 만들었다"

    # 터미널이면 색 코드가 붙는다
    runner.feed(_OUT_LINE.replace("Output dir:", "\x1b[1m\x1b[33mOutput dir:\x1b[0m"))
    run_dir = tmp_path / "outputs" / "train" / "2026-10-26" / "18-20-01_smolvla"
    assert got == [run_dir], "상대경로는 러너의 작업 폴더 기준이다"
    assert tm.output_dir == str(run_dir), "자동 경로를 알게 됐는데 상태·곡선 저장이 모른다"

    runner.feed(_OUT_LINE)
    assert got == [run_dir], "한 번만 남긴다 — 재부착이 저널을 다시 읽어도"


def test_an_explicit_output_dir_is_kept_and_remote_runs_are_left_alone(tmp_path):
    import asyncio

    runner = _Runner(tmp_path)
    tm = _manager(runner)
    got: list[Path] = []
    asyncio.run(tm.start(["x"], output_dir="/data/run1", on_output_dir=got.append))
    runner.feed(_OUT_LINE.replace("outputs/train/2026-10-26/18-20-01_smolvla", "/data/run1"))
    assert got == [Path("/data/run1")] and tm.output_dir == "/data/run1"

    # 원격이면 그 경로는 이 기계의 것이 아니다
    remote = _Runner(tmp_path)
    remote.is_remote = True
    tm = _manager(remote)
    got = []
    asyncio.run(tm.start(["x"], output_dir="", on_output_dir=got.append))
    remote.feed(_OUT_LINE)
    assert got == [] and tm.output_dir == ""


def test_lists_carry_a_created_date_not_just_the_mtime(tmp_path, monkeypatch):
    """"만든 날짜"는 디렉토리 mtime 이 아니다 — 에피소드를 추가할 때마다 mtime 은
    앞으로 밀린다. 스캔이 `created`(생성시각 → 표식 파일 → mtime 순)를 따로 실어야
    목록이 "언제 만들었나"를 말할 수 있다."""
    from app.core.config import settings
    from app.services.dataset_scanner import scan_datasets

    root = tmp_path / "lerobot" / "org" / "ds2"
    root.mkdir(parents=True)
    (root / "meta").mkdir()
    (root / "meta" / "info.json").write_text(json.dumps({"total_episodes": 1}))
    monkeypatch.setattr(settings, "lerobot_dir", tmp_path / "lerobot")
    monkeypatch.setattr(settings, "datasets_dir", tmp_path / "none")
    found = [d for d in scan_datasets() if d["id"] == "org/ds2"]
    assert found and found[0]["created"] and found[0]["modified"]
    # 이르거나 같아야 한다 — 만든 날짜가 수정 날짜보다 뒤일 수는 없다
    assert found[0]["created"] <= found[0]["modified"]


def test_checkpoints_inherit_the_runs_title_from_the_training_form(tmp_path):
    """⚠ 학습 폼의 제목·설명은 **run 루트**(output_dir/piper_notes.json)에 남는데
    스캐너는 체크포인트 폴더만 읽었다 — 그대로면 학습 때 적은 제목이 목록에
    영영 안 뜬다. run 의 사이드카를 읽어 체크포인트에 물려주고(`run_notes`),
    체크포인트 자기 사이드카가 있으면(모델 페이지 편집기) 그쪽이 이긴다."""
    from app.services.model_scanner import _scan_train_outputs

    run = tmp_path / "2026-09-09" / "pick_v3"
    for step in ("000100", "000200"):
        pm = run / "checkpoints" / step / "pretrained_model"
        pm.mkdir(parents=True)
        (pm / "config.json").write_text(json.dumps({"type": "act"}))
    ns.write_notes(run, kind="model", name="집기 v3", description="주간 데이터")
    # 두 번째 체크포인트는 자기 이름을 따로 가진다
    ns.write_notes(run / "checkpoints" / "000200" / "pretrained_model", kind="model",
                   name="집기 v3 — 최종", description="")
    items = {m["checkpoint"]: m for m in _scan_train_outputs(tmp_path)}
    assert items["000100"]["run_notes"]["name"] == "집기 v3"
    assert items["000100"]["notes"]["name"] == "집기 v3", "run 제목을 물려받지 못했다"
    assert items["000200"]["notes"]["name"] == "집기 v3 — 최종", "체크포인트 자기 사이드카가 져야 할 이유가 없다"
    assert items["000100"]["created"] and items["000100"]["run_created"]
    # 저장소는 학습 1행 표다(test_weight_table) — 이름 규칙은 백엔드 scan_runs 가 정한다
    label = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "CheckpointLabel.tsx").read_text()
    assert "runName(models.filter((x) => x.run === m.run))" in label, \
        "추론·학습이 고른 체크포인트를 이름 없이 경로(날짜)로만 보인다"
    types = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "models.ts").read_text()
    assert "run_notes?.name" in types and "notes?.name" in types, \
        "학습 폼 제목도, 모델 페이지에서 붙인 이름도 run 이름이 돼야 한다"
    # 데이터셋 목록은 표 | 썸네일 (DatasetBrowser — 저장소와 에피소드 페이지가 같이 쓴다)
    ds_list = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "DatasetBrowser.tsx").read_text()
    assert "만든 날짜" in ds_list
