"""검사 결과 보관 — 저장·조회·비교.

⚠ **팔은 시리얼을 안 준다.** Piper 프로토콜에 그런 필드가 없다 — SDK 를 다 뒤졌다.
자동으로 남길 수 있는 것은 **CAN 어댑터의 USB 시리얼**뿐이고, 그건 "어느 케이블에
물려 있었나" 다. 어느 팔인지는 사람이 적어야 하고, 화면이 그 사실을 말해야 한다.
"""

import pytest
from conftest import code_only
from pathlib import Path

from app.services import diag_store

PANEL = (Path(__file__).resolve().parents[2] / "frontend" / "src"
         / "components" / "DiagnosticsPanel.tsx")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(diag_store, "ROOT", tmp_path / "diagnostics")


def _run(err=1.0, cur=2.0, amp=20.0, intensity="normal") -> dict:
    return {
        "iface": "can3", "firmware": "S-V1.9-0", "adapter_serial": "0032004B",
        "rows": [{"t_s": 0.0}] * 5,
        "plan": {"intensity": intensity, "duration_s": 9.2,
                 "joints": [{"joint": "joint4", "amplitude_deg": amp, "direction": 1}]},
        "summary": {"joints": {"joint4": {
            "err_max_deg": err, "err_rms_deg": err / 2, "current_max_a": cur,
            "current_mean_a": cur / 2, "effort_max_nm": cur, "temp_rise_c": 0}},
            "outliers": {}},
    }


def test_a_saved_run_keeps_its_rows_but_the_list_does_not():
    """⚠ **행까지 저장한다** — 요약만 남기면 나중에 파형을 못 본다. 그런데 목록에
    다 실으면 한 회차가 1300행 × 120열이라 화면이 못 연다."""
    diag_store.save("A", _run())
    rec = diag_store.get("A")
    assert isinstance(rec["rows"], list) and len(rec["rows"]) == 5

    row = diag_store.list_saved()[0]
    assert row["rows"] == 5, "목록이 행 수를 안 준다"
    assert not isinstance(row["rows"], list), "목록에 행을 통째로 실었다"


def test_it_records_what_it_can_and_says_what_it_cannot():
    """어댑터 시리얼은 자동으로 남지만 **팔의 시리얼이 아니다.**"""
    diag_store.save("A", _run())
    assert diag_store.get("A")["adapter_serial"] == "0032004B"

    ui = PANEL.read_text()
    assert "팔은 시리얼을 안 줍니다" in ui, "사람이 적어야 한다는 걸 화면이 안 말한다"
    assert "케이블" in ui, "어댑터 시리얼이 무엇인지 화면이 안 말한다"


def test_comparing_gives_ratios_and_deltas():
    diag_store.save("A", _run(err=1.0, cur=2.0))
    diag_store.save("B", _run(err=2.0, cur=1.0))
    out = diag_store.compare(diag_store.get("A"), diag_store.get("B"))
    cell = out["joints"]["joint4"]["err_max_deg"]
    assert cell == {"a": 1.0, "b": 2.0, "delta": 1.0, "ratio": 2.0}
    assert out["joints"]["joint4"]["current_max_a"]["ratio"] == 0.5


def test_a_different_motion_is_called_out_not_hidden():
    """⚠ **모션이 다르면 비교가 거짓이다** — 진폭이나 강도가 다른 두 회차의 전류를
    나란히 놓으면 관절이 아니라 계획의 차이를 보는 것이다. 막지는 않되 다르다는
    사실을 함께 낸다: 감추면 사람이 관절을 의심한다."""
    diag_store.save("A", _run(amp=10.0, intensity="gentle"))
    diag_store.save("B", _run(amp=30.0, intensity="strong"))
    out = diag_store.compare(diag_store.get("A"), diag_store.get("B"))
    assert out["plan_differs"], "모션이 다른데 아무 말도 안 한다"
    assert any("강도" in m for m in out["plan_differs"])

    ui = code_only(PANEL.read_text())
    assert "cmp.plan_differs.length > 0" in ui, "화면이 그 경고를 안 띄운다"


def test_a_broken_file_does_not_block_the_list():
    """⚠ 깨진 파일 하나가 목록 전체를 막으면, 정작 필요한 나머지를 못 본다."""
    diag_store.save("A", _run())
    (diag_store.ROOT / "broken.json").write_text("{not json")
    assert [r["name"] for r in diag_store.list_saved()] == ["A"]


@pytest.mark.parametrize("name", ["", "../escape", "a" * 61, "bad/name"])
def test_a_name_cannot_escape_the_folder(name):
    """⚠ 이름이 경로가 되면 저장소 밖에 쓴다."""
    with pytest.raises(ValueError):
        diag_store.save(name, _run())
