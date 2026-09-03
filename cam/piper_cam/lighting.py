"""조명 측정·판정 — **순수 로직** (feature/lighting-watch.md §3·§6-1).

`graycard.py` 와 같은 자리다: 라이브(shm 프레임)와 저장본(에피소드 디코드
캐시)이 **같은 자로 잰다** — 라이브 알람의 임계와 뷰어의 눈금이 같은 단위여야
"녹화 때 경보 떴던 게 여기구나"가 숫자로 이어진다.

⚠ 입력은 **BGR** 이다 (OpenCV 기본). 세그먼트도 BGR 로 실측 확인됐다:
rsd 는 `to_bgr` 를 거쳐 발행하고(piper_rs/hub.py), camerad 는 OpenCV 프레임을
그대로 발행한다. R/B 가 뒤집히면 색 변화의 **방향**이 뒤집힌다 — 호출자가 맞춘다.

## 왜 평균 몇 개인가

조명 사건은 화면 전체가 함께 움직이는 사건이다. 64×64 축소 평균이면 충분하고,
그 덕에 게이트웨이 감시 루프에 얹어도 비용이 없다(μs 대). 히스토그램 거리·ML 은
이걸로 못 잡는 사건이 실측에서 나올 때 다시 논한다 (문서 §3-3).
"""

import math
import time
from dataclasses import dataclass, field

import numpy as np

# 축소 크기. 통계 안정(노이즈 평균화)과 비용의 균형 — graycard 도 평균 기반이다.
SMALL = 64
# 전역/국소 구분 격자. 팔·물체는 몇 칸만 움직이고 조명은 거의 전부 움직인다.
GRID = 3


# 눈금의 양끝. ±5 스톱이면 32배 밝기 차라 이 밖은 "완전히 틀렸다" 하나로 족하다.
EV_LIMIT = 5.0


def ev(luma: float) -> float:
    """평균 밝기 → 노출 눈금(스톱). 0.0 = 목표 노출, +1.0 = 두 배 밝다.

    ⚠ 0 점은 회색카드 보정이 맞추는 목표(`graycard.TARGET_LUMA`)와 **같은 값을
    쓴다.** 다르면 "0.0 EV" 와 "보정 완료" 가 서로 다른 밝기를 뜻하게 되어,
    화면 두 곳이 같은 카메라를 두고 다른 말을 한다.

    스톱(log₂)인 이유: 밝기는 곱으로 변한다. 노출을 두 배 주면 luma 도 두 배라
    "+1" 이 언제나 같은 크기의 조작을 뜻한다 — 뺄셈 눈금은 어두운 쪽에서 뭉갠다.
    """
    from piper_cam.graycard import TARGET_LUMA

    if luma <= 0:
        return -EV_LIMIT          # log₂(0) 은 -∞ 다. 눈금 끝으로 붙인다.
    return round(max(-EV_LIMIT, min(EV_LIMIT, math.log2(luma / TARGET_LUMA))), 1)


# 측광 모드 — 같은 프레임의 **어디를 보고 재느냐**다.
#
# ⚠ 셋 다 **같은 목표**(`graycard.TARGET_LUMA`)를 쓴다. 목표가 모드마다 다르면
#   같은 "+1.0 EV" 가 모드마다 다른 뜻이 되어 모드를 바꿔 비교하는 일 자체가
#   무의미해진다. 바뀌는 것은 "어디를 재나" 뿐이어야 한다.
METERING_MODES: tuple[str, ...] = ("average", "center", "spot")

# 스팟이 보는 창. 64×64 의 중앙 12×12 = 화면의 3.5% — 카메라의 스팟(1~5%)과 같은 크기다.
_SPOT = slice(26, 38)

# 중앙중점의 가중치 반경(정규화). 0.4 면 중앙 원 안이 전체 가중치의 약 3분의 2를
# 가져간다 — 고전적인 중앙중점(60~75%)과 같은 배분이다.
_CENTER_SIGMA = 0.4
_WEIGHTS: "np.ndarray | None" = None


def _center_weights() -> "np.ndarray":
    """중앙중점 가중치 (64,64). 한 번 만들어 재사용한다 — 프레임마다 새로
    만들면 2초 주기 × 카메라 수만큼 헛일이다."""
    global _WEIGHTS
    if _WEIGHTS is None:
        c = (SMALL - 1) / 2.0
        yy, xx = np.mgrid[0:SMALL, 0:SMALL]
        d = np.hypot(yy - c, xx - c) / np.hypot(c, c)
        _WEIGHTS = np.exp(-(d ** 2) / (2 * _CENTER_SIGMA ** 2))
    return _WEIGHTS


