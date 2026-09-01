# Piper Web

LeRobot 웹 인터페이스 — 로봇 모방학습 프레임워크를 웹에서 제어.

데이터 수집(에피소드 녹화), 추론·평가(체크포인트 배포, 실시간 파라미터 튜닝),
학습 모니터링, 비전 검출, E-stop 안전 정지.

## 설치

**스크립트 하나를 받아서 실행한다. 그게 전부다.**

```bash
curl -fsSLO https://raw.githubusercontent.com/WeGo-Robotics/wego-piper-web/master/deploy/piper-install.sh
chmod +x piper-install.sh
./piper-install.sh
```

> ⚠ **아직 올라가지 않았다.** 저장소는 공개지만 `master` 가 비어 있고, 이미지도
> 아직 발행 전이다 — 위 명령은 그 둘이 올라간 뒤에 동작한다. 그 전에는 주소를
> 직접 넘긴다: `PIPER_IMAGE=<레지스트리>/piper-web-backend ./piper-install.sh`

나머지는 전부 이미지 안에 있다 — 데몬·wheel·udev 규칙·compose·설치 스크립트까지.
파일을 여러 개 주면 하나를 빠뜨리고, 빠뜨린 것은 늘 나중에 엉뚱한 증상으로 드러난다.

스크립트가 하는 일:

1. **도커를 확인한다** — 없으면 설치 명령을 찍고 멈춘다
2. **이미지를 받는다** (`docker pull`) — 이미 가진 레이어는 안 받는다
3. **호스트 코드를 꺼낸다** — `docker create` 로 컨테이너를 **실행하지 않고** 파일만
4. **설치한다** — 꺼낸 `apply.sh` 가 전제 확인·udev·데몬 유닛·컨테이너까지

⚠ **전제가 빠져 있으면 명령을 찍고 멈춘다.** sudo 가 필요한 것은 스크립트가 직접
하지 않는다 — 무엇이 바뀌었는지 모르는 채로 설치가 끝나는 편이 더 나쁘다.
그 명령만 실행하고 다시 부르면 된다.

### 업데이트도 같은 명령이다

```bash
./piper-install.sh              # 최신
./piper-install.sh v0.3.10      # 특정 버전
./piper-install.sh --check      # 아무것도 안 바꾸고 상태만
```

이미 돼 있는 것은 건너뛴다. 두 번 돌려도 같다. 두 번째 설치부터는 바뀐 레이어만
받으므로 **~100MB** 다.

### 스크립트가 확인하는 것

못 맞추면 멈추고 무엇을 해야 하는지 찍는다.

| 항목 | 기준 | 못 맞추면 |
|---|---|---|
| docker · docker compose v2 | 있을 것 | `apt install docker.io docker-compose-v2` |
| docker 그룹 | 사용자가 속할 것 | `usermod -aG docker $USER` + **다시 로그인** |
| python3-venv · redis-server | 있을 것 | `apt install python3-venv redis-server` |
| **GPU 컴퓨트 능력** | **7.5 이상** (Turing / RTX 20xx·T4) | **GPU 를 바꿔야 한다** — Pascal·Volta 는 드라이버를 올려도 안 된다 |
| 드라이버 CUDA | **13.0 이상** | `apt install nvidia-driver-580` + 재부팅 |
| nvidia-container-toolkit | 있을 것 | NVIDIA 저장소를 먼저 붙여야 한다 (Ubuntu 아카이브에 없다) |
| udev 규칙 2종 | 설치돼 있을 것 | 번들에 들어 있다 — `cp` 명령을 찍어 준다 |
| redis 유닉스 소켓 · linger | 켜져 있을 것 | 설정 명령을 찍어 준다 |

⚠ **GPU 하한이 있다.** 이미지의 torch 는 cu130 빌드라 컴파일된 아키텍처가
`sm_75·80·86·90·100·120` 뿐이고 **PTX 가 없어 JIT 으로도 못 메꾼다.** 값이 바뀌면
컨테이너에 직접 물어서 확인한다:

