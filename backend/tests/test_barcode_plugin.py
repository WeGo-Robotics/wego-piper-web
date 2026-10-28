"""번들 `barcode` 플러그인 — 읽은 코드가 검출 계약 모양으로 나간다.

페이로드 쪽은 디코더 없이 돈다. 실제 디코딩 왕복은 zxing-cpp 가 깔린 곳에서만 돈다(플러그인 venv 의 의존).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from piper_plugin import Env, PluginStop

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("barcode_main", REPO / "plugins" / "barcode" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundled_manifest_is_valid_and_needs_its_venv():
    from app.services.plugins import read_manifest

    info = read_manifest(REPO / "plugins" / "barcode")
    assert info.manifest is not None, info.error
    assert info.manifest.python == "venv" and (REPO / "plugins" / "barcode" / "requirements.txt").is_file()
    assert info.manifest.outputs.detections == ["barcode"] and info.manifest.outputs.previews == ["det_barcode"]


def test_codes_payload_is_the_detection_contract():
    mod = _load()
    codes = [
        {"text": "PIPER-0042", "format": "Code 128", "corners": [[10, 20], [110, 22], [108, 60], [9, 58]]},
        {"text": "https://x.y", "format": "QR Code", "corners": [[200, 10], [260, 10], [260, 70], [200, 70]]},
    ]
    p = mod.codes_payload("barcode", 7, 1_000_000_000, (480, 640), codes, det_seq=3, publisher="barcode")
    by = {o["label"]: o for o in p["objects"]}
    assert by["PIPER-0042"]["bbox"] == [9.0, 20.0, 110.0, 60.0] and by["PIPER-0042"]["format"] == "Code 128"
    assert by["https://x.y"]["format"] == "QR Code" and all(o["conf"] == 1.0 for o in p["objects"])
    assert p["size"] == [640, 480] and p["frame_seq"] == 7 and p["det_seq"] == 3 and p["model"] == "zxing-cpp"
    assert "PIPER-0042" in p["text"]


def test_empty_frame_still_publishes_an_empty_detection():
    """코드가 사라졌다는 것도 소식이다 — 소비자가 낡은 '보임' 을 붙잡지 않는다."""
    p = _load().codes_payload("barcode", 1, 0, (480, 640), [])
    assert p["objects"] == [] and "검출 없음" in p["text"]


def test_setup_stops_in_plain_words_without_the_decoder(tmp_path):
    mod = _load()
    mod.zxingcpp = None
    p = mod.Barcode(Env(name="barcode", dir=tmp_path, runtime_dir=tmp_path / "rt", cameras={"watch": "top"},
                        config={"formats": "", "fps": 5, "repeat_s": 3, "preview": False}))
    with pytest.raises(PluginStop):
        p.setup()
    assert "의존 설치" in p._stop_reason


def test_read_codes_round_trip_and_format_filter():
    zxingcpp = pytest.importorskip("zxingcpp")
    import numpy as np

    mod = _load()
    frame = np.full((300, 700), 255, np.uint8)
    for fmt, text, x in ((zxingcpp.BarcodeFormat.Code128, "PIPER-0042", 20), (zxingcpp.BarcodeFormat.QRCode, "LOT-7", 520)):
        img = np.array(zxingcpp.create_barcode(text, fmt).to_image(scale=3, add_quiet_zones=True))
        frame[40:40 + img.shape[0], x:x + img.shape[1]] = img
    assert {c["text"] for c in mod.read_codes(frame)} == {"PIPER-0042", "LOT-7"}
    only_qr = mod.read_codes(frame, zxingcpp.barcode_formats_from_str("QRCode"))
    assert [c["text"] for c in only_qr] == ["LOT-7"] and len(only_qr[0]["corners"]) == 4
