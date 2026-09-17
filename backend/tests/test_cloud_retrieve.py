"""회수 — 가중치가 **집에 와야** 끝난 것이다 (feature/vast-training.md §5·§12-4).

⚠ 이 파일이 지키는 것은 **순서**다.

- `scp` 는 기계가 살아 있는 동안만 된다 → **파기 전에**
- Hub 는 기계를 안 탄다 → **파기 뒤에** (그전에 받으면 내려받는 동안 GPU 요금을 낸다)

둘을 바꿔 달면 하나는 빈 호스트를 향하고, 하나는 돈을 더 낸다.
"""

import asyncio
import inspect

import pytest

from app.services.cloud import retrieve


@pytest.fixture(autouse=True)
def fresh():
    retrieve.reset()
    yield
    retrieve.reset()


def _hub_has(monkeypatch, *files):
    """⚠ 대역도 **진짜 응답을 닮아야** 한다 — `ModelInfo` 에는 늘 `last_modified` 가
    있다. 없는 대역으로 통과시키면, 시각을 보는 코드를 넣는 순간 전부 무너진다."""
    from datetime import datetime, timezone

    class _Info:
        siblings = [type("S", (), {"rfilename": f})() for f in files]
        last_modified = datetime.now(timezone.utc)

    monkeypatch.setattr("huggingface_hub.HfApi.model_info", lambda self, r: _Info())


# ─────────────────────────────────────────────────────────────────────────────
# Hub 에 올라갔나
# ─────────────────────────────────────────────────────────────────────────────

def test_an_existing_repo_without_weights_is_not_a_successful_push(monkeypatch):
    """⚠ repo 가 있는 것과 가중치가 올라간 것은 다르다. 실측으로 푸시는 커밋 **넷**으로
    나뉘고, `initial commit` 만 남기고 죽을 수 있다."""
    _hub_has(monkeypatch, "README.md")
    assert retrieve.pushed("me/m") is False


def test_weights_present_means_the_hub_has_it(monkeypatch):
    _hub_has(monkeypatch, "config.json", "model.safetensors")
    assert retrieve.pushed("me/m") is True


def test_when_the_hub_cannot_be_asked_we_lean_towards_pulling(monkeypatch):
    """⚠ 모르면 **받는 쪽**으로 기운다. 전송비는 몇 센트지만 잃은 학습은 몇 시간이다."""
    def _boom(self, r):
        raise RuntimeError("HF 안 닿음")

    monkeypatch.setattr("huggingface_hub.HfApi.model_info", _boom)
    assert retrieve.pushed("me/m") is False


# ─────────────────────────────────────────────────────────────────────────────
# 로컬에 이미 있나 — **가중치 파일까지 본다**
# ─────────────────────────────────────────────────────────────────────────────

def test_a_half_downloaded_snapshot_is_not_here_yet(tmp_path):
    """⚠ 디렉토리만 보고 판정하면 받다 만 자리가 "있음" 이 된다 — 그러면 다시 받지
    않고, 사람은 못 쓰는 폴더를 보게 된다."""
    snap = tmp_path / "models--me--m" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    assert retrieve.local_snapshot("me/m", tmp_path) is None
    (snap / "model.safetensors").write_bytes(b"x")
    assert retrieve.local_snapshot("me/m", tmp_path) == snap


def test_nothing_downloaded_at_all(tmp_path):
    assert retrieve.local_snapshot("me/m", tmp_path) is None


def test_the_cache_name_is_what_the_scanner_reads():
    """⚠ 스캐너는 HF 캐시 모양만 읽는다. 이름이 어긋나면 파일은 있는데 **화면에 안 뜬다.**"""
    assert retrieve.cache_name("wego-hansu/sim_data2-act") == "models--wego-hansu--sim_data2-act"


# ─────────────────────────────────────────────────────────────────────────────
# 파기 전 — 기계가 있어야 되는 일
# ─────────────────────────────────────────────────────────────────────────────

def test_when_the_hub_has_it_we_do_not_touch_the_box(monkeypatch, tmp_path):
    """⚠ Hub 에 있으면 `scp` 를 안 쓴다. 같은 것을 두 번 받을 이유가 없다."""
    _hub_has(monkeypatch, "model.safetensors")
    called = []
    asyncio.run(retrieve.before_destroy("target", "me/m", tmp_path,
                                        rescue_fn=lambda *a: called.append(a)))
    assert called == []
    assert retrieve.status()["state"] == "waiting", "파기 뒤에 받겠다고 표시해야 한다"


