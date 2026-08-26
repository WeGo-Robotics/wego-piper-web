"""프리뷰 MJPEG 스트림.

폴링(`?t=` 를 200ms 마다 갱신)은 카메라당 초당 5장이었다. 서버가 느려서 끊긴
게 아니다 — 한 장에 1.2ms 였고 기계는 놀고 있었다. **초당 5장이 설계값**이었고,
프레임마다 별도 요청이라 간격까지 출렁였다.

폴링 주기를 올리는 것으로는 못 고친다: HTTP/1.1 은 오리진당 연결이 6개뿐이고,
그 경합이 E-stop heartbeat 를 굶겨 녹화를 죽인 적이 있다.
"""

import asyncio
from pathlib import Path

import pytest

from app.services import mjpeg

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


async def _collect(gen, n):
    out = []
    async for chunk in gen:
        out.append(chunk)
        if len(out) >= n:
            break
    return out


def test_frames_are_sent_as_multipart_parts():
    """`<img>` 가 네이티브로 재생하는 형식이라야 프론트가 `src` 만 바뀐다."""
    frames = [b"\xff\xd8one", b"\xff\xd8two"]
    it = iter(frames)
    gen = mjpeg._pump(lambda: next(it, None), fps=100, label="t")
    parts = asyncio.run(_collect(gen, 2))
    assert all(p.startswith(b"--piperframe") for p in parts)
    assert b"Content-Type: image/jpeg" in parts[0]
    assert parts[0].endswith(frames[0] + b"\r\n")


def test_an_unchanged_frame_is_not_resent():
    """같은 프레임을 다시 보내면 대역폭만 쓰고 화면은 그대로다.

    판정은 프레임을 가져오는 쪽이 한다 — 세그먼트는 `seq`, 버스는 바이트 비교.
    """
    calls = {"n": 0}

    def src():
        calls["n"] += 1
        return b"\xff\xd8x" if calls["n"] == 1 else None

    parts = asyncio.run(_collect(mjpeg._pump(src, fps=200, label="t"), 1))
    assert len(parts) == 1 and calls["n"] >= 1


def test_too_many_streams_are_refused_not_queued():
    """하나가 연결을 계속 쥔다. 무제한이면 잊고 열어둔 탭이 조용히 쌓인다."""
    assert mjpeg.MAX_STREAMS > 0
    import app.services.mjpeg as m

    m._open = m.MAX_STREAMS
    try:
        assert mjpeg.stream(lambda: None, label="t").status_code == 503
    finally:
        m._open = 0


def test_a_vanished_segment_does_not_end_the_stream():
    """⚠ camerad 를 재시작하면 세그먼트가 잠깐 사라진다.

    여기서 스트림을 끝내면 `<img>` 는 **마지막 프레임을 띄운 채 얼어붙고 스스로
    다시 붙지 않는다.** 사용자는 멈춘 줄 모르고 옛 장면을 본다 — 로봇 화면에서
    가장 위험한 종류의 거짓말이다.
    """
    src = (Path(__file__).resolve().parents[1] / "app" / "services"
           / "shm_snapshot.py").read_text()
    body = src.split("def segment_reader", 1)[1].split("\ndef ", 1)[0]
    assert 'state["sub"] = None' in body, "실패하면 포기해 버린다"
    assert "Subscriber(seg)" in body.split('if state["sub"] is None', 1)[1], \
        "다시 열지 않는다"


def test_the_live_view_uses_the_stream_and_the_rest_does_not():
    """카드마다 연결을 물면 목록만 열어도 카메라 수만큼 연결이 열린다.

    한 장짜리는 "설정을 바꿨으니 다시 보여줘" 자리에 그대로 필요하다.
    """
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    body = src.split("const previewSrc", 1)[1].split("\n  const ", 1)[0]
    assert "liveIds.has(id)" in body, "실시간 여부로 안 가른다"
    assert "/stream" in body and "/preview?t=" in body, "둘 중 하나가 없다"


def test_the_live_view_has_no_refresh_timer():
    """⚠ 주기적으로 `src` 를 건드리면 그때마다 연결이 끊겼다 붙어 **오히려 끊긴다.**"""
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    body = src.split("if (liveIds.size === 0) return", 1)[1].split("}, [liveIds])", 1)[0]
    assert "setInterval" not in body, "실시간 보기가 아직 타이머로 돈다"


def test_the_recording_preview_streams_too():
    """수집 화면이 바로 그 끊김을 보던 자리다."""
    src = (_SRC / "components" / "RecordPreview.tsx").read_text()
    assert "preview-stream/" in src, "아직 폴링한다"
    assert "setInterval(() => setTs" not in src, "프레임 폴링 타이머가 남아 있다"


def test_a_stream_waits_before_the_first_frame_then_gives_up(monkeypatch):
    """없는 카메라를 영영 붙들면 동시 스트림 자리만 먹는다.

    상한을 줄여서 잰다 — 실제 값(15초)을 그대로 기다리면 테스트가 그만큼 멈춘다.
    """
    monkeypatch.setattr(mjpeg, "FIRST_FRAME_TIMEOUT_S", 0.05)
    gen = mjpeg._pump(lambda: None, fps=200, label="t")
    assert asyncio.run(_collect(gen, 1)) == [], "첫 프레임 없이도 안 끝난다"


def test_the_wait_is_long_enough_for_a_daemon_restart():
    """너무 짧으면 카메라 데몬을 재시작하는 동안 연 스트림이 그냥 죽는다."""
    assert mjpeg.FIRST_FRAME_TIMEOUT_S >= 10.0


def test_a_stream_that_has_delivered_keeps_waiting():
    """⚠ 규칙이 첫 프레임 전후로 **다르다.**

    한 번이라도 받았다면 실재하는 카메라다. 잠깐 내려간 것을 두고 스트림을 접으면
    `<img>` 가 얼어붙고 스스로 안 돌아온다 — 사용자는 옛 장면을 현재로 본다.
    """
    n = {"i": 0}

    def src():
        n["i"] += 1
        return b"\xff\xd8x" if n["i"] == 1 else None   # 한 장 뒤로는 계속 없음

    async def run():
        got = []
        gen = mjpeg._pump(src, fps=500, label="t")
        try:
            async with asyncio.timeout(1.0):
                async for part in gen:
                    got.append(part)
        except TimeoutError:
            return got, "still_open"
        return got, "ended"

    parts, how = asyncio.run(run())
    assert len(parts) == 1
    assert how == "still_open", "받아본 적 있는 스트림을 접어 버린다"


def test_the_endpoint_does_not_refuse_while_the_publisher_is_down():
    """404 로 거절하면 재개방 로직에 **닿을 일이 없다** — 실제로 그랬다."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "cameras.py").read_text()
    body = src.split("async def preview_stream", 1)[1].split("\n@router", 1)[0]
    assert "404" not in body, "세그먼트가 없다고 스트림을 거절한다"
