"""YOLO 학습 데이터셋 API — 캡처·가져오기·갤러리 계약 (feature/yolo-training.md).

파일시스템이 정본이므로 테스트도 tmp_path 파일시스템으로 검증한다.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

# 1x1 JPEG (최소 유효 파일은 아니지만 매직 검사·왕복에 충분)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture()
def root(monkeypatch, tmp_path):
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "yolo_datasets_dir", tmp_path)
    return tmp_path


def test_dataset_crud_roundtrip(client, root):
    # 생성 → 목록 → 삭제
    r = client.post("/api/yolo/datasets", json={"name": "recycle", "classes": ["pet", "can"]})
    assert r.status_code == 200
    assert r.json() == {"name": "recycle", "classes": ["pet", "can"], "images": 0, "labeled": 0}

    assert [d["name"] for d in client.get("/api/yolo/datasets").json()["datasets"]] == ["recycle"]

    # 중복 생성 거부
    assert client.post("/api/yolo/datasets", json={"name": "recycle", "classes": ["x"]}).status_code == 400

    assert client.delete("/api/yolo/datasets/recycle").status_code == 200
    assert client.get("/api/yolo/datasets").json()["datasets"] == []


def test_dataset_name_and_classes_validation(client, root):
    assert client.post("/api/yolo/datasets", json={"name": "../evil", "classes": ["x"]}).status_code == 400
    assert client.post("/api/yolo/datasets", json={"name": "한글이름", "classes": ["x"]}).status_code == 400
    assert client.post("/api/yolo/datasets", json={"name": "ok", "classes": []}).status_code == 400
    assert client.post("/api/yolo/datasets", json={"name": "ok", "classes": ["a", "a"]}).status_code == 400


def test_classes_append_only(client, root):
    client.post("/api/yolo/datasets", json={"name": "d", "classes": ["a"]})
    r = client.post("/api/yolo/datasets/d/classes", json={"classes": ["b", "a", "c"]})
    assert r.json()["classes"] == ["a", "b", "c"]  # 기존 순서 유지 + 추가만


def test_image_upload_gallery_delete(client, root):
    client.post("/api/yolo/datasets", json={"name": "d", "classes": ["a"]})

    # 업로드 (출처 메타는 쿼리)
    r = client.post("/api/yolo/datasets/d/images?type=episode&dataset=lr1&episode=3&cam=top&t=1.5",
                    content=JPEG)
    assert r.status_code == 200
    fname = r.json()["file"]

    # JPEG 아니면 거부
    assert client.post("/api/yolo/datasets/d/images", content=b"PNG...").status_code == 400

    # 갤러리: 출처가 붙어 나온다
    listing = client.get("/api/yolo/datasets/d/images").json()
    assert listing["images"][0]["file"] == fname
    assert listing["images"][0]["labeled"] is False
    src = listing["images"][0]["source"]
    assert (src["type"], src["dataset"], src["episode"], src["t"]) == ("episode", "lr1", 3, 1.5)

    # 이미지 서빙
    assert client.get(f"/api/yolo/datasets/d/images/{fname}").content == JPEG
    # 경로 문자 파일명 거부
    assert client.get("/api/yolo/datasets/d/images/..%2Fclasses.json").status_code in (400, 404)

    assert client.delete(f"/api/yolo/datasets/d/images/{fname}").status_code == 200
    assert client.get("/api/yolo/datasets/d/images").json()["images"] == []


def test_import_episode_copies_cache_frames(client, root, monkeypatch, tmp_path):
    """디코딩 캐시 → stride 복사. 출처에 (dataset, episode, cam, frame) 이 남는다."""
    # 가짜 LeRobot 디코딩 캐시
    lr = tmp_path / "lerobot" / "myds"
    ep = lr / "images" / "observation.images.top" / "episode-000002"
    ep.mkdir(parents=True)
    for i in range(7):
        (ep / f"frame-{i:06d}.jpg").write_bytes(JPEG + bytes([i]))

    from app.services import dataset_scanner
    monkeypatch.setattr(dataset_scanner, "find_dataset_path", lambda _id: lr)

    client.post("/api/yolo/datasets", json={"name": "d", "classes": ["a"]})
    r = client.post("/api/yolo/datasets/d/import-episode", json={
        "dataset_id": "myds", "episode": 2, "cam": "top", "stride": 3,
    })
    assert r.status_code == 200
    assert r.json()["added"] == 3          # 프레임 0, 3, 6
    assert r.json()["total_frames"] == 7

    images = client.get("/api/yolo/datasets/d/images").json()["images"]
    frames = sorted(img["source"]["frame"] for img in images)
    assert frames == [0, 3, 6]
    assert images[0]["source"]["type"] == "episode"

    # indices 지정은 stride 를 무시한다
    r = client.post("/api/yolo/datasets/d/import-episode", json={
        "dataset_id": "myds", "episode": 2, "cam": "top", "indices": [5],
    })
    assert r.json()["added"] == 1

    # 캐시 없는 에피소드는 404 + 안내
    r = client.post("/api/yolo/datasets/d/import-episode", json={
        "dataset_id": "myds", "episode": 99, "cam": "top",
    })
    assert r.status_code == 404
    assert "decode-cache" in r.json()["detail"]


def test_label_roundtrip_via_service(root):
    """라벨 txt ↔ 박스 JSON 왕복 (2단계 라벨러의 기반 — 서비스 계층 계약)."""
    from app.services import yolo_dataset as yd

    yd.create_dataset("d", ["a", "b"])
    fname = yd.add_image("d", JPEG, {"type": "live", "cam": "c"})

    assert yd.read_label("d", fname) is None            # 미라벨
    yd.write_label("d", fname, [
        {"cls": 1, "cx": 0.5, "cy": 0.5, "w": 0.25, "h": 0.125},
    ])
    assert yd.read_label("d", fname) == [
        {"cls": 1, "cx": 0.5, "cy": 0.5, "w": 0.25, "h": 0.125},
    ]
    txt = (root / "d" / "labels" / fname).with_suffix(".txt").read_text()
    assert txt == "1 0.500000 0.500000 0.250000 0.125000\n"

    yd.write_label("d", fname, [])                      # 박스 0개 = 배경 샘플
    assert yd.read_label("d", fname) == []
    assert yd.summarize("d")["labeled"] == 1

    yd.clear_label("d", fname)                          # 미라벨로 되돌림
    assert yd.read_label("d", fname) is None

    # 검증: 범위 밖 클래스·좌표
    from app.services.yolo_dataset import YoloDatasetError
    with pytest.raises(YoloDatasetError):
        yd.write_label("d", fname, [{"cls": 9, "cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.1}])
    with pytest.raises(YoloDatasetError):
        yd.write_label("d", fname, [{"cls": 0, "cx": 1.5, "cy": 0.5, "w": 0.1, "h": 0.1}])


def test_label_http_roundtrip(client, root):
    """라벨 API — 화면(라벨러)이 쓰는 계약: null/[]/박스, 클래스 동봉."""
    client.post("/api/yolo/datasets", json={"name": "d", "classes": ["a", "b"]})
    fname = client.post("/api/yolo/datasets/d/images", content=JPEG).json()["file"]

    r = client.get(f"/api/yolo/datasets/d/labels/{fname}").json()
    assert r == {"boxes": None, "classes": ["a", "b"]}          # 미라벨

    boxes = [{"cls": 1, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.1}]
    assert client.put(f"/api/yolo/datasets/d/labels/{fname}", json={"boxes": boxes}).json()["boxes"] == boxes

    # 범위 밖은 400
    bad = [{"cls": 7, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.1}]
    assert client.put(f"/api/yolo/datasets/d/labels/{fname}", json={"boxes": bad}).status_code == 400

    assert client.delete(f"/api/yolo/datasets/d/labels/{fname}").status_code == 200
    assert client.get(f"/api/yolo/datasets/d/labels/{fname}").json()["boxes"] is None


def test_prelabel_txt_lines_name_matching():
    """사전 라벨 매핑 — 이름 완전 일치만, 좌표 클램프, 불일치 카운트."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "daemons" / "yolo_prelabel.py"
    spec = importlib.util.spec_from_file_location("yolo_prelabel", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    lines, dropped = mod.txt_lines(
        classes=["pet_bottle", "can"],
        names={0: "can", 1: "person", 2: "pet_bottle"},
        clss=[0, 1, 2],
        xywhn=[[0.5, 0.5, 0.2, 0.1], [0.1, 0.1, 0.1, 0.1], [0.9, 0.9, 0.3, 1.0001]],
    )
    assert dropped == 1                                   # person 은 데이터셋에 없다
    assert lines[0] == "1 0.500000 0.500000 0.200000 0.100000"   # can → id 1
    assert lines[1].startswith("0 0.900000 0.900000 0.300000 1.000000")  # 클램프


def _load_traind():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "daemons" / "yolo_traind.py"
    spec = importlib.util.spec_from_file_location("yolo_traind", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_split_keeps_groups_together():
    """같은 에피소드의 프레임이 train/val 로 갈라지면 mAP 가 거짓말을 한다."""
    mod = _load_traind()
    files = [f"f{i}.jpg" for i in range(30)]
    groups = {f: f"ep:ds:{i // 10}" for i, f in enumerate(files)}   # 3 그룹 × 10장

    train, val = mod.split_by_group(files, groups)
    assert sorted(train + val) == sorted(files)
    assert train and val
    # 그룹 무결성: 어느 그룹도 양쪽에 걸치지 않는다
    train_groups = {groups[f] for f in train}
    val_groups = {groups[f] for f in val}
    assert not (train_groups & val_groups)

    # 같은 시드 = 같은 분할 (재현성)
    assert mod.split_by_group(files, groups) == (train, val)


def test_split_single_group_falls_back_to_files():
    """그룹이 하나뿐이면 파일 단위로라도 나눈다 — val 없이는 학습이 안 돈다."""
    mod = _load_traind()
    files = [f"f{i}.jpg" for i in range(10)]
    groups = {f: "ep:ds:0" for f in files}
    train, val = mod.split_by_group(files, groups)
    assert len(val) >= 1 and len(train) >= 1
    assert sorted(train + val) == sorted(files)


def test_group_key_shapes():
    mod = _load_traind()
    assert mod.group_key({"type": "episode", "dataset": "d", "episode": 3}) == "ep:d:3"
    assert mod.group_key({"type": "live", "cam": "c", "at": 0}).startswith("live:c:")
    assert mod.group_key(None) == "unknown"


def test_train_requires_labeled_images(client, root):
    client.post("/api/yolo/datasets", json={"name": "d", "classes": ["a"]})
    r = client.post("/api/yolo/train", json={"dataset": "d"})
    assert r.status_code == 400
    assert "라벨된 이미지" in r.json()["detail"]


def test_train_status_shape(client, root):
    r = client.get("/api/yolo/train/status").json()
    assert {"state", "pid", "info", "progress"} <= r.keys()


def test_read_progress_parses_results_csv(root, monkeypatch):
    from app.services.yolo_train_manager import read_progress

    run = root / "d" / "runs" / "t1"
    run.mkdir(parents=True)
    (run / "results.csv").write_text(
        "epoch, train/box_loss, metrics/mAP50(B), metrics/mAP50-95(B)\n"
        "1, 1.5, 0.30, 0.20\n"
        "2, 1.2, 0.45, 0.31\n"
    )
    rows = read_progress("d", "t1")
    assert rows == [
        {"epoch": 1, "box_loss": 1.5, "map50": 0.3, "map50_95": 0.2},
        {"epoch": 2, "box_loss": 1.2, "map50": 0.45, "map50_95": 0.31},
    ]


def test_delete_image_removes_label_too(root):
    from app.services import yolo_dataset as yd

    yd.create_dataset("d", ["a"])
    fname = yd.add_image("d", JPEG, {"type": "live"})
    yd.write_label("d", fname, [{"cls": 0, "cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.1}])
    yd.delete_image("d", fname)
    assert not (root / "d" / "labels" / fname).with_suffix(".txt").exists()
    assert json.loads((root / "d" / "classes.json").read_text()) == ["a"]
