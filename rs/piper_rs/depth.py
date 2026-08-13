"""깊이맵 인코딩 — 미터법 depth → 정책이 먹을 수 있는 3채널 uint8.

refactor/camera-transport.md "깊이맵을 정책 입력으로 넣기" 의 구현.

## 왜 변환이 필요한가

LeRobot 데이터셋 계층은 미터법 depth 를 저장할 수 없다. 비디오로 굽히는 순간
uint8 3채널이 되므로, **어차피 변환된다** — 그렇다면 우리가 뜻을 아는 방식으로
변환해서 "또 하나의 카메라"로 넣는 편이 낫다.

## 지금까지 뭐가 틀렸나

프리뷰용 코드가 그대로 쓰이고 있었다:

    vis = cv2.convertScaleAbs(depth, alpha=0.03)     # 0~8.5m 를 0~255 로
    cv2.applyColorMap(vis, cv2.COLORMAP_JET)

셋 다 정책 입력으로는 나쁘다.

1. **범위가 하드코딩** — 작업 공간이 1m 안쪽이면 실제 쓰는 구간이 30단계밖에
   안 남는다. 해상도를 통째로 버린다.
2. **JET 은 단조롭지 않다** — 파랑→초록→빨강이 거리와 1:1 로 대응하지 않고,
   채널별로 오르내린다. 컨볼루션이 배우기에 최악의 인코딩이다.
3. **무효 픽셀(0)이 "가장 가까움"이 된다** — RealSense 는 못 읽은 픽셀을 0 으로
   준다. 그게 0m 로 해석되면 **눈앞에 벽이 있는 것처럼** 학습된다.

## 여기서 하는 것

    유효 픽셀:  near..far 를 0..254 로 **선형·단조** 매핑 (가까울수록 어둡다)
    무효 픽셀:  255 = "가장 멂" — 없는 것을 벽으로 보지 않게

3채널로 복제한다. 회색조면 채널 간 관계를 배울 게 없어 안전하고, 비디오 코덱도
그대로 다룬다. (컬러맵으로 정보 밀도를 올리는 안은 단조성을 잃어서 안 쓴다.)

## 파라미터는 rsd 가 소유한다

같은 픽셀값이 실행마다 다른 거리를 뜻하면 데이터셋이 조용히 오염된다.
`DepthEncoding` 을 rsd 가 들고, `info()` 로 내보내 데이터셋 메타에 남긴다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

# 무효 픽셀에 주는 값. **가장 멂**이다 — 없는 것을 벽으로 보면 위험한 쪽으로 틀린다.
INVALID = 255

# 유효 구간이 쓰는 최대값. 255 는 무효 전용이라 비워둔다.
VALID_MAX = 254


@dataclass(frozen=True)
class DepthEncoding:
    """깊이 인코딩 파라미터. **데이터셋 메타에 그대로 실린다.**

    ⚠ 이 값이 바뀌면 같은 픽셀값이 다른 거리를 뜻한다. 옛 데이터와 섞으면
    정책이 두 좌표계를 하나로 배운다 — 에러 없이 성능만 나빠지는 종류의 오염이다.
    """

    # 관심 구간(mm). 작업 공간에 맞춰 좁힐수록 해상도가 올라간다.
    # 기본값은 팔이 닿는 범위를 염두에 둔 것이지 장치 한계가 아니다.
    near_mm: int = 150
    far_mm: int = 1200
    # 회색조 단조 인코딩. 컬러맵은 단조성을 잃어 넣지 않는다.
    mode: str = "gray_linear"

    def to_dict(self) -> dict:
        return asdict(self)


def encode_depth(depth_mm: np.ndarray, enc: DepthEncoding) -> np.ndarray:
    """uint16 mm → (H, W, 3) uint8.

    **순수 함수다** — 하드웨어 없이 경계를 시험할 수 있어야 한다(안전 필터와 같은 이유).
    """
    if enc.far_mm <= enc.near_mm:
        raise ValueError(f"far_mm 이 near_mm 보다 커야 합니다: {enc}")

    d = depth_mm.astype(np.float32)
    invalid = depth_mm == 0            # RealSense 가 못 읽은 픽셀

    span = float(enc.far_mm - enc.near_mm)
    v = (d - enc.near_mm) / span * VALID_MAX
    # 구간 밖은 잘라 붙인다. 자르지 않으면 wrap 되어 **먼 것이 가까워 보인다.**
    np.clip(v, 0, VALID_MAX, out=v)

    out = v.astype(np.uint8)
    out[invalid] = INVALID
    # 3채널 복제 — 회색조면 채널 간 배울 관계가 없어 안전하고 코덱도 그대로 다룬다
    return np.repeat(out[:, :, None], 3, axis=2)
