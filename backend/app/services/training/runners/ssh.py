"""SSH 학습 러너 — 원격 GPU 박스에서 학습을 돌린다 (ROADMAP 3b-3.5-4).

`LocalRunner`/`SystemdRunner` 와 **같은 `TrainRunner` 이음매**에 붙는다. 위쪽
(`TrainManager` → 메트릭 파서 → WS)은 로그가 어디서 왔는지 모른다 — `_METRIC_RE`
는 journald 에서 온 줄이든 원격 `tail` 에서 온 줄이든 똑같이 읽는다.

## 소유자는 원격 tmux 세션이다

로컬 subprocess 도 systemd 유닛도 아니므로 **PID 가 없다**(`pid` 는 None 을
돌려준다). 대신 세션 이름이 핸들이다:

    piper-train-<job_id>

살아있는지는 `tmux has-session` 이 답한다. `SystemdProcess` 가 `systemctl
is-active` 에게 묻는 것과 같은 규율 — 캐시를 진실로 삼지 않는다. 다만 여기서는
질문 한 번이 SSH 왕복(수십~수백 ms)이라 `state` 를 부를 때마다 물어볼 수 없다.
그래서 두 겹으로 본다:

1. **끝났다는 신호를 원격이 밀어준다** — 스크립트가 마지막에 종료 코드를 로그에
   찍고(`_EXIT_MARK`), 로그를 따라 읽는 스레드가 그 줄을 보면 상태를 내린다.
2. 그 신호를 놓쳤을 때(연결이 끊겨 tail 이 죽는 경우)를 위해 **TTL 을 둔 확인**을
   덧댄다. 폴링이 아니라 최후 방어선이다.

## 왜 `injected_env()` 를 안 쓰는가

`ProcessManager`/`SystemdProcess` 가 자식에게 넣어주는 것(버스 주소·logfix
PYTHONPATH·HF 엔드포인트)은 **이 기계의 사실이다.** 원격에 그대로 넘기면 버스
주소가 원격에서 자기 자신의 localhost 를 가리키고, logfix 경로는 아예 없다.
원격 학습은 버스를 안 쓴다 — 진행률은 로그로만 온다. 그래서 `spec.env` 만 넘긴다.

## 범위

**전송은 안 한다.** 데이터셋·산출물 회수는 cloud-training 5~7단계다. 지금은
원격이 데이터셋을 이미 볼 수 있는 경우(사내 GPU 서버, NFS, 미리 복사)를 다룬다 —
`static_ssh` 프로바이더가 바로 그 경우이고 문서가 **1순위 개발 대상**으로 꼽았다.
"""

import logging
import shlex
import subprocess
import threading
import time
from collections.abc import Callable

from app.services.process_manager import ProcessState
from app.services.training.spec import TrainJobSpec

logger = logging.getLogger(__name__)

__all__ = ["SSHRunner", "available"]

# 원격 스크립트가 끝나면서 찍는 줄. 로그 스트림이 곧 상태 채널이 된다.
_EXIT_MARK = "__PIPER_EXIT__"

# `has-session` 을 다시 물어보기까지의 최소 간격. 위 마커를 놓쳤을 때만 쓰인다.
_RECHECK_S = 5.0

_SSH_OPTS = [
    "-o", "BatchMode=yes",           # 암호를 물으면 그 자리에서 실패한다 — 키가 전제다
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=accept-new",
]


def _ssh_argv(host: str, remote: str, keepalive: bool = False) -> list[str]:
    opts = list(_SSH_OPTS)
    if keepalive:
        # 로그를 따라 읽는 긴 연결이 조용히 끊기면 학습이 끝난 줄 안다
        opts += ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    return ["ssh", *opts, host, remote]


