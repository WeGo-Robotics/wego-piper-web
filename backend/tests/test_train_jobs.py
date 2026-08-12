"""학습 job 레지스트리 (feature/cloud-training.md 3단계).

지금까지 `TrainManager` 는 **단일 job 가정**이었다 — `train_manager.state` 하나가 곧
"학습 상태"고, WS 메시지에 누구 것인지가 없었다. 원격 job 이 하나라도 붙는 순간
두 job 이 서로의 상태를 덮어쓴다.

여기서 잠그는 것:

1. **로컬도 job 이다** (`job_id="local"`) — 원격용 경로를 따로 만들면 UI 가 두 벌이 된다
2. **학습 WS 메시지는 전부 `job_id` 를 싣는다** — 안 실으면 프론트가 거를 수가 없다
3. **레코드가 프로세스 밖에 있다** — 게이트웨이가 재시작해도 학습이 계속 보인다
4. **죽은 job 을 running 으로 남기지 않는다** — UI 가 영원히 "학습 중"이 된다
"""

import ast
import re
from pathlib import Path

import pytest

from app.core import ws_messages as M

_REPO = Path(__file__).resolve().parents[2]


# ── Redis 없이 도는 계약 검사 ────────────────────────────────────────────────

def test_all_train_messages_carry_job_id():
    """**핵심 계약** — 학습 브로드캐스트에 `job_id` 가 빠지면 프론트가 못 거른다.

    `ws.py` 에서 `M.TRAIN_*` 를 보내는 dict 리터럴에 `job_id` 키가 있는지 AST 로 본다.
    """
    tree = ast.parse((_REPO / "backend" / "app" / "routers" / "ws.py").read_text())
    scoped_consts = {
        n for n in dir(M)
        if isinstance(getattr(M, n, None), str) and getattr(M, n) in M.JOB_SCOPED
    }

    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        if "type" not in keys:
            continue
        # `"type": M.TRAIN_LOG` 형태에서 상수 이름을 꺼낸다
        value = node.values[keys.index("type")]
        if not (isinstance(value, ast.Attribute) and value.attr in scoped_consts):
            continue
        checked += 1
        assert "job_id" in keys, (
            f"ws.py:{node.lineno} — {value.attr} 메시지에 job_id 가 없다. "
            "job 이 둘이 되는 순간 서로의 상태를 덮어쓴다"
        )
    assert checked >= 3, f"job 스코프 메시지를 {checked}개밖에 못 찾았다 (패턴이 바뀌었나?)"


def test_frontend_declares_job_id_on_train_messages():
    """프론트 유니언도 같아야 한다 — 여기 없으면 `msg.job_id` 가 컴파일 에러다."""
    src = (_REPO / "frontend" / "src" / "types" / "ws.ts").read_text()
    union = src.split("export type WsMessage =", 1)[1].split("export type WsMessageType", 1)[0]
    for t in sorted(M.JOB_SCOPED):
        line = next((ln for ln in union.splitlines() if f"type: '{t}'" in ln), None)
        assert line, f"프론트 유니언에 {t} 가 없다"
        assert "job_id" in line, f"프론트 {t} 에 job_id 가 없다: {line.strip()}"


def test_local_job_id_matches_across_languages():
    """백엔드 계약과 프론트 상수가 같아야 한다. 다르면 로컬 학습이 화면에서 사라진다."""
    from piper_bus import contract as C

    src = (_REPO / "frontend" / "src" / "types" / "ws.ts").read_text()
    m = re.search(r"LOCAL_JOB_ID\s*=\s*'([^']+)'", src)
    assert m, "프론트에 LOCAL_JOB_ID 가 없다"
    assert m.group(1) == C.LOCAL_JOB_ID


def test_training_page_filters_by_job_id():
    """필터링이 없으면 job_id 를 실어도 소용이 없다."""
    src = (_REPO / "frontend" / "src" / "pages" / "TrainingPage.tsx").read_text()
    assert "job_id" in src and "viewJobId" in src, "TrainingPage 가 job_id 로 거르지 않는다"


def test_concurrent_limit_is_a_named_constant():
    """상한이 코드에 흩어지면 클라우드가 붙을 때 여러 곳을 고치게 된다."""
    from app.services.training.jobs import MAX_CONCURRENT_JOBS

    assert isinstance(MAX_CONCURRENT_JOBS, int) and MAX_CONCURRENT_JOBS >= 1


# ── Redis 가 있어야 도는 것 ──────────────────────────────────────────────────

pytest.importorskip("redis")
from piper_bus import Bus, contract as C  # noqa: E402

