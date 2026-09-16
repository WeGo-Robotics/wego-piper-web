"""인스턴스 수명 — **띄우는 동사와 끄는 동사는 같이 온다** (feature/vast-training.md §6, §8 W2).

⚠ 이 파일이 지키는 것은 **돈**이다. 지금 원격 학습에는 자동 가드가 사실상 없고(§11-0),
여기 있는 것 하나하나가 그 빈칸을 메운다. 전부 2026-09-16 실기에서 실제로 밟은
함정에서 나왔다 — 짐작으로 쓴 방어는 하나도 없다.
"""

import pytest

from app.services.cloud.providers import vast
from app.services.cloud.providers.base import CloudProvider, Instance


def _inst(iid=1, label="piper-x", status="running", **kw):
    return {"id": iid, "label": label, "actual_status": status,
            "gpu_name": "RTX 3060", "dph_total": 0.1,
            "ssh_host": "ssh5.vast.ai", "ssh_port": 32214, **kw}


class _Fake(vast.VastProvider):
    """CLI 를 흉내 낸다. `calls` 에 인자를, `rows` 에 남은 인스턴스를 둔다."""

    def __init__(self, rows=None, on_create=None):
        super().__init__()
        self.rows = list(rows or [])
        self.calls: list[list[str]] = []
        self.on_create = on_create

    def _raw(self, args, *, timeout=None):
        self.calls.append(args)
        if args[:2] == ["show", "instances"]:
            return self.rows
        if args[:2] == ["create", "instance"]:
            return self.on_create if self.on_create is not None else {
                "success": True, "new_contract": 42}
        if args[:2] == ["destroy", "instance"]:
            self.rows = [r for r in self.rows if str(r["id"]) != args[2]]
            raise RuntimeError("Expecting value")   # 실측: 빈 출력 → 파싱 실패
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 계약
# ─────────────────────────────────────────────────────────────────────────────

def test_the_provider_can_both_start_and_stop():
    """⚠ 하나만 있으면 "빌릴 수는 있는데 끌 수는 없는" 상태다 — 돈이 새는 구조다."""
    assert isinstance(vast.VastProvider(), CloudProvider)
    for verb in ("create", "destroy", "list_instances", "status"):
        assert callable(getattr(vast.VastProvider, verb, None)), f"{verb} 가 없다"


# ─────────────────────────────────────────────────────────────────────────────
# 파기 — 여기가 가장 중요하다
# ─────────────────────────────────────────────────────────────────────────────

def test_destroy_passes_y_or_it_does_not_destroy_anything():
    """⚠ **실측**: `-y` 가 없으면 `[y/N]` 프롬프트에서 멈췄다가 `Aborted.` 를 찍고
    **종료코드 0** 으로 끝난다. `&& echo 완료` 가 완료를 찍고, 인스턴스는 살아서 과금된다.

    이 세션에서 실제로 그렇게 됐다 — 파기한 줄 알았는데 목록에 그대로 있었다.
    """
    p = _Fake([_inst(7)])
    p.destroy(7)
    call = next(c for c in p.calls if c[:2] == ["destroy", "instance"])
    assert "-y" in call, f"-y 가 없다: {call}"


def test_destroy_is_judged_by_the_list_not_by_the_reply():
    """⚠ **실측**: `destroy -y --raw` 는 **빈 출력**을 낸다 — 파싱할 것이 없다.

    그래서 응답이 아니라 목록에서 사라졌는지로 판정한다. 파싱 실패는 정상 경로다.
    """
    p = _Fake([_inst(7), _inst(8)])
    assert p.destroy(7) is True          # 응답은 예외를 던졌지만 목록에서 사라졌다
    assert {i.id for i in p.list_instances()} == {8}


def test_destroy_reports_false_when_the_instance_survives():
    """⚠ 살아남은 것을 성공이라 말하면 **과금이 계속되는데 아무도 안 본다.**"""
    class _Stubborn(_Fake):
        def _raw(self, args, *, timeout=None):
            self.calls.append(args)
            if args[:2] == ["show", "instances"]:
                return self.rows
            if args[:2] == ["destroy", "instance"]:
                return {}                 # 지워지지 않았다
            return {}

    assert _Stubborn([_inst(7)]).destroy(7) is False


def test_an_error_body_never_reads_as_an_empty_list():
    """⚠ **실측**: CLI 는 오류도 종료코드 0 으로 내고 본문에 `{"error": true}` 를 싣는다.

    그걸 그대로 받으면 오류가 **빈 목록**으로 둔갑하고, 빈 목록은 "아무것도 안 돌고
    있다" 로 읽힌다 — 돈이 걸린 판단에서 가장 위험한 착각이다.
    """
    class _Err(_Fake):
        def _raw(self, args, *, timeout=None):
            return {"error": True, "msg": "Invalid user key"}

    with pytest.raises(RuntimeError):
        _Err().list_instances()


