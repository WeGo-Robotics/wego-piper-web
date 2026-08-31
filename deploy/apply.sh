#!/usr/bin/env bash
# 번들을 호스트에 적용한다. **첫 설치와 업데이트가 같은 명령이다.**
#
# ## 왜 하나인가
#
# 절차가 갈리면 "업데이트인 줄 알았는데 첫 설치였다"가 생긴다 — 그때 빠뜨리는
# 것은 늘 sudo 가 필요한 쪽(redis 소켓·linger·udev)이고, 증상은 "웹은 뜨는데
# 카메라도 팔도 안 보인다"라 원인을 찾기 어렵다.
#
# 그래서 **없는 것만 한다.** 이미 돼 있으면 건너뛴다. 두 번 돌려도 같다.
#
# 사용:  ./apply.sh [--check]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
say()  { printf "\n\033[1m%s\033[0m\n" "$1"; }

[ -f "$HERE/manifest.txt" ] || { echo "manifest.txt 가 없습니다 — 번들 안에서 실행하세요"; exit 1; }
# shellcheck disable=SC1090
source "$HERE/manifest.txt"
echo "piper-web $version  (직전 $prev, 빌드 $built_at)"

# ⚠ **매니페스트에 박힌 주소는 늙는다.** 빌드 머신이 DHCP·WiFi 면 IP 가 바뀌고,
#   그러면 이미 만든 번들의 `registry=` 가 죽은 주소를 가리킨다. 번들을 다시
#   굽지 않고 넘어갈 수 있어야 한다:
#       PIPER_REGISTRY=새주소:5000 ./apply.sh
if [ -n "${PIPER_REGISTRY:-}" ] && [ "${PIPER_REGISTRY}" != "${registry:-}" ]; then
  echo "  레지스트리 주소를 덮어씁니다: ${registry:-<없음>} → $PIPER_REGISTRY"
  registry="$PIPER_REGISTRY"
fi

VENV="$HOME/.venvs/piper-daemons"
DATA="${PIPER_DATA_ROOT:-/srv/piper-data}"

# ── 0. 호스트 전제 — sudo 가 필요한 것은 **찍어만 준다** ──────────────────
say "0. 호스트 전제"
NEED_SUDO=()
NEED_APT=()

# ⚠ **가장 큰 전제가 안 걸리고 있었다.** docker 가 없어도 이 절을 통과해 버려서
#   한참 뒤 `docker load` 가 `command not found` 로 깨졌다 — "전제를 찍어 주고
#   멈춘다"는 이 스크립트의 설계가 정작 docker·compose·venv 에서는 안 돌았다.
#   여기서도 **설치는 안 한다.** 무엇을 깔아야 하는지만 말하고 멈춘다.
command -v docker >/dev/null && ok "docker" || { bad "docker 없음"; NEED_APT+=(docker.io); }
# ⚠ **v2 여야 한다.** compose 파일이 `deploy.resources.reservations.devices` 로 GPU 를
#   잡는데 v1(`docker-compose`)은 그 키를 모른다.
docker compose version >/dev/null 2>&1 && ok "docker compose v2" \
  || { bad "docker compose v2 없음"; NEED_APT+=(docker-compose-v2); }
# ⚠ 데몬에 못 붙는 원인은 둘인데 **처방이 다르다** — 안 돌거나, 그룹이 아니거나.
if command -v docker >/dev/null; then
  if docker info >/dev/null 2>&1; then
    ok "docker 데몬"
  elif ! systemctl is-active --quiet docker 2>/dev/null; then
    bad "docker 데몬이 안 돎"
    NEED_SUDO+=("systemctl enable --now docker")
  else
    bad "docker 데몬 접근 권한 없음 — 그룹은 **다시 로그인해야** 반영된다"
    NEED_SUDO+=("usermod -aG docker $USER")
  fi
