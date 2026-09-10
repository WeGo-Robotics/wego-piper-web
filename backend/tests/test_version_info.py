"""버전 정보 — "지금 도는 것이 무엇인가" (feature/version-update.md §2, 1단계).

정본은 코드 밖이다(이미지 매니페스트·git). 바깥 소프트웨어는 출처가 셋(컨테이너·
호스트·데몬)이고, 없는 것은 비워 둔다 — 지어내지 않는다.
"""

import importlib.util
import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_the_version_comes_from_env_then_manifest_then_git(monkeypatch, tmp_path):
    """앱은 자기 버전을 모른다(package.json 0.0.0, pyproject 0.1.0). 릴리스 태그가 곧
    버전이고 그 사실은 이미지 ENV → 매니페스트 → git 순으로 남는다. 셋 다 없으면
    "unknown" — 지어내지 않는다."""
    from app.services import version as V
    mf = tmp_path / "manifest.txt"
    mf.write_text('version="v0.4.5"\nprev="v0.4.4"\nbuilt_at="2026-09-09T19:05:53+09:00"\nregistry="piper-build:5000"\n')
    monkeypatch.setattr(V, "MANIFEST", mf)
    monkeypatch.setenv("PIPER_VERSION", "v0.4.6")
    r = V.running_version()
    assert r["version"] == "v0.4.6" and r["source"] == "env" and r["prev"] == "v0.4.4"
    monkeypatch.delenv("PIPER_VERSION")
    r = V.running_version()
    assert r["version"] == "v0.4.5" and r["source"] == "manifest" and r["built_at"].startswith("2026-09-09")
    monkeypatch.setattr(V, "MANIFEST", tmp_path / "none.txt")
    r = V.running_version()
    assert r["source"] == "git" and r["version"].startswith("v0.")      # 이 저장소엔 태그가 있다
    monkeypatch.setattr(V.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert V.running_version() == {"version": "unknown", "source": None}


def test_wheels_field_says_what_this_release_actually_touched(monkeypatch, tmp_path):
    """`wheels`가 없거나 비면 이번 릴리스가 데몬 wheel 을 하나도 안 구웠다는 뜻 —
    화면의 wheel 드리프트 경고가 이 값으로 "무엇을 견줄지"를 정한다
    (v0.4.10을 .120에 실기로 올려 보고서야 드러난 오탐 — CHANGELOG v0.4.11)."""
    from app.services import version as V
    mf = tmp_path / "manifest.txt"
    mf.write_text('version="v0.4.7"\nprev="v0.4.6"\nbuilt_at="x"\nwheels="bus robot sim"\n')
    monkeypatch.setattr(V, "MANIFEST", mf)
    monkeypatch.delenv("PIPER_VERSION", raising=False)
    assert V.running_version()["wheels"] == "bus robot sim"
    mf.write_text('version="v0.4.10"\nprev="v0.4.9"\nbuilt_at="x"\nwheels=""\n')
    assert V.running_version()["wheels"] == ""


def test_cuda_is_read_off_the_torch_version_string_without_importing_torch():
    """`2.11.0+cu130` → 13.0. torch 를 import 하면 무겁다 — 문자열이면 된다."""
    from app.services.version import cuda_of
    assert cuda_of("2.11.0+cu130") == "13.0"
    assert cuda_of("2.9.1+cu128") == "12.8"
    assert cuda_of("2.11.0") is None and cuda_of(None) is None
    src = inspect.getsource(importlib.import_module("app.services.version") if False else __import__("app.services.version", fromlist=["x"]))
    assert "import torch" not in src


def test_daemons_report_their_package_versions_and_every_daemon_has_a_dist_list():
    """컨테이너 게이트웨이는 호스트 venv 를 볼 수 없다 — 데몬이 자기 것을 재서 자기
    보고에 싣는다. 우리 패키지는 소스 목록에서, 바깥 것은 계약 표에서. 없는 배포는
    빼놓는다."""
    from piper_bus import contract as C
    from piper_bus.client import dist_versions, self_report
    assert set(C.DAEMON_DISTS) == set(C.DAEMON_SOURCES), "데몬마다 바깥 배포 목록이 있어야 한다"
    assert "mujoco" in C.DAEMON_DISTS[C.SIMD] and "pyrealsense2" in C.DAEMON_DISTS[C.RSD]
    vs = dist_versions(C.DAEMON_SOURCES[C.SIMD])
    assert "piper-bus" in vs and "piper-sim" in vs, vs
    assert "nonexistent-dist" not in dist_versions(("daemons/nope.py", "nope"))
    rep = self_report(REPO, C.DAEMON_SOURCES[C.UNITD])
    assert rep["versions"].get("piper-bus") and rep["pid"] and rep["started"]


def test_unitd_reads_the_driver_from_proc_not_nvidia_smi():
    """nvidia-smi 는 드라이버가 걸리면 D-state 로 멈춰 RPC 루프를 먹는다 — 자원 패널이
    그걸로 한 번 통째로 굳었다. 버전은 /proc 에서 읽는다."""
    src = (REPO / "daemons" / "unitd.py").read_text()
    body = src.split("def host_info", 1)[1].split("\n    def ", 1)[0]
    code = body.split('"""', 2)[2]          # 독스트링은 nvidia-smi 를 **왜** 안 쓰는지 말한다
    assert "/proc/driver/nvidia/version" in code and "nvidia-smi" not in code
    assert '"host_info"' in src.split("_METHODS = ", 1)[1][:80]
    spec = importlib.util.spec_from_file_location("unitd", REPO / "daemons" / "unitd.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    info = mod.UnitHub().host_info()
    assert "versions" in info and "deploy" in info and info["versions"].get("python")


def test_the_release_stamps_the_tag_into_the_image_and_the_wheels():
    """wheel 이 전부 0.1.0 이면 호스트에 어느 릴리스가 깔렸는지 아무도 모른다 —
    release.sh 가 굽는 사본의 버전을 태그로 바꾸고 이미지에 ENV 로 박는다. 저장소의
    pyproject 는 그대로다."""
    df = (REPO / "backend" / "Dockerfile").read_text()
    assert "ARG PIPER_VERSION" in df and "ENV PIPER_VERSION=${PIPER_VERSION}" in df
    rel = (REPO / "deploy" / "release.sh").read_text()
    assert 'PIPER_VERSION="$VERSION" docker compose build "${IMAGES[@]}"' in rel
    assert 'PIPER_VERSION="$VERSION" "$REPO/deploy/stage-hostside.sh"' in rel
    compose = (REPO / "docker-compose.yml").read_text()
    assert "PIPER_VERSION: ${PIPER_VERSION:-unknown}" in compose, "compose 가 build arg 를 환경에서 안 받는다"
    stage = (REPO / "deploy" / "stage-hostside.sh").read_text()
    assert 'STAMP="${PIPER_VERSION#v}"' in stage and "sed -i" in stage and "mktemp -d" in stage
    assert 'version = "0.1.0"' in (REPO / "sim" / "pyproject.toml").read_text(), "저장소 pyproject 를 건드렸다"
    apply = (REPO / "deploy" / "apply.sh").read_text()
    assert 'echo "$version" > "$SRC/VERSION"' in apply, "적용본 기록이 없다"
    assert "for d in estopd robotd camerad rsd unitd; do systemctl --user restart" in apply


def test_the_version_card_draws_only_what_arrived_and_flags_wheel_drift():
    src = (REPO / "frontend" / "src" / "components" / "VersionCard.tsx").read_text()
    assert "/system/version" in src and "wheelMismatch" in src
    assert "piper-unitd 가 말합니다" in src and "데몬 자기 보고" in src
    # ⚠ 게이트웨이 버전 == 모든 wheel 버전은 틀린 전제였다 — 이번 릴리스가 실제로
    # 구운 패키지(매니페스트 wheels)만 견줘야 한다. 문자열 검사라 얕지만, 옛
    # 시그니처(`wheelMismatch(gw.version, info.daemons)`, 인자 둘)가 돌아오면 잡는다.
    assert "wheelMismatch(gw.version, gw.wheels, info.daemons)" in src
    page = (REPO / "frontend" / "src" / "pages" / "SettingsPage.tsx").read_text()
    assert "<VersionCard />" in page.split("tab === 'services'", 1)[1][:300]
    router = (REPO / "backend" / "app" / "routers" / "system.py").read_text()
    assert '@router.get("/version")' in router
