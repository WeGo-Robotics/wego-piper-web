"""관절 부하 감시 (층 1) — 슬립이 나는 **조건**을 잡는다.

슬립 중에는 추종 오차도 모터 속도도 fault 도 정상이고 토크만 다르다
(`piper_robot.load` 머리말). 그래서 사건은 못 잡고 조건만 잡는다 — 그 대신
조건 판정이 정확해야 한다: 가감속의 순간 피크마다 울리면 아무도 안 읽고,
임계 언저리에서 깜빡여도 마찬가지다.

임계값 자체는 아직 잠정이라 **임계와 무관하게 최대치를 늘 기록하는 것**이
이 층의 진짜 산출물이다. 진짜 임계는 그 기록과 층 2 의 `slip_raw` 를 맞대어
정한다 — 그래서 리셋이 두 층을 짝지어 내보내는지도 여기서 검사한다.
"""

import inspect
from pathlib import Path

import pytest

pytest.importorskip("piper_robot")

from piper_robot.load import LoadLimits, LoadWatch, describe  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]

# 검사용 임계 — 실제 잠정값(6.0)과 무관하게 고정한다. 기본값을 고치는 날
# 테스트가 같이 흔들리면 그 변경이 안전한지 알 수 없다.
LIMITS = LoadLimits(warn_nm=5.0, dwell_s=0.30, release=0.75)


def _feed(w: LoadWatch, joint: str, nm: float, t: float) -> list:
    return w.feed(t, {joint: nm}, {joint: nm / 1.18125})


def test_a_short_spike_is_not_an_alarm():
    """가감속의 순간 피크는 정상이다. dwell 을 못 채우면 울리면 안 된다."""
    w = LoadWatch(LIMITS)
    events = []
    for i in range(5):                       # 0.20초 — dwell(0.30) 미만
        events += _feed(w, "joint2", 9.0, i * 0.05)
    assert events == [], "0.2초짜리 피크에 울리면 매 동작마다 울린다"
    assert w.joints["joint2"].events == 0


def test_a_sustained_overload_raises_once_and_stays_quiet():
    """지속되면 울린다. 단 **한 번만** — 20Hz 로 같은 말을 쏟으면 로그가 묻힌다."""
    w = LoadWatch(LIMITS)
    events = []
    for i in range(40):                      # 2초 지속
        events += _feed(w, "joint2", 9.0, i * 0.05)
    assert len(events) == 1, f"경보가 {len(events)}번 떴다 — 새로 설 때만 알려야 한다"
    ev = events[0]
    assert ev.joint == "joint2" and ev.effort_nm == 9.0
    assert ev.held_s >= LIMITS.dwell_s
    assert w.joints["joint2"].over_s == pytest.approx(1.95, abs=0.01)


def test_hovering_at_the_threshold_does_not_flicker():
    """임계 바로 아래(해제선 위)로 내려온 것은 해제가 아니다.

    ⚠ 해제를 임계로 잡으면 4.9/5.1 을 오가는 부하에서 경보가 초당 몇 번씩
      섰다 풀렸다 한다 — 그 소음이 진짜 한 번을 덮는다.
    """
    w = LoadWatch(LIMITS)
    for i in range(20):
        _feed(w, "joint2", 9.0, i * 0.05)    # 경보 세우기
    assert w.joints["joint2"].raised
    # 4.5 = 임계(5.0) 아래지만 해제선(3.75) 위
    again = []
    for i in range(20, 40):
        again += _feed(w, "joint2", 4.5, i * 0.05)
    assert w.joints["joint2"].raised, "해제선 위인데 풀렸다"
    assert again == [], "풀리지도 않았는데 다시 울렸다"
    assert w.joints["joint2"].events == 1