fi
# ⚠ `venv` 는 파이썬에 딸려오지 않는다 — 데비안 계열은 `python3-venv` 가 따로다.
#   없으면 아래 2절의 `python3 -m venv` 가 깨진다.
python3 -c "import venv" >/dev/null 2>&1 && ok "python3 venv" \
  || { bad "python3-venv 없음"; NEED_APT+=(python3-venv); }
command -v redis-server >/dev/null && ok "redis-server" \
  || { bad "redis-server 없음"; NEED_APT+=(redis-server); }
# ⚠ **Ubuntu 아카이브에 없다** — NVIDIA 저장소를 먼저 붙여야 한다. 그래서 위
#   `apt install` 한 줄에 같이 넣지 않는다: 넣으면 "패키지를 찾을 수 없음" 으로
#   **그 줄 전체가 실패해** 나머지도 안 깔린다.
# ⚠ **이미지에 박힌 torch 가 지원하는 GPU 는 정해져 있다.** cu130 빌드의
#   `get_arch_list()` 는 sm_75·80·86·90·100·120 뿐이고 **PTX 가 안 들어 있어**
#   JIT 으로도 못 메꾼다 — Pascal(6.x)·Volta(7.0)는 드라이버를 아무리 올려도
#   안 돈다. 이 값이 바뀌면 컨테이너에 직접 물어서 고친다:
#     docker run --rm --gpus all --entrypoint python piper-web-backend:latest \
#       -c 'import torch; print(torch.cuda.get_arch_list())'
MIN_CC=7.5      # Turing. RTX 20xx·T4 부터
MIN_CUDA=13.0   # 이미지가 실은 CUDA 런타임 (nvidia-cuda-runtime 13.0.96)
# "$1 >= $2" 인가
vge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }
if command -v nvidia-smi >/dev/null 2>&1; then
  command -v nvidia-ctk >/dev/null && ok "nvidia-container-toolkit" || {
    bad "nvidia-container-toolkit 없음 — compose 의 GPU 예약이 실패한다"
    warn "  NVIDIA 저장소부터: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
  }
  # ⚠ **둘은 처방이 다르다.** 드라이버가 낮은 건 `apt install` 한 줄로 고치지만,
  #   컴퓨트 능력이 낮은 건 **GPU 를 바꿔야 한다.** 같은 ✗ 로 뭉뚱그리면 현장에서
  #   드라이버만 올려보다 시간을 버린다.
  GPUS="$(nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader 2>/dev/null || true)"
  if [ -z "$GPUS" ]; then
    warn "GPU 컴퓨트 능력을 못 읽었다 — nvidia-smi 가 낡았을 수 있다(525 미만)"
  else
    while IFS=, read -r gname gcc; do
      gname="$(echo "$gname" | xargs)"; gcc="$(echo "$gcc" | xargs)"
      [ -n "$gcc" ] || continue
      vge "$gcc" "$MIN_CC" && ok "GPU $gname (컴퓨트 $gcc)" \
        || bad "GPU $gname 컴퓨트 $gcc — 이 이미지는 $MIN_CC 이상만 돈다. **GPU 를 바꿔야 한다**"
    done <<< "$GPUS"
  fi
  # 드라이버가 지원하는 CUDA. 이미지의 런타임보다 낮으면 GPU 가 무엇이든 안 돈다.
  DRV_CUDA="$(nvidia-smi -q 2>/dev/null | sed -n 's/.*CUDA Version *: *\([0-9.]*\).*/\1/p' | head -1)"
  if [ -z "$DRV_CUDA" ]; then
    warn "드라이버 CUDA 버전을 못 읽었다"
  elif vge "$DRV_CUDA" "$MIN_CUDA"; then
    ok "드라이버 CUDA $DRV_CUDA"
  else
    bad "드라이버 CUDA $DRV_CUDA — 이 이미지는 $MIN_CUDA 이상이 필요하다"
    NEED_APT+=(nvidia-driver-580)
    warn "  드라이버를 올리면 **재부팅해야** 반영된다"
  fi
