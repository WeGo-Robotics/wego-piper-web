"""관절 검사 실행기 — robotd 안에서 돈다 (feature/joint-diagnostics.md).

⚠ **여기가 CAN 을 쥔 쪽이다.** 명령과 측정이 한 루프에 있어야 두 값의 시각이
맞는다. 게이트웨이에서 명령하고 robotd 가 재면 추종 오차에 통신 지연이 섞여,
멀쩡한 관절이 느린 것처럼 보인다.
"""

from __future__ import annotations

import logging
import threading
import time

from piper_robot import diagnostics as D
from piper_robot.joints import normalize_joint
from piper_robot.kinematics import ARM_JOINTS

logger = logging.getLogger(__name__)

#: 관절 상태 플래그 — 로그 열 이름과 `foc_status` 속성의 짝.
FLAGS = (("driver_overcurrent", "driver_overcurrent"), ("stall", "stall_status"),
         ("driver_error", "driver_error_status"), ("collision", "collision_status"),
         ("enabled", "driver_enable_status"))


class DiagRun:
    """한 번의 검사. 상태를 들고 있고 스스로 멈출 수 있다."""

    def __init__(self, arm, bridge, plan: D.Plan):
        self.arm, self.bridge, self.plan = arm, bridge, plan
        self.rows: list[dict] = []
        self.started_at = time.time()
        self.error: str | None = None
        self.done = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="diag")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """⚠ **토크를 끊지 않는다.** 끊으면 팔이 떨어진다 — 멈춘다는 것은 그
        자리에 서는 것이다."""
        self._stop.set()

    def status(self) -> dict:
        el = time.time() - self.started_at
        return {"running": not self.done, "elapsed_s": round(el, 2),
                "duration_s": self.plan.duration_s, "samples": len(self.rows),
                "error": self.error, "plan": self.plan.to_dict()}

    # ── 실행 ──

    def _run(self) -> None:
        dt = 1.0 / D.SAMPLE_HZ
        try:
            t0 = time.monotonic()
            while not self._stop.is_set():
                t = time.monotonic() - t0
                if t > self.plan.duration_s:
                    break
                self._tick(t)
                # 남은 시간만 잔다 — 측정에 걸린 만큼 빼야 주기가 안 밀린다
                time.sleep(max(0.0, dt - ((time.monotonic() - t0) - t)))
        except Exception as exc:                       # noqa: BLE001
            self.error = str(exc)
            logger.exception("검사 실패 (%s)", self.arm.iface)
        finally:
            self._hold()
            self.done = True

    def _tick(self, t: float) -> None:
        targets = {p.joint: p.target_deg(t) for p in self.plan.joints}
        self._command(targets)
        self.rows.append(self._sample(t, targets))

    def _command(self, targets_deg: dict[str, float]) -> None:
        """목표를 **안전층을 거쳐** 보낸다.

        ⚠ `JointCtrl` 로 직접 보내면 바닥·범위·변화율·데드맨이 전부 빠진다.
        검사가 그 구멍이 되면 안 된다 — 브리지의 `_send` 를 그대로 쓴다.
        """
        now = self.arm.read_joints_normalized() or {}
        values = dict(now)
        for j, deg in targets_deg.items():
            values[j] = normalize_joint(j, deg * 1000.0)
        self.bridge._send(values)

    def _sample(self, t: float, targets_deg: dict[str, float]) -> dict:
        p = self.arm._piper
        row: dict = {"t_s": round(t, 4)}
        fb = _try(lambda: p.GetArmJointMsgs().joint_state)
        ct = _try(lambda: p.GetArmJointCtrl().joint_ctrl)
        hi = _try(lambda: p.GetArmHighSpdInfoMsgs())
        lo = _try(lambda: p.GetArmLowSpdInfoMsgs())
        st = _try(lambda: p.GetArmStatus().arm_status)
        for i, name in enumerate(ARM_JOINTS, start=1):
            row |= _joint_row(name, i, fb, ct, hi, lo, st)
        return row


def _try(fn):
    """읽기 실패는 None 이다 — 0 으로 채우면 정상값처럼 보인다."""
    try:
        return fn()
    except Exception:
        return None


def _joint_row(name: str, i: int, fb, ct, hi, lo, st) -> dict:
    f = _num(fb, f"joint_{i}")
    c = _num(ct, f"joint_{i}")
    row = {
        f"{name}_feedback_deg": None if f is None else round(f / 1000.0, 4),
        f"{name}_ctrl_deg": None if c is None else round(c / 1000.0, 4),
        f"{name}_ctrl_minus_feedback_deg":
            None if (f is None or c is None) else round((c - f) / 1000.0, 4),
    }
    h = getattr(hi, f"motor_{i}", None) if hi else None
    if h is not None:
        row |= {f"{name}_motor_speed_rad_s": round(h.motor_speed / 1000.0, 4),
                f"{name}_motor_current_a": round(h.current / 1000.0, 4),
                f"{name}_motor_pos_rad": round(h.pos / 1000.0, 5),
                f"{name}_effort_nm": round(h.effort / 1000.0, 4)}
    m = getattr(lo, f"motor_{i}", None) if lo else None
    if m is not None:
        row |= {f"{name}_driver_voltage_v": round(m.vol / 10.0, 2),
                f"{name}_bus_current_a": round(m.bus_current / 1000.0, 4),
                f"{name}_foc_temp_c": m.foc_temp, f"{name}_motor_temp_c": m.motor_temp}
        row |= {f"{name}_{col}": bool(getattr(m.foc_status, attr, False))
                for col, attr in FLAGS}
    if st is not None:
        row |= {f"{name}_angle_limit": bool(getattr(st, f"joint_{i}_angle_limit", False)),
                f"{name}_comm_error":
                    bool(getattr(st, f"communication_status_joint_{i}", False))}
    return row


def _num(msg, attr):
    v = getattr(msg, attr, None) if msg else None
    return None if v is None else float(v)
