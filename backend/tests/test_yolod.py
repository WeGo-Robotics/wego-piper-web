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
