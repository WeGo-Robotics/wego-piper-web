#!/usr/bin/env bash
# 빌드 머신에 사설 레지스트리를 띄운다. **LAN 전용이다.**
#
# ## 왜 필요한가
#
# `docker save` 는 **자식 이미지를 저장해도 부모 레이어를 전부 담는다**(측정:
# 부모 28MB → 한 줄 얹은 자식 28MB). 그래서 베이스/앱을 갈라놔도 tar 로 보내는 한
# 매번 3.46GB 가 통째로 간다. 레지스트리는 **호스트에 없는 레이어만** 준다 —
# 베이스 7GB 는 처음 한 번, 이후 릴리스는 우리 코드 390MB 뿐이다.
#
# ⚠ **평문(HTTP)이다.** 사내 LAN 이라 그렇게 둔다. 호스트에서 이 주소를
#   `insecure-registries` 에 넣어야 붙는다 — `apply.sh` 가 확인하고 명령을 찍는다.
#   망 밖으로 낼 거면 TLS 부터 붙여야 한다.
#
# 사용:  ./deploy/registry.sh [--stop|--status]
set -euo pipefail
NAME=registry
PORT="${PIPER_REGISTRY_PORT:-5000}"
# ⚠ **기본은 루프백이다.** `registry:2` 에는 **인증이 없다** — 밖에 열면 같은 망의
#   누구나 `piper-web-backend:v0.3.10` 을 밀어넣을 수 있고, 로봇 호스트는 그것을
#   받아서 **그대로 실행한다.** 여는 것은 의식적인 선택이어야 한다:
#       PIPER_REGISTRY_BIND=0.0.0.0 ./deploy/registry.sh
#   빌드 머신에서 굽고 미는 데는 루프백으로 충분하다 — `release.sh` 도 `localhost`
#   로 민다. 열어야 할 때는 **로봇 호스트에 배포할 때뿐**이다.
BIND="${PIPER_REGISTRY_BIND:-127.0.0.1}"
# ⚠ 기본값이 `/srv/...` 가 아니다. 빌드 머신의 레지스트리는 **개발자 것**이라
#   설치에 sudo 를 요구할 이유가 없다. 공용 서버에 둘 거면 `PIPER_REGISTRY_DATA`
#   로 옮기면 된다.
DATA="${PIPER_REGISTRY_DATA:-$HOME/.local/share/piper-registry}"

case "${1:-}" in
  --stop)   docker rm -f "$NAME" >/dev/null 2>&1 && echo "멈췄다" || echo "안 돌고 있었다"; exit 0 ;;
  --status) docker ps --filter "name=^${NAME}$" --format '{{.Names}} {{.Status}} {{.Ports}}' | grep . \
              || { echo "안 돌고 있다"; exit 1; }
            # 어디에 열려 있는지가 status 의 핵심이다 — 0.0.0.0 이면 밖에서 보인다
            ss -ltn 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print "  듣는 곳: "$4}'
            exit 0 ;;
esac

if docker ps --filter "name=^${NAME}$" --format '{{.Names}}' | grep -q .; then
  HAVE="$(docker port "$NAME" 5000 2>/dev/null | head -1)"
  echo "이미 돌고 있다: $NAME  ${HAVE:-:$PORT}"
  # ⚠ 원하는 것과 다르게 열려 있으면 말해 준다. 조용히 두면 "잠갔다고 생각했는데
  #   0.0.0.0 이더라"가 된다 — 실제로 그랬다.
  case "${HAVE:-}" in
    "$BIND:$PORT") : ;;
    *) echo "⚠ 지금은 ${HAVE:-?} 에 열려 있는데 요청은 $BIND:$PORT 다."
       echo "  바꾸려면:  ./deploy/registry.sh --stop && PIPER_REGISTRY_BIND=$BIND $0" ;;
  esac
else
  # ⚠ 데이터는 **볼륨 밖 호스트 경로**에 둔다. 컨테이너를 지웠다고 이미지가
  #   사라지면 호스트들이 다음 pull 에서 통째로 다시 받는다.
  mkdir -p "$DATA" 2>/dev/null || { echo "✗ $DATA 를 못 만든다 — sudo mkdir -p $DATA && sudo chown $USER $DATA"; exit 1; }
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --restart=always --name "$NAME" -p "$BIND:$PORT:5000" \
    -v "$DATA:/var/lib/registry" registry:2 >/dev/null
  echo "띄웠다: $NAME  $BIND:$PORT  (저장소 $DATA)"
fi

IP="$(ip -4 -br addr show 2>/dev/null | grep -v '^lo' | grep -v '^docker\|^br-' | head -1 | awk '{print $3}' | cut -d/ -f1)"
NAME="${PIPER_REGISTRY_NAME:-piper-build}"
echo
echo "이 머신의 LAN 주소: ${IP:-<못 찾음>}"
cat <<TXT

⚠ **IP 를 그대로 쓰지 마세요.** 이 주소는 DHCP 로 받은 것이라 바뀝니다. 바뀌면
  호스트의 daemon.json 과 **이미 만들어 둔 번들의 매니페스트가 동시에** 죽습니다.
  이름을 하나 정해 호스트가 소유하게 하면, 주소가 바뀌어도 고칠 곳이 한 줄입니다.

지금은 **$BIND** 에만 열려 있습니다.

⚠ 로봇 호스트가 받으려면 **밖으로 열어야 합니다.** `registry:2` 에는 인증이 없으니
  열기 전에 알고 여세요 — 같은 망의 누구나 이미지를 밀어넣을 수 있고, 호스트는
  그것을 받아 그대로 실행합니다.

  ./deploy/registry.sh --stop
  PIPER_REGISTRY_BIND=0.0.0.0 ./deploy/registry.sh

  열었다면 방화벽으로 **그 호스트만** 들이는 것을 권합니다:
    sudo ufw allow from <로봇호스트IP> to any port $PORT proto tcp
    sudo ufw deny $PORT/tcp

로봇 호스트에서 (한 번만):

  # 1) 이름 → 주소.  나중에 IP 가 바뀌면 **이 줄만** 고친다
  echo "${IP:-<이 머신 IP>}  $NAME" | sudo tee -a /etc/hosts

  # 2) 평문 레지스트리를 신뢰.  이름을 쓰므로 다시 고칠 일이 없다
  #    /etc/docker/daemon.json 에  {"insecure-registries": ["$NAME:$PORT"]}
  sudo systemctl restart docker

빌드 머신에서:

  export PIPER_REGISTRY=$NAME:$PORT

⚠ 더 확실히 하려면 공유기에서 이 머신에 **DHCP 예약**을 걸어 주소를 고정하세요.
  그러면 위 1) 도 다시 손댈 일이 없습니다.
TXT
