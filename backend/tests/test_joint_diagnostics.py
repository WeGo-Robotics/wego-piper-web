"""관절 검사 — 움직이면서 재고 관절끼리 견준다 (feature/joint-diagnostics.md).

⚠ **모션 계산이 순수 함수인 이유가 여기 있다.** 진폭·여유·위상이 맞는지 팔을
안 움직이고 확인할 수 있어야 한다 — 진단이 사고의 원인이 되면 안 된다.
"""

import math

import pytest
from piper_robot import diagnostics as D


# ── 모션 ────────────────────────────────────────────────────────────────────

def test_the_swing_never_reaches_the_configured_limit():
    """⚠ 한계를 때리면 그 때림이 `angle_limit` 플래그로 기록되고, **검사가 자기
    결론을 만들어낸다.** 지금 자세에서 한계까지 남은 거리도 진폭을 깎아야 한다."""
    amp, note = D.plan_amplitude(center_deg=68.0, lo_deg=-70.0, hi_deg=70.0)
    assert amp == 0.0 and "여유" in note, (amp, note)

    amp, _ = D.plan_amplitude(center_deg=0.0, lo_deg=-70.0, hi_deg=70.0)
    assert 0 < amp <= D.MAX_AMP_DEG
    assert amp <= 70.0 - D.LIMIT_MARGIN_DEG


def test_a_narrow_joint_swings_less():
    """가동범위가 좁은 관절에 같은 진폭을 주면 그 관절만 한계에 가까워진다."""
    wide, _ = D.plan_amplitude(0.0, -150.0, 150.0)
    narrow, note = D.plan_amplitude(0.0, -30.0, 30.0)
    assert narrow < wide, (narrow, wide)
    assert note == "가동범위 기준"


def test_an_unknown_limit_swings_less_not_more():
    """⚠ 한계를 모르면 여유도 모른다. 안전층이 범위를 자르니 위험하진 않지만,
    한계에 닿으면 `angle_limit` 플래그가 서고 그게 측정에 섞인다 — 검사가 자기
    결론을 만들어내는 셈이다. 모르면 **작게** 흔든다."""
    amp, note = D.plan_amplitude(0.0, None, None)
    assert amp == D.UNKNOWN_LIMIT_AMP_DEG < D.MAX_AMP_DEG
    assert "몰라" in note, note


def test_all_joints_do_not_peak_at_the_same_moment():
    """⚠ 여섯이 한꺼번에 같은 방향으로 최대 속도를 내면 팔 전체가 크게 흔들린다.
    위상을 어긋나게 두는 이유다."""
    joints = [f"joint{i}" for i in range(1, 7)]
    plan = D.build_plan({j: 0.0 for j in joints},
                        {j: (-150.0, 150.0) for j in joints}, joints)
    phases = {p.phase_deg for p in plan.joints}
    assert len(phases) == 6, phases

    # 같은 순간의 속도(도함수)가 한 방향으로 몰리지 않는다
    t = 0.3
    d = [math.cos(2 * math.pi * t / D.PERIOD_S + math.radians(p.phase_deg))
         for p in plan.joints]
    assert abs(sum(d)) < len(d) * 0.7, d


def test_a_single_joint_run_has_no_phase_offset():
    """개별 측정에서 위상을 어긋나게 할 이유가 없다 — 비교 대상이 자기 자신이다."""
    plan = D.build_plan({"joint3": 0.0}, {"joint3": (-170.0, 0.0)}, ["joint3"])
    assert plan.joints[0].phase_deg == 0.0


def test_the_motion_starts_and_ends_where_it_began():
    """⚠ 검사가 끝났을 때 팔이 다른 자세에 있으면, 다음 작업이 그 자세에서
    시작한다. 사인파는 정수 주기라 제자리로 돌아온다."""
    p = D.JointPlan("joint1", center_deg=12.0, amplitude_deg=10.0)
    assert p.target_deg(0.0) == pytest.approx(12.0)
    assert p.target_deg(D.PERIOD_S * D.CYCLES) == pytest.approx(12.0, abs=1e-6)


# ── 분석 ────────────────────────────────────────────────────────────────────

def _rows(err_by_joint: dict[str, float], n: int = 20) -> list[dict]:
    out = []
    for _ in range(n):
        row = {}
        for j, e in err_by_joint.items():
            row |= {f"{j}_ctrl_minus_feedback_deg": e,
                    f"{j}_motor_current_a": 1.0, f"{j}_effort_nm": 1.0,
                    f"{j}_motor_temp_c": 30}
        out.append(row)
    return out


