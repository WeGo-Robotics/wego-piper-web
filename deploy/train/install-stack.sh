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
# ⚠ 절차가 Dockerfile.base 와 같은 이유도 같다: lerobot 0.5.0 은 torch<2.11 을
#   요구하지만 추론 기계는 2.11.0 으로 override 돼 있다. lerobot 을 먼저 깔고
#   torch 셋 + nvidia 런타임을 걷어낸 뒤 원하는 CUDA 빌드로 다시 깐다.
#   (`--no-deps` 로 깔면 nvidia-* 런타임이 안 들어와 `import torch` 가 죽는다.)
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

echo "== lerobot[smolvla]==$LEROBOT transformers==$TRANSFORMERS"
pip install "lerobot[smolvla]==$LEROBOT" "transformers==$TRANSFORMERS"

echo "== torch $TORCH / torchvision $TORCHVISION / torchcodec $TORCHCODEC ($TORCH_CUDA)"
pip uninstall -y torch torchvision torchcodec
pip freeze | grep -E '^nvidia-[a-z0-9_-]+-cu1[0-9]' | cut -d= -f1 | xargs -r pip uninstall -y
pip install --index-url "https://download.pytorch.org/whl/$TORCH_CUDA" \
    "torch==$TORCH" "torchvision==$TORCHVISION" "torchcodec==$TORCHCODEC"
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
print("lerobot", lerobot.__version__, "| torch", torch.__version__,
      "| torchvision", torchvision.__version__, "| torchcodec", torchcodec.__version__)
EOF
