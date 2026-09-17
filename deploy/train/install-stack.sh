#!/usr/bin/env bash
# 학습 스택(lerobot + torch) 설치. **핀은 한 곳 — 여기다.**
#
# 두 자리에서 같은 스크립트가 돈다:
#   - full 이미지: 굽는 동안 (deploy/train/Dockerfile 의 RUN)
#   - slim 이미지: 인스턴스 첫 부팅 때 (bootstrap.sh)
# 그래서 "포함형"과 "다운로드형"은 **같은 버전**을 깐다. 회선에 따라 어느 쪽을
# 고르든 학습 결과는 같은 환경에서 나온다.
#
# ⚠ 버전은 backend/Dockerfile.base 와 같아야 한다 — 추론 기계와 학습 기계가
#   갈리면 체크포인트가 로컬 추론에서 안 열릴 수 있다 (feature/cloud-training §4).
#   `test_train_image.py` 가 두 파일의 핀을 대조한다.
#
# ⚠ lerobot 0.5.0 은 `torch<2.11.0` 을 요구하지만 추론 기계는 2.11.0 이다 — 체크포인트가
#   거기서 열려야 하므로 그 핀은 **의도적으로** 넘긴다.
#
#   예전에는 lerobot 을 먼저 깔고(그때 PyPI 기본 빌드 torch 가 딸려 온다) 그걸 지운 뒤
#   원하는 CUDA 빌드로 다시 깔았다 — **torch 를 두 번 받았다.** 지금은 순서를 뒤집어
#   torch 를 먼저 한 번만 받고, lerobot 은 휠만 받은 뒤 그 의존성을 메타데이터에서
#   뽑아 torch 셋만 빼고 깐다. 제약 파일이 torch 를 못 내리게 막는다.
#
# CUDA 빌드는 TORCH_CUDA 로 고른다 (기본 cu126 — 호스트 드라이버 12.6 이상이면 된다).
#   RTX 50 계열(sm_120)은 cu128 이상이 필요하다 — env-check.sh 가 GPU 를 보고 말해 준다.
set -euo pipefail

LEROBOT="${LEROBOT:-0.5.0}"
TRANSFORMERS="${TRANSFORMERS:-5.3.0}"
TORCH="${TORCH:-2.11.0}"
TORCHVISION="${TORCHVISION:-0.26.0}"
TORCHCODEC="${TORCHCODEC:-0.11.0}"
TORCH_CUDA="${TORCH_CUDA:-cu126}"

export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
export TORCH   # 아래 확인 스크립트가 버전을 단언할 때 쓴다

TORCH_INDEX="https://download.pytorch.org/whl/$TORCH_CUDA"

# ── 1. torch 셋을 **먼저**, 원하는 CUDA 빌드로 ──
#
# ⚠ 예전에는 lerobot 을 먼저 깔고(그때 PyPI 기본 빌드 torch 가 딸려 온다) 그걸 지운 뒤
#   다시 깔았다. 즉 **torch 를 두 번 받았다** — 실측으로 설치본이 torch 1.7GB +
#   nvidia 런타임 4.0GB 다. 임대 인스턴스는 부팅마다 그걸 치렀다(실측 201초).
#
# ⚠ 의존성을 빼지 않는다(`--no-deps` 금지). nvidia 런타임이 torch 의 의존성으로 오고,
#   빠지면 `import torch` 가 죽는다.
echo "== torch $TORCH / torchvision $TORCHVISION / torchcodec $TORCHCODEC ($TORCH_CUDA)"
pip install --index-url "$TORCH_INDEX" \
    "torch==$TORCH" "torchvision==$TORCHVISION" "torchcodec==$TORCHCODEC"

# ── 2. lerobot 은 휠만 — 의존성은 3에서 우리가 고른다 ──
#
# ⚠ `--no-deps` 인 이유는 lerobot 0.5.0 의 핀이 `torch<2.11.0` 이기 때문이다. 그대로
#   두면 pip 이 방금 깐 2.11.0 을 **끌어내린다.** 우리가 2.11.0 을 쓰는 이유는 추론
#   기계(`backend/Dockerfile.base`)와 같은 버전이어야 체크포인트가 거기서 열리기
#   때문이다 — 그래서 이 핀은 **의도적으로** 넘긴다.
echo "== lerobot[smolvla]==$LEROBOT (휠만)"
pip install --no-deps "lerobot[smolvla]==$LEROBOT"

# ── 3. lerobot 의 의존성을 torch 셋만 빼고 깐다 ──
#
# ⚠ 목록을 손으로 적지 않는다. lerobot 0.5.0 의 요구사항은 139줄이고, 버전을 올릴 때마다
#   사람이 따라 적으면 반드시 어긋난다. **메타데이터에서 뽑는다.**
pip install -q "packaging"
python - > /tmp/piper-deps.txt <<'PYEOF'
from importlib.metadata import requires
from packaging.requirements import Requirement

WANT_EXTRAS = {"smolvla"}        # 우리가 쓰는 extra
SKIP = {"torch", "torchvision", "torchcodec"}   # 1에서 이미 깔았다

