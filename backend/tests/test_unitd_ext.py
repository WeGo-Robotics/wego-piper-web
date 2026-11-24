"""장치 확장 — 웹에서 이미지를 받아 사용자 유닛으로 켠다 (feature/pika.md §2).

지키는 것: 매니페스트는 장치 어휘만 고르고 ExecStart 를 못 쓴다, 허용 레지스트리 밖은 안 받는다,
핵심 데몬의 RPC 허브를 가로채지 못한다, 유닛은 검사한 이미지 ID 로 돈다, 등록된 확장만 unitd 가 만진다,
이 기계에서 빌드한 이미지는 제거 때 안 지운다.

docker·systemctl 은 가짜다 — 이 기계의 진짜 유닛과 등록부를 건드리지 않게 HOME·데이터 루트를 임시로 돌린다.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PIKA = {"name": "pika", "title": "PIKA (Sense·Gripper)", "contract": 1,
        "devices": ["usb-serial", "usb-raw", "hidraw"],
        "provides": {"pose": ["pika_sense_l", "pika_sense_r"], "grip": ["pika_gripper1"]},
        "rpc_hub": "pika", "features": ["feedback"], "ui": "server"}


def _load(name: str, file: str):
    sys.path.insert(0, str(REPO / "daemons"))
    spec = importlib.util.spec_from_file_location(name, REPO / "daemons" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def X(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PIPER_EXT_REGISTRY", raising=False)
    monkeypatch.delenv("PIPER_EXT_REGISTRIES", raising=False)
    monkeypatch.setenv("PIPER_DATA_ROOT", str(tmp_path / "data"))
    mod = _load("unitd_ext", "unitd_ext.py")
    sys.modules["unitd_ext"] = mod            # unitd 가 같은 모듈을 보게
    return mod


class FakeHost:
    """docker·systemctl 흉내 — 무엇을 불렀는지 남긴다."""

    def __init__(self, X, monkeypatch, images: dict[str, dict]):
        self.images = images
        self.calls: list[tuple] = []
        monkeypatch.setattr(X, "_docker", lambda: "/usr/bin/docker")
        monkeypatch.setattr(X, "inspect", lambda image: self.images.get(image))
        monkeypatch.setattr(X, "hidraw_major", lambda: 240)
        monkeypatch.setattr(X, "_systemctl", lambda *a, timeout=30: self._sys(a))
        monkeypatch.setattr(X.subprocess, "run", lambda cmd, **kw: self._run(cmd))

    def _sys(self, args):
        self.calls.append(("systemctl", *args))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def _run(self, cmd):
        self.calls.append(tuple(cmd))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _image(manifest, iid="sha256:" + "a" * 64):
    return {"id": iid, "labels": {"io.wego.piper.extension": json.dumps(manifest)}, "tags": []}


# ── 매니페스트 ──

def test_the_pika_manifest_passes_as_is(X):
    m = X.validate_manifest(PIKA)
    assert m["rpc_hub"] == "pika" and m["devices"] == ["usb-serial", "usb-raw", "hidraw"]
    assert m["provides"]["pose"] == ["pika_sense_l", "pika_sense_r"] and m["ui"] == "server"


@pytest.mark.parametrize("patch, msg", [
    ({"name": "Pika"}, "이름"),
    ({"name": "../x"}, "이름"),
    ({"contract": 2}, "계약 버전"),
    ({"devices": ["privileged"]}, "장치 어휘"),
    ({"devices": ["usb-raw", "/dev:/dev"]}, "장치 어휘"),
    ({"rpc_hub": "robotd"}, "핵심 데몬"),
    ({"rpc_hub": "estopd"}, "핵심 데몬"),
    ({"ui": "/etc"}, "ui"),
    ({"provides": {"arm": ["x"]}}, "provides"),
    ({"provides": {"pose": ["../x"]}}, "provides"),
])
def test_a_manifest_cannot_reach_beyond_the_vocabulary(X, patch, msg):
    with pytest.raises(ValueError, match=msg):
        X.validate_manifest({**PIKA, **patch})


def test_two_extensions_cannot_share_an_rpc_hub(X):
    with pytest.raises(ValueError, match="다른 확장"):
        X.validate_manifest(PIKA, taken_hubs={"pika"})


@pytest.mark.parametrize("ref", ["ghcr.io/wego-robotics/pika-docker:0.1.0", "pika-docker:dev",
                                 "ghcr.io/wego-robotics/pika-docker@sha256:" + "b" * 64])
def test_image_refs_that_look_like_images_pass(X, ref):
    assert X.check_image_ref(ref) == ref


@pytest.mark.parametrize("ref", ["-v/:/host", "--privileged", "a b", "x;rm -rf /", "", "ghcr.io/x:$(id)"])
def test_an_image_ref_cannot_smuggle_docker_options(X, ref):
    with pytest.raises(ValueError, match="이미지 주소"):
        X.check_image_ref(ref)


def test_only_allowed_registries_are_pulled(X, monkeypatch):
    assert X.registry_allowed("ghcr.io/wego-robotics/pika-docker:1")
    assert not X.registry_allowed("ghcr.io/someone/pika-docker:1")
    assert not X.registry_allowed("docker.io/library/ubuntu")
    monkeypatch.setenv("PIPER_EXT_REGISTRIES", "registry.lan:5000/robots")
    assert X.registry_allowed("registry.lan:5000/robots/pika:1")


# ── 유닛 템플릿 ──

def _entry(X, manifest=PIKA):
    return {"image": "ghcr.io/wego-robotics/pika-docker:0.1.0", "image_id": "sha256:" + "c" * 64,
            "manifest": X.validate_manifest(manifest), "data_dir": "/srv/piper-data/ext/pika"}


def test_the_unit_isolates_the_network_and_opens_only_the_declared_devices(X):
    text = X.render_unit(_entry(X), docker="/usr/bin/docker", hidraw=240)
    run = text.split("ExecStart=", 1)[1].split("\nExecStop", 1)[0]
    assert "--network none" in run and "--ipc host" in run
    assert '"--device-cgroup-rule=c 188:* rmw"' in run and '"--device-cgroup-rule=c 189:* rmw"' in run
    assert '"--device-cgroup-rule=c 240:* rmw"' in run
    assert "-v /srv/piper-data/ext/pika:/data" in run and "PIPER_EXT_RPC_HUB=pika" in run
    for banned in ("--privileged", "--cap-add", "--network host", "--pid"):
        assert banned not in run, f"템플릿에 {banned} 가 있다"
    # 이름표(태그)가 아니라 검사한 이미지 ID 로 돈다
    assert run.rstrip().endswith("sha256:" + "c" * 64)


def test_a_deviceless_extension_does_not_see_dev(X):
    text = X.render_unit(_entry(X, {**PIKA, "devices": []}), docker="/usr/bin/docker", hidraw=240)
    assert "/dev:/dev" not in text and "device-cgroup-rule" not in text


def test_hidraw_is_skipped_when_the_host_has_no_hidraw_major(X):
    text = X.render_unit(_entry(X), docker="/usr/bin/docker", hidraw=None)
    assert "c None" not in text and "c 188:* rmw" in text


# ── 추가·제거 ──

def test_adding_a_local_image_writes_a_unit_registers_and_starts_it(X, monkeypatch):
    host = FakeHost(X, monkeypatch, {"pika-docker:dev": _image(PIKA)})
    out = X.install("pika-docker:dev")
    assert out == {"name": "pika", "unit": "piper-ext-pika.service"}
    unit = X.unit_dir() / "piper-ext-pika.service"
    assert unit.exists() and "sha256:" + "a" * 64 in unit.read_text()
    reg = X.load()
    assert reg["pika"]["pulled"] is False, "이 기계에 있던 이미지 — 받지 않았다"
    assert Path(reg["pika"]["data_dir"]).is_dir()
    assert ("systemctl", "enable", "piper-ext-pika.service") in host.calls
    assert ("systemctl", "restart", "piper-ext-pika.service") in host.calls
    assert not any(c[:2] == ("/usr/bin/docker", "pull") for c in host.calls)
    assert X.units() == {"ext-pika": "PIKA (Sense·Gripper)"}


def test_an_image_from_an_unknown_registry_is_refused_before_pulling(X, monkeypatch):
    host = FakeHost(X, monkeypatch, {})
    with pytest.raises(ValueError, match="허용 레지스트리"):
        X.install("docker.io/evil/pika:1")
    assert X.load() == {} and not host.calls


def test_an_image_without_the_label_is_refused(X, monkeypatch):
    FakeHost(X, monkeypatch, {"ubuntu:22.04": {"id": "sha256:" + "d" * 64, "labels": {}, "tags": []}})
    with pytest.raises(ValueError, match="라벨"):
        X.install("ubuntu:22.04")
    assert X.load() == {} and not list(X.unit_dir().glob("*.service")) if X.unit_dir().exists() else True


def test_removing_keeps_a_locally_built_image_but_drops_a_pulled_one(X, monkeypatch):
    host = FakeHost(X, monkeypatch, {"pika-docker:dev": _image(PIKA)})
    X.install("pika-docker:dev")
    X.remove("pika")
    assert X.load() == {} and not (X.unit_dir() / "piper-ext-pika.service").exists()
    assert not any(c[:3] == ("/usr/bin/docker", "image", "rm") for c in host.calls), "개발 이미지를 지웠다"
    # 받은 것은 지운다
    X.install("pika-docker:dev")
    reg = X.load()
    reg["pika"]["pulled"] = True
    X._save(reg)
    X.remove("pika")
    assert any(c[:3] == ("/usr/bin/docker", "image", "rm") for c in host.calls)


# ── unitd 허용 목록 ──

def test_unitd_controls_a_registered_extension_and_nothing_else(X, monkeypatch):
    FakeHost(X, monkeypatch, {"pika-docker:dev": _image(PIKA)})
    u = _load("unitd", "unitd.py")
    with pytest.raises(ValueError, match="우리 유닛"):
        u.check_action("ext-pika", "start")
    X.install("pika-docker:dev")
    assert u.check_action("ext-pika", "stop") == "piper-ext-pika.service"
    assert u.check_action("piper-ext-pika", "disable") == "piper-ext-pika.service"
    assert u.catalog()["ext-pika"] == ("PIKA (Sense·Gripper)", "extension")
    assert "piper-ext-pika.service" in u.log_units("all")
    with pytest.raises(ValueError, match="우리 유닛"):
        u.check_action("ext-other", "start")
