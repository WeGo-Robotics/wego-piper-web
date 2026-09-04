"""관절 검사 — 움직이면서 재고 관절끼리 견준다 (feature/joint-diagnostics.md).

⚠ **모션 계산은 순수 함수다.** 하드웨어 없이 부를 수 있어야 진폭·여유·위상이
맞는지 팔을 안 움직이고 확인할 수 있다. 진단이 사고의 원인이 되면 안 된다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: 몇 주기 돌리나.
CYCLES = 2

#: 강도별 진폭 상한 (도).
#:
#: ⚠ **작게 흔들면 부하가 안 걸린다.** ±10° / 15.7°/s 로는 토크가 0.17 N·m 밖에
#:   안 나왔다 — 그 정도로는 멀쩡한 관절과 나쁜 관절이 구분되지 않는다. 관절
#:   이상은 **부하가 걸릴 때** 드러난다는 것이 이 검사의 전제다.
AMPLITUDES_DEG = {"gentle": 10.0, "normal": 20.0, "strong": 30.0}
DEFAULT_INTENSITY = "normal"

#: 팔이 허용하는 최대 속도의 몇 할까지 쓰나.
#:
#: ⚠ **더 빠르게는 못 간다.** 관절의 설정 최대 속도가 0.3 rad/s(17.2°/s)라, 그보다
#:   빠른 목표를 주면 팔이 못 따라오고 그 미달이 "추종 오차" 로 기록된다 — 검사가
#:   팔의 한계를 관절 이상으로 오독하는 셈이다. 그래서 **폭을 키우고 주기를 그
#:   한계에 맞춰 늘린다.** 부하는 속도가 아니라 이동 범위에서 온다.
SPEED_FRACTION = 0.8
#: 최대 속도를 못 읽었을 때 가정할 값 (도/초). 실측 Piper 기본이 17.2 다.
FALLBACK_SPD_DEG_S = 17.2
#: 주기 하한 (초). 아무리 작은 진폭이라도 이보다 빠르게 흔들지 않는다.
MIN_PERIOD_S = 2.0
#: 가동범위 대비 진폭 비율.
RANGE_FRACTION = 0.25
#: 설정 한계에서 남기는 여유 (도). 한계에 닿으면 그 자체가 이상 신호가 된다.
LIMIT_MARGIN_DEG = 5.0
#: 한계를 못 읽었을 때의 진폭 (도).
#:
#: ⚠ 안전층(`clamp_range`)이 캘리브레이션 범위를 자르므로 위험하지는 않다.
#:   다만 **한계에 닿으면 `angle_limit` 플래그가 서고, 그게 측정 결과에 섞인다** —
#:   검사가 자기 결론을 만들어내는 셈이다. 모르면 작게 흔든다.
UNKNOWN_LIMIT_AMP_DEG = 5.0
#: 전체 측정에서 관절마다 어긋나게 주는 위상 (도). 한꺼번에 같은 방향으로
#: 최대 속도를 내면 팔 전체가 크게 흔들린다.
PHASE_STEP_DEG = 60.0
#: 샘플 주기 (Hz).
SAMPLE_HZ = 50.0


@dataclass
class JointPlan:
    """이 관절을 얼마나 흔들 것인가."""

    joint: str
    center_deg: float
    amplitude_deg: float
    period_s: float = MIN_PERIOD_S
    phase_deg: float = 0.0
    #: 진폭이 깎였으면 그 이유 — 화면이 사람에게 그대로 보여준다.
    note: str = ""

    def target_deg(self, t: float) -> float:
        ph = math.radians(self.phase_deg)
        return self.center_deg + self.amplitude_deg * math.sin(
            2 * math.pi * t / self.period_s + ph)

    @property
    def peak_speed_deg_s(self) -> float:
        """사인파의 최대 속도 — 화면이 "얼마나 빠른가" 를 말할 근거."""
        return round(self.amplitude_deg * 2 * math.pi / self.period_s, 1)


@dataclass
class Plan:
    joints: list[JointPlan] = field(default_factory=list)
    duration_s: float = MIN_PERIOD_S * CYCLES
    intensity: str = DEFAULT_INTENSITY

    def to_dict(self) -> dict:
        return {"duration_s": round(self.duration_s, 1), "intensity": self.intensity,
                "joints": [{"joint": j.joint, "center_deg": round(j.center_deg, 2),
                            "amplitude_deg": round(j.amplitude_deg, 2),
                            "period_s": round(j.period_s, 1),
                            "peak_speed_deg_s": j.peak_speed_deg_s,
                            "phase_deg": j.phase_deg, "note": j.note}
                           for j in self.joints]}


def period_for(amplitude_deg: float, max_spd_deg_s: float | None) -> float:
    """이 진폭을 팔의 속도 한계 안에서 흔들려면 주기가 얼마여야 하나.

    ⚠ 사인파의 최대 속도는 `A·2π/T` 다. 팔의 한계보다 빠른 목표를 주면 못 따라
    오고, 그 미달이 추종 오차로 기록되어 **검사가 팔의 한계를 관절 이상으로
    오독한다.** 그래서 속도가 아니라 주기를 늘려 폭을 키운다.
    """
    v = (max_spd_deg_s or FALLBACK_SPD_DEG_S) * SPEED_FRACTION
    return max(MIN_PERIOD_S, round(amplitude_deg * 2 * math.pi / max(v, 0.1), 1))


def plan_amplitude(center_deg: float, lo_deg: float | None,
                   hi_deg: float | None, cap_deg: float | None = None) -> tuple[float, str]:
    """이 관절을 얼마나 흔들 수 있나. `(진폭, 깎인 이유)`.

    ⚠ **세 가지 중 가장 작은 것**이다 — 상한, 가동범위 비율, 그리고 지금 자세에서
    한계까지 남은 거리. 마지막을 빼먹으면 한계 근처에 있는 관절이 한계를 때리고,
    그 때림이 "이상" 으로 기록되어 **검사가 자기 결론을 만들어낸다.**
    """
    cap = cap_deg or AMPLITUDES_DEG[DEFAULT_INTENSITY]
    if lo_deg is None or hi_deg is None or hi_deg <= lo_deg:
        return min(UNKNOWN_LIMIT_AMP_DEG, cap), "한계를 몰라 보수적으로"
    amp, note = cap, ""
    if True:
        by_range = (hi_deg - lo_deg) * RANGE_FRACTION
        if by_range < amp:
            amp, note = by_range, "가동범위 기준"
        room = min(center_deg - (lo_deg + LIMIT_MARGIN_DEG),
                   (hi_deg - LIMIT_MARGIN_DEG) - center_deg)
        if room < amp:
            amp, note = max(room, 0.0), "한계까지 여유 부족"
    return round(amp, 2), note


def build_plan(centers: dict[str, float], limits: dict[str, tuple],
               joints: list[str], intensity: str = DEFAULT_INTENSITY,
               speeds: dict[str, float] | None = None) -> Plan:
    """검사할 관절들의 모션 계획. `joints` 가 하나면 개별, 여럿이면 전체다."""
    cap = AMPLITUDES_DEG.get(intensity, AMPLITUDES_DEG[DEFAULT_INTENSITY])
    speeds = speeds or {}
    out = Plan(intensity=intensity)
    for i, name in enumerate(joints):
        lo, hi = limits.get(name, (None, None))
        amp, note = plan_amplitude(centers.get(name, 0.0), lo, hi, cap)
        out.joints.append(JointPlan(
            joint=name, center_deg=centers.get(name, 0.0), amplitude_deg=amp,
            period_s=period_for(amp, speeds.get(name)),
            # 개별 측정에서는 위상을 어긋나게 할 이유가 없다
            phase_deg=(PHASE_STEP_DEG * i) if len(joints) > 1 else 0.0,
            note=note))
    # ⚠ **전체 측정은 주기를 하나로 맞춘다.** 관절마다 다른 주기로 돌면 위상을
    #   어긋나게 둔 뜻이 사라지고, 어느 순간 여럿이 겹쳐 최대 속도를 낸다.
    if out.joints:
        period = max(j.period_s for j in out.joints)
        for j in out.joints:
            j.period_s = period
        out.duration_s = period * CYCLES
    return out


def summarize(rows: list[dict], joints: list[str]) -> dict:
    """관절별 요약과 **튀는 놈** 표시.

    ⚠ **판정을 내리지 않는다.** "joint2 가 고장" 이 아니라 "joint2 의 추종 오차가
    다른 관절의 3.4배" 라고 쓴다. 절대 기준이 우리에게 없기 때문이고, 원인은
    사람이 봐야 하기 때문이다.
    """
    per: dict[str, dict] = {}
    for j in joints:
        errs = [abs(r[f"{j}_ctrl_minus_feedback_deg"]) for r in rows
                if r.get(f"{j}_ctrl_minus_feedback_deg") is not None]
        cur = [abs(r[f"{j}_motor_current_a"]) for r in rows
               if r.get(f"{j}_motor_current_a") is not None]
        eff = [abs(r[f"{j}_effort_nm"]) for r in rows
               if r.get(f"{j}_effort_nm") is not None]
        temps = [r[f"{j}_motor_temp_c"] for r in rows
                 if r.get(f"{j}_motor_temp_c") is not None]
        flags = sorted({f for f in ("driver_overcurrent", "stall", "driver_error",
                                    "collision", "angle_limit", "comm_error")
                        if any(r.get(f"{j}_{f}") for r in rows)})
        per[j] = {
            "samples": len(errs),
            "err_max_deg": round(max(errs), 3) if errs else None,
            "err_rms_deg": round(math.sqrt(sum(e * e for e in errs) / len(errs)), 3)
                           if errs else None,
            "current_max_a": round(max(cur), 3) if cur else None,
            "current_mean_a": round(sum(cur) / len(cur), 3) if cur else None,
            "effort_max_nm": round(max(eff), 3) if eff else None,
            "temp_rise_c": (max(temps) - min(temps)) if temps else None,
            "flags": flags,
        }
    return {"joints": per, "outliers": _outliers(per)}


#: 중앙값 대비 이 배수를 넘으면 "튄다" 고 본다. 관절끼리는 같은 모션을 했으므로
#: 배수가 절대값보다 뜻이 크다.
OUTLIER_RATIO = 2.0


def _outliers(per: dict[str, dict]) -> dict[str, list[str]]:
    """항목별로 중앙값의 `OUTLIER_RATIO` 배를 넘는 관절들."""
    out: dict[str, list[str]] = {}
    for key in ("err_max_deg", "err_rms_deg", "current_max_a", "effort_max_nm"):
        vals = [(j, d[key]) for j, d in per.items() if d.get(key)]
        if len(vals) < 3:          # 셋도 안 되면 중앙값이 뜻이 없다
            continue
        med = sorted(v for _, v in vals)[len(vals) // 2]
        if med <= 0:
            continue
        hits = [j for j, v in vals if v > med * OUTLIER_RATIO]
        if hits:
            out[key] = hits
    return out
