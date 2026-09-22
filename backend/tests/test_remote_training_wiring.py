"""원격 학습 배선 — **손으로 우회하던 것을 코드가 한다** (feature/vast-training.md §12-6).

2026-09-16 실기에서 한 바퀴를 돌리려고 사람이 손으로 한 일이 그대로 이 파일의 목록이다:
원격 인터프리터 심기 · HF 토큰 파일 심기 · 회수 repo 채우기 · SSH 권한 교정 ·
auto-tmux 끄기. 하나라도 빠지면 원격 학습은 **조용히** 못 돈다.
"""

import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────────────────────
# 러너가 자기 정체를 말한다
# ─────────────────────────────────────────────────────────────────────────────

def test_only_the_ssh_runner_says_it_is_remote():
    """⚠ `occupies_local_gpu` 로 유추하지 않는다 — 그건 다른 질문이다.

    "이 기계 GPU 를 먹나" 와 "다른 기계에서 도나" 는 지금 우연히 같은 답을 주지만,
    둘이 갈리는 러너(같은 기계의 컨테이너 러너 등)가 생기면 조용히 틀린다.
    """
    from app.services.training.runners.local import LocalRunner
    from app.services.training.runners.ssh import SSHRunner
    from app.services.training.runners.systemd import SystemdRunner

    # ⚠ `getattr(..., False)` 로 보지 않는다 — 선언을 빠뜨려도 통과해 버린다.
    #   러너들은 Protocol 을 **상속하지 않으므로**(구조적 타이핑) 기본값이 안 붙는다.
    assert SSHRunner.is_remote is True
    assert LocalRunner.is_remote is False
    assert SystemdRunner.is_remote is False


# ─────────────────────────────────────────────────────────────────────────────
# 원격 인터프리터
# ─────────────────────────────────────────────────────────────────────────────

def test_remote_python_is_an_absolute_path_into_the_image_venv():
    """⚠ `python` 만 두면 못 찾는다.

    학습 이미지의 venv 는 `/opt/venv` 인데 그 경로는 **SSH 로그인 셸 PATH 에 없다**
    (실측: `bash -lc 'echo $PATH'` → `/usr/local/sbin:/usr/local/bin:…`). 절대경로여야 한다.
    """
    from app.core.config import settings

    assert settings.train_remote_python.startswith("/"), "절대경로가 아니다"
    assert "/opt/venv/" in settings.train_remote_python


def test_local_run_keeps_using_the_local_interpreter():
    """원격 배선이 로컬 학습을 건드리면 안 된다."""
    from app.core.cli_mapping import build_train_args
    from app.core.config import settings

    args = build_train_args({"dataset_repo_id": "a/b", "policy_type": "act"})
    assert args[0] == settings.grpc_python


# ─────────────────────────────────────────────────────────────────────────────
# HF 토큰 — 원격에만, 그리고 스크립트를 통해서
# ─────────────────────────────────────────────────────────────────────────────

def test_token_goes_to_the_remote_but_never_to_a_local_run(tmp_path, monkeypatch):
    """⚠ 로컬 학습에 토큰을 실을 이유가 없다 — 자격증명은 필요한 곳에만 간다."""
    from app.routers import training as mod

    monkeypatch.setattr("huggingface_hub.get_token", lambda: "hf_TESTTOKEN")

    local = mod._train_env("bf16") or {}
    assert "HF_TOKEN" not in local
    assert local["ACCELERATE_MIXED_PRECISION"] == "bf16"

    remote = mod._train_env("bf16", remote=True) or {}
    assert remote["HF_TOKEN"] == "hf_TESTTOKEN"


def test_missing_token_does_not_crash_the_start(tmp_path, monkeypatch):
    """토큰이 없어도 시작 자체는 막지 않는다 — 공개 데이터셋만 쓸 수도 있다."""
    from app.routers import training as mod

    monkeypatch.setattr("huggingface_hub.get_token", lambda: None)
    env = mod._train_env("off", remote=True)
    assert env is None or "HF_TOKEN" not in env


