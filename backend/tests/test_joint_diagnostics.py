"""관절 검사 — 움직이면서 재고 관절끼리 견준다 (feature/joint-diagnostics.md).

⚠ **모션 계산이 순수 함수인 이유가 여기 있다.** 진폭·여유·위상이 맞는지 팔을
안 움직이고 확인할 수 있어야 한다 — 진단이 사고의 원인이 되면 안 된다.
"""

import math

import pytest
from piper_robot import diagnostics as D


# ── 모션 ────────────────────────────────────────────────────────────────────

def test_stronger_means_both_wider_and_faster():
    """⚠ 부하는 폭에서만 오지 않는다. 폭만 키우고 속도를 고정하면 **현장보다
    네 배 느린 부하**만 보게 된다 — 실제 수집 데이터의 상위 5% 가 60~73°/s 인데
    (`SPEED_DEG_S` 주석의 실측) 검사가 13.8°/s 로 흔들고 있었다."""
    accels = {"joint6": 5.0}
    lim = {"joint6": (-150.0, 150.0)}
    plans = {it: D.build_plan({"joint6": 0.0}, lim, ["joint6"], it, accels)
             for it in ("gentle", "normal", "strong")}
    peaks = [plans[it].joints[0].peak_speed_deg_s
             for it in ("gentle", "normal", "strong")]
    assert peaks == sorted(peaks), f"강도를 올렸는데 안 빨라진다: {peaks}"
    assert peaks[-1] > 60.0, f"가장 강한 설정이 수집 속도에 한참 못 미친다: {peaks[-1]}"

    strong, gentle = plans["strong"], plans["gentle"]
    assert strong.joints[0].amplitude_deg > gentle.joints[0].amplitude_deg * 2, \
        "강하게가 실제로 더 크게 안 흔든다"


def test_the_acceleration_limit_beats_the_wanted_speed():
    """⚠ 팔이 못 내는 가속도를 주면 못 따라온 만큼이 추종 오차로 기록된다 —
    검사가 **자기가 만든 미달**을 관절 이상으로 읽는다. 속도표는 목표지 보장이
    아니고, 가속도 한계가 이겨야 한다."""
    amp = 30.0
    # 넉넉한 팔: 원하는 속도가 그대로 나온다
    loose = D.period_for(amp, 70.0, max_acc_rad_s2=5.0)
    # 굼뜬 팔: 같은 요구인데 가속도가 주기를 늘린다
    tight = D.period_for(amp, 70.0, max_acc_rad_s2=0.3)
    assert tight > loose, f"가속도 한계가 무시됐다: {loose} → {tight}"

    peak = math.radians(amp) * (2 * math.pi / tight) ** 2
    assert peak <= 0.3 + 1e-6, f"가속도 한계를 넘겼다: {peak:.3f} > 0.3"


def test_all_joints_share_one_period():
    """⚠ 관절마다 주기가 다르면 위상을 어긋나게 둔 뜻이 사라진다 — 어느 순간
    여럿이 겹쳐 같은 방향으로 최대 속도를 낸다."""
    joints = [f"joint{i}" for i in range(1, 7)]
    p = D.build_plan({j: 0.0 for j in joints},
                     {"joint1": (-150.0, 150.0), "joint5": (-70.0, 70.0)},
                     joints, "normal", {j: 17.2 for j in joints})
    assert len({j.period_s for j in p.joints}) == 1, "주기가 제각각이다"


def test_the_swing_never_reaches_the_configured_limit():
    """⚠ 한계를 때리면 그 때림이 `angle_limit` 플래그로 기록되고, **검사가 자기
    결론을 만들어낸다.** 지금 자세에서 한계까지 남은 거리도 진폭을 깎아야 한다."""
    # 한계에 붙어 있으면 **한쪽으로** 흔든다 (0 으로 깎지 않는다)
    amp, direction, note = D.plan_amplitude(center_deg=68.0, lo_deg=-70.0, hi_deg=70.0)
    assert amp > 0 and direction == -1, (amp, direction, note)

    amp, direction, _ = D.plan_amplitude(center_deg=0.0, lo_deg=-70.0, hi_deg=70.0)
    assert 0 < amp <= D.AMPLITUDES_DEG["strong"] and direction == 0
    assert amp <= 70.0 - D.LIMIT_MARGIN_DEG


