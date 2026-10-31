#!/usr/bin/env python3
"""grasp-guard — 파지 위치가 어긋나 팔이 물체를 **누르기 시작하면** 관절 토크로 알아채고 원위치로 돌린다.

사과 분류 시연에서 정책이 파지 자세를 잘못 잡으면 그리퍼가 사과 위를 짓누른다. 그러면 둘 중 하나다 —
사과가 미끄러져 튕겨 나가거나, 팔이 계속 밀어 관절 드라이버가 과전류로 **스스로 끊는다**(뻗는다).
끊긴 관절은 간섭물을 치워도 안 돌아오고 0x150 리셋만 살린다. 그런데 그 리셋은 추론 중엔 거절되고,
원위치 파킹도 관절 이상이면 거절된다 — 그래서 **끊기기 전에** 푸는 것이 이 플러그인의 일이다.

## 왜 토크인가

누르는 동안 팔은 **막혀 있다** — 관절은 거의 안 움직이는데 명령은 계속 아래를 향한다. 관절 상태만 보면
정상 파지(내려가서 잠깐 서고 닫는다)와 구별이 안 된다. 다른 것은 토크 하나다. robotd 가 20Hz 로 관절
토크를 재고 있고(`piper_robot/load.py`), 게이트웨이 `GET /api/robots/load` 가 그 값을 낸다(범위 `robot.read`).

임계는 2026-10-31 사과 분류 시연(.117, 양팔 SmolVLA-S) 실측이다 (robotd 누적 최대):

    can1 오른팔, 21:52~22:04 노랑 7회차      J2 4.7 · J3 3.4 · J4 3.8 · J5 5.8 N·m
    can0 왼팔, 21:57:41 (빨강 회차 30, 타임아웃)  J2 8.9 · J3 7.6 · J4 5.0 · J5 9.0 N·m
                                             6N·m 을 넘고 0.2초 만에 9N·m — 빠르다
    드라이버가 스스로 끊은 기록 (can3 J5)      10.3A ≈ 9.9N·m (`load.py` 머리말)

그래서 **지속 임계 6.0N·m·0.15초 + 즉시 임계 7.0N·m** 두 겹이다. 지속은 가감속의 순간 봉우리를 넘기고,
즉시는 0.2초 만에 치솟는 누름을 기다리지 않는다 — 10Hz 표본에서 지속 0.15초는 세 표본(0.2초)이라 그 사고에선
이미 봉우리다. 즉시 임계를 정상 최대보다 1.2 위(7.0)에 둬 한 표본 앞에서 잡는다(재현: 5.0→6.2→7.0→8.9 를
7.0 에서 끊는다). ⚠ 정상 최대(J5 5.8)와 지속 임계(6.0) 사이가 좁다 —
헛 발동이 보이면 상태의 `peaks`(관절별 최대)를 보고 그 팔의 그 관절만 올린다. 헛 발동의 값은 원위치 한 번(~10초)이고
놓친 값은 시연이 멈추는 것이라 낮은 쪽에 뒀다.

## 팔마다 다른 것 — 임계 둘뿐

임계(`<팔>_limits`)와 즉시 임계(`<팔>_hard_nm`)는 팔마다 둔다. 양팔은 맡은 상자가 달라 평소 토크가 다르고
(위 실측에서 임계에 붙은 건 오른팔 J5 하나다), 한 팔을 올리려고 다른 팔까지 무뎌지면 안 된다. 나머지는 공통이다 —
양팔 정책이 하나라 리셋·일시정지는 어느 팔이 걸리든 두 팔에 같이 걸리고, 파킹은 두 팔을 같은 원점으로 한 번에 보낸다.

## 돌리는 길 — 리셋(원위치)

`self.robot.reset()` → 래퍼가 다음 제어 루프에서 파킹한다(`wrapper/parking_controller.py`): 1단계가
joint1·2 만 원점으로 보내고 **나머지는 지금 자리에 세운다** — 누르던 힘이 그 순간 풀리고 그리퍼가 들린다.
2단계가 전체를 원점으로 보내고, 끝나면 래퍼가 **스스로 재개**한다(같은 지시문으로 처음부터 다시 잡는다).
일시정지(pause)는 그보다 늦다 — 명령이 끊긴 뒤 robotd 데드맨(300ms)이 현재 자세로 세울 때까지 계속 민다.

리셋을 보낸 뒤 팔이 원위치(`HOME`)에 닿는지 shm 상태로 본다:

- 닿으면 `rearm_s` 뒤에 다시 감시한다 — 재출발 순간의 토크는 세지 않는다
- `stall_s` 동안 원위치로 다가가지 않으면 **파킹이 거절된 것**이다(관절 이상 → `check_healthy` 실패 →
  래퍼는 파킹 없이 곧장 재개한다). 일시정지하고 사람을 부른다 — 0x150 리셋이 필요하다
- `trip_window_s` 안에 `max_trips` 번 넘게 누르면 정책이 같은 자리를 계속 누르는 것이다 — 일시정지

⚠ **파킹 도중의 일시정지는 먹지 않는다** — 래퍼의 리셋 처리가 파킹을 마치고 `_paused=False` 로 재개한다.
그래서 멈출 때(`_halt`)는 처음 `REASSERT_S` 동안 1초마다 일시정지를 다시 보낸다(멱등이다). 그 뒤에도
팔이 움직이면 누군가(사람·다른 플러그인) 다시 켠 것이다 — 싸우지 않고 다시 감시한다.

추론이 안 돌 때는 기록만 한다 — 그 토크는 정책이 낸 게 아니다(조그·손으로 민 것).

    piper-plugin run plugins/grasp-guard --arm left=<CAN> [--arm right=<CAN>] \
        --api http://127.0.0.1:8000 [--dry-run]
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass

from piper_plugin import Plugin

#: 원위치 — `lerobot_robot_piper.motors.tables.INITIALIZE_POSITION` 과 같다(정규화 눈금, 그리퍼 제외).
#: 리셋 파킹이 팔을 여기로 보낸다 — 닿았나로 복귀가 됐는지 본다
HOME = {"joint1": 0.0, "joint2": -100.0, "joint3": 100.0, "joint4": 0.0, "joint5": 35.0, "joint6": 0.0}

#: 멈춘 뒤 일시정지를 다시 보내는 시간(초) — 파킹 최대(1단계 8 + 2단계 10)를 덮는다
REASSERT_S = 20.0
#: 원위치로 "다가갔다" 고 칠 최소 거리 줄어듦 (정규화 눈금)
PROGRESS = 2.0
#: 멈춘 뒤 팔이 움직이는지 보는 창(초)과 그 창의 관절 변동 하한 — apple-sort 의 쉼 판정과 같은 눈금
MOTION_WINDOW_S = 1.0
MOTION_TOL = 2.0
#: 토크 표본 간격이 이보다 벌어지면 지속 시간을 처음부터 다시 센다 (`load.MAX_GAP_S` 와 같은 이유)
MAX_GAP_S = 0.5


def parse_limits(text: str) -> dict[str, float]:
    """`joint2 = 6.0` 줄들 → `{joint2: 6.0}`. `#` 줄은 주석."""
    out: dict[str, float] = {}
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        joint, sep, nm = line.partition("=")
        joint = joint.strip()
        if not sep or joint not in HOME:
            raise ValueError(f"임계 줄은 `joint1..joint6 = N·m` 모양이어야 한다: {line!r}")
        out[joint] = float(nm)
    return out


@dataclass(frozen=True)
class Trip:
    joint: str
    nm: float
    limit: float
    held_s: float
    #: `hard` 즉시 임계 · `sustained` 지속 임계
    kind: str


class PressWatch:
    """팔 하나의 관절 토크 → 누름 판정. **순수 로직이다** — 시각을 인자로 받으므로 하드웨어 없이 경계를
    시험하고, 기록해 둔 토크에 리플레이해 "켰다면 몇 번 울렸나" 를 셀 수 있다 (`load.LoadWatch` 와 같은 이유)."""

    def __init__(self, limits: dict[str, float], *, hard_nm: float, dwell_s: float) -> None:
        self.limits = dict(limits)
        self.hard_nm = float(hard_nm)
        self.dwell_s = float(dwell_s)
        self.over_since: dict[str, float | None] = {j: None for j in self.limits}
        #: 판정과 무관하게 쌓는 관절별 최대 — 임계를 고를 근거다
        self.peak: dict[str, float] = {j: 0.0 for j in self.limits}
        self._last_t: float | None = None

    def feed(self, t: float, nm: dict[str, float]) -> Trip | None:
        """표본 하나. 넘은 관절 중 토크가 가장 큰 것 하나를 돌려준다 — 서 있는 동안 매번 돌려준다
        (끊는 쪽이 `clear()` 로 다시 센다)."""
        gap = self._last_t is not None and not (0.0 <= t - self._last_t <= MAX_GAP_S)
        self._last_t = t
        worst: Trip | None = None
        for joint, limit in self.limits.items():
            if joint not in nm:
                continue
            v = abs(float(nm[joint]))
            self.peak[joint] = max(self.peak[joint], v)
            if gap:
                self.over_since[joint] = None
            trip = None
            if v >= limit:
                if self.over_since[joint] is None:
                    self.over_since[joint] = t
                held = t - self.over_since[joint]
                if held >= self.dwell_s:
                    trip = Trip(joint, v, limit, held, "sustained")
            else:
                self.over_since[joint] = None
            if v >= self.hard_nm:
                trip = Trip(joint, v, self.hard_nm, t - (self.over_since[joint] or t), "hard")
            if trip and (worst is None or trip.nm > worst.nm):
                worst = trip
        return worst

    def clear(self) -> None:
        """지속 판정만 지운다 — 최대는 측정값이라 남긴다."""
        self.over_since = {j: None for j in self.limits}
        self._last_t = None


def describe(slot: str, iface: str, trip: Trip) -> str:
    """누름 문구 — 로그·상태·저널이 같은 말을 한다."""
    if trip.kind == "hard":
        return f"{slot}({iface}) {trip.joint}: 토크 {trip.nm:.1f}N·m ≥ 즉시 임계 {trip.limit:.1f}"
    return f"{slot}({iface}) {trip.joint}: 토크 {trip.nm:.1f}N·m ≥ {trip.limit:.1f} 가 {trip.held_s:.2f}초 지속"


class GraspGuard(Plugin):
    def setup(self):
        self.slots = [k for k in ("left", "right") if self.arms.get(k)]
        if not self.slots:
            self.stop("팔 슬롯(left)을 골라라 — 어느 팔의 토크를 볼지 모른다")
        self.watch = {}
        for k in self.slots:                                    # 임계는 팔마다 (머리말 「팔마다 다른 것」)
            limits = parse_limits(self.config[f"{k}_limits"])
            if not limits:
                self.stop(f"{k}_limits 가 비었다 — `joint2 = 6.0` 처럼 한 줄 이상 적어라")
            self.watch[k] = PressWatch(limits, hard_nm=float(self.config[f"{k}_hard_nm"]),
                                       dwell_s=float(self.config["dwell_s"]))
        self.acting = not self.config.get("dry_run") and self.config["action"] != "log"
        self.phase = "guarding"
        self.trips: deque[float] = deque()
        self.n_trips = 0
        self.last_trip = ""
        self.halt_reason = ""
        self.inference = False
        self._activity_at = 0.0
        self._state_at = 0.0
        self._quiet_until = 0.0
        # 복귀 — (보낸 시각, 지금까지 가장 가까웠던 거리, 그때 시각, 원위치에 닿은 시각)
        self._recover: list | None = None
        self._halt_at = 0.0
        self._reassert_at = 0.0
        self._motion: deque = deque()
        self.run_id = self.env.run_id or time.strftime("run_%Y%m%d_%H%M%S")
        self.journal_path = self.env.runtime_dir / "runs" / f"{self.run_id}.jsonl"
        self.interval = 1.0 / max(float(self.config["poll_hz"]), 0.5)
        for k, w in self.watch.items():
            self.log.info("감시 — %s(%s) 임계 %s · 즉시 %.1fN·m", k, self.arms[k], w.limits, w.hard_nm)
        self.log.info("누르면 %s%s", self.config["action"], "" if self.acting else " (기록만)")

    # ── 읽기 ──

    def _load(self, slot: str) -> dict[str, float] | None:
        """robotd 가 잰 지금 관절 토크(N·m). 못 읽으면 None."""
        r = self.api.get(f"/api/robots/load?iface={self.arms[slot]}", timeout_s=1.0)
        if r.status == 403:
            # 옛 게이트웨이 — `robot.read` 에 부하 조회가 없다 (backend/app/core/plugin_scopes.py)
            self.stop("토크를 읽을 권한이 없다 — 게이트웨이의 robot.read 범위에 GET /api/robots/load 가 "
                      "들어간 버전이 필요하다")
        if not r.ok:
            return None
        joints = ((r.json() or {}).get("status") or {}).get("joints") or {}
        return {j: float(row.get("now_nm") or 0.0) for j, row in joints.items()}

    def _pose(self) -> dict[str, dict[str, float]] | None:
        """고른 팔 전부의 관절(정규화) — 하나라도 없거나 낡았으면 None (모르면 닿았다고 보지 않는다)."""
        out = {}
        for k in self.slots:
            s = self.arm(k).read()
            if s is None or not s.fresh(1.0):
                return None
            out[k] = {j: v for j, v in s.joints.items() if j in HOME}
        return out

    def _home_dist(self, pose: dict[str, dict[str, float]]) -> float:
        return max(abs(v - HOME[j]) for joints in pose.values() for j, v in joints.items())

    def _refresh_activity(self, t: float) -> None:
        if t - self._activity_at < 1.0:
            return
        self._activity_at = t
        running = self.robot.inference_running()
        if running is not None:
            self.inference = running

    # ── 루프 ──

    def loop(self):
        t0 = time.monotonic()
        self._refresh_activity(t0)
        torque = {}
        for k in self.slots:
            nm = self._load(k)
            if nm is None:
                continue
            torque[k] = {j: round(nm[j], 1) for j in self.watch[k].limits if j in nm}
            trip = self.watch[k].feed(t0, nm)
            if trip is not None and self.phase == "guarding" and t0 >= self._quiet_until:
                self._on_trip(k, trip, nm, t0)
        if self.phase == "recovering":
            self._check_recovery(t0)
        elif self.phase == "halted":
            self._check_halted(t0)
        # 상태 KV 는 화면용이라 2Hz 면 된다 — 단계가 바뀌면 바로
        if t0 - self._state_at >= 0.5 or self.phase != self._state.get("phase"):
            self._state_at = t0
            self.state(phase=self.phase, inference=self.inference, torque=torque, trips=self.n_trips,
                       peaks={k: {j: round(v, 1) for j, v in w.peak.items()} for k, w in self.watch.items()},
                       last_trip=self.last_trip, halt=self.halt_reason,
                       error="" if len(torque) == len(self.slots) else "토크를 못 읽은 팔이 있다 (robotd·연결)")
        rest = self.interval - (time.monotonic() - t0)
        if rest > 0:
            self.sleep(rest)

    def _on_trip(self, slot: str, trip: Trip, nm: dict[str, float], t: float) -> None:
        text = describe(slot, self.arms[slot], trip)
        self.n_trips += 1
        self.last_trip = f"{time.strftime('%H:%M:%S')} {text}"
        for w in self.watch.values():
            w.clear()
        pose = self._pose()
        rec = {"event": "trip", "slot": slot, "iface": self.arms[slot], **asdict(trip),
               "torque": {j: round(v, 2) for j, v in nm.items()},
               "pose": {k: {j: round(v, 1) for j, v in p.items()} for k, p in (pose or {}).items()},
               "inference": self.inference}
        if not self.inference or not self.acting:
            # 추론이 안 돌면 우리 정책의 누름이 아니다. 기록만 하고 한동안 같은 말을 안 한다
            self._quiet_until = t + 2.0
            self.log.warning("누름 — %s (%s)", text, "추론 없음 · 기록만" if self.acting else "기록만")
            self._emit({**rec, "action": "log"})
            return
        self.trips.append(t)
        while self.trips and t - self.trips[0] > float(self.config["trip_window_s"]):
            self.trips.popleft()
        if len(self.trips) > int(self.config["max_trips"]):
            self._emit({**rec, "action": "halt"})
            self._halt(f"{self.config['trip_window_s']}초 안에 {len(self.trips)}번 눌렀다 — 같은 자리를 계속 "
                       f"누른다. 판 위 물체 자리·정책을 볼 차례", t)
            return
        act = self.config["action"]
        self.log.warning("누름 — %s → %s", text, act)
        self._emit({**rec, "action": act})
        if act == "estop":
            self.robot.estop()                                  # 플러그인도 같이 선다 (E-stop 대상)
            self.stop(f"누름 — {text} → E-stop")
        elif act == "pause":
            self._halt(f"누름 — {text}", t)
        else:
            r = self.robot.reset()
            if not r.ok:
                self._halt(f"누름 — {text} · 원위치 리셋이 거절됐다 ({r.status})", t)
                return
            dist = self._home_dist(pose) if pose else float("inf")
            self._recover = [t, dist, t, None]
            self.phase = "recovering"

    def _check_recovery(self, t: float) -> None:
        since, best, best_at, reached = self._recover
        if reached is not None:
            if t - reached >= float(self.config["rearm_s"]):  # 원위치 뒤 재출발 — 이제 다시 센다
                self.log.info("원위치 복귀 %.1f초 — 다시 감시한다", reached - since)
                self._emit({"event": "recovered", "took_s": round(reached - since, 2)})
                self._rearm(forget=False)                       # 반복은 계속 센다 — 같은 자리를 또 누르면 멈춘다
            return
        pose = self._pose()
        dist = self._home_dist(pose) if pose else None
        if dist is not None and dist <= float(self.config["home_tol"]):
            for w in self.watch.values():                       # 재출발 순간의 토크는 세지 않는다
                w.clear()
            self._recover[3] = t
            return
        if dist is not None and dist < best - PROGRESS:
            self._recover[1:3] = [dist, t]
        if t - self._recover[2] > float(self.config["stall_s"]):
            far = "팔 상태를 못 읽는다 (robotd·shm)" if dist is None else f"원위치까지 {dist:.0f} 남았다"
            self._halt(f"리셋을 보냈는데 팔이 원위치로 안 간다 ({far}) — 관절 이상으로 파킹이 거절됐을 수 있다. "
                       f"추론을 멈추고 리셋(0x150)한 뒤 다시 켜라", t)
        elif t - since > float(self.config["recover_s"]):
            self._halt(f"{self.config['recover_s']}초 안에 원위치에 못 닿았다 (원위치까지 {dist or 0:.0f})", t)

    def _halt(self, reason: str, t: float) -> None:
        """일시정지하고 사람을 부른다. 파킹 중이면 먹지 않으므로 `_check_halted` 가 다시 보낸다."""
        self.phase = "halted"
        self.halt_reason = reason
        self._halt_at = self._reassert_at = t
        self._recover = None
        self._motion.clear()
        r = self.robot.pause()
        self.log.warning("멈춤 — %s (일시정지 %s)", reason, r.status)
        self._emit({"event": "halt", "reason": reason, "paused": r.ok})

    def _check_halted(self, t: float) -> None:
        if t - self._halt_at < REASSERT_S:
            if t - self._reassert_at >= 1.0:
                self._reassert_at = t
                self.robot.pause()
            return
        # 그 뒤에도 팔이 움직이면 누군가 다시 켰다 — 싸우지 않고 다시 감시한다
        pose = self._pose()
        if pose is None:
            return
        self._motion.append((t, [v for p in pose.values() for v in p.values()]))
        while self._motion and t - self._motion[0][0] > MOTION_WINDOW_S:
            self._motion.popleft()
        spread = max((max(c) - min(c) for c in zip(*(v for _, v in self._motion))), default=0.0)
        if spread >= MOTION_TOL:
            self.log.info("멈춘 뒤 팔이 다시 움직인다 (추론 재개) — 다시 감시한다")
            self._emit({"event": "rearm", "by": "motion"})
            self._rearm()

    def _rearm(self, *, forget: bool = True) -> None:
        """다시 감시. `forget` — 반복 횟수도 지운다. 사람이 손을 본 뒤(버튼·재개)에만 지운다."""
        self.phase = "guarding"
        self.halt_reason = ""
        self._recover = None
        if forget:
            self.trips.clear()
        for w in self.watch.values():
            w.clear()

    def on_command(self, name, args):
        if name == "rearm":
            self.log.info("다시 감시 (버튼)")
            self._emit({"event": "rearm", "by": "command"})
            self._rearm()

    def _emit(self, rec: dict) -> None:
        """저널(`.runtime/runs/<run_id>.jsonl`) + 토픽 `grasp_guard` — 다른 플러그인(apple-sort)이 읽을 수 있다."""
        rec = {"ts": time.time(), "run": self.run_id, **rec}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            self.publish("grasp_guard", rec)
        except Exception as exc:                                # 버스가 없어도 감시는 돈다
            self.log.debug("토픽 발행 실패: %s", exc)


if __name__ == "__main__":
    raise SystemExit(GraspGuard.main())
