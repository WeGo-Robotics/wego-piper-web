"""배타 규칙 표의 불변식과 회귀 테스트.

이 표(refactor/10-exclusive-mode-guard.md)를 만든 원인이 "같은 규칙을 8곳에 손으로 적었고
전부 달랐다" 였다. 표가 한 곳이 됐으니, 이제 그 표 자체가 어긋나지 않는지를 지킨다.
"""

import asyncio

import pytest
from fastapi import HTTPException

from app.services import exclusivity as ex
from app.services.exclusivity import Activity


@pytest.fixture
def idle(monkeypatch):
    """모든 활동을 정지 상태로 만들고, 원하는 것만 켜는 헬퍼를 준다."""
    running: set[Activity] = set()
    monkeypatch.setattr(
        ex,
        "STATE_PROVIDERS",
        {a: (lambda a=a: a in running) for a in ex.STATE_PROVIDERS},
    )
    return running


# ── 표 자체의 불변식 ──────────────────────────────────────────────────────────


def test_blocked_by_covers_every_activity():
    """새 Activity 를 추가하고 표에 안 넣으면 조용히 "아무것도 안 막음"이 된다."""
    assert set(ex.BLOCKED_BY) == set(Activity)


def test_no_self_block():
    """자기 자신은 표에 없어야 한다. 자기 실행 여부는 blocking() 이 따로 본다."""
    for activity, blockers in ex.BLOCKED_BY.items():
        assert activity not in blockers, f"{activity.value} 가 자신을 막고 있다"


def test_symmetry_among_stateful_activities():
    """A가 B를 막으면 B도 A를 막아야 한다.

    표를 고칠 때 한쪽만 고치는 것이 정확히 이 리팩터링을 만든 사고다
    (녹화가 추론을 막는데 추론은 녹화를 안 막았다).
    상태가 없는 활동(ENCODER_PROBE, CAMERA_ACCESS)은 남을 막지 못하므로 제외한다.
    """
    stateful = set(ex.STATE_PROVIDERS)
    for a in stateful:
        for b in ex.BLOCKED_BY[a]:
            if b not in stateful:
                continue
            assert a in ex.BLOCKED_BY[b], (
                f"비대칭: {a.value} 는 {b.value} 에 막히는데 "
                f"{b.value} 는 {a.value} 에 안 막힌다"
            )


def test_stateless_activities_block_nobody():
    """상태 제공자가 없는 활동은 조회만 한다 — running() 에 나타나면 안 된다."""
    for a in Activity:
        if a in ex.STATE_PROVIDERS:
            continue
        assert not ex.is_running(a)


def test_every_estop_target_has_a_stopper():
    """정지 방법 없는 E-stop 대상은 조용히 살아남는다."""
    for a in ex.ESTOP_TARGETS:
        assert a in ex.STOPPERS, f"{a.value} 의 정지 방법이 없다"
        assert a in ex.STATE_PROVIDERS, f"{a.value} 의 상태를 알 수 없다"


def test_every_activity_has_a_label():
    """라벨이 없으면 409 메시지 생성에서 KeyError 로 죽는다."""
    for a in Activity:
        assert a in ex.LABELS and ex.LABELS[a]


# ── 동작 ──────────────────────────────────────────────────────────────────────


def test_idle_blocks_nothing(idle):
    assert ex.running() == []
    for a in ex.STATE_PROVIDERS:
        assert ex.blocking(a) == []
        assert ex.blocked_reason(a) is None
        ex.require_idle(a)  # raise 하지 않아야 한다


def test_require_idle_raises_409(idle):
    idle.add(Activity.TRAINING)
    with pytest.raises(HTTPException) as exc:
        ex.require_idle(Activity.INFERENCE)
    assert exc.value.status_code == 409
    assert "학습" in exc.value.detail


def test_self_running_message_differs(idle):
    """이미 자기가 실행 중일 때는 "이미 실행 중" 이라고 말해야 한다."""
    idle.add(Activity.RECORDING)
    with pytest.raises(HTTPException) as exc:
        ex.require_idle(Activity.RECORDING)
    assert "이미 실행 중" in exc.value.detail


def test_stopping_counts_as_busy(monkeypatch):
    """STOPPING 중에도 프로세스는 살아 있고 카메라·CAN·GPU 를 쥐고 있다.

    기존 가드들이 여기서 갈렸다 (`not in (idle, error)` vs `in (running, starting)`).
    """
    from app.services.process_manager import ProcessManager, ProcessState

    pm = ProcessManager()
    for state, expected in [
        (ProcessState.IDLE, False),
        (ProcessState.ERROR, False),
        (ProcessState.STARTING, True),
        (ProcessState.RUNNING, True),
        (ProcessState.STOPPING, True),
    ]:
        pm._state = state
        assert ex._busy(pm.state) is expected, f"{state} 판정이 다르다"


# ── 회귀: 이 리팩터링이 고친 실제 버그들 ──────────────────────────────────────