def test_the_runner_exports_env_inside_the_script_not_the_ssh_session():
    """⚠ **여기가 토큰이 실제로 닿는 유일한 경로다.**

    tmux 서버는 기동 시점 환경을 얼린다(실측: 서버 기동 전 변수는 새 세션에 보이고
    후는 안 보인다). 그래서 ssh 세션 env 로는 학습 프로세스에 못 닿는다. `_build_script`
    가 스크립트 **안에서** `export` 하고 그 스크립트가 tmux 안에서 돌기 때문에 닿는다.

    ⚠ 소스를 grep 하지 않고 **함수를 부른다** — 그래야 리네임·포매팅에 안 깨지고,
    반대로 동작이 깨지면 반드시 걸린다.
    """
    from app.services.training.runners.base import TrainJobSpec
    from app.services.training.runners.ssh import SSHRunner

    body = SSHRunner(host="dummy")._build_script(
        TrainJobSpec(cmd=["/opt/venv/bin/python", "-m", "x"],
                     env={"HF_TOKEN": "hf_x"}, total_steps=1, output_dir="/o"))
    lines = body.splitlines()
    exp = next(i for i, ln in enumerate(lines) if ln == "export HF_TOKEN=hf_x")
    cmd = next(i for i, ln in enumerate(lines) if ln.startswith("/opt/venv/bin/python"))
    assert exp < cmd, "export 가 명령보다 뒤에 있으면 학습이 토큰 없이 돈다"