```bash
docker run --rm --gpus all --entrypoint python piper-web-backend:latest \
  -c 'import torch; print(torch.cuda.get_arch_list())'
```

⚠ **팔을 쓴다면 CAN 이름 규칙을 만들어야 한다.** 규칙이 없으면 인터페이스가
커널 열거 순서대로 붙어, 포트를 바꿔 꽂거나 부팅 순서가 달라지는 순간 **두 팔의
이름이 뒤바뀐다** — 등록·슬롯·프리셋이 이름을 키로 쓰므로 **저장된 설정이 반대
팔을 가리킨다.**

⚠ **규칙은 배포물에 안 들어 있다.** 시리얼이 어댑터마다 달라서, 남의 시리얼이 든
규칙은 이 머신에서 아무 줄도 매칭되지 않는다 — 그런데 udev 는 그걸 에러로 치지
않아 증상이 "팔 0개" 뿐이다. **이 머신에서 만든다:**

```bash
cd ~/piper-web-deploy/<버전>/udev
python3 list-can-adapters.py --watch     # 팔을 움직여 어느 쪽이 어느 팔인지 확인
python3 list-can-adapters.py --write-rule | sudo tee /etc/udev/rules.d/99-piper-can.rules
sudo nano /etc/udev/rules.d/99-piper-can.rules   # can_arm1/2 → can_leader1/can_follower1
sudo udevadm control --reload-rules              # 그 뒤 어댑터를 다시 꽂는다
```

⚠ 이름(`can_arm1`)은 **임시다.** 어느 쪽이 어느 팔인지는 시리얼로도 펌웨어로도
알 수 없다 — 움직여 본 사람만 안다. 그럴듯한 이름을 지어내는 것보다 임시 이름을
두고 고치게 하는 편이 안전하다.

## 무엇이 어디서 도는가

**도커는 게이트웨이와 프론트만 감싼다.** 하드웨어 층은 어느 쪽이든 호스트다 —
CAN·USB·커널 모듈은 컨테이너가 다룰 수 있는 것이 아니다.

| | 어디서 |
|---|---|
| 게이트웨이 · 프론트엔드 | **컨테이너** |
| estopd · robotd · camerad · rsd | **호스트 systemd 유닛** |
| Redis | 호스트 (유닉스 소켓 — 버스를 네트워크에 안 연다) |

셋은 서로 `/dev/shm` 세그먼트와 Redis 버스로만 만난다.

## 데이터는 한 곳이다

| 호스트 | 컨테이너 | 용도 |
|---|---|---|
| `${PIPER_DATA_ROOT:-/srv/piper-data}` | `/data` | 데이터 전부 (hf 캐시·outputs·logs·config) |
| `/run/redis` | `/run/redis` | 버스 유닉스 소켓 |

⚠ **호스트의 `~/.cache/huggingface` 와 `~/.config/piper-web` 은 일부러 안 마운트한다.**
호스트 절대경로가 컨테이너로 새어 들어가면 환경마다 경로가 갈린다. 내부 레이아웃과
각 `PIPER_*` 경로는 `backend/Dockerfile` 의 "데이터 경로 계약" ENV 에 있다.

`ipc: host` 로 `/dev/shm` 을 호스트와 공유한다 — 이게 없으면 세그먼트가 안 보인다.

⚠ **다시 깔아도 데이터는 안 지운다.** `/srv/piper-data`,
`~/.cache/huggingface/lerobot`(녹화한 데이터셋), `~/.config/piper-web` 셋은 그대로 둔다.

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

## 그 밖

| | 어디 |
|---|---|
| 개발 (저장소에서 직접 실행) | [CLAUDE.md](CLAUDE.md) |
| 릴리스 (이미지 굽기·배포) | [deploy/RELEASE-CHECKLIST.md](deploy/RELEASE-CHECKLIST.md) |
| 라이선스 | [LICENSE](LICENSE) (Apache-2.0) · [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) |
