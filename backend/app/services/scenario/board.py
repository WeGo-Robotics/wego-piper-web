"""작업판 — 격자·칸 이름·구역·로봇 자리.

⚠ **칸 이름은 그림 기준이다 — 로봇이 어느 변에 있든.** 칸 이름은 열 글자 + 행 번호이고 `A1` 이
그림(=인쇄물)의 왼쪽 위다. 인쇄물·설계 화면·(나중의) 안내 창과 음성이 모두 같은 그림을 쓰므로
로봇을 어느 변(`robot_side`)에 두든 "B3" 은 종이와 화면에서 같은 칸이다. 로봇 자리를 따라가는
것은 "가까운 순" 규칙의 거리와 인쇄·화면의 로봇 표시뿐이다.

좌표는 판의 왼쪽 위 모서리가 원점, x 는 오른쪽, y 는 아래쪽(mm).

## 구역(상자·지그 자리)은 격자 안에도, 바깥에도

- **안** — 칸의 묶음. 그 칸에는 물체를 놓지 않는다.
- **바깥** — 한 변에 붙은 직사각형. 변을 따라서는 칸에 맞추고(`start`~`end` 번째 칸), 바깥쪽으로는
  `gap_mm` 띄워 `depth_mm` 깊이. 물체 칸을 줄이지 않는다. 인쇄할 때 판이 바깥 구역까지 넓어진다.

생성기에게 구역은 담을 곳(이름)일 뿐이라 둘의 차이를 모른다.

구역(상자·지그 자리)도 칸으로 잡는다. 칸 단위면 화면에서 클릭 한 번으로 정하고, 인쇄도
같은 격자 위에 윤곽만 그리면 된다. 구역과 금지 칸에는 물체를 놓지 않는다.
"""

from __future__ import annotations

import math
import re
import string
from dataclasses import asdict, dataclass

from app.services.scenario import ScenarioError

MAX_COLS = len(string.ascii_uppercase)
MAX_ROWS = 50
CELL_MM_RANGE = (20.0, 300.0)

# 로봇이 있는 변. 상자(구역)는 보통 나머지 세 변에 둔다.
ROBOT_SIDES = ("top", "bottom", "left", "right")

_CELL = re.compile(r"^([A-Z])([1-9][0-9]?)$")
_ZONE_ID = re.compile(r"^[A-Za-z][\w-]{0,31}$")


@dataclass(frozen=True)
class Outside:
    """격자 바깥 구역 — `side` 변의 `start`~`end` 번째 칸(0 부터, 끝 포함) 맞은편."""
    side: str
    start: int
    end: int
    depth_mm: float
    gap_mm: float


@dataclass(frozen=True)
class Zone:
    id: str
    label: str
    cells: tuple[str, ...]           # 격자 안 구역의 칸 — 바깥 구역이면 비어 있다
    outside: Outside | None = None


