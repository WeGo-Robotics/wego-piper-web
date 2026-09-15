#!/usr/bin/env bash
# 학습 기계 환경 체크 — 임대 인스턴스(또는 아무 SSH 박스) 안에서 돌린다.
#
#   GPU · 드라이버 · 디스크 · 도구 · 학습 스택 · **외부 회선** 을 재고, 그 회선에서
#   full(스택 포함) 이미지와 slim(부팅 때 설치) 이미지 중 어느 쪽이 빠를지 말해 준다.
#
# 사용:
#   /opt/piper/env-check.sh                       # 이미지 안 (full·slim 둘 다 들어 있다)
#   curl -fsSL https://raw.githubusercontent.com/WeGo-Robotics/wego-piper-web/master/deploy/train/env-check.sh | bash
#                                                 # 아무 박스 — torch 없어도 된다
#   env-check.sh --quick                          # 회선 측정 생략
#
# 마지막 줄은 기계가 읽는 JSON 이다:  ENVCHECK {...}
# (W2 의 프로바이더 층이 이 줄을 파싱한다 — 사람용 출력은 위에 따로 있다.)
#
# ⚠ 회선 측정은 **실제 쓰는 경로**에서 잰다: PyTorch 인덱스(slim 의 torch), PyPI(slim 의
#   나머지), GHCR(full 의 이미지 pull), HF(데이터셋·체크포인트). 100MB 또는 20초.
#   speedtest 류의 숫자는 이 네 곳과 무관하다.
set -uo pipefail

QUICK=0
# 실측 2026-09-14 (0.5.0-cu126): full 이미지는 압축 4.47GB(풀면 4.7GB). slim 부팅이 받는 wheel 은
# 8.2GB — lerobot 이 torch 2.10 과 그 CUDA 런타임(3.5GB)을 먼저 끌어왔다가 걷히고 추론 기계와
# 같은 2.11 을 다시 받는다(install-stack.sh 의 절차). 사무실 회선에서 slim 첫 부팅 317초.
FULL_MB="${PIPER_TRAIN_FULL_MB:-4500}"   # full 이미지 pull 량(압축)
SLIM_MB="${PIPER_TRAIN_SLIM_MB:-8200}"   # slim 이 부팅 때 받는 wheel 량
SLIM_INSTALL_S="${PIPER_TRAIN_SLIM_INSTALL_S:-90}"   # 받은 뒤 푸는·걷는 시간
for a in "$@"; do case "$a" in --quick) QUICK=1 ;; esac; done