def test_dropping_clears_and_a_second_overload_raises_again():
    """해제선 아래로 내려오면 풀리고, 다음 과부하는 **새 사건**이다."""
    w = LoadWatch(LIMITS)
    for i in range(20):
        _feed(w, "joint2", 9.0, i * 0.05)
    for i in range(20, 30):
        _feed(w, "joint2", 1.0, i * 0.05)    # 해제선(3.75) 아래
    assert not w.joints["joint2"].raised
    second = []
    for i in range(30, 50):
        second += _feed(w, "joint2", 9.0, i * 0.05)
    assert len(second) == 1
    assert w.joints["joint2"].events == 2


def test_the_peak_is_recorded_even_when_nothing_ever_alarms():
    """**이 층의 진짜 산출물이다.** 임계가 잠정인 동안, 임계를 한 번도 안 넘긴
    부하 기록이 곧 임계를 정할 근거다. 여기가 비면 숫자를 고칠 방법이 없다."""
    w = LoadWatch(LIMITS)
    for i, nm in enumerate([1.0, 3.2, 4.9, 2.0, 0.5]):
        _feed(w, "joint5", nm, i * 0.05)
    st = w.joints["joint5"]
    assert st.events == 0 and not st.raised
    assert st.peak_nm == 4.9, "경보가 없다고 기록도 없으면 임계를 못 고친다"
    assert st.peak_at == pytest.approx(0.10)
    assert w.snapshot()["limits"]["provisional"] is True, \
        "잠정값이라는 사실이 스냅샷에 없으면 화면이 근거 있는 숫자처럼 보여준다"


def test_history_keeps_the_maximum_not_the_average():
    """⚠ 1초 요약을 평균으로 내면 짧은 과부하가 주변 표본에 희석돼 사라진다."""
    w = LoadWatch(LIMITS)
    for i in range(21):                      # 0 ~ 1.0초
        nm = 8.0 if i == 7 else 0.5          # 한 표본만 튄다
        _feed(w, "joint3", nm, i * 0.05)
    rows = w.history()["rows"]
    assert rows, "1초가 지났는데 기록이 없다"
    idx = w.history()["joints"].index("joint3")
    assert rows[0]["nm"][idx] == 8.0, "평균을 냈다 — 피크가 사라진다"


def test_a_sampling_gap_does_not_become_a_sustained_overload():
    """데몬이 멈췄다 깬 사이는 **모르는 시간**이다.

    ⚠ 그 간극을 dwell 로 세면 깨어나는 첫 표본에서 곧장 경보가 뜨고, 누적
      초에는 멈춰 있던 시간이 통째로 들어간다 — 둘 다 거짓이다.
    """
    w = LoadWatch(LIMITS)
    _feed(w, "joint2", 9.0, 0.0)
    events = _feed(w, "joint2", 9.0, 120.0)  # 2분 멈췄다 깼다
    assert events == [], "끊긴 시간을 지속으로 셌다"
    assert w.joints["joint2"].over_s == 0.0, "멈춰 있던 2분이 누적에 들어갔다"
    # 깬 뒤로 다시 지속되면 그때는 정상적으로 울린다
    later = []
    for i in range(1, 20):
        later += _feed(w, "joint2", 9.0, 120.0 + i * 0.05)
    assert len(later) == 1


def test_a_gap_closes_the_history_bucket_where_it_actually_ended():
    """⚠ 끊기기 직전의 피크에 **깬 시각**을 붙이면, 30분 그래프에서 봉우리가
    있지도 않았던 자리로 몇 분 옮겨 그려진다. 그건 빈 구간보다 나쁘다."""
    w = LoadWatch(LIMITS)
    _feed(w, "joint3", 8.0, 10.0)
    _feed(w, "joint3", 0.5, 10.5)
    _feed(w, "joint3", 0.5, 300.0)           # 5분 멈췄다 깼다
    rows = w.history()["rows"]
    idx = w.history()["joints"].index("joint3")
    assert rows, "끊길 때 모아 둔 구간을 안 닫았다"
    assert rows[0]["t"] == 10.5, f"피크가 t={rows[0]['t']} 로 옮겨졌다"
    assert rows[0]["nm"][idx] == 8.0