def test_when_the_hub_is_empty_we_pull_off_the_box_before_it_dies(monkeypatch, tmp_path):
    """⚠ **이 경우가 보험의 이유다.** 푸시는 종료 시점 한 번뿐이라, 중간에 죽은 회차는
    Hub 에 아무것도 없다 — 지금 안 받으면 영영 못 받는다(§12-4)."""
    _hub_has(monkeypatch, "README.md")
    got = tmp_path / "rescued"
    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=lambda *a: got))
    assert retrieve.status() == {"state": "done", "repo_id": "me/m", "path": str(got),
                                 "source": "scp", "detail": ""}


def test_a_rescue_that_finds_nothing_is_not_a_success(monkeypatch, tmp_path):
    _hub_has(monkeypatch, "README.md")
    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=lambda *a: None))
    assert retrieve.status()["state"] == "missing"


def test_a_failed_rescue_never_raises(monkeypatch, tmp_path):
    """⚠ 회수 실패가 파기를 막으면 본전도 못 찾는다 — 기계가 계속 돌면서 요금이 나간다."""
    _hub_has(monkeypatch, "README.md")

    def _boom(*a):
        raise RuntimeError("회선 끊김")

    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=_boom))
    assert retrieve.status()["state"] == "failed" and "회선" in retrieve.status()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# 파기 뒤 — 기계가 없어도 되는 일
# ─────────────────────────────────────────────────────────────────────────────

def test_the_weights_come_home_without_anyone_clicking(monkeypatch, tmp_path):
    """⚠ **이게 이번 작업의 요점이다.** 전에는 푸시가 성공하면 여기서 끝이었고, 사람이
    저장소 페이지에서 [다운로드]를 눌러야 로컬에 왔다 — 자리를 뜬 사람에게는 다 됐는데
    못 쓰는 상태다."""
    _hub_has(monkeypatch, "model.safetensors")
    landed = tmp_path / "models--me--m" / "snapshots" / "deadbeef"
    monkeypatch.setattr(retrieve, "pull", lambda repo, d: landed)

    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=lambda *a: None))
    asyncio.run(retrieve.after_destroy("me/m", tmp_path))
    assert retrieve.status()["state"] == "done"
    assert retrieve.status()["path"] == str(landed)


def test_a_rescued_run_is_not_downloaded_again(monkeypatch, tmp_path):
    """⚠ `scp` 로 이미 받았으면 Hub 를 또 안 탄다 — 전송비를 두 번 낼 이유가 없다."""
    _hub_has(monkeypatch, "README.md")
    pulled = []
    monkeypatch.setattr(retrieve, "pull", lambda *a: pulled.append(a))
    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=lambda *a: tmp_path))
    asyncio.run(retrieve.after_destroy("me/m", tmp_path))
    assert pulled == []


def test_an_already_downloaded_repo_does_not_hit_the_network(monkeypatch, tmp_path):
    """같은 저장소로 다시 돌린 경우. 캐시가 있어도 네트워크를 아예 안 타는 편이 낫다."""
    _hub_has(monkeypatch, "model.safetensors")
    snap = tmp_path / "models--me--m" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.safetensors").write_bytes(b"x")
    monkeypatch.setattr(retrieve, "pull",
                        lambda *a: pytest.fail("이미 있는데 또 받았다"))

    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=lambda *a: None))
    asyncio.run(retrieve.after_destroy("me/m", tmp_path))
    assert retrieve.status()["state"] == "done" and retrieve.status()["source"] == "local"


def test_a_download_that_hangs_gives_up_and_says_where_to_get_it(monkeypatch, tmp_path):
    """⚠ 상한이 지키는 것은 **돈이 아니라 다음 임대**다 — 기계는 이미 파기됐고,
    매달린 다운로드는 `busy()` 를 계속 참으로 둬서 다음 학습을 막는다.

    ⚠ 그리고 실패해도 **잃은 것은 없다** — Hub 에 그대로 있다. 그 말을 화면에 남긴다.
    """
    _hub_has(monkeypatch, "model.safetensors")

    def _hang(*a):
        import time
        time.sleep(0.3)   # ⚠ 짧게. 스레드가 안 끝나면 `asyncio.run` 종료가 그만큼 걸린다

    monkeypatch.setattr(retrieve, "pull", _hang)
    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=lambda *a: None))
    asyncio.run(retrieve.after_destroy("me/m", tmp_path, timeout=0.05))
    st = retrieve.status()
    assert st["state"] == "failed" and "저장소 페이지" in st["detail"]