ok()   { printf '  ✓ %s\n' "$*"; }
bad()  { printf '  ✗ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
num()  { printf '%s' "${1:-0}" | sed 's/[^0-9.]//g'; }

J_GPU="" J_DRIVER="" J_CUDA_MAX="" J_CC="" J_VRAM=0 J_DISK=0 J_SHM=0 J_RAM=0 J_CPU=0
J_READY=false J_TORCH="" J_TORCH_CUDA="" J_CUDA_OK=false J_ARCH_OK=false J_LEROBOT="" J_ACT_AUX=false
J_MB_PT=0 J_MB_PYPI=0 J_MB_GHCR=0 J_MB_HF=0 J_ETA_FULL=0 J_ETA_SLIM=0 J_REC="unknown"
NOTES=()

echo "== 기계 =="
if have nvidia-smi; then
    line="$(nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv,noheader 2>/dev/null | head -1)"
    if [ -n "$line" ]; then
        J_GPU="$(echo "$line" | cut -d, -f1 | sed 's/^ *//')"
        J_DRIVER="$(echo "$line" | cut -d, -f2 | tr -d ' ')"
        J_VRAM="$(num "$(echo "$line" | cut -d, -f3)")"
        J_CC="$(echo "$line" | cut -d, -f4 | tr -d ' ')"
        J_CUDA_MAX="$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9.]+' | head -1 | cut -d' ' -f3)"
        ok "GPU $J_GPU · VRAM ${J_VRAM}MiB · sm_${J_CC/./} · 드라이버 $J_DRIVER (CUDA 최대 ${J_CUDA_MAX:-?})"
        n="$(nvidia-smi -L 2>/dev/null | wc -l)"; [ "$n" -gt 1 ] && warn "GPU 가 ${n}장 — lerobot-train 은 1장만 쓴다"
    else
        bad "nvidia-smi 는 있는데 GPU 를 못 읽는다"; NOTES+=("gpu_unreadable")
    fi
else
    bad "nvidia-smi 없음 — GPU 를 못 본다"; NOTES+=("no_nvidia_smi")
fi
J_CPU="$(nproc 2>/dev/null || echo 0)"
J_RAM="$(awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)"
J_SHM="$(df -m /dev/shm 2>/dev/null | awk 'NR==2{print $2}')"; J_SHM="${J_SHM:-0}"
J_DISK="$(df -BG "${PWD:-/}" 2>/dev/null | awk 'NR==2{gsub("G","",$4); print $4}')"; J_DISK="${J_DISK:-0}"
ok "CPU ${J_CPU}코어 · RAM ${J_RAM}GB · /dev/shm ${J_SHM}MB · 디스크 남음 ${J_DISK}GB ($PWD)"
[ "$J_SHM" -lt 1024 ] 2>/dev/null && warn "/dev/shm 이 1GB 미만 — DataLoader worker 가 'bus error' 로 죽을 수 있다 (--shm-size)"
[ "$J_DISK" -lt 20 ] 2>/dev/null && { warn "디스크 20GB 미만 — 데이터셋 + 체크포인트 자리가 모자랄 수 있다"; NOTES+=("low_disk"); }

echo "== 도구 =="
for t in tmux ffmpeg git python3 curl; do
    if have "$t"; then
        vflag="--version"; [ "$t" = tmux ] && vflag="-V"   # tmux 만 -V 다
        ok "$t $("$t" "$vflag" 2>&1 | head -1 | grep -oE '[0-9]+(\.[0-9]+)+[a-z]?' | head -1)"
    else bad "$t 없음"; NOTES+=("no_$t"); fi
done

echo "== 학습 스택 =="
if [ -f /opt/piper/.ready ]; then J_READY=true; ok "준비됨: $(cat /opt/piper/.ready)"
elif [ -f /opt/piper/bootstrap.log ]; then warn "설치 중이거나 실패 — /opt/piper/bootstrap.log 확인"
else warn "스택 없음 (slim 부팅 전이거나 이 이미지가 아니다)"; fi
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || true)"
if [ -n "$PY" ]; then
    probe="$("$PY" - <<'EOF' 2>/dev/null
import json
out = {}
try:
    import torch
    out["torch"] = torch.__version__
    out["torch_cuda"] = torch.version.cuda or ""
    out["cuda_ok"] = bool(torch.cuda.is_available())
    out["arch"] = list(torch.cuda.get_arch_list()) if out["cuda_ok"] else []
    if out["cuda_ok"]:
        cc = torch.cuda.get_device_capability(0)
        out["cc"] = f"{cc[0]}.{cc[1]}"
        out["arch_ok"] = f"sm_{cc[0]}{cc[1]}" in out["arch"]
        if out["arch_ok"]:
            # 커널이 실제로 도는지 — "no kernel image" 는 여기서 난다
            (torch.ones(64, 64, device="cuda") @ torch.ones(64, 64, device="cuda")).sum().item()
            out["matmul"] = True
except Exception as e:
    out.setdefault("error", str(e)[:200])
try:
    import lerobot; out["lerobot"] = lerobot.__version__
except Exception: pass
try:
    import lerobot_policy_act_aux; out["act_aux"] = True
except Exception: out["act_aux"] = False
print(json.dumps(out))
EOF
)"
    if [ -n "$probe" ]; then
        J_TORCH="$(echo "$probe" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("torch",""))')"
        J_TORCH_CUDA="$(echo "$probe" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("torch_cuda",""))')"
        J_CUDA_OK="$(echo "$probe" | "$PY" -c 'import json,sys; print("true" if json.load(sys.stdin).get("cuda_ok") else "false")')"
        J_ARCH_OK="$(echo "$probe" | "$PY" -c 'import json,sys; print("true" if json.load(sys.stdin).get("matmul") else "false")')"
        J_LEROBOT="$(echo "$probe" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("lerobot",""))')"
        J_ACT_AUX="$(echo "$probe" | "$PY" -c 'import json,sys; print("true" if json.load(sys.stdin).get("act_aux") else "false")')"
        err="$(echo "$probe" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("error",""))')"
        if [ -n "$J_TORCH" ]; then
            if [ "$J_CUDA_OK" = true ] && [ "$J_ARCH_OK" = true ]; then
                ok "torch $J_TORCH (CUDA $J_TORCH_CUDA) — GPU 커널 동작 확인"
            elif [ "$J_CUDA_OK" = true ]; then
                bad "torch $J_TORCH (CUDA $J_TORCH_CUDA) 는 이 GPU(sm_${J_CC/./}) 커널이 없다 — 다른 CUDA 빌드(예: cu128)로 구운 이미지가 필요하다"
                NOTES+=("arch_mismatch")
            else
                bad "torch $J_TORCH 가 GPU 를 못 쓴다 (${err:-드라이버/CUDA 불일치}) — 드라이버 CUDA ${J_CUDA_MAX:-?} vs torch CUDA ${J_TORCH_CUDA:-?}"
                NOTES+=("cuda_unavailable")
            fi
        else
            warn "torch 없음${err:+ ($err)}"
        fi
        [ -n "$J_LEROBOT" ] && ok "lerobot $J_LEROBOT" || warn "lerobot 없음"
        [ "$J_ACT_AUX" = true ] && ok "lerobot_policy_act_aux import 됨" || warn "act_aux 없음 — --policy.type=act_aux 는 못 쓴다"
    fi