def test_per_joint_thresholds_win_over_the_shared_one():
    """관절마다 모터도 감속비도 달라서 여섯이 같은 값일 이유가 없다."""
    w = LoadWatch(LoadLimits(warn_nm=5.0, dwell_s=0.1, per_joint={"joint5": 2.0}))
    events = []
    for i in range(10):
        events += w.feed(i * 0.05, {"joint2": 3.0, "joint5": 3.0})
    assert [e.joint for e in events] == ["joint5"]
    assert w.snapshot()["limits"]["warn_nm"]["joint5"] == 2.0
    assert w.snapshot()["limits"]["warn_nm"]["joint2"] == 5.0


def test_reset_opens_a_new_window():
    """누적 창은 층 2(0x150 리셋)가 닫고 연다 — 그래야 두 값이 같은 구간이다."""
    w = LoadWatch(LIMITS)
    for i in range(20):
        _feed(w, "joint2", 9.0, i * 0.05)
    assert w.joints["joint2"].peak_nm == 9.0
    w.reset()
    assert w.joints["joint2"].peak_nm == 0.0
    assert w.joints["joint2"].events == 0 and not w.joints["joint2"].raised
    assert w.history()["rows"] == []


def test_the_message_says_what_to_do_next():
    """경보만으로는 슬립을 확정 못 한다 — 문구가 층 2 를 가리켜야 한다."""
    w = LoadWatch(LIMITS)
    events = []
    for i in range(20):
        events += _feed(w, "joint5", 7.5, i * 0.05)
    msg = describe("can3", events[0])
    assert "can3" in msg and "joint5" in msg and "7.5" in msg
    assert "0x150" in msg, "실제 슬립을 확인할 길을 안 알려준다"


# ── 배선 ──

def test_load_is_read_from_the_broadcast_cache_not_a_round_trip():
    """⚠ `Search*` 계열은 팔에 **물어보는** 호출이라 왕복이 있다. 발행 루프
    옆에서 20Hz 로 부르면 핫패스가 그만큼 밀린다. 0x251~6 은 팔이 스스로
    뿌리는 것이라 캐시 읽기로 끝난다."""
    import textwrap

    from conftest import python_code_only
    from piper_robot.arm import Arm

    # ⚠ docstring 을 빼고 본다 — "`Search*` 는 안 쓴다" 는 설명이 그 금지
    #   검사에 걸린다 (conftest 가 이 덫을 위해 만든 도구다).
    src = python_code_only(textwrap.dedent(inspect.getsource(Arm.read_load)))
    assert "GetArmHighSpdInfoMsgs" in src
    assert "Search" not in src, "물어보는 호출이 섞였다 — 핫패스에 왕복이 생긴다"
    assert "with self._lock" in src, "팔 핸들을 락 없이 만졌다"


def test_the_watch_runs_whether_or_not_a_consumer_is_attached():
    """슬립은 조그·파킹·텔레옵 중에도 나고 그때가 최다 트리거다 — 표본은
    명령 루프가 아니라 **발행 루프**에 있어야 한다."""
    src = (_ROOT / "robot" / "piper_robot" / "publish.py").read_text()
    body = src.split("def _publish_loop", 1)[1].split("\n    def ", 1)[0]
    assert "_sample_load()" in body, "부하 표본이 발행 루프에 없다"
    cmd = src.split("def _command_loop", 1)[1].split("\n    def ", 1)[0]
    assert "_sample_load" not in cmd, "소비자가 붙어야만 감시가 도는 구조다"


def test_the_reset_carries_the_load_window_it_closes():
    """층 1 과 층 2 는 짝으로만 뜻이 있다 — 리셋이 둘을 한 보고에 담아야
    "경보는 떴는데 안 밀렸다"(임계가 낮다)와 "안 떴는데 밀렸다"(놓쳤다)를
    가릴 수 있다. 버리기만 하면 창이 닫힌 순간의 값이 사라진다."""
    src = (_ROOT / "daemons" / "robotd.py").read_text()
    body = src.split("def clear_errors", 1)[1].split("\n    def ", 1)[0]
    assert "load_reset" in body, "리셋이 부하 창을 닫지 않는다"
    assert 'row["load"]' in body, "닫힌 창의 기록을 보고에 안 싣는다"

    pub = (_ROOT / "robot" / "piper_robot" / "publish.py").read_text()
    reset = pub.split("def load_reset", 1)[1].split("\n    def ", 1)[0]
    assert reset.index("snapshot()") < reset.index("load.reset()"), \
        "기록을 뜨기 전에 버렸다"


