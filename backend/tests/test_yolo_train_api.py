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


def test_delete_image_removes_label_too(root):
    from app.services import yolo_dataset as yd

    yd.create_dataset("d", ["a"])
    fname = yd.add_image("d", JPEG, {"type": "live"})
    yd.write_label("d", fname, [{"cls": 0, "cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.1}])
    yd.delete_image("d", fname)
    assert not (root / "d" / "labels" / fname).with_suffix(".txt").exists()
    assert json.loads((root / "d" / "classes.json").read_text()) == ["a"]
