"""녹화 wrapper 의 종료 위생 — 정상 종료가 에러처럼 보이지 않게.

녹화가 끝날 때마다 로그에 두 덩어리가 쏟아져 사용자가 실패로 읽었다.
둘 다 **작업이 다 끝난 뒤** 나오는 것이라 데이터에는 영향이 없었지만,
진짜 원인을 가리고 종료 코드까지 오염시켰다.

1. `pynput` ImportError 스택트레이스 — X 가 없는 환경의 정상 동작
2. `terminate called without an active exception` — C++ 정적 소멸자의 abort
"""

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SRC = (_REPO / "wrapper" / "start_record.py").read_text()


def test_is_headless_is_patched_in_the_module_that_calls_it():
    """`lerobot_record` 가 `from ... import is_headless` 로 **자기 네임스페이스에**
    들여온다 — 원본 모듈(`control_utils`)만 고치면 아무 효과가 없다.

    이 구분을 놓치면 "고쳤는데 로그가 그대로"가 된다.
    """
    assert "LR.is_headless" in _SRC, "lerobot_record 네임스페이스를 안 고쳤다"
    assert "CU.is_headless" in _SRC, "control_utils 쪽도 함께 막아둔다"


def test_exits_without_running_cxx_static_destructors():
    """`record()` 뒤에 `os._exit(0)` — abort 로 종료 코드가 더럽혀지지 않게.

    ⚠ **버퍼를 먼저 비워야 한다.** `os._exit` 는 파이썬 정리를 통째로 건너뛰므로
    flush 없이 부르면 마지막 로그 줄이 사라진다.
    """
    tree = ast.parse(_SRC)
    main = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "__name__ == '__main__'"
    )
    body = [ast.unparse(s) for s in main.body]
    assert body[0] == "record()", f"record() 가 먼저여야 한다: {body}"
    assert "os._exit(0)" in body[-1], f"마지막이 os._exit 이어야 한다: {body}"
    flushes = [b for b in body if "flush()" in b]
    assert len(flushes) >= 2, f"stdout/stderr 를 모두 비워야 한다: {body}"
    assert body.index(flushes[-1]) < len(body) - 1, "flush 가 _exit 보다 뒤에 있다"


def test_names_used_at_exit_are_imported():
    """`sys`/`os` 가 실제로 import 돼 있는지.

    이 파일은 부트스트랩을 정리하다 살아있는 import 를 지운 전력이 있어
    (refactor/03-wrapper-bootstrap.md) 정적으로 못 박는다.
    """
    tree = ast.parse(_SRC)
    top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    assert {"os", "sys"} <= top, f"종료 경로가 쓰는 모듈이 없다: {top}"


# ── 녹화 중 task 변경 ────────────────────────────────────────────────────────

def test_task_override_hooks_record_loop_not_the_frame_loop():
    """**에피소드 경계에서만** 바꾼다.

    LeRobot 은 `record_loop` 진입 시점의 task 를 그 에피소드의 **모든 프레임**에 찍는다
    (`frame = {..., "task": single_task}`). 프레임 단위로 갈아끼우면 한 에피소드 안에서
    task 가 섞여 "에피소드 = 하나의 task" 전제가 깨진다.
    """
    assert "LR.record_loop" in _SRC, "record_loop 을 감싸지 않았다"
    assert "log_rerun_data" not in _SRC.split("_orig_record_loop", 1)[1], (
        "프레임 단위 훅에 task 를 끼워넣고 있다"
    )


def test_task_override_falls_back_to_the_cli_value():
    """버스에 값이 없으면 `--dataset.single_task` 를 그대로 쓴다."""
    tree = ast.parse(_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_record_loop")
    src = ast.unparse(fn)
    assert "if task and" in src, "빈 값일 때도 덮어써서 task 가 사라질 수 있다"
    assert "except Exception" in src, "버스가 죽으면 녹화가 멈춘다"


def test_next_episode_control_is_not_called_a_skip():
    """**회귀** — `right` 는 건너뛰기가 아니다.

    LeRobot 은 이 신호로 루프를 빠져나온 뒤 `dataset.save_episode()` 로 떨어진다 —
    **에피소드는 저장된다.** "건너뛰기"라고 부르면 사용자가 "이번 걸 버린다"로 읽고,
    실제로 그렇게 오해했다. 버리는 것은 재녹화(`left`)뿐이다.
    """
    from piper_bus import contract as C

    assert not hasattr(C, "CONTROL_SKIP"), "오해를 부르는 이름이 되살아났다"
    assert C.CONTROL_NEXT == "right"

    ui = (_REPO / "frontend" / "src" / "pages" / "RecordingPage.tsx").read_text()
    controls = ui.split("에피소드 제어", 1)[1].split("Task 변경", 1)[0]
    # JSX 주석은 화면에 안 보인다 — "건너뛰기가 아니다"라는 설명까지 잡으면 안 된다
    visible = re.sub(r"\{/\*.*?\*/\}", "", controls, flags=re.S)
    assert "건너뛰기" not in visible, "에피소드 제어 UI 에 '건너뛰기' 가 남아 있다"
    assert "저장" in visible, "저장된다는 사실이 문구에 없다"


# ── wrapper 의 전역 대입 (gRPC 원격 추론) ───────────────────────────────────

def test_wrappers_declare_the_globals_they_assign():
    """**회귀** — gRPC 원격 추론이 제어 루프 첫 줄에서 죽었다.

        UnboundLocalError: cannot access local variable '_paused'

    `main()` 이 `_paused` 를 대입하면서 `global` 선언을 빠뜨렸다. 파이썬은 그러면
    지역 변수로 보고, 읽는 순간 터진다 — 원격 추론이 **한 번도 돈 적이 없었다**
    (Total steps: 0). 실행해봐야만 드러나는 종류라 여기서 정적으로 막는다.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "wrapper"
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        module_globals = {
            t.id for n in tree.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name) and t.id.startswith("_")
        }
        if not module_globals:
            continue
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            declared = {nm for n in ast.walk(fn) if isinstance(n, ast.Global) for nm in n.names}
            assigned = {
                t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)
            }
            # 읽기도 하는 것만 문제다 — 대입만 하고 안 읽으면 지역 변수여도 무해하다
            read = {n.id for n in ast.walk(fn)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            shadowed = (assigned & module_globals & read) - declared
            assert not shadowed, (
                f"{path.name}:{fn.name} 이 전역 {sorted(shadowed)} 을 global 선언 없이 "
                "대입한다 — 읽는 순간 UnboundLocalError 다")
