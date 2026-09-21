"""가중치 보험 — **파기 전에 직접 끌어온다** (feature/vast-training.md §5·§12-4).

⚠ 이건 "있으면 좋은 것" 이 아니다. 실측으로 `push_to_hub` 는 학습이 **끝날 때 한 번만**
올린다 — `save_freq` 로 체크포인트가 다섯 개 생겨도 Hub 커밋은 종료 시각 한 묶음뿐이다.
중간에 죽으면 Hub 에 아무것도 없고, 몇 시간짜리 결과가 기계와 함께 사라진다.
"""

import subprocess
from pathlib import Path

from app.services.cloud import rescue as R
from app.services.training.runners.ssh import SSHTarget

TARGET = SSHTarget(host="h", port=2222, user="root", key_path="/k/id",
                   known_hosts="/k/kh")
FOUND = "/root/outputs/train/2026-09-17/00-30-00_act/checkpoints/last/pretrained_model"


def _run_ok(_t, _cmd):
    return subprocess.CompletedProcess([], 0, FOUND + "\n", "")


def _capture(monkeypatch) -> list:
    """`scp` 호출을 가로채 인자를 모은다 — 네트워크는 안 건드린다."""
    calls: list[list[str]] = []

    def _fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(R.subprocess, "run", _fake)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# 무엇을 받고 무엇을 안 받나
# ─────────────────────────────────────────────────────────────────────────────

def test_it_pulls_only_the_weights_never_the_optimizer_state(monkeypatch, tmp_path):
    """⚠ 실측: `last/pretrained_model` 198MB 대 `last/training_state` **394MB**.

    후자는 추론에 안 쓰는데 Vast 는 나가는 트래픽에 GB 당 과금한다($0.017/GB) — 두 배를
    더 내고 안 쓸 것을 받을 이유가 없다.
    """
    calls = _capture(monkeypatch)
    R.rescue(TARGET, "me/m", tmp_path, run=_run_ok)
    src = next(a for a in calls[0] if a.startswith("h:") or ":" in a and "/root/" in a)
    assert src.endswith("pretrained_model"), src
    assert "training_state" not in " ".join(calls[0])


def test_scp_reuses_the_ssh_options_so_the_two_cannot_drift(monkeypatch, tmp_path):
    """⚠ 포트·키·known_hosts 를 따로 조립하면 ssh 는 되는데 scp 만 실패하는 날이 온다.

    그리고 `scp` 는 포트를 **`-P`** 로 받는다(ssh 는 `-p`) — 그대로 넘기면 조용히 틀린다.
    """
    calls = _capture(monkeypatch)
    d = tmp_path / "models--me--m" / "snapshots" / "rescued"
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    R.rescue(TARGET, "me/m", tmp_path, run=_run_ok)
    cmd = calls[0]
    assert cmd[0] == "scp" and "-r" in cmd
    assert cmd[cmd.index("-P") + 1] == "2222", "포트를 -p 로 넘겼다"
    assert cmd[cmd.index("-i") + 1] == "/k/id"
    assert "UserKnownHostsFile=/k/kh" in cmd


def test_it_lands_in_the_same_shape_as_a_local_run(tmp_path):
    """⚠ 예전에는 HF 캐시 모양(`models--org--name/snapshots/rescued`)으로 떨어뜨렸다.
    파일은 화면에 떴지만 **로컬 학습과 다른 덩어리**로 읽혀서, 같은 학습의 중간
    체크포인트와 최종본이 목록에서 갈라졌다.

    로컬 학습 실측: `~/outputs/train/2026-09-18/09-52-01_act/checkpoints/020000/
    pretrained_model`. 클라우드 결과도 글자 하나까지 이 모양이어야 한다.
    """
    got = R.dest_for(FOUND, "wego-hansu/my-act", "last", tmp_path)
    assert got == (tmp_path / "2026-09-17" / "00-30-00_act"
                   / "checkpoints" / "last" / "pretrained_model")


def test_the_run_name_comes_from_the_box_not_from_us(tmp_path):
    """⚠ 이름을 우리가 지으면 로컬과 갈린다. 원격도 같은 lerobot 이라 `2026-09-17/
    00-30-00_act` 를 **이미 지어 놨다** — 저장소 이름(`my-act`)은 거기 안 들어간다.

    제로패딩도 마찬가지다: `020000` 은 lerobot 이 붙인 것이라 그대로 따라가야 한다.
    """
    src = "/root/outputs/train/2026-09-18/02-13-23_act/checkpoints/020000/pretrained_model"
    got = R.dest_for(src, "wego-hansu/my-act", "020000", tmp_path)
    assert "my-act" not in str(got)
    assert got.parent.name == "020000", "제로패딩을 우리가 고쳐 쓰면 안 된다"