else
    bad "python 없음"
fi
have lerobot-train && ok "lerobot-train 있음" || warn "lerobot-train 없음 (스택 설치 전이면 정상)"

# ── 회선 ──
speed_mb() {  # url [bearer] → MB/s (첫 100MB 또는 20초)
    local url="$1" hdr=()
    [ -n "${2:-}" ] && hdr=(-H "Authorization: Bearer $2")
    local bps
    bps="$(curl -sL --max-time 20 -r 0-104857599 "${hdr[@]}" -o /dev/null -w '%{speed_download}' "$url" 2>/dev/null || true)"
    awk -v b="${bps:-0}" 'BEGIN{printf "%.1f", b/1048576}'
}
ghcr_blob() {  # 공개 이미지의 가장 큰 레이어 → "url token"
    local repo="wego-robotics/piper-web-backend" tok
    tok="$(curl -s "https://ghcr.io/token?scope=repository:${repo}:pull&service=ghcr.io" \
           | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)"
    [ -z "$tok" ] && return 1
    local acc='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
    local digest
    digest="$(curl -s -H "Authorization: Bearer $tok" -H "Accept: $acc" "https://ghcr.io/v2/${repo}/manifests/latest" \
      | "$PY" -c '
import json, sys, urllib.request
d = json.load(sys.stdin); repo = sys.argv[1]; tok = sys.argv[2]
if "manifests" in d:  # index → linux/amd64 매니페스트
    m = next((m for m in d["manifests"] if m.get("platform", {}).get("os") == "linux"), d["manifests"][0])
    # ⚠ 이 파이썬은 bash 작은따옴표 안에 있다 — 작은따옴표를 쓰면 거기서 문자열이 끝난다
    req = urllib.request.Request("https://ghcr.io/v2/" + repo + "/manifests/" + m["digest"],
        headers={"Authorization": "Bearer " + tok, "Accept": m["mediaType"]})
    d = json.load(urllib.request.urlopen(req, timeout=15))
big = max(d["layers"], key=lambda l: l["size"])
print(big["digest"])' "$repo" "$tok" 2>/dev/null)"
    [ -z "$digest" ] && return 1
    echo "https://ghcr.io/v2/${repo}/blobs/${digest} $tok"
}
pypi_url() {  # transformers 5.3.0 wheel — slim 이 실제로 받는 것 중 큰 것
    curl -s https://pypi.org/pypi/transformers/5.3.0/json \
      | "$PY" -c 'import json,sys; u=[x for x in json.load(sys.stdin)["urls"] if x["packagetype"]=="bdist_wheel"]; print(u[0]["url"] if u else "")' 2>/dev/null
}

