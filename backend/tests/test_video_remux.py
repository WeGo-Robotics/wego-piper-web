"""에피소드 삭제의 영상 remux (wrapper/video_remux.py).

하나를 지워도 LeRobot 은 카메라 mp4 를 통째로 AV1 재인코딩했다 — 120개에 85초, h264 → AV1,
남은 에피소드마다 세대 손실(실측 2026-10-29). remux 는 같은 일을 5초에 **픽셀 그대로** 한다.
"""

import sys
from pathlib import Path

import pytest

av = pytest.importorskip("av")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wrapper"))
import video_remux  # noqa: E402

FPS = 15
EP = 12          # 에피소드 하나의 프레임 수


def _record(path: Path, episodes: int = 3) -> None:
    """녹화처럼 — B 프레임을 쓰고 **에피소드 첫 프레임은 IDR** 인 h264."""
    import numpy as np

    with av.open(str(path), "w") as out:
        st = out.add_stream("libx264", rate=FPS, options={"bf": "2", "g": "1000", "forced-idr": "1"})
        st.width, st.height, st.pix_fmt = 64, 48, "yuv420p"
        for i in range(episodes * EP):
            img = np.full((48, 64, 3), (i * 7) % 256, np.uint8)
            img[:, : (i % 64)] = 255 - img[:, : (i % 64)]
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = i
            if i % EP == 0:
                frame.pict_type = av.video.frame.PictureType.I
            for p in st.encode(frame):
                out.mux(p)
        for p in st.encode():
            out.mux(p)


def _frames(path: Path) -> list[bytes]:
    with av.open(str(path)) as c:
        return [f.to_ndarray(format="rgb24").tobytes() for f in c.decode(c.streams.video[0])]


def test_deleting_the_middle_keeps_the_other_frames_exactly(tmp_path):
    src, dst = tmp_path / "src.mp4", tmp_path / "dst.mp4"
    _record(src)
    with av.open(str(src)) as c:
        reordered = any(p.pts != p.dts for p in c.demux(c.streams.video[0]) if p.pts is not None)
    assert reordered, "B 프레임이 없는 영상으로는 이 테스트가 아무것도 안 잰다"

    assert video_remux.keep_episodes(src, dst, [(0, EP), (2 * EP, 3 * EP)], FPS)

    before = _frames(src)
    assert _frames(dst) == before[:EP] + before[2 * EP:], "남긴 에피소드의 프레임이 달라졌다"
    with av.open(str(dst)) as c:
        assert c.streams.video[0].codec_context.name == "h264", "코덱이 바뀌었다 — 재인코딩했다"


def test_a_start_that_is_not_a_keyframe_falls_back(tmp_path):
    """키프레임이 아닌 자리에서 자르면 앞 프레임 없이 디코딩이 깨진다 — 재인코딩으로 넘긴다."""
    src, dst = tmp_path / "src.mp4", tmp_path / "dst.mp4"
    _record(src)
    assert not video_remux.keep_episodes(src, dst, [(0, EP), (EP + 3, 3 * EP)], FPS)
    assert not dst.exists(), "반쯤 쓴 파일을 남겼다 — LeRobot 이 그 자리에 다시 쓴다"


def test_deletes_go_through_the_remux_launcher():
    """삭제만 remux 를 건 런처로 — 다른 편집은 CLI 그대로다."""
    src = (Path(__file__).resolve().parents[2] / "wrapper" / "edit_dataset.py").read_text()
    assert 'with_name("edit_cli.py")' in src and 'op == "delete_episodes"' in src
    cli = (Path(__file__).resolve().parents[2] / "wrapper" / "edit_cli.py").read_text()
    assert cli.index("video_remux.install()") < cli.index("lerobot_edit_dataset"), \
        "LeRobot 을 먼저 불러오면 패치가 늦다"
