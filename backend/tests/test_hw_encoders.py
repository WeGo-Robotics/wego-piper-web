"""`vcodec=auto` 는 열리는 하드웨어 인코더만 고른다 (2026-10-10).

PyAV wheel 은 NVENC 를 품고 있어 `av.codec.Codec("h264_nvenc", "w")` 가 GPU 없이도 성공한다.
LeRobot 의 `auto` 는 그걸 "쓸 수 있다"로 읽어, GPU 없는 노트북에서 `auto → h264_nvenc` 가 되고
첫 프레임에서 `avcodec_open2(h264_nvenc)` 로 녹화가 죽었다.

여기서 지키는 것: 코덱이 목록에 있어도 **열리지 않으면 빠진다** · LeRobot 의 후보 **순서**는
유지된다 · 목록에 없는 후보는 열어 보지도 않는다 · 판정은 **한 번만** 한다(`resolve_vcodec`
이 여러 번 불린다) · 녹화 래퍼가 실제로 이걸 설치한다.
"""

import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "wrapper"))


def test_a_codec_that_is_listed_but_does_not_open_is_dropped():
    import hw_encoders

    order = ["h264_videotoolbox", "h264_nvenc", "hevc_nvenc", "h264_vaapi"]
    listed = {"h264_nvenc", "hevc_nvenc"}
    assert hw_encoders.usable(order, listed, lambda n: False) == []


def test_the_first_working_candidate_keeps_lerobots_order():
    import hw_encoders

    order = ["h264_nvenc", "hevc_nvenc", "h264_qsv"]
    listed = set(order)
    assert hw_encoders.usable(order, listed, lambda n: n != "h264_nvenc") == ["hevc_nvenc", "h264_qsv"]


def test_a_candidate_that_is_not_listed_is_never_opened():
    import hw_encoders

    tried = []
    hw_encoders.usable(["h264_qsv", "h264_nvenc"], {"h264_nvenc"}, lambda n: tried.append(n) or True)
    assert tried == ["h264_nvenc"], "빌드에 없는 코덱을 열어 보려 했다"


@pytest.fixture
def fake_lerobot(monkeypatch):
    """`lerobot.datasets.video_utils` 의 자리만 흉내 낸다 — 진짜 LeRobot 이 없어도 돈다."""
    vu = types.ModuleType("lerobot.datasets.video_utils")
    vu.HW_ENCODERS = ["h264_videotoolbox", "h264_nvenc", "hevc_nvenc"]
    vu.detect_available_hw_encoders = lambda: ["h264_nvenc", "hevc_nvenc"]   # 빌드에 든 것
    vu._get_codec_options = lambda name: {"crf": "30"}

    def resolve_vcodec(vcodec):
        if vcodec != "auto":
            return vcodec
        for enc in vu.HW_ENCODERS:
            if enc in vu.detect_available_hw_encoders():
                return enc
        return "libsvtav1"

    vu.resolve_vcodec = resolve_vcodec
    datasets = types.ModuleType("lerobot.datasets")
    datasets.video_utils = vu
    lerobot = types.ModuleType("lerobot")
    lerobot.datasets = datasets
    for name, mod in (("lerobot", lerobot), ("lerobot.datasets", datasets),
                      ("lerobot.datasets.video_utils", vu)):
        monkeypatch.setitem(sys.modules, name, mod)
    return vu


def test_auto_falls_back_to_software_when_no_hardware_opens(fake_lerobot, monkeypatch):
    import hw_encoders

    monkeypatch.setattr(hw_encoders, "_frame_open_ok", lambda name, options: False)
    hw_encoders.install()
    assert fake_lerobot.resolve_vcodec("auto") == "libsvtav1"


def test_auto_still_picks_hardware_where_it_really_opens(fake_lerobot, monkeypatch):
    import hw_encoders

    monkeypatch.setattr(hw_encoders, "_frame_open_ok", lambda name, options: True)
    hw_encoders.install()
    assert fake_lerobot.resolve_vcodec("auto") == "h264_nvenc"


def test_an_explicit_codec_is_never_second_guessed(fake_lerobot, monkeypatch):
    import hw_encoders

    monkeypatch.setattr(hw_encoders, "_frame_open_ok", lambda name, options: False)
    hw_encoders.install()
    assert fake_lerobot.resolve_vcodec("h264_nvenc") == "h264_nvenc"


def test_the_probe_runs_once_however_often_auto_is_resolved(fake_lerobot, monkeypatch):
    import hw_encoders

    calls = []
    monkeypatch.setattr(hw_encoders, "_frame_open_ok", lambda name, options: calls.append(name) or False)
    hw_encoders.install()
    for _ in range(3):
        fake_lerobot.resolve_vcodec("auto")
    assert sorted(calls) == ["h264_nvenc", "hevc_nvenc"], f"같은 후보를 여러 번 열었다: {calls}"


def test_the_record_wrapper_installs_the_probe_and_survives_its_failure():
    src = (REPO / "wrapper" / "start_record.py").read_text()
    # 판정이 깨져도 녹화를 막지 않는다 — install 이 try/except 안에 있어야 한다
    assert "try:\n    import hw_encoders\n    hw_encoders.install()\nexcept Exception" in src, (
        "녹화 래퍼가 판정을 설치하지 않거나, 실패했을 때 녹화까지 죽는다")