def test_the_rpc_is_reachable():
    """데몬이 노출 목록에 없으면 게이트웨이가 못 부른다 — 조용히 안 된다."""
    src = (_ROOT / "daemons" / "robotd.py").read_text()
    methods = src.split("_METHODS = {", 1)[1].split("}", 1)[0]
    assert '"load_status"' in methods and '"load_history"' in methods


# ── 임계 설정 (로봇 › 상세 › 부하) ──

def test_disabling_silences_the_alarm_but_keeps_measuring():
    """⚠ 끄는 것은 "울리지 마라" 지 "재지 마라" 가 아니다.

    임계가 잠정인 동안 기록은 임계를 고칠 유일한 근거다. 같이 끄면, 경보가
    시끄러워서 끈 사람이 정확히 그 시끄러웠던 구간의 숫자를 영영 못 본다.
    """
    w = LoadWatch(LoadLimits(enabled=False, warn_nm=5.0, dwell_s=0.3))
    events = []
    for i in range(40):
        events += _feed(w, "joint2", 9.0, i * 0.05)
    st = w.joints["joint2"]
    assert events == [] and st.events == 0 and not st.raised
    assert st.peak_nm == 9.0, "꺼진 동안 기록이 끊겼다 — 임계를 고칠 근거가 사라진다"
    assert st.over_s > 0, "임계 위에서 보낸 시간도 계측이다"


def test_retune_clears_a_stale_alarm_but_keeps_the_measurements():
    """⚠ 임계를 5→8 로 올리면 해제선도 6.0 으로 함께 올라간다. 부하 6.0 은 새
    임계 아래인데 해제선 아래는 아니라, 옛 판정이 안 풀린 채 굳는다."""
    w = LoadWatch(LoadLimits(warn_nm=5.0, dwell_s=0.3))
    for i in range(20):
        _feed(w, "joint2", 6.0, i * 0.05)
    assert w.joints["joint2"].raised

    w.retune(LoadLimits(warn_nm=8.0, dwell_s=0.3))
    assert not w.joints["joint2"].raised, "임계를 올렸는데 경보가 안 풀렸다"
    assert w.joints["joint2"].peak_nm == 6.0, "측정값까지 지웠다"
    assert w.joints["joint2"].events == 1, "지난 사건 수는 사실이다"


def test_limits_are_clamped_and_blank_joints_fall_back_to_the_shared_value():
    """빈 칸은 "공통값을 쓴다" 는 뜻이다 — 0 으로 저장하면 그 관절이 영구히 운다."""
    from piper_robot import load_store

    cfg = load_store._apply(LoadLimits(), {
        "warn_nm": 999, "dwell_s": 0.0,
        "per_joint": {"joint5": 4.0, "joint2": None, "joint3": "", "nope": 3.0},
    })
    assert cfg.warn_nm == load_store.WARN_MAX_NM, \
        "드라이버가 끊는 지점 위로 올라가면 경보가 뜨기 전에 팔이 먼저 선다"
    assert cfg.dwell_s == load_store.DWELL_MIN_S
    assert cfg.per_joint == {"joint5": 4.0}, "빈 값·모르는 관절이 저장됐다"