def test_a_narrow_joint_swings_less():
    """가동범위가 좁은 관절에 같은 진폭을 주면 그 관절만 한계에 가까워진다."""
    wide, _, _ = D.plan_amplitude(0.0, -150.0, 150.0)
    narrow, _, note = D.plan_amplitude(0.0, -30.0, 30.0)
    assert narrow < wide, (narrow, wide)
    assert note == "가동범위 기준"


def test_an_unknown_limit_swings_less_not_more():
    """⚠ 한계를 모르면 여유도 모른다. 안전층이 범위를 자르니 위험하진 않지만,
    한계에 닿으면 `angle_limit` 플래그가 서고 그게 측정에 섞인다 — 검사가 자기
    결론을 만들어내는 셈이다. 모르면 **작게** 흔든다."""
    amp, _, note = D.plan_amplitude(0.0, None, None, cap_deg=30.0)
    assert amp == D.UNKNOWN_LIMIT_AMP_DEG < 30.0
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
    d = [math.cos(2 * math.pi * t / p.period_s + math.radians(p.phase_deg))
         for p in plan.joints]
    assert abs(sum(d)) < len(d) * 0.7, d


def test_a_single_joint_run_has_no_phase_offset():
    """개별 측정에서 위상을 어긋나게 할 이유가 없다 — 비교 대상이 자기 자신이다."""
    plan = D.build_plan({"joint3": 0.0}, {"joint3": (-170.0, 0.0)}, ["joint3"])
    assert plan.joints[0].phase_deg == 0.0


def test_the_motion_starts_and_ends_where_it_began():
    """⚠ 검사가 끝났을 때 팔이 다른 자세에 있으면, 다음 작업이 그 자세에서
    시작한다. 사인파는 정수 주기라 제자리로 돌아온다."""
    p = D.JointPlan("joint1", center_deg=12.0, amplitude_deg=10.0, period_s=4.0)
    assert p.target_deg(0.0) == pytest.approx(12.0)
    assert p.target_deg(4.0 * D.CYCLES) == pytest.approx(12.0, abs=1e-6)


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


def test_a_crash_in_cleanup_still_ends_the_run():
    """⚠ **끝났다는 표시가 뒷정리보다 먼저다.** 정리에서 예외가 나면 `done` 이
    영영 안 서고 화면은 "진행 중" 으로 굳는다 — 실기에서 그랬다: `_hold` 를 안
    만들어 둔 채 불러 `AttributeError`, 모션은 8초에 끝났는데 77초째 진행 중.
    정리는 못 해도 **끝난 것은 끝난 것이다.**"""
    import threading

    from piper_robot.diag_runner import DiagRun

    class _Boom:
        def read_joints_normalized(self):
            raise RuntimeError("팔이 안 읽힌다")
        iface = "can0"

    run = DiagRun.__new__(DiagRun)
    run.arm, run.bridge = _Boom(), None
    run.plan = D.build_plan({}, {}, [])
    run.rows, run.error, run.done = [], None, False
    run.started_at = 0.0
    run._stop = threading.Event()
    run._run()
    assert run.done is True, "정리가 터지면 영영 안 끝난다"


def test_stopping_holds_the_pose_instead_of_dropping_it():
    """⚠ 마지막 목표가 먼 곳이었으면 팔은 계속 그리로 간다 — **현재 자세를 실제로
    명령해야** 선다. 토크를 끊는 것은 답이 아니다(팔이 떨어진다)."""
    import inspect
    import textwrap

    from conftest import python_code_only
    from piper_robot.diag_runner import DiagRun

    src = python_code_only(textwrap.dedent(inspect.getsource(DiagRun._hold)))
    assert "read_joints_normalized" in src and "_send" in src, "그 자리에 안 선다"
    assert "Disable" not in src, "토크를 끊는다 — 팔이 떨어진다"


