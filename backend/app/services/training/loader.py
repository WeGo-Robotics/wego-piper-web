"""학습 데이터 로더 진단 — step 시간이 어디에 쓰이나, workers 를 몇으로 올리나.

설계와 실측은 `feature/train-loader-workers.md`. 요약:

* LeRobot 은 배치 대기(`data_s`)와 GPU 갱신(`updt_s`)을 **차례로** 잰다 — step 시간이
  곧 둘의 합이고, `data_s` 는 GPU 가 배치를 기다리며 논 시간이다.
* 로더가 병목이면 워커 N 개가 쉬지 않고 돈다. 워커 하나가 배치 하나를 만드는 시간을
  T_b 라 하면 `updt_s + data_s = T_b / N` 이므로 T_b 를 거꾸로 얻고, GPU 를 안 굶길
  워커 수 `T_b / updt_s` 를 낸다.
* **대기가 작으면 상한만 안다**(T_b ≤ N·step). 그때는 권하지 않는다 — 줄이라는 말도
  못 한다.

순수 함수다. 숫자와 판정 코드만 내고 문장은 화면이 만든다.
"""

from __future__ import annotations

import math
from statistics import median

# 이 아래로 기다리면 로더는 충분하다고 본다 — workers 로 얻을 수 있는 게 최대 1.11배.
WAIT_THRESHOLD = 0.10
# 계산이 낙관적인 몫(실측 5~7% — 워커가 늘면 메인 스레드도 느려진다) + 에폭마다의 워커
# 재기동(LeRobot 은 persistent_workers 를 안 켠다) 여유.
MARGIN = 1.2
# 학습 기계 코어 중 워커에 못 주는 몫 — 메인 프로세스 + pin_memory 스레드.
# 실측: 영상 데이터셋 워커 하나가 코어 하나쯤을 쓴다(N=12 에서 20코어의 63%).
RESERVED_CORES = 2
# 이 위면 CPU 가 이미 찼다 — 워커를 늘려도 서로 코어를 뺏을 뿐이다.
CPU_BUSY_PCT = 90.0
# 첫 구간(워커 기동·cudnn 자동 튜닝)을 버린 뒤 이만큼 쌓여야 판정한다.
MIN_SAMPLES = 3
# 마지막 몇 구간의 중앙값을 쓰나 — 에폭 재기동이 튀는 구간을 거른다.
WINDOW = 5


def advise(
    samples: list[tuple[float, float]],
    num_workers: int | None,
    *,
    cores: int | None = None,
    cpu_pct: float | None = None,
    amp: str | None = None,
) -> dict:
    """`samples` — 로그 구간마다의 `(updt_s, data_s)`, 첫 구간은 이미 뺀 것.

    `reason`:
      - `warming` 아직 모른다 (구간이 모자라거나 workers 수를 모른다)
      - `enough` 로더가 따라온다 — 처방 없음
      - `add_workers` workers 를 `recommended` 로 올리면 된다
      - `cpu_limit` workers 로는 다 못 메운다 (코어 상한에 걸리거나 CPU 가 이미 찼다)
    """
    if num_workers is None or num_workers < 0 or len(samples) < MIN_SAMPLES:
        return {"reason": "warming", "num_workers": num_workers, "samples": len(samples)}
    recent = samples[-WINDOW:]
    out = judge(median(s[0] for s in recent), median(s[1] for s in recent), num_workers,
                cores=cores, cpu_pct=cpu_pct, amp=amp)
    out["samples"] = len(samples)
    return out


def judge(
    u: float,
    d: float,
    num_workers: int,
    *,
    cores: int | None = None,
    cpu_pct: float | None = None,
    amp: str | None = None,
) -> dict:
    """구간 대표값 `(updt_s, data_s)` 하나로 판정한다 — 학습 폼은 지난 실측 프로파일로 부른다."""
    out: dict = {"reason": "warming", "num_workers": num_workers}
    if u <= 0:
        return out
    step = u + d
    wait = d / step
    cap = max(1, cores - RESERVED_CORES) if cores else None
    out.update(
        update_s=round(u, 4), data_s=round(d, 4),
        wait_pct=round(wait * 100, 1), max_gain=round(step / u, 2), cap=cap,
        cpu_pct=cpu_pct, amp_off=amp == "off",
    )
    if wait < WAIT_THRESHOLD:
        out.update(reason="enough", recommended=num_workers)
        return out

    # 워커 하나가 배치 하나를 만드는 시간. 0 이면 메인 프로세스가 로드하므로 대기가 곧 그것이다.
    batch_s = num_workers * step if num_workers > 0 else d
    need = max(num_workers + 1, math.ceil(batch_s / u * MARGIN))
    if cpu_pct is not None and cpu_pct >= CPU_BUSY_PCT:
        # 이 계산은 T_b 가 N 과 무관하다는 가정 위에 있는데, CPU 가 찼으면 그 가정이 깨진다.
        out.update(reason="cpu_limit", recommended=num_workers, cpu_busy=True)
        return out
    rec = need if cap is None else min(need, cap)
    if rec <= num_workers:
        out.update(reason="cpu_limit", recommended=num_workers)
        return out
    pred = max(u, batch_s / rec)
    out.update(
        reason="add_workers" if rec == need else "cpu_limit",
        recommended=rec,
        predicted_wait_pct=round((pred - u) / pred * 100, 1),
    )
    return out