def test_an_unreadable_remote_path_still_lands_somewhere(tmp_path):
    """⚠ 이름을 못 읽었다고 받은 것을 버리면 안 된다 — 저장소 이름으로라도 놓는다."""
    got = R.dest_for("", "wego-hansu/my-act", "last", tmp_path)
    assert got == tmp_path / "my-act" / "checkpoints" / "last" / "pretrained_model"


def test_a_rescue_reports_the_local_run_directory(monkeypatch, tmp_path):
    """받은 자리를 그대로 돌려준다 — 화면이 이걸 "받은 자리" 로 보여 준다."""
    def _fake(cmd, **kw):
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "config.json").write_text("{}")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(R.subprocess, "run", _fake)
    got = R.rescue(TARGET, "me/m", tmp_path, run=_run_ok)
    assert got == (tmp_path / "2026-09-17" / "00-30-00_act"
                   / "checkpoints" / "last" / "pretrained_model")


# ─────────────────────────────────────────────────────────────────────────────
# 실패해도 파기를 막지 않는다
# ─────────────────────────────────────────────────────────────────────────────

def test_a_missing_checkpoint_is_reported_not_raised(tmp_path):
    """학습이 체크포인트를 남기기 전에 죽었을 수 있다 — 그건 예외가 아니라 사실이다."""
    def _none(_t, _c):
        return subprocess.CompletedProcess([], 0, "", "")

    assert R.rescue(TARGET, "me/m", tmp_path, run=_none) is None


def test_a_timeout_gives_up_rather_than_blocking_the_teardown(monkeypatch, tmp_path):
    """⚠ 느린 호스트에서 200MB 를 끄는 동안에도 과금은 계속된다. **보험이 본체보다
    비싸지면 보험이 아니다** — 포기하고 파기한다."""
    def _slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(R.subprocess, "run", _slow)
    assert R.rescue(TARGET, "me/m", tmp_path, run=_run_ok) is None


def test_a_failed_scp_never_raises(monkeypatch, tmp_path):
    """⚠ 보험 실패가 파기를 막으면 본전도 못 찾는다 — 기계가 계속 돌면서 요금이 나간다."""
    monkeypatch.setattr(R.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "거부됨"))
    assert R.rescue(TARGET, "me/m", tmp_path, run=_run_ok) is None


def test_files_without_a_config_are_not_called_a_rescue(monkeypatch, tmp_path):
    """⚠ **파일이 왔다고 쓸 수 있는 게 아니다.** `config.json` 이 없으면 스캐너가 정책으로
    안 읽는다 — 성공이라 말하면 사람이 있다고 믿고 찾다가 못 찾는다."""
    _capture(monkeypatch)          # scp 는 성공하지만 아무것도 안 만든다
    assert R.rescue(TARGET, "me/m", tmp_path, run=_run_ok) is None


# ⚠ Hub 확인(`pushed`)과 파기 전후 순서는 `test_cloud_retrieve.py` 로 옮겼다 —
#   보험(`scp`)과 회수 정책은 다른 파일이다.


def test_rescue_runs_before_the_teardown_not_after():
    """⚠ 순서가 전부다. 파기 뒤에 끌어오면 **기계가 이미 없다.**"""
    import inspect

    from app.services.cloud import procure

    src = inspect.getsource(procure.procure_and_train)
    assert src.index("rescue_if_needed(") < src.index("job.finish("), \
        "회수가 파기 뒤에 있다 — 그때는 기계가 없다"


# ─────────────────────────────────────────────────────────────────────────────
# 사람이 [중지] 를 눌렀을 때 — **여기서 회수를 앞지르면 결과를 잃는다**
# ─────────────────────────────────────────────────────────────────────────────

def test_manual_stop_lets_the_flow_retrieve_before_destroying():
    """⚠ 중지가 곧바로 파기하면 `scp` 가 **사라진 호스트**를 향한다.

    조달 흐름은 학습이 멈춘 것을 보고 `retrieving` 에서 회수한 다음 파기한다. 중지가
    그걸 앞지르면, 푸시도 안 된 회차에서 결과가 통째로 사라진다 — 보험을 넣은 이유가
    정확히 그 경우다.
    """
    import inspect

    from app.services.cloud import rent

    src = inspect.getsource(rent.stop_now)
    assert "shield" in src and "STOP_GRACE_S" in src, "흐름에 맡기지 않고 바로 파기한다"
    # 그렇다고 무한정 믿지도 않는다 — 기다리다 못 끄는 것이 제일 나쁘다
    assert "job.finish" in src, "흐름이 없을 때의 파기 경로가 없다"


def test_finishing_is_seen_quickly_even_though_budget_is_checked_slowly():
    """⚠ 끝난 뒤의 시간은 **빈 기계에 내는 돈**이다. 상한(30초)과 같은 주기로 보면
    중지 후 회수가 그만큼 늦어진다."""
    from app.services.cloud import procure

    assert procure._DONE_POLL_S <= 5.0 < procure.TICK_S