def test_it_enables_the_motors_before_measuring():
    """⚠ **모터가 꺼져 있으면 명령이 아무 일도 안 한다.** 그런데 팔은 전압·온도를
    멀쩡히 보고하므로 로그만 보면 정상이다 — 실기에서 397행이 전부 0 으로
    남았고, 그게 "고장" 으로 읽힐 뻔했다."""
    import inspect
    import textwrap

    from conftest import python_code_only
    from piper_robot.diag_runner import DiagRun

    src = python_code_only(textwrap.dedent(inspect.getsource(DiagRun._run)))
    assert "enable_for_motion" in src, "끈 채로 재려 한다"
    assert "모터를 켜지 못했습니다" in inspect.getsource(DiagRun._run), \
        "못 켜도 그냥 재서 0 을 남긴다"


def test_the_tracking_error_is_measured_against_what_we_commanded():
    """⚠ 팔의 지령 레지스터(0x15x)는 우리 명령을 **되비추지 않는다** — 실측:
    관절이 ±10° 로 흔들리는 내내 `ctrl_deg` 가 0 이어서 추종 오차가 통째로
    진폭과 같게 나왔다(10.068°). 우리는 무엇을 시켰는지 정확히 아는데 그걸
    안 쓰고 팔에게 되물은 셈이었다. 고친 뒤 같은 관절이 1.16° 다."""
    from piper_robot.diag_runner import _joint_row

    row = _joint_row("joint6", 6, None, None, None, None, None, target_deg=8.65)
    assert row["joint6_ctrl_deg"] == 8.65, "시킨 값을 안 쓴다"


def test_a_joint_we_did_not_command_falls_back_to_the_register():
    """안 시킨 관절은 참고값이라도 있어야 한다 — 같이 흔들렸는지 보려면."""
    from piper_robot.diag_runner import _joint_row

    ct = type("C", (), {"joint_1": 1234})()
    row = _joint_row("joint1", 1, None, ct, None, None, None, target_deg=None)
    assert row["joint1_ctrl_deg"] == 1.234


def test_a_joint_parked_at_its_limit_still_gets_measured():
    """⚠ **한계에 붙어 있으면 대칭으로 못 흔든다.** 예전에는 진폭을 0 으로 깎아
    그 관절이 **아예 안 움직였다** — 실기에서 can3 의 joint3 가 한계 `−170~0°` 의
    0° 에 있어 "거의 안 움직인다" 로 보고됐다. 반대쪽에 165° 가 남아 있는데도.

    한쪽 스윙은 **지금 자세에서 출발해 지금 자세로 돌아온다** — 튀지 않으면서
    남은 공간을 쓴다. (고친 뒤 같은 관절이 −20.1°~−0.1°, 토크 2.07 N·m.)"""
    p = D.build_plan({"joint3": -0.1}, {"joint3": (-170.0, 0.0)}, ["joint3"],
                     "normal", {"joint3": 17.2})
    j = p.joints[0]
    assert j.amplitude_deg > 0, "한계에 붙었다고 안 움직인다"
    assert j.direction == -1, "여유가 있는 쪽으로 안 간다"
    # 출발과 도착이 지금 자세여야 한다
    assert j.target_deg(0.0) == pytest.approx(-0.1, abs=1e-6)
    assert j.target_deg(j.period_s) == pytest.approx(-0.1, abs=1e-6)
    # 한계를 넘지 않는다
    lows = [j.target_deg(t / 100 * j.period_s) for t in range(101)]
    assert min(lows) >= -170.0 + D.LIMIT_MARGIN_DEG, min(lows)


def test_a_one_sided_swing_is_not_faster_for_the_same_amplitude():
    """한쪽 스윙은 `A·π/T` 라 대칭(`A·2π/T`)의 절반이다 — 주기를 그만큼 줄여도
    팔의 속도 한계를 안 넘는다."""
    sym = D.JointPlan("j", 0.0, 20.0, period_s=9.1, direction=0)
    one = D.JointPlan("j", 0.0, 20.0, period_s=9.1, direction=1)
    assert one.peak_speed_deg_s == pytest.approx(sym.peak_speed_deg_s / 2, rel=0.01)