def meter(luma: "np.ndarray") -> dict[str, float]:
    """축소된 luma 판 → 모드별 측광값.

    셋을 **다 계산해서 실어 보낸다.** 고른 하나만 보내면 모드를 바꿀 때마다
    다음 샘플(2초)을 기다려야 하고, 무엇보다 **비교가 안 된다** — 측광이
    수상할 때 사람이 제일 먼저 하는 일이 모드를 바꿔 보는 것이다.
    """
    w = _center_weights()
    return {
        "average": round(float(luma.mean()), 1),
        "center": round(float((luma * w).sum() / w.sum()), 1),
        "spot": round(float(luma[_SPOT, _SPOT].mean()), 1),
    }


def features(frame_bgr: np.ndarray, ts: float | None = None) -> dict:
    """프레임 하나 → 조명 특징. 버스 payload 가 그대로 되는 모양이다 (문서 §5).

    반환: ``{ts, luma, sat_pct, dark_pct, log_rg, log_bg, grid[9]}``
    - luma: 0~255 평균 밝기
    - sat/dark_pct: 포화(>250)·암부(<5) 픽셀 비율 % — 노출 이상의 표지
    - log_rg/log_bg: log₂(R̄/Ḡ)·log₂(B̄/Ḡ) — 색온도/WB 의 **변화**용.
      로그라 노출 변화와 분리된다. 절대 Kelvin 은 일부러 안 구한다
    - grid: 3×3 칸별 luma — 판정의 전역성 검사용
    """
    import cv2

    if frame_bgr is None or frame_bgr.ndim != 3 or frame_bgr.shape[2] < 3:
        raise ValueError("BGR 3채널 프레임이 필요합니다")
    small = cv2.resize(frame_bgr[:, :, :3], (SMALL, SMALL),
                       interpolation=cv2.INTER_AREA).astype(np.float32)
    b, g, r = small[:, :, 0], small[:, :, 1], small[:, :, 2]
    luma = 0.114 * b + 0.587 * g + 0.299 * r
    # INTER_AREA 로 3×3 축소 = 칸별 평균 — 반복문 없이 격자가 나온다
    cells = cv2.resize(small, (GRID, GRID), interpolation=cv2.INTER_AREA)
    cell_luma = (0.114 * cells[:, :, 0] + 0.587 * cells[:, :, 1]
                 + 0.299 * cells[:, :, 2]).flatten()
    metered = meter(luma)
    eps = 1.0  # 완전 암흑에서 log(0) 방지 — 1/255 수준이라 판정에 영향 없다
    return {
        "ts": time.time() if ts is None else ts,
        # ⚠ `luma` 는 **평균 그대로 둔다.** Judge 의 급변 판정이 이걸 기준선으로
        #   쓰는데, 사람이 고른 측광 모드에 따라 뜻이 바뀌면 경보가 조용히
        #   달라진다 — 표시를 바꾸려다 안전 경보를 건드리는 셈이다.
        "luma": round(float(luma.mean()), 1),
        "metering": metered,
        "ev": {k: ev(v) for k, v in metered.items()},
        "sat_pct": round(float((luma > 250).mean() * 100), 1),
        "dark_pct": round(float((luma < 5).mean() * 100), 1),
        "log_rg": round(float(math.log2((r.mean() + eps) / (g.mean() + eps))), 3),
        "log_bg": round(float(math.log2((b.mean() + eps) / (g.mean() + eps))), 3),
        "grid": [round(float(v), 1) for v in cell_luma],
    }


@dataclass
class JudgeConfig:
    """판정 임계 — **절대 단위**다 (σ 아님: 야간 정지 장면은 분산이 0 에 수렴해
    z-score 가 폭발한다). 기본값은 문서 §3-2 의 출발점이고 실측 튜닝 대상이다."""

    luma_jump: float = 20.0     # 밝기 급변 임계 (0~255)
    color_jump: float = 0.10    # 색 급변 임계 (log₂ 단위)
    enter_count: int = 3        # 이만큼 연속이어야 경보 진입
    exit_count: int = 5         # 이만큼 연속 정상이어야 해제
    fast_tau_s: float = 2.0     # "방금" — 측정 노이즈 흡수
    slow_tau_s: float = 60.0    # "지금까지의 정상"
    global_cells: int = 7       # 9칸 중 이만큼이 같은 방향이어야 전역(조명)이다
    cell_jump: float = 10.0     # 칸이 "움직였다"로 칠 최소 이동 (luma_jump 의 절반)
    warmup_s: float = 10.0      # 기준선(slow)이 서기 전에는 판정하지 않는다


