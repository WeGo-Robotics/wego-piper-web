"""작업판 PDF — A4·A3 여러 장으로 나눠 찍는다 (feature/scenario-designer.md §10).

matplotlib `PdfPages` 로 굽는다. 그림 크기를 인치로 주면 mm 가 그대로 나오는 벡터 PDF 다 —
브라우저 인쇄(SVG + `@page`)는 브라우저마다 여백·축소 처리가 달라 mm 를 장담 못 한다.

## 모드 (§10-2)

- `faint`   — 사람 눈에만 보이는 판(기본). 탑 카메라 한 픽셀이 바닥에서 1mm 가까이를 덮으므로
  그 1/3 이하 굵기의 연회색 선은 한 픽셀 안에서 바탕과 섞여 대비가 몇 % 로 떨어지고, 영상
  압축이 그것마저 지운다. 칸 이름·자·안내는 판 둘레 **띠**로 몬다.
- `bold`    — 진한 판. 칸 이름이 칸 안에 들어간다.
- `overlay` — 겹쳐보기 전용 보정 카드. 윤곽·태그·자만 찍고 판은 보정 뒤에 치운다.

## 띠

판(격자 + 바깥 구역) 둘레 30mm 띠에 AprilTag(36h11, 네 모서리)·칸 이름·100mm 자·로봇 표시가 들어간다.
태그는 흐리게 찍을 수 없다 — 대비가 있어야 검출된다. 그래서 띠는 접거나 잘라 낼 수 있게
두고 보정·가시성 검사 때만 펼친다. 태그 자리(`tag_centers`)는 판 크기만으로 정해지므로
나중의 겹쳐보기(호모그래피)가 PDF 없이도 같은 자리를 안다.

## 문구

PDF 안 문구는 **화면이 넘긴다**(`labels`) — 화면 언어로 이미 옮긴 문장이다. 이 모듈은 번역을
모르고, 받은 문장을 그릴 한글 글꼴이 없으면(컨테이너에 CJK 글꼴이 없을 수 있다) 영어 기본
문구로 찍는다. 깨진 네모(□)가 찍힌 판보다 영어가 낫다.
"""

from __future__ import annotations

import io
import math
from dataclasses import asdict, dataclass

from app.services.scenario import ScenarioError
from app.services.scenario.board import Board

PAPERS = {"a4": (210.0, 297.0), "a3": (297.0, 420.0)}
MODES = ("faint", "bold", "overlay")
MARGIN_MM = 5.0     # 프린터가 못 찍는 가장자리
OVERLAP_MM = 10.0   # 장 사이 겹침 띠
STRIP_MM = 30.0     # 판 둘레 띠 — 태그·칸 이름·자·안내
TAG_MM = 20.0
LINE_MM_RANGE = (0.1, 1.0)

FAINT = "#d6d6d6"   # 흰 종이 대비 약 16% — 0.25mm 선이면 카메라 한 픽셀에서 몇 % 로 묽어진다
STRIP_TEXT = "#808080"

LABELS_EN = {
    "robot": "Robot side",
    "gap": "robot base center {mm} mm",
    "scale": "{mm} mm - print at actual size (100%), turn off 'fit to page'",
    "fold": "Fold under or cut off this strip - unfold only to calibrate",
    "page": "{n}/{total} (row {row}, col {col})",
}

_MM_TO_PT = 72 / 25.4


@dataclass(frozen=True)
class Layout:
    paper: str
    orientation: str
    page_w: float
    page_h: float
    cols: int
    rows: int
    canvas_w: float
    canvas_h: float

    @property
    def pages(self) -> int:
        return self.cols * self.rows

    @property
    def printable(self) -> tuple[float, float]:
        return (self.page_w - 2 * MARGIN_MM, self.page_h - 2 * MARGIN_MM)

    def to_dict(self) -> dict:
        return {**asdict(self), "pages": self.pages}


def _tiles(total: float, printable: float) -> int:
    if total <= printable:
        return 1
    return math.ceil((total - OVERLAP_MM) / (printable - OVERLAP_MM))


def layout(board: Board, paper: str = "a4") -> Layout:
    """장 나누기 — 세로·가로 중 장수가 적은 쪽."""
    if paper not in PAPERS:
        raise ScenarioError(f"용지는 {', '.join(PAPERS)} 중 하나입니다")
    fx0, fy0, fx1, fy1 = board.frame         # 격자 + 바깥 구역
    cw, ch = fx1 - fx0 + 2 * STRIP_MM, fy1 - fy0 + 2 * STRIP_MM
    short, long_ = PAPERS[paper]
    options = []
    for orient, (pw, ph) in (("portrait", (short, long_)), ("landscape", (long_, short))):
        cols = _tiles(cw, pw - 2 * MARGIN_MM)
        rows = _tiles(ch, ph - 2 * MARGIN_MM)
        # 동률이면 판의 모양을 따른다 — 넓은 판은 가로로
        prefer = 0 if (orient == "landscape") == (cw >= ch) else 1
        options.append((cols * rows, prefer, Layout(paper, orient, pw, ph, cols, rows, cw, ch)))
    return min(options, key=lambda o: o[:2])[2]


