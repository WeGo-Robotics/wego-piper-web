"""`--dataset.vcodec=auto` 가 **열리는** 인코더만 고르게 한다.

## 왜 필요한가

LeRobot 의 `auto` 는 `av.codec.Codec(name, "w")` 가 성공하면 "쓸 수 있다"고 본다. 그건
**FFmpeg 빌드에 그 코덱이 들어 있다**는 뜻이지 장치가 있다는 뜻이 아니다. PyAV wheel 은
NVENC 를 품고 있어서, GPU 가 없는 기계에서도 `auto → h264_nvenc` 가 되고 첫 프레임을 쓰는
순간 `PermissionError: avcodec_open2(h264_nvenc)` 로 녹화가 죽는다 (GPU 없는 컨테이너에서
재현: 2026-10-10, `available HW encoders: ['h264_nvenc', 'hevc_nvenc']`).

## 하는 일

후보를 **실제로 한 번 열어 본다.** 열리는 것만 "있다"고 하면 `auto` 는 LeRobot 의 원래 순서
그대로 열리는 첫 하드웨어 인코더를 고르고, 하나도 없으면 LeRobot 이 소프트웨어(libsvtav1)로
떨어진다. 나중에 PyAV 가 QSV·VAAPI 를 품게 되면 **여기를 안 고쳐도** 열리는 기계에서 저절로
선택된다.

사용자가 `h264_nvenc` 를 **명시한** 경우는 건드리지 않는다 — 명시한 것은 그대로 시도한다.
"""

import functools


def _frame_open_ok(name: str, options: dict) -> bool:
    """`name` 인코더로 작은 프레임 하나를 실제로 인코딩해 본다."""
    from fractions import Fraction

    import av
    import numpy as np

    try:
        # 컨테이너 없이 코덱만 연다 — 실패하는 자리가 `avcodec_open2` 라 거기까지면 충분하다.
        # 일부 하드웨어 인코더는 너무 작은 프레임을 거절하므로 넉넉한 크기로 연다.
        ctx = av.CodecContext.create(name, "w")
        ctx.width, ctx.height, ctx.pix_fmt = 320, 240, "yuv420p"
        ctx.time_base = Fraction(1, 30)
        ctx.framerate = Fraction(30, 1)
        ctx.options = {k: str(v) for k, v in options.items()}
        ctx.open()
        frame = av.VideoFrame.from_ndarray(np.zeros((240, 320, 3), np.uint8), format="rgb24")
        frame.pts = 0
        ctx.encode(frame)
        ctx.encode(None)
        return True
    except Exception:
        return False


def usable(candidates, listed, can_open) -> list[str]:
    """`listed` 에 있는 후보 중 `can_open(name)` 이 참인 것을 **후보 순서대로** 돌려준다."""
    return [name for name in candidates if name in listed and can_open(name)]


def install() -> None:
    """`lerobot.datasets.video_utils.detect_available_hw_encoders` 를 열어 보는 판정으로 바꾼다.

    `resolve_vcodec` 는 이 함수를 모듈 전역으로 찾으므로 속성만 바꾸면 된다. 실패해도 녹화를
    막지 않는다 — 원래 판정이 그대로 남는다.
    """
    from lerobot.datasets import video_utils as vu

    original = vu.detect_available_hw_encoders

    @functools.lru_cache(maxsize=1)
    def _detect() -> tuple[str, ...]:
        listed = set(original())
        picked = usable(
            vu.HW_ENCODERS, listed,
            lambda name: _frame_open_ok(name, _options_for(vu, name)),
        )
        skipped = [n for n in vu.HW_ENCODERS if n in listed and n not in picked]
        if skipped:
            print(f"[start_record] vcodec auto: {skipped} 는 코덱은 있으나 열리지 않아 건너뜁니다 "
                  "(이 기계에 해당 하드웨어가 없습니다)", flush=True)
        return tuple(picked)

    vu.detect_available_hw_encoders = lambda: list(_detect())


def _options_for(vu, name: str) -> dict:
    """LeRobot 이 실제로 줄 옵션과 같게 연다 — 다르면 판정과 실제가 갈린다."""
    try:
        return vu._get_codec_options(name)
    except Exception:
        return {}