# ─────────────────────────────────────────────────────────────────────────────
# 생성
# ─────────────────────────────────────────────────────────────────────────────

def test_create_refuses_to_leave_a_stopped_instance_behind():
    """⚠ `--cancel-unavail` 이 없으면 스케줄 실패 시 **정지된 인스턴스가 조용히 생기고
    스토리지 과금이 계속된다.** 실패는 실패로 끝나야 한다."""
    p = _Fake([_inst(42)])
    p.create(1, template_hash="h", disk_gb=40, label="piper-x")
    call = next(c for c in p.calls if c[:2] == ["create", "instance"])
    assert "--cancel-unavail" in call


def test_create_always_states_the_disk_size():
    """⚠ 템플릿의 `recommended_disk_space` 가 적용 안 되면 기본값으로 떠서 이미지
    전개가 안 들어가고 pull 이 실패한다."""
    p = _Fake([_inst(42)])
    p.create(1, template_hash="h", disk_gb=40, label="piper-x")
    call = next(c for c in p.calls if c[:2] == ["create", "instance"])
    assert "--disk" in call and call[call.index("--disk") + 1] == "40"


def test_create_labels_the_instance_because_that_is_how_orphans_are_found():
    """⚠ 라벨은 장식이 아니다 — 레지스트리를 잃으면 **우리 것을 알아보는 유일한 단서**다."""
    p = _Fake([_inst(42, label="piper-job7")])
    got = p.create(1, template_hash="h", disk_gb=40, label="piper-job7")
    call = next(c for c in p.calls if c[:2] == ["create", "instance"])
    assert call[call.index("--label") + 1] == "piper-job7"
    assert got.label == "piper-job7"


@pytest.mark.parametrize("reply", [
    {"success": False, "msg": "no offer"},
    {"success": True},                      # id 가 없다
    {"success": True, "new_contract": None},
    [],
])
def test_create_raises_instead_of_pretending(reply):
    """⚠ 만들어졌는지 모르는 채로 성공을 돌려주면, **만들어졌는데 아무도 모르는**
    인스턴스가 생긴다 — 고아의 정의다."""
    with pytest.raises(RuntimeError):
        _Fake([], on_create=reply).create(1, template_hash="h", disk_gb=40, label="x")


# ─────────────────────────────────────────────────────────────────────────────
# 상태
# ─────────────────────────────────────────────────────────────────────────────

def test_status_reads_actual_status_not_the_intended_one():
    """⚠ CLI 1.7 이 실제로 읽는 필드는 `actual_status` 다. `intended_status` 로 판단하면
    "곧 그렇게 될 것" 을 "그렇다" 로 읽는다 — pull 중인 기계를 준비됐다고 말하게 된다."""
    p = _Fake([_inst(3, status="loading", intended_status="running",
                     cur_state="running")])
    got = p.status(3)
    assert got.status == "loading" and got.running is False


def test_status_is_none_once_it_is_gone():
    assert _Fake([]).status(3) is None


def test_ssh_target_comes_from_the_instance():
    got = _Fake([_inst(3)]).status(3)
    assert got.ssh.host == "ssh5.vast.ai" and got.ssh.port == 32214 and got.ssh.user == "root"


def test_no_ssh_target_while_it_is_still_coming_up():
    """주소가 없는 것과 `localhost` 를 주는 것은 다르다 — 없으면 없다고 한다."""
    p = _Fake([_inst(3, status="loading", ssh_host=None, ssh_port=None)])
    assert p.status(3).ssh is None


# ─────────────────────────────────────────────────────────────────────────────
# 가장 안쪽 가드 — 사람도 게이트웨이도 없을 때 학습을 끝내는 것
# ─────────────────────────────────────────────────────────────────────────────

def test_the_script_caps_its_own_runtime_when_asked():
    """⚠ §6-1. 사람·게이트웨이·인터넷이 전부 사라져도 학습은 반드시 끝나야 한다.

    지금 원격 학습을 자동으로 멈추는 것은 **이것 말고 없다**(§11-0).
    `timeout` 은 이미지에 있고(실측 `/usr/bin/timeout`, coreutils 9.1) 죽일 때 124 를 낸다.
    """
    from app.services.training.runners.ssh import SSHRunner
    from app.services.training.spec import TrainJobSpec

    body = SSHRunner(host="d")._build_script(
        TrainJobSpec(cmd=["python", "train.py"], max_hours=6))
    assert "timeout 21600 python train.py" in body


def test_no_cap_means_the_command_is_untouched():
    """로컬 학습은 상한이 0 이라 지금과 **똑같이** 돌아야 한다."""
    from app.services.training.runners.ssh import SSHRunner
    from app.services.training.spec import TrainJobSpec

    body = SSHRunner(host="d")._build_script(TrainJobSpec(cmd=["python", "train.py"]))
    assert "\npython train.py\n" in body and "timeout" not in body