# ─────────────────────────────────────────────────────────────────────────────
# 회수 경로
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def remote_client(monkeypatch):
    """원격 러너가 붙은 게이트웨이. **실제로 호출해 본다.**

    ⚠ 소스를 grep 하는 검사로는 **조건이 뒤집혀도 통과한다** — 실제로 그랬다.
    `if not params.get(...)` 에서 `not` 이 빠져 "repo_id 가 **있을 때**" 거절하게
    됐는데, 문구를 찾는 테스트는 초록이었다.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.training import train_manager

    started: dict = {}

    class _FakeRemote:
        is_remote = True
        occupies_local_gpu = False
        pid = None

    async def _fake_start(args, **kw):
        started["args"] = list(args)
        started["env"] = kw.get("env_extra")

    monkeypatch.setattr(train_manager, "runner", _FakeRemote())
    monkeypatch.setattr(train_manager, "start", _fake_start)
    monkeypatch.setattr("app.routers.training.require_idle", lambda *a, **k: None)
    monkeypatch.setattr("app.routers.training._require_push_permission",
                        lambda *a, **k: _noop())
    monkeypatch.setattr("huggingface_hub.get_token", lambda: "hf_TESTTOKEN")
    return TestClient(app), started


async def _noop():
    return None


_BODY = {"dataset_repo_id": "a/b", "policy_type": "act", "steps": 10}


def test_remote_start_is_refused_without_a_push_target(remote_client):
    """⚠ `policy_repo_id` 가 없으면 `cli_mapping` 이 `--policy.push_to_hub=false` 를
    강제한다 — 학습은 **멀쩡히 끝나고 가중치만 사라진다.**

    실측(§12-4)으로 푸시는 **종료 시점 한 번뿐**이라 그 한 번이 유일한 기회다.
    """
    client, _ = remote_client
    r = client.post("/api/training/start", json=_BODY)
    assert r.status_code == 400
    assert "결과를 가져올 수 없습니다" in r.json()["detail"]


def test_remote_start_succeeds_with_a_push_target(remote_client):
    """⚠ 거절 조건이 **뒤집히면** 멀쩡한 요청이 전부 막힌다. 반대 방향도 본다."""
    client, started = remote_client
    r = client.post("/api/training/start", json={**_BODY, "policy_repo_id": "me/m"})
    assert r.status_code == 200, r.text
    # 원격 인터프리터가 실제로 실렸는가 — argv[0] 이 로컬 절대경로면 즉사한다
    from app.core.config import settings
    assert started["args"][0] == settings.train_remote_python
    assert started["env"]["HF_TOKEN"] == "hf_TESTTOKEN"


def test_local_start_still_allows_an_empty_repo_id():
    """로컬 학습은 Hub 없이 돌 수 있어야 한다 — 그게 기본 사용법이다."""
    from app.core.cli_mapping import build_train_args

    args = build_train_args({"dataset_repo_id": "a/b", "policy_type": "act"})
    assert "--policy.push_to_hub=false" in args


# ─────────────────────────────────────────────────────────────────────────────
# 즉사를 정상 완주와 구별한다
# ─────────────────────────────────────────────────────────────────────────────

def test_log_stream_reads_from_the_beginning():
    """⚠ `tail -n 0` 이면 1초 안에 죽는 실패가 **화면에서 정상 완주와 같아진다.**

    exit 127 · draccus 인자 오류 · unknown policy type 은 파일에 에러를 다 쓰고 죽는데,
    새 줄이 없어 `_EXIT_MARK` 분기를 못 타고 `_finish(IDLE)` 로 끝난다. IDLE 은 정상
    완주와 같은 값이다. `start()` 가 바로 위에서 `: > {log}` 로 비우므로 처음부터
    읽어도 이전 실행분이 안 섞인다.
    """
    src = (_REPO / "backend/app/services/training/runners/ssh.py").read_text()
    start = src.split("    async def start(", 1)[1].split("\n    async def stop", 1)[0]
    assert ": > {log}" in start, "로그를 비우지 않는다"
    assert "_start_log_stream(from_start=True)" in start, "처음부터 읽지 않는다"


# ─────────────────────────────────────────────────────────────────────────────
# 이미지 — 이게 없으면 아무도 접속할 수 없다
# ─────────────────────────────────────────────────────────────────────────────

BOOTSTRAP = _REPO / "deploy/train/bootstrap.sh"


def test_bootstrap_is_valid_shell():
    r = subprocess.run(["bash", "-n", str(BOOTSTRAP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:400]


def _bootstrap_parts() -> tuple[str, str]:
    """`(즉시 교정 함수 본문, 배경 루프 본문)`.

    ⚠ 파일 전체에서 문자열을 찾으면 **둘 중 하나를 지워도 통과한다.** 구간을 갈라서 본다.
    """
    src = BOOTSTRAP.read_text()
    fn = src.split("piper_fix_ssh() {", 1)[1].split("\n}", 1)[0]
    loop = re.search(r"nohup sh -c '(.*?)' 9>&-", src, re.S)
    assert loop, "배경 루프가 없다 — 키 주입이 늦으면 교정 기회를 놓친다"
    return fn, loop.group(1)


@pytest.mark.parametrize("needle, why", [
    ("chmod 600 /root/.ssh/authorized_keys",
     "Vast 가 심은 키의 권한이 우리 sshd(StrictModes yes)를 통과 못 한다"),
    ("chmod 700 /root/.ssh", "디렉토리 권한도 본다"),
    ("/root/.no_auto_tmux",
     "Vast 가 .bashrc 에서 모든 SSH 세션을 tmux 로 감싼다 — SSHRunner 가 통째로 막힌다"),
])
def test_both_the_immediate_fix_and_the_retry_loop_do_it(needle, why):
    """⚠ **둘 다 없으면 이 이미지로 띄운 인스턴스에 접속할 수 없다** (2026-09-16 실기).

    즉시 교정은 onstart 가 키 주입보다 **뒤**일 때를 덮고, 배경 루프는 **앞**일 때를 덮는다.
    어느 한쪽만 남아도 절반만 덮인다.
    """
    fn, loop = _bootstrap_parts()
    assert needle in fn, f"즉시 교정에 없다: {why}"
    assert needle in loop, f"배경 루프에 없다: {why}"


def test_the_immediate_fix_is_actually_called():
    """⚠ 함수만 남기고 **호출을 지워도** 문자열 검사는 통과한다."""
    assert re.search(r"^piper_fix_ssh$", BOOTSTRAP.read_text(), re.M), \
        "piper_fix_ssh 를 정의만 하고 부르지 않는다"


def test_the_background_loop_does_not_inherit_the_lock():
    """⚠ 배경 루프가 `flock` fd 9 를 물려받으면 **3분 내내 락이 안 풀린다.**

    bootstrap 은 두 곳에서 불릴 수 있고(Vast onstart + 이미지 CMD) 그러면 두 번째가
    정확히 그만큼 멈춘다. 실측으로 `9>&-` 를 붙이면 2회차가 180초 → 0초가 된다.
    """
    assert "9>&-" in BOOTSTRAP.read_text(), "배경 루프에 락 fd 를 물려준다"


def test_the_background_loop_body_is_valid_shell():
    """⚠ `bash -n` 은 `sh -c '...'` **따옴표 안을 파싱하지 않는다** — 통째로 문자열이다."""
    _, loop = _bootstrap_parts()
    r = subprocess.run(["sh", "-n"], input=loop, capture_output=True, text=True)
    assert r.returncode == 0, f"배경 루프가 셸 문법 오류: {r.stderr[:300]}"


def test_ssh_fix_runs_before_the_ready_shortcut():
    """⚠ full 이미지는 `.ready` 가 이미 있어 bootstrap 이 **즉시 빠져나간다.**

    교정을 그 아래 두면 full 에서는 한 번도 안 돌고, full 이야말로 우리가 학습에
    쓰려던 변종이다.
    """
    src = BOOTSTRAP.read_text()
    assert src.index("no_auto_tmux") < src.index('if [ -f "$READY" ]'), \
        "SSH 교정이 READY 조기탈출 뒤에 있다 — full 에서 안 돈다"


# ─────────────────────────────────────────────────────────────────────────────
# 자격증명이 남의 기계에 남지 않게
#
# ⚠ 전제를 분명히 해 둔다: 임대 서버의 **호스트 운영자는 `/proc/<pid>/environ` 으로
# 어차피 토큰을 읽는다.** 아래 검사가 막는 것은 *같은 기계의 다른 사용자*와 *파기하지
# 못한 인스턴스에 무기한 남는 것* 이다. 호스트에 대한 답은 권한이 아니라 **권한을 좁힌
# 토큰**이고 그건 §10 결정 3 이다.
# ─────────────────────────────────────────────────────────────────────────────

def test_the_remote_script_is_created_private_not_chmodded_afterwards():
    """⚠ `cat > f && chmod 600 f` 는 **늦다.**

    파일은 먼저 umask 대로(보통 644) 만들어지고 **토큰이 다 써진 뒤에야** 조여진다.
    게다가 이전 실행이 남긴 파일 위에 덮어쓰면 그 파일의 모드를 승계한다.
    """
    src = (_REPO / "backend/app/services/training/runners/ssh.py").read_text()
    start = src.split("    async def start(", 1)[1].split("\n    def _wipe_script", 1)[0]
    assert "umask 077" in start, "생성 시점에 권한을 조이지 않는다"
    assert "rm -f" in start, "이전 파일의 모드를 승계할 수 있다"


def test_the_script_deletes_itself_so_an_orphan_does_not_keep_the_token():
    """⚠ 게이트웨이 쪽에서 지우면 **그 순간 게이트웨이가 죽어 있을 때** 못 지운다.

    그게 정확히 걱정되는 경우(고아 인스턴스)다. 스크립트가 스스로 지우면 그것까지 덮는다.
    종료 마커를 찍은 **뒤**라 상태 채널은 이미 흘렀다.
    """
    from app.services.training.runners.base import TrainJobSpec
    from app.services.training.runners.ssh import SSHRunner

    body = SSHRunner(host="dummy")._build_script(
        TrainJobSpec(cmd=["/opt/venv/bin/python", "-m", "x"],
                     env={"HF_TOKEN": "hf_SECRET"}, total_steps=1, output_dir="/o"))
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert lines[-1] == 'rm -f "$0"', f"마지막 줄이 자기 삭제가 아니다: {lines[-1]!r}"
    assert "__PIPER_EXIT__" in lines[-2], "종료 마커보다 먼저 지우면 상태 채널을 잃는다"


def test_stopping_also_wipes_the_script_because_it_never_reached_the_end():
    """[중지] 로 죽인 스크립트는 자기 삭제 줄에 **도달하지 못한다.**"""
    src = (_REPO / "backend/app/services/training/runners/ssh.py").read_text()
    stop = src.split("    async def stop(", 1)[1].split("\n    def ", 1)[0]
    assert "_wipe_script()" in stop


def test_token_lookup_also_honours_the_env_var(monkeypatch, tmp_path):
    """⚠ `HF_TOKEN` 환경변수로 로그인한 게이트웨이는 토큰 **파일이 없다.**

    파일만 보면 조용히 빈손으로 보내고, 실패는 임대 GPU 가 이미 도는 뒤에 드러난다.
    """
    from app.routers import training as mod

    monkeypatch.setattr("app.services.hub_client.token_path",
                        lambda: tmp_path / "no-such-file")
    monkeypatch.setattr("huggingface_hub.get_token", lambda: "hf_FROM_ENV")
    env = mod._train_env("off", remote=True) or {}
    assert env.get("HF_TOKEN") == "hf_FROM_ENV"


def test_readiness_does_not_hand_out_the_account_email():
    """⚠ 게이트웨이 로그인은 켜야 걸리고 기본은 꺼져 있다 — 꺼져 있으면 LAN 의 누구나 읽는다.

    화면이 쓰는 것은 크레딧뿐이다. 안 쓰는 것을 내보낼 이유가 없다.
    """
    src = (_REPO / "backend/app/routers/cloud.py").read_text()
    body = src.split("async def readiness", 1)[1]
    assert '"account": account' not in body, "계정 객체(이메일 포함)를 그대로 내보낸다"
    assert '"credit"' in body, "화면이 쓰는 크레딧까지 없애면 안 된다"


def test_restore_skips_export_lines_so_the_token_never_becomes_an_argument():
    """⚠ `restore()` 는 스크립트를 다시 읽어 인자를 되찾는다.

    토큰 줄(`export HF_TOKEN=…`)을 명령으로 오인하면 인자가 통째로 망가지고,
    최악에는 토큰이 argv 로 나간다.
    """
    src = (_REPO / "backend/app/services/training/runners/ssh.py").read_text()
    body = src.split("    def restore(", 1)[1].split("\n    def ", 1)[0]
    assert '"export "' in body or "'export '" in body, "export 줄을 안 거른다"


# ─────────────────────────────────────────────────────────────────────────────
# ⚠ 시작 경로는 **둘**이다 — 하나만 고치면 다른 쪽 사용자만 깨진다
#
# 실제로 그랬다: `_amp_env` 를 `_train_env` 로 바꾸면서 `/start` 만 고치고
# `/start-custom` 을 놓쳐 **100% NameError(500)** 가 됐다. 위의 테스트들은 전부
# 초록이었다 — 아무도 `/start-custom` 을 지나가지 않았기 때문이다.
# ─────────────────────────────────────────────────────────────────────────────

_TRAINING_PY = _REPO / "backend/app/routers/training.py"


def test_no_dangling_reference_to_the_old_env_builder():
    """이름을 바꿨으면 **모든** 호출부가 따라와야 한다."""
    src = (_REPO / "backend/app").rglob("*.py")
    bad = [p for p in src if "_amp_env" in p.read_text()]
    assert not bad, f"사라진 _amp_env 를 아직 부른다: {[str(p) for p in bad]}"


@pytest.mark.parametrize("handler", ["start_training", "start_training_custom"])
def test_both_start_paths_carry_the_remote_wiring(handler):
    """⚠ 한쪽만 배선하면 그쪽을 안 쓰는 사람만 조용히 못 돈다.

    `/start-custom` 은 "직접 편집한 인자" 경로다. 원본 주석이 이미 같은 말을 한다 —
    *"오히려 손으로 고친 쪽이 push_to_hub 를 끄는 걸 잊기 쉽다"*.
    """
    src = _TRAINING_PY.read_text()
    body = src.split(f"async def {handler}", 1)[1].split("\n@router", 1)[0]
    assert "is_remote" in body, f"{handler}: 원격 여부를 안 본다"
    assert "_train_env(" in body, f"{handler}: 토큰을 실을 수 없다"
    assert "train_remote_python" in body, f"{handler}: 원격 인터프리터를 안 쓴다"
    assert "결과를 가져올 수 없습니다" in body, f"{handler}: 회수 경로 가드가 없다"


def test_custom_args_are_not_silently_rewritten():
    """⚠ 사람이 일부러 쓴 경로를 조용히 덮어쓰면 "내가 쓴 것과 다른 게 돌았다" 가 된다.

    preview 가 넣어 준 로컬 인터프리터일 때만 바꾸고, 바꿨다는 사실을 응답에 적는다.
    """
    src = _TRAINING_PY.read_text()
    body = src.split("async def start_training_custom", 1)[1].split("\n@router", 1)[0]
    assert "== settings.grpc_python" in body, "무조건 덮어쓴다 — 조건이 없다"
    assert '"note"' in body, "바꿨다는 사실을 응답에 안 적는다"


# ─────────────────────────────────────────────────────────────────────────────
# Hub 조회 — "없음" 과 "고장" 을 구별해야 한다
#
# ⚠ §5 규율: **업로드 검증 전에는 provision 하지 않는다.** 그 검증이 "안 올라감" 과
# "Hub 장애" 를 구별 못 하면, 사람은 둘 다 보고 인스턴스를 띄우거나 둘 다 보고 안 띄운다.
# 예전에는 404 가 **500** 으로 나갔다.
# ─────────────────────────────────────────────────────────────────────────────

class _HttpErr(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.response = type("R", (), {"status_code": code})()


@pytest.mark.parametrize("upstream, expect", [
    (404, 404),   # Hub 에 없다 → 그대로 없다고
    (401, 401),   # 로그인이 안 됐다 → 권한 문제로
    (403, 403),
    (500, 502),   # Hub 가 아프다 → 우리가 아픈 게 아니라 상류가 아프다
    (None, 502),  # 네트워크 등 status 가 없는 예외
])
def test_hub_dataset_lookup_maps_upstream_status(monkeypatch, upstream, expect):
    from fastapi.testclient import TestClient

    from app.main import app

    async def boom(repo_id):
        raise _HttpErr(upstream) if upstream else RuntimeError("연결 실패")

    monkeypatch.setattr("app.services.hub_client.get_dataset_info", boom)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/hub/datasets/someone/nope")
    assert r.status_code == expect, r.text


def test_the_not_found_message_admits_it_could_be_private(monkeypatch):
    """⚠ 실측: **토큰 없이** 비공개 repo 를 조회하면 401 이 온다.

    "없습니다" 라고 단정하면 사람이 이름을 의심하며 헤맨다 — 로그인을 의심해야 하는데.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    async def boom(repo_id):
        raise _HttpErr(401)

    monkeypatch.setattr("app.services.hub_client.get_dataset_info", boom)
    c = TestClient(app, raise_server_exceptions=False)
    detail = c.get("/api/hub/datasets/x/y").json()["detail"]
    assert "로그인" in detail


