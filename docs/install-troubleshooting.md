# 설치 트러블슈팅 — 증상에서 찾는다

설치는 [스크립트 하나](../README.md#설치)(`piper-install.sh` → `apply.sh`)다. 그게 멈추거나,
끝났는데 뭔가 안 보일 때 여기서 **증상**으로 찾는다. 처방은 전부 이 저장소가 실제로
겪고 고친 것이다 — 근거 없는 항목은 없다. 배포하는 쪽의 절차는
[RELEASE-CHECKLIST](../deploy/RELEASE-CHECKLIST.md), 소스로 도는 기계는 `deploy/install.sh`.

## 0. 먼저 볼 것 — 세 가지

```bash
~/piper-web-deploy/<버전>/apply.sh --check      # 아무것도 안 바꾸고 전제·적용 상태만 다시 본다
journalctl --user -u piper-robotd -n 50          # 데몬 하나의 저널 (camerad·rsd·estopd·unitd·simd·so101d 도 같다)
systemctl --user list-units 'piper-*'            # 무엇이 돌고 무엇이 죽었나
```

웹이 뜬다면 사이드바 **[로그]** (시스템 탭)가 데몬 저널을 SSH 없이 보여 준다(기본은 경고 이상, 데몬별·기간별, 내려받기). **[설정 → 서비스]** 는 — 죽은 데몬, "코드가 더 새것"
경고, 맨 위 버전 카드(게이트웨이·데몬 wheel 버전이 다르면 노란 줄).

## 1. 스크립트가 멈췄다 — "아래를 먼저 실행하세요"

`apply.sh` 는 sudo 를 **직접 쓰지 않는다.** 전제가 빠지면 명령을 찍고 멈춘다.
그 명령만 실행하고 **같은 명령을 다시** 부르면 된다 — 이미 된 것은 건너뛴다.

| 찍힌 말 | 뜻 | 처방 |
|---|---|---|
| `docker 없음` / `docker compose v2 없음` | v1(`docker-compose`)은 GPU 예약 키를 모른다 | `sudo apt install docker.io docker-compose-v2` |
| `docker 데몬 접근 권한 없음` / `그룹 video 없음` / `그룹 dialout 없음` | 데몬은 이 사용자로 돈다. `video` 없으면 카메라 스캔 0개, `dialout` 없으면 SO-101 시리얼을 못 연다 | `sudo usermod -aG docker,video,dialout $USER` — **그 뒤 다시 로그인**해야 반영된다. SSH 라면 끊고 다시 붙는다 |
| `python3-venv 없음` / `redis-server 없음` | 데비안 계열은 venv 가 따로다. Redis 는 버스 전체다 | `sudo apt install python3-venv redis-server` |
| `redis 소켓 없음` | 컨테이너는 유닉스 소켓으로 붙는다. `redis.conf` 를 고치고 **재시작을 빠뜨리면** 소켓 파일이 안 생긴다(실측) | 찍힌 `sed` 두 줄 + `sudo systemctl restart redis-server`, 그 뒤 `apply.sh` 다시 |
| `linger 꺼짐` | 로그아웃하면 사용자 유닛(데몬·학습·녹화)이 **통째로** 죽는다 | `sudo loginctl enable-linger $USER` |
| `$DATA 없음` | 컨테이너 데이터 루트(`/srv/piper-data`) | `sudo mkdir -p /srv/piper-data && sudo chown $USER /srv/piper-data` |
| `nvidia-container-toolkit 없음` | Ubuntu 아카이브에 없다 — NVIDIA 저장소를 먼저 붙여야 한다. 그래서 apt 한 줄에 같이 안 넣는다 | [NVIDIA 설치 안내](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) → `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `드라이버 CUDA x.y — 13.0 이상이 필요` | 이미지의 torch 가 cu130 이다 | `sudo apt install nvidia-driver-580` + **재부팅** |
| `GPU … 컴퓨트 x.y — 7.5 이상만 돈다` | torch 빌드에 PTX 가 없어 JIT 으로도 못 메꾼다 — Pascal·Volta 는 드라이버를 올려도 안 된다 | **GPU 를 바꿔야 한다** |
| `nvidia-smi` 가 "커널과 통신 실패" | 커널을 여러 번 올리는 동안 **지금 커널용 모듈 패키지가 한 번도 안 깔림**(dkms 도 없음) | `sudo apt install dkms nvidia-dkms-580-open && sudo depmod -a && sudo modprobe nvidia` — 재부팅 없이 올라왔다 |
| `레지스트리 … insecure-registries 에 없다` | 사내 레지스트리는 평문(HTTP)이다 | `/etc/docker/daemon.json` 에 `{"insecure-registries": ["piper-build:5000"]}` → `sudo systemctl restart docker` |
| `udev … 없음` | RealSense libusb 규칙 | 찍힌 `cp` + `sudo udevadm control --reload-rules && sudo udevadm trigger` |
| `CAN 이름 규칙이 없다 — 어댑터는 꽂혀 있다` | 규칙이 없으면 커널 열거 순서로 붙어 **두 팔 이름이 뒤바뀐다** | [README 의 CAN 절](../README.md) — `list-can-adapters.py --watch` 로 어느 쪽이 어느 팔인지 확인하고 규칙을 **이 기계에서** 만든다 |

⚠ 그룹은 **재로그인**이 반영 조건이다. `newgrp video` 는 그 셸만 바꾼다. 이미 도는
데몬은 옛 그룹을 쥐고 있으니 그룹을 넣은 뒤 `apply.sh` 를 다시 돌려 재시작시킨다.

## 2. 받지 못한다

| 증상 | 원인 | 처방 |
|---|---|---|
| `lookup piper-build … server misbehaving` | 빌드 기계 이름이 이 호스트에서 안 풀린다 | `/etc/hosts` 에 `<빌드기계 IP> piper-build`. 또는 `PIPER_IMAGE=<IP>:5000/piper-web-backend ./piper-install.sh vX` |
| `레지스트리 … 응답하지 않는다` | 빌드 기계가 레지스트리를 안 열었다 — **배포할 때만 연다**(인증이 없어서) | 빌드 기계에서 `PIPER_REGISTRY_BIND=0.0.0.0 ./deploy/registry.sh`, 확인 `./deploy/registry.sh --status` |
| ghcr 에서 `denied` | 패키지가 private 이다 | 호스트에 `read:packages` **전용** 토큰으로 `docker login ghcr.io`. push 토큰을 호스트에 두지 않는다 |
| 망이 없다 | — | 빌드 기계에서 `./deploy/release.sh vX --offline` → USB 로 tar(3.5GB) → 호스트에서 `tar xzf … && ./vX/apply.sh` |
| 받는 중인데 몇 분째 말이 없다 | v0.4.6 까지는 `docker pull` 이 터미널 밖에서 막대를 안 그렸고 apply.sh 는 `-q` 였다 | v0.4.7 부터 레이어·받은 바이트·속도를 한 줄로 찍는다(도커가 크기를 알려 주면 퍼센트·남은 시간까지)(터미널은 제자리 갱신, 저널은 5초마다 한 줄). **1분 넘게 새 줄이 없으면** 망이다 — `curl http://piper-build:5000/v2/` |
| 두 번째 설치인데 몇 GB 를 받는다 | tar 경로다 — `docker save` 는 부모 레이어를 전부 담는다 | 레지스트리 경로로. 직전 버전을 가진 호스트가 받는 양은 ~100MB 다 |

## 3. 웹은 뜨는데 카메라·팔이 안 보인다

| 증상 | 원인 | 처방 |
|---|---|---|
| 카메라 스캔 0개인데 `lsusb`·`/dev/video*` 엔 있다 | 사용자가 **`video` 그룹에 없다.** `/dev/video*` 는 `root:video` 이고 그래픽 세션 사용자에게만 ACL 이 간다 — SSH 로 들어오면 "아까는 됐는데 지금 안 됨" | `sudo usermod -aG video $USER` → 재로그인 → 데몬 재시작(`apply.sh` 또는 [설정 → 서비스]) |
| RealSense 만 0개 | `pyrealsense2` 는 V4L2 백엔드라 `/dev/video*` 를 직접 연다 — 같은 원인. `rs.log_to_console(debug)` 에 `Permission denied` 가 찍힌다 | 위와 같다 |
| CAN 이 갑자기 통째로 사라졌다(카메라도 같이) | xHCI 컨트롤러가 죽었다 — `dmesg` 에 `xhci_hcd … HC died` | 로봇 페이지 **[USB 진단/복구]** 또는 `echo -n <PCI주소> \| sudo tee /sys/bus/pci/drivers/xhci_hcd/{unbind,bind}`. 버튼은 `/etc/sudoers.d/piper-usb-recover` 의 NOPASSWD 항목이 있어야 돈다 |
| `/health` 조차 멈춘다, 프로세스가 `D` 상태 | RealSense UVC 컨트롤 질의가 커널에서 굳었다 — SIGKILL 도 안 먹는다 | 그 카메라의 USB 포트를 리바인딩: `echo 0 > /sys/bus/usb/devices/<포트>/authorized; sleep 2; echo 1 > …`(sudo). 포트는 `lsusb -t` 로 매번 확인 — 바뀐다 |
| 서비스 패널에 데몬이 죽어 있다 | 저널이 말한다 | `journalctl --user -u piper-<이름> -n 100`. 흔한 것: 그룹(위), 세그먼트 정리(아래), venv 에 패키지 없음(`~/.venvs/piper-daemons/bin/pip show piper-robot`) |
| "팔 세그먼트가 없습니다 (robotd 가 떠 있나요?)" 인데 데몬은 돈다 | 다른 데몬의 기동 정리가 살아 있는 세그먼트를 지웠던 사고(v0.4.4 이전). 발행자는 unlink 된 파일에 계속 써서 **published 는 오르는데 아무도 못 연다** | v0.4.4 이상으로. 그 전이면 발행 데몬 재시작 |
| SO-101 리더가 "연결돼 있지 않습니다" — 데몬 재시작 직후 | so101d 는 기동 시 스스로 붙지 않는다 | 로봇 페이지 시리얼 카드의 **[연결]** |
| frontend 가 포트를 못 잡는다 | 이 호스트의 `:80`/`:8080` 을 남이 쓴다(WMS 등) | **`PIPER_WEB_PORT=8081 ./piper-install.sh`** — 한 번 주면 배포 디렉토리 `.env` 에 남아 업데이트에도 유지된다(`ss -ltnp` 로 빈 포트 확인). 예전 방식 `~/piper-web-deploy/current/docker-compose.override.yml` 재배정도 그대로 동작한다 — 단 `ports:` 병합은 append 라 **`!override`** 태그가 있어야 실제로 바뀐다. `apply.sh` 는 override 를 보존한다 |
| 처음 설치인데 "이번 릴리스에 없음" 으로 이미지·wheel·데몬을 건너뛰고, compose 가 `piper-web-frontend` 를 못 찾는다 | v0.4.14 이하 `apply.sh` 는 매니페스트(그 릴리스에서 **바뀐 것**)만 적용했다 — 직전 릴리스가 깔린 호스트 전제. backend 만 든 릴리스를 처음 깔면 나머지가 빈다 | v0.4.15 이상으로(`./piper-install.sh`, latest) — 없는 것은 번들에서 깐다. 옛 번들로 처음 깔아야 하면 세 레이어가 다 든 릴리스(v0.4.13)를 먼저 |
| 2절에서 pip 가 `Cannot install piper-bus X … and piper-bus Y … conflicting dependencies` (ResolutionImpossible) | 꺼낸 디렉토리(`~/piper-web-deploy/<버전>`)에 **이전 시도의 wheel** 이 남아 같은 배포가 두 버전 — `docker cp` 는 있는 디렉토리에 겹쳐 놓는다(v0.4.15 이하 piper-install.sh) | `rm -rf ~/piper-web-deploy/<버전> && ./piper-install.sh`. v0.4.16 부터는 꺼내기 전에 비우고, apply.sh 도 매니페스트 버전 도장이 찍힌 wheel 만 깐다 |
| backend 가 `could not select device driver "nvidia" with capabilities: [[gpu]]` 로 안 뜬다 | GPU 없는 호스트인데 compose 조합에 GPU 예약이 남아 있다 — v0.4.13 이하 번들(경고만 하고 넘김)이거나, compose 가 2.24 미만이라 nogpu 조각의 `!reset` 이 안 먹는다 | `docker compose version` 확인 → 2.24 미만이면 `docker-compose-v2`(docker.com 저장소는 `docker-compose-plugin`)를 올리고 `apply.sh` 를 다시 돌린다 — 3c 절이 `.env` 의 `COMPOSE_FILE` 에 `docker-compose.nogpu.yml` 을 끼운다. 옛 번들이면 override 파일에 `services: backend: deploy: !reset {}` |
| backend 가 버스에 못 붙는다 | redis 소켓 없음(§1) | `sudo systemctl restart redis-server` 뒤 `docker compose restart backend` |

## 4. 고쳤는데 그대로다

| 증상 | 원인 | 처방 |
|---|---|---|
| 고친 버그가 그대로 재현된다 | **유닛은 기동 시점의 코드로 돈다.** 파일이 새로워도 재시작 전엔 아무 일도 없다 | [설정 → 서비스] 의 "코드가 더 새것" 표시 → [재시작]. 배포에서는 `apply.sh` 4절이 estopd·robotd·camerad·rsd·unitd 를 재시작한다 |
| 버전 카드가 "게이트웨이 vX 인데 데몬 wheel 이 다르다" | 업데이트 뒤 데몬이 재시작을 못 받았다 | 그 데몬 [재시작]. 켜 둔 선택 데몬(simd·so101d)은 `install-daemons.sh --optional` 이 재시작한다 |
| 새 라우트가 404·405 | 게이트웨이가 옛 프로세스다 — 소스 기계에서 `--reload` 없이 띄운 uvicorn 이 그렇다 | [설정 → 서비스] 게이트웨이 [재시작] (활동 없을 때) |
| 게이트웨이 재시작을 눌러도 "수동 조작 중" | 릴레이·조그 세션이 남았다 — v0.4.5 이전엔 [해제]가 세션을 쥔 채였다 | 로봇 페이지에서 릴레이 [정지]. v0.4.5 이상은 해제가 팔로워를 놓는다 |

## 5. 선택 데몬 — simd(시뮬레이션) · so101d(SO-101)

둘은 **깔리되 꺼진 채**다. [설정 → 서비스] 에서 켜고 "부팅 시 시작"을 고른다.
재설치해도 그 선택은 그대로다.

| 증상 | 원인 | 처방 |
|---|---|---|
| 켰는데 바로 죽는다 — 저널에 `No module named mujoco` / `scservo_sdk` | `apply.sh` 가 PyPI 에서 `mujoco`·`feetech-servo-sdk` 를 까는데 **못 깔아도 멈추지 않는다**(선택이라). 경고만 남는다 | `~/.venvs/piper-daemons/bin/pip install mujoco feetech-servo-sdk` (PyPI 가 닿아야 한다) |
| 시뮬 팔은 도는데 카메라 연결이 실패 | EGL 이 없다 — 렌더러는 카메라를 연결할 때 만든다 | `sudo apt install libegl1 libgl1-mesa-dri` (NVIDIA 면 드라이버의 EGL 로 충분) → simd 재시작 |
| SO-101 모터 무응답 | 전원·데이지체인 케이블, 또는 `dialout` 그룹 | 케이블·전원 먼저. 그다음 §1 의 그룹 |
| SO-101 "캘리브레이션이 없습니다 — 등록·수집하면 안 됩니다" | 캘리브레이션 파일이 없다(`~/.cache/huggingface/lerobot/calibration/teleoperators/so101_leader/`) | 로봇 페이지 시리얼 카드의 **[캘리브레이션]** 마법사 — 양끝까지 돌리면 중앙을 알아서 잡는다 |

## 6. 웹 [업데이트] 가 안 된다

| 증상 | 원인 | 처방 |
|---|---|---|
| 버튼이 잠겨 있고 "piper-unitd 가 없습니다" | 켜기/끄기·업데이트는 호스트의 unitd 가 실행한다 — 컨테이너 게이트웨이는 systemctl 이 없다 | v0.4.5 이상은 `apply.sh` 가 깐다. 소스 기계는 `deploy/install-daemons.sh unitd` |
| v0.4.5 → v0.4.6 을 웹에서 못 올린다 | 그 전 unitd 엔 업데이트 창구가 없다 | **이 한 번만 손으로**: `PIPER_IMAGE=piper-build:5000/piper-web-backend ./piper-install.sh v0.4.6`. 그다음부터 버튼 |
| [적용] 이 409 | 녹화·추론·학습·수동 조작 중 — 마지막 단계가 게이트웨이와 데몬을 갈아치운다 | 끝내고 다시 |
| 적용이 "전제가 빠져 있어 멈췄습니다" 를 띄운다 | §1 과 같다 — 스크립트는 sudo 를 안 쓴다 | 보이는 블록을 복사해 호스트에서 실행, 그룹이면 재로그인, 다시 [적용] |
| 적용 뒤 화면이 안 돌아온다 | 게이트웨이가 갈아치워지는 동안은 원래 끊긴다. 몇 분이 지나도 안 오면 apply 가 중간에 멈춘 것 | 호스트에서 `journalctl --user -u piper-update -n 200` — 실패 줄이 거기 있다. `~/piper-web-deploy/<버전>/apply.sh --check` |
| 이전으로 돌리고 싶다 | 받아 둔 버전 디렉토리가 남아 있다 | 버전 카드의 "받아 둔 다른 버전 → 이 버전으로", 또는 `~/piper-web-deploy/<이전>/apply.sh`. 데이터는 어느 방향이든 안 건드린다 |

## 7. 로그아웃했더니 다 죽었다

재부팅도 OOM 도 없는데 데몬·학습·녹화가 **동시에** 사라졌다면 십중팔구 `Linger=no` 다 —
마지막 세션이 끝나면 systemd 가 사용자 프로세스를 전부 내린다.
`loginctl show-user $USER | grep Linger` 로 확인, `sudo loginctl enable-linger $USER`.
`apply.sh` 가 이걸 검사하지만, 검사 뒤에 누가 껐을 수 있다.

## 8. 처음부터 다시 깔기

[RELEASE-CHECKLIST 의 "처음부터 다시 깔려면"](../deploy/RELEASE-CHECKLIST.md#처음부터-다시-깔려면).
요점 둘: **데이터(`/srv/piper-data`·`~/.cache/huggingface/lerobot`·`~/.config/piper-web`)는
지우지 않는다**, `docker-compose.override.yml` 을 먼저 빼돌린다. 다시 까는 명령은
평소 업데이트와 같은 `apply.sh` 다.