def test_the_timeout_exit_code_still_reaches_the_marker():
    """⚠ `timeout` 이 죽이면 124 가 나온다. 그게 마커로 흘러야 화면이 "끝났다" 가 아니라
    "시간 초과로 끊겼다" 를 말할 수 있다 — 삼키면 정상 완주와 구별이 안 된다."""
    from app.services.training.runners.ssh import _EXIT_MARK, SSHRunner
    from app.services.training.spec import TrainJobSpec

    lines = [l for l in SSHRunner(host="d")._build_script(
        TrainJobSpec(cmd=["python", "t.py"], max_hours=1)).splitlines() if l.strip()]
    i = next(n for n, l in enumerate(lines) if l.startswith("timeout "))
    assert lines[i + 1] == f'echo "{_EXIT_MARK} $?"', "마커가 명령 바로 뒤가 아니다"


# ─────────────────────────────────────────────────────────────────────────────
# 화면이 읽는 창구 — **사람이 유일한 가드인 동안 볼 수 있어야 한다**
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import cloud as router

    state = {"rows": [], "destroyed": []}

    class _P:
        def list_instances(self):
            return [router.vast.parse_instance(r) if hasattr(router.vast, "parse_instance")
                    else vast.VastProvider()._parse_instance(r) for r in state["rows"]]

        def destroy(self, iid):
            state["destroyed"].append(iid)
            state["rows"] = [r for r in state["rows"] if r["id"] != iid]
            return True

    monkeypatch.setattr(router, "_provider", lambda: _P())
    return TestClient(app), state


def test_instances_are_listed_with_what_they_cost(client):
    c, st = client
    st["rows"] = [_inst(5, label="piper-j1")]
    r = c.get("/api/cloud/instances")
    assert r.status_code == 200
    got = r.json()["instances"][0]
    assert got["id"] == 5 and got["rate_usd_h"] == 0.1 and got["status"] == "running"


def test_an_instance_the_registry_does_not_know_is_flagged_as_an_orphan(client, monkeypatch):
    """⚠ 게이트웨이가 죽었다 살아나거나 레코드를 잃으면 생긴다. 라벨이 **레지스트리를
    잃고도 남는 유일한 단서**다(§6-3)."""
    from app.routers import cloud as router

    c, st = client
    st["rows"] = [_inst(5, label="piper-j1")]
    monkeypatch.setattr(router.job_registry, "list", lambda: [])
    d = c.get("/api/cloud/instances").json()
    assert d["orphans"] == 1 and d["instances"][0]["orphan"] is True


def test_someone_elses_instance_is_never_called_ours(client, monkeypatch):
    """⚠ 남의 것을 고아라 하면 사람이 **남의 학습을 끈다** — 더 나쁜 실수다."""
    from app.routers import cloud as router

    c, st = client
    st["rows"] = [_inst(5, label="someone-else")]
    monkeypatch.setattr(router.job_registry, "list", lambda: [])
    d = c.get("/api/cloud/instances").json()
    assert d["orphans"] == 0 and d["instances"][0]["orphan"] is False


def test_orphans_are_listed_first_because_they_are_what_needs_looking_at(client, monkeypatch):
    from app.routers import cloud as router

    c, st = client
    st["rows"] = [_inst(1, label="piper-known"), _inst(9, label="piper-lost")]

    class _Rec:
        instance_id = "1"
    monkeypatch.setattr(router.job_registry, "list", lambda: [_Rec()])
    ids = [i["id"] for i in c.get("/api/cloud/instances").json()["instances"]]
    assert ids == [9, 1], "고아가 맨 위가 아니다"


def test_destroying_reports_success_only_when_it_is_confirmed(client):
    c, st = client
    st["rows"] = [_inst(5)]
    r = c.delete("/api/cloud/instances/5")
    assert r.status_code == 200 and r.json()["destroyed"] is True
    assert st["destroyed"] == [5]


def test_an_unconfirmed_destroy_is_not_a_200(client, monkeypatch):
    """⚠ **조용한 성공이 이 경로에서 가장 비싼 거짓말이다** — 사람이 껐다고 믿고
    자리를 뜬다. 그리고 요금은 계속 나간다."""
    from app.routers import cloud as router

    class _Stubborn:
        def list_instances(self):
            return []

        def destroy(self, iid):
            return False

    monkeypatch.setattr(router, "_provider", lambda: _Stubborn())
    c, _ = client
    r = c.delete("/api/cloud/instances/5")
    assert r.status_code == 502
    assert "과금이 계속될 수 있습니다" in r.json()["detail"]
    assert "vastai show instances" in r.json()["detail"], "사람이 할 일을 안 적었다"