def test_the_result_charts_overlay_the_joints():
    """⚠ 이 검사의 전제는 "여섯이 같은 모션을 했으니 서로가 서로의 대조군" 이다.
    그러면 그래프도 겹쳐야 한다 — 나란히 놓으면 눈이 축을 오가야 하고, 그게 바로
    표만 봐서는 안 보이던 차이다."""
    from pathlib import Path

    from conftest import code_only

    src = code_only((Path(__file__).resolve().parents[2] / "frontend" / "src"
                     / "components" / "DiagCharts.tsx").read_text())
    assert "joints.map" in src, "관절을 겹쳐 그리지 않는다"
    assert "MAX_POINTS" in src, "1361행을 그대로 그린다"

    panel = code_only((Path(__file__).resolve().parents[2] / "frontend" / "src"
                       / "components" / "DiagnosticsPanel.tsx").read_text())
    for field in ("ctrl_minus_feedback_deg", "motor_current_a", "effort_nm"):
        assert field in panel, f"{field} 그래프가 없다"


def test_a_joint_blocked_below_is_lifted_before_swinging():
    """⚠ **아래가 막힌 관절이 있다.** can3 의 joint5 는 아래로 가면 기구물에
    걸려 안 움직인다 — 그 자리에서 그대로 흔들면 아래 절반이 막힌 채로 재고,
    검사는 그 미달을 "추종 오차" 로 적는다. 관절이 아니라 기구물을 재는 셈이다.
    """
    lim = {"joint5": (-70.0, 70.0)}
    p = D.build_plan({"joint5": 0.0}, lim, ["joint5"], "strong",
                     {"joint5": 5.0}, up={"joint5": 1})
    j = p.joints[0]
    assert j.center_deg >= 25.0, f"중심이 안 띄워졌다: {j.center_deg}"
    lowest = min(j.target_deg(t / 50.0) for t in range(int(50 * j.period_s) + 1))
    assert lowest >= 0.0, f"궤적이 아래로 내려간다: {lowest:.1f}°"


def test_the_lift_never_guesses_which_way_is_up():
    """⚠ 위가 어느 부호인지는 **기구학이 정한다.** 찍으면 띄우려던 것이 아래로
    밀어 넣는 일이 된다 — 관절 부호 규약은 팔마다, 자세마다 다르다."""
    lim = {"joint5": (-70.0, 70.0)}
    plain = D.build_plan({"joint5": 0.0}, lim, ["joint5"], "strong", {"joint5": 5.0})
    assert plain.joints[0].center_deg == 0.0, "방향을 모르는데 중심을 옮겼다"

    down = D.build_plan({"joint5": 0.0}, lim, ["joint5"], "strong",
                        {"joint5": 5.0}, up={"joint5": -1})
    assert down.joints[0].center_deg < 0, "위 방향이 음수인 팔에서 반대로 띄웠다"


def test_the_lift_stays_inside_the_limits():
    """한계에 닿으면 그 자체가 이상 신호로 기록된다 — 띄우다 한계를 치면 안 된다."""
    lim = {"joint5": (-70.0, 70.0)}
    p = D.build_plan({"joint5": 60.0}, lim, ["joint5"], "strong",
                     {"joint5": 5.0}, up={"joint5": 1})
    j = p.joints[0]
    assert j.center_deg <= 70.0 - D.LIMIT_MARGIN_DEG + 1e-6, f"한계를 넘겼다: {j.center_deg}"
    top = max(j.target_deg(t / 50.0) for t in range(int(50 * j.period_s) + 1))
    assert top <= 70.0 - D.LIMIT_MARGIN_DEG + 1e-6, f"궤적이 한계를 넘는다: {top:.1f}°"


