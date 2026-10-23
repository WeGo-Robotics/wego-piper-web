"""플러그인 레지스트리·실행 토큰·E-stop 배선 (feature/plugins.md §3~§6, 1단계).

실제 프로세스는 띄우지 않는다 — 소유자를 가짜로 바꿔 **무엇을 어떤 환경으로 띄우려
했는지**를 본다. 프로세스 소유권 자체는 process_manager·systemd_process 의 테스트가 진다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core import plugin_scopes
from app.services import plugins as P
from app.services.process_manager import ProcessState

GOOD = """
name: {name}
version: 0.1.0
title: "테스트"
entry: main.py
cameras:
  - {{ key: watch, label: "감시", required: true }}
config:
  - {{ key: conf, kind: number, default: 0.5, min: 0, max: 1 }}
  - {{ key: label, kind: text, default: "x" }}
permissions: [activity.read, inference.start]
resources: {{ gpu: {gpu} }}
"""


def _make(root: Path, name: str, gpu: bool = False, body: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(body if body is not None else GOOD.format(name=name, gpu=str(gpu).lower()))
    (d / "main.py").write_text("print('hi')\n")
    return d


class FakePM:
    def __init__(self) -> None:
        self.state = ProcessState.IDLE
        self.pid = None
        self.cmd = None
        self.env = None
        self._cb = None

    def set_state_callback(self, cb):
        self._cb = cb

    @property
    def is_running(self):
        from app.services.process_manager import ProcessState
        return self.state in (ProcessState.RUNNING, ProcessState.STARTING)

    def set_log_callback(self, cb):
        self._log_cb = cb

    async def start(self, cmd, env_extra=None):
        self.cmd, self.env = cmd, env_extra
        self.state, self.pid = ProcessState.RUNNING, 4242

    async def stop(self):
        self.state, self.pid = ProcessState.IDLE, None

    async def kill(self):
        self.state, self.pid = ProcessState.IDLE, None

    def recent_log(self, n=20):
        return ["bye"]


@pytest.fixture
def reg(tmp_path, monkeypatch):
    from app.core.config import settings

    user_dir = tmp_path / "plugins"
    user_dir.mkdir()
    monkeypatch.setattr(settings, "plugins_dir", user_dir)
    monkeypatch.setattr(P, "BUNDLED_DIR", tmp_path / "bundled")
    r = P.PluginRegistry()
    fakes: dict[str, FakePM] = {}

    def make_process(unit: str):
        pm = fakes.setdefault(unit, FakePM())
        return pm

    monkeypatch.setattr("app.services.systemd_process.make_process", make_process)
    # 카메라 세그먼트: 'top' 만 살아 있다
    monkeypatch.setattr("piper_shm.list_segments", lambda: ["top"])
    # E-stop 동기화는 버스가 필요하다 — 여기선 부르기만 확인
    calls = []
    monkeypatch.setattr("app.services.estop_bridge.estop_bridge.sync_activities", lambda: calls.append(1))
    r._fakes, r._sync_calls = fakes, calls
    return r


# ── 매니페스트 ────────────────────────────────────────────────────────────────


def test_manifest_rejects_bad_names_and_unknown_scopes(tmp_path):
    bad_name = _make(tmp_path, "Bad Name", body=GOOD.format(name="Bad Name", gpu="false"))
    assert "이름" in (P.read_manifest(bad_name).error or "")
    d = _make(tmp_path, "p2", body=GOOD.format(name="p2", gpu="false").replace("activity.read", "root.everything"))
    assert "모르는 권한 범위" in (P.read_manifest(d).error or "")


def test_manifest_reports_yaml_line_numbers(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: broken\nconfig:\n  - { key: a\n")
    (d / "main.py").write_text("")
    err = P.read_manifest(d).error or ""
    assert "문법 오류" in err and "번째 줄" in err


def test_manifest_requires_entry_and_matching_folder_name(tmp_path):
    d = tmp_path / "p3"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: other\n")
    assert "폴더 이름" in (P.read_manifest(d).error or "")
    (d / "plugin.yaml").write_text("name: p3\nentry: nope.py\n")
    assert "entry" in (P.read_manifest(d).error or "")


def test_manifest_star_scope_and_duplicate_keys():
    m = P.Manifest(name="ok", permissions=["*"])
    assert m.title == "ok" and m.permissions == ["*"]
    with pytest.raises(Exception, match="겹친다"):
        P.Manifest(name="dup", config=[{"key": "a"}, {"key": "a"}])


# ── 레지스트리 ────────────────────────────────────────────────────────────────


def test_scan_prefers_user_copy_over_bundled(reg, tmp_path):
    from app.core.config import settings

    _make(P.BUNDLED_DIR, "same")
    _make(settings.plugins_dir, "same")
    infos = {i.name: i for i in reg.scan()}
    assert infos["same"].bundled is False and infos["same"].dir.parent == settings.plugins_dir


def test_start_builds_command_env_and_grant(reg):
    from app.core.config import settings

    _make(settings.plugins_dir, "p1")
    out = asyncio.run(reg.start("p1", {"conf": 0.9}, {"watch": "top"}))

    pm = reg._fakes["piper-plugin-p1"]
    assert pm.cmd[0] == settings.grpc_python and pm.cmd[-1].endswith("p1/main.py")
    env = pm.env
    assert env["PIPER_PLUGIN_NAME"] == "p1"
    assert json.loads(env["PIPER_PLUGIN_CONFIG"]) == {"conf": 0.9, "label": "x"}   # 기본값 채움
    assert json.loads(env["PIPER_PLUGIN_CAMERAS"]) == {"watch": "top"}
    assert env["PIPER_API_BASE"].endswith(f":{settings.port}")
    grant = reg.grant_for(env["PIPER_API_TOKEN"])
    assert grant is not None and grant.name == "p1" and grant.scopes == {"activity.read", "inference.start"}
    assert out["state"] == "running" and out["pid"] == 4242
    # 설정은 다음 시작을 위해 남는다
    assert reg.saved_config("p1")["config"]["conf"] == 0.9
    # E-stop 키
    assert reg.estop_pids() == {"plugin:p1": 4242}
    assert reg._sync_calls


def test_stop_revokes_token_and_clears_estop_pid(reg):
    from app.core.config import settings

    _make(settings.plugins_dir, "p1")
    asyncio.run(reg.start("p1", {}, {"watch": "top"}))
    token = reg._fakes["piper-plugin-p1"].env["PIPER_API_TOKEN"]
    asyncio.run(reg.stop("p1"))
    assert reg.grant_for(token) is None
    assert reg.estop_pids() == {"plugin:p1": None}      # 지우라고 None 을 올린다
    assert reg.status("p1")["state"] == "idle" and reg.status("p1")["log_tail"] == ["bye"]


def test_start_refuses_missing_or_disconnected_camera(reg):
    from fastapi import HTTPException

    from app.core.config import settings

    _make(settings.plugins_dir, "p1")
    with pytest.raises(HTTPException) as e:
        asyncio.run(reg.start("p1", {}, {}))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(reg.start("p1", {}, {"watch": "ghost"}))
    assert e.value.status_code == 409


def test_gpu_plugin_is_blocked_by_training(reg, monkeypatch):
    from fastapi import HTTPException

    from app.core.config import settings
    from app.services import exclusivity as ex

    _make(settings.plugins_dir, "g", gpu=True)
    _make(settings.plugins_dir, "c", gpu=False)
    monkeypatch.setitem(ex.STATE_PROVIDERS, ex.Activity.TRAINING, lambda: True)
    with pytest.raises(HTTPException) as e:
        asyncio.run(reg.start("g", {}, {"watch": "top"}))
    assert e.value.status_code == 409 and "학습" in e.value.detail
    # GPU 를 안 쓰는 플러그인은 학습 중에도 뜬다
    asyncio.run(reg.start("c", {}, {"watch": "top"}))
    assert reg.any_running() and not reg.any_running(gpu_only=True)
    # `gpu: true` 라도 장치를 cpu 로 고르면 이 실행은 GPU 플러그인이 아니다 — 학습 중에도 뜬다
    asyncio.run(reg.start("g", {"device": "cpu"}, {"watch": "top"}))
    assert reg.status("g")["gpu"] is False and not reg.any_running(gpu_only=True)
    asyncio.run(reg.stop("g"))
    monkeypatch.setitem(ex.STATE_PROVIDERS, ex.Activity.TRAINING, lambda: False)
    asyncio.run(reg.start("g", {"device": "cuda:0"}, {"watch": "top"}))
    assert reg.status("g")["gpu"] is True and reg.any_running(gpu_only=True)


def test_double_start_is_409_and_kill_all_clears_everything(reg):
    from fastapi import HTTPException

    from app.core.config import settings

    _make(settings.plugins_dir, "p1")
    asyncio.run(reg.start("p1", {}, {"watch": "top"}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(reg.start("p1", {}, {"watch": "top"}))
    assert e.value.status_code == 409
    asyncio.run(reg.kill_all())
    assert not reg.any_running() and reg._grants == {}


def test_install_copies_folder_and_remove_refuses_bundled(reg, tmp_path):
    from fastapi import HTTPException

    from app.core.config import settings

    src = _make(tmp_path / "elsewhere", "inst")
    (src / ".runtime").mkdir()
    info = reg.install_from_path(src)
    assert info.manifest and info.dir == settings.plugins_dir / "inst"
    assert not (info.dir / ".runtime").exists()
    with pytest.raises(HTTPException) as e:
        reg.install_from_path(src)
    assert e.value.status_code == 409
    _make(P.BUNDLED_DIR, "bun")
    with pytest.raises(HTTPException) as e:
        reg.remove("bun")
    assert e.value.status_code == 400
    reg.remove("inst")
    assert not (settings.plugins_dir / "inst").exists()


def test_orphan_cleanup_touches_only_our_prefix(reg, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "process_runner", "systemd")
    monkeypatch.setattr("shutil.which", lambda _: "/bin/systemctl")
    calls: list[list[str]] = []

    class R:
        returncode = 0
        stdout = ("piper-plugin-a.service loaded active running x\n"
                  "piper-train-local.service loaded active running y\n"
                  "piper-plugin-b.service loaded failed failed z\n")

    def run(argv, **kw):
        calls.append(argv)
        return R()

    monkeypatch.setattr("subprocess.run", run)
    stopped = reg.cleanup_orphans()
    assert stopped == ["piper-plugin-a.service", "piper-plugin-b.service"]
    stops = [c[-1] for c in calls if "stop" in c]
    assert "piper-train-local.service" not in stops


def test_orphan_cleanup_is_a_noop_without_systemd(reg, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "process_runner", "local")
    assert reg.cleanup_orphans() == []


# ── 배타 표와 E-stop 표 ───────────────────────────────────────────────────────


def test_plugin_is_an_estop_target_and_gpu_variant_contends_with_training():
    from app.services import exclusivity as ex

    assert ex.Activity.PLUGIN in ex.ESTOP_TARGETS and ex.Activity.PLUGIN in ex.STOPPERS
    assert ex.Activity.TRAINING in ex.BLOCKED_BY[ex.Activity.PLUGIN_GPU]
    assert ex.Activity.PLUGIN_GPU in ex.BLOCKED_BY[ex.Activity.TRAINING]
    assert ex.BLOCKED_BY[ex.Activity.PLUGIN] == []


def test_bundled_hello_manifest_is_valid():
    """저장소에 든 예제는 늘 읽혀야 한다 — 설치 흐름의 첫 번째 재료다."""
    info = P.read_manifest(P.REPO / "plugins" / "hello")
    assert info.manifest is not None, info.error
    assert info.bundled and info.manifest.permissions == ["activity.read"]
    assert plugin_scopes.allows(frozenset(info.manifest.permissions), "GET", "/api/activity")


# ── 4단계 — 화면이 쓰는 것: zip 설치, 로그·상태 콜백 ─────────────────────────


def _zip_bytes(entries: dict[str, str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_install_from_zip_with_a_top_folder_or_flat_root(reg):
    from app.core.config import settings

    manifest = GOOD.format(name="zipped", gpu="false")
    info = reg.install_from_zip(_zip_bytes({"anything/plugin.yaml": manifest, "anything/main.py": "x"}))
    assert info.manifest and info.dir == settings.plugins_dir / "zipped"      # zip 폴더 이름이 아니라 매니페스트 이름
    reg.remove("zipped")
    info = reg.install_from_zip(_zip_bytes({"plugin.yaml": manifest, "main.py": "x"}))
    assert info.manifest and info.name == "zipped"


def test_install_from_zip_rejects_traversal_and_garbage(reg):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        reg.install_from_zip(_zip_bytes({"../evil/plugin.yaml": "name: evil\n", "../evil/main.py": ""}))
    assert e.value.status_code == 400 and "경로" in e.value.detail
    with pytest.raises(HTTPException) as e:
        reg.install_from_zip(b"not a zip")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        reg.install_from_zip(_zip_bytes({"a/readme.md": "", "b/readme.md": ""}))
    assert e.value.status_code == 400 and "plugin.yaml" in e.value.detail


def test_log_and_state_callbacks_reach_the_screen(reg):
    """WS 가 꽂는 콜백 — 어느 플러그인 것인지 이름이 붙고, 도는 중 로그 꼬리는 상태에도 실린다."""
    from app.core.config import settings

    _make(settings.plugins_dir, "p1")
    got_logs, got_states = [], []
    reg.set_log_callback(lambda n, line: got_logs.append((n, line)))
    reg.set_state_callback(lambda n, st: got_states.append((n, st.value)))
    asyncio.run(reg.start("p1", {}, {"watch": "top"}))
    pm = reg._fakes["piper-plugin-p1"]
    pm._cb(pm.state)                      # 소유자가 상태 변화를 알린 것처럼
    reg._on_log("p1", "hello from plugin")
    assert ("p1", "hello from plugin") in got_logs
    assert ("p1", "running") in got_states
    assert reg.status("p1")["log_tail"] == ["hello from plugin"]


# ── 6단계 — venv 의존, 자원 제한 ──────────────────────────────────────────────


class FakeUnitPM(FakePM):
    """유닛 소유자 흉내 — `unit` 이 있으면 레지스트리가 자원 제한 속성을 넘긴다."""
    unit = "piper-plugin-x"

    async def start(self, cmd, env_extra=None, properties=None):
        self.props = properties
        await super().start(cmd, env_extra)


def test_venv_plugin_refuses_to_start_until_deps_are_installed(reg):
    from fastapi import HTTPException

    from app.core.config import settings

    body = GOOD.format(name="v", gpu="false").replace("permissions:", "python: venv\npermissions:")
    d = _make(settings.plugins_dir, "v", body=body)
    (d / "requirements.txt").write_text("somepkg==1.0\n")
    with pytest.raises(HTTPException) as e:
        asyncio.run(reg.start("v", {}, {"watch": "top"}))
    assert e.value.status_code == 409 and "의존 설치" in e.value.detail
    assert reg.deps("v") == {"python": "venv", "venv_ready": False, "requirements": True, "installing": False, "log_tail": []}

    asyncio.run(reg.install_deps("v"))
    dpm = reg._fakes["piper-plugin-deps-v"]
    script = dpm.cmd[-1]
    assert dpm.cmd[:2] == ["bash", "-c"]
    assert "-m venv --system-site-packages" in script and "requirements.txt" in script and ".deps-ok" in script
    # 설치 로그는 플러그인 로그 채널에도 흐른다 (화면의 로그 탭)
    dpm._log_cb("Collecting somepkg")
    assert "[deps] Collecting somepkg" in reg.recent_logs("v")
    # 끝난 것처럼: 표지 + python
    venv = reg.venv_dir("v")
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    (venv / ".deps-ok").touch()
    dpm.state = ProcessState.IDLE
    assert reg.deps("v")["venv_ready"] is True
    asyncio.run(reg.start("v", {}, {"watch": "top"}))
    assert reg._fakes["piper-plugin-v"].cmd[0] == str(venv / "bin" / "python")


def test_isolated_venv_does_not_inherit_site_packages(reg):
    from app.core.config import settings

    body = GOOD.format(name="iso", gpu="false").replace("permissions:", "python: venv-isolated\npermissions:")
    _make(settings.plugins_dir, "iso", body=body)
    asyncio.run(reg.install_deps("iso"))
    assert "--system-site-packages" not in reg._fakes["piper-plugin-deps-iso"].cmd[-1]


def test_system_python_has_no_deps_step(reg):
    from fastapi import HTTPException

    from app.core.config import settings

    _make(settings.plugins_dir, "sys1")
    with pytest.raises(HTTPException) as e:
        asyncio.run(reg.install_deps("sys1"))
    assert e.value.status_code == 400


def test_resource_limits_go_to_the_unit_owner_only(reg, monkeypatch):
    from app.core.config import settings

    body = GOOD.format(name="lim", gpu="false").replace("resources: { gpu: false }",
                                                          'resources: { gpu: false, memory: "2G", cpu: "150%" }')
    _make(settings.plugins_dir, "lim", body=body)
    unit_pm = FakeUnitPM()
    monkeypatch.setattr("app.services.systemd_process.make_process", lambda unit: unit_pm)
    asyncio.run(reg.start("lim", {}, {"watch": "top"}))
    assert unit_pm.props == {"MemoryMax": "2G", "CPUQuota": "150%"}
    assert reg.status("lim")["limits_applied"] is True


# ── 7단계 — 플러그인 간 입출력 ────────────────────────────────────────────────


def test_inputs_are_resolved_saved_and_handed_to_the_process(reg, monkeypatch):
    from fastapi import HTTPException

    from app.core.config import settings

    body = GOOD.format(name="chain", gpu="false").replace(
        "config:", 'inputs:\n  - { key: det, kind: detections, source: "top" }\n  - { key: grades, kind: topic, required: false }\nconfig:')
    _make(settings.plugins_dir, "chain", body=body)
    monkeypatch.setattr(reg, "sources", lambda: {"detections": {"top": {"publisher": "detector"}}, "topics": {}})
    asyncio.run(reg.start("chain", {}, {"watch": "top"}, {"grades": "grades"}))
    env = reg._fakes["piper-plugin-chain"].env
    assert json.loads(env["PIPER_PLUGIN_INPUTS"]) == {"det": "top", "grades": "grades"}     # 기본 소스 + 고른 것
    st = reg.status("chain")
    assert st["inputs"] == {"det": "top", "grades": "grades"}
    assert st["inputs_live"] == {"det": True, "grades": False}                             # 말만 한다, 막지 않는다
    assert reg.saved_config("chain")["inputs"]["det"] == "top"
    asyncio.run(reg.stop("chain"))
    # 필수 입력에 기본도 선택도 없으면 400
    body2 = GOOD.format(name="chain2", gpu="false").replace("config:", "inputs:\n  - { key: det }\nconfig:")
    _make(settings.plugins_dir, "chain2", body=body2)
    with pytest.raises(HTTPException) as e:
        asyncio.run(reg.start("chain2", {}, {"watch": "top"}))
    assert e.value.status_code == 400 and "입력" in e.value.detail


def test_bundled_size_grader_declares_its_wiring():
    info = P.read_manifest(P.REPO / "plugins" / "size-grader")
    assert info.manifest is not None, info.error
    assert [s.key for s in info.manifest.inputs] == ["det"] and info.manifest.inputs[0].source == "top"
    assert info.manifest.outputs.topics == ["grades"]


# ── 8단계 — 멀티 인스턴스 (§7-4) ─────────────────────────────────────────────


def test_instances_run_side_by_side_with_their_own_token_env_and_estop_key(reg):
    from fastapi import HTTPException

    from app.core.config import settings

    body = GOOD.format(name="det", gpu="false").replace("permissions:", "multi_instance: true\npermissions:")
    _make(settings.plugins_dir, "det", body=body)
    reg.create_instance("det", "cam2")
    assert reg.instances("det") == ["det", "det:cam2"]
    asyncio.run(reg.start("det", {"conf": 0.3}, {"watch": "top"}))
    asyncio.run(reg.start("det:cam2", {"conf": 0.7}, {"watch": "top"}))
    a, b = reg._fakes["piper-plugin-det"], reg._fakes["piper-plugin-det:cam2"]
    assert a.env["PIPER_PLUGIN_NAME"] == "det" and b.env["PIPER_PLUGIN_NAME"] == "det:cam2"
    assert b.env["PIPER_PLUGIN_BASE_NAME"] == "det" and b.env["PIPER_PLUGIN_INSTANCE"] == "cam2"
    assert b.cmd[-1].endswith("det/main.py")                                   # 같은 코드
    assert a.env["PIPER_API_TOKEN"] != b.env["PIPER_API_TOKEN"]
    assert reg.grant_for(b.env["PIPER_API_TOKEN"]).name == "det:cam2"
    assert reg.estop_pids() == {"plugin:det": 4242, "plugin:det:cam2": 4242}
    st = reg.status("det:cam2")
    assert st["plugin"] == "det" and st["instance"] == "cam2" and st["config"]["conf"] == 0.7
    assert reg.status("det")["instance"] is None
    # 인스턴스는 도는 중에 못 지운다, 멈추면 런타임만 지우고 코드는 남는다
    with pytest.raises(HTTPException) as e:
        reg.remove("det:cam2")
    assert e.value.status_code == 409
    asyncio.run(reg.stop("det:cam2"))
    reg.remove("det:cam2")
    assert reg.instances("det") == ["det"] and (settings.plugins_dir / "det" / "main.py").exists()


def test_instances_need_the_manifest_flag_and_a_valid_label(reg):
    from fastapi import HTTPException

    from app.core.config import settings

    _make(settings.plugins_dir, "single")
    with pytest.raises(HTTPException) as e:
        reg.create_instance("single", "b")
    assert e.value.status_code == 400 and "multi_instance" in e.value.detail
    with pytest.raises(HTTPException) as e:
        asyncio.run(reg.start("single:b", {}, {"watch": "top"}))
    assert e.value.status_code == 400
    body = GOOD.format(name="multi", gpu="false").replace("permissions:", "multi_instance: true\npermissions:")
    _make(settings.plugins_dir, "multi", body=body)
    with pytest.raises(HTTPException) as e:
        reg.create_instance("multi", "bad label!")
    assert e.value.status_code == 400
    reg.create_instance("multi", "x")
    with pytest.raises(HTTPException) as e:
        reg.create_instance("multi", "x")
    assert e.value.status_code == 409


def test_instance_inherits_the_default_instance_settings_and_shares_the_venv(reg):
    from app.core.config import settings

    body = GOOD.format(name="vm", gpu="false").replace("permissions:", "multi_instance: true\npython: venv\npermissions:")
    _make(settings.plugins_dir, "vm", body=body)
    reg.save_config("vm", {"conf": 0.9}, {"watch": "top"})
    reg.create_instance("vm", "two")
    assert reg.saved_config("vm:two")["config"]["conf"] == 0.9
    assert reg.venv_dir("vm:two") == reg.venv_dir("vm")
    assert reg.deps("vm:two")["python"] == "venv"
