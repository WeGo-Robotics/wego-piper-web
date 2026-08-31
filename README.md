# Piper Web

LeRobot 웹 인터페이스 — 로봇 모방학습 프레임워크를 웹에서 제어.

## 실행 방법

### 요구사항

- Node.js 20+ (nvm use 24 권장)
- Python 3.11+
- LeRobot 0.5+ (`pip install lerobot`)
- **Redis** — 데몬끼리 만나는 버스다. 없으면 웹이 "데몬 없음"만 띄운다
- `video`·`dialout` 그룹 — 카메라와 USB-CAN 접근

### 설치

```bash
./deploy/install.sh          # 설치
./deploy/install.sh --check  # 확인만
```

sudo 가 필요한 것(시스템 패키지·그룹·udev)은 **명령을 찍어 주고 사람이 실행**한다.
스크립트가 몰래 sudo 를 쓰면 무엇이 바뀌었는지 아무도 모른다.

<details>
<summary>스크립트가 하는 일 (수동으로 하려면)</summary>

```bash
# 1. 서브모듈 — URDF. 지오메트리를 **다시 구울 때만** 필요하다(구운 npz 는 저장소에 있다)
git submodule update --init --recursive

# 2. 파이썬 패키지 — **순서가 있다**
#    ⚠ `pip install -e backend/` 만으로는 안 된다. backend 는 아래 것들에
#      의존하지 않으므로 하나도 안 딸려온다.
for p in bus shm robot cam rs phase act_aux \
         vendor/wego_piper vendor/lerobot_robot_piper \
         vendor/lerobot_robot_pipershm vendor/lerobot_camera_pipershm; do
  pip install -e "$p"
done
pip install -e "backend[dev]"

# 3. 설정 — ⚠ **코드 기본값이 `direct` 라 안전층이 빠진다**
cp deploy/env.example backend/.env

# 4. udev — CAN 어댑터 이름 고정 + RealSense 접근
sudo cp deploy/udev/99-piper-can.rules backend/udev/99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload

# 5. 데몬 + 로그아웃해도 살아남게
./deploy/install-daemons.sh estopd robotd camerad rsd gateway frontend
sudo loginctl enable-linger $USER

# 6. 프론트엔드
cd frontend && npm install
```
</details>

⚠ **CAN 어댑터 이름은 udev 로 고정해야 한다.** 규칙이 없으면 커널이 열거 순서대로
`can0..can3` 을 붙이고, 그 순서는 다시 꽂을 때마다 바뀐다 — 어제의 `can1` 이 오늘은
다른 팔이 된다. `deploy/udev/99-piper-can.rules` 는 **시리얼로** 이름을 박는다.
어댑터를 늘렸으면 `deploy/udev/list-can-adapters.py` 로 시리얼을 뽑아 규칙에 추가한다.

### 개발 서버 (백엔드 + 프론트엔드 동시)

```bash
./dev.sh
```

브라우저에서 http://localhost:5173 접속.

### 개별 실행

```bash
# 백엔드
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 프론트엔드 (별도 터미널)
cd frontend
npm run dev
```

### 빌드

```bash
cd frontend
npm run build       # 프로덕션 빌드
npx tsc --noEmit    # 타입 체크
```

## Docker 배포

전체 스택(백엔드 + LeRobot + Piper 플러그인 + 프론트엔드)을 컨테이너로 제공한다.
호스트 환경(Python 3.13 / torch 2.11.0+cu130 / lerobot 0.5.0)을 그대로 재현하며,
**로봇이 물리적으로 연결된 호스트**에서만 실행한다 (하드웨어 직접 접근이 필요).

### 사전 요구사항 (호스트)

⚠ **컨테이너는 장치를 하나도 안 연다.** 카메라도 팔도 **호스트 데몬**이 쥐고,
컨테이너는 `/dev/shm` 세그먼트와 Redis 로만 붙는다. 그래서 아래가 다 갖춰지지
않으면 웹은 뜨지만 **카메라도 팔도 안 보인다.**

- Docker + Docker Compose v2
- [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) (GPU 패스스루)
- **하드웨어 데몬을 호스트에서 기동** — 이게 빠지면 아무것도 안 보인다
  ```bash
  ./deploy/install-daemons.sh estopd robotd camerad rsd
  sudo loginctl enable-linger $USER
  ```
- **호스트 redis 에 유닉스 소켓 켜기** (`/etc/redis/redis.conf`) — 기본값은 주석 처리돼 있다
  ```
  unixsocket /run/redis/redis-server.sock
  unixsocketperm 770
  ```
  TCP 로 안 여는 이유: 버스가 **E-stop heartbeat 을 나른다.**
