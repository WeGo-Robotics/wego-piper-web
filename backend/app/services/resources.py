"""GPU·디스크 — 대시보드가 "지금 여유가 있나"를 보여줄 재료.

## 왜 별도 모듈인가

`nvidia-smi` 는 **멈출 수 있다.** 드라이버가 걸리면 프로세스가 D-state 로 들어가
영영 안 돌아온다 — 이 저장소는 D405 의 UVC 컨트롤 질의로 정확히 그걸 겪었고,
그때 **이벤트 루프 전체가 먹통**이 됐다(대시보드 하나 때문에 웹이 통째로 죽는다).
그래서 여기서 나가는 모든 외부 호출은 **타임아웃이 있고, 실패해도 None 이다.**
자원 표시가 없는 것과 웹이 안 뜨는 것은 비교할 일이 아니다.
"""

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

# ⚠ 짧게 잡는다. 이 값이 지나면 **없는 셈 친다** — 대시보드는 3~5초마다 다시 묻고,
#   느린 응답을 기다리느라 화면이 비는 것보다 "모름" 이 낫다.
NVIDIA_SMI_TIMEOUT_S = 3.0

_FIELDS = ("name", "utilization.gpu", "memory.used", "memory.total",
           "temperature.gpu", "driver_version")


def gpus() -> list[dict]:
    """GPU 목록. nvidia-smi 가 없거나 멈추면 **빈 목록**이다."""
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(_FIELDS)}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_S).stdout
    except FileNotFoundError:
        return []                      # GPU 없는 기계 — 정상이다
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("nvidia-smi 실패 (자원 표시만 빠진다): %s", e)
        return []

    rows = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(_FIELDS):
            continue
        name, util, used, total, temp, driver = parts

        def _num(v):
            # `[N/A]` 가 온다 — 노트북 GPU 는 온도를 안 주기도 한다
            try:
                return int(float(v))
            except ValueError:
                return None

        rows.append({
            "name": name, "driver": driver,
            "util_pct": _num(util),
            "mem_used_mb": _num(used), "mem_total_mb": _num(total),
            "temp_c": _num(temp),
        })
    return rows


# CPU 사용률은 /proc/stat 두 번의 **차분**이다. 직전 스냅숏(busy, total)을 들고 있다가
# 다음 호출에서 그 사이 구간의 사용률을 낸다 — 첫 호출은 기준점만 잡고 None.
_prev_cpu: tuple[int, int] | None = None


def cpu_pct() -> float | None:
    """전체 코어 평균 CPU 사용률(%). 직전 호출 이후 구간 기준. 못 읽으면 None."""
    global _prev_cpu
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
    except OSError as e:
        logger.warning("/proc/stat 읽기 실패: %s", e)
        return None
    if not parts or parts[0] != "cpu":
        return None
    vals = [int(x) for x in parts[1:] if x.isdigit()]
    if len(vals) < 5:
        return None
    idle = vals[3] + vals[4]              # idle + iowait
    total = sum(vals)
    prev, _prev_cpu = _prev_cpu, (total - idle, total)
    if prev is None:
        return None
    dt = total - prev[1]
    if dt <= 0:
        return None
    return round(((total - idle) - prev[0]) / dt * 100, 1)


def disk(path: str) -> dict | None:
    """한 경로의 디스크 여유. 못 읽으면 None."""
    try:
        u = shutil.disk_usage(path)
    except OSError as e:
        logger.warning("디스크 조회 실패 %s: %s", path, e)
        return None
    return {"path": path, "total_gb": round(u.total / 1e9, 1),
            "free_gb": round(u.free / 1e9, 1),
            "used_pct": round((u.total - u.free) / u.total * 100) if u.total else None}
