"""색(HSV)과 크기로 빨강·노랑·작은 사과를 찾는다 — `autolabel.py` 가 작은 사과를 보탤 때 쓴다.
cv2·numpy 만 쓰는 순수 함수다. 플러그인 런타임은 이걸 쓰지 않는다(학습한 검출기를 쓴다).

혼자서는 세기에 못 쓴다: 두 색이 섞인 큰 빨간 사과(옅은 노란 줄무늬)가 고리 조각으로 쪼개져
작은 사과 여럿이 된다 — 600 프레임 판 위 개수 정확도 시작 79% · 끝 92%.

COCO 검출기도 이 장면에서 혼자 쓸 수 없었다(2026-10-31, `dualarm_sort_apple_stage` 위 카메라 실측):
작은 사과(지름 ~35 px)를 못 잡고, 노란 사과는 `apple`·`orange` 두 상자로, 붙은 두 개는 한 상자로 낸다.
흰 판 위의 채도 높은 사과는 색으로 거의 그대로 떨어진다 —

    빨강 H ≥168 또는 ≤4 · 노랑 H 19~35 · 주황 상자 H 8~18 · 판 S ≈ 0 · 그림자 H 145~166 S < 72   (OpenCV H 0~179)

큰 빨간 사과의 옅은 줄무늬는 채도가 50~80 이라 S ≥ 80 만 쓰면 사과가 조각나 작은 사과 여럿으로
세어졌다. 그래서 **이력 문턱**이다 — S ≥ `s_min` 인 씨앗이 닿는 S ≥ `s_weak` 영역까지만 사과로 본다.
붙어 있는 사과는 채운 마스크의 거리 변환 봉우리로 가른다 (봉우리 값 = 안쪽 반지름).
작은 사과도 빨갛다 — `small` 은 색이 아니라 지름으로 정한다.

판 위인지는 **중심**이 ROI 안인지로 본다. 상자 안 사과도 찾아서 `on_board=False` 로 돌려준다
(미리보기에서 보이라고) — 세기(`count`)는 판 위만 센다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

LABELS = ("red", "yellow", "small")


@dataclass
class BoardSpec:
    #: 판 영역 x1,y1,x2,y2 (0~1) — 사과 중심이 이 안이면 판 위
    roi: tuple[float, float, float, float] = (0.16, 0.31, 0.89, 0.92)
    #: 씨앗 채도 — 이 이상인 픽셀이 있어야 사과다
    s_min: int = 80
    #: 씨앗에 이어진 영역은 이 채도까지 사과로 본다 (옅은 줄무늬·반사광 가장자리)
    s_weak: int = 40
    v_min: int = 60
    #: lo > hi 면 0 을 넘어가는 범위다 (빨강)
    red_hue: tuple[int, int] = (168, 4)
    yellow_hue: tuple[int, int] = (19, 35)
    #: 지름(px)이 이보다 작으면 `small`
    small_px: float = 50.0
    #: 지름(px)이 이보다 작은 얼룩은 버린다
    min_px: float = 16.0


def parse_roi(text: str) -> tuple[float, float, float, float]:
    parts = [float(v) for v in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError("roi 는 x1,y1,x2,y2 네 값 (0~1)")
    x1, y1, x2, y2 = parts
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def parse_hue(text: str) -> tuple[int, int]:
    parts = [int(float(v)) for v in str(text).split(",")]
    if len(parts) != 2 or not all(0 <= v <= 179 for v in parts):
        raise ValueError("색상 범위는 lo,hi 두 값 (0~179)")
    return parts[0], parts[1]


def _hue_mask(h: np.ndarray, lo: int, hi: int) -> np.ndarray:
    return (h >= lo) | (h <= hi) if lo > hi else (h >= lo) & (h <= hi)


def _grow(seed: np.ndarray, weak: np.ndarray) -> np.ndarray:
    """`weak` 의 연결 성분 중 `seed` 픽셀을 품은 것만 — 이력 문턱 (씨앗 ⊂ weak 이어야 한다)."""
    n, lab = cv2.connectedComponents(weak)
    keep = np.zeros(n, bool)
    keep[np.unique(lab[seed > 0])] = True
    keep[0] = False
    return keep[lab].astype(np.uint8) * 255


def _filled(mask: np.ndarray) -> np.ndarray:
    """얼룩 정리 + 구멍 메우기 — 반사광(채도 낮음)·꼭지(어두움)가 사과 안에 구멍을 낸다."""
    k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    m = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k3)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k5)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros_like(m)
    cv2.drawContours(out, contours, -1, 255, thickness=cv2.FILLED)
    return out


def _peaks(mask: np.ndarray, min_r: float) -> list[tuple[float, float, float]]:
    """거리 변환 봉우리 `(반지름, x, y)` — 큰 것부터. 한 사과 안의 잔 봉우리는 눌러 없앤다."""
    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    k = max(3, int(min_r) * 2 + 1)
    dil = cv2.dilate(dist, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    peak = ((dist >= dil - 1e-3) & (dist >= min_r)).astype(np.uint8)
    n, lab = cv2.connectedComponents(peak)
    cands = []
    for i in range(1, n):
        ys, xs = np.nonzero(lab == i)
        cands.append((float(dist[ys, xs].max()), float(xs.mean()), float(ys.mean())))
    cands.sort(reverse=True)
    kept: list[tuple[float, float, float]] = []
    for r, x, y in cands:
        # 맞닿은 두 사과의 중심 거리는 kr + r 이다. 큰 것부터 받으므로, 받은 사과의 원 안쪽
        # (kr + 0.3r) 에 중심이 있으면 따로 선 사과일 수 없다 — 같은 사과의 잔 봉우리다
        if all(math.hypot(x - kx, y - ky) > kr + 0.3 * r for kr, kx, ky in kept):
            kept.append((r, x, y))
    return kept


def find_apples(frame_bgr: np.ndarray, spec: BoardSpec) -> list[dict]:
    """프레임 → 사과 목록 `{label, conf, bbox, center, diameter, on_board}` (좌표 px)."""
    h, w = frame_bgr.shape[:2]
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    lit = val >= spec.v_min
    x1, y1, x2, y2 = spec.roi
    out = []
    for color, (lo, hi) in (("red", spec.red_hue), ("yellow", spec.yellow_hue)):
        weak = lit & _hue_mask(hue, lo, hi) & (sat >= min(spec.s_weak, spec.s_min))
        seed = weak & (sat >= spec.s_min)
        mask = _filled(_grow(seed.astype(np.uint8), weak.astype(np.uint8)))
        for r, cx, cy in _peaks(mask, spec.min_px / 2):
            d = 2 * r
            out.append({
                "label": "small" if d < spec.small_px else color,
                "conf": 1.0,
                "bbox": [round(cx - r, 1), round(cy - r, 1), round(cx + r, 1), round(cy + r, 1)],
                "center": [round(cx, 1), round(cy, 1)],
                "diameter": round(d, 1),
                "on_board": x1 <= cx / w <= x2 and y1 <= cy / h <= y2,
            })
    return out


def count(apples: list[dict]) -> dict[str, int]:
    """판 위 사과 수 — 라벨마다 (없는 라벨도 0 으로)."""
    c = {k: 0 for k in LABELS}
    for a in apples:
        if a["on_board"]:
            c[a["label"]] = c.get(a["label"], 0) + 1
    return c


def draw(frame_bgr: np.ndarray, apples: list[dict], spec: BoardSpec, lines: list[str] = ()) -> np.ndarray:
    """미리보기 — ROI 사각형, 사과 원(판 밖은 회색), 왼쪽 위에 글 몇 줄."""
    img = frame_bgr.copy()
    h, w = img.shape[:2]
    x1, y1, x2, y2 = spec.roi
    cv2.rectangle(img, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (0, 200, 0), 1)
    colors = {"red": (40, 40, 230), "yellow": (0, 210, 240), "small": (230, 120, 30)}
    for a in apples:
        cx, cy = (int(v) for v in a["center"])
        col = colors.get(a["label"], (200, 200, 200)) if a["on_board"] else (150, 150, 150)
        cv2.circle(img, (cx, cy), max(2, int(a["diameter"] / 2)), col, 2)
        cv2.putText(img, a["label"], (cx - 20, cy - int(a["diameter"] / 2) - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
    for i, text in enumerate(lines):
        cv2.putText(img, text, (8, 18 + 18 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, text, (8, 18 + 18 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return img
