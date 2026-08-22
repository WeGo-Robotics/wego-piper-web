"""회색 카드로 카메라의 색·밝기를 맞춘다 — **순수 로직**
(feature/gray-card-calibration.md).

`controls.py` 와 같은 자리다: camerad 와 rsd 가 함께 쓰고, 하드웨어 없이
경계를 시험할 수 있어야 한다.

## 왜 필요한가

정책은 색에 민감하다. 오전에 모은 데이터와 오후에 도는 추론이 화이트밸런스만
달라도 다른 관측이 된다. 카메라가 둘이면 서로도 안 맞는다 — 탑뷰와 손목이 같은
물체를 다른 색으로 보면 정책이 그걸 **다른 물체의 특징으로 배운다.**

회색 카드는 그 둘에 **재현 가능한 기준점**을 준다: "이 카드가 중성 회색으로,
정해진 밝기로 보이게 하라." 사람이 슬라이더를 감으로 맞추는 것과 달리 다음 주에도
같은 결과가 나온다.

## 무엇을 재나

- **중성도** — 카드 위에서 R·G·B 가 같은가. 다르면 화이트밸런스가 틀어진 것이다
- **밝기** — 카드가 목표 밝기로 보이는가. 다르면 노출이 틀어진 것이다

## 무엇을 못 하나

⚠ **기하 캘리브레이션이 아니다.** 렌즈 왜곡·초점거리·카메라 간 위치는 체커보드가
필요하고 여기서 다루지 않는다. 회색 카드는 **광학(색·밝기)** 전용이다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 18% 회색 카드가 sRGB 로 인코딩됐을 때의 값. 0.18^(1/2.2) × 255 ≈ 118.
TARGET_LUMA = 118.0

# 이 안이면 맞은 것으로 본다. 카드 인쇄 편차와 센서 잡음이 이 정도 된다.
LUMA_TOLERANCE = 8.0
NEUTRAL_TOLERANCE_PCT = 2.0

# 판정을 믿을 수 없게 만드는 것들
_CLIP_LOW, _CLIP_HIGH = 4, 251      # 포화되면 진짜 밝기를 모른다
_MAX_CLIPPED_PCT = 5.0
_MAX_SPREAD_PCT = 12.0              # 카드가 고르게 안 보이면(그림자·반사) 못 믿는다


@dataclass(frozen=True)
class GrayCardReading:
    """카드 영역에서 읽은 값. **판정과 처방을 여기서 다 뽑는다.**"""

    b: float
    g: float
    r: float
    luma: float
    clipped_pct: float
    spread_pct: float
    pixels: int

    @property
    def neutral_error_pct(self) -> float:
        """R·G·B 가 평균에서 얼마나 벗어났나. 0 이면 완전 중성."""
        mean = (self.b + self.g + self.r) / 3.0
        if mean <= 0:
            return 100.0
        return max(abs(c - mean) for c in (self.b, self.g, self.r)) / mean * 100.0

    @property
    def usable(self) -> tuple[bool, str]:
        """이 측정을 믿어도 되는가. **못 믿을 때 조용히 값을 내놓지 않는다.**

        포화되거나 얼룩진 카드로 계산한 노출은 다음 주에 재현되지 않는다 —
        그게 이 기능의 존재 이유인데 그걸 스스로 깨는 셈이다.
        """
        if self.pixels < 100:
            return False, "카드 영역이 너무 작습니다"
        if self.clipped_pct > _MAX_CLIPPED_PCT:
            return False, (f"카드의 {self.clipped_pct:.0f}% 가 포화됐습니다 — "
                           "노출을 낮추거나 조명을 줄이세요")
        if self.spread_pct > _MAX_SPREAD_PCT:
            return False, (f"카드 밝기가 고르지 않습니다({self.spread_pct:.0f}%) — "
                           "그림자나 반사가 걸렸는지 보세요")
        return True, "OK"

    def verdict(self, target: float = TARGET_LUMA) -> tuple[bool, str]:
        """맞았나. 아니면 **무엇이 얼마나 틀렸는지** 말한다."""
        ok, why = self.usable
        if not ok:
            return False, why
        bad = []
        if abs(self.luma - target) > LUMA_TOLERANCE:
            bad.append(f"밝기 {self.luma:.0f} (목표 {target:.0f})")
        if self.neutral_error_pct > NEUTRAL_TOLERANCE_PCT:
            bad.append(f"색 치우침 {self.neutral_error_pct:.1f}%")
        return (False, " · ".join(bad)) if bad else (True, "맞음")

    def to_dict(self) -> dict:
        ok, why = self.usable
        return {"b": round(self.b, 1), "g": round(self.g, 1), "r": round(self.r, 1),
                "luma": round(self.luma, 1),
                "neutral_error_pct": round(self.neutral_error_pct, 2),
                "clipped_pct": round(self.clipped_pct, 1),
                "spread_pct": round(self.spread_pct, 1),
                "pixels": self.pixels, "usable": ok, "why": why}


def center_roi(shape, frac: float = 0.3) -> tuple[int, int, int, int]:
    """화면 가운데 상자 `(x, y, w, h)`.

    카드를 여기 채우게 하는 것이 ROI 를 그리게 하는 것보다 낫다 — 손이 하나 덜
    가고, 가운데는 렌즈 주변부 광량 저하(비네팅)가 가장 적은 곳이다.
    """
    h, w = shape[:2]
    bw, bh = int(w * frac), int(h * frac)
    return (w - bw) // 2, (h - bh) // 2, bw, bh


def measure(bgr: np.ndarray, roi: tuple[int, int, int, int] | None = None
            ) -> GrayCardReading:
    """카드 영역을 읽는다. `roi` 가 없으면 가운데 상자."""
    if bgr.ndim != 3 or bgr.shape[2] != 3:
        raise ValueError(f"BGR 3채널이 아닙니다: {bgr.shape}")
    x, y, w, h = roi if roi else center_roi(bgr.shape)
    patch = bgr[max(0, y):y + h, max(0, x):x + w].reshape(-1, 3).astype(np.float32)
    if patch.size == 0:
        raise ValueError("ROI 가 프레임 밖입니다")

    b, g, r = (float(patch[:, i].mean()) for i in range(3))
    # 회색 카드라 채널이 같다는 전제 — 굳이 가중 휘도를 쓰면 색 치우침이
    # 밝기 오차로 새어 들어온다. 여기서는 둘을 **따로** 보고 싶다.
    lum = patch.mean(axis=1)
    clipped = float(((lum <= _CLIP_LOW) | (lum >= _CLIP_HIGH)).mean() * 100.0)
    mean = float(lum.mean())
    spread = float(lum.std() / mean * 100.0) if mean > 0 else 100.0
    return GrayCardReading(b=b, g=g, r=r, luma=mean, clipped_pct=clipped,
                           spread_pct=spread, pixels=int(patch.shape[0]))


def exposure_for(reading: GrayCardReading, current_us: float,
                 lo: float, hi: float, target: float = TARGET_LUMA) -> float:
    """카드를 목표 밝기로 만들 노출(µs).

    밝기는 노출에 **거의 비례**하므로 한 번의 비례 보정으로 대부분 잡힌다.
    반복이 필요하면 호출자가 다시 재고 다시 부르면 된다 — 여기는 순수 함수다.

    ⚠ 어두운 쪽 배수를 제한한다. 카드가 거의 검게 나온 상태(조명이 꺼졌거나
    렌즈가 막힌 경우)에서 비례 보정을 그대로 믿으면 노출을 최대까지 밀어붙여
    **다음 프레임이 새하얗게** 된다.
    """
    if current_us <= 0:
        raise ValueError(f"현재 노출이 양수여야 합니다: {current_us}")
    if reading.luma <= 0:
        return min(max(current_us * 2.0, lo), hi)
    ratio = min(max(target / reading.luma, 0.25), 4.0)
    return min(max(current_us * ratio, lo), hi)