def test_the_ceiling_is_a_guardrail_not_a_physics_claim():
    """⚠ **이 테스트는 한 번 틀린 것을 지킨 적이 있다.** 원래는
    `WARN_MAX_NM < 9.9` 였다 — "드라이버가 10.3A(≈9.9N·m)에서 끊는다" 는 J5
    한 건을 여섯 관절의 천장으로 읽은 것이었고, J2 가 13.5N·m 까지 가면서
    깨졌다. 잘못된 숫자를 테스트가 지키고 있었던 셈이다.

    지금 지키는 것은 숫자가 아니라 **두 방향의 함정**이다:

    - 너무 낮으면: 관측한 값을 임계로 못 고른다 (J2 에 쓸 임계가 없다)
    - 너무 높으면: 팔이 도달할 수 없는 값이라 감시가 장식이 된다
    """
    from piper_robot import load_store

    assert load_store.clamp_warn(13.5) == 13.5, "관측한 값을 못 고른다"
    # 상한은 전류 기준으로 잡고 가장 큰 계수로 환산한 값이다 — 그 관계가
    # 깨지면 누군가 숫자만 손으로 고친 것이다.
    from piper_robot.load import EFFORT_COEFF

    assert load_store.WARN_MAX_NM == round(
        load_store.WARN_MAX_A * max(EFFORT_COEFF.values()), 1)
    assert load_store.WARN_MAX_A < 25, "팔이 못 내는 값까지 열면 감시가 장식이 된다"


def test_limits_survive_a_round_trip_to_disk(tmp_path, monkeypatch):
    """저장이 안 되면 데몬을 올릴 때마다 다시 맞춰야 한다."""
    from piper_robot import load_store

    monkeypatch.setattr(load_store, "PATH", tmp_path / "load.json")
    load_store.save({"can3": LoadLimits(warn_nm=4.5, per_joint={"joint5": 3.0}),
                     "can1": LoadLimits(enabled=False)})
    back = load_store.load()
    assert back["can3"].warn_nm == 4.5 and back["can3"].per_joint == {"joint5": 3.0}
    assert back["can1"].enabled is False
    assert "can2" not in back, "저장 안 한 팔까지 생겨나면 안 된다"


def test_a_missing_file_is_not_an_error(tmp_path, monkeypatch):
    """처음 켠 설치에는 파일이 없다 — 그게 정상이고 기본값으로 가야 한다."""
    from piper_robot import load_store

    monkeypatch.setattr(load_store, "PATH", tmp_path / "nope.json")
    assert load_store.load() == {}


def test_thresholds_are_per_arm(tmp_path, monkeypatch):
    """⚠ 하나로 묶으면, 슬립이 잦은 한 관절을 잡으려고 조인 임계가 멀쩡한
    나머지 팔을 종일 울린다 (실기에서 can3 joint5 만 반복해서 났다)."""
    from piper_robot import load_store, publish

    monkeypatch.setattr(load_store, "PATH", tmp_path / "load.json")
    m = publish.ArmBridgeManager()
    m._load = {}
    m.set_load_limits("can3", {"warn_nm": 3.0})
    assert m.load_limits("can3").warn_nm == 3.0
    assert m.load_limits("can1").warn_nm == LoadLimits().warn_nm, \
        "한 팔의 임계가 다른 팔까지 바꿨다"


def test_changing_limits_reaches_the_running_bridge(tmp_path, monkeypatch):
    """저장만 하고 적용을 안 하면 다음 연결까지 안 바뀌는데, 사용자는 화면에서
    바꿨으니 바뀐 줄 안다 — 안전 설정에서 그 어긋남이 제일 위험하다."""
    from piper_robot import load_store, publish

    monkeypatch.setattr(load_store, "PATH", tmp_path / "load.json")
    m = publish.ArmBridgeManager()
    m._load = {}

    class _FakeBridge:
        def __init__(self):
            self.load = LoadWatch()

    m.bridges["can3"] = _FakeBridge()
    m.set_load_limits("can3", {"warn_nm": 2.5})
    assert m.bridges["can3"].load.limits.warn_nm == 2.5, "살아 있는 브리지에 안 닿았다"


