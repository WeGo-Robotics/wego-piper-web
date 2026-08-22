"""깊이로 배경을 지운다 — **컬러 프레임에서** (feature/depth-background-mask.md).

## 무엇을 하나

깊이가 `far_mm` 보다 먼 픽셀을 컬러에서 지운다. 정책이 배경을 안 보게 하는 것이
목적이다 — 작업대를 옮기거나 뒤에 사람이 지나가도 같은 관측이 되게.

## 왜 far 만 보나 (near 는 안 자른다)

**배경은 뒤에 있는 것이다.** 카메라에 아주 가까운 것을 지우면 집으려는 물체가
손 바로 앞에 왔을 때 사라진다 — 정확히 필요한 순간에 안 보이게 된다.
`near_mm` 은 깊이 인코딩의 해상도 창일 뿐이고 여기서는 안 쓴다.

## 왜 무효 픽셀을 살리나

⚠ **깊이가 없다고 물체가 없는 게 아니다.** D405 는 실측에서 프레임의 **42%** 를
못 읽었다(범위 밖·무늬 없는 면·반사). 그걸 배경으로 치면 물체 한가운데가
숭숭 뚫린다. 모르면 남기는 쪽이 덜 위험하다 — 배경이 덜 잘릴 뿐이지만,
반대로 틀리면 정책이 봐야 할 것을 지운다.

깊이의 `INVALID` 규칙(`depth.py`)과 방향이 같다: 모르는 것을 **안전한 쪽**으로 민다.
거기서는 "가장 멂"이 안전이고, 여기서는 "남긴다"가 안전이다.

## 정렬이 전제다

깊이와 컬러는 다른 센서라 그대로 겹치지 않는다. 호출자가 **컬러에 정렬된**
깊이를 넘겨야 한다 (`rs.align(rs.stream.color)`). 모양이 다르면 마스킹하지 않는다 —
어긋난 마스크는 안 하느니만 못하다.
"""

from __future__ import annotations

import numpy as np

# 지운 자리에 넣는 색(BGR). 검정은 "여기 아무것도 없다"로 읽히고, 코덱이
# 평탄한 영역을 싸게 압축한다.
FILL_BGR = (0, 0, 0)


def background_mask(depth_raw: np.ndarray, far_mm: float, units_m: float) -> np.ndarray:
    """지울 픽셀이 True. **무효(raw 0)는 False** — 모르면 남긴다.

    순수 함수다. 하드웨어 없이 경계를 시험할 수 있어야 한다.
    """
    if far_mm <= 0:
        raise ValueError(f"far_mm 이 양수여야 합니다: {far_mm}")
    if not units_m or units_m <= 0:
        raise ValueError(f"depth_units 가 양수여야 합니다: {units_m}")

    # 비교를 raw 쪽에서 한다 — 프레임을 mm 로 바꾸면 배열을 하나 더 만든다
    # (`encode_depth` 와 같은 이유).
    far_raw = far_mm / (units_m * 1000.0)
    return (depth_raw > 0) & (depth_raw > far_raw)


def apply_mask(color_bgr: np.ndarray, depth_raw: np.ndarray,
               far_mm: float, units_m: float,
               fill: tuple[int, int, int] = FILL_BGR) -> np.ndarray:
    """컬러에서 배경을 지운다. **깊이는 컬러에 정렬돼 있어야 한다.**

    모양이 안 맞으면 원본을 그대로 돌려준다 — 어긋난 마스크보다 낫고,
    호출자가 정렬을 빠뜨린 것을 조용히 덮지 않도록 판단은 호출자에게 남긴다
    (`shapes_match` 로 미리 물어볼 수 있다).
    """
    if not shapes_match(color_bgr, depth_raw):
        return color_bgr
    out = color_bgr.copy()
    out[background_mask(depth_raw, far_mm, units_m)] = fill
    return out


def shapes_match(color_bgr: np.ndarray, depth_raw: np.ndarray) -> bool:
    """마스크를 씌워도 되는가 — 두 프레임의 픽셀이 같은 곳을 가리키는가."""
    return color_bgr.ndim == 3 and depth_raw.ndim == 2 \
        and color_bgr.shape[:2] == depth_raw.shape
