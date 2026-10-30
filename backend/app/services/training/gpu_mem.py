"""학습 GPU 메모리 — 재고, 남기고, 다음 학습의 배치를 미리 가늠한다.

필요 메모리 = **고정 몫** + **샘플당 몫 × 배치**.

- 고정 몫은 파라미터 수로 계산한다 — fp32 가중치 전부 + 학습하는 것의 기울기·AdamW 두 모멘트
  + CUDA 컨텍스트. bf16 AMP 여도 마스터 가중치·옵티마이저 상태는 fp32 다. 파라미터 수는 LeRobot 이
  로그에 찍는다(`num_learnable_params=…`).
- 샘플당 몫은 실측에서 고정 몫을 뺀 나머지를 배치로 나눈 것이다. 실측은 학습 중 `nvidia-smi` 의
  그 프로세스 사용량 — PyTorch 캐시 할당기는 잡은 메모리를 안 돌려주므로 몇 step 이면 최대치에서 멈춘다.

2026-10-30 실측(SmolVLA-S 전체 학습, 카메라 3대, bf16, RTX 5090): 배치 16 → 26,828MiB, 배치 20 → 30.3GiB
에서 메모리 부족. 고정 6.8GB + 샘플당 1.25GB 가 둘 다 맞춘다.
"""

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

MiB = 1024 * 1024
#: CUDA 컨텍스트·cuDNN 작업 공간 — 파라미터와 무관하게 프로세스가 잡는 몫
CONTEXT_MB = 500
#: nvidia-smi 총량 중 프로세스가 못 쓰는 몫. 실측: 총 32,607MiB 인데 PyTorch 가 본 용량은 31.35GiB,
#: 메모리 부족 순간 이 프로세스 30.28GiB + 다른 프로세스 537MiB + 남은 30MiB — 약 1GB 가 빈다.
RESERVE_MB = 1024
#: 권장 배치는 쓸 수 있는 메모리의 이만큼까지만 — 조각화·저장 순간의 출렁임 여유
HEADROOM = 0.9

#: 메모리를 바꾸는 학습 스위치 — 무엇을 학습하나(기울기·옵티마이저 상태·역전파 활성값)
MEM_SWITCHES = ("freeze_vision_encoder", "train_expert_only", "gradient_checkpointing")

PARAMS_RE = re.compile(r"num_(learnable|total)_params=(\d+)")
# torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 34.00 MiB. GPU 0 has a total capacity of
# 31.35 GiB of which 29.62 MiB is free. ... this process has 30.28 GiB memory in use. ...
OOM_RE = re.compile(r"CUDA out of memory")
_CAPACITY = re.compile(r"total capacity of ([\d.]+) GiB")
_IN_USE = re.compile(r"this process has ([\d.]+) GiB memory in use")


def query(pid: int | None) -> dict | None:
    """nvidia-smi 한 번 — `{total_mb, used_mb(그 프로세스), other_mb(다른 프로세스 합)}`. 못 읽으면 None.

    ⚠ GPU 가 여럿이면 첫 번째만 본다 — 학습은 `cuda` 하나로 돈다.
    """
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
        apps = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                               "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=5)
        total = int(gpu.stdout.split()[0])
        procs: dict[int, int] = {}
        for line in apps.stdout.splitlines():
            p, mb = (x.strip() for x in line.split(","))
            procs[int(p)] = procs.get(int(p), 0) + int(mb)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {
        "total_mb": total,
        "used_mb": procs.get(pid) if pid else None,
        "other_mb": sum(mb for p, mb in procs.items() if p != pid),
    }


def fixed_mb(total_params: int, learnable_params: int) -> float:
    """배치와 무관한 몫 (위 설명)."""
    return (total_params * 4 + learnable_params * (4 + 8)) / MiB + CONTEXT_MB


def parse_oom(line: str) -> dict | None:
    """메모리 부족 줄 → `{in_use_mb, capacity_mb}` (모르는 값은 None). 아니면 None."""
    if not OOM_RE.search(line):
        return None
    cap, used = _CAPACITY.search(line), _IN_USE.search(line)
    return {
        "capacity_mb": round(float(cap.group(1)) * 1024) if cap else None,
        "in_use_mb": round(float(used.group(1)) * 1024) if used else None,
    }


def estimate(profile: dict, batch: int, total_mb: int, other_mb: int) -> dict | None:
    """프로파일 하나로 `batch` 의 메모리를 가늠한다. 근거가 없으면 None.

    근거는 성공한 실측(`measured`)이 먼저다. 없으면 메모리 부족 기록(`oom`) — 죽은 순간까지 쓴
    양이라 **실제는 더 든다**: 필요량은 하한, 권장 배치는 상한이다.
    """
    params = profile.get("params") or {}
    if not (params.get("total") and params.get("learnable")) or batch <= 0:
        return None
    fixed = fixed_mb(params["total"], params["learnable"])
    oom = profile.get("oom") or {}
    if profile.get("peak_mb") and profile.get("batch"):
        per, basis = (profile["peak_mb"] - fixed) / profile["batch"], "measured"
    elif oom.get("in_use_mb") and oom.get("batch"):
        per, basis = (oom["in_use_mb"] - fixed) / oom["batch"], "oom"
    else:
        return None
    if per <= 0:
        return None

    usable = total_mb - other_mb - RESERVE_MB
    need = fixed + per * batch
    max_batch = max(0, int((usable * HEADROOM - fixed) // per))
    level = "over" if need > usable else "tight" if need > usable * HEADROOM else "ok"
    # 죽은 배치 이상은 계산이 뭐라든 죽는다 — 같은 기계·같은 설정에서 실제로 죽었다
    if oom.get("batch") and (basis == "oom" or oom["batch"] > profile.get("batch", 0)):
        max_batch = min(max_batch, oom["batch"] - 1)
        if batch >= oom["batch"]:
            level = "over"
    return {
        "need_mb": round(need), "usable_mb": round(usable), "total_mb": total_mb,
        "fixed_mb": round(fixed), "per_sample_mb": round(per),
        "max_batch": max_batch, "level": level, "basis": basis,
    }


def _norm(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v).lower()


def switches_from_cmd(cmd: list[str]) -> dict[str, str]:
    """`--policy.<스위치>=<값>` 만."""
    out = {}
    for a in cmd:
        for k in MEM_SWITCHES:
            if a.startswith(f"--policy.{k}="):
                out[k] = a.split("=", 1)[1]
    return out


def profile_key(dataset: str, policy: str, amp: str, switches: dict) -> str:
    """메모리를 바꾸는 것 — 데이터셋(카메라 수·해상도) · 정책 · AMP · 무엇을 학습하나.

    배치는 키가 아니라 계산의 변수다. ⚠ 아키텍처 값(chunk_size·dim_model 등)은 넣지 않았다 —
    그걸 바꾸면 같은 키라도 어긋난다.
    """
    sw = ",".join(f"{k}={_norm(switches[k])}" for k in MEM_SWITCHES if k in switches)
    return f"{dataset}|{policy}|{amp}|{sw}"