def _run(host: str, remote: str, timeout: float = 15.0,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(_ssh_argv(host, remote), input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def available(host: str = "", workdir: str = "") -> tuple[bool, str]:
    """이 러너를 쓸 수 있는가. **못 쓰면 사유를 말한다.**

    조용히 `LocalRunner` 로 떨어지면 "원격 GPU 에서 돈다"고 믿는데 실제로는 이
    기계의 GPU 를 먹고 있는 상태가 된다 — GPU 경합 가드가 통째로 거짓말이 된다.
    """
    from app.core.config import settings

    host = host or settings.train_ssh_host
    workdir = workdir or settings.train_ssh_workdir
    if not host:
        return False, "원격 학습 호스트가 설정되지 않았습니다 (PIPER_TRAIN_SSH_HOST)"
    try:
        # 접속·tmux·작업 디렉토리를 **한 번에** 확인한다. 왕복이 비싸다.
        r = _run(host, f"command -v tmux >/dev/null && mkdir -p {shlex.quote(workdir)}")
    except subprocess.TimeoutExpired:
        return False, f"{host} 에 접속할 수 없습니다 (시간 초과)"
    except FileNotFoundError:
        return False, "ssh 가 없습니다"
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        why = err[-1] if err else f"종료 코드 {r.returncode}"
        return False, f"{host}: {why}"
    return True, "OK"


class SSHRunner:
    """`TrainRunner` 구현. 인터페이스는 `LocalRunner` 와 같다."""
    # ⚠ **원격 GPU 다.** 배타 가드가 이걸 보고 추론·녹화를 안 막는다.
    occupies_local_gpu = False
    is_remote = True


    def __init__(self, job_id: str = "local", host: str = "", workdir: str = "") -> None:
        from app.core.config import settings

        self.host = host or settings.train_ssh_host
        self.workdir = workdir or settings.train_ssh_workdir
        self.session = f"piper-train-{job_id}"
        self._state = ProcessState.IDLE
        self._on_log: Callable[[str], None] | None = None
        self._on_state: Callable[[ProcessState], None] | None = None
        self._log_thread: threading.Thread | None = None
        self._log_proc: subprocess.Popen | None = None
        self._log_stop = threading.Event()
        self._checked_at = 0.0

    # ── 원격 경로 ──

    @property
    def _script(self) -> str:
        return f"{self.workdir}/{self.session}.sh"

    @property
    def _log(self) -> str:
        return f"{self.workdir}/{self.session}.log"

    # ── 상태 ──

    @property
    def state(self) -> ProcessState:
        """세션에게 **물어본다.** 단, 매번은 아니다 — SSH 왕복이 비싸다.

        보통은 로그 스트림이 `_EXIT_MARK` 로 먼저 알려준다. 여기 확인이 실제로
        일하는 경우는 그 연결이 끊겨 마커를 못 본 때다.
        """
        if self._state in (ProcessState.RUNNING, ProcessState.STARTING):
            now = time.time()
            if now - self._checked_at >= _RECHECK_S:
                self._checked_at = now
                if not self._alive():
                    self._finish(ProcessState.IDLE)
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state in (ProcessState.RUNNING, ProcessState.STARTING)

    @property
    def pid(self) -> int | None:
        """**항상 None.** 원격 PID 는 이 기계에서 아무 의미가 없다.

        estopd 는 PID 를 SIGKILL 하는데 그 PID 가 여기 있는 남의 프로세스일 수도
        있다. 학습은 `ESTOP_TARGETS` 가 아니라 애초에 대상이 아니지만, 그 이유로
        기대면 안 되므로 러너 쪽에서 확실히 막는다.
        """
        return None

    def _alive(self) -> bool:
        try:
            return _run(self.host, f"tmux has-session -t {shlex.quote(self.session)}"
                        ).returncode == 0
        except Exception as exc:
            # 네트워크가 잠깐 끊긴 것과 학습이 끝난 것은 다르다 — 살아있다고 본다.
            # 틀리면 다음 확인이 잡는다. 반대로 틀리면 진행 중인 학습이 화면에서
            # 사라진다.
            logger.warning("원격 세션 확인 실패 (%s): %s", self.session, exc)
            return True

    def _set_state(self, state: ProcessState) -> None:
        self._state = state
        if self._on_state:
            self._on_state(state)

    def _finish(self, state: ProcessState) -> None:
        self._stop_log_stream()
        self._set_state(state)

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self._on_log = cb

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        self._on_state = cb

    # ── 실행 ──

    def _build_script(self, spec: TrainJobSpec) -> str:
        """원격에서 돌 셸 스크립트.

        명령을 tmux 인자로 밀어넣으면 따옴표가 셸을 세 겹(로컬 → ssh → tmux)
        지나며 깨진다. 파일로 보내면 그 문제가 통째로 사라지고, **`restore()` 가
        인자를 되찾는 경로**도 같이 생긴다 (systemd 의 `ExecStart` 자리).
        """
        lines = ["#!/usr/bin/env bash", "set -o pipefail"]
        for k, v in (spec.env or {}).items():
            lines.append(f"export {k}={shlex.quote(str(v))}")
        # 진행률 복원용. 로그에는 안 나오는 사실이라 여기 적어둔다.
        lines.append(f"# piper-total-steps: {spec.total_steps}")
        lines.append(f"# piper-output-dir: {spec.output_dir}")
        # ⚠ **가장 안쪽 가드다** (feature/vast-training.md §6-1). 사람·게이트웨이·인터넷이
        #   전부 사라져도 학습 프로세스는 반드시 끝난다 — 임대 GPU 는 우리가 안 보는
        #   동안에도 시간당으로 과금되고, 지금은 이것 말고 자동으로 멈추는 것이 없다.
        #   `timeout` 은 coreutils 라 이미지에 있다(실측: 학습 이미지에서 확인).
        #
        #   ⚠ 종료 코드를 **삼키지 않는다.** `timeout` 이 죽이면 124 가 그대로 아래
        #   마커로 흘러, 화면이 "끝났다" 가 아니라 "시간 초과로 끊겼다" 를 말할 수 있다.
        cmd = shlex.join(spec.cmd)
        if spec.max_hours and spec.max_hours > 0:
            cmd = f"timeout {int(spec.max_hours * 3600)} {cmd}"
        lines.append(cmd)
        # ⚠ 종료 코드를 **로그로** 흘린다. 이게 상태 채널이다 — 이 줄이 없으면
        #   학습이 끝나도 화면이 계속 "실행 중" 이고, 다음 학습이 막힌다.
        lines.append(f'echo "{_EXIT_MARK} $?"')
        # ⚠ **스크립트가 자기를 지운다.** 여기에 자격증명(HF 토큰)이 `export` 로 들어
        #   있는데, 남겨 두면 남의 기계에 계속 남는다 — 인스턴스를 파기하지 못하면
        #   무기한이다. 게이트웨이 쪽에서 지우면 그 순간 게이트웨이가 죽어 있을 때
        #   못 지운다. **스스로 지우면 그 경우까지 덮는다.**
        #   종료 마커를 찍은 **뒤**라 상태 채널은 이미 흘렀고, 세션도 곧 끝나므로
        #   `restore()` 가 이 파일을 찾을 이유가 없다(살아있을 때만 읽는다).
        lines.append('rm -f "$0"')
        return "\n".join(lines) + "\n"

    async def start(self, spec: TrainJobSpec) -> None:
        ok, why = available(self.host, self.workdir)
        if not ok:
            raise RuntimeError(f"SSH 러너를 쓸 수 없습니다: {why}")
        if self._alive():
            raise RuntimeError(f"원격 세션이 이미 실행 중입니다: {self.session}")

        script, log = shlex.quote(self._script), shlex.quote(self._log)
        # ⚠ **이 스크립트는 자격증명을 담는다** — 원격 학습이면 `spec.env` 에 HF 토큰이
        #   실리고 `_build_script` 가 그걸 `export` 로 적는다.
        #
        #   ⚠ `cat >` 뒤에 `chmod` 를 붙이면 **늦다.** 파일은 먼저 umask 대로(보통 644)
        #   만들어지고 토큰이 다 써진 뒤에야 조여진다. 게다가 이전 실행이 남긴 파일 위에
        #   덮어쓰면 그 파일의 모드를 승계한다. 그래서 **지우고 umask 로** 만든다.
        #
        #   ⚠ 이걸로 막는 것은 **같은 기계의 다른 사용자**다. 임대 서버의 호스트 운영자는
        #   `/proc/<pid>/environ` 으로 어차피 읽는다 — 거기에 대한 답은 권한이 아니라
        #   **권한을 좁힌 토큰**이다 (§10 결정 3).
        r = _run(self.host,
                 f"rm -f {script} && (umask 077 && cat > {script}) && : > {log}",
                 stdin=self._build_script(spec))
        if r.returncode != 0:
            raise RuntimeError(f"원격 스크립트 전송 실패: {(r.stderr or '').strip()}")

        self._set_state(ProcessState.STARTING)
        # `tmux new-session -d` 는 세션을 띄우고 바로 돌아온다 — 학습을 기다리지 않는다.
        #
        # ⚠ **리다이렉션은 tmux 가 실행할 명령 안에 있어야 한다.** 밖에 두면
        #   `> log` 가 tmux **클라이언트**에 걸려 (즉시 끝나고 아무것도 안 찍는다)
        #   학습 출력은 세션의 pty 로 가버린다. 로그 파일이 0바이트로 남고
        #   화면에는 아무 줄도 안 온다 — 실기에서 이렇게 조용히 실패했다.
        r = _run(self.host,
                 f"tmux new-session -d -s {shlex.quote(self.session)} "
                 + shlex.quote(f"bash {script} > {log} 2>&1"))
        if r.returncode != 0:
            self._set_state(ProcessState.ERROR)
            raise RuntimeError(f"원격 세션 시작 실패: {(r.stderr or '').strip()}")

        self._set_state(ProcessState.RUNNING)
        self._checked_at = time.time()
        # ⚠ **처음부터 읽는다.** `tail -n 0` 이면 1초 안에 죽는 실패(exit 127 ·
        #   draccus 인자 오류 · unknown policy type)가 파일에 다 쓰여 있는데도 **새 줄이
        #   없어** `_EXIT_MARK` 분기를 못 타고 `_finish(IDLE)` 로 끝난다. IDLE 은
        #   **정상 완주와 같은 값**이라 화면에서 구별이 안 된다.
        #   바로 위에서 `: > {log}` 로 파일을 비웠으므로 이전 실행분이 섞이지 않는다.
        self._start_log_stream(from_start=True)
        logger.info("원격 학습 시작: %s @ %s", self.session, self.host)

    def _wipe_script(self) -> None:
        """자격증명이 든 스크립트를 원격에서 지운다.

        ⚠ 학습이 끝나면 이 파일이 할 일은 끝났는데, 남겨 두면 토큰이 **남의 기계에
        계속 있는다** — 인스턴스를 파기하지 못한 경우(고아)에는 무기한이다.
        실패는 삼킨다: 지우기 실패가 학습 종료를 막을 이유는 없다.
        """
        try:
            _run(self.host, f"rm -f {shlex.quote(self._script)}")
        except Exception as exc:                                    # noqa: BLE001
            logger.warning("원격 스크립트를 지우지 못했습니다 (%s): %s", self.session, exc)

    async def stop(self) -> None:
        try:
            _run(self.host, f"tmux kill-session -t {shlex.quote(self.session)}")
        except Exception as exc:
            logger.warning("원격 세션 종료 실패 (%s): %s", self.session, exc)
        self._wipe_script()
        self._finish(ProcessState.IDLE)

    # ── 로그 ──

    def _start_log_stream(self, from_start: bool = False) -> None:
        """원격 로그를 따라 읽어 콜백으로 넘긴다.

        `from_start` 는 **재부착할 때** 쓴다 — 게이트웨이가 없던 동안의 로그를
        처음부터 다시 읽어야 화면의 진행률이 이어진다 (`SystemdProcess` 가
        journald 로 하는 것과 같다).

        ⚠ **스레드로 돈다. asyncio 가 아니다.** `restore()` 는 `TrainRunner`
        프로토콜상 **동기** 메서드라 이벤트 루프 밖에서 불릴 수 있다.
        """
        if self._log_thread and self._log_thread.is_alive():
            return

        def _pump() -> None:
            follow = f"tail -n {'+1' if from_start else '0'} -f {shlex.quote(self._log)}"
            try:
                proc = subprocess.Popen(_ssh_argv(self.host, follow, keepalive=True),
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
            except Exception as exc:
                logger.warning("원격 로그 연결 실패 (%s): %s", self.session, exc)
                return
            self._log_proc = proc
            try:
                for raw in proc.stdout or ():
                    if self._log_stop.is_set():
                        break
                    line = raw.rstrip("\n")
                    if line.startswith(_EXIT_MARK):
                        code = line.split(maxsplit=1)[-1]
                        logger.info("원격 학습 종료: %s (code=%s)", self.session, code)
                        self._set_state(ProcessState.IDLE if code == "0"
                                        else ProcessState.ERROR)
                        break
                    if line and self._on_log:
                        self._on_log(line)
            finally:
                try:
                    proc.terminate()
                except Exception:
                    pass

        self._log_stop.clear()
        self._log_thread = threading.Thread(target=_pump, daemon=True,
                                            name=f"ssh-log-{self.session}")
        self._log_thread.start()

    def _stop_log_stream(self) -> None:
        self._log_stop.set()
        try:
            if self._log_proc:
                self._log_proc.terminate()
        except Exception:
            pass
        self._log_proc = None
        self._log_thread = None

    # ── 복원 ──

    def restore(self) -> TrainJobSpec | None:
        """게이트웨이 재시작 후 살아있는 원격 세션에 다시 붙는다.

        PID 파일을 보지 않는다 — 원격 tmux 가 진실이다. 총 스텝은 로그에 안 나오는
        사실이라 스크립트 파일에서 되찾는다(`SystemdRunner` 가 `ExecStart` 에서
        되찾는 것과 같은 자리). 없으면 화면이 "4000 / 0" 이 된다.
        """
        if not self.host or not self._alive():
            return None

        cmd: list[str] = []
        total, out_dir = 0, ""
        try:
            r = _run(self.host, f"cat {shlex.quote(self._script)}")
            for line in (r.stdout or "").splitlines():
                if line.startswith("# piper-total-steps:"):
                    total = int(line.split(":", 1)[1].strip() or 0)
                elif line.startswith("# piper-output-dir:"):
                    out_dir = line.split(":", 1)[1].strip()
                elif not line.startswith(("#", "export ", "set ", "echo ")) and line.strip():
                    cmd = shlex.split(line)
        except Exception as exc:
            logger.warning("원격 스크립트를 읽지 못했습니다 (%s): %s", self.session, exc)

        self._set_state(ProcessState.RUNNING)
        self._checked_at = time.time()
        self._start_log_stream(from_start=True)
        logger.info("원격 재부착: %s @ %s — 총 %d 스텝, 출력 %s",
                    self.session, self.host, total, out_dir or "?")
        return TrainJobSpec(cmd=cmd, total_steps=total, output_dir=out_dir)
