"""학습 전용 이미지 (feature/vast-training.md §4) — full·slim 두 변종과 환경 체크.

## 여기서 잠그는 것

1. **핀은 한 곳(install-stack.sh)이고 추론 기계(Dockerfile.base)와 같다** — 학습 기계와
   추론 기계의 lerobot·torch 가 갈리면 체크포인트가 로컬 추론에서 안 열릴 수 있다.
   두 변종이 같은 스크립트를 쓰므로 "포함형"과 "다운로드형"의 환경도 같다.
2. **레이어 순서** — 스택 5GB 가 act_aux·스크립트·버전 ENV 보다 위. 정책 코드 한 줄에
   5GB 를 다시 굽지 않는다 (v0.5.0 의 교훈을 그대로).
3. **회사 코드는 act_aux 뿐** — 로봇·카메라·게이트웨이 코드가 임대 서버에 갈 이유가 없다.
4. **부팅은 멱등** — 두 번 불려도 한 번만 깔고, 끝나면 `.ready` 를 남긴다.
5. **환경 체크는 실제 쓰는 경로에서 회선을 잰다** — PyTorch 인덱스·PyPI·GHCR·HF.
   speedtest 숫자는 그 네 곳과 무관하다. 그리고 torch 없이도 돈다(slim 부팅 전).
6. **push 는 --push 일 때만** — 릴리스 규칙과 같다.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TRAIN = REPO / "deploy" / "train"
DF = TRAIN / "Dockerfile"
STACK = TRAIN / "install-stack.sh"
BOOT = TRAIN / "bootstrap.sh"
CHECK = TRAIN / "env-check.sh"
BUILD = REPO / "deploy" / "build-train.sh"
BASE_DF = REPO / "backend" / "Dockerfile.base"


def _lines(path: Path) -> list[str]:
    """Dockerfile 을 명령 한 줄씩으로 — 주석을 걷고 이어짐을 잇는다."""
    from conftest import code_only

    joined, buf = [], ""
    for l in code_only(path.read_text()).splitlines():
        buf += l.rstrip("\\") if l.rstrip().endswith("\\") else l
        if not l.rstrip().endswith("\\"):
            if buf.strip():
                joined.append(" ".join(buf.split()))
            buf = ""
    return joined


def _pin(src: str, name: str) -> str:
    m = re.search(rf'^{name}="\$\{{{name}:-([^}}]+)\}}"', src, re.M)
    assert m, f"install-stack.sh 에 {name} 핀이 없다"
    return m.group(1)


def test_the_stack_pins_match_the_inference_image():
    """추론 기계와 같은 버전이어야 체크포인트가 그대로 열린다."""
    stack = STACK.read_text()
    base = BASE_DF.read_text()
    for name, pat in (
        ("LEROBOT", r'lerobot\[smolvla\]==([0-9.]+)'),
        ("TRANSFORMERS", r'transformers==([0-9.]+)'),
        ("TORCH", r'\btorch==([0-9.]+)'),
        ("TORCHVISION", r'torchvision==([0-9.]+)'),
        ("TORCHCODEC", r'torchcodec==([0-9.]+)'),
    ):
        want = re.search(pat, base).group(1)
        assert _pin(stack, name) == want, f"{name}: 학습 {_pin(stack, name)} ≠ 추론 {want}"
    # 절차도 같다 — lerobot 먼저, torch 셋과 nvidia 런타임을 걷고, 원하는 CUDA 빌드로 다시
    assert stack.index("pip install \"lerobot[smolvla]") < stack.index("pip uninstall -y torch torchvision torchcodec") \
        < stack.index("download.pytorch.org/whl/$TORCH_CUDA")
    assert "nvidia-[a-z0-9_-]+-cu1" in stack, "옛 nvidia 런타임을 안 걷는다 — import torch 가 죽는다"
    # ⚠ torchcodec 의 CUDA 빌드는 NPP 를 링크하는데 torch 의존성에 NPP 가 없다 — 없으면
    #   `import torchcodec` 이 libnppicc 로 죽어 데이터셋 영상을 못 읽는다 (첫 빌드에서 실제로)
    assert 'pip install "nvidia-npp-' in stack, "NPP 런타임을 안 깐다 — torchcodec 이 안 열린다"
    assert stack.index('nvidia-npp-') > stack.index("download.pytorch.org/whl/$TORCH_CUDA"), \
        "NPP 를 torch 보다 먼저 깔면 torch 재설치 때 걷혀 나간다"
    # 깔아도 로더가 못 찾는다 — LD_LIBRARY_PATH 는 ssh/tmux 셸에 안 따라오므로 ldconfig 로 등록
    assert "ldconfig" in stack and "ld.so.conf.d" in stack, "NPP 디렉토리를 로더에 안 알린다"
    # 주석은 뺀다 — 왜 환경변수를 안 쓰는지 설명하느라 그 이름이 나온다
    from conftest import code_only
    assert "LD_LIBRARY_PATH" not in code_only(stack) and "LD_LIBRARY_PATH" not in code_only(DF.read_text()), \
        "환경변수에 기대면 tmux 세션의 학습이 못 찾는다"


def test_the_two_variants_share_one_install_script():
    """full 은 굽는 동안, slim 은 부팅 때 — **같은 스크립트**다. 두 벌이면 갈린다."""
    lines = _lines(DF)
    stack_run = next(l for l in lines if l.startswith("RUN ") and "install-stack.sh" in l)
    assert "full)" in stack_run and "slim)" in stack_run, "변종 분기가 없다"
    assert "install-stack.sh" in BOOT.read_text(), "부팅 스크립트가 다른 절차로 깐다"
    assert "ARG VARIANT" in DF.read_text()
    # 잘못된 변종은 조용히 full 이나 slim 이 되지 않는다
    assert "exit 1" in stack_run


def test_the_stack_layer_comes_before_everything_that_changes():
    """스택 5GB 위에 act_aux·스크립트·버전 ENV — 순서가 바뀌면 정책 한 줄에 5GB 를 다시 굽는다."""
    lines = _lines(DF)
    i_stack = next(n for n, l in enumerate(lines) if l.startswith("RUN ") and "install-stack.sh" in l)
    i_aux = next(n for n, l in enumerate(lines) if l.startswith("COPY act_aux/"))
    i_scripts = next(n for n, l in enumerate(lines) if l.startswith("COPY ") and "env-check.sh" in l)
    assert i_stack < i_aux < i_scripts, "스택 레이어가 자주 바뀌는 것 뒤에 있다"
    last_run = max(n for n, l in enumerate(lines) if l.startswith("RUN "))
    i_ver = next(n for n, l in enumerate(lines) if l.startswith("ENV PIPER_TRAIN_IMAGE="))
    assert last_run < i_ver, "이미지 태그 ENV 가 RUN 앞에 있다 — 태그마다 전부 다시 굽는다"
    # act_aux 는 --no-deps — 의존성은 full 이면 위에 있고 slim 이면 부팅 때 온다
    aux_run = next(l for l in lines if l.startswith("RUN ") and "/opt/piper/act_aux" in l)
    assert "--no-deps" in aux_run


def test_the_image_carries_only_act_aux():
    """학습은 데이터셋만 본다. 로봇·카메라·게이트웨이 코드가 임대 서버에 갈 이유가 없다."""
    copies = [l for l in _lines(DF) if l.startswith("COPY ")]
    for l in copies:
        src = l.split()[1]
        assert src.startswith(("act_aux/", "deploy/train/")), f"학습 이미지에 실릴 것이 아니다: {l}"
    df = DF.read_text()
    for pkg in ("backend/", "vendor/", "robot/", "cam/", "rs/", "wrapper/", "daemons/"):
        assert f"COPY {pkg}" not in df, f"{pkg} 가 학습 이미지에 들어간다"


def test_the_train_image_shares_the_inference_base_digest():
    """파이썬·glibc 가 같아야 환경 차이를 하나라도 줄인다. 올릴 때는 둘을 같이."""
    want = re.search(r"^FROM (python:[^\s]+@sha256:[0-9a-f]+)", BASE_DF.read_text(), re.M).group(1)
    assert f"FROM {want}" in DF.read_text(), "학습 이미지 베이스가 추론 베이스와 다르다"


def test_the_image_has_what_the_runner_and_the_check_need():
    df = DF.read_text()
    for tool in ("tmux", "ffmpeg", "openssh-server", "curl"):
        assert tool in df, f"{tool} 이 없다"
    # ⚠ lerobot 의존성 evdev 는 py3.13 wheel 이 없는 sdist 다 — gcc 와 linux/input.h 가
    #   없으면 full 은 굽다가, slim 은 부팅 때 죽는다 (첫 빌드에서 실제로 그랬다)
    for pkg in ("build-essential", "linux-libc-dev"):
        assert pkg in df, f"{pkg} 가 없다 — evdev 가 안 구워진다"
    assert "bootstrap.sh" in df and "env-check.sh" in df
    assert 'CMD ["bash", "-c", "/opt/piper/bootstrap.sh' in df, "docker run 으로 띄우면 부팅 스크립트가 안 돈다"


def test_bootstrap_is_idempotent_and_leaves_a_marker():
    src = BOOT.read_text()
    assert "flock" in src, "onstart 와 CMD 가 동시에 부르면 두 번 깐다"
    assert '[ -f "$READY" ]' in src and "exit 0" in src, "이미 깔렸는데 또 깐다"
    assert '> "$READY"' in src, "끝났다는 표시가 없다 — 러너가 언제 시작할지 모른다"
    assert "/opt/piper/.ready" in src and "/opt/piper/.ready" in DF.read_text(), \
        "full 이미지와 부팅 스크립트가 다른 마커를 본다"


def test_env_check_measures_the_paths_that_are_actually_used():
    from conftest import code_only

    src = code_only(CHECK.read_text())
    for host in ("download.pytorch.org", "pypi.org", "ghcr.io", "huggingface.co"):
        assert host in src, f"{host} 회선을 안 잰다"
    assert "speed_download" in src, "curl 이 잰 속도를 안 읽는다"
    assert "compute_cap" in src and "get_arch_list" in src, \
        "GPU 세대와 torch 커널을 대조하지 않는다 — RTX 50 에서 cu126 이 조용히 죽는다"
    assert "torch.cuda.is_available" in src
    assert "ENVCHECK {" in src, "기계가 읽을 한 줄이 없다"
    assert '"recommend"' in src and "full" in src and "slim" in src
    assert "--quick" in src, "회선 측정을 건너뛸 길이 없다"
    # torch 가 없어도 죽지 않는다 — import 는 try 안에서
    assert "except Exception" in src


def test_build_script_pushes_only_when_asked():
    from conftest import code_only

    src = code_only(BUILD.read_text())
    assert "--push" in src and "PUSH=1" in src
    pushes = [l for l in src.splitlines() if "docker push" in l]
    assert pushes, "push 경로가 없다"
    assert "if [ $PUSH = 1 ]" in src, "push 가 플래그 뒤에 있지 않다"
    # 핀은 install-stack.sh 에서 읽는다 — 태그의 lerobot 버전을 여기 또 적지 않는다
    assert "install-stack.sh" in src and "LEROBOT=" in src
    assert "--only" in src and "--cuda" in src
    assert "docker build -f deploy/train/Dockerfile" in src