def test_reconnecting_an_arm_does_not_quietly_restore_the_default():
    """브리지 객체는 재연결에 재사용되지만 저장된 임계가 자기 발로 따라오지는
    않는다 — 안 걸어주면 팔을 뽑았다 꽂는 순간 잠정 기본값으로 돌아간다."""
    src = (_ROOT / "robot" / "piper_robot" / "publish.py").read_text()
    body = src.split("    def start(self, arm)", 1)[1].split("\n    def ", 1)[0]
    assert "retune" in body, "연결할 때 저장된 임계를 다시 안 건다"
    assert body.index("retune") < body.index("b.start()"), \
        "발행이 시작된 뒤에 임계를 걸면 그 사이 표본이 옛 임계로 판정된다"


def test_the_route_answers_limits_and_live_load_together():
    """⚠ 따로 부르게 두면 화면이 둘을 **다른 시각의 값**으로 나란히 놓는다.
    임계가 잠정인 동안 사람은 그 둘을 비교해서 숫자를 고른다."""
    src = (_ROOT / "backend" / "app" / "routers" / "robots.py").read_text()
    body = src.split('@router.get("/load")', 1)[1].split("@router.", 1)[0]
    assert "get_load_limits" in body and "load_status" in body
    assert "503" in body, "데몬이 없을 때 기본값을 지어내면 안 된다"