@dataclass(frozen=True)
class Board:
    cols: int
    rows: int
    cell_mm: float
    blocked: frozenset[str]
    zones: tuple[Zone, ...]
    # 판의 로봇 쪽 가장자리에서 로봇 베이스 중심까지. "가까운 순" 규칙이 이걸로 잰다.
    robot_gap_mm: float = 100.0
    robot_side: str = "top"

    # ── 만들기 ──

    @classmethod
    def from_dict(cls, d: dict) -> Board:
        try:
            cols, rows = int(d["cols"]), int(d["rows"])
            cell_mm = float(d["cell_mm"])
        except (KeyError, TypeError, ValueError):
            raise ScenarioError("작업판에 열·행·칸 크기가 있어야 합니다")
        if not (1 <= cols <= MAX_COLS and 1 <= rows <= MAX_ROWS):
            raise ScenarioError(f"작업판 크기는 1~{MAX_COLS}열, 1~{MAX_ROWS}행입니다")
        lo, hi = CELL_MM_RANGE
        if not (lo <= cell_mm <= hi):
            raise ScenarioError(f"칸 크기는 {lo:g}~{hi:g}mm 입니다")

        def check(name: str) -> str:
            m = _CELL.match(str(name))
            if not m or string.ascii_uppercase.index(m[1]) >= cols or int(m[2]) > rows:
                raise ScenarioError(f"작업판에 없는 칸입니다: {name}")
            return str(name)

        blocked = frozenset(check(c) for c in d.get("blocked", []))
        zones: list[Zone] = []
        taken: dict[str, str] = {}
        for z in d.get("zones", []):
            zid = str(z.get("id", ""))
            if not _ZONE_ID.match(zid):
                raise ScenarioError(f"구역 id 는 영문으로 시작하는 32자 이내여야 합니다: {zid!r}")
            if any(zz.id == zid for zz in zones):
                raise ScenarioError(f"구역 id 가 겹칩니다: {zid}")
            label = str(z.get("label", "")) or zid
            if z.get("outside"):
                zones.append(Zone(zid, label, (), _outside(zid, z["outside"], cols, rows)))
                continue
            cells = tuple(check(c) for c in z.get("cells", []))
            if not cells:
                raise ScenarioError(f"구역 {zid} 에 칸이 없습니다")
            for c in cells:
                if c in taken:
                    raise ScenarioError(f"칸 {c} 가 구역 {taken[c]} 와 {zid} 에 겹칩니다")
                taken[c] = zid
            zones.append(Zone(zid, label, cells))
        outs = [z for z in zones if z.outside]
        for i, a in enumerate(outs):
            for b in outs[i + 1:]:
                if a.outside.side == b.outside.side and not (
                        a.outside.end < b.outside.start or b.outside.end < a.outside.start):
                    raise ScenarioError(f"바깥 구역 {a.id} 와 {b.id} 가 겹칩니다")

        gap = float(d.get("robot_gap_mm", 100.0))
        side = str(d.get("robot_side") or "top")
        if side not in ROBOT_SIDES:
            raise ScenarioError(f"로봇 쪽은 {', '.join(ROBOT_SIDES)} 중 하나입니다")
        board = cls(cols, rows, cell_mm, blocked, tuple(zones), max(0.0, gap), side)
        if not board.work_cells:
            raise ScenarioError("물체를 놓을 칸이 없습니다 — 금지 칸·구역이 판 전체를 덮습니다")
        return board

    # ── 칸 ──

    @staticmethod
    def name(col: int, row: int) -> str:
        return f"{string.ascii_uppercase[col]}{row + 1}"

    @staticmethod
    def parse(name: str) -> tuple[int, int]:
        m = _CELL.match(name)
        if not m:
            raise ScenarioError(f"칸 이름이 아닙니다: {name}")
        return string.ascii_uppercase.index(m[1]), int(m[2]) - 1

    @property
    def cells(self) -> list[str]:
        """행 우선(그림의 위 줄부터), 줄 안에서는 왼쪽부터."""
        return [self.name(c, r) for r in range(self.rows) for c in range(self.cols)]

    def zone_rect(self, z: Zone) -> tuple[float, float, float, float] | None:
        """바깥 구역의 직사각형 (x, y, 폭, 높이) mm. 격자 안 구역이면 None."""
        o = z.outside
        if o is None:
            return None
        cell = self.cell_mm
        along0, along = o.start * cell, (o.end - o.start + 1) * cell
        if o.side == "top":
            return (along0, -o.gap_mm - o.depth_mm, along, o.depth_mm)
        if o.side == "bottom":
            return (along0, self.height_mm + o.gap_mm, along, o.depth_mm)
        if o.side == "left":
            return (-o.gap_mm - o.depth_mm, along0, o.depth_mm, along)
        return (self.width_mm + o.gap_mm, along0, o.depth_mm, along)

    @property
    def frame(self) -> tuple[float, float, float, float]:
        """격자 + 바깥 구역을 덮는 사각형 (x0, y0, x1, y1) mm — 인쇄물이 덮어야 하는 범위."""
        x0, y0, x1, y1 = 0.0, 0.0, self.width_mm, self.height_mm
        for z in self.zones:
            r = self.zone_rect(z)
            if r:
                x0, y0 = min(x0, r[0]), min(y0, r[1])
                x1, y1 = max(x1, r[0] + r[2]), max(y1, r[1] + r[3])
        return (x0, y0, x1, y1)

    @property
    def zone_cells(self) -> set[str]:
        return {c for z in self.zones for c in z.cells}

    @property
    def work_cells(self) -> list[str]:
        """물체를 놓을 수 있는 칸."""
        off = self.blocked | self.zone_cells
        return [c for c in self.cells if c not in off]

    def zone(self, zid: str) -> Zone | None:
        return next((z for z in self.zones if z.id == zid), None)

    # ── 기하 ──

    @property
    def width_mm(self) -> float:
        return self.cols * self.cell_mm

    @property
    def height_mm(self) -> float:
        return self.rows * self.cell_mm

    def center_mm(self, name: str) -> tuple[float, float]:
        c, r = self.parse(name)
        return ((c + 0.5) * self.cell_mm, (r + 0.5) * self.cell_mm)

    @property
    def robot_mm(self) -> tuple[float, float]:
        """로봇 베이스 중심 — 로봇 쪽 변의 가운데에서 `robot_gap_mm` 바깥."""
        w, h, g = self.width_mm, self.height_mm, self.robot_gap_mm
        return {"top": (w / 2, -g), "bottom": (w / 2, h + g),
                "left": (-g, h / 2), "right": (w + g, h / 2)}[self.robot_side]

    def tie_key(self, name: str) -> int:
        """거리가 같은 칸끼리의 순서 — 로봇이 위·아래면 왼쪽부터, 왼쪽·오른쪽이면 위쪽부터.

        거리가 같은 두 칸은 로봇을 지나는 축에 대해 좌우 대칭이다. 그 축을 가로지르는 방향
        (위·아래 로봇이면 열, 왼쪽·오른쪽 로봇이면 행)으로 가른다.
        """
        c, r = self.parse(name)
        return c if self.robot_side in ("top", "bottom") else r

    def dist_to_robot(self, name: str) -> float:
        x, y = self.center_mm(name)
        rx, ry = self.robot_mm
        # ⚠ 0.1mm 로 반올림한다 — 좌우 대칭 칸이 부동소수 오차로 갈리면 "같으면 왼쪽부터" 규칙이
        #   조용히 안 걸린다.
        return round(math.hypot(x - rx, y - ry), 1)

    def chebyshev(self, a: str, b: str) -> int:
        (ac, ar), (bc, br) = self.parse(a), self.parse(b)
        return max(abs(ac - bc), abs(ar - br))

    def to_dict(self) -> dict:
        return {
            "cols": self.cols, "rows": self.rows, "cell_mm": self.cell_mm,
            "blocked": sorted(self.blocked),
            "zones": [{"id": z.id, "label": z.label, "cells": list(z.cells),
                       **({"outside": asdict(z.outside)} if z.outside else {})} for z in self.zones],
            "robot_gap_mm": self.robot_gap_mm,
            "robot_side": self.robot_side,
        }


def _outside(zid: str, d: dict, cols: int, rows: int) -> Outside:
    side = str(d.get("side", ""))
    if side not in ROBOT_SIDES:
        raise ScenarioError(f"바깥 구역 {zid} 의 변은 {', '.join(ROBOT_SIDES)} 중 하나입니다")
    length = cols if side in ("top", "bottom") else rows
    try:
        start, end = int(d.get("start", 0)), int(d.get("end", 0))
        depth, gap = float(d.get("depth_mm", 120)), float(d.get("gap_mm", 10))
    except (TypeError, ValueError):
        raise ScenarioError(f"바깥 구역 {zid} 의 자리가 숫자가 아닙니다")
    if not 0 <= start <= end < length:
        raise ScenarioError(f"바깥 구역 {zid} 의 칸 범위가 변을 벗어납니다")
    if not (10 <= depth <= 500 and 0 <= gap <= 300):
        raise ScenarioError(f"바깥 구역 {zid} 의 깊이는 10~500mm, 띄움은 0~300mm 입니다")
    return Outside(side, start, end, depth, gap)