def tag_centers(board: Board) -> list[tuple[int, float, float]]:
    """(태그 id, x, y) — 판 좌표(mm). 띠의 네 모서리, 그림의 왼쪽 위가 0 이고 시계 방향.

    띠는 격자가 아니라 **격자 + 바깥 구역**(`board.frame`)을 두른다.
    """
    s = STRIP_MM / 2
    x0, y0, x1, y1 = board.frame
    return [(0, x0 - s, y0 - s), (1, x1 + s, y0 - s), (2, x1 + s, y1 + s), (3, x0 - s, y1 + s)]


def _tag_image(tag_id: int):
    import cv2

    dic = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    return cv2.aruco.generateImageMarker(dic, tag_id, 200)


def _font_family() -> tuple[list[str], bool]:
    """(글꼴 목록, 한글을 그릴 수 있나). CJK 글꼴이 없으면 DejaVu 만."""
    from matplotlib import font_manager

    names = {f.name for f in font_manager.fontManager.ttflist}
    for cand in ("Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "Noto Sans CJK JP",
                 "Noto Sans CJK SC", "Malgun Gothic", "AppleGothic"):
        if cand in names:
            return [cand, "DejaVu Sans"], True
    return ["DejaVu Sans"], False


def _has_cjk(text: str) -> bool:
    return any(0x1100 <= ord(ch) <= 0x11FF or 0x3040 <= ord(ch) <= 0x30FF
               or 0x3130 <= ord(ch) <= 0x318F or 0x4E00 <= ord(ch) <= 0x9FFF
               or 0xAC00 <= ord(ch) <= 0xD7AF for ch in text)