def test_the_panel_says_it_does_not_limit_current():
    """⚠ **여기가 이 기능의 제일 위험한 오해다.** '전류 리미트'로 읽은 사람은
    설정해 뒀으니 팔이 보호된다고 믿는데, 펌웨어에 상한 명령 자체가 없어서
    실제로 막는 것은 아무것도 없다. 화면이 그 말을 해야 한다."""
    src = (_ROOT / "frontend" / "src" / "components" / "LoadGuardPanel.tsx").read_text()
    assert "전류를 제한하지 않습니다" in src
    assert "0x150" in src, "실제 슬립을 확인할 길을 안 알려준다"
    assert "잠정" in src, "잠정값을 근거 있는 숫자처럼 보여주면 안 된다"

    page = (_ROOT / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    assert "LoadGuardPanel" in page and "'부하'" in page, "상세 창에 탭이 없다"


# ── 경보가 화면까지 오는가 ──

def test_the_alarm_text_is_written_when_it_fires_not_when_it_is_read():
    """⚠ 경보가 선 순간의 토크·지속시간은 그 뒤 표본에 덮여 사라진다.

    실기(2026-09-09): 10:22:39 에 11.4N·m 로 울렸는데 2분 뒤 상태를 보니
    `now_nm` 은 0.0 이었다. 읽는 쪽이 나중에 문장을 조립하면 **경보 때와 다른
    숫자**를 말한다 — "0.0N·m 과부하" 같은 소리가 된다.
    """
    w = LoadWatch(LIMITS, name="can0")
    for i in range(20):
        _feed(w, "joint2", 11.4, i * 0.05)
    for i in range(20, 40):
        _feed(w, "joint2", 0.0, i * 0.05)      # 손을 뗐다

    st = w.snapshot()["joints"]["joint2"]
    assert st["now_nm"] == 0.0, "전제가 깨졌다 — 지금값이 안 떨어졌다"
    assert "11.4" in st["last_text"], "경보 당시 토크가 안 남았다"
    assert "can0" in st["last_text"] and st["last_at"] > 0


def test_a_finished_overload_is_still_reported():
    """⚠ **이게 실기에서 놓친 것이다.** 과부하는 몇 초 만에 끝난다. 상태
    (`raised`)로 알리면 자리를 비웠던 사람에게는 아무 일도 없던 것이 된다 —
    사건 카운터로 봐야 "눌렸었다" 가 남는다."""
    from app.services.load_alerts import LoadAlertWatch

    w = LoadAlertWatch()
    quiet = {"joints": {"joint2": {"events": 0, "raised": False, "last_text": ""}}}
    assert w._diff("can0", quiet) == [], "기준을 잡기 전에 알렸다"

    # 과부하가 났다 이미 끝났다 — raised 는 False 인데 카운터는 늘었다
    done = {"joints": {"joint2": {"events": 1, "raised": False,
                                  "last_text": "can0 joint2: 토크 11.4N·m …",
                                  "last_at": 100.0, "peak_nm": 13.5, "over_s": 18.8}}}
    out = w._diff("can0", done)
    assert len(out) == 1, "끝난 과부하를 안 알렸다 — 상태만 보고 있다"
    assert out[0]["joint"] == "joint2" and "11.4" in out[0]["text"]
    assert w._diff("can0", done) == [], "같은 사건을 두 번 알렸다"


def test_a_gateway_restart_does_not_replay_old_events():
    """데몬의 카운터는 게이트웨이 재시작을 넘어 살아남는다 — 기준을 안 잡으면
    부팅하자마자 지난 사건이 방금 난 것처럼 쏟아진다."""
    from app.services.load_alerts import LoadAlertWatch

    w = LoadAlertWatch()
    old = {"joints": {"joint2": {"events": 7, "raised": False, "last_text": "x"}}}
    assert w._diff("can0", old) == [], "처음 본 카운터를 사건으로 셌다"


def test_a_reset_rebaselines_instead_of_alerting():
    """0x150 리셋이 창을 닫으면 카운터가 0 으로 돌아간다 — 줄어든 것은 사건이
    아니라 새 창이다."""
    from app.services.load_alerts import LoadAlertWatch

    w = LoadAlertWatch()
    w._diff("can0", {"joints": {"joint2": {"events": 3, "last_text": "x"}}})
    assert w._diff("can0", {"joints": {"joint2": {"events": 0, "last_text": ""}}}) == []
    out = w._diff("can0", {"joints": {"joint2": {"events": 1, "last_text": "y",
                                                 "last_at": 1.0}}})
    assert len(out) == 1, "리셋 뒤 첫 사건을 놓쳤다"


def test_the_alert_reaches_the_screen():
    """⚠ **이 기능의 첫 실패가 정확히 여기였다.** robotd 는 잡아서 로그에 적고
    있었는데 화면까지 오는 길이 없어서, 사람은 팔을 눌러 놓고 "아무 알람도 안
    뜬다" 고 했다. 감지와 통보는 다른 일이다."""
    main = (_ROOT / "backend" / "app" / "main.py").read_text()
    assert "load_alert_watch" in main and "broadcast_load_alert" in main, \
        "감시 루프가 부하 경보를 안 본다"

    ws = (_ROOT / "backend" / "app" / "core" / "ws_messages.py").read_text()
    types = (_ROOT / "frontend" / "src" / "types" / "ws.ts").read_text()
    assert "robot_load_alert" in ws and "robot_load_alert" in types, \
        "WS 계약 양쪽에 타입이 있어야 한다"

    layout = (_ROOT / "frontend" / "src" / "components" / "Layout.tsx").read_text()
    assert "LoadAlerts" in layout, "리스너가 어디에도 안 붙어 있다"


def test_the_load_message_is_not_wiped_when_the_load_drops():
    """⚠ `DeviceAlerts` 는 매번 `clear(PREFIX)` 한다 — 상태니까 맞다. 과부하에
    같은 짓을 하면 부하가 내려가는 순간 경보가 스스로 사라진다."""
    from conftest import code_only

    src = code_only(
        (_ROOT / "frontend" / "src" / "components" / "LoadAlerts.tsx").read_text())
    assert "clear(" not in src, "부하 경보를 지우고 있다 — 사건은 남아야 한다"
    assert "notify(" in src


def test_the_ceiling_no_longer_blocks_what_the_arm_actually_does():
    """⚠ **처음 적은 상한이 틀렸다.** "드라이버가 9.9N·m 에서 끊는다" 는 J5 한
    건을 보편 한계로 읽은 것이었고, J2 는 13.5N·m 까지 갔는데 안 끊겼다.
    9.0 상한이었다면 J2 에는 쓸 만한 임계를 아예 못 넣는다."""
    from piper_robot import load_store

    assert load_store.clamp_warn(13.5) == 13.5, \
        "실측으로 관측한 값을 임계로 고를 수 없다"