from app.services.process_manager import ProcessState  # noqa: E402
from app.services.training.jobs import JobRecord, JobRegistry  # noqa: E402


@pytest.fixture
def registry():
    b = Bus()
    if not b.ping():
        pytest.skip("Redis 미실행")
    b.r.delete(C.TRAIN_JOBS, C.TRAIN_LOG_LINES)
    for k in b.r.scan_iter(match=C.train_log_key("*")):
        b.r.delete(k)
    yield JobRegistry(b)
    b.r.delete(C.TRAIN_JOBS, C.TRAIN_LOG_LINES)


def test_roundtrip(registry):
    registry.put(JobRecord(job_id="local", state=ProcessState.RUNNING.value, total_steps=1000))
    got = registry.get("local")
    assert got is not None
    assert got.state == ProcessState.RUNNING.value and got.total_steps == 1000
    assert got.is_active


def test_list_is_newest_first(registry):
    registry.put(JobRecord(job_id="a", created_at="2026-08-01T00:00:00+09:00"))
    registry.put(JobRecord(job_id="b", created_at="2026-08-02T00:00:00+09:00"))
    assert [j.job_id for j in registry.list()] == ["b", "a"]


def test_active_only_counts_unfinished(registry):
    registry.put(JobRecord(job_id="run", state=ProcessState.RUNNING.value))
    registry.put(JobRecord(job_id="done", state=ProcessState.IDLE.value))
    assert [j.job_id for j in registry.active()] == ["run"]


def test_unknown_fields_are_dropped_not_fatal(registry):
    """옛 레코드가 남아 있어도 죽지 않아야 한다 — 필드는 앞으로 계속 는다."""
    registry._b().put_job("old", {"job_id": "old", "state": "running", "죽은필드": 1})
    got = registry.get("old")
    assert got is not None and got.job_id == "old"


def test_log_ring_buffer_reports_what_it_dropped(registry):
    """**조용히 버리지 않는다** — 몇 줄이 잘렸는지 알려줘야 UI 가 '…생략'을 띄운다."""
    over = C.TRAIN_LOG_MAX + 50
    for i in range(over):
        registry.append_log("local", f"line-{i}")
    out = registry.logs("local", 0, 10)
    assert out["total"] == over
    assert out["buffered"] == C.TRAIN_LOG_MAX
    assert out["dropped"] == 50
    # 링버퍼는 **뒤쪽(최신)** 을 남긴다
    assert out["lines"][0] == "line-50"


def test_delete_clears_logs_too(registry):
    registry.put(JobRecord(job_id="local"))
    registry.append_log("local", "x")
    registry.delete("local")
    assert registry.get("local") is None
    assert registry.logs("local")["total"] == 0


def test_registry_survives_a_dead_bus():
    """**버스가 죽어도 학습은 돌아야 한다.** 레지스트리는 부가 기능이지 실행 경로가 아니다."""
    class DeadBus:
        def __getattr__(self, _name):
            def boom(*a, **k):
                raise ConnectionError("redis down")
            return boom

    reg = JobRegistry(DeadBus())
    reg.put(JobRecord(job_id="local"))          # 예외가 새어나오면 학습 시작이 실패한다
    assert reg.get("local") is None
    assert reg.list() == []
    reg.append_log("local", "line")
    assert reg.logs("local")["lines"] == []


def test_manager_marks_dead_job_as_finished(registry):
    """**죽은 job 을 running 으로 남기면 UI 가 영원히 '학습 중'이라고 말한다.**

    서버 재시작 시 `runner.restore()` 가 실패하는데 레코드는 살아 있는 상황.
    """
    from app.services.training.manager import TrainManager

    class NoRestoreRunner:
        state = ProcessState.IDLE
        is_running = False
        pid = None

        def set_log_callback(self, cb): pass
        def set_state_callback(self, cb): pass
        async def start(self, spec): pass
        async def stop(self): pass
        def restore(self): return None

    registry.put(JobRecord(
        job_id="local", state=ProcessState.RUNNING.value,
        total_steps=5000, output_dir="/tmp/out",
    ))
    tm = TrainManager(runner=NoRestoreRunner(), registry=registry)

    assert tm.restore_running_process() is False
    after = registry.get("local")
    assert after.state == ProcessState.IDLE.value
    # **정리한다고 내용까지 지우면 안 된다.** 재시작 직후 트래커는 비어 있는데
    # 그 빈 값으로 덮어쓰면 "재시작해도 학습이 보인다"는 이점이 사라진다.
    assert after.total_steps == 5000, "빈 트래커가 total_steps 를 0으로 덮어썼다"
    assert after.output_dir == "/tmp/out", "빈 output_dir 이 기존 값을 지웠다"