def render(
    board: Board,
    *,
    paper: str = "a4",
    mode: str = "faint",
    line_mm: float = 0.25,
    title: str = "",
    labels: dict[str, str] | None = None,
) -> bytes:
    """작업판 PDF 바이트."""
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    if mode not in MODES:
        raise ScenarioError(f"인쇄 모드는 {', '.join(MODES)} 중 하나입니다")
    lo, hi = LINE_MM_RANGE
    if not lo <= line_mm <= hi:
        raise ScenarioError(f"선 굵기는 {lo:g}~{hi:g}mm 입니다")
    lay = layout(board, paper)
    family, cjk = _font_family()

    def label(key: str, **kw) -> str:
        text = (labels or {}).get(key) or LABELS_EN[key]
        if _has_cjk(text) and not cjk:
            text = LABELS_EN[key]
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return LABELS_EN[key].format(**kw)

    safe_title = title if (cjk or not _has_cjk(title)) else ""
    w, h, cell = board.width_mm, board.height_mm, board.cell_mm
    faint, bold, overlay = mode == "faint", mode == "bold", mode == "overlay"
    pw, ph = lay.printable
    tags = [(tid, x, y, _tag_image(tid)) for tid, x, y in tag_centers(board)]
    fx0, fy0, fx1, fy1 = board.frame

    def lw(mm: float) -> float:
        return mm * _MM_TO_PT

    grid_color, grid_lw = (FAINT, line_mm) if faint else ("#000000", 0.5)
    mark_color = FAINT if faint else "#777777"

    def draw(ax, x0: float, y0: float, n: int, row: int, col: int) -> None:
        def line(xs, ys, color, width, **kw):
            ax.plot(xs, ys, color=color, linewidth=lw(width), solid_capstyle="butt", **kw)

        def text(x, y, s, size, color, **kw):
            ax.text(x, y, s, fontsize=size, color=color, family=family, clip_on=True, **kw)

        # 판 윤곽
        line([0, w, w, 0, 0], [0, 0, h, h, 0], grid_color if not overlay else "#666666",
             grid_lw if not overlay else 0.3)

        if not overlay:
            for c in range(1, board.cols):
                line([c * cell, c * cell], [0, h], grid_color, grid_lw)
            for r in range(1, board.rows):
                line([0, w], [r * cell, r * cell], grid_color, grid_lw)
            arm = 1.5
            for name in board.cells:
                cx, cy = board.center_mm(name)
                if name in board.blocked:
                    k = cell * 0.3
                    line([cx - k, cx + k], [cy - k, cy + k], mark_color, grid_lw)
                    line([cx - k, cx + k], [cy + k, cy - k], mark_color, grid_lw)
                    continue
                line([cx - arm, cx + arm], [cy, cy], mark_color, grid_lw)
                line([cx, cx], [cy - arm, cy + arm], mark_color, grid_lw)
                if bold:
                    text(cx - cell / 2 + 2, cy - cell / 2 + 2, name, 7, "#555555", ha="left", va="top")

            # 구역 — 같은 구역끼리 맞닿은 변은 빼고 바깥 변만
            zone_color, zone_lw = ("#aaaaaa", max(line_mm, 0.4)) if faint else ("#000000", 0.8)
            for z in board.zones:
                cells = set(z.cells)
                for name in z.cells:
                    ci, ri = board.parse(name)
                    x1, y1 = ci * cell, ri * cell
                    for dc, dr, xs, ys in ((0, -1, [x1, x1 + cell], [y1, y1]),
                                           (0, 1, [x1, x1 + cell], [y1 + cell, y1 + cell]),
                                           (-1, 0, [x1, x1], [y1, y1 + cell]),
                                           (1, 0, [x1 + cell, x1 + cell], [y1, y1 + cell])):
                        nc, nr = ci + dc, ri + dr
                        inside = 0 <= nc < board.cols and 0 <= nr < board.rows
                        if not (inside and Board.name(nc, nr) in cells):
                            line(xs, ys, zone_color, zone_lw)
                rect = board.zone_rect(z)
                if rect:                              # 바깥 구역 — 직사각형 하나
                    rx, ry, rw, rh = rect
                    line([rx, rx + rw, rx + rw, rx, rx], [ry, ry, ry + rh, ry + rh, ry], zone_color, zone_lw)
                    zx, zy = rx + rw / 2, ry + rh / 2
                else:
                    zx = sum(board.center_mm(c)[0] for c in z.cells) / len(z.cells)
                    zy = sum(board.center_mm(c)[1] for c in z.cells) / len(z.cells)
                zl = z.label if (cjk or not _has_cjk(z.label)) else z.id
                text(zx, zy, zl, 8, "#999999" if faint else "#333333", ha="center", va="center")

            # 칸 이름 — 띠에 (흐린 판은 여기에만 있다)
            for c in range(board.cols):
                x = (c + 0.5) * cell
                for y in (-5.0, h + 5.0):
                    text(x, y, Board.name(c, 0)[0], 8, STRIP_TEXT, ha="center", va="center")
            for r in range(board.rows):
                y = (r + 0.5) * cell
                for x in (-5.0, w + 5.0):
                    text(x, y, str(r + 1), 8, STRIP_TEXT, ha="center", va="center")

            if faint:
                # 접는 선 — 격자 + 바깥 구역 바로 바깥
                d = 2.0
                line([fx0 - d, fx1 + d, fx1 + d, fx0 - d, fx0 - d], [fy0 - d, fy0 - d, fy1 + d, fy1 + d, fy0 - d],
                     "#c0c0c0", 0.2, linestyle=(0, (4, 3)))
                text(fx1 - 2, fy0 - STRIP_MM + 8, label("fold"), 5.5, STRIP_TEXT, ha="right", va="center")

        # 로봇 쪽 — 로봇이 있는 변의 띠에 화살표·간격. 아래 띠는 자(100mm)가 위 두 줄을 쓰므로
        # 그 아래 줄에, 왼쪽·오른쪽 띠는 세워서 쓴다
        gap_text = label("gap", mm=f"{board.robot_gap_mm:g}")
        side = board.robot_side
        if side == "top":
            line([w / 2, w / 2], [0, -4], "#555555", 0.4)
            text(w / 2, fy0 - STRIP_MM + 8, "\u25b2 " + label("robot"), 8, "#444444", ha="center", va="center")
            text(w / 2, fy0 - STRIP_MM + 14, gap_text, 6, STRIP_TEXT, ha="center", va="center")
        elif side == "bottom":
            line([w / 2, w / 2], [h, h + 4], "#555555", 0.4)
            text(w / 2, fy1 + 22, "\u25bc " + label("robot"), 8, "#444444", ha="center", va="center")
            text(w / 2, fy1 + 27, gap_text, 6, STRIP_TEXT, ha="center", va="center")
        else:
            # 세운 글자에서 ▲ 의 위가 곧 바깥(로봇 쪽)이다 — 왼쪽 띠는 반시계, 오른쪽 띠는 시계 방향으로 세운다
            sx, rot = (fx0 - STRIP_MM + 8, 90) if side == "left" else (fx1 + STRIP_MM - 8, -90)
            arrow = "\u25b2 "
            gx = sx + (6 if side == "left" else -6)
            ex = (0, -4) if side == "left" else (w, w + 4)
            line(list(ex), [h / 2, h / 2], "#555555", 0.4)
            text(sx, h / 2, arrow + label("robot"), 8, "#444444", ha="center", va="center", rotation=rot)
            text(gx, h / 2, gap_text, 6, STRIP_TEXT, ha="center", va="center", rotation=rot)
        if safe_title:
            text(fx0 + 2, fy0 - STRIP_MM + 8, safe_title, 7, "#444444", ha="left", va="center")

        # 100mm 자 — 프린터 축소를 잡는다
        bar = 100.0 if fx1 - fx0 >= 120 else 50.0
        bx, by = fx0 + 5, fy1 + 10
        line([bx, bx + bar], [by, by], "#000000", 0.4)
        for k in range(int(bar / 10) + 1):
            tick = 3.0 if k % 5 == 0 else 1.5
            line([bx + k * 10, bx + k * 10], [by - tick, by], "#000000", 0.25)
        text(bx, by + 6, label("scale", mm=f"{bar:g}"), 6, "#444444", ha="left", va="center")

        # 등록 십자 (로봇 좌표 등록용, §12) — 판의 세 모서리
        for cx, cy in ((0, 0), (w, 0), (0, h)):
            line([cx - 4, cx + 4], [cy, cy], mark_color if faint else "#000000", 0.25)
            line([cx, cx], [cy - 4, cy + 4], mark_color if faint else "#000000", 0.25)

        # 장 사이 맞춤 — 겹치는 자리 표시와 띠 안의 맞춤 십자
        step_x, step_y = pw - OVERLAP_MM, ph - OVERLAP_MM
        for j in range(1, lay.cols):
            xs = fx0 - STRIP_MM + j * step_x
            line([xs, xs], [fy0 - STRIP_MM, fy1 + STRIP_MM], "#cfcfcf" if faint else "#999999", 0.2,
                 linestyle=(0, (2, 3)))
            for yy in (fy0 - STRIP_MM + 22, fy1 + STRIP_MM - 6):
                line([xs + 2, xs + 8], [yy, yy], "#666666", 0.25)
                line([xs + 5, xs + 5], [yy - 3, yy + 3], "#666666", 0.25)
        for i in range(1, lay.rows):
            ys = fy0 - STRIP_MM + i * step_y
            line([fx0 - STRIP_MM, fx1 + STRIP_MM], [ys, ys], "#cfcfcf" if faint else "#999999", 0.2,
                 linestyle=(0, (2, 3)))
            for xx in (fx0 - STRIP_MM + 6, fx1 + STRIP_MM - 6):
                line([xx - 3, xx + 3], [ys + 5, ys + 5], "#666666", 0.25)
                line([xx, xx], [ys + 2, ys + 8], "#666666", 0.25)

        for _tid, tx, ty, img in tags:
            half = TAG_MM / 2
            ax.imshow(img, cmap="gray", vmin=0, vmax=255, interpolation="nearest", aspect="auto",
                      extent=(tx - half, tx + half, ty + half, ty - half))

        # 장 번호 — 장의 왼쪽 위 구석에 아주 작게
        text(x0 + 2, y0 + 3, label("page", n=n, total=lay.pages, row=row, col=col), 5,
             "#b0b0b0", ha="left", va="top")

    buf = io.BytesIO()
    with PdfPages(buf, metadata={"Title": safe_title or "PIPER board", "Creator": "PIPER"}) as pdf:
        n = 0
        for i in range(lay.rows):
            for j in range(lay.cols):
                n += 1
                fig = Figure(figsize=(lay.page_w / 25.4, lay.page_h / 25.4))
                ax = fig.add_axes((MARGIN_MM / lay.page_w, MARGIN_MM / lay.page_h,
                                   pw / lay.page_w, ph / lay.page_h))
                x0 = fx0 - STRIP_MM + j * (pw - OVERLAP_MM)
                y0 = fy0 - STRIP_MM + i * (ph - OVERLAP_MM)
                ax.set_xlim(x0, x0 + pw)
                ax.set_ylim(y0 + ph, y0)
                ax.set_axis_off()
                draw(ax, x0, y0, n, i + 1, j + 1)
                ax.set_xlim(x0, x0 + pw)          # imshow 가 한계를 바꾸지 못하게 다시 박는다
                ax.set_ylim(y0 + ph, y0)
                pdf.savefig(fig)
    return buf.getvalue()
