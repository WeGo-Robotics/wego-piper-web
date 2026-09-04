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
            # ⚠ **켜고 시작한다.** 모터가 꺼져 있으면 명령이 아무 일도 안 하고,
            #   팔은 전압·온도를 멀쩡히 보고하므로 로그만 보면 정상인데 전부 0 이다.
            if not self.arm.enable_for_motion():
                raise RuntimeError("모터를 켜지 못했습니다 — 팔 전원과 에러 상태를 "
                                   "확인하세요 (검사는 팔을 움직여야 잽니다)")
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
            # ⚠ **끝났다는 표시가 먼저다.** 뒷정리에서 예외가 나면 `done` 이
            #   영영 안 서고 화면은 "진행 중" 으로 굳는다 — 실제로 그랬다
            #   (`_hold` 를 안 만들어 둔 채 불러서 AttributeError, 77초째 진행 중).
            #   정리는 못 해도 끝난 것은 끝난 것이다.
            self.done = True
            try:
                self._hold()
            except Exception as exc:                   # noqa: BLE001
                logger.warning("검사 종료 정지 실패 (%s): %s", self.arm.iface, exc)

    def _hold(self) -> None:
        """끝나거나 멈출 때 **그 자리에 세운다.**

        ⚠ 토크를 끊지 않는다 — 끊으면 팔이 떨어진다. 마지막 목표가 먼 곳이었으면
        팔은 계속 그리로 가므로, **현재 자세를 실제로 명령해야** 선다
        (`publish.ArmBridge._hold` 와 같은 이유).
        """
        now = self.arm.read_joints_normalized()
        if now:
            self.bridge._send(dict(now))

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
            row |= _joint_row(name, i, fb, ct, hi, lo, st, targets_deg.get(name))
        return row


def _try(fn):
    """읽기 실패는 None 이다 — 0 으로 채우면 정상값처럼 보인다."""
    try:
        return fn()
    except Exception:
        return None


def _joint_row(name: str, i: int, fb, ct, hi, lo, st, target_deg=None) -> dict:
    f = _num(fb, f"joint_{i}")
    fdeg = None if f is None else round(f / 1000.0, 4)
    # ⚠ **우리가 보낸 목표를 지령으로 쓴다.** 팔의 지령 레지스터(0x15x)는 우리
    #   명령을 되비추지 않는다 — 실측: 관절이 ±10° 로 흔들리는 내내 `ctrl_deg`
    #   가 0 이어서 **추종 오차가 통째로 진폭과 같게** 나왔다(10.068°).
    #   우리는 무엇을 시켰는지 정확히 아는데, 그걸 안 쓰고 팔에게 되물은 셈이다.
    #   안 시킨 관절은 그때만 레지스터를 읽는다(참고값).
    cdeg = round(target_deg, 4) if target_deg is not None else (
        None if (c := _num(ct, f"joint_{i}")) is None else round(c / 1000.0, 4))
    row = {
        f"{name}_feedback_deg": fdeg,
        f"{name}_ctrl_deg": cdeg,
        f"{name}_ctrl_minus_feedback_deg":
            None if (fdeg is None or cdeg is None) else round(cdeg - fdeg, 4),
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