def test_hf_endpoint_also_reaches_the_remote(monkeypatch):
    """사내 미러를 쓰는 설정이면 원격도 같은 곳을 봐야 한다 — 기본 Hub 로 가면 못 찾는다."""
    from app.core.config import settings
    from app.routers import training as mod

    monkeypatch.setattr("huggingface_hub.get_token", lambda: "hf_x")
    monkeypatch.setattr(settings, "hf_endpoint", "https://mirror.example")
    assert (mod._train_env("off", remote=True) or {})["HF_ENDPOINT"] == "https://mirror.example"


def test_status_reports_the_live_runner_not_a_stale_record():
    """⚠ `JobRecord.runner` 는 `_sync_record()` 가 마지막으로 쓴 **과거 스냅샷**이다.

    실측으로 어제 학습의 `"systemd"` 가 그대로 남아 있었다. 원격이냐에 인터프리터·
    토큰·회수 가드가 매달려 있으므로 화면은 **지금** 값을 봐야 한다.
    """
    from app.services.training import train_manager

    s = train_manager.get_status()
    assert "remote" in s and "runner" in s
    assert s["remote"] is bool(getattr(train_manager.runner, "is_remote", False))


def test_an_active_remote_record_is_not_wiped_when_we_fell_back_to_local(monkeypatch):
    """⚠ **"못 물어봤다" 와 "끝났다" 는 다르다.**

    `_default_runner()` 는 임포트 시점에 SSH 프로브를 딱 한 번 한다. 그때 네트워크가
    흔들리면 프로세스 수명 내내 로컬이 되고, 그 상태로 레코드를 IDLE 로 덮으면
    **원격 tmux 는 학습을 계속하는데 화면에서 사라지고 임대 GPU 는 계속 과금된다.**
    지금은 파기 코드도 예산 상한도 없어(§11-0) 사람이 눈치채는 것이 유일한 가드다.
    """
    from app.services.training import train_manager as tm

    rec = type("R", (), {"is_active": True, "runner": "ssh", "history": None})()
    monkeypatch.setattr(tm.registry, "get", lambda _j: rec)
    monkeypatch.setattr(tm.runner, "restore", lambda: None)
    monkeypatch.setattr(tm.tracker, "reset", lambda: None)
    wiped: list = []
    monkeypatch.setattr(tm, "_sync_record", lambda **kw: wiped.append(kw))

    assert tm.restore_running_process() is False
    assert not wiped, "원격 레코드를 IDLE 로 덮었다 — 살아있는 학습을 화면에서 지운다"
