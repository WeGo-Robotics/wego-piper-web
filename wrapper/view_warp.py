"""정책 카메라 화면 보정 — 카메라가 학습 녹화 때 자리에서 비켜났으면 정책이 받는 이미지를 학습 화면으로 편다
(feature/target-conditioning.md §5).

정책은 학습 녹화 때의 화면을 배웠다. 카메라가 그 뒤에 비켜나면(117 위 카메라: 45px 처짐) 정책은 처음 보는
화면을 받는다. 목표 좌표만 학습 좌표로 고쳐 보내면(apple-sort 0.1.9) 이미지와 목표가 서로 어긋난다 — 117 에서
판 맨 앞 줄 사과는 이미지 속 자리가 학습 때 사과가 한 번도 없던 띠라 팔이 출발하지 않았다.

변환은 재는 쪽(apple-sort — 판 AprilTag)이 버스 `view_warp` 로 보내고 여기서는 받은 대로 편다:

    {"stream": "top", "frame": [640, 480], "m": [[a, b, c], [d, e, f]]}    지금 화면 px → 학습 화면 px
    None                                                                    끈다 (원본 그대로)

- `stream` 은 카메라 스트림 이름 — 그 스트림을 읽는 정책 카메라(`--cameras` 의 `segment`)에 모두 건다.
- 프레임 크기가 `frame` 과 다르면 행렬을 그 크기로 옮겨 쓴다.
- 화면 밖에서 들어오는 띠(카메라가 아래로 처졌으면 아래쪽)는 가장자리 픽셀로 채운다 — 검정 띠보다 덜 낯설고,
  거울처럼 비추면 없는 사과가 생긴다.
- 배율·회전·이동이 지나치면 받지 않는다 — 재는 쪽이 틀린 것이다. backend `param_bridge.view_warp_value` 와
  같은 규칙이다 (테스트가 대조).

torch 를 import 하지 않는다 — 테스트가 그냥 돈다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

SCALE_RANGE = (0.8, 1.25)      # 축마다 배율
MAX_ROT_DEG = 10.0
MAX_SHIFT_FRAC = 0.5           # 화면 가운데의 이동 — 폭·높이의 절반까지
MAX_FRAME = 8192


def check_range(m: np.ndarray, w: int, h: int) -> None:
    """받을 수 있는 변환인가 — 아니면 ValueError."""
    (a, b, c), (d, e, f) = m
    if a * e - b * d <= 0:
        raise ValueError("뒤집힌 변환이다")
    sx, sy = math.hypot(a, d), math.hypot(b, e)
    lo, hi = SCALE_RANGE
    if not (lo <= sx <= hi and lo <= sy <= hi):
        raise ValueError(f"배율이 지나치다: ({sx:.3f}, {sy:.3f})")
    rot = math.degrees(math.atan2(d, a))
    if abs(rot) > MAX_ROT_DEG:
        raise ValueError(f"회전이 지나치다: {rot:.1f}°")
    # 원점 기준 이동(c, f)은 배율·회전이 섞여 크게 보인다 — 화면 가운데가 옮겨지는 만큼을 본다
    cx, cy = w / 2, h / 2
    dx, dy = a * cx + b * cy + c - cx, d * cx + e * cy + f - cy
    if abs(dx) > w * MAX_SHIFT_FRAC or abs(dy) > h * MAX_SHIFT_FRAC:
        raise ValueError(f"이동이 지나치다: ({dx:.0f}, {dy:.0f})px")


@dataclass(frozen=True)
class ViewWarp:
    stream: str
    frame: tuple[int, int]      # (w, h) — 행렬을 잰 화면 크기
    m: np.ndarray               # (2, 3) 지금 화면 px → 학습 화면 px

    def matrix_for(self, w: int, h: int) -> np.ndarray:
        """이 크기 화면에 쓸 행렬 — 잰 크기와 다르면 픽셀 눈금을 옮긴다."""
        fw, fh = self.frame
        if (w, h) == (fw, fh):
            return self.m
        s = np.diag([w / fw, h / fh, 1.0])
        return (s @ np.vstack([self.m, [0.0, 0.0, 1.0]]) @ np.linalg.inv(s))[:2]

    def apply(self, img: np.ndarray) -> np.ndarray:
        import cv2

        h, w = img.shape[:2]
        return cv2.warpAffine(img, self.matrix_for(w, h), (w, h), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)

    def summary(self) -> str:
        """`top: 화면 가운데가 학습 때보다 (+0, +41)px ×1.019 -0.0°` — apple-sort 로그와 같은 부호. apple-sort 는 판 표지
        자리에서 재므로 배율만큼 값이 다르다(117: 표지 +45, 가운데 +41)."""
        (a, b, c), (d, e, f) = self.m
        cx, cy = self.frame[0] / 2, self.frame[1] / 2
        dx, dy = cx - (a * cx + b * cy + c), cy - (d * cx + e * cy + f)
        scale = 1 / math.sqrt(a * e - b * d)
        rot = -math.degrees(math.atan2(d, a))
        return f"{self.stream}: 화면 가운데가 학습 때보다 ({dx:+.0f}, {dy:+.0f})px ×{scale:.3f} {rot:+.1f}°"


def parse_view_warp(value) -> ViewWarp | None:
    """버스 값 → ViewWarp. `None`·빈 값은 끔. 모양·범위가 틀리면 ValueError — 호출부가 끈다(원본 쪽으로 틀린다)."""
    if value is None or value == "" or value == {}:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"화면 보정은 {{stream, frame, m}} 이어야 한다: {value!r}")
    stream = value.get("stream")
    if not isinstance(stream, str) or not stream or len(stream) > 64:
        raise ValueError(f"스트림 이름이 이상하다: {stream!r}")
    frame = value.get("frame")
    if not isinstance(frame, (list, tuple)) or len(frame) != 2:
        raise ValueError(f"frame 은 [w, h] 여야 한다: {frame!r}")
    w, h = int(frame[0]), int(frame[1])
    if not (0 < w <= MAX_FRAME and 0 < h <= MAX_FRAME):
        raise ValueError(f"frame 크기가 이상하다: {frame!r}")
    m = np.asarray(value.get("m"), dtype=float)
    if m.shape != (2, 3) or not np.all(np.isfinite(m)):
        raise ValueError(f"m 은 유한한 2×3 행렬이어야 한다: {value.get('m')!r}")
    check_range(m, w, h)
    return ViewWarp(stream, (w, h), m)


class WarpSlot:
    """버스가 바꾸고 추론 스레드가 읽는 자리. 통째로 갈아끼우므로 잠금이 필요 없다."""

    def __init__(self) -> None:
        self.current: ViewWarp | None = None
        self._applied: ViewWarp | None = None     # 처음 편 것을 한 번만 알린다

    def update(self, value) -> ViewWarp | None:
        """버스 값으로 바꾼다. 틀리면 끄고 ValueError 를 다시 던진다 — 틀린 행렬로 펴느니 원본이 낫다."""
        try:
            self.current = parse_view_warp(value)
        except (TypeError, ValueError):
            self.current = None
            raise
        return self.current

    def apply(self, obs: dict, cameras: dict, prefix: str) -> tuple[list[str], ViewWarp | None]:
        """`obs` 의 이미지 중 보정 스트림을 읽는 카메라를 편다(제자리).

        (편 키, 이 보정을 처음 적용했으면 그 보정 — 알릴 것). 버스가 도중에 바꿔도 이 호출은 한 보정으로 끝난다."""
        warp = self.current
        if warp is None:
            return [], None
        keys = []
        for cam, cfg in cameras.items():
            if isinstance(cfg, dict) and cfg.get("segment") == warp.stream:
                img = obs.get(prefix + cam)
                if isinstance(img, np.ndarray) and img.ndim == 3:
                    obs[prefix + cam] = warp.apply(img)
                    keys.append(prefix + cam)
        fresh = warp if self._applied is not warp else None
        self._applied = warp
        return keys, fresh
