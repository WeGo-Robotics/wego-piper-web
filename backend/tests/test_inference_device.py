"""추론 장치 선택 — GPU 가 없는 PC 에서도 CPU 로 돌릴 수 있다 (2026-10-10).

예전에는 `device`/`policy_device` 가 `cuda` 로 박혀 있어 GPU 없는 기계에서 로컬 추론이 시작조차 못
했다(UI 에도 선택지가 없었다). ACT 는 CPU 로도 청크 단위로는 돈다(카메라 2대 기준 청크당 수백 ms).

여기서 지키는 것: 기본은 **예전 그대로 cuda**(GPU 있는 기계의 명령이 한 글자도 안 바뀐다) ·
cpu 를 고르면 로컬은 `--device cpu`, 서버는 `--policy-device cpu` · cpu 에서는 AMP 를 켜지 않는다 ·
cuda/cpu 밖의 값은 거절 · **미리보기와 실행이 같은 조립기를 타서 화면의 명령이 거짓이 되지 않는다**.
"""

import pytest
from pydantic import ValidationError


def _args(device=None, mode="local"):
    from app.routers.models import InferencePreviewRequest, _build_args_for

    kw = {"checkpoint_path": "/models/act/pretrained_model", "inference_mode": mode,
          "robot_port": "can0", "policy_type": "act"}
    if device is not None:
        kw["device"] = device
    return _build_args_for(InferencePreviewRequest(**kw), "piper_follower", "can0")


def _value(args, flag):
    return args[args.index(flag) + 1]


def test_the_default_is_cuda_exactly_as_before():
    args = _args()
    assert _value(args, "--device") == "cuda"
    assert "--use-amp" in args, "GPU 기계의 기본 명령이 달라졌다"


def test_cpu_runs_the_local_wrapper_on_cpu_without_amp():
    args = _args("cpu")
    assert _value(args, "--device") == "cpu"
    assert "--use-amp" not in args, "CPU 에서 AMP 를 켜 둘 이유가 없다"


def test_cpu_in_server_mode_goes_to_the_policy_device_flag():
    args = _args("cpu", mode="server")
    assert _value(args, "--policy-device") == "cpu"
    assert "cuda" not in args


def test_server_mode_default_stays_cuda():
    assert _value(_args(mode="server"), "--policy-device") == "cuda"


def test_only_cuda_or_cpu_is_accepted():
    from app.routers.models import InferencePreviewRequest, InferenceStartRequest

    for model in (InferencePreviewRequest, InferenceStartRequest):
        with pytest.raises(ValidationError):
            model(checkpoint_path="/m", device="tpu")


def test_the_start_request_carries_the_choice_too():
    """미리보기만 받고 시작이 못 받으면 화면의 명령과 실제 실행이 갈린다."""
    from app.routers.models import InferenceStartRequest

    assert InferenceStartRequest(checkpoint_path="/m", device="cpu").device == "cpu"
    assert InferenceStartRequest(checkpoint_path="/m").device == "cuda"


# ── GPU 없는 기계는 CUDA 를 못 고른다 ──


@pytest.fixture
def nodes(monkeypatch):
    """`/dev` 의 장치 노드와 `nvidia-smi` 유무를 흉내 낸다."""
    import glob
    import shutil

    state = {"glob": [], "smi": None}
    monkeypatch.setattr(glob, "glob", lambda pat: [p for p in state["glob"] if p.startswith(pat.split("[")[0].rstrip("*"))])
    monkeypatch.setattr(shutil, "which", lambda name: state["smi"] if name == "nvidia-smi" else None)
    return state


def test_no_nvidia_node_and_no_smi_means_no_cuda(nodes):
    from app.services import resources

    assert resources.cuda_present() is False


def test_an_nvidia_device_node_means_cuda(nodes):
    from app.services import resources

    nodes["glob"] = ["/dev/nvidia0"]
    assert resources.cuda_present() is True


def test_a_jetson_gpu_node_means_cuda(nodes):
    from app.services import resources

    nodes["glob"] = ["/dev/nvhost-gpu"]
    assert resources.cuda_present() is True


def test_the_driver_tool_alone_means_cuda(nodes):
    from app.services import resources

    nodes["smi"] = "/usr/bin/nvidia-smi"
    assert resources.cuda_present() is True


def test_proc_driver_nvidia_is_not_trusted():
    """컨테이너의 /proc 은 호스트 커널의 것이라 GPU 를 안 준 컨테이너에서도 보인다(실측).
    설명(docstring)에는 적혀 있어도 **코드가 그 경로를 보면 안 된다**."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "app" / "services" / "resources.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "cuda_present")
    docstring = fn.body[0].value  # 첫 식 = docstring
    strings = [n.value for n in ast.walk(fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str) and n is not docstring]
    assert not any("/proc/driver" in x for x in strings), "컨테이너에서 거짓 양성이 된다"


def test_the_devices_route_comes_before_the_model_id_catch_all():
    """뒤에 있으면 `inference/devices` 가 모델 id 로 먹힌다."""
    from app.routers import models

    paths = [getattr(r, "path", "") for r in models.router.routes
             if "GET" in getattr(r, "methods", set())]
    assert "/api/models/inference/devices" in paths
    assert paths.index("/api/models/inference/devices") < paths.index("/api/models/{model_id:path}")


def test_the_devices_endpoint_reports_cuda(monkeypatch):
    import asyncio

    from app.routers import models
    from app.services import resources

    monkeypatch.setattr(resources, "cuda_present", lambda: False)
    assert asyncio.run(models.inference_devices()) == {"cuda": False}
    monkeypatch.setattr(resources, "cuda_present", lambda: True)
    assert asyncio.run(models.inference_devices()) == {"cuda": True}
