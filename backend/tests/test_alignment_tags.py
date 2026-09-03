"""AprilTag 로 재는 정렬 검사 — 검출과 자세 계산 (feature/alignment-check.md).

⚠ **팔을 안 움직이고 계산이 맞는지 확인할 수 있어야 한다.** 그래서 검출·자세는
순수 함수로 두고, 여기서는 **알고 있는 거리**에 태그를 그려 넣어 그 거리가
되돌아오는지 본다. 실기에서 처음 재는 값이 틀렸는지 맞았는지는 알 수가 없다.
"""

import math

import cv2
import numpy as np
import pytest

from piper_cam.tags import (DEFAULT_FAMILY, Intrinsics, TagPose, detect,
                            deviation, families)

INTR = Intrinsics(fx=600.0, fy=600.0, cx=320.0, cy=240.0)
TAG_MM = 40.0
SIZE = (480, 640)


def render(tag_id: int = 7, z_mm: float = 300.0, x_mm: float = 0.0,
           y_mm: float = 0.0, family: str = DEFAULT_FAMILY) -> np.ndarray:
    """카메라 앞 `z_mm` 에 정면으로 놓인 태그를 그린다 (핀홀 투영)."""
    d = cv2.aruco.getPredefinedDictionary(
        getattr(cv2.aruco, {"36h11": "DICT_APRILTAG_36h11",
                            "25h9": "DICT_APRILTAG_25h9"}[family]))
    # ⚠ **흰 여백(quiet zone)이 있어야 검출된다.** 태그 무늬만 그리면 0개가
    #   나온다 — 실물 태그를 인쇄할 때 여백을 남기는 것과 같은 이유다.
    px, pad = 400, 60
    marker = cv2.copyMakeBorder(cv2.aruco.generateImageMarker(d, tag_id, px),
                                pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    h = TAG_MM / 2.0
    # tags.detect 와 **같은 축**이어야 한다 (y 아래가 양수) — 어긋나면 태그가
    # 상하로 뒤집혀 그려지고, 뒤집힌 AprilTag 는 디코딩되지 않는다.
    obj = np.array([[-h, -h, 0.0], [h, -h, 0.0], [h, h, 0.0], [-h, h, 0.0]])
    obj = obj + np.array([x_mm, y_mm, z_mm])
    uv = (obj @ INTR.matrix().T)
    uv = (uv[:, :2] / uv[:, 2:3]).astype(np.float32)
    # 원본에서 **태그 자체**의 네 귀퉁이 (여백은 뺀다)
    src = np.array([[pad, pad], [pad + px, pad],
                    [pad + px, pad + px], [pad, pad + px]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(src, uv)
    frame = np.full((*SIZE, 3), 255, np.uint8)
    cv2.warpPerspective(cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR), m,
                        (SIZE[1], SIZE[0]), dst=frame,
                        borderMode=cv2.BORDER_TRANSPARENT)
    return frame


def test_a_tag_at_a_known_distance_reads_that_distance():
    """계산이 맞는지 확인할 유일한 방법 — 답을 아는 장면을 넣어 보는 것."""
    found = detect(render(z_mm=300.0), INTR, TAG_MM)
    assert len(found) == 1, found
    p = found[0]
    assert p.tag_id == 7
    # ⚠ 여유가 1.5% 인 이유는 솔버가 아니라 **합성 영상**이다. 코너가 정수
    #   픽셀로 떨어져 80px 짜리 태그가 79px 로 그려지고(1.25%), 작게 보이는
    #   만큼 멀게 읽힌다. 실물에서는 서브픽셀 보정이 이 몫을 줄인다.
    assert abs(p.z_mm - 300.0) < 300.0 * 0.015, p
    assert abs(p.x_mm) < 2.0 and abs(p.y_mm) < 2.0, p


def test_moving_the_tag_moves_the_reading_the_same_way():
    p = detect(render(z_mm=300.0, x_mm=25.0), INTR, TAG_MM)[0]
    assert abs(p.x_mm - 25.0) < 2.0, p


def test_nothing_found_is_an_empty_list_not_an_error():
    """⚠ 태그가 안 보이는 건 흔한 일이다(가려짐·조명). 예외로 만들면 호출부가
    정상 흐름을 예외로 다루게 된다."""
    assert detect(np.full((*SIZE, 3), 128, np.uint8), INTR, TAG_MM) == []


def test_the_size_and_family_must_be_real():
    """⚠ 크기를 지어내면 mm 단위 답이 **그럴듯한 모양으로** 틀린다."""
    with pytest.raises(ValueError):
        detect(render(), INTR, 0.0)
    with pytest.raises(ValueError):
        detect(render(), INTR, TAG_MM, family="없는계열")
    assert DEFAULT_FAMILY in families()


# ── 기준 대비 편차 ──────────────────────────────────────────────────────────

def test_no_movement_reads_as_no_deviation():
    base = detect(render(z_mm=300.0), INTR, TAG_MM)[0]
    d = deviation(base, base)
    assert d["dist_mm"] == 0.0 and d["rot_deg"] == 0.0, d


def test_a_known_shift_comes_back_as_that_shift():
    base = detect(render(z_mm=300.0), INTR, TAG_MM)[0]
    now = detect(render(z_mm=300.0, x_mm=10.0), INTR, TAG_MM)[0]
    d = deviation(base, now)
    assert abs(d["dx_mm"] - 10.0) < 1.5, d
    assert abs(d["dist_mm"] - 10.0) < 1.5, d


def test_rotation_is_a_relative_rotation_not_a_vector_subtraction():
    """⚠ 로드리게스 벡터를 그냥 빼면 회전이 클수록 틀린다 — 회전은 벡터 공간이
    아니다. 축이 다른 두 90° 회전의 상대각은 120° 지 벡터 차의 크기가 아니다."""
    # 축이 수직인 두 180° 회전. 합성은 나머지 축의 180° 회전이다.
    a = TagPose(0, 0, 0, 300, (math.pi, 0.0, 0.0))
    b = TagPose(0, 0, 0, 300, (0.0, math.pi, 0.0))
    got = deviation(a, b)["rot_deg"]
    assert abs(got - 180.0) < 0.5, got
    naive = math.degrees(math.hypot(math.pi, math.pi))     # 254° — 있을 수 없는 각
    assert naive - got > 70, "벡터 뺄셈과 구분이 안 된다 — 검사가 무의미하다"


# ── 왜곡 ────────────────────────────────────────────────────────────────────

D405 = Intrinsics(fx=432.86, fy=432.20, cx=421.46, cy=240.76,
                  coeffs=(-0.054861, 0.061178, -0.000609, 0.00042, -0.02014),
                  model="distortion.inverse_brown_conrady")


def test_distortion_is_not_assumed_to_be_zero():
    """⚠ "RealSense 는 보정된 프레임을 준다" 가 참이 아니었다. D405 컬러의 실측
    계수는 k1=-0.055 다 — 화면 가장자리에서 1% 넘게 휘고, 300mm 거리에서 수 mm 다.
    mm 를 재겠다는 도구가 무시할 크기가 아니다."""
    from piper_cam.tags import undistort

    corner = np.array([[60.0, 60.0]])          # 화면 구석
    moved = undistort(corner, D405)[0]
    shift = math.hypot(*(moved - corner[0]))
    assert shift > 3.0, f"구석에서 보정이 사실상 없다: {shift:.2f}px"

    center = np.array([[D405.cx, D405.cy]])    # 광축에서는 왜곡이 없다
    assert np.allclose(undistort(center, D405), center, atol=1e-6)


def test_no_coefficients_means_no_change():
    """⚠ 계수를 모르는 카메라에 보정을 지어내면 안 된다 — 그대로 둔다."""
    from piper_cam.tags import undistort

    pts = np.array([[10.0, 20.0], [300.0, 400.0]])
    assert np.allclose(undistort(pts, INTR), pts)


def test_the_inverse_model_is_not_fed_to_opencv_as_forward_coefficients():
    """⚠ `inverse_brown_conrady` 는 **왜곡을 푸는 방향**의 계수라, OpenCV 의
    정방향 `distCoeffs` 자리에 그냥 넣으면 부호가 반대로 먹는다. 두 방향을
    구분하지 않으면 보정이 오차를 **두 배로** 만든다."""
    from piper_cam.tags import undistort

    corner = np.array([[60.0, 60.0]])
    inverse = undistort(corner, D405)[0]
    forward = undistort(corner, Intrinsics(D405.fx, D405.fy, D405.cx, D405.cy,
                                           D405.coeffs, "distortion.brown_conrady"))[0]
    assert math.hypot(*(inverse - forward)) > 1.0, "두 모델을 같게 다룬다"
    # 정방향으로 풀면 반대쪽으로 간다 — 중심에서 멀어지는 방향
    assert (np.linalg.norm(inverse - [D405.cx, D405.cy])
            != pytest.approx(np.linalg.norm(forward - [D405.cx, D405.cy]), abs=0.5))