class _Ewma:
    """스칼라/벡터 공용 지수이동평균. 첫 값은 그대로 물려받는다."""

    def __init__(self, tau_s: float) -> None:
        self.tau_s = tau_s
        self.v: np.ndarray | float | None = None

    def update(self, x, dt_s: float):
        if self.v is None:
            self.v = np.array(x, dtype=np.float64) if np.ndim(x) else float(x)
        else:
            a = 1.0 - math.exp(-max(dt_s, 1e-6) / self.tau_s)
            self.v = self.v + a * (np.asarray(x, dtype=np.float64) - self.v) \
                if np.ndim(x) else self.v + a * (float(x) - self.v)
        return self.v


@dataclass
class Judge:
    """카메라 **하나**의 판정 상태 기계 (문서 §3-2).

    `update(feats, t)` 를 샘플마다 부르면 현재 활성 문제 목록을 돌려준다.
    경보의 add/clear 전이는 호출자(device_watch)가 목록 차이로 처리하므로
    여기는 "지금 무엇이 이상한가"만 답한다.

    판정 뒤에도 slow 는 새 값으로 계속 수렴한다 — 조명이 **바뀐 채 유지**되면
    경보는 한 번 뜨고 새 상태가 정상이 된다. 의도한 동작이다.
    """

    cfg: JudgeConfig = field(default_factory=JudgeConfig)

    def __post_init__(self) -> None:
        self._fast = {k: _Ewma(self.cfg.fast_tau_s) for k in ("luma", "rg", "bg", "grid")}
        self._slow = {k: _Ewma(self.cfg.slow_tau_s) for k in ("luma", "rg", "bg", "grid")}
        self._t0: float | None = None
        self._last_t: float | None = None
        self._streak: dict[str, int] = {"brightness": 0, "color": 0}
        self._calm: dict[str, int] = {"brightness": 0, "color": 0}
        self._active: dict[str, dict] = {}

    def update(self, feats: dict, t: float) -> list[dict]:
        if self._t0 is None:
            self._t0 = t
        dt = 0.0 if self._last_t is None else t - self._last_t
        self._last_t = t

        vals = {"luma": feats["luma"], "rg": feats["log_rg"],
                "bg": feats["log_bg"], "grid": feats["grid"]}
        fast = {k: self._fast[k].update(v, dt) for k, v in vals.items()}
        slow = {k: self._slow[k].update(v, dt) for k, v in vals.items()}

        if t - self._t0 < self.cfg.warmup_s:
            return []          # 기준선이 아직 없다 — 스트림 시작 직후를 판정하면 오보다

        d_luma = float(fast["luma"] - slow["luma"])
        d_cells = np.asarray(fast["grid"]) - np.asarray(slow["grid"])
        # 전역성: 전체 평균과 같은 방향으로 움직인 칸 수. 팔·물체는 몇 칸만 움직인다.
        moved = int(np.sum((np.abs(d_cells) > self.cfg.cell_jump)
                           & (np.sign(d_cells) == np.sign(d_luma or 1.0))))
        bright = abs(d_luma) > self.cfg.luma_jump and moved >= self.cfg.global_cells

        d_rg = float(fast["rg"] - slow["rg"])
        d_bg = float(fast["bg"] - slow["bg"])
        color = max(abs(d_rg), abs(d_bg)) > self.cfg.color_jump

        self._step("brightness", bright, {"type": "brightness", "delta": round(d_luma, 1)})
        self._step("color", color, {"type": "color", "delta_rg": round(d_rg, 3),
                                    "delta_bg": round(d_bg, 3)})
        return list(self._active.values())

    def _step(self, name: str, candidate: bool, detail: dict) -> None:
        """히스테리시스: 진입은 enter_count 연속, 해제는 exit_count 연속 정상."""
        if candidate:
            self._streak[name] += 1
            self._calm[name] = 0
            if self._streak[name] >= self.cfg.enter_count and name not in self._active:
                self._active[name] = detail
            elif name in self._active:
                self._active[name] = detail        # 활성 중엔 최신 수치로 갱신
        else:
            self._streak[name] = 0
            if name in self._active:
                self._calm[name] += 1
                if self._calm[name] >= self.cfg.exit_count:
                    del self._active[name]
                    self._calm[name] = 0
