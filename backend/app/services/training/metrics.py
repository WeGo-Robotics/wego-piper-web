"""학습 메트릭 파싱과 히스토리 — **실행 방식과 무관하다.**

`lerobot-train` 의 로그 한 줄만 있으면 되므로, 로컬 subprocess 든 원격 SSH 든
같은 코드를 쓴다. 이걸 프로세스 관리에서 떼어내는 것이
클라우드 학습 지원의 절반이다 (feature/cloud-training.md §1-(1)).
"""

import re
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from app.services.training import gpu_mem
from app.services.training.loader import WINDOW

# step:200 smpl:12.8K ep:106 epch:1.07 loss:0.342 grdn:2.157 lr:1.0e-03 updt_s:0.456 data_s:0.012
# step:1K smpl:64K ep:154 epch:3.07 ...  (1000스텝 이후 K 접미사)
METRIC_RE = re.compile(
    r"step:([\d.]+[KM]?)\s+smpl:([\d.]+[KM]?)\s+ep:([\d.]+[KM]?)\s+epch:([\d.]+)\s+"
    r"loss:([\d.]+)\s+grdn:([\d.]+)\s+lr:([\d.e+-]+)\s+"
    r"updt_s:([\d.]+)\s+data_s:([\d.]+)"
)

# Training:  19%|█▊        | 37000/200000 [7:40:13<34:15:32,  1.32step/s]
# LeRobot 의 tqdm 진행 줄. 메트릭 줄의 step 은 1000 단위로 반올림돼(`37K`) 남은 시간이 ±6분씩
# 출렁이지만, 여기엔 **정확한 step 과 루프가 흐른 시간**이 한 줄에 있다 — 재부착이 저널을 처음부터
# 다시 읽어도 시계가 안 틀어진다. total 은 `steps − 시작 step` 이라 resume 해도 `total − n` 이 남은 수다.
# ⚠ `Training:` 으로 묶는다 — 체크포인트 샤드 로딩 같은 다른 진행 막대도 같은 모양이다.
TQDM_RE = re.compile(r"Training:\s*\d+%\|[^|]*\|\s*(\d+)/(\d+)\s*\[((?:\d+:)?\d+:\d+)<")

#: tqdm 으로 속도를 재려면 루프가 이만큼은 흘러야 한다 — 첫 step 들엔 워커 기동·cudnn 튜닝이 섞인다
MIN_LOOP_S = 60


def parse_num(s: str) -> int:
    """'34K' → 34000, '1.2M' → 1200000, '200' → 200"""
    s = s.strip()
    if s.endswith("K"):
        return int(float(s[:-1]) * 1000)
    elif s.endswith("M"):
        return int(float(s[:-1]) * 1000000)
    return int(s)


def parse_clock(s: str) -> int:
    """tqdm 경과 `7:40:13`·`40:13` → 초. 시간 자리는 24 를 넘는다(`34:15:32`)."""
    sec = 0
    for part in s.split(":"):
        sec = sec * 60 + int(part)
    return sec


@dataclass
class TrainMetrics:
    step: int = 0
    total_steps: int = 0
    loss: float = 0.0
    grad_norm: float = 0.0
    lr: float = 0.0
    epoch: float = 0.0
    update_time: float = 0.0
    data_time: float = 0.0


