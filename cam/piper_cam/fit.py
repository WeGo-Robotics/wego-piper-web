"""캡처 프레임 → 출력 크기. camerad·rsd 가 **같은 계산**을 쓴다.

출력 해상도를 캡처와 따로 두는 이유는 화각이다 — 많은 UVC 카메라가 저해상도를 센서
가운데를 **잘라서** 만든다(AR0234 실측 2026-10-06: 640×480 이 가운데 1/3). 넓은 모드로
받아 여기서 줄이면 데이터셋 크기는 같고 화각은 넓다.

규칙 하나: **늘이지 않는다.** 비율이 다르면 가운데를 잘라 비율부터 맞춘 뒤 줄인다.
늘이면 물체가 찌그러지고, 정책은 그 찌그러짐을 그대로 배운다.

⚠ 크기를 바꾸면 **카메라 내부 파라미터도 바뀐다.** 정렬 검사가 초점거리·주점으로 mm 를
계산하므로, 프레임만 줄이고 intrinsics 를 그대로 두면 답이 조용히 틀린다 —
`fit_intrinsics` 가 프레임과 같은 자르기·축소를 적용한다.
"""

from __future__ import annotations


def fit_box(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int, int, int, float]:
    """`(x0, y0, crop_w, crop_h, scale)` — 원본에서 잘라낼 상자와 그 뒤의 배율."""
    want = dst_w / dst_h
    if src_w / src_h > want:                 # 옆이 남는다 → 좌우를 자른다
        cw, ch = round(src_h * want), src_h
    elif src_w / src_h < want:               # 위아래가 남는다
        cw, ch = src_w, round(src_w / want)
    else:
        cw, ch = src_w, src_h
    return (src_w - cw) // 2, (src_h - ch) // 2, cw, ch, dst_w / cw


def fit_frame(frame, dst: tuple[int, int] | None, nearest: bool = False):
    """프레임을 출력 크기로. `dst` 가 None 이거나 이미 그 크기면 **그대로** 돌려준다.

    `nearest=True` 는 깊이용이다 — 면적 평균은 물체 경계에서 앞뒤 거리를 섞어
    **존재하지 않는 거리**를 만든다(인코딩된 깊이도 마찬가지로 값이 뜻을 잃는다).
    """
    if not dst:
        return frame
    dw, dh = int(dst[0]), int(dst[1])
    fh, fw = frame.shape[:2]
    if (fw, fh) == (dw, dh):
        return frame
    import cv2

    x0, y0, cw, ch, _ = fit_box(fw, fh, dw, dh)
    if (cw, ch) != (fw, fh):
        frame = frame[y0:y0 + ch, x0:x0 + cw]
    interp = cv2.INTER_NEAREST if nearest else (
        cv2.INTER_AREA if dw <= cw else cv2.INTER_LINEAR)
    return cv2.resize(frame, (dw, dh), interpolation=interp)


def fit_intrinsics(intr: dict | None, dst: tuple[int, int] | None) -> dict | None:
    """intrinsics 에 프레임과 **같은** 자르기·축소를 적용한다. 왜곡 계수는 그대로다
    (정규화 좌표에 걸리는 값이라 크기와 무관하다)."""
    if not intr or not dst:
        return intr
    sw, sh = int(intr["width"]), int(intr["height"])
    dw, dh = int(dst[0]), int(dst[1])
    if (sw, sh) == (dw, dh):
        return intr
    x0, y0, _, _, s = fit_box(sw, sh, dw, dh)
    return {**intr, "fx": intr["fx"] * s, "fy": intr["fy"] * s,
            "cx": (intr["cx"] - x0) * s, "cy": (intr["cy"] - y0) * s,
            "width": dw, "height": dh}