def test_it_finds_the_joint_that_stands_out():
    """⚠ 절대 기준이 없다. 여섯이 **같은 모션**을 했으므로 관절끼리가 서로의
    대조군이고, 중앙값 대비 배수가 절대값보다 뜻이 크다."""
    errs = {f"joint{i}": 0.1 for i in range(1, 7)}
    errs["joint2"] = 0.34                     # 3.4배
    got = D.summarize(_rows(errs), list(errs))
    assert got["outliers"].get("err_max_deg") == ["joint2"], got["outliers"]
    assert got["joints"]["joint2"]["err_max_deg"] == 0.34


def test_it_reports_measurements_not_a_verdict():
    """⚠ 판정을 내리지 않는다 — 절대 합격/불합격 기준이 우리에게 없다. 결과는
    측정값과 "다른 관절 대비 튄다" 까지고, 원인은 사람이 본다."""
    errs = {f"joint{i}": 0.1 for i in range(1, 7)}
    errs["joint2"] = 0.34
    got = D.summarize(_rows(errs), list(errs))

    assert set(got) == {"joints", "outliers"}, got.keys()
    for verdict in ("verdict", "pass", "fail", "ok", "healthy", "broken"):
        assert verdict not in got, f"판정 필드가 있다: {verdict}"
        assert verdict not in got["joints"]["joint2"], verdict
    # 튄다는 것은 **어느 항목이** 튀는지까지다 (오차는 최대·RMS 둘 다 걸린다)
    assert got["outliers"]["err_max_deg"] == ["joint2"]
    assert all(hits == ["joint2"] for hits in got["outliers"].values()), got["outliers"]


def test_two_joints_are_not_enough_to_call_an_outlier():
    """⚠ 셋도 안 되면 중앙값이 뜻이 없다 — 둘 중 하나는 언제나 '큰 쪽'이다."""
    got = D.summarize(_rows({"joint1": 0.1, "joint2": 9.9}), ["joint1", "joint2"])
    assert got["outliers"] == {}, got["outliers"]


def test_flags_seen_during_the_run_are_kept():
    """순간값이 아니라 **한 번이라도 섰는지**를 본다 — 부하가 걸린 그 순간에만
    서는 플래그가 이 검사가 찾으려는 것이다."""
    rows = _rows({"joint2": 0.1})
    rows[7]["joint2_driver_overcurrent"] = True
    got = D.summarize(rows, ["joint2"])
    assert got["joints"]["joint2"]["flags"] == ["driver_overcurrent"]


# ── 안전 ────────────────────────────────────────────────────────────────────

def test_it_refuses_while_the_arm_is_already_moving(monkeypatch):
    """⚠ 검사는 팔을 흔드는 일이라 돌고 있는 작업을 망가뜨리고, 그 작업이 만드는
    부하가 측정에도 섞인다.

    ⚠ **앱을 띄우지 않는다.** `TestClient` 로 기동하면 그 기동이 매니저 상태를
    남겨 **뒤따르는 다른 파일의 테스트가 깨졌다** (카메라 프로파일·YOLO 셋).
    가드는 라우터 함수를 직접 불러도 그대로 도므로 앱이 필요 없다.
    """
    import asyncio

    from fastapi import HTTPException

    from app.routers.robots import DiagStartRequest, diag_start
    from app.services import exclusivity

    monkeypatch.setattr(exclusivity, "running",
                        lambda: [exclusivity.Activity.INFERENCE])
    with pytest.raises(HTTPException) as err:
        asyncio.run(diag_start(DiagStartRequest(iface="can0")))
    assert err.value.status_code == 409 and "추론" in err.value.detail


def test_a_master_arm_is_not_offered(monkeypatch):
    """⚠ 마스터는 외부 명령을 무시한다 — "안 움직였다" 가 곧 고장으로 오독된다.
    화면이 아예 후보에서 뺀다."""
    from pathlib import Path

    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[2] / "frontend" / "src"
                     / "components" / "DiagnosticsPanel.tsx").read_text())
    assert "master_slave !== 'master'" in src, "마스터 팔을 고를 수 있다"


def test_stopping_does_not_cut_the_torque():
    """⚠ 멈춘다는 것은 **그 자리에 서는 것**이다. 토크를 끊으면 팔이 떨어진다."""
    import inspect
    import textwrap

    from conftest import python_code_only
    from piper_robot.diag_runner import DiagRun

    src = python_code_only(textwrap.dedent(inspect.getsource(DiagRun.stop)))
    assert "DisableArm" not in src and "disable" not in src.lower()


def test_the_command_goes_through_the_safety_filter():
    """⚠ `JointCtrl` 로 직접 보내면 바닥·범위·변화율·데드맨이 전부 빠진다 —
    검사가 그 구멍이 되면 안 된다."""
    import inspect
    import textwrap

    from conftest import python_code_only
    from piper_robot.diag_runner import DiagRun

    src = python_code_only(textwrap.dedent(inspect.getsource(DiagRun._command)))
    assert "bridge._send" in src, "안전층을 우회한다"
    assert "JointCtrl" not in src
