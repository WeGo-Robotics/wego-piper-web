"""학습 관리 — 러너(실행 방식)와 메트릭(파싱)을 조합한다.

이전에는 `TrainManager` 가 곧 `ProcessManager` 였다 — `state`/`is_running`/`stop` 이
전부 로컬 subprocess 에 위임됐고, 원격 job 에는 PID 도 SIGTERM 도 stdout 파이프도 없다
(feature/cloud-training.md §1-(1)).

이제 `TrainRunner` 이음매만 보므로 `SSHRunner`(클라우드) 나
`SystemdRunner`(데몬 분리) 를 나란히 붙일 수 있다.
"""

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

from app.services.process_manager import ProcessState
from app.services.training.jobs import (
    LOCAL_JOB_ID,
    JobRecord,
    JobRegistry,
    job_registry,
)
from app.services.training import loader
from app.services.training.metrics import MetricsTracker
from app.services.training.runners.base import TrainRunner
from app.services.training.runners.local import LocalRunner

from app.services.training.spec import TrainJobSpec

logger = logging.getLogger(__name__)

# LeRobot 이 자리를 정한 뒤 한 번 찍는 줄: `INFO ... lerobot_train.py:329 Output dir: outputs/train/…`
# 터미널이면 색 코드가 붙는다.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_OUTPUT_DIR = re.compile(r"Output dir:\s*(\S.*?)\s*$")