def test_recording_blocks_training_and_inference(idle):
    """녹화 중에 학습·추론이 시작됐다 (training.py/models.py 에 record 검사가 없었다)."""
    idle.add(Activity.RECORDING)
    for target in (Activity.TRAINING, Activity.INFERENCE):
        with pytest.raises(HTTPException):
            ex.require_idle(target)


def test_training_blocks_recording(idle):
    idle.add(Activity.TRAINING)
    with pytest.raises(HTTPException):
        ex.require_idle(Activity.RECORDING)


def test_policy_server_and_training_are_mutually_exclusive(idle):
    """정책 서버가 어느 가드에도 없어서 학습과 GPU 를 동시에 잡을 수 있었다."""
    idle.add(Activity.TRAINING)
    with pytest.raises(HTTPException):
        ex.require_idle(Activity.POLICY_SERVER)
    idle.clear()
    idle.add(Activity.POLICY_SERVER)
    with pytest.raises(HTTPException):
        ex.require_idle(Activity.TRAINING)


def test_inference_and_policy_server_coexist(idle):
    """서버 모드 추론은 정책 서버를 필요로 한다 — 서로 막으면 안 된다."""
    idle.add(Activity.POLICY_SERVER)
    ex.require_idle(Activity.INFERENCE)
    idle.clear()
    idle.add(Activity.INFERENCE)
    ex.require_idle(Activity.POLICY_SERVER)


def test_dataset_edit_blocked_by_inference(idle):
    """편집이 추론과 같은 전역 ProcessManager 를 써서 핸들을 덮어썼다."""
    idle.add(Activity.INFERENCE)
    with pytest.raises(HTTPException):
        ex.require_idle(Activity.DATASET_EDIT)


def test_upload_blocks_nothing_and_is_blocked_by_nothing(idle):
    """업로드는 네트워크/디스크만 쓴다."""
    idle.add(Activity.UPLOAD)
    for target in (Activity.INFERENCE, Activity.RECORDING, Activity.TRAINING):
        ex.require_idle(target)
    idle.clear()
    idle.add(Activity.TRAINING)
    ex.require_idle(Activity.UPLOAD)


def test_encoder_probe_reports_reason_but_never_raises(idle):
    """인코더는 409 가 아니라 CPU 폴백이다 — blocked_reason 만 쓴다."""
    idle.add(Activity.TRAINING)
    assert ex.blocked_reason(Activity.ENCODER_PROBE) == "학습"
    idle.clear()
    assert ex.blocked_reason(Activity.ENCODER_PROBE) is None


def test_camera_access_blocked_by_inference_and_recording(idle):
    """D405 가 D-state 로 물리는 것을 막는 기존 가드가 유지돼야 한다."""
    for owner, label in [(Activity.INFERENCE, "추론"), (Activity.RECORDING, "녹화")]:
        idle.clear()
        idle.add(owner)
        assert ex.blocked_reason(Activity.CAMERA_ACCESS) == label
    idle.clear()
    assert ex.blocked_reason(Activity.CAMERA_ACCESS) is None


def test_estop_targets_include_recording():
    """E-stop 이 추론만 죽이고 녹화는 안 죽였다. 녹화도 팔을 움직인다."""
    assert Activity.RECORDING in ex.ESTOP_TARGETS
    assert Activity.INFERENCE in ex.ESTOP_TARGETS


def test_estop_all_stops_running_targets_only(idle, monkeypatch):
    stopped: list[Activity] = []

    async def fake_stop(a: Activity):
        stopped.append(a)

    monkeypatch.setattr(
        ex, "STOPPERS", {a: (lambda a=a: fake_stop(a)) for a in ex.ESTOP_TARGETS}
    )
    idle.add(Activity.RECORDING)
    result = asyncio.run(ex.estop_all())
    assert stopped == [Activity.RECORDING]
    assert result == [Activity.RECORDING]


def test_estop_continues_after_one_failure(idle, monkeypatch):
    """하나가 실패해도 나머지는 시도해야 한다 — 부분 성공이라도 해야 한다."""
    stopped: list[Activity] = []

    async def boom():
        raise RuntimeError("kill failed")

    async def ok():
        stopped.append(Activity.RECORDING)

    monkeypatch.setattr(
        ex, "STOPPERS", {Activity.INFERENCE: boom, Activity.RECORDING: ok}
    )
    idle.update({Activity.INFERENCE, Activity.RECORDING})
    result = asyncio.run(ex.estop_all())
    assert stopped == [Activity.RECORDING]
    assert result == [Activity.RECORDING]


# ── 스냅샷 (프론트 계약) ──────────────────────────────────────────────────────


def test_snapshot_shape(idle):
    idle.add(Activity.INFERENCE)
    snap = ex.snapshot()
    assert snap["running"] == ["inference"]
    assert snap["blocked"]["training"] == ["inference"]
    assert snap["blocked"]["inference"] == ["inference"]  # 자기 자신
    assert snap["blocked"]["upload"] == []
    assert snap["labels"]["recording"] == "녹화"
