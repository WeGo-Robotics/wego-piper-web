"""rsd 색 채널 순서.

노란 물체가 파랗게 나왔다. 스트림은 `bgr8` 로 열리는데 읽는 쪽은 주석에
`# rgb8` 이라고 적힌 채 `COLOR_RGB2BGR` 을 돌리고 있었다 — R 과 B 가 뒤집혔다.

두 코드가 멀리 떨어져 있어서 생겼다: 프로파일 요청이 데몬까지 닿게 고치면서
`enable_stream` 에 `rs.format.bgr8` 이 붙었고, 읽는 쪽은 그대로였다.
**해상도를 지정한 카메라만** 그 갈래를 타서 더 늦게 드러났다.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rs"))

from piper_rs.hub import to_bgr  # noqa: E402

# 노란색 — 이 버그가 실제로 드러난 색이다. BGR 로 [0, 255, 255].
YELLOW_BGR = np.full((2, 2, 3), (0, 255, 255), dtype=np.uint8)
YELLOW_RGB = np.full((2, 2, 3), (255, 255, 0), dtype=np.uint8)


def test_a_bgr_frame_is_left_alone():
    """⚠ **회귀** — 여기서 한 번 더 뒤집어서 노란색이 파랗게 나왔다."""
    assert np.array_equal(to_bgr(YELLOW_BGR, "format.bgr8"), YELLOW_BGR)


def test_an_rgb_frame_is_converted():
    """기본 프로파일(포맷 미지정) 갈래는 여전히 rgb8 로 온다 — 그쪽은 바꿔야 한다."""
    assert np.array_equal(to_bgr(YELLOW_RGB, "format.rgb8"), YELLOW_BGR)


@pytest.mark.parametrize("fmt", ["format.rgb8", "format.bgr8"])
def test_yellow_survives_either_format(fmt):
    """어느 갈래로 열리든 **결과가 같아야** 한다. 그게 이 함수의 존재 이유다."""
    src = YELLOW_RGB if fmt.endswith("rgb8") else YELLOW_BGR
    out = to_bgr(src, fmt)
    b, g, r = out[0, 0]
    assert (b, g, r) == (0, 255, 255), f"{fmt}: 노란색이 아니다 (B={b} G={g} R={r})"


def test_an_unknown_format_is_not_guessed_at():
    """모르는 포맷을 아무렇게나 바꾸면 원인이 한 겹 더 멀어진다.

    그대로 두면 화면이 이상한 채로라도 **어디서 틀렸는지**가 남는다.
    """
    odd = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
    assert np.array_equal(to_bgr(odd, "format.yuyv"), odd)


def test_the_reader_does_not_hardcode_a_format():
    """가정으로 되돌아가면 같은 버그가 그대로 돌아온다."""
    src = (Path(__file__).resolve().parents[2] / "rs" / "piper_rs" / "hub.py").read_text()
    # 색 프레임을 읽는 자리만 본다 — `to_bgr` 자신은 당연히 변환을 들고 있다
    reader = src.split("cf = frames.get_color_frame()", 1)[1].split("if \"depth\"", 1)[0]
    assert "COLOR_RGB2BGR" not in reader, "읽는 쪽이 다시 포맷을 단정한다"
    assert "cf.get_profile().format()" in reader, "프레임 포맷을 안 본다"
    assert "to_bgr(" in reader, "공용 변환을 안 쓴다"
