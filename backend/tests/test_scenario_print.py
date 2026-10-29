"""시나리오 설계기 — 작업판 PDF (feature/scenario-designer.md §10).

지키는 것:

1. **mm 가 그대로다** — 용지 크기가 정확하고, 장 나누기가 겹침 띠를 셈한다.
2. **한글 글꼴이 없어도 찍힌다** — 컨테이너에 CJK 글꼴이 없으면 영어 문구로. 깨진 네모(□)가
   찍힌 판보다 낫다.
3. 태그 자리는 판 크기만으로 정해진다 — 겹쳐보기가 PDF 없이도 같은 자리를 안다.
"""

import re

import pytest

from app.services.scenario import ScenarioError, printing
from app.services.scenario.board import Board


def _board(cols=5, rows=4, cell=80):
    return Board.from_dict({"cols": cols, "rows": rows, "cell_mm": cell,
                            "zones": [{"id": "bin", "label": "상자", "cells": ["A1"]}]})


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


def test_layout_picks_fewer_pages():
    lay = printing.layout(_board(5, 4), "a4")      # 판 400×320 + 띠 → 460×380
    assert lay.canvas_w == 460 and lay.canvas_h == 380
    assert lay.orientation == "landscape" and lay.pages == 4
    a3 = printing.layout(_board(5, 4), "a3")
    assert a3.pages < lay.pages


def test_small_board_is_one_page():
    lay = printing.layout(_board(2, 2, 60), "a4")
    assert lay.pages == 1


def test_overlap_is_counted():
    """인쇄 폭에서 겹침 띠만큼 덜 나아간다 — 겹침을 빼먹으면 장 사이에 틈이 생긴다."""
    pw = 297 - 2 * printing.MARGIN_MM
    exactly_two = 2 * pw - printing.OVERLAP_MM
    assert printing._tiles(exactly_two, pw) == 2
    assert printing._tiles(exactly_two + 1, pw) == 3


@pytest.mark.parametrize("mode", printing.MODES)
def test_render_every_mode(mode):
    b = _board()
    pdf = printing.render(b, mode=mode, title="사과", labels={"robot": "로봇 쪽"})
    assert pdf.startswith(b"%PDF")
    assert _pages(pdf) == printing.layout(b).pages
    # A4 가로 = 841.89 × 595.28 pt
    assert re.search(rb"/MediaBox \[ ?0 0 841\.8\d+ 595\.2\d+ ?\]", pdf)


def test_without_cjk_font_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(printing, "_font_family", lambda: (["DejaVu Sans"], False))
    pdf = printing.render(_board(), labels={"robot": "로봇 쪽", "scale": "{mm} mm 실제 크기"})
    assert pdf.startswith(b"%PDF")


def test_label_with_bad_placeholder_does_not_break_printing():
    pdf = printing.render(_board(), labels={"gap": "{없는자리}"})
    assert pdf.startswith(b"%PDF")


def test_tag_centers_sit_in_the_strip_corners():
    b = _board(5, 4)
    s = printing.STRIP_MM / 2
    assert printing.tag_centers(b) == [(0, -s, -s), (1, 400 + s, -s), (2, 400 + s, 320 + s), (3, -s, 320 + s)]


def test_bad_inputs_are_refused():
    with pytest.raises(ScenarioError):
        printing.render(_board(), mode="neon")
    with pytest.raises(ScenarioError):
        printing.render(_board(), line_mm=3)
    with pytest.raises(ScenarioError):
        printing.layout(_board(), "letter")


def test_print_api_returns_pdf():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    body = {"board": {"cols": 4, "rows": 3, "cell_mm": 70}, "paper": "a4", "mode": "bold"}
    r = c.post("/api/scenario/print", json=body)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    lay = c.post("/api/scenario/print/layout", json=body).json()
    assert lay["pages"] == _pages(r.content)


@pytest.mark.parametrize("side", ["top", "bottom", "left", "right"])
def test_robot_mark_on_every_side(side):
    b = Board.from_dict({"cols": 5, "rows": 4, "cell_mm": 80, "robot_side": side})
    pdf = printing.render(b, labels={"robot": "로봇 쪽"})
    assert _pages(pdf) == printing.layout(b).pages


def test_sheet_grows_to_cover_outside_zones():
    """바깥 상자 자리도 종이에 찍힌다 — 판(띠·태그)이 바깥 구역까지 넓어진다."""
    inside = Board.from_dict({"cols": 5, "rows": 4, "cell_mm": 80})
    out = Board.from_dict({"cols": 5, "rows": 4, "cell_mm": 80, "zones": [
        {"id": "bin", "outside": {"side": "top", "start": 1, "end": 3, "depth_mm": 120, "gap_mm": 10}}]})
    assert printing.layout(out).canvas_h == printing.layout(inside).canvas_h + 130
    s = printing.STRIP_MM / 2
    assert printing.tag_centers(out)[0] == (0, -s, -130 - s)
    pdf = printing.render(out, mode="faint")
    assert _pages(pdf) == printing.layout(out).pages