def test_a_download_failure_never_raises(monkeypatch, tmp_path):
    _hub_has(monkeypatch, "model.safetensors")

    def _boom(*a):
        raise RuntimeError("HF 503")

    monkeypatch.setattr(retrieve, "pull", _boom)
    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=lambda *a: None))
    asyncio.run(retrieve.after_destroy("me/m", tmp_path))
    assert retrieve.status()["state"] == "failed"


def test_nothing_happens_without_a_repo(tmp_path):
    asyncio.run(retrieve.before_destroy("t", "", tmp_path, rescue_fn=lambda *a: tmp_path))
    asyncio.run(retrieve.after_destroy("", tmp_path))
    assert retrieve.status()["state"] == "idle"


# ─────────────────────────────────────────────────────────────────────────────
# 순서 — 이 파일의 이유
# ─────────────────────────────────────────────────────────────────────────────

def test_the_scp_path_runs_before_the_teardown_not_after():
    """⚠ 파기 뒤에 `scp` 하면 **기계가 이미 없다.**"""
    from app.services.cloud import procure

    src = inspect.getsource(procure.procure_and_train)
    assert src.index("rescue_if_needed(") < src.index("job.finish("), \
        "회수가 파기 뒤에 있다 — 그때는 기계가 없다"


def test_the_hub_download_runs_after_the_teardown_not_before():
    """⚠ 반대다. Hub 는 기계를 안 타므로 파기 전에 받으면 **내려받는 동안 GPU 요금을
    낸다** — 그리고 그만큼 파기가 늦어진다. 가장 중요한 불변식을 회선에 묶을 수 없다."""
    from app.services.cloud import rent

    src = inspect.getsource(rent.start)
    assert src.index("procure_and_train(") < src.index("after_destroy("), \
        "Hub 다운로드가 파기 전에 있다 — 빈 기계에 요금을 낸다"


def test_the_download_waits_until_the_runner_is_restored():
    """⚠ 러너를 되돌리기 **전에** 받으면, 느린 다운로드 동안 원격 러너가 꽂힌 채로
    남는다 — 그 사이 로컬 학습을 걸면 죽은 호스트로 붙는다."""
    from app.services.cloud import rent

    src = inspect.getsource(rent.start)
    assert src.index("train_manager.runner = original") < src.index("after_destroy("), \
        "러너 복구보다 다운로드가 먼저다"


def test_a_new_rental_clears_the_previous_result():
    """⚠ 안 지우면 새 임대를 걸었는데 화면이 **지난번 가중치**를 "받았음" 으로 보여 준다."""
    from app.services.cloud import rent

    src = inspect.getsource(rent.start)
    assert "retrieve.reset()" in src


# ─────────────────────────────────────────────────────────────────────────────
# 화면이 받아 가는 자리
# ─────────────────────────────────────────────────────────────────────────────

def test_the_rent_status_carries_the_retrieval(monkeypatch, tmp_path):
    """⚠ **파기됨 ≠ 끝남.** 기계를 끈 뒤에도 Hub 에서 받는 구간이 남는다 — 그 자리를
    안 주면 화면이 "파기됨" 에서 멈춘 것처럼 보이고, 사람은 아직 안 온 가중치로
    추론을 걸려다 그제서야 안다."""
    from fastapi.testclient import TestClient

    from app.main import app

    _hub_has(monkeypatch, "model.safetensors")
    c = TestClient(app)

    d = c.get("/api/cloud/rent").json()
    assert d["retrieval"]["state"] == "idle", "회수 자리가 응답에 없다"

    asyncio.run(retrieve.before_destroy("t", "me/m", tmp_path, rescue_fn=lambda *a: None))
    d = c.get("/api/cloud/rent").json()
    assert d["retrieval"]["state"] == "waiting" and d["retrieval"]["repo_id"] == "me/m"