for raw in requires("lerobot") or []:
    r = Requirement(raw)
    if r.name.lower().replace("_", "-") in SKIP:
        continue
    # 기본 의존성 + 우리가 쓰는 extra 만. 다른 extra 는 False 로 떨어진다.
    if r.marker is not None and not any(
            r.marker.evaluate({"extra": e}) for e in (WANT_EXTRAS | {""})):
        continue
    print(str(r))
PYEOF
echo "transformers==$TRANSFORMERS" >> /tmp/piper-deps.txt
echo "== 의존성 $(wc -l < /tmp/piper-deps.txt)줄 (torch 셋 제외)"

# ⚠ **제약으로 torch 를 못 내리게 막는다.** 139개의 이행 의존성 중 누군가 `torch<2.11`
#   을 끌면 pip 은 조용히 downgrade 한다 — 그러면 우리가 방금 받은 것을 또 버리는
#   셈이고, 더 나쁘게는 추론 기계와 버전이 갈린다. 제약이 있으면 조용히 내려가는 대신
#   **여기서 실패한다.**
cat > /tmp/piper-constraints.txt <<EOF
torch==$TORCH
torchvision==$TORCHVISION
torchcodec==$TORCHCODEC
EOF
PIP_CONSTRAINT=/tmp/piper-constraints.txt \
    pip install --extra-index-url "$TORCH_INDEX" -r /tmp/piper-deps.txt

# ⚠ 여기서 pip 이 이런 줄을 찍는다 — **의도한 것이다.**
#     ERROR: ... lerobot 0.5.0 requires torch<2.11.0 ... but you have torch 2.11.0+cu126
#   "ERROR" 로 시작해서 실패처럼 보이지만 pip 은 0 으로 끝나고, 진짜 판정은 아래 확인이다.
#   로그를 읽는 사람이 여기서 멈추지 않도록 바로 옆에 적어 둔다.
echo "== ↑ torch 핀 경고는 의도한 것이다 (추론 기계와 버전을 맞추려고 넘겼다). 아래 확인이 판정이다."

# ⚠ torchcodec 의 CUDA 빌드는 NPP(libnppicc)를 링크하는데 torch wheel 의 의존성에는 NPP 가
#   없다 — 없으면 `import torchcodec` 이 "libnppicc.so.12: cannot open" 으로 죽고, 그러면
#   lerobot 이 데이터셋 영상을 한 프레임도 못 읽는다. 실제로 첫 빌드가 여기서 죽었다.
#   (추론 기계의 베이스 이미지도 같은 증상이다 — 별도 수정 대상.)
pip install "nvidia-npp-${TORCH_CUDA:0:4}"
# ⚠ 깔아도 못 찾는다 — pip 의 nvidia 패키지는 `site-packages/nvidia/npp/lib` 에 .so 를 두는데,
#   torch 는 자기가 아는 라이브러리(cublas·cudnn…)만 미리 올리고 NPP 는 모른다. 동적 로더에
#   그 디렉토리를 알려야 한다. LD_LIBRARY_PATH 는 ssh·tmux 로 들어온 셸에 안 따라오므로
#   ldconfig 로 시스템에 등록한다 (이 스크립트는 root 로 돈다 — 이미지 빌드도, Vast 인스턴스도).
NPP_LIB="$(python -c 'import sysconfig, os; print(os.path.join(sysconfig.get_paths()["purelib"], "nvidia", "npp", "lib"))')"
if [ -d "$NPP_LIB" ]; then
    echo "$NPP_LIB" > /etc/ld.so.conf.d/piper-npp.conf && ldconfig
    ldconfig -p | grep -q libnppicc || { echo "✗ libnppicc 를 ldconfig 가 못 본다: $NPP_LIB" >&2; exit 1; }
else
    echo "✗ NPP 라이브러리 디렉토리가 없다: $NPP_LIB" >&2; exit 1
fi

# 확인 — 여기서 못 열면 학습도 못 연다. 조용히 넘어가지 않는다.
python - <<'EOF'
import lerobot, torch, torchvision, torchcodec

# ⚠ **학습 진입점까지 열어 본다.** 네 개를 import 하는 것과 `lerobot-train` 이 도는 것은
#   다르다 — 의존성 하나가 빠지면 여기서는 멀쩡하고, 기계를 빌린 뒤에 죽는다.
import lerobot.scripts.lerobot_train  # noqa: F401

# ⚠ 버전을 **단언**한다. 이행 의존성이 torch 를 끌어내리면 위의 제약이 막지만,
#   막지 못한 경로가 있더라도 여기서 걸린다 — 추론 기계와 갈리면 체크포인트가 안 열린다.
import os
want = os.environ.get("TORCH", "2.11.0")
assert torch.__version__.split("+")[0] == want, f"torch {torch.__version__} ≠ {want}"

print("lerobot", lerobot.__version__, "| torch", torch.__version__,
      "| torchvision", torchvision.__version__, "| torchcodec", torchcodec.__version__)
EOF