else
  warn "NVIDIA GPU 가 안 보인다 — 학습·추론은 못 돈다"
fi

# ⚠ 평문 레지스트리는 **daemon.json 에 적어야만** 붙는다. 안 적으면 pull 이
#   "server gave HTTP response to HTTPS client" 로 죽는데, 그 메시지만 보고
#   무엇을 고쳐야 하는지 알기 어렵다. 도커는 `127.0.0.0/8` 만 기본으로 믿는다.
if [ -n "${registry:-}" ]; then
  # ⚠ **`wego/...` 같은 값은 주소가 아니라 도커 허브 네임스페이스다.** HTTPS 로
  #   가므로 insecure 설정과 무관하다. 포트가 없으면 그 경우로 본다 — 이걸 안
  #   가르면 공개 배포에서 매번 거짓 경보가 난다.
  if [[ "$registry" != *:* ]]; then
    ok "공개 레지스트리 ($registry)"
  # 도커는 루프백을 기본으로 믿는다 — 그건 확인할 것이 없다
  elif [[ "$registry" == localhost:* || "$registry" == 127.* ]]; then
    ok "레지스트리 $registry (루프백 — 기본 신뢰)"
  elif docker info 2>/dev/null | grep -qF " $registry"; then
    ok "레지스트리 신뢰됨 ($registry)"
  else
    bad "레지스트리 $registry 가 insecure-registries 에 없다 — pull 이 실패한다"
    echo "       /etc/docker/daemon.json 에 넣고 도커를 재시작하세요:"
    echo "         {\"insecure-registries\": [\"$registry\"]}"
    NEED_SUDO+=("systemctl restart docker   # daemon.json 을 고친 뒤")
  fi
  if [[ "$registry" != *:* ]]; then
    :   # 공개 레지스트리 — 살아 있는지는 pull 이 말해 준다
  elif curl -fsS --max-time 5 "http://$registry/v2/" >/dev/null 2>&1; then
    ok "레지스트리 응답 ($registry)"
  else
    bad "레지스트리 $registry 가 응답하지 않는다"
    echo "       빌드 머신에서 레지스트리가 도는지:  ./deploy/registry.sh --status"
    echo "       빌드 머신 주소가 바뀌었다면 (DHCP·WiFi 면 바뀐다):"
    echo "         이 호스트의 /etc/hosts 에서 그 이름의 IP 를 고치거나,"
    echo "         한 번만 넘어가려면:  PIPER_REGISTRY=<새주소>:5000 ./apply.sh"
  fi
fi

[ -d "$DATA" ] && ok "데이터 루트 $DATA" || { bad "$DATA 없음"; NEED_SUDO+=("mkdir -p $DATA && chown $USER $DATA"); }
# ⚠ 컨테이너는 유닉스 소켓으로만 버스에 붙는다. 설정만 하고 redis 를 재시작 안 하면
#   소켓 파일이 없어 backend 가 "E-stop 버스에 연결할 수 없습니다" 로 뜬다(실제 사고).
# ⚠ redis 가 **아직 안 깔렸으면 건너뛴다.** 없는 `/etc/redis/redis.conf` 에 sed 를
#   걸라고 시키면 그 명령이 실패하고, 사람은 그게 왜 실패했는지 모른다.
if command -v redis-server >/dev/null; then
  [ -S /run/redis/redis-server.sock ] && ok "redis 유닉스 소켓" || {
    bad "redis 소켓 없음"
    NEED_SUDO+=("sed -i 's|^# *unixsocket |unixsocket |; s|^# *unixsocketperm .*|unixsocketperm 770|' /etc/redis/redis.conf")
    NEED_SUDO+=("systemctl restart redis-server")
  }