- 데이터 루트: `sudo mkdir -p /srv/piper-data` (경로는 `PIPER_DATA_ROOT` 로 바꾼다)
- CAN 인터페이스 호스트에서 구성 — 커널/네트워크 레벨이라 컨테이너 밖.
  udev 규칙으로 이름을 고정한다(위 [설치](#설치) 참고)
- `v4l2loopback` 커널 모듈 호스트에 로드 — 커널 모듈은 컨테이너에서 삽입 불가
- RealSense udev 규칙: `sudo cp backend/udev/99-realsense-libusb.rules /etc/udev/rules.d/ && sudo udevadm control --reload`

### 내부 패키지 (vendor/)

`lerobot_robot_piper`, `wego_piper` 는 공개 PyPI에 없어 `vendor/` 에 스냅샷으로 포함되어 있다.
소스가 바뀌면 [vendor/README.md](vendor/README.md) 의 갱신 방법을 따라 다시 떠야 한다.

### 이미지가 둘로 갈라져 있다

| 파일 | 만드는 것 | 내용 | 언제 다시 굽나 |
|---|---|---|---|
| [backend/Dockerfile.base](backend/Dockerfile.base) | `piper-web-base:<BASE_VERSION>` | 데비안·apt·venv·lerobot·torch(cu130)·piper_sdk·grpcio — **서드파티만, ~7GB** | 서드파티 스택이 바뀔 때만 |
| [backend/Dockerfile](backend/Dockerfile) | `piper-web-backend` | bus·shm·robot·vendor·backend·wrapper·policies — **회사 코드만, ~390MB** | 릴리스마다 |

⚠ **베이스에는 `COPY` 를 넣지 마라.** [build-base.sh](deploy/build-base.sh) 가 컨텍스트 없이
굽기 때문에 넣는 순간 빌드가 실패한다 — 회사 코드가 안 섞여야 이 이미지를 공개
레지스트리에 올릴 수 있다.

⚠ **베이스는 다이제스트로 박혀 있다.** `python:3.13-slim-bookworm` 은 뜬 태그라
데비안 패치마다 다른 이미지가 되고, 그러면 1번 레이어부터 갈려 아래 전부가 다시
구워진다. 실제로 그랬다 — v0.3.8 과 v0.3.9 는 **레이어 24개 중 24개가 달랐다.**
베이스를 올릴 때는 `BASE_VERSION` 과 다이제스트를 같이 바꾼다:

```bash
docker buildx imagetools inspect python:3.13-slim-bookworm | grep Digest
```

### 실행

```bash
./deploy/build-base.sh            # 베이스 (없을 때만 굽는다. 낡았으면 알아서 다시)
docker compose up -d --build      # 앱 빌드 + 기동
docker compose logs -f backend    # 백엔드 로그
docker compose down               # 정지
```

⚠ 베이스 없이 `docker compose build` 를 부르면 `pull access denied for
piper-web-base` 로 죽는다. `release.sh` 는 알아서 먼저 확인한다.

브라우저에서 `http://<호스트IP>/` 접속 (nginx `:80` → bridge 네트워크의 `backend:8000` 프록시).

### 볼륨 — **두 개뿐이다**

| 호스트 | 컨테이너 | 용도 |
|---|---|---|
| `${PIPER_DATA_ROOT:-/srv/piper-data}` | `/data` | 데이터 전부 (hf 캐시·outputs·logs·config) |
| `/run/redis` | `/run/redis` | 버스 유닉스 소켓 |

⚠ **호스트의 `~/.cache/huggingface` 와 `~/.config/piper-web` 은 일부러 안 마운트한다.**
호스트 절대경로가 컨테이너로 새어 들어가면 환경마다 경로가 갈린다. 내부 레이아웃과
각 `PIPER_*` 경로는 `backend/Dockerfile` 의 "데이터 경로 계약" ENV 에 있다.

`ipc: host` 로 `/dev/shm` 을 호스트와 공유한다 — 이게 없으면 세그먼트가 안 보인다
(`/dev/shm` 은 컨테이너마다 격리된다).

### 호스트 설치와 무엇이 다른가

| | 호스트 설치 | 도커 |
|---|---|---|
| 게이트웨이·프론트 | systemd 유닛 | **컨테이너** |
| estopd·robotd·camerad·rsd | systemd 유닛 | **똑같이 호스트 systemd 유닛** |
| Redis | TCP | **유닉스 소켓** (버스를 네트워크에 안 연다) |
| 파이썬 패키지 | `./deploy/install.sh` | 이미지 안에 구움 |

즉 **도커는 게이트웨이·프론트만 감싼다.** 하드웨어 층은 어느 쪽이든 호스트다 —
CAN·USB·커널 모듈은 컨테이너가 다룰 수 있는 것이 아니다.

> GPU가 안 잡히면 compose 의 `deploy.resources...` 대신 `runtime: nvidia` 를 사용한다.
> 호스트 `:80` 이 이미 사용 중이면 충돌한다 (frontend 가 그 포트만 낸다).

## 실기 배포 — 따라 하기

빌드 머신에서 번들을 만들고, 로봇 호스트에서 한 번 적용한다.
**어느 레이어를 올릴지는 사람이 정하지 않는다** — 직전 태그와의 diff 가 정한다.

### 1. 빌드 머신

```bash
./deploy/release.sh v0.3.9 --dry-run   # 무엇이 올라갈지 먼저 본다
./deploy/release.sh v0.3.9             # 번들 하나 (dist/piper-web-v0.3.9.tar.gz)
scp dist/piper-web-v0.3.9.tar.gz <호스트>:~/
```

`--dry-run` 이 이렇게 답한다:

```
릴리스 v0.3.9  (직전 v0.3.8, 파일 171 개 변경)
  backend 이미지 : 예
  frontend 이미지: 예
  데몬 wheel     : bus cam robot rs shm
  데몬 소스·유닛 : 예
```

### 2. 로봇 호스트

```bash
tar xzf piper-web-v0.3.9.tar.gz
./v0.3.9/apply.sh            # 첫 설치·업데이트 **같은 명령**
```

sudo 가 필요한 것이 남아 있으면 **명령을 찍고 멈춘다.** 그것만 실행하고 다시 부른다:

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 python3-venv redis-server
sudo usermod -aG docker $USER          # 다시 로그인해야 반영된다
sudo mkdir -p /srv/piper-data && sudo chown $USER /srv/piper-data
sudo sed -i 's|^# *unixsocket |unixsocket |' /etc/redis/redis.conf
sudo systemctl restart redis-server
sudo loginctl enable-linger $USER
sudo cp ~/v0.3.9/udev/*.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
./v0.3.9/apply.sh
```

⚠ **`nvidia-container-toolkit` 은 위 apt 한 줄에 없다.** Ubuntu 아카이브에 없어서
[NVIDIA 저장소](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)를
먼저 붙여야 한다 — 같이 넣으면 "패키지를 찾을 수 없음" 으로 **그 줄 전체가 실패해**
docker 도 redis 도 안 깔린다. 없으면 compose 의 GPU 예약이 실패한다.

⚠ **GPU 에 하한이 있다.** 이미지가 싣는 torch 는 cu130 빌드라 컴파일된 아키텍처가
`sm_75·80·86·90·100·120` 뿐이고 **PTX 가 없어 JIT 으로도 못 메꾼다.**

| 항목 | 기준 | 못 맞추면 |
|---|---|---|
| 컴퓨트 능력 | **7.5 이상** (Turing / RTX 20xx·T4) | **GPU 를 바꿔야 한다.** Pascal(GTX 10xx)·Volta(V100)는 드라이버를 올려도 안 된다 |
| 드라이버 CUDA | **13.0 이상** | `sudo apt install nvidia-driver-580` + 재부팅 |

`apply.sh` 가 둘 다 확인하고 **처방을 갈라서** 찍는다 — 드라이버만 올려보다 시간을
버리는 일이 없도록. 값이 바뀌면 컨테이너에 직접 물어서 고친다:

```bash
docker run --rm --gpus all --entrypoint python piper-web-backend:latest \
  -c 'import torch; print(torch.cuda.get_arch_list())'
```

⚠ **udev 규칙은 번들에 들어 있다** — 저장소를 안 받아도 된다. 없으면 조용히
실패한다: RealSense 는 libusb 로 장치를 못 열어 **카메라가 0개**로 잡히고, CAN 은
`can0`/`can1` 로 붙어 **저장된 팔 등록이 반대 팔을 가리킨다.**

⚠ **CAN 규칙의 시리얼은 번들을 구운 머신의 배선이다.** 어댑터가 다르면 어느 줄도
매칭되지 않는데 udev 는 그걸 에러로 안 친다 — `apply.sh` 가 꽂힌 어댑터와 대조해
경고한다. 경고가 나오면 규칙을 이 머신에 맞게 고친다:

```bash
python3 v0.3.9/udev/list-can-adapters.py           # 시리얼 ↔ 인터페이스
python3 v0.3.9/udev/list-can-adapters.py --watch   # 팔을 움직여 어느 쪽인지 확인
```

### 3. 확인

```bash
./v0.3.9/apply.sh --check    # 아무것도 안 바꾸고 상태만 본다
```

### 처음부터 다시 깔려면

⚠ **데이터를 지우지 않는다.** 아래 셋은 그대로 둔다:

| 경로 | 내용 |
|---|---|
| `/srv/piper-data` | 컨테이너가 쓰는 모델·설정·로그 |
| `~/.cache/huggingface/lerobot` | **녹화한 데이터셋** |
| `~/.config/piper-web` | 사용자 설정 |

⚠ **`docker-compose.override.yml` 을 먼저 빼돌린다.** 그 호스트의 포트 사정이
거기 있다 — 192.168.0.120 은 `:80` 을 WMS 가 쓰고 있어 8081 로 빼 두었다.
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
| 전송 | 2분 31초 (LAN) |
| 정리 | 유닛·컨테이너·이미지 전부 0, 디스크 **175G → 243G** |
| `docker load` | 11GB — 이 단계가 제일 오래 걸린다 |
| 재설치 | 데몬 4개 active, 컨테이너 2개 up |
| 확인 | `/health` 200, 카메라 1대 인식 |

`~/piper-web-deploy` 가 **58GB** 였다 — 릴리스마다 이미지 tar 가 쌓인다.
정기적으로 지울 값어치가 있다.

### 세 레이어가 있는 이유

컨테이너는 **장치를 하나도 안 연다.** CAN·USB·커널 모듈은 컨테이너가 다룰 수
있는 것이 아니라, 하드웨어는 호스트 데몬이 쥐고 컨테이너는 `/dev/shm` 과
Redis 로만 붙는다. 그래서 올릴 것이 셋이다:

| 레이어 | 무엇이 바뀌면 | 어디로 |
|---|---|---|
| 이미지 | `backend/ frontend/ wrapper/ policies/ phase/ vendor/ act_aux/` | `docker load` |
| 데몬 wheel | `bus/ shm/ robot/ cam/ rs/` | 호스트 venv |
| 데몬 소스·유닛 | `daemons/ deploy/systemd/` | `~/piper-web-deploy/current` + systemd |

`bus`·`shm`·`robot` 은 **양쪽**이다 — 이미지 안에도 들어가고 호스트 venv 에도
깔린다. 한쪽만 올리면 컨테이너와 데몬이 같은 라이브러리의 다른 코드로 돈다.

## 추론 로그

### CSV 로깅

gRPC 추론 실행 시 매 스텝 자동 기록됩니다.

- **파일 위치**: `/tmp/piper_inference_YYYYMMDD_HHMMSS.csv`
- **종료 시** WebSocket으로 파일 경로 알림

#### 기록 컬럼

| 구분 | 컬럼 | 설명 |
|------|------|------|
| 메타 | `timestamp`, `step`, `fps`, `queue_size` | 시간, 스텝, FPS, 액션 큐 잔량 |
| 정책 출력 | `target_{joint}.pos` | 필터 적용 전 원본 액션 |
| 전송값 | `filtered_{joint}.pos` | 속도 제한 + 저역통과 등 필터 후 실제 전송 |
| 로봇 위치 | `actual_{joint}.pos` | 로봇이 보고한 현재 관절 위치 |
| 기타 | `task`, `paused` | 태스크 문자열, 일시정지 여부 |

### API

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /api/logs` | CSV 로그 파일 목록 |
| `GET /api/logs/download/{filename}` | CSV 파일 다운로드 |

### 분석 도구

`tools/analyze_inference_log.py` — pandas + matplotlib 기반 시각화.

```bash
# 통계 + 그래프 (GUI)
python tools/analyze_inference_log.py /tmp/piper_inference_20260404_213500.csv

# 특정 관절만
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --joint joint1

# PNG 저장 (GUI 없이, 서버 환경)
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --save

# 통계만 출력
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --no-plot
```

#### 출력 예시

```
============================================================
Duration: 45.2s | Steps: 1356 | Avg FPS: 30.0
Queue empty: 226/1356 (16.7%)
============================================================
Joint               |target-actual| mean        max        std
------------------------------------------------------------
joint1.pos                         1.234      5.678      0.891
...

Joint               |target-filtered| mean        max
----------------------------------------------------
joint1.pos                           0.456      2.345
...
```

#### 생성 파일 (`--save` 옵션)

| 파일 | 내용 |
|------|------|
| `*_overview.png` | 관절별 target / filtered / actual 비교 |
| `*_error.png` | 관절별 추적 오차 (target - actual) |
| `*_fps_queue.png` | FPS + 액션 큐 사이즈 시계열 |
