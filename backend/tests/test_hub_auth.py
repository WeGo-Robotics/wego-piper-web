"""HuggingFace 로그인 — 설정의 저장소 탭.

⚠ **컨테이너 배포에는 로그인 경로가 아예 없었다.** 개발 머신은 호스트 토큰을
보는데 컨테이너의 `HF_HOME` 은 `/data/hf` 라 거기엔 토큰이 없다. 호스트에서
`huggingface-cli login` 을 해도 컨테이너는 그 파일을 못 본다.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import code_only

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
PANEL = _SRC / "components" / "HfAccountPanel.tsx"


@pytest.fixture
def client():
    return TestClient(app)


def test_an_empty_token_is_refused(client):
    """공백만 넣고 저장하면 **멀쩡한 토큰을 지운다.** 400 으로 막는다."""
    assert client.post("/api/hub/login", json={"token": "   "}).status_code == 400


def test_a_bad_token_is_refused_and_not_echoed(client):
    """⚠ **검증하고 나서 저장한다.** 그냥 쓰면 오타 하나로 조용히 미로그인이 되고,
    그 사실은 몇 시간 뒤 업로드 단계에서야 드러난다.

    ⚠ 그리고 **예외 문자열에 토큰이 섞여 나올 수 있다.** 그대로 내보내면 에러
    메시지가 자격증명 유출 경로가 된다.
    """
    bad = "hf_this_token_is_not_real_000"
    r = client.post("/api/hub/login", json={"token": bad})
    assert r.status_code == 401, r.text
    assert bad not in r.text, "에러 응답에 토큰이 그대로 실렸다"


def test_the_token_is_never_returned(client):
    """한 번 넣은 값을 화면에서 다시 읽을 수 있게 하면 그 화면이 유출 경로가 된다."""
    body = client.get("/api/hub/whoami").json()
    assert "token" not in body, "응답에 토큰 자체가 들어 있다"
    assert set(body) <= {
        "logged_in", "username", "fullname", "avatar_url", "orgs",
        "token_name", "token_role", "token_path", "error",
    }, f"예상 밖 필드: {sorted(body)}"


def test_the_ui_says_where_the_token_lives(client):
    """⚠ 호스트에서 로그인해도 컨테이너는 다른 자리를 본다. 어디에 저장되는지가
    보여야 "로그인했는데 왜 안 되지" 를 안 겪는다."""
    assert client.get("/api/hub/whoami").json().get("token_path"), "자리를 안 알려준다"
    assert "token_path" in PANEL.read_text(), "화면이 자리를 안 보여준다"


def test_a_read_only_token_is_called_out():
    """⚠ 읽기 전용 토큰으로도 `whoami` 는 성공한다 — 로그인돼 보이는데 업로드에서만
    실패하고, 학습이라면 그걸 **몇 시간 뒤 마지막 단계에서** 만난다."""
    src = PANEL.read_text()
    assert "token_role === 'read'" in src, "권한을 안 본다"
    assert "읽기 전용" in src, "사용자에게 안 알린다"


def test_the_input_is_cleared_after_saving():
    """화면에 남겨 두면 어깨너머로 읽히고, 실수로 복사되어 다른 곳에 붙는다."""
    body = PANEL.read_text().split("const login", 1)[1].split("const logout", 1)[0]
    assert "setToken('')" in body, "입력란을 안 비운다"
    assert 'type="password"' in PANEL.read_text(), "토큰이 화면에 그대로 보인다"


def test_logout_asks_first():
    """⚠ 되돌릴 수 없다 — 토큰을 다시 받아 와야 한다. 그리고 `window.confirm` 은
    쓰지 않는다(이벤트 루프를 멈춰 E-stop heartbeat 을 끊는다)."""
    src = PANEL.read_text()
    assert "await confirm(" in src, "묻지 않고 지운다"
    assert "window.confirm" not in src, "window.confirm 은 heartbeat 을 끊는다"


# ── 학습 시작 전 검사 ────────────────────────────────────────────────────────


def _whoami(monkeypatch, result):
    """`hub_client.get_api().whoami` 를 갈아끼운다. `result` 가 예외면 던진다."""
    from app.services import hub_client

    class _Api:
        def whoami(self):
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(hub_client, "get_api", lambda: _Api())


@pytest.fixture(autouse=True)
def leave_training_idle():
    """⚠ **이 파일은 학습 시작을 실제로 두드린다.** 인자가 가짜라 곧 실패하지만,
    그 사이 `train_manager` 가 켜진 상태로 남는다 — 그러면 **뒤에 오는 테스트가
    409(배타 모드)** 를 받는다. 실제로 `test_yolo_train_api` 가 그렇게 깨졌고,
    단독으로는 통과해서 원인을 찾는 데 시간이 걸렸다.
    """
    yield
    from app.services.process_manager import ProcessState
    from app.services.training import train_manager

    # 러너가 둘이다(local/systemd). 상태를 쥔 쪽을 찾아 되돌린다 —
    # 어느 쪽이든 뒤 테스트가 409 를 받으면 안 된다.
    owner = getattr(train_manager.runner, "pm", train_manager.runner)
    if hasattr(owner, "_set_state"):
        owner._set_state(ProcessState.IDLE)


def _start(client, extra=()):
    return client.post("/api/training/start-custom",
                       json={"args": ["lerobot-train", "--policy.repo_id=me/x", *extra],
                             "total_steps": 10})


def test_training_is_blocked_when_not_logged_in(client, monkeypatch):
    """⚠ `--policy.repo_id` 를 쓰면 학습이 **끝나고 나서** Hub 로 올린다. 토큰이
    없으면 몇 시간을 돌린 뒤 마지막 단계에서 실패한다 — 체크포인트는 남지만
    화면에는 실패로 뜨고, 사람은 학습이 통째로 날아간 줄 안다."""
    _whoami(monkeypatch, {})           # name 이 없다 = 미로그인
    r = _start(client)
    assert r.status_code == 400, r.text
    assert "로그인" in r.json()["detail"]


def test_training_is_blocked_on_a_read_only_token(client, monkeypatch):
    """읽기 전용 토큰으로도 `whoami` 는 성공한다 — 로그인돼 보이는데 업로드만 죽는다."""
    _whoami(monkeypatch, {"name": "me", "auth": {"accessToken": {"role": "read"}}})
    r = _start(client)
    assert r.status_code == 400
    assert "읽기 전용" in r.json()["detail"]


def test_a_write_token_passes(client, monkeypatch):
    """막는 것은 **확실히 실패할 때**뿐이다."""
    _whoami(monkeypatch, {"name": "me", "auth": {"accessToken": {"role": "write"}}})
    assert _start(client).status_code != 400


def test_an_unreachable_hub_does_not_block_training(client, monkeypatch):
    """⚠ **토큰이 나쁜 것과 HF 가 안 닿는 것은 다르다.** 사내망이 느리다고 학습을
    못 걸게 하면 그게 더 나쁘다 — 확인할 수 없으면 통과시킨다."""
    _whoami(monkeypatch, OSError("network unreachable"))
    assert _start(client).status_code != 400


def test_turning_push_off_skips_the_check(client, monkeypatch):
    """올릴 생각이 없으면 권한을 물을 이유가 없다."""
    _whoami(monkeypatch, {})           # 미로그인인데도
    assert _start(client, ["--policy.push_to_hub=false"]).status_code != 400


# ── repo_id 입력 ────────────────────────────────────────────────────────────

REPO_INPUT = _SRC / "components" / "RepoIdInput.tsx"


def test_the_namespace_is_picked_not_typed():
    """⚠ 통째로 타이핑하면 `/` 를 빠뜨린다. LeRobot 은 `repo_id.split("/")` 를
    **두 개로 언패킹**하므로 슬래시가 없으면 녹화가 시작에서 죽는다.
    네임스페이스는 고를 수 있는 값(내 계정 + 소속 조직)이라 드롭다운이면 그
    실수가 사라진다."""
    src = REPO_INPUT.read_text()
    assert "<select" in src, "네임스페이스를 고를 수 없다"
    assert "acc.orgs" in src, "소속 조직이 후보에 없다"
    for page in ("RecordingPage.tsx", "TrainingPage.tsx"):
        assert "RepoIdInput" in (_SRC / "pages" / page).read_text(), \
            f"{page} 가 아직 통짜 입력이다"


def test_leaving_it_empty_is_an_explicit_choice():
    """⚠ 학습의 `policy.repo_id` 는 **비울 수 있다** — 비우면
    `--policy.push_to_hub=false` 가 붙어 로컬에만 남는다. 빈 칸을 그냥 두는 것과
    "올리지 않음" 을 고르는 것은 다르다. 후자만 의도한 것으로 읽힌다."""
    src = REPO_INPUT.read_text()
    assert "allowEmpty" in src, "비울 수 있는 자리를 구분하지 않는다"
    assert "올리지 않음" in src, "비움이 선택지로 안 보인다"

    train = (_SRC / "pages" / "TrainingPage.tsx").read_text()
    assert "<RepoIdInput value={policyRepoId} allowEmpty" in train, \
        "학습에서 비울 수 없다 — 로컬 학습을 못 하게 된다"
    rec = (_SRC / "pages" / "RecordingPage.tsx").read_text()
    assert "allowEmpty" not in rec.split("RepoIdInput", 1)[1][:200], \
        "녹화는 비우면 안 된다 — 데이터셋을 어디에 쓸지 정해야 한다"


def test_it_falls_back_to_a_text_box_only_when_logged_out():
    """⚠ 글상자로 떨어지는 경우는 **하나뿐이어야 한다**: 로그인이 안 돼 후보를
    모를 때. 한때 "저장된 값이 내 네임스페이스가 아니면" 도 폴백이었는데, 그러면
    남의 저장소를 가리키던 화면에서 고르기가 아예 안 붙은 것처럼 보였다."""
    src = code_only(REPO_INPUT.read_text())   # TSX 다 — `//` 를 걷는다
    assert "if (spaces.length === 0)" in src, "폴백 조건이 로그인 여부가 아니다"
    assert "설정 → 저장소에서 로그인하면" in src, "왜 못 고르는지 안 알려준다"


def test_a_foreign_namespace_stays_in_the_list():
    """⚠ 내 것이 아닌 네임스페이스를 목록에서 **빼면 안 된다.** 빼면 고를 수 없는
    값이 되어 통짜 글상자로 되돌아간다. 넣어 두고 남의 것이라고 적으면, 조용히
    다른 곳을 가리키는 것도 막고 고르기도 계속 쓸 수 있다."""
    src = REPO_INPUT.read_text()
    assert "foreign ? [ns, ...mine] : mine" in src, "남의 네임스페이스가 목록에서 빠진다"
    assert "내 계정 아님" in src, "남의 것인지 구분이 안 된다"
    assert "업로드가 거부될 수 있습니다" in src, "왜 문제인지 안 알려준다"