fi
loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes" && ok "linger" || {
  bad "linger 꺼짐 — 로그아웃하면 데몬이 통째로 죽는다"
  NEED_SUDO+=("loginctl enable-linger $USER")
}
# ⚠ udev 는 **없어도 아무 에러가 안 난다** — 그냥 조용히 안 된다. RealSense
#   규칙이 없으면 libusb 가 장치를 못 열어 **카메라 0개**로 잡히고, CAN 규칙이
#   없으면 인터페이스가 `can0`/`can1` 로 붙어 **저장된 팔 등록이 반대 팔을
#   가리킨다.** 둘 다 증상만 보고는 원인을 못 찾으므로 여기서 막는다.
UDEV_RELOAD=0
for r in "$HERE"/udev/*.rules; do
  [ -e "$r" ] || continue            # 옛 번들에는 udev/ 가 없다 — 그건 그냥 넘긴다
  n="$(basename "$r")"
  if cmp -s "$r" "/etc/udev/rules.d/$n"; then
    ok "udev $n"
  elif [ -f "/etc/udev/rules.d/$n" ]; then
    # ⚠ **이미 있는 규칙은 덮지 않는다. 그건 그 호스트의 것이다** —
    #   `docker-compose.override.yml` 과 같은 이유다. 실측으로 확인했다:
    #     · CAN 규칙에는 **그 호스트 어댑터의 시리얼**이 박혀 있다. 번들 것으로
    #       덮으면 빌드 머신의 시리얼을 가리켜 팔이 영영 이름을 못 얻는다.
    #       120 에는 `piper-can-up.service` 트리거 줄까지 있었는데 그것도 지워졌을 것이다
    #     · RealSense 규칙은 120 쪽이 **88줄 더 많았다**(Intel 공식 규칙). 우리 것은
    #       손으로 추린 부분집합이라, 덮는 것은 다운그레이드다
    #   번들의 규칙은 **아무것도 없는 새 머신**을 위한 것이지 기존 호스트를 고치는
    #   물건이 아니다. 그래서 여기서는 알려만 주고 넘어간다.
    warn "udev $n 이 번들과 다르다 — **호스트 것을 그대로 둔다**"
    echo "       다른 점을 보려면:  diff /etc/udev/rules.d/$n $HERE/udev/$n"
  else
    bad "udev $n 없음"
    NEED_SUDO+=("cp $HERE/udev/$n /etc/udev/rules.d/")
    UDEV_RELOAD=1
  fi
done
[ $UDEV_RELOAD = 1 ] && NEED_SUDO+=("udevadm control --reload-rules && sudo udevadm trigger")

# ⚠ CAN 규칙의 시리얼은 **번들을 구운 머신의 배선**이다. 어댑터가 다른 머신에서는
#   어느 줄도 매칭되지 않는데, 그 실패가 "팔 0개" 로만 보인다 — udev 는 매칭
#   안 된 규칙을 에러로 치지 않는다. 그래서 꽂혀 있는 것과 대조해 둔다.
CAN_RULE="$HERE/udev/99-piper-can.rules"
if [ -f "$CAN_RULE" ]; then
  ATTACHED=""
  for d in /sys/bus/usb/devices/*/; do
    [ -f "$d/idVendor" ] || continue
    [ "$(cat "$d/idVendor")" = "1d50" ] && [ "$(cat "$d/idProduct" 2>/dev/null)" = "606f" ] \
      && ATTACHED="$ATTACHED $(cat "$d/serial" 2>/dev/null)"
  done
  if [ -z "${ATTACHED// /}" ]; then
    warn "USB-CAN 어댑터가 안 꽂혀 있다 — 시리얼 대조를 건너뛴다"
  else
    while read -r sn; do
      [ -n "$sn" ] || continue
      case " $ATTACHED " in
        *" $sn "*) ok "CAN 시리얼 $sn" ;;
        *) warn "CAN 시리얼 $sn 이 이 머신에 없다 — 규칙이 이 배선의 것이 아니다.
       꽂힌 어댑터를 보고 규칙을 고치세요:  python3 $HERE/udev/list-can-adapters.py" ;;
      esac
    done < <(sed -n 's/.*ATTRS{serial}=="\([^"]*\)".*/\1/p' "$CAN_RULE")
  fi