def _cmd_arg(cmd: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    for a in cmd:
        if a.startswith(prefix):
            return a[len(prefix):]
    return None


def _loader_context(cmd: list[str], env: dict[str, str] | None) -> dict:
    """로더 진단에 필요한 것을 명령에서 읽는다. `env` 가 None 이면(재부착) AMP 는 모른다."""
    try:
        # 인자가 없으면 LeRobot 기본값(4)으로 돈다
        workers = int(_cmd_arg(cmd, "num_workers") or 4)
    except ValueError:
        workers = None
    policy = _cmd_arg(cmd, "policy.type")
    if policy is None and (path := _cmd_arg(cmd, "policy.path")):
        # 체크포인트에서 이어 학습 — 정책 이름은 그 config 에 있다
        try:
            policy = json.loads((Path(path) / "config.json").read_text()).get("type")
        except (OSError, ValueError, AttributeError):
            policy = None
    return {
        "num_workers": workers,
        "amp": None if env is None else (env.get("ACCELERATE_MIXED_PRECISION") or "off"),
        "dataset": _cmd_arg(cmd, "dataset.repo_id"),
        "policy": policy,
        "batch_size": _cmd_arg(cmd, "batch_size"),
    }


def loader_profile_key(dataset: str, policy: str, amp: str) -> str:
    """로더 비용은 데이터셋·정책(관측 프레임 수)에, GPU 시간은 정책·AMP 에 달렸다.
    배치는 키에 넣지 않는다 — 배치 2배에 updt_s 1.8배·T_b 2.0배라 필요한 워커가 12%
    차이뿐이고, 권장값의 여유(1.2)가 덮는다 (feature/train-loader-workers.md §6 실측)."""
    return f"{dataset}|{policy}|{amp}"


def _default_runner() -> "TrainRunner":
    """설정이 고른 러너. **못 쓰면 조용히 떨어지지 않고 말한다.**

    조용히 `LocalRunner` 로 폴백하면 "재시작해도 학습이 살아있다"고 믿는데
    실제로는 아닌 상태가 된다 — 그게 가장 나쁜 결과다.

    원격이 로컬보다 먼저다. 둘 다 설정돼 있으면 원격이 이긴다 — `process_runner`
    는 "이 기계에서 어떻게 띄우나"이고 `train_ssh_host` 는 "어느 기계에서 도나"라
    층이 다르다.
    """
    from app.core.config import settings

    if settings.train_ssh_host:
        from app.services.training.runners.ssh import SSHRunner
        from app.services.training.runners.ssh import available as ssh_available

        ok, why = ssh_available()
        if ok:
            return SSHRunner()
        # ⚠ 여기서 조용히 로컬로 떨어지면 **이 기계의 GPU 를 먹는다.** 원격에서
        #   돈다고 믿고 추론을 같이 걸면 OOM 이다 — 그래서 크게 남긴다.
        logger.warning("원격 학습 러너를 쓸 수 없어 local 로 돕니다 — %s "
                       "(학습이 이 기계의 GPU 를 씁니다)", why)
        return LocalRunner()

    if settings.process_runner != "systemd":
        return LocalRunner()

    from app.services.training.runners.systemd import SystemdRunner, available

    ok, why = available()
    if not ok:
        logger.warning("systemd 러너를 쓸 수 없어 local 로 돕니다 — %s "
                       "(재시작하면 학습이 화면에서 사라집니다)", why)
        return LocalRunner()
    return SystemdRunner()


class TrainManager:
    def __init__(
        self,
        runner: TrainRunner | None = None,
        job_id: str = LOCAL_JOB_ID,
        registry: JobRegistry | None = None,
    ) -> None:
        self.runner: TrainRunner = runner or _default_runner()
        self.tracker = MetricsTracker()
        self.output_dir: str = ""
        # 로컬도 job_id 를 갖는다 — 원격이 붙어도 같은 경로를 타게 하려는 것이다
        # (feature/cloud-training.md 3단계).
        self.job_id = job_id
        self.registry = registry or job_registry
        self._on_metrics: Callable[[dict], None] | None = None
        self._original_log_cb: Callable[[str], None] | None = None
        self._original_state_cb: Callable[[ProcessState], None] | None = None
        self._on_output_dir: Callable[[Path], None] | None = None
        self._loader: dict = {}
        self.tracker.set_update_callback(self._emit_metrics)
        # ⚠ **여기서 건다. WS 연결 때가 아니다.** 예전에는 `set_log_callback` 이
        #   `ws.py` 에서만 불렸다 — 즉 **브라우저가 붙어야** 메트릭이 쌓였다.
        #   재부착이 저널을 처음부터 재생해도 받을 사람이 없어 전부 버려졌고
        #   (실측: 저널에 233줄, 히스토리엔 2점), 학습 도중 화면을 열면 그
        #   순간부터의 몇 점만 보였다.
        self.runner.set_log_callback(self._intercept_log)

    # ── 상태 (실행 방식 무관) ──

    @property
    def state(self) -> ProcessState:
        return self.runner.state

    @property
    def is_running(self) -> bool:
        return self.runner.is_running

    @property
    def uses_local_gpu(self) -> bool:
        """이 학습이 **이 기계의** GPU 를 쓰는가. 배타 가드가 본다.

        기본은 True 다 — 모르면 막는 쪽이 안전하다. 원격 러너만 False 로 답한다.
        """
        return getattr(self.runner, "occupies_local_gpu", True)

    @property
    def metrics(self):
        return self.tracker.metrics

    @property
    def history(self):
        return self.tracker.history

    # ── 콜백 ──

    def set_metrics_callback(self, cb: Callable[[dict], None]) -> None:
        self._on_metrics = cb

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        """WS 가 로그를 받아 갈 곳을 등록한다.

        ⚠ 러너 연결은 **생성 때 이미 끝났다** — 여기서 다시 걸면 그전까지 쌓던
        것을 잃지는 않지만, "브라우저가 붙어야 시작된다" 는 오해를 남긴다.
        """
        self._original_log_cb = cb

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        self._original_state_cb = cb
        self.runner.set_state_callback(self._intercept_state)

    def _intercept_log(self, line: str) -> None:
        """로그를 가로채 메트릭을 뽑고 원본 콜백에 그대로 전달."""
        self.tracker.feed(line)
        if "Output dir:" in line:
            self._output_dir_reported(line)
        # 6시간 학습이면 수만 줄이라 전부 WS 로 밀면 브라우저가 죽는다.
        # 버스 링버퍼에 남겨 REST 페이지네이션으로 읽게 한다 (WS 로는 신규분만).
        self.registry.append_log(self.job_id, line)
        if self._original_log_cb:
            self._original_log_cb(line)

    def _output_dir_reported(self, line: str) -> None:
        """LeRobot 이 실행 폴더를 정했다 — 그 폴더에 남길 것을 이제 남긴다.

        ⚠ **그 전에는 폴더를 만들면 안 된다.** LeRobot 은 `cfg.validate()` 에서 폴더가
        이미 있으면 `FileExistsError` 로 죽는다(resume 이 아니면). 시작 직후에 사이드카를
        쓰려고 mkdir 하면 그 학습이 몇 초 뒤 죽는다. 이 줄은 validate 뒤에 찍힌다.
        ⚠ Output Dir 을 비우면(자동) 경로는 LeRobot 이 정하므로 **여기서만** 알 수 있다.
        상대경로는 러너의 작업 폴더 기준이다. 원격이면 이 기계의 경로가 아니라 손대지 않는다.
        """
        workdir = getattr(self.runner, "workdir", None)
        m = _OUTPUT_DIR.search(_ANSI.sub("", line))
        if m is None or workdir is None or getattr(self.runner, "is_remote", False):
            return
        out = Path(workdir) / m.group(1)
        # 자동 경로였다면 이제야 안다 — 상태·곡선 저장이 이 값을 쓴다
        if not self.output_dir:
            self.output_dir = str(out)
        cb, self._on_output_dir = self._on_output_dir, None
        if cb is None:
            return
        try:
            cb(out)
        except Exception as exc:                                  # noqa: BLE001
            logger.warning("실행 폴더 사이드카 기록 실패 (%s): %s", out, exc)

    def _intercept_state(self, state: ProcessState) -> None:
        """상태 변화를 레지스트리에 반영하고 원본 콜백에 전달.

        여기서 쓰지 않으면 **게이트웨이가 재시작할 때 학습 상태가 사라진다** —
        지금까지의 그 버그다. 레지스트리가 프로세스 밖에 있으니 다시 읽으면 그만이다.
        """
        self._sync_record(state=state.value)
        if self._original_state_cb:
            self._original_state_cb(state)

    def _emit_metrics(self) -> None:
        status = self.get_status()
        self._sync_record(metrics=status)
        self._save_loader_profile(status["loader"])
        if self._on_metrics:
            self._on_metrics(status)

    def _sync_record(self, state: str | None = None, metrics: dict | None = None) -> None:
        record = self.registry.get(self.job_id) or JobRecord(job_id=self.job_id)
        record.runner = type(self.runner).__name__.replace("Runner", "").lower()
        record.state = state if state is not None else self.state.value
        # ⚠ **빈 값으로 덮어쓰지 않는다.** 서버 재시작 직후엔 트래커가 비어 있는데,
        # 그대로 쓰면 살아남은 레코드의 `total_steps`/`output_dir` 이 0과 ""가 된다 —
        # 레지스트리를 둔 이유(재시작해도 학습이 보인다)를 스스로 깎는다.
        if self.output_dir:
            record.output_dir = self.output_dir
        if self.tracker.metrics.total_steps:
            record.total_steps = self.tracker.metrics.total_steps
        if metrics is not None:
            record.metrics = metrics
        # 곡선도 함께 남긴다. 1000스텝당 1점이라 100K 학습이 100점 — 부담이 없다.
        if self.tracker.history.steps:
            record.history = self.tracker.history.to_dict()
            self._save_history(record.history)
        self.registry.put(record)

    def _save_loader_profile(self, advice: dict) -> None:
        """다음 학습 폼이 workers 권장값을 미리 채우도록 남긴다 — 이 기계 학습만.
        원격은 임대할 때마다 기계가 달라 지난 실측이 다음 기계에 안 맞는다."""
        ctx = self._loader
        if (advice.get("reason") == "warming" or getattr(self.runner, "is_remote", False)
                or not (ctx.get("dataset") and ctx.get("policy") and ctx.get("amp"))):
            return
        from datetime import datetime

        self.registry.put_loader_profile(
            loader_profile_key(ctx["dataset"], ctx["policy"], ctx["amp"]),
            {
                "num_workers": advice["num_workers"],
                "batch_size": ctx.get("batch_size"),
                "update_s": advice["update_s"], "data_s": advice["data_s"],
                "cpu_pct": advice.get("cpu_pct"),
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )

    def _save_history(self, history: dict) -> None:
        """곡선을 **실행 폴더에도** 쓴다 — 레지스트리 레코드는 다음 학습을 시작하면 지워진다.

        가중치 분석의 체크포인트 개요가 지난 학습의 곡선을 여기서 읽는다
        (feature/weight-analyzer.md §3). 원격 학습이면 이 기계에 폴더가 없으니 건너뛴다.
        """
        from pathlib import Path

        from app.services.weight_eval import TRAIN_METRICS_FILE

        out = Path(self.output_dir) if self.output_dir else None
        if out is None or not out.is_dir():
            return
        try:
            tmp = out / (TRAIN_METRICS_FILE + ".tmp")
            tmp.write_text(json.dumps(history))
            tmp.replace(out / TRAIN_METRICS_FILE)
        except OSError as exc:
            logger.warning("학습 곡선 저장 실패 (%s): %s", out, exc)

    # ── 실행 ──

    async def start(
        self,
        cmd: list[str],
        total_steps: int = 0,
        output_dir: str = "",
        env_extra: dict[str, str] | None = None,
        max_hours: float = 0.0,
        on_output_dir: Callable[[Path], None] | None = None,
    ) -> None:
        """`on_output_dir` — LeRobot 이 실행 폴더를 정한 뒤 한 번 불린다(`_output_dir_reported`).
        게이트웨이가 그 전에 재시작하면 불리지 않는다."""
        self.tracker.reset(total_steps=total_steps)
        self.output_dir = output_dir
        self._on_output_dir = on_output_dir
        self._loader = _loader_context(cmd, env_extra or {})
        # 이전 job 의 로그·레코드를 치운다. 남겨두면 새 학습 로그가 옛 줄 뒤에 붙어
        # "어디부터 이번 학습인가"를 알 수 없다 — 버스 링버퍼는 세션을 넘어 살아남는다.
        self.registry.delete(self.job_id)
        self._sync_record(state=ProcessState.STARTING.value)
        await self.runner.start(
            TrainJobSpec(
                cmd=cmd,
                total_steps=total_steps,
                output_dir=output_dir,
                env=env_extra or {},
                # ⚠ **가장 안쪽 가드**(§6-1). 임대 GPU 는 우리가 안 보는 동안에도
                #   시간당으로 과금된다 — 로컬은 0 이라 지금과 똑같이 돈다.
                max_hours=max_hours,
            )
        )

    async def stop(self) -> None:
        await self.runner.stop()

    def restore_running_process(self) -> bool:
        """서버 재시작 후 살아있는 학습에 다시 붙는다.

        레지스트리도 함께 맞춘다. 안 맞추면 프로세스는 죽었는데 레코드만
        `running` 으로 남아 UI 가 영원히 "학습 중"이라고 말한다.
        """
        # ⚠ **비우는 것이 먼저다.** `runner.restore()` 는 재부착하면서 저널을
        #   **처음부터 다시 읽는 스레드**를 띄운다 — 없던 동안의 진행을 화면에
        #   채우려는 것이다. 그런데 그 뒤에 `reset()` 을 부르면 방금 채운 히스토리를
        #   통째로 지운다. 실측: 재시작 뒤 로스 곡선에 점이 2개만 남았다.
        self.tracker.reset()
        # ⚠ **저장된 곡선을 먼저 되살린다.** 저널 재생만 믿으면 로그가 순환됐거나
        #   유닛이 재시작된 뒤에는 앞부분이 통째로 없다. 재생분은 이 위에 이어진다
        #   — `append` 가 뒤로 가는 값을 버리므로 겹쳐도 안전하다.
        saved = self.registry.get(self.job_id)
        if saved and saved.history:
            self.tracker.history.load(saved.history)
            logger.info("저장된 로스 곡선 복원: %d점", len(self.tracker.history.steps))
        spec = self.runner.restore()
        if spec is None:
            stale = self.registry.get(self.job_id)
            if stale and stale.is_active:
                # ⚠ **"못 물어봤다" 와 "끝났다" 는 다르다.** 레코드가 원격(ssh)이었는데
                #   지금 러너가 로컬이면, 그건 학습이 끝난 게 아니라 **우리가 물어볼
                #   수단을 잃은 것**이다. `_default_runner()` 는 임포트 시점에 SSH 프로브를
                #   딱 한 번 하고, 그때 네트워크가 흔들리면 프로세스 수명 내내 로컬로
                #   떨어진다. 그 상태로 레코드를 IDLE 로 덮으면 **원격 tmux 는 멀쩡히
                #   학습을 계속하는데 화면에서는 사라지고, 임대 GPU 는 계속 과금된다.**
                #   지금은 파기할 코드도 예산 상한도 없으므로(§11-0) 사람이 눈치채는
                #   것이 유일한 가드다 — 그 단서를 지우지 않는다.
                if stale.runner == "ssh" and not getattr(self.runner, "is_remote", False):
                    logger.warning(
                        "원격 학습 레코드(%s)가 있는데 지금 러너는 로컬이다 — "
                        "끝났다고 단정하지 않고 그대로 둔다. 인스턴스가 살아 있을 수 "
                        "있으니 `vastai show instances` 로 확인하라.", self.job_id)
                    return False
                logger.info("죽은 학습 job 레코드를 정리한다: %s", self.job_id)
                self._sync_record(state=ProcessState.IDLE.value)
            return False
        # 총 스텝만 채운다 — **여기서 다시 비우면** 재생된 히스토리가 사라진다
        self.tracker.metrics.total_steps = spec.total_steps
        # 유닛 argv 만 남는다 — 환경변수로 들어간 AMP 는 모른다
        self._loader = _loader_context(spec.cmd, None)
        self.output_dir = spec.output_dir
        self._sync_record()
        return True

    def get_status(self) -> dict:
        m = self.tracker.metrics
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "step": m.step,
            "total_steps": m.total_steps,
            "progress": self.tracker.progress(),
            "loss": m.loss,
            "grad_norm": m.grad_norm,
            "lr": m.lr,
            "epoch": m.epoch,
            "update_s": m.update_time,  # GPU 갱신 시간 (병목 힌트)
            "data_s": m.data_time,      # 데이터 로딩 시간 (병목 힌트)
            "loader": self.loader_advice(),
            "output_dir": self.output_dir,
            # ⚠ **지금 러너를 실시간으로 싣는다.** `JobRecord.runner` 는 `_sync_record()`
            #   가 마지막으로 쓴 **과거 스냅샷**이라 재기동을 안 따라온다(실측: 어제
            #   학습의 "systemd" 가 그대로 남아 있었다). 그런데 원격이냐 아니냐에
            #   인터프리터·HF 토큰·회수 가드가 전부 매달려 있으므로, 사람이 그 값을
            #   지금 볼 수 있어야 한다 — 조용한 로컬 폴백이 가장 비싼 실패다.
            "runner": type(self.runner).__name__.replace("Runner", "").lower(),
            "remote": bool(getattr(self.runner, "is_remote", False)),
        }


    def loader_advice(self) -> dict:
        """로더 진단 (feature/train-loader-workers.md). 코어·CPU 는 **이 기계** 것이라
        원격 학습이면 넘기지 않는다 — 남의 기계를 우리 숫자로 자르면 틀린다."""
        remote = bool(getattr(self.runner, "is_remote", False))
        if remote:
            cores = cpu = None
        else:
            from app.services import trends

            cores, cpu = os.cpu_count(), trends.latest_cpu()
        return loader.advise(
            list(self.tracker.timings), self._loader.get("num_workers"),
            cores=cores, cpu_pct=cpu, amp=self._loader.get("amp"),
        )


train_manager = TrainManager()
