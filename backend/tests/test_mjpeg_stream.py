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


def test_the_still_preview_is_only_for_the_modal_and_unregistered_cards():
    """등록된 카드는 상시 화면(`LiveCameraFrame`)이다 — 한 장짜리 `previewSrc` 는 "설정을 바꿨으니
    다시 보여줘" 자리(설정 모달·미등록 카드)에 남는다. 카드마다 스트림을 물면 안 된다:
    브라우저 연결(6)을 다 쓰면 E-stop 요청이 나가지 못한다.
    """
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    body = src.split("const previewSrc", 1)[1].split("\n  const ", 1)[0]
    assert "/preview?t=" in body
    assert "/stream" not in body, "카드마다 스트림을 문다"
    assert "liveIds" not in src, "실시간 보기 토글이 남아 있다"
    assert "handleProbe" not in src, "업데이트 버튼의 흔적이 남아 있다"


def test_the_live_frame_is_never_given_a_src_or_a_refresh_timer():
    """⚠ `src` 를 주면 부모가 다시 그려질 때(3초 폴링마다) React 가 그 값으로 되돌려 쓴다 —
    상시 화면이 한 장짜리로 깜빡인다. 프레임은 훅이 `<img>` 에 직접 건다.
    """
    comp = (_SRC / "components" / "LiveCameraFrame.tsx").read_text()
    tag = comp.split("<img", 1)[1].split("/>", 1)[0]
    assert "src=" not in tag, "React 가 src 를 되돌려 쓴다"
    hook = (_SRC / "hooks" / "useCameraLive.ts").read_text()
    live = hook.split("export function useLiveFrames", 1)[1].split("// ── 멈춰 있으면", 1)[0]
    assert "setInterval" not in live, "상시 화면이 타이머로 돈다 — 스트림이 밀어준다"
    assert "/api/cameras/live" in live


def test_a_stopped_camera_is_revived_not_left_for_a_button():
    """⚠ 손으로 누르던 `실시간 보기` 가 사라졌다 — 멈춘 카메라는 페이지가 스스로 다시 연다.

    다만 쉬지 않고 두드리지 않는다(멈춘 장치를 반복해서 여는 것이 D405 가 D-state 로 물리는 대표
    경로였다): 시도 사이가 늘고, 횟수에 상한이 있다.
    """
    hook = (_SRC / "hooks" / "useCameraLive.ts").read_text()
    body = hook.split("export function useReviveStopped", 1)[1]
    assert "/cameras/connect" in body
    assert "REVIVE_WAIT_MS" in body and "REVIVE_WAIT_MS.length" in body, "시도 횟수 상한이 없다"
    page = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    assert "useReviveStopped(" in page
    # 설정 모달에서 사람이 만지는 중인 장치 값을 연결이 프로파일로 덮으면 안 된다
    assert "!settingsCam" in page.split("useReviveStopped(", 1)[1].split(")", 1)[0]


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


# ── 한 연결에 여러 카메라 (카메라 페이지) ──


def _parts(raw: bytes) -> list[tuple[str, bytes]]:
    """`X-Camera` 헤더와 본문을 가른 결과 — 프론트의 파서가 하는 일과 같다."""
    from urllib.parse import unquote

    out = []
    for chunk in raw.split(b"--piperframe\r\n")[1:]:
        head, _, body = chunk.partition(b"\r\n\r\n")
        fields = dict(line.split(": ", 1) for line in head.decode().split("\r\n"))
        n = int(fields["Content-Length"])
        out.append((unquote(fields["X-Camera"]), body[:n]))
    return out


def test_one_connection_carries_every_camera_and_names_each_part():
    """카드마다 스트림을 물면 브라우저 연결(6)을 다 쓰고 E-stop 요청이 큐에 선다."""
    readers = {
        "/dev/video0": iter([b"\xff\xd8a"]).__next__,
        "rs:123:color": iter([b"\xff\xd8b"]).__next__,
    }

    def wrap(fn):
        def _f():
            try:
                return fn()
            except StopIteration:
                return None
        return _f

    gen = mjpeg._pump_mux({k: wrap(v) for k, v in readers.items()}, fps=200, label="t")
    got = asyncio.run(_collect(gen, 2))
    parts = _parts(b"".join(got))
    assert dict(parts) == {"/dev/video0": b"\xff\xd8a", "rs:123:color": b"\xff\xd8b"}


def test_a_camera_without_a_frame_is_skipped_not_fatal():
    """정지·유휴 종료된 카메라가 섞여 있는 것이 정상이다 — 나머지는 계속 흐른다."""
    calls = {"n": 0}

    def alive():
        calls["n"] += 1
        return b"\xff\xd8x" if calls["n"] == 1 else None

    gen = mjpeg._pump_mux({"stopped": lambda: None, "alive": alive}, fps=200, label="t")
    parts = _parts(b"".join(asyncio.run(_collect(gen, 1))))
    assert parts == [("alive", b"\xff\xd8x")]


def test_one_broken_reader_does_not_take_the_others_down():
    def broken():
        raise RuntimeError("segment gone")

    gen = mjpeg._pump_mux({"bad": broken, "ok": lambda: b"\xff\xd8y"}, fps=200, label="t")
    parts = _parts(b"".join(asyncio.run(_collect(gen, 1))))
    assert parts == [("ok", b"\xff\xd8y")]


def test_multiplex_counts_as_one_stream_and_honours_the_cap():
    import app.services.mjpeg as m

    m._open = m.MAX_STREAMS
    try:
        assert m.multiplex({"a": lambda: None}, label="t").status_code == 503
    finally:
        m._open = 0


def test_the_live_reader_follows_a_rename(monkeypatch):
    """⚠ 카드에서 이름을 바꾸면 데몬이 세그먼트를 새 이름으로 옮긴다. 이름을 만들 때 한 번만 풀면
    상시 화면이 옛 이름을 문 채 **영영 빈 채로** 남는다 — 에러도 안 난다."""
    import piper_shm
    from app.services import shm_snapshot

    names = {"rs:1:color": "rs_1_color"}
    opened = []

    def fake_reader(cam_id, quality=80):
        seg = names[cam_id]
        opened.append(seg)
        return lambda: f"frame-from-{seg}".encode()

    monkeypatch.setattr(piper_shm, "segment_for_camera", lambda c: names[c])
    monkeypatch.setattr(shm_snapshot, "segment_reader", fake_reader)

    read = shm_snapshot.following_reader("rs:1:color")
    assert read() == b"frame-from-rs_1_color"
    assert read() == b"frame-from-rs_1_color" and opened == ["rs_1_color"], "이름이 그대로인데 다시 연다"
    names["rs:1:color"] = "right"             # 개명 — 데몬이 세그먼트를 옮겼다
    assert read() == b"frame-from-right"
    assert opened == ["rs_1_color", "right"]


def test_the_cameras_page_does_not_open_one_stream_per_card():
    """⚠ 되돌리기 쉬운 회귀다 — 카드에 `/stream` 을 다시 걸면 카메라 6대에서 정지 버튼이 막힌다."""
    page = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    ready_card = page.split("{/* 등록됨 */}", 1)[1].split("{/* 미연결 카메라", 1)[0]
    assert "/stream" not in ready_card
    assert "LiveCameraFrame" in ready_card


def test_live_endpoint_refuses_more_cameras_than_it_will_encode():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers.cameras import MAX_LIVE_CAMERAS

    with TestClient(app) as c:
        ids = "&".join(f"ids=/dev/video{i}" for i in range(MAX_LIVE_CAMERAS + 1))
        r = c.get(f"/api/cameras/live?{ids}")
    assert r.status_code == 400
