"""번들 `detector` 플러그인 — 검출 데몬이 지키던 계약을 플러그인판도 지킨다 (feature/plugins.md §13).

예전 `test_detectord.py` 에서 옮겨 온 것: 카메라 인자 파싱, 발행자별 메타 버스 왕복, 자기소개 모양.
페이로드 계약은 SDK 로 가서 `test_plugin_sdk.py` 가 본다. torch 없이 돈다 — 모델은 가짜다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from piper_plugin.detections import model_meta

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("detector_main", REPO / "plugins" / "detector" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_cams_alias_and_bare():
    mod = _load()
    assert mod.parse_cams("top=rs_123_color, hand") == {"top": "rs_123_color", "hand": "hand"}
    assert mod.parse_cams("") == {}


def test_model_meta_reads_the_detector_shape():
    class M:
        task = "detect"
        names = {0: "a", 1: "b"}
        n_params = 12
        n_layers = 3

    meta = model_meta(M(), "PekingU/rtdetr_v2_r18vd", "cpu", 0.25, 5.0, 640, {"top": "seg"})
    assert meta["model"] == "rtdetr_v2_r18vd" and meta["classes"] == 2 and meta["params"] == 12
    assert meta["cams"] == {"top": "seg"} and "gflops" not in meta


def test_model_meta_survives_broken_model():
    class Broken:
        @property
        def names(self):
            raise RuntimeError("no")

    meta = model_meta(Broken(), "x", "cpu", 0.1, 1.0, 320)
    assert meta["model"] == "x" and meta["conf"] == 0.1


def test_bus_vision_meta_is_per_publisher():
    """발행자 둘이 서로 덮어쓰지 않는다 — 검출기 동시 여러 개의 전제(§13-3)."""
    from piper_bus import contract as C
    from piper_bus.client import Bus

    try:
        bus = Bus()
        bus.r.ping()
    except Exception:
        pytest.skip("redis 없음")
    try:
        bus.put_vision_meta("det_a", {"model": "m1"})
        bus.put_vision_meta("det_b", {"model": "m2"})
        assert bus.get_vision_meta("det_a")["model"] == "m1" and bus.get_vision_meta("det_a")["publisher"] == "det_a"
        assert bus.get_vision_meta("det_b")["model"] == "m2"
        assert {"det_a", "det_b"} <= set(bus.vision_meta_names())
        assert 0 < bus.r.pttl(C.vision_meta_key("det_a")) <= C.VISION_META_TTL_MS
        # 검출 이름 스캔에 메타가 섞이면 안 된다 (접두사 분리 이유)
        assert not any(n.startswith("vision_meta") or n in ("det_a", "det_b") for n in bus.detection_names())
    finally:
        bus.r.delete(C.vision_meta_key("det_a"), C.vision_meta_key("det_b"))


def test_bundled_manifest_is_valid_and_gpu():
    from app.services.plugins import read_manifest

    info = read_manifest(REPO / "plugins" / "detector")
    assert info.manifest is not None, info.error
    assert info.manifest.resources.gpu is True and info.manifest.ui == "/detector"
    assert {f.key for f in info.manifest.config} >= {"cams", "model", "conf", "fps", "imgsz", "device"}


def test_vision_start_maps_onto_the_plugin(monkeypatch):
    """`/api/vision/start` 호환층 — 검출 페이지가 보내던 인자가 플러그인 설정이 된다."""
    import asyncio

    from app.routers import vision

    calls = {}

    async def fake_start(name, config, cameras):
        calls["name"], calls["config"] = name, config
        return {"state": "running"}

    monkeypatch.setattr(vision._plugins, "start", fake_start)
    body = vision.StartRequest(cams={"top": "rs_1_color", "hand": "dev_video2"}, model="PekingU/rtdetr_v2_r18vd",
                               fps=5, conf=0.3, imgsz=640)
    asyncio.run(vision.start_detectord(body))
    assert calls["name"] == "detector"
    assert calls["config"]["cams"] == "top=rs_1_color, hand=dev_video2"
    assert calls["config"]["model"] == "PekingU/rtdetr_v2_r18vd" and calls["config"]["conf"] == 0.3
