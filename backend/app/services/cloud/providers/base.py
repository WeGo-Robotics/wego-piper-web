"""임대 GPU 프로바이더의 공통 어휘 — 오퍼 한 줄과 그 경고 (feature/vast-training.md §9-2).

## ⚠ `dph_total` 은 우리 가격이 아니다

Vast 가 주는 `dph_total` 에 섞인 디스크는 **검색 기본값(약 5GB)** 이고 우리 템플릿은
40GB 를 쓴다. 실측으로 항등식이 확인됐다 — `dph_total == dph_base + storage_total_cost`
(표본 13/13, 오차 0). 그래서 화면에 쓰는 값은 **우리가 요청할 디스크로 다시 계산한다**:

    시간당 = dph_base + storage_cost × disk_gb / HOURS_PER_MONTH

40GB 기준으로 표시값보다 12% 비싸다 (0.3527 → 0.394). 이 차이를 모르고 `dph_total` 을
쓰면 예산 상한과 "최대 N시간" 이 **전부 낙관적으로** 틀린다.

## ⚠ 전송비는 시간당이 아니라 GB 당이다

`inet_down_cost` / `inet_up_cost` 는 $/GB 다. 데이터셋을 올리고 체크포인트를 내리는
왕복에만 붙으므로 시간당에 섞지 않는다 — 섞으면 오래 도는 학습일수록 과대 계상된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: 한 달을 몇 시간으로 보나. `storage_cost` 가 $/GB/월 이라 시간당으로 되돌릴 때 쓴다.
HOURS_PER_MONTH = 730.0

#: 우리 학습 이미지가 cu126 이라 호스트 드라이버가 이 위여야 한다 (§2 · 템플릿 필터와 같은 값).
MIN_CUDA = 12.4
#: LeRobot 데이터로더가 굶지 않을 최소치. ⚠ 실측에서 `cpu_cores_effective` 가 0 인
#: 오퍼가 3/60 있었다 — GPU 만 보고 고르면 이걸 집는다.
MIN_CPU_CORES = 4.0

# ── 우리 이미지가 실제로 돌릴 수 있는 GPU 세대 ────────────────────────────────
#
# ⚠ **`cuda_vers`/`cuda_max_good` 으로는 이걸 못 본다.** 둘 다 CUDA *버전*이지 GPU
# 아키텍처가 아니다. Blackwell 호스트의 `cuda_max_good` 은 12.8~13.3 이라 `MIN_CUDA`
# 검사를 여유롭게 통과한다 — 즉 **못 도는 기계에 경고가 하나도 안 붙는다**.
#
# 판정 근거는 **이미지 자신이 찍는 문장**이다. 이 머신의 RTX 5090 에 실제 이미지를
# 물려 받은 출력(2026-09-16):
#
#     $ docker run --rm --gpus all ghcr.io/wego-robotics/piper-train:full-cu126 \
#           python -c "import torch; torch.cuda.get_arch_list()"
#     NVIDIA GeForce RTX 5090 with CUDA capability sm_120 is not compatible ...
#     The current PyTorch install supports CUDA capabilities
#         sm_50 sm_60 sm_70 sm_75 sm_80 sm_86 sm_90
#
# ⚠ **로컬 파이썬의 torch 로 이걸 확인하면 틀린다.** 이 머신의 torch 는 2.10+cu128
# 이라 arch list 가 sm_70~sm_120 이다 — 맥스웰·파스칼이 빠지고 Blackwell 이 들어 있는,
# **이미지와 다른 집합**이다. 처음에 그 값을 근거로 하한을 700 으로 잡았다가 구형
# 10종을 없는 죄로 막을 뻔했다. 근거는 이미지 안에서 떠야 한다.
#
# sm_XY 큐빈은 같은 major 안에서 위로 호환된다(sm_86 이 sm_89 에서 돈다). 그래서
# major 만 보면 되고, 위 목록의 major 는 5·6·7·8·9 다.
#
# ⚠ **cu128 은 상위집합이 아니다.** Blackwell(sm_100·sm_120)을 얻는 대신 맥스웰·파스칼을
# 잃는다. 그래서 "cu128 로 다시 구우면 된다" 는 공짜가 아니고, 어느 쪽도 80종을 다
# 덮지 못한다. 지금 판정은 **기본 템플릿(cu126) 기준**이다.
#
# ⚠ `env-check.sh` 가 이미 같은 검사를 한다 — 다만 인스턴스를 빌린 뒤에 한다. 그때는
# 이미 돈이 나간 뒤다. 그래서 고르는 자리에서 먼저 말한다.
CC_MIN = 500    # sm_50 (맥스웰) — 이미지가 담고 있는 가장 낮은 커널
CC_MAX = 1000   # sm_100 (B200) 부터는 cu128 이상이 필요하다

#: Vast 가 주는 `compute_cap` 중 **실제로 존재하는 값들**. ⚠ 엉터리가 섞여 있다 —
#: 실측에서 Quadro P2000=140, P4000=243 인데 둘 다 실물은 파스칼(cc 6.1)이다. 140·243 은
#: 어떤 컴퓨트 능력에도 대응하지 않는다. 구간으로만 보면 이 둘이 "너무 구형" 으로
#: 떨어지는데 그건 **틀린 답**이다(sm_60 커널이 있으니 돈다). 모르면 모른다고 한다.
KNOWN_CC = frozenset({500, 520, 600, 610, 700, 750, 800, 860, 890, 900, 1000, 1030, 1200})


def gpu_support(compute_cap: float | None) -> str:
    """`'ok'` | `'too_old'` | `'too_new'` | `'unknown'` — 기본 템플릿(cu126) 기준.

    ⚠ 판정이지 금지가 아니다. 화면은 막지 말고 **말해 주기만** 한다 — 왜 안 되는지
    모르는 것보다 알고 고르는 편이 낫다.
    """
    if not compute_cap:
        return "unknown"
    # ⚠ 알 수 없는 값을 "구형" 으로 단정하지 않는다. 실측의 140·243 은 실물이 파스칼이라
    #   막으면 틀린 답이 된다 — 모른다고 말하는 편이 정직하고, 그 편이 덜 해롭다.
    if compute_cap not in KNOWN_CC:
        return "unknown"
    if compute_cap < CC_MIN:
        return "too_old"
    if compute_cap >= CC_MAX:
        return "too_new"
    return "ok"


@dataclass(frozen=True)
class OfferFilter:
    """화이트리스트 필터. **여기가 주입 경계다.**

    ⚠ 프로바이더는 이 값들을 질의 **문자열로 연결**한다. 그러니 자유 문자열 필드를
    만들지 않는다 — 라우터가 타입 있는 쿼리 파라미터로 이걸 채우고, 프로바이더는
    고정된 필드 표에서만 고른다. GPU 이름만 문자열인데 모양을 따로 검사한다.
    """

    #: 고를 GPU 들. **비어 있으면 제약 없음 = 전체**다.
    #: ⚠ 사용자가 "싹 다 보여줘" 라고 했다(2026-09-16). 고정 목록으로는 그 말을 못 들어준다 —
    #: 실제 카탈로그는 80종이고, 고정 7개에는 3060 이 없었고 대신 RTX 5090(우리 이미지로는
    #: 못 도는 sm_120)이 들어 있었다.
    gpu_names: tuple[str, ...] = ("RTX 4090",)
    num_gpus: int = 1
    min_cuda: float = MIN_CUDA
    min_reliability: float = 0.98
    min_inet_down: float = 200.0
    min_cpu_cores: float | None = None
    max_price: float | None = None
    #: ⚠ **필터이자 가격 입력이다.** 기계의 여유 디스크를 거르는 동시에 아래 계산식의
    #: `disk_gb` 가 된다. 바꾸면 표 전체의 $/h 가 다시 계산돼야 한다.
    disk_gb: float = 40.0
    limit: int = 50


@dataclass(frozen=True)
class Offer:
    """빌릴 수 있는 기계 한 대. 필드 100개 중 **판단에 쓰이는 것만** 남긴다."""

    id: int
    machine_id: int
    gpu_name: str
    num_gpus: int
    gpu_ram_gb: float

    #: 우리 디스크로 다시 계산한 시간당 — **화면에 쓰는 값**
    hourly: float
    #: GPU 만. `hourly` 의 출처를 화면에서 쪼개 보일 수 있게 남긴다
    dph_base: float
    #: 위 둘의 차. 즉 디스크 몫
    storage_hourly: float
    disk_gb: float
    #: Vast 가 표시하는 값. ⚠ **우리 가격이 아니다** — 비교용으로만 남긴다
    dph_total: float
    inet_down_cost_per_gb: float
    inet_up_cost_per_gb: float
    #: ⚠ 속도(Mbps)와 비용($/GB)은 다른 값이다. 데이터셋을 올릴 때 걸리는 시간은
    #: 속도가 정하고, 청구서는 비용이 정한다 — 표에 둘 다 필요하다.
    inet_down_mbps: float
    inet_up_mbps: float

    cuda_max_good: float
    reliability: float
    dlperf: float
    dlperf_per_dph: float
    cpu_cores: float
    cpu_ram_gb: float
    disk_space_gb: float
    disk_bw: float
    geolocation: str
    duration_days: float
    verified: bool
    rentable: bool
    #: Vast 가 주는 값 그대로. ⚠ 엉터리가 섞여 있다 — `gpu_support()` 설명 참고
    compute_cap: float | None
    #: 'ok' | 'too_old' | 'too_new' | 'unknown' — 기본 템플릿(cu126) 기준
    support: str

    #: 사람이 읽을 경고. 비어 있으면 걸리는 게 없다는 뜻이다
    warnings: tuple[str, ...] = ()


def hourly_total(raw: dict, disk_gb: float) -> tuple[float, float]:
    """`(시간당 합계, 디스크 몫)`.

    ⚠ **`dph_total` 은 우리 가격이 아니다.** 거기 섞인 디스크는 검색 기본값(약 5GB)
    이고 우리 템플릿은 40GB 를 쓴다. 실측으로 항등식이 확인됐다 —
    `dph_total == dph_base + storage_total_cost` (표본 13/13, 오차 0). 그래서:

        시간당 = dph_base + storage_cost × disk_gb / HOURS_PER_MONTH

    ⚠ 보정률은 **오퍼마다 다르다** — `storage_cost` 가 호스트마다 10배까지 벌어져
    같은 40GB 라도 +3% 인 오퍼와 +12% 인 오퍼가 있다. 일률적인 곱셈으로는 못 맞춘다.

    ⚠ `dph_base` 가 없으면 `dph_total` 로 물러선다. 0 원짜리 기계를 만들어 내는 것보다
    조금 비싸게 잡는 편이 안전하다.
    """
    base = raw.get("dph_base")
    if base is None:
        base = raw.get("dph_total") or 0.0
    storage = (raw.get("storage_cost") or 0.0) * disk_gb / HOURS_PER_MONTH
    return base + storage, storage


def warnings_for(*, cpu_cores: float, cuda_max_good: float,
                 disk_space_gb: float, disk_gb: float,
                 compute_cap: float | None = None) -> tuple[str, ...]:
    """행에 붙일 경고. **실측에서 실제로 걸리는 것만** 만든다.

    ⚠ `verification` 은 넣지 않는다 — 표본 60/60 이 전부 `verified` 라 4090 급에선
    아무도 안 보는 열이 하나 늘 뿐이다. `duration` 도 최소 5일·중앙 65일이라 가드가
    아니라 열로만 둔다. 짐작으로 배지를 만들면 표만 시끄러워진다.

    ⚠ **세대 경고가 `cuda_max_good` 으로는 안 잡힌다.** Blackwell 호스트는 12.8~13.3 을
    보고해 `MIN_CUDA` 검사를 통과한다 — 그래서 `compute_cap` 을 따로 본다.
    """
    out: list[str] = []
    support = gpu_support(compute_cap)
    if support == "too_new":
        out.append("우리 이미지(cu126)에 이 GPU 커널이 없습니다 — cu128 로 다시 구워야 합니다")
    elif support == "too_old":
        out.append("너무 구형이라 이미지에 이 GPU 커널이 없습니다")
    if cpu_cores < MIN_CPU_CORES:
        out.append(f"CPU {cpu_cores:g}코어 — 데이터로더가 굶습니다")
    if cuda_max_good < MIN_CUDA:
        out.append(f"CUDA {cuda_max_good:g} — 이미지가 요구하는 {MIN_CUDA} 미만")
    if disk_space_gb < disk_gb:
        out.append(f"디스크 {disk_space_gb:g}GB — 요청한 {disk_gb:g}GB 보다 작습니다")
    return tuple(out)


@dataclass(frozen=True)
class GpuModel:
    """카탈로그 한 줄 — "이 기종이 있고, 대충 얼마고, 우리 이미지로 도는가"."""

    name: str
    compute_cap: float | None
    #: `gpu_support()` 의 판정
    support: str
    vram_gb: float
    #: **지금 필터에서** 본 오퍼 수. ⚠ 새로고침마다 흔들리는 값이다(실측: 같은 질의
    #: 3회에 총 행수 234~238, 한 기종 안에서 18행 중 10행만 같은 id). 확정 수량이
    #: 아니라 "이번에 본 수" 다 — 화면도 그렇게 말해야 한다.
    offers_seen: int
    #: 참고용 최저 시간당. ⚠ 표의 값과 같은 기준(디스크 포함)으로 계산해야 한다 —
    #: 여기만 `dph_total` 을 쓰면 고를 땐 싸 보이고 표에선 비싸지는 화면이 된다
    min_hourly: float
    #: 지금 필터로 실제 기계가 있나. `False` 면 골라도 표가 빈다
    available: bool
    #: 없으면 왜 없는지. ⚠ **조용히 빈 목록을 주지 않기 위한 필드다** — 사용자가
    #: RTX 5090 을 고르면 시장에 수십 대가 있는데도 0행이 뜨고, 이유를 아무 데도
    #: 안 적어 주면 화면이 고장난 줄 안다
    reason: str | None = None


@dataclass(frozen=True)
class SSHTarget:
    """빌린 기계에 들어가는 주소. `SSHRunner` 가 이걸로 붙는다."""

    host: str
    port: int = 22
    user: str = "root"
    key_path: str = ""


@dataclass(frozen=True)
class Instance:
    """빌린 기계 하나. **돈이 걸린 유일한 객체다.**

    ⚠ `label` 은 장식이 아니라 **고아를 찾는 유일한 단서**다. 레지스트리를 잃어도
    라벨이 붙어 있으면 "이건 우리 것" 을 알 수 있다(§6-3).
    """

    id: int
    label: str
    status: str
    gpu_name: str
    rate_usd_h: float
    ssh: SSHTarget | None
    image: str = ""
    #: 사람이 읽을 진행 메시지 (pull 중이면 그 줄이 온다)
    message: str = ""

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass(frozen=True)
class Template:
    """미리 올려 둔 학습 템플릿 한 벌."""

    id: int
    name: str
    image: str
    tag: str | None
    #: ⚠ `create instance --template_hash` 가 쓰는 값. **고칠 때마다 바뀐다**(`id` 는
    #: 그대로) — 그래서 어디에도 박아 두지 않고 **쓰기 직전에 조회한다**(§12-7).
    hash_id: str
    disk_gb: float
    description: str
    #: 'full' | 'slim' | '' — 이름에서 읽는다. 기본값 고르기와 설명에 쓴다
    #:
    #: ⚠ **둘 다 학습할 수 있다.** 한때 slim 을 "학습 불가" 로 막았는데 틀렸다 —
    #: 이미지 안에 lerobot 이 없는 것은 맞지만, `bootstrap.sh` 가 첫 부팅 때
    #: `install-stack.sh` 로 **full 과 같은 버전**을 깐다. 다른 것은 "언제" 뿐이다.
    variant: str


@runtime_checkable
class CloudProvider(Protocol):
    """§8 W2 의 계약.

    ⚠ **띄우는 동사와 끄는 동사는 같이 온다.** 하나만 있으면 "빌릴 수는 있는데 끌 수는
    없는" 상태가 되고, 그건 돈이 새는 구조다(§6).
    """

    name: str

    # ── 조회 ──
    def search(self, f: OfferFilter) -> list[Offer]: ...
    def templates(self) -> list[Template]: ...
    def whoami(self) -> dict: ...

    # ── 수명 ──
    def create(self, offer_id: int, *, template_hash: str, disk_gb: float,
               label: str) -> Instance: ...
    def list_instances(self) -> list[Instance]: ...
    def status(self, instance_id: int) -> Instance | None: ...
    def destroy(self, instance_id: int) -> bool: ...
