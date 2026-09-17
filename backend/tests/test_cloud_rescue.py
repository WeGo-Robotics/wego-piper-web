"""가중치 보험 — **파기 전에 직접 끌어온다** (feature/vast-training.md §5·§12-4).

⚠ 이건 "있으면 좋은 것" 이 아니다. 실측으로 `push_to_hub` 는 학습이 **끝날 때 한 번만**
올린다 — `save_freq` 로 체크포인트가 다섯 개 생겨도 Hub 커밋은 종료 시각 한 묶음뿐이다.
중간에 죽으면 Hub 에 아무것도 없고, 몇 시간짜리 결과가 기계와 함께 사라진다.
"""

import subprocess
from pathlib import Path

import pytest

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
    (tmp_path / "models--me--m" / "snapshots" / "rescued").mkdir(parents=True)
    (tmp_path / "models--me--m" / "snapshots" / "rescued" / "config.json").write_text("{}")
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


def test_it_lands_where_the_model_scanner_actually_looks(tmp_path):
    """⚠ 스캐너는 HF 캐시 모양만 읽는다. 아무 데나 두면 파일은 있는데 **화면에 안 뜬다** —
    회수했다고 믿는데 못 쓰는 상태가 된다."""
    got = R.snapshot_dir(tmp_path, "wego-hansu/my-act")
    assert got == tmp_path / "models--wego-hansu--my-act" / "snapshots" / "rescued"


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


# ─────────────────────────────────────────────────────────────────────────────
# 언제 쓰나 — Hub 확인이 먼저다
# ─────────────────────────────────────────────────────────────────────────────

def test_an_existing_repo_without_weights_is_not_a_successful_push(monkeypatch):
    """⚠ repo 가 있는 것과 가중치가 올라간 것은 다르다. 실측으로 푸시는 커밋 **넷**으로
    나뉘고, `initial commit` 만 남기고 죽을 수 있다."""
    from app.services.cloud import rent

    class _Info:
        siblings = [type("S", (), {"rfilename": "README.md"})()]

    monkeypatch.setattr("huggingface_hub.HfApi.model_info", lambda self, r: _Info())
    assert rent._pushed("me/m") is False


def test_weights_present_means_no_rescue_needed(monkeypatch):
    from app.services.cloud import rent

    class _Info:
        siblings = [type("S", (), {"rfilename": "model.safetensors"})()]

    monkeypatch.setattr("huggingface_hub.HfApi.model_info", lambda self, r: _Info())
    assert rent._pushed("me/m") is True


def test_when_the_hub_cannot_be_asked_we_lean_towards_pulling(monkeypatch):
    """⚠ 모르면 **받는 쪽**으로 기운다. 전송비는 몇 센트지만 잃은 학습은 몇 시간이다."""
    from app.services.cloud import rent

    def _boom(self, r):
        raise RuntimeError("HF 안 닿음")

    monkeypatch.setattr("huggingface_hub.HfApi.model_info", _boom)
    assert rent._pushed("me/m") is False


def test_rescue_runs_before_the_teardown_not_after():
    """⚠ 순서가 전부다. 파기 뒤에 끌어오면 **기계가 이미 없다.**"""
    import inspect

    from app.services.cloud import procure

    src = inspect.getsource(procure.procure_and_train)
    assert src.index("rescue_if_needed(") < src.index("job.finish("), \
        "회수가 파기 뒤에 있다 — 그때는 기계가 없다"
