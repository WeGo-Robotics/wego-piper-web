"""에피소드 삭제가 남은 영상을 **다시 압축하지 않게** — 패킷을 그대로 옮긴다 (remux).

## 왜

v3 는 카메라마다 에피소드 여럿을 mp4 한 파일에 담는다. LeRobot 의 `delete_episodes` 는 그
파일에서 지운 것을 빼려고 **남은 전부를 디코딩해 libsvtav1 로 재인코딩**한다
(`dataset_tools._keep_episodes_from_video_with_av`). 실측(2026-10-29, 120개·23,689프레임·카메라 3대):

- 85초·CPU 10분 — 하나를 지워도 데이터셋 크기에 비례한다
- 녹화한 h264(NVENC)가 **AV1 로 바뀐다** — 코덱이 조용히 바뀐다
- 지울 때마다 남은 에피소드가 **한 번 더 손실 압축**된다 — 세대 손실이 쌓인다

## 어떻게

녹화는 에피소드마다 따로 인코딩해 이어 붙이므로 **에피소드의 첫 프레임은 늘 키프레임**이다
(실측 121/121). 그러면 남길 에피소드의 패킷을 그대로 새 파일에 옮기고 타임스탬프만 당기면 된다.
⚠ NVENC 는 B 프레임을 쓴다 — 디코드 순서와 표시 순서가 다르다. 그래서 프레임 번호는 표시
순서(pts)로 매기고(LeRobot 도 디코딩된 순서로 센다), 에피소드 안의 패킷은 pts·dts 를 **같은
양만큼** 민다.

조건이 하나라도 안 맞으면 `False` — 부르는 쪽이 LeRobot 의 재인코딩으로 넘어간다. 틀린 영상을
만드는 것보다 느린 것이 낫다.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _plan(packets: list[tuple[int, int, bool]], ranges: list[tuple[int, int]],
          step: int) -> dict[int, int] | None:
    """디코드 순서의 패킷 `(pts, dts, keyframe)` → `{패킷 위치: 밀 양}`. 못 하면 `None`."""
    first = min(p[0] for p in packets)
    idx = []
    for pts, _, _ in packets:
        q, r = divmod(pts - first, step)
        if r:
            return None                         # 고정 프레임레이트가 아니다
        idx.append(q)
    n = len(packets)
    if sorted(idx) != list(range(n)):
        return None                             # 빠지거나 겹친 프레임이 있다
    keyframes = {i for i, (_, _, key) in zip(idx, packets) if key}

    shifts: dict[int, int] = {}
    out_start = 0
    for start, end in ranges:
        if not (0 <= start < end <= n) or start not in keyframes:
            return None
        pos = [k for k, i in enumerate(idx) if start <= i < end]
        # 에피소드는 따로 인코딩돼 닫힌 GOP 다 — 디코드 순서에서도 한 덩어리여야 한다
        if pos != list(range(pos[0], pos[0] + len(pos))):
            return None
        shift = (out_start - start) * step - first
        for k in pos:
            shifts[k] = shift
        out_start += end - start

    # mp4 는 dts 가 엄격히 늘어야 한다 — 이어 붙인 자리에서 어긋나면 그만둔다
    last = None
    for k in sorted(shifts):
        dts = packets[k][1] + shifts[k]
        if last is not None and dts <= last:
            return None
        last = dts
    return shifts


def keep_episodes(input_path, output_path, episodes_to_keep: list[tuple[int, int]],
                  fps: float) -> bool:
    """`episodes_to_keep`(반열린 프레임 구간)만 남긴 영상을 재인코딩 없이 쓴다. 못 하면 `False`."""
    import av

    ranges = sorted(episodes_to_keep)
    out_path = Path(output_path)
    try:
        with av.open(str(input_path)) as inp:
            if len(inp.streams) != 1 or not inp.streams.video:
                return False
            ist = inp.streams.video[0]
            ticks = 1 / (fps * ist.time_base)
            if ticks.denominator != 1:
                return False
            packets = [(p.pts, p.dts, p.is_keyframe) for p in inp.demux(ist)
                       if p.pts is not None and p.dts is not None]
        if not packets:
            return False
        shifts = _plan(packets, ranges, int(ticks))
        if shifts is None:
            return False

        written = 0
        with av.open(str(input_path)) as inp, av.open(str(out_path), "w") as out:
            ist = inp.streams.video[0]
            ost = out.add_stream_from_template(ist)
            k = -1
            for p in inp.demux(ist):
                if p.pts is None or p.dts is None:
                    continue
                k += 1
                shift = shifts.get(k)
                if shift is None:
                    continue
                p.pts += shift
                p.dts += shift
                p.stream = ost
                out.mux(p)
                written += 1
        if written != sum(e - s for s, e in ranges):
            raise RuntimeError(f"옮긴 패킷 {written} ≠ 남길 프레임 {sum(e - s for s, e in ranges)}")
        return True
    except Exception as exc:
        logger.warning("remux 실패 (%s): %s", input_path, exc)
        if out_path.exists():
            os.remove(out_path)
        return False


def install() -> None:
    """LeRobot 의 재인코딩 자리에 remux 를 먼저 시도하게 한다.

    `_copy_and_reindex_videos` 가 이 함수를 모듈 전역으로 찾으므로 속성만 바꾸면 된다
    (hw_encoders.install 과 같은 방식).
    """
    from lerobot.datasets import dataset_tools as T

    original = T._keep_episodes_from_video_with_av

    def keep(input_path, output_path, episodes_to_keep, fps, vcodec="libsvtav1", pix_fmt="yuv420p"):
        if keep_episodes(input_path, output_path, episodes_to_keep, fps):
            logger.info("영상을 다시 압축하지 않고 옮겼습니다 (remux): %s", output_path)
            return
        logger.warning("remux 조건이 안 맞아 LeRobot 의 재인코딩(%s)으로 갑니다: %s", vcodec, input_path)
        original(input_path, output_path, episodes_to_keep, fps, vcodec, pix_fmt)

    T._keep_episodes_from_video_with_av = keep