# ─────────────────────────────────────────────────────────────────────────────
# ⚠ 같은 저장소로 두 번째를 돌렸을 때 — **지난 회차 것을 이번 것으로 읽으면 안 된다**
# ─────────────────────────────────────────────────────────────────────────────

def _hub_at(monkeypatch, when, *files):
    class _Info:
        siblings = [type("S", (), {"rfilename": f})() for f in files]
        last_modified = when

    monkeypatch.setattr("huggingface_hub.HfApi.model_info", lambda self, r: _Info())


def test_weights_older_than_this_run_are_not_this_runs_weights(monkeypatch):
    """⚠ 이번 회차가 중간에 죽으면 Hub 에 남은 것은 **지난번 가중치**다. 파일만 보면
    "올라갔다" 가 되어 `scp` 보험을 건너뛰고 — 이번 결과를 영영 잃은 채 지난번 것을
    받아 놓고 성공이라 말한다."""
    from datetime import datetime, timedelta, timezone

    started = datetime.now(timezone.utc)
    _hub_at(monkeypatch, started - timedelta(hours=3), "model.safetensors")
    assert retrieve.pushed("me/m", since=started.timestamp()) is False


def test_weights_newer_than_this_run_are_this_runs_weights(monkeypatch):
    from datetime import datetime, timedelta, timezone

    started = datetime.now(timezone.utc)
    _hub_at(monkeypatch, started + timedelta(minutes=4), "model.safetensors")
    assert retrieve.pushed("me/m", since=started.timestamp()) is True


def test_without_a_start_time_we_keep_the_old_behaviour(monkeypatch):
    """`since` 를 안 주면 시각은 안 본다 — 혼자 부르는 자리(수동 확인 등)를 위해."""
    from datetime import datetime, timedelta, timezone

    _hub_at(monkeypatch, datetime.now(timezone.utc) - timedelta(days=9), "model.safetensors")
    assert retrieve.pushed("me/m") is True


def test_an_unknown_timestamp_leans_towards_rescuing(monkeypatch):
    """⚠ 모르면 **받는 쪽**으로. 전송비는 몇 센트지만 잃은 학습은 몇 시간이다."""
    _hub_at(monkeypatch, None, "model.safetensors")
    assert retrieve.pushed("me/m", since=1.0) is False


def test_the_rental_passes_its_own_start_time(monkeypatch):
    """⚠ `Budget.started_at` 은 `monotonic` 이라 Hub 시각과 못 비교한다 — 벽시계여야 한다."""
    import inspect

    from app.services.cloud import rent

    src = inspect.getsource(rent.start)
    assert "started_at = time.time()" in src and "since=started_at" in src


def test_a_timestamp_without_a_timezone_is_read_as_utc(monkeypatch):
    """⚠ tz 없는 시각에 `timestamp()` 를 부르면 **로컬 시각으로 읽는다** — KST 에서는
    방금 올린 것이 9시간 전 것으로 둔갑해, 멀쩡히 푸시된 회차가 매번 `scp` 를 탄다."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    _hub_at(monkeypatch, (now + timedelta(minutes=1)).replace(tzinfo=None),
            "model.safetensors")
    assert retrieve.pushed("me/m", since=now.timestamp()) is True


def test_stopping_while_the_download_runs_does_not_claim_to_destroy_again():
    """⚠ 흐름에는 파기 **뒤** Hub 다운로드가 붙어 있다. 200MB 를 받는 중이면 중지가
    기다리는 90초를 넘기는 것이 정상인데, 그때 "직접 파기합니다" 를 찍으면 로그가
    거짓말을 한다 — 읽는 사람은 파기가 늦어진 줄 안다."""
    import inspect
    import textwrap

    from conftest import python_code_only

    from app.services.cloud import rent

    # ⚠ 주석을 걷어내고 본다. "…를 찍으면 로그가 거짓말을 한다" 고 적어둔 설명이
    #   "그 문구를 찍는다" 검사에 걸린다 — `code_only` 가 있는 이유가 그거다.
    src = python_code_only(textwrap.dedent(inspect.getsource(rent.stop_now)))
    tail = src[src.index("TimeoutError"):]
    assert "Phase.DESTROYED" in tail, "이미 파기됐는지 안 보고 파기 문구를 찍는다"
    assert tail.index("Phase.DESTROYED") < tail.index("직접 파기합니다")