@dataclass
class TrainHistory:
    steps: list[int] = field(default_factory=list)
    losses: list[float] = field(default_factory=list)
    grad_norms: list[float] = field(default_factory=list)
    lrs: list[float] = field(default_factory=list)
    max_points: int = 5000  # 메모리 제한

    def append(self, step: int, loss: float, grad_norm: float, lr: float) -> None:
        """⚠ **같은 step 이 여러 번 온다.** LeRobot 은 step 을 `step:25K` 처럼
        **1000 단위로 반올림해서** 찍는데 `log_freq` 는 그보다 작다 — 실측으로
        `step:25K` 가 연속 5번 나왔다. 그대로 쌓으면 **같은 x 에 서로 다른 loss** 가
        찍혀 그래프에 수직선이 서고, 곡선이 톱니처럼 보인다(실제로 그렇게 보였다:
        50점 중 39점이 중복이었다).

        더 정밀한 step 은 로그 어디에도 없다. tqdm 의 `37600/100000` 은 **다른
        줄**이라 이 줄에서 못 얻는다(83줄 중 0줄에만 같이 있었다). 그래서
        **x 해상도가 1000 스텝인 것을 그대로 인정하고**, 같은 step 이 다시 오면
        마지막 점을 갱신한다 — 없는 해상도를 지어내지 않는다.
        """
        if self.steps and step < self.steps[-1]:
            # ⚠ **뒤로 가는 값은 버린다.** 게이트웨이를 재시작하면 저널을 처음부터
            #   다시 읽으므로(재부착), 옛 줄이 새 줄 뒤에 붙을 수 있다. 마지막 점에
            #   덮어쓰면 x 가 되돌아가 선이 되짚어 그어진다 — 곡선이 아니라 그물이
            #   된다. 이 검사가 없을 때 테스트가 정확히 그걸 잡았다.
            return
        if self.steps and step == self.steps[-1]:
            self.steps[-1] = step
            self.losses[-1] = loss
            self.grad_norms[-1] = grad_norm
            self.lrs[-1] = lr
            return
        if len(self.steps) >= self.max_points:
            # 오래된 절반 버리기
            half = self.max_points // 2
            self.steps = self.steps[half:]
            self.losses = self.losses[half:]
            self.grad_norms = self.grad_norms[half:]
            self.lrs = self.lrs[half:]
        self.steps.append(step)
        self.losses.append(loss)
        self.grad_norms.append(grad_norm)
        self.lrs.append(lr)

    def to_dict(self) -> dict:
        return {
            "steps": self.steps,
            "losses": self.losses,
            "grad_norms": self.grad_norms,
            "lrs": self.lrs,
        }

    def load(self, d: dict) -> None:
        """저장된 곡선을 되살린다. 형식이 이상하면 **조용히 비운다** —
        곡선 하나 때문에 학습 화면이 통째로 안 뜨는 편이 더 나쁘다."""
        try:
            steps = [int(x) for x in d.get("steps", [])]
            losses = [float(x) for x in d.get("losses", [])]
            grads = [float(x) for x in d.get("grad_norms", [])]
            lrs = [float(x) for x in d.get("lrs", [])]
        except (TypeError, ValueError):
            return
        n = min(len(steps), len(losses), len(grads), len(lrs))
        if not n:
            return
        self.steps, self.losses = steps[:n], losses[:n]
        self.grad_norms, self.lrs = grads[:n], lrs[:n]

    def clear(self) -> None:
        self.steps.clear()
        self.losses.clear()
        self.grad_norms.clear()
        self.lrs.clear()


