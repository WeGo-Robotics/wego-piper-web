# 배포 절차 (드래프트) — 192.168.0.120 실기 배포 대비

> 이 문서는 아직 **개발 소스 배포**를 실제 제품 배포 절차로 옮기는 중간 기록이다.
> 2026-08-13, 로봇 호스트 `192.168.0.120`(sw-han-Thin-15-B13VE)를 대상으로 사전 점검한
> 결과를 남긴다. 아직 실행은 안 했고, 순서와 항목만 확정한 상태다 — [미결정](#미결정) 참고.

## 왜 "소스 체크아웃"이 아닌가

지금까지의 배포는 호스트에 저장소를 `git clone`/`pull`해서 그 자리에서 돌리는 방식이었다.
제품처럼 배포하려면 "무엇을 어떤 버전으로 올렸는지"가 커밋 로그가 아니라 **아티팩트**로
남아야 한다. 그래서 두 갈래로 나눈다.

## 아키텍처: 이미지 레이어 + 데몬 레이어

`docker-compose.yml`이 `privileged`/`/dev` 마운트를 뺀 이후, 하드웨어(CAN·카메라·RealSense)는
컨테이너가 아니라 **호스트에서 도는 Python 데몬**(`daemons/estopd.py` 등, systemd 유저 유닛)이
쥔다. 즉 "이미지 하나로 끝"이 아니라 두 레이어를 따로 배포해야 한다.

| 레이어 | 내용물 | 배포 방식 |
|---|---|---|
| **이미지** | backend, frontend (`docker-compose.yml`) | 로컬 빌드 → `docker save` → `scp` → 호스트 `docker load` |
| **데몬** | `bus/ cam/ rs/ robot/ shm/ phase/ vendor/*` (순수 파이썬, `daemons/*.py`가 import) | 로컬에서 wheel 빌드 → `scp` → 호스트 전용 venv에 `pip install` |

레지스트리(GHCR 등)는 안 쓰기로 했다 — 아직 설정된 게 없고, 로컬 빌드 후 `docker save`/`scp`가
지금 규모에는 더 간단하다는 판단.

---

## 절차

### 1. 릴리즈 소스 정리 (로컬)

- [ ] `wip/upgrade`의 미커밋 변경 정리 (지금 기준 4개 modified + 1 untracked)
- [ ] `origin/wip/upgrade`로 push (지금 기준 19커밋 앞서 있음, 미푸시)
- [ ] release 태그 (예: `v0.1.x`) — **주의**: 지난번 `v0.1.0` 태그를 잘못 찍어서 지운 적
      있다. 브랜치 정리와 push까지 끝난 뒤에 태그를 찍을 것.

### 2. 이미지 빌드 & 전달

- [ ] `docker compose build` (backend + frontend, 로컬)
- [ ] `docker save piper-web-backend piper-web-frontend | gzip > piper-web-<tag>.tar.gz`
- [ ] `scp`로 호스트에 전달
- [ ] 호스트에서 `docker load < piper-web-<tag>.tar.gz`
- [ ] 호스트에 `docker-compose.yml` + `backend/.env` 배포판(레포 전체 아님) 전달 — compose가
      `build:`가 아니라 `image:`를 보게 조정 필요 (지금 파일은 `build: context: .`로 돼 있어
      그대로 쓰면 호스트에서 다시 빌드하려 든다)

### 3. 데몬 레이어 wheel 빌드 & 전달

- [ ] 로컬에서 `bus/ cam/ rs/ robot/ shm/ phase/`, `vendor/lerobot_camera_pipershm/`,
      `vendor/lerobot_robot_pipershm/`, `vendor/wego_piper/` 각각 `python -m build --wheel`
      (전부 setuptools, 순수 파이썬 — 로컬 py3.13 / 호스트 py3.12 버전 차이 무관)
- [ ] wheel들 `scp`로 호스트에 전달
- [ ] 호스트에 데몬 전용 venv 생성:
      `python3 -m venv --system-site-packages ~/.venvs/piper-daemons`
      (`--system-site-packages`로 이미 깔려 있는 `numpy`/`opencv-python-headless`/`Pillow`/
      `piper-sdk`/`python-can` 재사용)
- [ ] venv에 wheel 설치 + 부족한 것 PyPI에서 설치: `redis`(파이썬 클라이언트),
      `pyrealsense2` (호스트 인터넷 됨 — pypi.org 200 확인함)
- [ ] `deploy/install-daemons.sh`를 이 venv를 activate한 셸에서 실행 (스크립트가
      "지금 셸의 python3"를 그대로 쓰기 때문 — `/deploy/install-daemons.sh:12` 참고)

### 4. 호스트 인프라 준비

- [ ] `sudo mkdir -p /srv/piper-data` (데이터 루트, 현재 없음)
- [ ] `redis-server` 설치 (현재 바이너리 자체가 없음) + `/etc/redis/redis.conf`에
      `unixsocket /run/redis/redis-server.sock` / `unixsocketperm 770` 추가
      (컨테이너는 유닉스소켓으로, 호스트 데몬들은 기본 TCP `127.0.0.1:6379`로 붙으므로
      **둘 다** 켜져 있어야 함)
- [ ] `nvidia-container-toolkit` 설치 (현재 없음 — GPU 패스스루 불가 상태)
- [ ] `nvidia-smi` 통신 실패 원인 확인 — 드라이버 패키지(`nvidia-driver-580-open`)는 깔려
      있는데 커널 모듈과 통신이 안 됨. 재부팅으로 해결되는지 우선 확인
- [ ] CAN: `can0` UP / `can1` DOWN — 이 배포가 듀얼암을 쓰는지에 따라 `can1` 기동 필요 여부
      결정
- [ ] v4l2loopback 커널 모듈 현재 미로드 — 런타임에 데몬이 올리는 게 맞는지 확인
      (RealSense udev 규칙은 이미 설치돼 있어 손댈 것 없음)

### 5. systemd 데몬 설치 + 상시 기동

- [ ] `deploy/install-daemons.sh` 실행 (estopd, robotd, camerad, rsd 전부)
- [ ] `loginctl show-user $USER`로 `Linger=yes` 확인, 아니면
      `sudo loginctl enable-linger $USER` (안 하면 로그아웃 시 데몬 전부 죽음 —
      [[project_linger_session_kill]] 참고)

### 6. 검증

- [ ] `docker compose up -d` 후 `http://192.168.0.120/` 접속
- [ ] `journalctl --user -u piper-robotd -f` 등으로 데몬 4개 다 살아있는지 확인
- [ ] E-stop, 카메라 프리뷰, CAN 연결 등 하드웨어 관련 항목은
      [refactor/HARDWARE-CHECKLIST.md](../refactor/HARDWARE-CHECKLIST.md) 절차를 새 호스트에서
      재실행

---

## 2026-08-13 사전 점검 스냅샷

| 항목 | 상태 |
|---|---|
| Docker / Compose | 로컬 29.1.3 / 호스트 29.3.1, 둘 다 compose 플러그인 있음, sw-han이 docker 그룹 |
| 아키텍처 | 로컬/호스트 둘 다 x86_64 — 크로스 빌드 불필요 |
| GPU 하드웨어 | RTX 4050 Max-Q 있음 (lspci 확인) |
| nvidia-container-toolkit | 미설치 |
| nvidia-smi | 드라이버 있는데 커널과 통신 실패 |
| CAN | can0 UP, can1 DOWN |
| v4l2loopback | 미로드, `/dev/video0-8`는 있음 |
| RealSense udev 규칙 | 설치됨 (`99-realsense-libusb.rules`) |
| redis-server | 미설치 (바이너리 없음) |
| `/srv/piper-data` | 없음 |
| systemd 데몬 유닛 | 없음 (호스트 저장소가 이 기능이 없는 `master` 브랜치라서) |
| 호스트 파이썬 | 3.12.3 (`/usr/bin/python3`), PEP 668 externally-managed |
| 이미 설치돼 있는 것 | `numpy`, `opencv-python-headless`, `Pillow`, `piper-sdk 0.6.1`, `python-can 4.6.1` |
| 없는 것 | `redis`(py client), `pyrealsense2` (poetry 캐시에 cp312 wheel은 있음) |
| 인터넷 | 됨 (pypi.org 200) |
| 호스트 저장소 상태 | `master` 브랜치, `1b19fbf`, origin과 ahead 62 / behind 49, `.claude/settings.json` 로컬 수정 있음 |

---

## 미결정

- compose 파일을 `build:` → `image:` 참조로 바꿀지, 아니면 배포용으로 별도
  `docker-compose.release.yml`을 둘지
- 데몬 venv 표준 경로 (`~/.venvs/piper-daemons` 가제) — 여러 호스트에 배포한다면
  경로를 고정하고 `install-daemons.sh`가 그 경로를 찾게 바꿀지 검토
- release 버전 태깅 규칙 (`v0.1.x` 계속 쓸지, 이미지 태그와 git 태그를 어떻게 묶을지)
- `can1`을 이 배포에서 쓰는지 (듀얼암 여부) — 사람 확인 필요
- 호스트 `master` 브랜치에 남아있는 62커밋(ahead)이 뭔지 — 그냥 두고 새 브랜치로 덮을지,
  따로 볼 게 있는지 확인 필요