if [ "$QUICK" = 0 ] && [ -n "$PY" ]; then
    echo "== 외부 회선 (100MB 또는 20초씩) =="
    J_MB_PT="$(speed_mb 'https://download.pytorch.org/whl/cu126/torch-2.11.0%2Bcu126-cp313-cp313-manylinux_2_28_x86_64.whl')"
    printf '  PyTorch 인덱스  %6s MB/s   (slim: torch wheel)\n' "$J_MB_PT"
    u="$(pypi_url)"; [ -n "$u" ] && J_MB_PYPI="$(speed_mb "$u")"
    printf '  PyPI            %6s MB/s   (slim: 나머지 wheel)\n' "$J_MB_PYPI"
    if gb="$(ghcr_blob)"; then J_MB_GHCR="$(speed_mb ${gb})"; fi
    printf '  GHCR            %6s MB/s   (full: 이미지 pull)\n' "$J_MB_GHCR"
    J_MB_HF="$(speed_mb 'https://huggingface.co/lerobot/act_aloha_sim_transfer_cube_human/resolve/main/model.safetensors')"
    printf '  HuggingFace     %6s MB/s   (데이터셋 받기 · 체크포인트 올리기의 상대)\n' "$J_MB_HF"

    echo "== 추천 =="
    J_ETA_FULL="$(awk -v mb="$FULL_MB" -v s="$J_MB_GHCR" 'BEGIN{ if (s+0 <= 0) print 0; else printf "%d", mb/s }')"
    slim_dl="$(awk -v mb="$SLIM_MB" -v a="$J_MB_PT" -v b="$J_MB_PYPI" 'BEGIN{ s=(a+0<b+0||b+0<=0)?a:b; if (s+0<=0) print 0; else printf "%d", mb/s }')"
    J_ETA_SLIM="$(( slim_dl > 0 ? slim_dl + SLIM_INSTALL_S : 0 ))"
    [ "$J_ETA_FULL" -gt 0 ] && printf '  full  ≈ %d분 (%sMB / %s MB/s)\n' "$((J_ETA_FULL/60))" "$FULL_MB" "$J_MB_GHCR" \
                             || warn "GHCR 를 못 쟀다 — full 이미지 pull 시간을 모른다"
    [ "$J_ETA_SLIM" -gt 0 ] && printf '  slim  ≈ %d분 (%sMB 받기 + 설치 %d초)\n' "$((J_ETA_SLIM/60))" "$SLIM_MB" "$SLIM_INSTALL_S" \
                             || warn "PyTorch/PyPI 를 못 쟀다 — slim 설치 시간을 모른다"
    if [ "$J_ETA_FULL" -gt 0 ] && [ "$J_ETA_SLIM" -gt 0 ]; then
        if [ "$J_ETA_FULL" -le "$J_ETA_SLIM" ]; then J_REC=full; else J_REC=slim; fi
        printf '  → %s 이미지 (추정치 — 압축 크기·설치 시간은 실측 후 갱신)\n' "$J_REC"
    elif [ "$J_ETA_SLIM" -gt 0 ]; then
        J_REC=slim; printf '  → slim 이미지 (GHCR 가 막혀 있으면 full 은 애초에 못 받는다)\n'
    elif [ "$J_ETA_FULL" -gt 0 ]; then
        J_REC=full; printf '  → full 이미지 (PyPI 가 막혀 있으면 slim 은 부팅 때 죽는다)\n'
    else
        bad "회선을 하나도 못 쟀다 — 바깥으로 안 나가거나 URL 이 막혔다"; NOTES+=("no_network")
    fi
    awk -v s="$J_MB_HF" 'BEGIN{ if (s+0 > 0 && s+0 < 10) print "  ⚠ HF 회선이 10MB/s 미만 — 데이터셋 수십 GB 면 학습보다 받기가 오래 걸린다" }'
fi

# 기계용 한 줄
# ⚠ pipefail 아래서 `grep -v` 가 빈 결과로 1 을 돌려주면 `|| echo` 까지 같이 찍혀 JSON 이
#   깨진다 — 빈 줄 거르기는 파이썬이 한다
notes_json="$(printf '%s\n' "${NOTES[@]}" | "$PY" -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))' 2>/dev/null)"
notes_json="${notes_json:-[]}"
printf 'ENVCHECK {"gpu":"%s","driver":"%s","cuda_max":"%s","compute_cap":"%s","vram_mib":%s,"cpu":%s,"ram_gb":%s,"shm_mb":%s,"disk_free_gb":%s,"stack_ready":%s,"torch":"%s","torch_cuda":"%s","cuda_ok":%s,"arch_ok":%s,"lerobot":"%s","act_aux":%s,"mbps":{"pytorch":%s,"pypi":%s,"ghcr":%s,"hf":%s},"eta_s":{"full":%s,"slim":%s},"recommend":"%s","notes":%s}\n' \
    "$J_GPU" "$J_DRIVER" "$J_CUDA_MAX" "$J_CC" "${J_VRAM:-0}" "$J_CPU" "$J_RAM" "$J_SHM" "$J_DISK" "$J_READY" \
    "$J_TORCH" "$J_TORCH_CUDA" "$J_CUDA_OK" "$J_ARCH_OK" "$J_LEROBOT" "$J_ACT_AUX" \
    "$J_MB_PT" "$J_MB_PYPI" "$J_MB_GHCR" "$J_MB_HF" "$J_ETA_FULL" "$J_ETA_SLIM" "$J_REC" "$notes_json"