class MetricsTracker:
    """로그 줄을 먹여주면 메트릭을 뽑아 히스토리에 쌓는다.

    러너(로컬/SSH/…)가 로그를 어디서 가져오든 여기로 흘려보내면 된다.
    """

    def __init__(self) -> None:
        self.metrics = TrainMetrics()
        self.history = TrainHistory()
        # 로그 구간마다의 (updt_s, data_s) — 로더 진단 재료(`loader.advise`).
        # ⚠ 히스토리와 따로 둔다: 히스토리는 1000 step 해상도라 같은 step 을 덮어쓴다.
        self.timings: deque[tuple[float, float]] = deque(maxlen=WINDOW)
        self._timing_lines = 0
        # tqdm 진행 — 처음 본 (n, 경과초) 와 마지막 (n, total, 경과초). 남은 시간 재료(`remaining`).
        self._loop_first: tuple[int, int] | None = None
        self._loop_last: tuple[int, int, int] | None = None
        # 메모리 계산 재료 — LeRobot 이 찍는 파라미터 수, 메모리 부족으로 죽었으면 그 줄 (gpu_mem.py)
        self.params: dict[str, int] = {}
        self.oom: dict | None = None
        # 진행을 마지막으로 본 때(monotonic). ⚠ journald 는 `\n` 에서만 줄을 끊어서 tqdm 의 `\r` 갱신은
        # 메트릭 줄과 함께 **몰려서** 온다 — 남은 시간은 그 순간 기준이라 흐른 만큼 빼야 한다.
        self.seen_at: float | None = None
        self._on_update: Callable[[], None] | None = None

    def set_update_callback(self, cb: Callable[[], None]) -> None:
        self._on_update = cb

    def reset(self, total_steps: int = 0) -> None:
        self.metrics = TrainMetrics(total_steps=total_steps)
        self.history.clear()
        self.timings.clear()
        self._timing_lines = 0
        self._loop_first = self._loop_last = None
        self.params = {}
        self.oom = None
        self.seen_at = None

    def feed(self, line: str) -> bool:
        """로그 한 줄 처리. 메트릭 줄이었으면 True."""
        self._feed_progress(line)
        if p := gpu_mem.PARAMS_RE.search(line):
            self.params[p.group(1)] = int(p.group(2))
        if self.oom is None and (o := gpu_mem.parse_oom(line)):
            self.oom = o
        m = METRIC_RE.search(line)
        if not m:
            return False
        self.seen_at = time.monotonic()
        self.metrics.step = parse_num(m.group(1))
        self.metrics.epoch = float(m.group(4))
        self.metrics.loss = float(m.group(5))
        self.metrics.grad_norm = float(m.group(6))
        self.metrics.lr = float(m.group(7))
        self.metrics.update_time = float(m.group(8))
        self.metrics.data_time = float(m.group(9))
        # 첫 구간은 버린다 — 워커 기동·cudnn 자동 튜닝이 섞여 있다
        self._timing_lines += 1
        if self._timing_lines > 1:
            self.timings.append((self.metrics.update_time, self.metrics.data_time))

        self.history.append(
            self.metrics.step, self.metrics.loss,
            self.metrics.grad_norm, self.metrics.lr,
        )
        if self._on_update:
            self._on_update()
        return True

    def _feed_progress(self, line: str) -> None:
        """tqdm 진행 줄을 먹는다. 매 step 오므로 콜백을 부르지 않는다 — 화면은 메트릭 줄 때 받는다.

        러너가 `\\r` 에서 안 끊으면 갱신 여러 개가 한 줄에 붙어 온다 — 마지막 것이 지금이다.
        """
        last = None
        for last in TQDM_RE.finditer(line):
            pass
        if last is None:
            return
        n, total, elapsed = int(last.group(1)), int(last.group(2)), parse_clock(last.group(3))
        # 루프가 새로 시작했으면(재시작·resume) 처음부터 다시 잰다
        if self._loop_first is None or n < self._loop_first[0] or elapsed < self._loop_first[1]:
            self._loop_first = (n, elapsed)
        self._loop_last = (n, total, elapsed)
        self.seen_at = time.monotonic()

    def remaining(self) -> tuple[float, str] | None:
        """남은 시간(초)과 근거 — **마지막으로 본 순간**(`seen_at`) 기준. 모르면 None.

        - `progress` — tqdm 이 잰 실제 진행 속도(처음 본 뒤 평균). 체크포인트 저장·에폭 재기동까지
          들어간 벽시계라 이쪽이 맞다.
        - `step_time` — tqdm 이 없을 때. 메트릭 줄의 updt_s+data_s 중앙값이라 저장 시간이 빠져
          조금 짧게 나온다(실측 2%).
        """
        if self._loop_first and self._loop_last:
            n0, e0 = self._loop_first
            n, total, e = self._loop_last
            if n >= total:
                return 0.0, "progress"
            if e - e0 >= MIN_LOOP_S and n > n0:
                return (total - n) * (e - e0) / (n - n0), "progress"
        m = self.metrics
        if m.total_steps and m.step and self.timings:
            per = statistics.median(u + d for u, d in self.timings)
            return max(m.total_steps - m.step, 0) * per, "step_time"
        return None

    def progress(self) -> float:
        if self.metrics.total_steps > 0 and self.metrics.step > 0:
            return round(self.metrics.step / self.metrics.total_steps, 4)
        return 0.0