fi

if [ ${#NEED_APT[@]} -gt 0 ] || [ ${#NEED_SUDO[@]} -gt 0 ]; then
  echo
  echo "  아래를 먼저 실행하세요 (이 스크립트는 sudo 를 직접 쓰지 않습니다):"
  # ⚠ 패키지는 **한 줄로 묶는다.** 여러 줄이면 사람이 하나 빠뜨리고, 빠뜨린 것은
  #   늘 그 다음 절에서야 엉뚱한 에러로 드러난다.
  [ ${#NEED_APT[@]} -gt 0 ] && echo "    sudo apt update && sudo apt install -y ${NEED_APT[*]}"
  for c in ${NEED_SUDO[@]+"${NEED_SUDO[@]}"}; do echo "    sudo $c"; done
  [ $CHECK = 0 ] && exit 1
fi

# ── 1. 이미지 ─────────────────────────────────────────────────────────────
if [ -n "${images:-}" ]; then
  say "1. 이미지 ($images)"
  if [ $CHECK = 1 ]; then
    for s in $images; do
      docker image inspect "piper-web-$s:$version" >/dev/null 2>&1 \
        && ok "piper-web-$s:$version" || bad "piper-web-$s:$version 미적용"
    done
  elif [ -n "${registry:-}" ]; then
    # ⚠ **이미 있는 레이어는 안 받는다.** 그게 tar 를 버린 이유다 — 실측으로
    #   v0.3.9 를 가진 호스트가 다음 릴리스에서 받는 양이 3.46GB → 104.5MB 였다.
    for s in $images; do
      docker pull -q "$registry/piper-web-$s:$version"
      # 로컬 이름으로 옮긴다. compose 는 `image: piper-web-backend` 로 참조하므로
      # `:latest` 가 없으면 **다시 빌드하려 든다.**
      docker tag "$registry/piper-web-$s:$version" "piper-web-$s:$version"
      docker tag "$registry/piper-web-$s:$version" "piper-web-$s:latest"
      ok "pull piper-web-$s:$version → :latest"
    done
  elif [ ! -f "$HERE/images.tar.gz" ]; then
    # ⚠ 이미지를 가져올 방법이 없다. 예전에는 없는 tar 를 풀려다 gunzip 오류로
    #   죽어서, 무엇이 잘못됐는지 알 수 없었다.
    bad "이미지를 가져올 방법이 없습니다 — 번들에 tar 도 없고 매니페스트에 registry 도 없습니다"
    exit 1
  else
    gunzip -c "$HERE/images.tar.gz" | docker load
    # ⚠ compose 는 `image: piper-web-backend` (태그 생략=latest) 로 참조한다.
    #   `:latest` 로도 달아두지 않으면 compose 가 **다시 빌드하려 든다.**
    for s in $images; do
      docker tag "piper-web-$s:$version" "piper-web-$s:latest"
      ok "piper-web-$s:$version → :latest"
    done
  fi
else
  say "1. 이미지 — 이번 릴리스에 없음"
fi

# ── 2. 데몬 라이브러리 ────────────────────────────────────────────────────
if [ -n "${wheels:-}" ]; then
  say "2. 데몬 wheel ($wheels)"
  if [ ! -d "$VENV" ]; then
    if [ $CHECK = 1 ]; then bad "$VENV 없음"; else
      # ⚠ `--system-site-packages` 로 만든다 — numpy·opencv·piper-sdk 를 다시 안 깐다.
      python3 -m venv --system-site-packages "$VENV" && ok "venv 생성"
      "$VENV/bin/pip" install -q redis pyrealsense2 && ok "PyPI 의존(redis·pyrealsense2)"
    fi
  fi
  if [ $CHECK = 1 ]; then
    for p in $wheels; do
      "$VENV/bin/pip" show "piper-${p}" >/dev/null 2>&1 || "$VENV/bin/pip" show "piper_${p}" >/dev/null 2>&1 \
        && ok "piper-$p" || bad "piper-$p 미설치"
    done
  else
    "$VENV/bin/pip" install -q --no-deps --force-reinstall "$HERE"/wheels/*.whl
    ok "wheel $(ls "$HERE"/wheels | wc -l) 개"
  fi
else
  say "2. 데몬 wheel — 이번 릴리스에 없음"
fi

# ── 3. 데몬 소스 + 유닛 ───────────────────────────────────────────────────
SRC="$HOME/piper-web-deploy/current"
if [ -n "${daemons:-}" ]; then
  say "3. 데몬 소스·유닛"
  if [ $CHECK = 1 ]; then
    [ -d "$SRC/daemons" ] && ok "$SRC" || bad "$SRC 없음"
  else
    mkdir -p "$SRC" && tar xzf "$HERE/daemons.tar.gz" -C "$SRC" && ok "풀었다: $SRC"
    # ⚠ 설치 스크립트는 **지금 셸의 python3** 를 유닛에 박는다. venv 를 켜고 불러야
    #   데몬이 wheel 을 볼 수 있다 (deploy/install-daemons.sh 참고).
    ( . "$VENV/bin/activate" && "$SRC/deploy/install-daemons.sh" estopd robotd camerad rsd )
    ok "유닛 설치·기동"
  fi
else
  say "3. 데몬 소스·유닛 — 이번 릴리스에 없음"
fi

# ── 3b. compose 파일 ──────────────────────────────────────────────────────
say "3b. compose"
mkdir -p "$SRC"
if [ $CHECK = 1 ]; then
  [ -f "$SRC/docker-compose.yml" ] && ok "docker-compose.yml" || bad "docker-compose.yml 없음"
else
  cp "$HERE/docker-compose.yml" "$SRC/" && ok "docker-compose.yml"
  # env 예시는 참고용으로만 둔다 — 실제 `.env` 는 사람이 만든 것이라 안 덮는다
  cp "$HERE/backend.env.example" "$SRC/" 2>/dev/null || true
fi
# ⚠ **override 는 손대지 않는다.** 그 호스트의 사정(포트 충돌 회피)이 거기 있다.
if [ -f "$SRC/docker-compose.override.yml" ]; then
  ok "override 보존: $(grep -oE '"[0-9]+:[0-9]+"' "$SRC/docker-compose.override.yml" | tr '\n' ' ')"
elif [ -f "$HOME/override.keep.yml" ]; then
  # ⚠ **정리하면서 백업해 둔 것이 있으면 되돌린다.** 실측: 재설치 뒤 이걸
  #   빠뜨려 frontend 가 :80 에 붙었다 — 이 호스트는 :80 을 WMS 가 쓰므로
  #   그 서비스가 마침 안 떠 있어서 충돌만 안 났을 뿐이다.
  cp "$HOME/override.keep.yml" "$SRC/docker-compose.override.yml"
  ok "override 복원: ~/override.keep.yml"
else
  warn "override 없음 — :80 이 비어 있는지 확인하세요 (ss -ltnp)"
fi

# ── 4. 기동 ───────────────────────────────────────────────────────────────
say "4. 기동"
if [ $CHECK = 0 ]; then
  # 데몬이 먼저다 — 컨테이너는 세그먼트와 버스가 있어야 뭔가 보인다.
  for d in estopd robotd camerad rsd; do systemctl --user restart "piper-$d" 2>/dev/null || true; done
  ok "데몬 재시작"
  ( cd "$SRC" && docker compose up -d ) && ok "컨테이너 기동" || \
    bad "docker compose 실패 — $SRC 에서 직접 보세요"
fi
for d in estopd robotd camerad rsd; do
  systemctl --user is-active --quiet "piper-$d" && ok "piper-$d" || bad "piper-$d 안 돎"
done

say "확인"
echo "  ./apply.sh --check      # 적용 상태만 다시 본다"
echo "  docker compose logs -f backend"
