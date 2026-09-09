"""관절 부하 감시 — 슬립이 나는 **조건**을 잡는다 (층 1).

## 사건은 못 잡는다. 조건은 잡는다.

엔코더가 로터 쪽에 있어서, 클러치가 미끄러지는 동안 피드백은 명령을 그대로
따라간다 (`Arm.clear_error` 의 머리말, piper_sdk #120). 슬립 중의 관측치는:

    추종 오차   작다 (정상 운동과 같다)
    모터 속도   정상 (로터는 계속 돈다)
    fault       없다
    토크        높다   ← 다른 것은 이것 하나뿐

그래서 **"슬립했나"는 관절 안에서 물을 수 없다.** 대신 "슬립이 나는 구간에
들어갔나"를 묻는다 — 유지 토크를 넘긴 채 시간이 흐르는 것이 원인이고, 원인은
토크 하나로 관측된다. 무거운 것을 정상적으로 드는 중에도 뜨는 오탐은 감수한다:
놓치는 쪽이 훨씬 비싸다 (놓치면 데이터셋이 조용히 오염된다).

확정은 **층 2** 가 한다 — `Arm.clear_error` 의 `slip_raw`, 즉 0x150 리셋 전후
피드백 간극이 실제로 밀린 각도다. 여기서 경보가 떴는데 그 간극이 0 이면 그건
그냥 무거웠던 것이다. 두 층은 그렇게 짝으로 읽어야 뜻이 생긴다.

## ⚠ 임계값은 **잠정**이다 — 그래서 항상 기록한다

우리는 이 팔의 클러치 유지 토크를 모른다. 관측한 것은 둘뿐이다:

- 진단 중 can3 joint5 가 기구 한계에 걸렸을 때 **10.3A** 에서 드라이버가 스스로
  끊었다 (`diagnostics.ABORT_FLAGS` 주석)
- 2026-09-09 can0 joint2 를 손으로 바닥에 눌렀을 때 **11.4A / 13.5N·m** 까지
  갔는데 **드라이버는 안 끊었다**

⚠ 둘째가 첫째의 해석을 뒤집었다. 한때 여기에 "드라이버는 9.9N·m 에서 끊는다" 고
  적어 뒀는데, 그 10.3A 는 joint5 한 건의 값이었고 보편 한계가 아니었다.
  같은 전류라도 관절군마다 토크가 다르다는 것(계수 1.18125 vs 0.95844)까지
  겹쳐서, 한 숫자를 모든 관절의 천장으로 쓴 것이 틀렸다.

그래서 기본 임계는 그 아래 어딘가로 **잠정** 설정하고, 임계와 무관하게
**관절별 최대 토크를 계속 기록한다**. 진짜 값은 여기 쌓인 기록과 층 2 의
`slip_raw` 를 맞대어 정한다 — 코드가 미리 알 수 있는 숫자가 아니다.

`safety.py` 와 같은 이유로 **순수 로직**이다: 하드웨어 없이 경계 조건을 시험할
수 있어야 하고, 기록해 둔 에피소드에 리플레이해서 "켰다면 몇 번 울렸을까" 를
로봇을 켜기 전에 셀 수 있어야 한다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping

from piper_robot.kinematics import ARM_JOINTS

#: 전류(A) → 토크(N·m) 고정 계수. **SDK 가 쓰는 값 그대로다**
#: (`arm_feedback_high_spd.cal_effort`). 화면이 "6.0N·m 는 몇 A 인가" 를
#: 말하려면 필요하다 — 사람은 전류로 생각하는데 슬립은 토크로 난다.
#:
#: ⚠ 여기서 값을 지어내면 안 된다. 계수가 관절군마다 다른 것이 곧 "같은 전류라도
#:   손목과 어깨가 내는 힘이 다르다" 는 뜻이고, 그래서 임계도 관절마다 다르다.
EFFORT_COEFF: dict[str, float] = {
    "joint1": 1.18125, "joint2": 1.18125, "joint3": 1.18125,
    "joint4": 0.95844, "joint5": 0.95844, "joint6": 0.95844,
}


def amps_for(joint: str, nm: float) -> float:
    """토크 임계가 전류로는 몇 A 인가. 화면 표시용."""
    return nm / EFFORT_COEFF[joint]


#: 잠정 경보 임계 (N·m). 위 머리말의 9.9N·m(드라이버가 끊은 지점)보다 확실히
#: 아래여야 뜻이 있다 — 끊긴 뒤에 알려주는 경보는 경보가 아니다.
#:
#: ⚠ **이 값을 근거 있는 숫자로 착각하면 안 된다.** 관절마다 모터도 감속비도
#:   다르므로 여섯 개가 같은 값일 이유가 없다. 실기 기록이 쌓이면 `per_joint`
#:   로 관절마다 따로 준다. 지금 이 값의 역할은 "일단 무언가는 울리게 해서
#:   기록을 모으기 시작하는 것" 이다.
PROVISIONAL_WARN_NM = 6.0

#: 이만큼 **지속**해야 경보다 (초). 슬립은 준정적으로 미끄러진다 — 가감속의
#: 순간 피크는 정상이고, 그걸 세면 매 동작마다 울린다.
DEFAULT_DWELL_S = 0.30

#: 해제 비율 — 임계의 이만큼 **아래로** 내려와야 경보가 풀린다. 임계 언저리에서
#: 오르내릴 때 경보가 깜빡이는 것을 막는다.
DEFAULT_RELEASE = 0.75

#: 기록 요약 간격 (초)과 보관 개수. 1초 × 1800 = **30분**, `bus_watch` 와 같은 창.
#:
#: ⚠ **요약은 평균이 아니라 최대다.** 우리가 찾는 것은 피크인데 평균을 내면
#:   1초짜리 과부하가 주변 20 표본에 희석돼 사라진다. 눌러 담을수록 평균은
#:   "조용했다" 로 수렴한다.
HISTORY_INTERVAL_S = 1.0
HISTORY = 1800

#: 표본 간격이 이보다 벌어지면 그 구간은 누적에서 뺀다. 데몬이 멈췄다 깬
#: 시간을 "과부하가 계속됐다" 로 세면 누적 초가 통째로 거짓이 된다.
MAX_GAP_S = 1.0


@dataclass(frozen=True)
class LoadLimits:
    """언제 울릴 것인가. 관절별로 다르게 줄 수 있다."""

    #: 경보를 낼 것인가.
    #:
    #: ⚠ **꺼도 기록은 계속된다.** 끄는 것은 "울리지 마라" 지 "재지 마라" 가
    #:   아니다 — 임계가 잠정인 동안 기록은 임계를 고칠 유일한 근거라서, 그걸
    #:   같이 끄면 끈 동안의 구간을 영영 되살릴 수 없다. 경보가 시끄러워서 끈
    #:   사람은 정확히 그 시끄러웠던 구간의 숫자를 나중에 보고 싶어한다.
    enabled: bool = True
    warn_nm: float = PROVISIONAL_WARN_NM
    dwell_s: float = DEFAULT_DWELL_S
    release: float = DEFAULT_RELEASE
    #: 관절 이름 → 임계. 없는 관절은 `warn_nm` 을 쓴다.
    per_joint: Mapping[str, float] = field(default_factory=dict)

    def warn_for(self, joint: str) -> float:
        return float(self.per_joint.get(joint, self.warn_nm))


@dataclass
class JointLoad:
    """한 관절이 지금까지 겪은 것. **임계와 무관하게** 최대치는 늘 쌓인다."""

    #: 임계를 넘긴 채 이어지고 있는 구간의 시작 (없으면 None)
    over_since: float | None = None
    #: 경보가 서 있나 (dwell 을 채웠나)
    raised: bool = False
    #: 관측한 최대 토크와 그 시각
    peak_nm: float = 0.0
    peak_at: float = 0.0
    #: 임계 위에서 보낸 누적 시간과 경보 횟수
    over_s: float = 0.0
    events: int = 0
    #: 가장 최근 표본
    now_nm: float = 0.0
    now_a: float = 0.0
    #: 마지막 경보의 **문구와 시각**.
    #:
    #: ⚠ 문구를 여기 남기는 이유는 게이트웨이가 스냅샷만 보기 때문이다. 경보가
    #:   선 순간의 토크·지속시간은 그 뒤 표본에 덮여 사라지므로, 게이트웨이가
    #:   나중에 문장을 조립하면 **경보 때와 다른 숫자**를 말한다. 잰 쪽이 그때
    #:   바로 적어두는 것만이 맞는다.
    last_text: str = ""
    last_at: float = 0.0

    def to_dict(self) -> dict:
        return {"now_nm": round(self.now_nm, 3), "now_a": round(self.now_a, 3),
                "peak_nm": round(self.peak_nm, 3), "peak_at": round(self.peak_at, 2),
                "over_s": round(self.over_s, 2), "events": self.events,
                "raised": self.raised,
                "last_text": self.last_text, "last_at": round(self.last_at, 2)}


@dataclass
class LoadEvent:
    """경보 하나. 사람이 읽을 문구는 부르는 쪽이 만든다."""

    joint: str
    effort_nm: float
    current_a: float
    warn_nm: float
    held_s: float
    at: float


class LoadWatch:
    """팔 하나의 관절 부하. 표본을 먹이면 새로 선 경보를 돌려준다.

    시각을 인자로 받는다 — `time` 을 안 부르므로 테스트가 몇 분짜리 과부하를
    한순간에 흘려볼 수 있고, 녹화해 둔 표본에 그대로 리플레이할 수 있다.
    """

    def __init__(self, limits: LoadLimits | None = None,
                 history: int = HISTORY, name: str = "") -> None:
        self.limits = limits or LoadLimits()
        #: 문구에 박히는 이름(iface). 잰 쪽이 문장을 만들어야 하므로 필요하다.
        self.name = name
        self.joints: dict[str, JointLoad] = {j: JointLoad() for j in ARM_JOINTS}
        self._hist: deque = deque(maxlen=history)
        self._last_t: float | None = None
        #: 아직 기록으로 안 넘긴 구간의 관절별 최대
        self._bucket: dict[str, float] = {}
        self._bucket_t: float | None = None

    # ── 먹이기 ──

    def feed(self, t: float, effort_nm: Mapping[str, float],
             current_a: Mapping[str, float] | None = None) -> list[LoadEvent]:
        """표본 하나. **새로 선** 경보만 돌려준다 — 서 있는 동안은 조용하다.

        매 표본마다 다시 알리면 20Hz 로 같은 말이 쏟아져 로그가 묻힌다.
        """
        dt = 0.0 if self._last_t is None else t - self._last_t
        # ⚠ **끊긴 구간은 "이어졌다" 고 말하면 안 된다.** 데몬이 멈췄다 깬 사이에
        #   부하가 계속됐는지 우리는 모른다. 누적 초에서 빼는 것만으로는 모자라고,
        #   지속 판정도 처음부터 다시 세야 한다 — 안 그러면 멈춘 시간이 그대로
        #   dwell 로 계산돼 깨어나는 첫 표본에서 곧장 경보가 뜬다.
        gap = self._last_t is not None and not (0.0 <= dt <= MAX_GAP_S)
        if gap:
            # ⚠ 모아 두던 구간은 **끊기기 전 시각**으로 닫는다. 그냥 두면 깬
            #   시각이 붙어, 끊기기 직전의 피크가 30분 그래프에서 몇 분 뒤로
            #   옮겨 그려진다 — 있지도 않았던 자리에 봉우리가 선다.
            self._flush(self._last_t)
            self._bucket_t = None       # 다음 구간은 깬 시각부터 새로 센다
            dt = 0.0
        self._last_t = t

        events: list[LoadEvent] = []
        for joint in ARM_JOINTS:
            if joint not in effort_nm:
                continue
            if gap:
                self.joints[joint].over_since = None
            nm = abs(float(effort_nm[joint]))
            amp = abs(float((current_a or {}).get(joint, 0.0)))
            ev = self._step(joint, t, dt, nm, amp)
            if ev is not None:
                events.append(ev)
        self._record(t, effort_nm)
        return events

    def _step(self, joint: str, t: float, dt: float,
              nm: float, amp: float) -> LoadEvent | None:
        st = self.joints[joint]
        st.now_nm, st.now_a = nm, amp
        if nm > st.peak_nm:
            st.peak_nm, st.peak_at = nm, t

        warn = self.limits.warn_for(joint)
        if nm >= warn:
            st.over_s += dt
            if st.over_since is None:
                st.over_since = t
            elif (self.limits.enabled and not st.raised
                  and t - st.over_since >= self.limits.dwell_s):
                st.raised = True
                st.events += 1
                ev = LoadEvent(joint=joint, effort_nm=nm, current_a=amp,
                               warn_nm=warn, held_s=t - st.over_since, at=t)
                st.last_text, st.last_at = describe(self.name, ev), t
                return ev
        elif nm < warn * self.limits.release:
            # ⚠ 해제는 **임계 아래가 아니라 해제선 아래**다. 임계에 딱 붙어
            #   오르내리는 부하에서 경보가 초당 몇 번씩 깜빡이는 것을 막는다.
            st.over_since = None
            st.raised = False
        return None

    def _record(self, t: float, effort_nm: Mapping[str, float]) -> None:
        """구간 최대를 모아 1초에 한 줄씩 남긴다."""
        if self._bucket_t is None:
            self._bucket_t = t
        for joint in ARM_JOINTS:
            if joint in effort_nm:
                nm = abs(float(effort_nm[joint]))
                if nm > self._bucket.get(joint, 0.0):
                    self._bucket[joint] = nm
        if t - self._bucket_t >= HISTORY_INTERVAL_S:
            self._flush(t)

    def _flush(self, t: float | None) -> None:
        """모아 둔 구간 최대를 한 줄로 닫는다. 빈 구간은 남기지 않는다."""
        if t is None or not self._bucket:
            self._bucket, self._bucket_t = {}, None
            return
        # 값은 `ARM_JOINTS` 순서의 리스트다 — 30분 × 여섯 관절을 키 이름까지
        # 붙여 보내면 대부분이 관절 이름 문자열이 된다.
        self._hist.append({"t": round(t, 2),
                           "nm": [round(self._bucket.get(j, 0.0), 3)
                                  for j in ARM_JOINTS]})
        self._bucket = {}
        self._bucket_t = t

    # ── 읽기 ──

    def snapshot(self) -> dict:
        """지금 상태와 누적. 임계도 함께 낸다 — 화면이 숫자를 해석하려면 필요하다."""
        return {
            "joints": {j: st.to_dict() for j, st in self.joints.items()},
            "limits": {"enabled": self.limits.enabled,
                       "warn_nm": {j: self.limits.warn_for(j) for j in ARM_JOINTS},
                       "dwell_s": self.limits.dwell_s,
                       "release": self.limits.release,
                       "provisional": True},
            "raised": sorted(j for j, st in self.joints.items() if st.raised),
        }

    def history(self, limit: int | None = None) -> dict:
        rows = list(self._hist)
        return {"joints": list(ARM_JOINTS),
                "rows": rows[-limit:] if limit else rows}

    def retune(self, limits: LoadLimits) -> None:
        """임계를 갈아끼운다. **판정만 지우고 계측은 남긴다.**

        ⚠ 서 있던 경보를 안 지우면 임계를 올린 뒤에도 안 풀릴 수 있다: 임계를
          5→8 로 올려도 해제선(8×0.75=6.0)이 함께 올라가, 부하 6.0 은 새 임계
          아래인데 해제선 아래는 아니라 경보가 영영 서 있는다. 새 임계로 다시
          판정해야 할 상태를 옛 판정이 막는 셈이다.

        피크·누적·기록은 **측정값**이라 남긴다 — 임계를 바꿨다고 지난 부하가
        달라지지 않고, 오히려 그 기록이 새 임계를 고른 근거다.
        """
        self.limits = limits
        for st in self.joints.values():
            st.over_since, st.raised = None, False

    def reset(self) -> None:
        """누적을 버린다. **층 2 가 실제 슬립을 잰 직후**가 이걸 부를 자리다 —
        거기서 창이 닫히고 다음 창이 열린다."""
        self.joints = {j: JointLoad() for j in ARM_JOINTS}
        self._hist.clear()
        self._bucket, self._bucket_t, self._last_t = {}, None, None


def describe(iface: str, ev: LoadEvent) -> str:
    """경보 문구. **여기서만 만든다** — 로그와 화면이 같은 말을 해야 한다."""
    return (f"{iface} {ev.joint}: 토크 {ev.effort_nm:.1f}N·m ({ev.current_a:.1f}A) 가 "
            f"{ev.held_s:.2f}초 지속 — 임계 {ev.warn_nm:.1f}N·m. 슬립 위험 구간이다. "
            f"실제로 밀렸는지는 리셋(0x150) 전후 간극으로 확인하세요")