def test_the_run_walks_to_the_start_pose_first():
    """⚠ 중심을 띄우면 t=0 목표가 지금 자세에서 떨어져 있다. 바로 흔들면 팔이
    그리로 튀고, 그 과도응답이 첫 주기 측정에 섞인다."""
    from pathlib import Path

    from conftest import python_code_only

    src = (Path(__file__).resolve().parents[2]
           / "robot/piper_robot/diag_runner.py").read_text()
    body = python_code_only(src)
    assert "_approach" in body, "출발 자세로 데려가는 구간이 없다"
    assert body.index("self._approach()") < body.index("while not self._stop.is_set()"), \
        "접근이 측정 루프보다 뒤에 있다"


def _run_with_rows(rows):
    """`DiagRun` 을 팔 없이 세워서 중단 판정만 돌린다."""
    from piper_robot.diag_runner import DiagRun

    run = DiagRun.__new__(DiagRun)
    run.plan = D.Plan(joints=[D.JointPlan(joint="joint5", center_deg=0.0,
                                          amplitude_deg=10.0)])
    run._abort_run = 0
    for row in rows:
        run._check_abort(row)
    return run


def test_a_stalled_joint_stops_the_run():
    """⚠ **팔은 멈췄다고 말하고 있었는데 검사가 계속 밀었다.** 실기에서 걸린
    joint5 를 6 초간 밀어 모터 온도가 65 → 87°C 까지 올랐고 드라이버가 스스로
    차단했다. 플래그를 열에 적기만 하고 반응을 안 한 것이 구멍이었다 — 재려는
    것이 관절 상태인데 그 과정에서 관절을 상하게 하면 검사가 아니라 사고다."""
    stalled = {"joint5_stall": True, "joint5_ctrl_minus_feedback_deg": 2.0}
    with pytest.raises(RuntimeError, match="걸려서"):
        _run_with_rows([stalled] * D.ABORT_FRAMES)


def test_one_noisy_frame_does_not_stop_the_run():
    """한 프레임 튀었다고 멈추면 멀쩡한 검사가 자꾸 끊긴다."""
    bad = {"joint5_stall": True}
    good = {"joint5_stall": False, "joint5_ctrl_minus_feedback_deg": 0.5}
    _run_with_rows([bad, good] * 10)          # 예외가 안 나면 통과


def test_a_joint_that_never_follows_stops_the_run():
    """⚠ 플래그를 못 읽는 팔도 있다. **안 따라오는 것 자체**가 보루다 —
    실기에서 명령 65° 인데 피드백이 22° 에 머물렀다(오차 44°)."""
    stuck = {"joint5_ctrl_minus_feedback_deg": 44.7}
    with pytest.raises(RuntimeError, match="추종 오차"):
        _run_with_rows([stuck] * D.ABORT_FRAMES)


def test_the_approach_is_watched_too():
    """⚠ **걸림은 접근 구간에서 먼저 난다** — 띄우러 가다 막히는 것이 흔들다
    막히는 것보다 앞선다. 측정 안 하는 구간이라고 안 보면 안 된다."""
    import inspect
    import textwrap

    from conftest import python_code_only
    from piper_robot.diag_runner import DiagRun

    body = python_code_only(textwrap.dedent(inspect.getsource(DiagRun._approach)))
    assert "_check_abort" in body, "접근 구간이 감시를 안 받는다"


def test_the_stop_message_says_how_to_recover():
    """⚠ **멈추는 것으로 끝이 아니다.** 기구 한계에 한 번 부딪히면 과전류·위치
    오차가 래치되어 그 뒤로는 어느 방향으로도 안 움직인다 — 기구물을 치워도
    안 돌아온다. `0x150` 리셋이 에러를 지워야 다시 움직이는데, 그 버튼은 영점
    굽기 창 안에 있어 검사가 끊긴 사람이 스스로 찾지 못한다.
    """
    with pytest.raises(RuntimeError) as e:
        _run_with_rows([{"joint5_stall": True}] * D.ABORT_FRAMES)
    assert "리셋" in str(e.value), "복구하는 길을 안 알려준다"
    assert "0x150" in str(e.value)
