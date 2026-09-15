"""브라우저 API 클라이언트의 배압 — 요청이 쌓여 탭이 마비되지 않게 (실기 2026-09-14).

## 무슨 일이 있었나

게이트웨이를 재시작한 뒤 WS 가 한동안 안 붙었다. E-stop heartbeat 가 0.5초마다 HTTP 로
떨어졌고, 그 요청들이 응답을 못 받은 채(개발 프록시가 물고 있음) 미리보기·조명·장치 폴링과
함께 수백 개 쌓였다. Chrome 이 탭당 한도를 넘기자 **수집 정지 버튼의 요청까지
`ERR_INSUFFICIENT_RESOURCES` 로 브라우저 밖으로 나가지 못했다.** 게이트웨이 로그엔 정지
요청이 아예 없었고, 화면은 아무 말이 없었다 — 사용자는 "세 버튼이 다 안 된다"만 봤다.

## 여기서 잠그는 것

1. 모든 요청에 **시간 제한**이 있다 — 응답 없는 요청은 끊겨 자리를 비운다
2. 같은 GET 이 아직 안 끝났으면 **새로 보내지 않는다** — 폴링 타이머가 응답보다 빨라도 안 쌓인다
3. heartbeat 는 **짧게** 끊는다 — 못 닿으면 watchdog 이 세우는 게 맞다
4. 수집 제어 버튼은 **실패를 말한다** — 조용한 실패가 "안 된다"로 보였다
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "frontend" / "src" / "services" / "api.ts"
ESTOP = REPO / "frontend" / "src" / "components" / "EStopButton.tsx"
RECORD = REPO / "frontend" / "src" / "pages" / "RecordingPage.tsx"


def _code(path: Path) -> str:
    from conftest import code_only
    return code_only(path.read_text())


def test_every_request_has_a_timeout():
    src = _code(API)
    assert "AbortController" in src and "signal: ctl.signal" in src, "요청에 시간 제한이 없다 — 응답 없는 요청이 쌓인다"
    m = re.search(r"DEFAULT_TIMEOUT_MS = ([\d_]+)", src)
    assert m and 5_000 <= int(m.group(1).replace("_", "")) <= 60_000, "기본 시간 제한이 없거나 터무니없다"
    assert "clearTimeout(timer)" in src, "타이머를 안 지운다"
    assert "AbortError" in src and "응답 없음" in src, "시간 초과를 사람이 읽을 말로 안 바꾼다"


def test_identical_gets_in_flight_are_shared_not_stacked():
    src = _code(API)
    assert "inflightGets" in src and "inflightGets.get(path)" in src and "inflightGets.delete(path)" in src, \
        "같은 GET 이 진행 중인데 또 보낸다 — 폴링이 겹쳐 쌓인다"
    get = src.split("get: <T>(path: string", 1)[1].split("post:", 1)[0]
    assert "if (cur) return cur" in get, "진행 중인 약속을 돌려주지 않는다"
    # POST 는 공유하지 않는다 — 명령은 각각이다
    post = src.split("post: <T>", 1)[1].split("put:", 1)[0]
    assert "inflightGets" not in post


def test_the_heartbeat_is_cut_short_instead_of_piling_up():
    src = _code(ESTOP)
    m = re.search(r"api\.post\('/estop/heartbeat',[^)]*\{ timeoutMs: (\d+) \}\)", src)
    assert m and int(m.group(1)) <= 5000, "HTTP heartbeat 에 짧은 시간 제한이 없다 — 0.5초마다 쌓인다"


def test_recording_controls_report_failure_instead_of_staying_silent():
    src = _code(RECORD)
    assert "const control = async (path: string, label: string)" in src
    ctl = src.split("const control = async", 1)[1].split("const handleStop", 1)[0]
    assert "timeoutMs" in ctl and "notify({" in ctl and "level: 'error'" in ctl and "요청 실패" in ctl, \
        "정지·저장하고 다음·재녹화가 실패해도 화면이 조용하다"
    for path, label in (("/recording/stop", "정지"), ("/recording/skip", "저장하고 다음"), ("/recording/rerecord", "재녹화")):
        assert f"control('{path}', '{label}')" in src, f"{label} 버튼이 실패를 안 알린다"
