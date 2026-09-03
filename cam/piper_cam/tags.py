"""AprilTag 검출과 6D 자세 — **순수 함수** (feature/alignment-check.md).

하드웨어 없이 부를 수 있고 부작용이 없다. 그래야 합성 영상으로 검증할 수 있고,
정렬 검사의 계산이 맞는지를 팔을 움직이지 않고 확인할 수 있다.

⚠ **새 의존성이 없다.** `cv2.aruco` 에 AprilTag 사전이 들어 있다 —
`pupil-apriltags` 같은 패키지를 더 넣으면 배포 이미지가 커지고 라이선스도 는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# 기본 계열. 36h11 은 오검출률이 가장 낮아 로봇 계측의 표준이다 —
# 16h5 는 태그 수가 적어 잡음에 잘못 걸린다.
DEFAULT_FAMILY = "36h11"

_FAMILIES = {
    "36h11": "DICT_APRILTAG_36h11",
    "36h10": "DICT_APRILTAG_36h10",
    "25h9": "DICT_APRILTAG_25h9",
    "16h5": "DICT_APRILTAG_16h5",
}


@dataclass(frozen=True)
class Intrinsics:
    """카메라 내부 파라미터 (픽셀). RealSense 가 스스로 신고하는 값이다."""

    fx: float
    fy: float
    cx: float
    cy: float
    # ⚠ **0 이라고 가정하지 않는다.** 실측 D405 컬러: [-0.055, 0.061, -0.0006,
    #   0.0004, -0.020]. k1 이 -0.055 면 화면 가장자리에서 1% 넘게 휘고, 300mm
    #   거리에서 수 mm 다 — mm 를 재겠다는 도구가 무시할 크기가 아니다.
    coeffs: tuple[float, ...] = ()
    # RealSense 는 `inverse_brown_conrady` 를 쓴다 — 이름 그대로 **왜곡을 푸는**
    # 방향의 계수라, OpenCV 의 (정방향) distCoeffs 자리에 그대로 넣으면 안 된다.
    model: str = ""

    def matrix(self) -> np.ndarray:
        return np.array([[self.fx, 0.0, self.cx],
                         [0.0, self.fy, self.cy],
                         [0.0, 0.0, 1.0]], dtype=np.float64)


@dataclass(frozen=True)
class TagPose:
    """카메라 기준 태그 자세. 위치는 **mm**, 회전은 로드리게스 벡터(라디안)."""

    tag_id: int
    x_mm: float
    y_mm: float
    z_mm: float
    rvec: tuple[float, float, float]

    def to_dict(self) -> dict:
        return {"tag_id": self.tag_id, "x_mm": round(self.x_mm, 2),
                "y_mm": round(self.y_mm, 2), "z_mm": round(self.z_mm, 2),
                "rvec": [round(v, 5) for v in self.rvec]}

    @classmethod
    def from_dict(cls, d: dict) -> "TagPose":
        return cls(int(d["tag_id"]), float(d["x_mm"]), float(d["y_mm"]),
                   float(d["z_mm"]), tuple(float(v) for v in d["rvec"]))


def families() -> list[str]:
    return list(_FAMILIES)


def undistort(pixels: np.ndarray, intr: Intrinsics) -> np.ndarray:
    """왜곡된 화소 좌표 → 이상적 핀홀 좌표. 계수가 없으면 그대로 돌려준다.

    ⚠ RealSense 의 `inverse_brown_conrady` 는 **왜곡을 푸는 방향**의 계수다 —
    librealsense 의 deproject 가 이 다항식을 그대로 적용해 광선을 얻는다. 그래서
    OpenCV 의 `distCoeffs`(정방향, 이상→왜곡) 자리에 그냥 넣으면 부호가 반대로
    먹는다. 여기서 직접 풀고 solvePnP 에는 계수 없이 넘긴다.

    정방향(`brown_conrady`) 모델이면 반복으로 푼다 — 닫힌 해가 없다.
    """
    import cv2

    p = np.asarray(pixels, dtype=np.float64).reshape(-1, 2)
    if not intr.coeffs or not any(intr.coeffs):
        return p
    c = list(intr.coeffs) + [0.0] * (5 - len(intr.coeffs))
    k1, k2, p1, p2, k3 = c[:5]

    x = (p[:, 0] - intr.cx) / intr.fx
    y = (p[:, 1] - intr.cy) / intr.fy
    if "inverse" in intr.model:
        r2 = x * x + y * y
        f = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        ux = x * f + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        uy = y * f + 2.0 * p2 * x * y + p1 * (r2 + 2.0 * y * y)
    else:
        und = cv2.undistortPoints(p.reshape(-1, 1, 2), intr.matrix(),
                                  np.array(c[:5], dtype=np.float64))
        ux, uy = und.reshape(-1, 2)[:, 0], und.reshape(-1, 2)[:, 1]
    return np.stack([ux * intr.fx + intr.cx, uy * intr.fy + intr.cy], axis=1)


def detect(frame_bgr: np.ndarray, intr: Intrinsics, tag_mm: float,
           family: str = DEFAULT_FAMILY) -> list[TagPose]:
    """프레임에서 태그를 찾아 카메라 기준 자세를 낸다. 못 찾으면 빈 목록.

    ⚠ **내부 파라미터를 지어내지 않는다.** 초점거리를 추측하면 답이 mm 단위의
    그럴듯한 모양으로 나오면서 틀린다 — 틀린 줄도 모르게 된다. 없으면 호출부가
    거절해야 한다.
    """
    import cv2

    if tag_mm <= 0:
        raise ValueError(f"태그 크기가 양수여야 합니다: {tag_mm}")
    if family not in _FAMILIES:
        raise ValueError(f"모르는 태그 계열입니다: {family}")

    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, _FAMILIES[family]))
    params = cv2.aruco.DetectorParameters()
    # 코너를 서브픽셀로 다듬는다. mm 를 재는 일이라 코너 한 픽셀이 그대로 오차다.
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_APRILTAG
    corners, ids, _ = cv2.aruco.ArucoDetector(d, params).detectMarkers(
        cv2.cvtColor(frame_bgr[:, :, :3], cv2.COLOR_BGR2GRAY))
    if ids is None or len(ids) == 0:
        return []

    h = tag_mm / 2.0
    # 태그 평면 좌표 (mm). aruco 의 코너 순서와 같아야 한다: 좌상→우상→우하→좌하.
    #
    # ⚠ **y 는 아래가 양수다** — 카메라 축과 같게 맞춘 것이다. OpenCV 예제는
    #   y-up 을 쓰는데, 그러면 태그 좌표계가 영상과 상하로 뒤집혀 화면에서 위로
    #   옮긴 태그가 ΔY 음수로 나온다. 사람이 화면을 보며 읽는 값이라 축이 화면과
    #   같아야 한다. (합성 영상 테스트에서 이 불일치가 그대로 드러났다 —
    #   뒤집힌 태그는 아예 디코딩되지 않는다.)
    obj = np.array([[-h, -h, 0.0], [h, -h, 0.0], [h, h, 0.0], [-h, h, 0.0]],
                   dtype=np.float64)
    k = intr.matrix()
    zero = np.zeros((5, 1))

    out: list[TagPose] = []
    for c, i in zip(corners, ids.flatten()):
        pts = undistort(c.reshape(4, 2).astype(np.float64), intr)
        # ⚠ `SOLVEPNP_IPPE_SQUARE` 를 안 쓴다. 그 플래그는 물체점 순서를 **y-up 으로
        #   강제**하는데(어기면 조용히 0 을 돌려준다 — 실제로 그랬다), 그러면 태그
        #   좌표계가 영상과 상하로 뒤집힌다. `IPPE` 는 평면 대상 일반이라 순서를
        #   강요하지 않고, 합성 영상에서 두 방법의 답이 같은 것을 확인했다.
        ok, rvec, tvec = cv2.solvePnP(obj, pts, k, zero,
                                      flags=cv2.SOLVEPNP_IPPE)
        if not ok:
            continue
        t = tvec.flatten()
        out.append(TagPose(int(i), float(t[0]), float(t[1]), float(t[2]),
                           tuple(float(v) for v in rvec.flatten())))
    return sorted(out, key=lambda p: p.tag_id)


def deviation(baseline: TagPose, now: TagPose) -> dict:
    """기준 대비 얼마나 틀어졌나. 위치는 mm, 회전은 도.

    ⚠ 회전 차이는 **두 회전의 상대 회전**이어야 한다. 로드리게스 벡터를 그냥
    빼면 회전이 클수록 틀린 값이 나온다 — 회전은 벡터 공간이 아니다.
    """
    import cv2

    dx, dy, dz = now.x_mm - baseline.x_mm, now.y_mm - baseline.y_mm, now.z_mm - baseline.z_mm
    r0, _ = cv2.Rodrigues(np.array(baseline.rvec, dtype=np.float64))
    r1, _ = cv2.Rodrigues(np.array(now.rvec, dtype=np.float64))
    rel, _ = cv2.Rodrigues(r1 @ r0.T)
    angle = float(np.linalg.norm(rel))
    return {
        "dx_mm": round(dx, 2), "dy_mm": round(dy, 2), "dz_mm": round(dz, 2),
        "dist_mm": round(math.sqrt(dx * dx + dy * dy + dz * dz), 2),
        "rot_deg": round(math.degrees(angle), 3),
    }
