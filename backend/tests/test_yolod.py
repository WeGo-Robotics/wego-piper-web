"""yolod — 페이로드 계약 + 버스 왕복 (torch 없이).

무거운 import(ultralytics·cv2)는 yolod.main() 안에만 있다 — 여기서는
순수 함수와 버스 계약만 검증한다. 판단 스텝(오케스트레이터)이 소비하는
형태가 곧 LLM 프롬프트 재료라, 이 계약이 곧 인식↔판단 경계다.
"""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def yolod():
    script = Path(__file__).resolve().parents[2] / "daemons" / "yolod.py"
    spec = importlib.util.spec_from_file_location("yolod", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_payload_shape_and_rounding(yolod):
    p = yolod._payload(
        "top", 42, 1_700_000_000_123_456_789, (480, 848),
        names={0: "person", 39: "bottle"},
        xyxy=[[10.123, 20.456, 110.789, 220.001]],
        confs=[0.91234],
        clss=[39],
    )
    assert p["cam"] == "top"
    assert p["frame_seq"] == 42
    assert p["size"] == [848, 480]          # (w, h) — bbox 좌표계와 같은 순서
    assert abs(p["ts"] - 1_700_000_000.123) < 0.01
    (obj,) = p["objects"]
    assert obj["label"] == "bottle"
    assert obj["conf"] == 0.912
    assert obj["bbox"] == [10.1, 20.5, 110.8, 220.0]
    assert obj["center"] == [60.5, 120.2]   # (x1+x2)/2, (y1+y2)/2 반올림


def test_payload_carries_inference_speed(yolod):
    """ultralytics speed(단계별 ms) → 화면 표시용 필드. text(LLM 프롬프트)에는 안 섞인다."""
    p = yolod._payload(
        "top", 1, 0, (480, 848), names={0: "x"},
        xyxy=[[0, 0, 1, 1]], confs=[0.5], clss=[0],
        speed={"preprocess": 1.234, "inference": 12.345, "postprocess": 0.567},
    )
    assert p["infer_ms"] == 12.3
    assert p["speed_ms"] == {"preprocess": 1.2, "inference": 12.3, "postprocess": 0.6}
    assert "12.3" not in p["text"]

    # speed 없이 부르면(구버전 호출·테스트) 필드도 없다
    p2 = yolod._payload("top", 1, 0, (2, 2), names={}, xyxy=[], confs=[], clss=[])
    assert "infer_ms" not in p2
    assert "det_seq" not in p2


def test_payload_det_seq_is_detection_counter(yolod):
    """fps 는 det_seq(검출 횟수)로 잰다 — frame_seq 는 카메라 발행 카운터라 다른 값이다."""
    p = yolod._payload(
        "top", 300, 0, (2, 2), names={}, xyxy=[], confs=[], clss=[], det_seq=50,
    )
    assert p["det_seq"] == 50
    assert p["frame_seq"] == 300  # 서로 독립 — 30fps 카메라 / 5fps 검출이면 6배 차이


def test_payload_sorts_largest_first(yolod):
    """LLM 프롬프트에서 잘려도 주된 물체가 남아야 한다."""
    p = yolod._payload(
        "top", 1, 0, (480, 848),
        names={0: "small", 1: "big"},
        xyxy=[[0, 0, 10, 10], [0, 0, 100, 100]],
        confs=[0.9, 0.5],
        clss=[0, 1],
    )
    assert [o["label"] for o in p["objects"]] == ["big", "small"]


def test_unknown_class_id_falls_back_to_number(yolod):
    p = yolod._payload("c", 1, 0, (10, 10), names={}, xyxy=[[0, 0, 1, 1]],
                       confs=[0.5], clss=[77])
    assert p["objects"][0]["label"] == "77"


def test_detections_text_is_prompt_ready(yolod):
    p = yolod._payload(
        "top", 1, 0, (480, 848),
        names={39: "bottle"},
        xyxy=[[100, 100, 200, 300]],
        confs=[0.87],
        clss=[39],
    )
    text = yolod.detections_text(p)
    assert text == "[top 848x480] bottle(0.87) center=(150,200)"

    empty = yolod._payload("hand", 1, 0, (480, 848), names={}, xyxy=[], confs=[], clss=[])
    assert yolod.detections_text(empty) == "[hand] 검출 없음"


def test_parse_cams_alias_and_bare(yolod):
    assert yolod._parse_cams(["top=rs_123_color", "hand"]) == {
        "top": "rs_123_color",
        "hand": "hand",
    }


def test_model_meta_reads_the_detector_shape(yolod):
    """자기소개 계약 — 화면(검출 데모)이 그리는 필드들. torch 없이 가짜 모델로.

    ⚠ 예전에는 ultralytics 의 `model.info()` 튜플에서 읽고 **GFLOPs** 도 실었다.
    RT-DETR 로 옮기면서 모델이 값을 직접 준다. GFLOPs 는 **뺐다** — 우리가 못
    재는 값이라 0 을 넣으면 화면이 "0 GFLOPs" 라고 조용히 거짓말한다.
    """
    class FakeModel:
        task = "detect"
        names = {0: "person", 39: "bottle"}
        n_params = 2_616_248
        n_layers = 100

    meta = yolod._model_meta(FakeModel(), "PekingU/rtdetr_v2_r18vd", "cuda:0",
                             0.25, 5.0, 640, {"top": "rs_1_color"})
    assert meta == {
        "model": "rtdetr_v2_r18vd", "device": "cuda:0", "conf": 0.25, "fps": 5.0,
        "imgsz": 640, "cams": {"top": "rs_1_color"}, "task": "detect", "classes": 2,
        "layers": 100, "params": 2_616_248,
    }
    assert "gflops" not in meta, "못 재는 값을 적고 있다"

    # 커스텀 가중치는 절대경로로 들어온다 — 화면에는 마지막 조각만
    short = yolod._model_meta(FakeModel(), "/a/b/best-0831", "cpu", 0.5, 1.0, 320)
    assert short["model"] == "best-0831"
    assert short["cams"] == {}


def test_model_meta_survives_broken_model(yolod):
    """모델에서 못 읽어도 기본 필드는 남는다 — 표시용 정보가 데몬을 죽이면 안 된다."""
    class BrokenModel:
        @property
        def names(self):
            raise RuntimeError("no")

    meta = yolod._model_meta(BrokenModel(), "m", "cpu", 0.5, 1.0, 640)
    assert meta["model"] == "m"
    assert "params" not in meta


def test_bus_yolo_meta_roundtrip(yolod):
    """메타 버스 계약: 최신값 덮어쓰기 + TTL (검출 키와 같은 stale 안전망)."""
    from piper_bus import contract as C
    from piper_bus.client import Bus

    try:
        bus = Bus()
        bus.r.ping()
    except Exception:
        pytest.skip("redis 없음")

    meta = {"model": "yolo11n.pt", "device": "cpu", "conf": 0.25, "fps": 5.0}
    try:
        bus.put_yolo_meta(meta)
        assert bus.get_yolo_meta() == meta
        assert 0 < bus.r.pttl(C.YOLO_META) <= C.YOLO_META_TTL_MS
        # 검출 이름 스캔에 "meta" 가 섞이면 안 된다 (접두사 분리 이유)
        assert "meta" not in bus.detection_names()
    finally:
        bus.r.delete(C.YOLO_META)


def test_bus_detections_roundtrip(yolod):
    """버스 계약: 최신값 덮어쓰기 + TTL 키 + 이름 나열 (preview 와 같은 패턴)."""
    from piper_bus import contract as C
    from piper_bus.client import Bus

    try:
        bus = Bus()
        bus.r.ping()
    except Exception:
        pytest.skip("redis 없음")

    name = "test_yolod_selftest"
    payload = yolod._payload("t", 7, 123, (2, 2), names={0: "x"},
                             xyxy=[[0, 0, 1, 1]], confs=[0.5], clss=[0])
    try:
        bus.put_detections(name, payload)
        assert bus.get_detections(name) == payload
        assert name in bus.detection_names()
        # TTL 이 걸려 있어야 한다 — yolod 가 죽으면 낡은 검출이 사라져야 하므로
        assert 0 < bus.r.pttl(C.vision_key(name)) <= C.VISION_TTL_MS
    finally:
        bus.r.delete(C.vision_key(name))
