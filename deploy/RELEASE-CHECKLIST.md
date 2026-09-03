# 배포 절차 — 로봇 호스트 실기 배포 기록

> 2026-08-13에 사전 점검을 남겼고, 2026-08-14에 `v0.2.0`을 실제로 이 절차대로
> 로봇 호스트에 배포해서 끝까지 검증했다. 아래 절차는
> 그 실행 순서 그대로이고, [겪은 문제](#겪은-문제와-해결) 절이 다음 배포 때 반복하지
> 않아도 되게 남긴 함정들이다.

## 왜 "소스 체크아웃"이 아닌가

지금까지의 배포는 호스트에 저장소를 `git clone`/`pull`해서 그 자리에서 돌리는 방식이었다.
제품처럼 배포하려면 "무엇을 어떤 버전으로 올렸는지"가 커밋 로그가 아니라 **아티팩트**로
남아야 한다. 그래서 두 갈래로 나눈다.

## 아키텍처: 이미지 레이어 + 데몬 레이어

`docker-compose.yml`이 `privileged`/`/dev` 마운트를 뺀 이후, 하드웨어(CAN·카메라·RealSense)는
컨테이너가 아니라 **호스트에서 도는 Python 데몬**(`daemons/estopd.py` 등, systemd 유저 유닛)이
쥔다. 즉 "이미지 하나로 끝"이 아니라 세 갈래를 따로 배포해야 한다.

| 레이어 | 내용물 | 배포 방식 |
|---|---|---|
| **이미지** | backend, frontend (`docker-compose.yml`) | 로컬 빌드 → `docker save` → `scp` → 호스트 `docker load` |
| **데몬 라이브러리** | `bus/ cam/ rs/ robot/ shm/` (순수 파이썬, `daemons/*.py`가 직접 import) | 로컬에서 wheel 빌드 → `scp` → 호스트 전용 venv에 `pip install` |
| **데몬 소스 + 유닛 정의** | `daemons/*.py`, `deploy/systemd/*.service`, `deploy/install-daemons.sh` | 소스 그대로 tar로 묶어 `scp` (이건 wheel이 아니라 daemons/의 엔트리포인트 자체라 패키징 대상이 아님) |

`phase/`, `vendor/*`는 데몬이 import하지 않는다 — `backend/Dockerfile`이 이미지 빌드 때
같이 넣으므로 별도 wheel 불필요 (`bus/shm/robot/phase/vendor`만 COPY, `cam/rs`는 호스트 전용).

레지스트리는 둘 다 쓴다 (처음엔 "안 쓰기로 했다"였는데 뒤집었다 — tar 는 무엇을
고쳤든 매번 3.46GB 를 옮기기 때문이다):

- **로컬 레지스트리** (`registry.sh`, `PIPER_REGISTRY=piper-build:5000`) — 현장망 배포
- **GHCR** (`PIPER_REGISTRY=ghcr.io/swhan-wego`) — 외부망. v0.4.3 부터 올라가 있다.
  push 는 `docker login ghcr.io` (classic PAT, `write:packages`) 후 release.sh 그대로.
  ⚠ 패키지는 **private** 이다 — 호스트가 GHCR 에서 직접 pull 하려면 호스트에도
  `read:packages` 전용 토큰으로 `docker login` 이 필요하다. push 용 토큰을
  호스트에 두지 말 것.

---

## 원터치 — 이 두 줄이 절차다

⚠ **태그가 빌드보다 먼저다.** `release.sh` 는 `git tag --sort=-v:refname` 의 첫 줄을
직전 버전으로 삼아 diff 로 레이어를 정한다. 태그 없이 구우면 기준이 뒤로 밀려
이미 배포한 것까지 다시 담는다 — 실측으로 "173 파일 변경" 이던 판정이 태그를 찍자
**5 파일**이 됐다.

```bash
git push && git tag -a v0.3.10 -m "..." && git push origin v0.3.10

# 레지스트리로 (망이 있을 때 — 권장)
./deploy/registry.sh --stop
PIPER_REGISTRY_BIND=0.0.0.0 ./deploy/registry.sh    # 배포할 때만 연다
export PIPER_REGISTRY=piper-build:5000
./deploy/release.sh v0.3.10 --dry-run               # 무엇이 올라갈지 먼저
./deploy/release.sh v0.3.10                         # 이미지 push

# 망 없는 현장 (USB)
./deploy/release.sh v0.3.10 --offline                # 3.46GB tar
```

호스트는 **스크립트 하나**로 받는다 — [README](../README.md#설치). 이미지 안에
데몬·wheel·udev·compose·`apply.sh` 가 다 들어 있다.

| | 전송량 |
|---|---|
| tar (`--offline`) | **3.46 GB** — 무엇을 고쳤든 매번 |
| 레지스트리 (실측) | **104.5 MB** — 직전 버전을 가진 호스트가 받는 양 |

⚠ `docker save` 는 **자식 이미지를 저장해도 부모 레이어를 전부 담는다**(측정: 부모
28MB → 한 줄 얹은 자식 28MB). 그래서 베이스/앱을 갈라놔도 tar 로 보내는 한 매번
전부 간다 — 전송량이 줄려면 레지스트리여야 한다.

⚠ 베이스 이미지(서드파티 ~7GB)는 `release.sh` 가 **없거나 낡을 때만** 굽는다.
`Dockerfile.base` 를 고치면 내용 해시가 달라져 자동으로 다시 굽는다.

**어느 레이어가 필요한지 사람이 판단하지 않는다.** `release.sh` 가 직전 태그와의
diff 로 정한다. 아래 [절차](#절차-수동)는 그 스크립트가 하는 일을 풀어 쓴 것이고,
문제가 났을 때 어디를 볼지 알려면 여전히 읽을 값어치가 있다.

### 왜 자동으로 정하나 — 이력이 답이다

15회 중 **12회가 이미지만**이었고 세 레이어 전부는 3회였다. 그런데 매번 사람이
판단했고, **실제로 틀렸다**:

> `v0.3.4` 는 이력에 `wheel(cam·rs) + backend` 로 적혀 있다. 그 태그의 diff 에는
> `frontend/src/types/ws.ts` 가 들어 있다 — **frontend 를 안 올렸다.**

빠뜨리면 호스트에서 옛 코드가 돈다. 그리고 그건 배포 직후가 아니라 한참 뒤에
"왜 이 기능이 안 되지" 로 나타난다.

판정 규칙 (`deploy/release.sh`):

| 바뀐 경로 | 올릴 것 |
|---|---|
| `backend/ wrapper/ policies/ act_aux/ phase/ vendor/` | backend 이미지 |
| `frontend/` | frontend 이미지 |
| `bus/ shm/ robot/` | **backend 이미지 + 데몬 wheel** (양쪽이 쓴다) |
| `cam/ rs/` | 데몬 wheel (호스트 전용 — 이미지엔 없다) |
| `daemons/ deploy/systemd/ deploy/install-daemons.sh` | 데몬 소스·유닛 |
| `tests/ *.md refactor/ feature/ docs/` | **아무것도** — 도는 것을 안 바꾼다 |

`bus`·`shm`·`robot` 이 양쪽인 것이 요점이다. 한쪽만 올리면 **컨테이너와 데몬이
서로 다른 코드로 돈다** — 같은 라이브러리를 둘이 쓰기 때문이다.

### 첫 설치와 업데이트가 같은 명령이다

`apply.sh` 는 **없는 것만 한다.** 두 번 돌려도 같고, 첫 설치면 전부 한다.
절차가 갈리면 "업데이트인 줄 알았는데 첫 설치였다" 가 생기는데, 그때 빠뜨리는
것은 늘 sudo 가 필요한 쪽(redis 소켓·linger·데이터 루트)이고 증상은
**"웹은 뜨는데 카메라도 팔도 안 보인다"** 라 원인을 찾기 어렵다.

sudo 가 필요한 것은 **명령을 찍어 주고 멈춘다** — 스크립트가 몰래 쓰면 무엇이
바뀌었는지 아무도 모른다.

```bash
./v0.3.9/apply.sh --check    # 적용 상태만 본다 (아무것도 안 바꾼다)
```

---

## 절차 (수동)


### 0. 버전 번호 — **최하위 자리만 올린다**

`v0.3.1` → `v0.3.2` → … → `v0.3.100` → `v0.3.1000`. 자릿수가 몇이 되든 상관없다.
**major·minor 는 사용자가 명시적으로 요청할 때만** 올린다.

> ⚠ 2026-08-14 배포에서 `v0.2.9` 다음을 `v0.3.0` 으로 올렸는데, 그건 잘못이다.
> 9 다음은 **10** 이지 올림이 아니다. 버전 번호가 무엇을 뜻하는지는 배포하는
> 사람이 정할 일이 아니다.

마지막 태그를 보고 끝자리에 +1 한다:

```bash
git tag --sort=-v:refname | head -1     # 예: v0.3.1 → 다음은 v0.3.2
```

### 1. 릴리즈 소스 정리 (로컬)

- [x] `wip/upgrade` 미커밋 변경 정리, `origin/wip/upgrade`로 push
- [x] release 태그 — **주의**: 지난번 `v0.1.0` 태그를 잘못 찍어서 지운 적 있다.
      브랜치 정리와 push까지 끝난 뒤에 태그를 찍을 것. 번호는 [0단계](#0-버전-번호--최하위-자리만-올린다) 규칙.

### 2. 이미지 빌드 & 전달

- [x] `docker compose build` (backend + frontend, 로컬) — backend 11.2GB, frontend 99.8MB
- [x] `docker tag piper-web-backend:latest piper-web-backend:v0.2.0` (frontend도 동일)
- [x] `docker save piper-web-backend:v0.2.0 piper-web-frontend:v0.2.0 | gzip > piper-web-v0.2.0.tar.gz` (3.4GB)
- [x] `scp`로 호스트에 전달, 호스트에서 `docker load`
- [x] 호스트에서 `docker tag ...:v0.2.0 ...:latest` — `docker-compose.yml`이 `image: piper-web-backend`
      (태그 생략 = `:latest`)로 참조하므로, 로드한 이미지를 `:latest`로도 태깅해야 compose가
      다시 빌드하려 들지 않는다. (`build:`→`image:` 전환은 여전히 [미결정](#미결정))

### 3. 데몬 레이어 wheel + 소스 빌드 & 전달

- [x] 로컬에서 `bus/ shm/ cam/ robot/ rs/` 각각 `pip wheel --no-deps -w <out> ./<pkg>`
      (전부 setuptools, 순수 파이썬 — 로컬 py3.13 / 호스트 py3.12 버전 차이 무관)
      — **주의**: 저장소 안에서 빌드하면 `<pkg>/build/` 산출물이 남는다. 빌드 후
      `rm -rf {bus,cam,robot,rs,shm}/build` 로 지울 것 (git에 안 잡히지만 지저분함).
- [x] `daemons/*.py`, `deploy/systemd/`, `deploy/install-daemons.sh`, `docker-compose.yml`,
      `backend/.env`를 별도 tar로 묶음 (이건 wheel이 아니라 소스 그대로 — 데몬의
      엔트리포인트라서 패키징 대신 파일 전달)
- [x] wheel + 소스 tar `scp`로 호스트에 전달
- [x] 호스트에 데몬 전용 venv 생성: `python3 -m venv --system-site-packages ~/.venvs/piper-daemons`
      (`--system-site-packages`로 이미 깔려 있는 `numpy`/`opencv-python-headless`/`Pillow`/
      `piper-sdk`/`python-can` 재사용)
- [x] venv에 wheel `--no-deps` 설치 + 부족한 것 PyPI에서 설치: `redis`, `pyrealsense2`
- [x] `deploy/install-daemons.sh`를 이 venv를 activate한 셸에서 실행 (스크립트가
      "지금 셸의 python3"를 그대로 쓰기 때문 — `deploy/install-daemons.sh:12`)

### 4. 호스트 인프라 준비 (sudo 필요)

- [x] `sudo mkdir -p /srv/piper-data && chown $USER /srv/piper-data`
- [x] `apt-get install -y redis-server` + `/etc/redis/redis.conf`에
      `unixsocket /run/redis/redis-server.sock` / `unixsocketperm 770` 추가 +
      **`systemctl restart redis-server`** (컨테이너는 유닉스소켓, 호스트 데몬은 기본 TCP
      `127.0.0.1:6379`로 붙으므로 둘 다 켜져 있어야 함)
- [x] `nvidia-container-toolkit` 설치 + `nvidia-ctk runtime configure --runtime=docker` +
      `systemctl restart docker`
- [x] `sudo loginctl enable-linger $USER`
- [x] CAN: 이 배포는 "1 리더 / 1 팔로워"라 `can0`(follower)·`can1`(leader) **둘 다** 필요.
      `can1`이 안 올라와 있으면:
      ```
      sudo modprobe gs_usb
      sudo ip link set can1 down
      sudo ip link set can1 type can bitrate 1000000
      sudo ip link set can1 up
      ```
      (`robot/piper_robot/can.py:init_can_interface`와 동일한 시퀀스. 이 인터페이스명이
      `can0`/`can1`인지 `can_follower1`/`can_leader1`인지는 `~/piper_config.json`으로 확인)
- [x] v4l2loopback — **불필요.** `refactor/camera-transport.md`에서 후보로 검토했지만
      shm 방식이 채택됐다 (LeRobot 수정 0이 이유). `CLAUDE.md`/Dockerfile 주석에 남은 언급은
      stale 문서.

### 5. systemd 데몬 설치 + 상시 기동

- [x] `deploy/install-daemons.sh` 실행 (estopd, robotd, camerad, rsd 전부) — venv activate된
      셸에서 실행해야 함
- [x] `loginctl show-user $USER`로 `Linger=yes` 확인
- [ ] (선택) 로컬 판단 LLM — Ollama 바이너리를 `~/tools/bin` 에 풀고
      `deploy/install-daemons.sh ollama` + `ollama pull qwen2.5:7b` +
      `.env` 에 `PIPER_LLM_*` (없는 머신은 유닛 Condition 이 건너뜀)
- [ ] (선택) YOLO 검출 — 데몬 python 에 `pip install ultralytics`
      (piper-yolod 는 설치 불필요 — `/api/vision/start` 가 유닛으로 띄움)

### 6. `docker compose up` 검증

- [x] 호스트 전용 `docker-compose.override.yml`로 포트 조정 필요할 수 있음 — **80/8080이
      이미 이 호스트의 다른 서비스(WMS 창고관리시스템, 별개 node 앱)가 쓰고 있었다.**
      compose가 여러 파일의 `ports:`를 **병합(append)**하지 **치환하지 않으므로**,
      override에서 포트를 바꾸려면 `ports: !override [...]` 로 명시해야 한다:
      ```yaml
      services:
        frontend:
          ports: !override
            - "8081:80"
      ```
      단순히 `ports: ["8081:80"]`만 쓰면 80과 8081 둘 다 바인딩을 시도해서 여전히 실패한다.
      **포트를 정하기 전에 `ss -ltnp`로 이미 쓰는 포트를 꼭 확인할 것** — 로봇 전용
      호스트가 아니라 다른 서비스가 같이 도는 워크스테이션일 수 있다.
- [x] `docker compose up -d` 후 backend 로그에 `E-stop 버스에 연결할 수 없습니다` /
      `job 조회 실패 ... No such file or directory` 가 보이면 → redis unixsocket을 설정만 하고
      재시작을 안 한 상태에서 컨테이너가 먼저 뜬 것. `docker compose restart backend`로 해결.
- [x] `curl http://<host>:<port>/health` 로 backend 확인, frontend를 통한 프록시도 확인
- [ ] E-stop, 카메라 프리뷰, 실제 팔 움직임 등 하드웨어 관련 항목은
      [refactor/HARDWARE-CHECKLIST.md](../refactor/HARDWARE-CHECKLIST.md) 절차를 새 호스트에서
      재실행 (사람이 팔을 잡아야 하는 부분이라 아직 안 함)

---

## 겪은 문제와 해결

| 문제 | 원인 | 해결 |
|---|---|---|
| GPU 드라이버는 깔려 있는데 `nvidia-smi`가 커널과 통신 실패 | `nvidia-driver-580-open` 메타패키지가 특정 커널 버전용 `linux-modules-nvidia-580-open-<kernel>` 패키지에 의존하는데, 호스트가 커널을 여러 번 업데이트하는 동안 **현재 실행 중인 커널(6.17.0-35)용 모듈 패키지가 한 번도 설치된 적이 없었음** (dkms 자체도 미설치라 자동 재빌드도 안 됨) | `apt install dkms nvidia-dkms-580-open` (dkms 기반 패키지로 전환 — 커널이 또 바뀌어도 자동 재빌드됨) → `depmod -a && modprobe nvidia` — **재부팅 불필요**, 새로 빌드된 모듈을 바로 올릴 수 있었다 |
| `docker compose up`에서 frontend가 포트 바인딩 실패 | 이 호스트가 로봇 전용이 아니라 다목적 워크스테이션 — :80은 별개 운영 서비스(WMS), :8080은 다른 node 프로세스가 이미 사용 중 | 사용 중이지 않은 포트(8081)로 `docker-compose.override.yml`에서 재배정. `ports:` 병합이 append라 `!override` YAML 태그로 명시해야 실제로 바뀜 |
| backend가 redis/E-stop 버스에 못 붙음 | `redis.conf`에 `unixsocket` 설정을 append만 하고 `systemctl restart redis-server`를 빠뜨림 — 컨테이너가 이미 그 상태에서 떠서 소켓 파일 자체가 없었음 | `systemctl restart redis-server`로 소켓 생성 확인 후 `docker compose restart backend` |

---

## 2026-08-14 배포 후 상태 
| 항목 | 상태 |
|---|---|
| `piper-web-backend/frontend:v0.2.0` | `docker load` 완료, `:latest`로도 태깅 |
| `docker compose up` | 정상, frontend `:8081`, backend `/health` 200 |
| GPU | RTX 4050, `docker --gpus all` 검증 완료 |
| redis | unixsocket + TCP 둘 다 동작 |
| 데몬 4개 (estopd/robotd/camerad/rsd) | systemd 유저 유닛, active, 재시작 0회 |
| CAN | `can0`(follower) + `can1`(leader) 둘 다 UP, 1Mbps |
| RealSense | 장치 인식됨 (color/depth/infrared) |
| v4l2loopback | 불필요 (shm 방식 채택으로 대체됨) |

---

## 처음부터 다시 깔려면

⚠ **데이터를 지우지 않는다.** 아래 셋은 그대로 둔다:

| 경로 | 내용 |
|---|---|
| `/srv/piper-data` | 컨테이너가 쓰는 모델·설정·로그 |
| `~/.cache/huggingface/lerobot` | **녹화한 데이터셋** |
| `~/.config/piper-web` | 사용자 설정 |

⚠ **`docker-compose.override.yml` 을 먼저 빼돌린다.** 그 호스트의 포트 사정이
거기 있다 — 로봇 호스트는 `:80` 을 WMS 가 쓰고 있어 8081 로 빼 두었다.
빠뜨리면 frontend 가 `:80` 에 붙는다(실제로 그렇게 됐다).

```bash
# 0. 호스트 사정 백업
cp ~/piper-web-deploy/current/docker-compose.override.yml ~/override.keep.yml

# 1. 세운다
cd ~/piper-web-deploy/current && docker compose down
systemctl --user stop    piper-{estopd,robotd,camerad,rsd}
systemctl --user disable piper-{estopd,robotd,camerad,rsd}

# 2. 지운다 — 데이터는 위 표의 경로라 여기 없다
rm -rf ~/piper-web-deploy ~/.venvs/piper-daemons
rm -f  ~/.config/systemd/user/piper-*.service
systemctl --user daemon-reload
docker images -q --filter reference='piper-web-*' | sort -u | xargs -r docker rmi -f

# 3. 다시 깐다 — 평소 업데이트와 **같은 명령**
./v0.3.9/apply.sh
```

`apply.sh` 는 `~/override.keep.yml` 이 있으면 알아서 되돌린다.

### 실제로 해 본 기록 (2026-08-28, v0.3.9)

| 단계 | 실측 |
|---|---|
| 번들 빌드 | 3.5GB (backend·frontend + wheel 5 + 데몬 + compose) |

⚠ **이 절은 README 에 있었다.** README 는 이제 "스크립트 하나" 만 설명한다 —
받는 사람이 볼 것과 배포하는 사람이 볼 것은 다르고, 섞여 있으면 받는 사람이
자기 경우가 아닌 절차를 따라 하다 막힌다.

## 배포 이력

| 버전 | 시각 | 올린 레이어 | 내용 |
|---|---|---|---|
| v0.2.0 | 08-14 15:17 | 이미지 + 데몬 wheel + 데몬 소스 | 최초 전체 배포 |
| v0.2.1~v0.2.5 | 08-14 저녁 | 이미지 | 리더 shm 텔레오퍼레이터, CAN RPC, 사운드 |
| v0.2.6 | 08-14 22:02 | **frontend 이미지만** (27MB) | 에피소드 경과 시간 |
| v0.2.7 | 08-14 22:36 | 이미지 (backend+frontend) | 장치 사라짐 경보 |
| v0.2.8 | 08-14 22:52 | **backend 이미지만** | 경보 판정을 "발행 멈춤"으로 (v0.2.7 은 진짜 USB 뽑기를 못 잡았다) |
| v0.2.9 | 08-14 23:07 | **backend 이미지만** | 재시작 시점에 이미 멈춰 있던 장치도 잡는다 |
| v0.3.0 | 08-14 23:27 | 이미지 (backend+frontend) | 뽑힌 카메라를 스캔 목록에서 뺀다 |
| v0.3.1 | 08-14 23:44 | 이미지 (backend+frontend) | 경보를 "멈춘 발행"만으로 좁힘 + WS 재연결 시 재조회 |
| v0.3.2 | 08-15 00:12 | **세 레이어 전부** | 카메라 데몬이 사라짐을 스스로 판정 (`lost()`) |
| v0.3.3 | 08-15 00:32 | **세 레이어 전부** | robotd 도 같은 판정 — `/sys/class/net` 소멸 |
| v0.3.4 | 08-15 00:52 | wheel(cam·rs) + backend | rsd 결정적 신호 추가 · 꽂힌 장치를 케이블 탓으로 안 함 · camerad 폭주 수정 |
| v0.3.5 | 08-15 01:14 | **frontend 이미지만** (27MB) | 시스템 메시지 인터페이스 — `alert`/`confirm` 30곳 제거 |
| v0.3.6 | 08-15 02:16 | 이미지 (backend+frontend) | 멈춘 영상을 정상처럼 안 보이게 (`streaming`) · 스캔 썸네일 오보 제거 |
| v0.3.7 | 08-15 02:38 | **backend 이미지만** | 원격 추론 — 이미지에 `grpcio`, `grpc_python` 을 `sys.executable` 로 |
| v0.3.8 | 08-15 02:48 | 이미지 (backend+frontend) | gRPC 추론이 **한 번도 못 돌던** `_paused` 버그 · 서버 모드 체크포인트 입력 |
| v0.3.9 | 08-28 15:05 | **세 레이어 전부** | 원터치 릴리스(`release.sh`/`apply.sh`) 도입 · .120 완전 삭제 후 재설치 |

⚠ **v0.3.9 의 태그는 소급해서 찍었다.** 번들을 굽고 배포한 뒤 태그를 빠뜨렸고,
그 사이 v0.3.8 위에 145 커밋이 쌓여 다음 릴리스의 diff 기준이 통째로 밀려 있었다.
`3a0c57a` 가 맞다는 근거는 태그 메시지에 적어 두었다 — 번들의 `apply.sh`·compose·
env 와 wheel 소스 14개가 그 커밋과 바이트 단위로 일치하고, 앞뒤 커밋과는 다르다.

**태그가 먼저다.** `release.sh` 는 `git tag --sort=-v:refname` 의 첫 줄을 직전
버전으로 삼아 diff 로 레이어를 정한다. 태그 없이 구우면 기준이 뒤로 밀려 이미
배포한 것까지 다시 담는다 — 실제로 v0.3.10 을 굽기 직전 판정이 "173 파일 변경"
이었고, 태그를 찍자 **5 파일**이 됐다.

### 원격 추론 (.42 서버 ↔ .120 클라이언트)

```bash
# .42 — 정책 서버. **0.0.0.0 으로 열어야** 원격에서 붙는다 (기본은 127.0.0.1)
curl -X POST localhost:8000/api/policy-server/start \
     -H 'content-type: application/json' -d '{"host":"0.0.0.0","port":8088,"fps":30}'

# .120 — 닿는지 먼저 본다. TCP 만 열려도 gRPC 가 안 될 수 있다(모듈 없음)
curl -X POST http://localhost:8081/api/policy-server/check-remote \
     -H 'content-type: application/json' -d '{"address":"<정책서버IP>:8088"}'
```

⚠ **모델은 서버가 로드한다** — 체크포인트 경로는 **.42 기준**이어야 한다.
클라이언트(.120)에 모델이 0개인 것은 정상이다.

⚠ **카메라는 클라이언트 것이다.** 정책이 기대하는 키(`top`·`hand` 등)를 클라이언트가
전부 갖고 있어야 한다 — 서버에 있어도 소용없다.

⚠ **API 로 추론을 시작하면 2초 뒤 죽는다.** heartbeat 는 브라우저가 보낸다 —
없으면 estopd 가 제대로 죽인다(`code -9`). CLI 로 시험하려면 heartbeat 를 따로 보낸다:

```bash
while true; do curl -s -X POST localhost:8000/api/estop/heartbeat -o /dev/null; sleep 0.4; done &
```

**2026-08-15 실기**: .42 단독(서버+클라이언트, gRPC 루프백)으로 790스텝 / 40초,
실제 19.90fps(목표 20). 원격 경로 자체는 이때 처음 끝까지 돌았다.

> ⚠ **배포로 백엔드를 재시작하면 장치 감시 기억이 비워진다.** v0.2.8 배포 직후
> 이미 뽑혀 있던 RealSense 가 조용했던 이유가 이것이었다. v0.2.9 에서 "남아 있는데
> 멈춘 세그먼트"도 아는 것으로 치게 고쳤지만, **배포 직후에는 경보 상태를 한 번
> 확인**하는 편이 좋다 — 재시작 전에 있던 문제가 그대로인지 알 수 있다.

### 두 번째 배포부터는 레이어를 **먼저 재보고** 정한다

v0.2.7 때 세 레이어를 다 올릴 뻔했는데, 확인해보니 데몬 레이어는 이미 같았다:

```bash
# 로컬과 타겟의 실제 내용을 비교한다 — 타임스탬프는 믿지 않는다
ssh <host> md5sum ~/.venvs/piper-daemons/lib/python3*/site-packages/piper_robot/hub.py \
                  ~/piper-web-deploy/current/daemons/robotd.py
md5sum robot/piper_robot/hub.py daemons/robotd.py
```

레이어별로 무엇이 바뀌었는지는 이렇게 본다:

```bash
git diff --name-only <이전태그>..HEAD -- backend/ frontend/ wrapper/ policies/ vendor/  # 이미지
git diff --name-only <이전태그>..HEAD -- bus/ shm/ cam/ rs/ robot/                      # wheel
git diff --name-only <이전태그>..HEAD -- daemons/ deploy/                               # 데몬 소스
```

### backend 만 바뀌어도 3.4GB 다 — 줄일 방법이 없다

v0.2.8 은 backend 파일 2개만 바뀌었는데도 3.4GB 를 보냈다. `docker save` 는 델타를
못 만들고, 바뀐 레이어(`COPY backend/`)만 골라 보낼 방법이 없다. `docker load` 가
있는 레이어를 건너뛰어도 **전송량은 그대로**다. 이걸 줄이려면 레지스트리가 필요하고,
그건 [미결정](#미결정) 항목이다.

### frontend 만 바뀌었으면 3.4GB 를 보내지 않는다

v0.2.6 이 그렇게 했다 — `docker save piper-web-frontend:<ver>` 만 하면 **27MB** 다.
backend 이미지가 11.2GB(압축 3.4GB)라 둘을 묶으면 매번 3.4GB 를 보내게 된다.
`docker load` 가 이미 있는 레이어는 건너뛰지만 **전송량은 안 줄어든다.**

---

## 미결정

- compose 파일을 `build:` → `image:` 참조로 바꿀지, 아니면 배포용으로 별도
  `docker-compose.release.yml`을 둘지 — 지금은 로드한 이미지를 `:latest`로 태깅해서 우회함
- 데몬 venv 표준 경로 (`~/.venvs/piper-daemons`) — 여러 호스트에 배포한다면
  경로를 고정하고 `install-daemons.sh`가 그 경로를 찾게 바꿀지 검토
- release 버전 태깅 규칙 — `v0.2.0`까지는 순번대로 감. 이미지 태그와 git 태그를 자동으로
  묶는 스크립트는 아직 없음 (수작업)
- `docker-compose.override.yml`의 포트 재배정을 release tarball에 템플릿으로 포함시킬지,
  아니면 매번 그 호스트의 빈 포트를 확인해서 손으로 만들지
- 호스트 `master` 브랜치에 남아있던 62커밋(ahead) — 이번 배포는 `wip/upgrade` 기준으로
  이미지를 만들어 우회했지만, 호스트에 남은 구버전 체크아웃 자체는 안 건드림
