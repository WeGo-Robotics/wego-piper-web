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
| **데몬 라이브러리** | `bus/ cam/ rs/ robot/ shm/ so101/ sim/` (순수 파이썬, `daemons/*.py`가 직접 import) | 로컬에서 wheel 빌드 → `scp` → 호스트 전용 venv에 `pip install`. so101·sim 의 바깥 의존(feetech-servo-sdk·mujoco)은 apply.sh 가 PyPI 에서 |
| **데몬 소스 + 유닛 정의** | `daemons/*.py`, `deploy/systemd/*.service`, `deploy/install-daemons.sh` | 소스 그대로 tar로 묶어 `scp` (이건 wheel이 아니라 daemons/의 엔트리포인트 자체라 패키징 대상이 아님) |

`phase/`, `vendor/*`는 데몬이 import하지 않는다 — `backend/Dockerfile`이 이미지 빌드 때
같이 넣으므로 별도 wheel 불필요 (`bus/shm/robot/so101/phase/vendor`만 COPY, `cam/rs/sim`은 호스트 전용 —
so101 은 데몬이 호스트여도 관절 매핑 표를 게이트웨이 릴레이·컨테이너 안 녹화 프로세스가 읽어 양쪽이다).

레지스트리는 둘 다 쓴다 (처음엔 "안 쓰기로 했다"였는데 뒤집었다 — tar 는 무엇을
고쳤든 매번 3.46GB 를 옮기기 때문이다):

- **로컬 레지스트리** (`registry.sh`, `PIPER_REGISTRY=piper-build:5000`) — 현장망 배포
- **GHCR** (`PIPER_REGISTRY=ghcr.io/wego-robotics` — `piper-install.sh` 의 기본 주소와
  같아야 한다; 처음엔 `ghcr.io/swhan-wego` 였다) — 외부망. v0.4.3 부터 올라가 있다.
  push 는 `docker login ghcr.io` (classic PAT, `write:packages`) 후 release.sh 그대로.
  ⚠ 패키지는 **private** 이다 — 호스트가 GHCR 에서 직접 pull 하려면 호스트에도
  `read:packages` 전용 토큰으로 `docker login` 이 필요하다. push 용 토큰을
  호스트에 두지 말 것.

---

## 릴리스 규칙 — **배포판은 개발 머신이 아니다**

2026-09-15 하루에 같은 부류의 사고가 셋 났다: 로봇 등록 [UP] 이 sudo 를 물었고, 회색 카드
보정이 "camerad 가 응답하지 않습니다" 였고, 카메라 프로파일 저장이 500 이었다. 증상은 셋 다
달랐지만 원인은 하나다 — **개발 머신에는 있고 배포판에는 없는 것.** 개발 머신은 게이트웨이가
저장소에서 직접 돌고 호스트에 온갖 것이 깔려 있다. 배포판은 컨테이너 안이고, 데몬은 호스트의
사용자 유닛이며, 그 사이에 있는 것은 버스와 `/dev/shm` 뿐이다.

그래서 **기능을 만들 때가 아니라 릴리스할 때** 아래를 본다. 규칙마다 그걸 지키는 테스트를
달았다 — 규칙이 문서에만 있으면 다음에 또 빠져나간다.

| # | 규칙 | 왜 (겪은 일) | 지키는 것 |
|---|---|---|---|
| R1 | **게이트웨이가 import 하는 데몬 패키지는 이미지에 깐다** | 이미지가 `cam` 을 "데몬 전용"이라며 뺐는데 게이트웨이가 `piper_cam` 을 import 한다 → 배포판에서만 프로파일 저장 500 | `test_every_daemon_package_the_gateway_imports_is_installed_in_the_image` — 사람의 선언이 아니라 **소스의 import 에서** 규칙을 끌어낸다 |
| R2 | **장치·네트워크 네임스페이스가 필요한 조회는 데몬 RPC 를 거친다** | 컨테이너에는 `can0` 이 없다. 포트 카드가 `ip` 를 직접 불러 bitrate 가 빈칸이었고(초기화 안 된 것처럼 보였다), [DOWN] 은 `Cannot find device "can0"`, 버스 가드는 BUS-OFF 를 못 읽고 **조용히 통과**했다 | `test_bringing_a_can_interface_down_goes_through_robotd_like_bringing_it_up` · `test_the_teleop_bus_guard_asks_robotd_instead_of_failing_open` · `test_the_ports_card_reads_bus_stats_from_robotd_not_from_the_container` |
| R3 | **데몬 venv 의 바깥 의존은 설치가 직접 깔고, 점검에서도 본다** | `--system-site-packages` 로 "시스템에 있겠지" 로 뒀는데 맨 우분투엔 없었다 → [연결] 이 `piper_sdk not installed` | `test_the_install_puts_piper_sdk_in_the_daemon_venv_and_checks_it` — 컨테이너와 **같은 핀**인지까지 대조 |
| R4 | **매니페스트는 "이번에 바뀐 것"일 뿐 — 없거나 낡으면 깐다. 그리고 기동할 때 맞는지 본다** | 데몬을 실은 릴리스를 건너뛴 기계는 영영 못 따라잡았다(camerad 가 9월 1일자로 남아 회색 카드가 죽었다). wheel 은 v0.4.15 에 같은 함정을 이미 겪었다. ⚠ 게다가 그 불일치를 **아무도 말하지 않았다** — 판정이 버전 카드(브라우저)에만 있었고 wheel 만 봤다 | `test_a_fresh_host_installs_everything_the_bundle_carries` · `test_a_host_that_skipped_a_release_still_catches_up_on_the_daemon_source` · `test_a_restart_checks_that_the_daemons_match_the_release_and_says_so` · `test_a_host_that_skipped_a_release_still_catches_up_on_the_wheels` |
| R5 | **실패는 진짜 사유를 말한다** | 한 문구가 죽음·타임아웃·**동사를 모름**(옛 데몬)을 다 덮어, 사람이 데몬을 재시작하며 시간을 버렸다 — 고칠 곳은 데몬 갱신이었다 | `test_a_daemon_that_does_not_know_the_verb_does_not_look_like_a_dead_one` |
| R6 | **스크립트는 sudo 를 직접 쓰지 않는다. 처방은 복사하면 끝나야 한다** | 줄 앞에만 `sudo` 가 붙어 `&&` 뒤가 일반 사용자로 돌았다 | `test_apply_never_runs_sudo_itself` · `test_every_chained_sudo_prescription_carries_sudo_on_each_part` · `test_every_sudo_prescription_apply_prints_is_in_the_doc` |
| R7 | **태그가 빌드보다 먼저. 버전은 최하위 자리만. 병행 세션을 확인한다** | 태그 없이 구우면 판정이 뒤로 밀린다. 같은 날 다른 세션이 릴리스를 내므로 `git tag --sort=-v:refname`·원격 태그·CHANGELOG 첫 항목을 대조하고, `git log v<최신>..HEAD` 의 남의 커밋도 CHANGELOG 에 싣는다 | 아래 원터치 절 · [0단계](#0-버전-번호--최하위-자리만-올린다) |
| R9 | **반복되는 절차는 스크립트가 한다 — 사람이 환경변수로 고르지 않는다** | v0.5.5 를 사설 레지스트리로만 올려 GHCR 에서 받는 호스트가 옛 버전을 "최신" 이라 봤다. 화면은 맞았고 틀린 것은 릴리스였다 — 선택이 스크립트 밖에 있었다 | `test_the_offline_path_still_exists`(`DEFAULT_REGISTRIES` 가 있는지) · `test_a_public_registry_is_not_pushed_through_localhost` · 스크립트가 push 뒤 레지스트리에 **다시 물어본다** |
| R10 | **적용한 호스트가 하나라도 있으면 같은 번호를 다시 쓰지 않는다** | 호스트의 "새 버전 확인"은 버전 **문자열**을 견준다. 같은 번호로 다시 끊으면 `v0.5.5 == v0.5.5` 라 "최신입니다" 라고 답하고 **스스로는 영영 안 받는다** — 그 기계마다 손으로 다시 깔아야 한다. v0.5.5 는 세 번 끊었는데, 앞의 둘은 아무도 안 받은 상태라 괜찮았고 **세 번째는 .120 이 이미 받은 뒤였다** | `test_the_release_warns_before_reusing_a_published_version` — `release.sh` 가 굽기 전에 레지스트리에 물어보고 말한다(막지는 않는다 — 아무도 안 받았으면 다시 끊어도 된다) |
| R8 | **릴리스 뒤 이미지 안을 열어 확인한다** | 매니페스트·wheel·데몬 소스·apply.sh 는 **이미지 안으로** 나간다. 굽고 나서 보지 않으면 "고쳤다고 믿는 것"이 안 실려 나간다 | 아래 확인 명령 |

### R8 — 굽고 나서 이 한 번

```bash
docker run --rm piper-web-backend:<태그> sh -c '
  grep -E "^(version|images|wheels|daemons)=" /opt/piper-host/manifest.txt
  ls /opt/piper-host/wheels | head
  python -c "import piper_cam, piper_bus, piper_shm; print(\"import OK\")"
  grep -c "이번에 고친 표시" /opt/piper-host/apply.sh'
```

고친 것이 호스트에서 도는 것이면(데몬·apply.sh·wheel) **그 파일이 번들 안에 있는지**를 본다.
고친 것이 게이트웨이에서 도는 것이면 **컨테이너 안에서 import 되는지**를 본다.

### 규칙이 늘어나는 방식

사고가 나면 고치는 것으로 끝내지 않는다 — **규칙 한 줄과 그걸 지키는 테스트 하나**를 같이
남긴다. 위 표의 R1~R5 가 전부 그렇게 생겼다. 테스트 없이 문서에만 적은 규칙은 다음 사람이
같은 자리에서 또 빠져나간다.

## 원터치 — 이 두 줄이 절차다

⚠ **태그가 빌드보다 먼저다.** `release.sh` 는 `git tag --sort=-v:refname` 의 첫 줄을
직전 버전으로 삼아 diff 로 레이어를 정한다. 태그 없이 구우면 기준이 뒤로 밀려
이미 배포한 것까지 다시 담는다 — 실측으로 "173 파일 변경" 이던 판정이 태그를 찍자
**5 파일**이 됐다.

```bash
git push && git tag -a v0.5.6 -m "..." && git push origin v0.5.6
./deploy/release.sh v0.5.6 --dry-run     # 무엇이 올라갈지 먼저
./deploy/release.sh v0.5.6               # 이게 전부다

# 망 없는 현장 (USB) — 이때만 tar
./deploy/release.sh v0.5.6 --offline     # 3.46GB
```

⚠ **어느 레지스트리에 올릴지 사람이 고르지 않는다.** 스크립트가 `DEFAULT_REGISTRIES`
(지금은 GHCR + 사설 `piper-build:5000`) 둘 다에 올리고, 올라간 것을 **다시 물어봐
확인한다.** 사설 레지스트리가 안 떠 있으면 건너뛰되 끝에서 크게 말한다.

> ⚠ v0.5.5 를 `PIPER_REGISTRY=piper-build:5000` 로만 올렸더니 GHCR 에는 안 갔고, 거기서
> 받는 호스트(.120·.44)의 "새 버전 확인" 이 계속 v0.5.4 를 최신이라 답했다. 화면은 맞는
> 말을 하고 있었다 — 틀린 것은 릴리스였다. 환경변수 하나에 결과가 갈리는데 그 선택이
> 스크립트 **밖**에 있었던 것이 원인이라, 그 뒤로 선택을 스크립트 안으로 넣었다.
> `PIPER_REGISTRY` 로 하나만 지정하는 것은 여전히 되지만, **나머지 레지스트리에서 받는
> 호스트는 그 버전을 영영 못 본다.**

사설 레지스트리를 쓰려면 먼저 띄운다 (배포할 때만 연다):

```bash
./deploy/registry.sh --stop
PIPER_REGISTRY_BIND=0.0.0.0 ./deploy/registry.sh
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
| `bus/ shm/ robot/ so101/` | **backend 이미지 + 데몬 wheel** (양쪽이 쓴다 — so101 은 `relay_map` 때문) |
| `cam/ rs/ sim/` | 데몬 wheel **+ backend 이미지** (컨테이너가 import 하진 않지만 wheel 이 이미지 안 `/opt/piper-host` 로 실려 나간다 — v0.4.14) |
| `daemons/ deploy/systemd/ deploy/install-daemons.sh` | 데몬 소스·유닛 (+ backend 이미지 — 컨테이너도 `/app/daemons` 로 돌린다) |
| `deploy/apply.sh piper-install.sh pull-progress.py update-source.sh stage-hostside.sh env.example deploy/udev/ docker-compose*.yml` | **backend 이미지** — 이미지에 실리는 호스트 코드. 안 구우면 새 apply.sh 는 아무 데도 안 간다 (v0.4.14 가 이 판정에 막혔었다) |
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

- [x] `sudo mkdir -p /srv/piper-data && sudo chown $USER /srv/piper-data` (⚠ `&&` 뒤에도 sudo — 없으면 chown 이 "Operation not permitted")
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

- [x] `deploy/install-daemons.sh estopd robotd camerad rsd unitd --optional simd so101d`
      — venv activate된 셸에서 실행해야 함. simd·so101d 는 **깔리되 꺼진 채** —
      웹 [설정 → 서비스] 에서 켜고 "부팅 시 시작"을 고른다 (feature/services.md)
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

받는 쪽이 볼 증상별 처방은 [docs/install-troubleshooting.md](../docs/install-troubleshooting.md) 에 있다 — 여기는 배포하는 쪽의 기록이다.

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
| 데몬 5개 (estopd/robotd/camerad/rsd/unitd) | systemd 유저 유닛, active, 재시작 0회 |
| 선택 데몬 (simd/so101d) | 설치됨·inactive·disabled — 웹 [서비스] 에서 켜면 active |
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
| v0.3.10 | 08-28 | 이미지 | ⚠ **git 태그가 없다** — 레지스트리에만 있다. 다음에 이 번호를 기준으로 삼지 말 것 |
| v0.4.0 | 09-01 01:21 | 이미지 | 라이선스 정리와 한 줄 설치 |
| v0.4.1 | 09-01 17:55 | 이미지 | 대시보드, 로스 곡선, 체크포인트 정리 |
| v0.4.2 | 09-02 10:07 | 이미지 | 서비스 패널을 컨테이너에서 — 데몬 자가보고를 버스로 |
| v0.4.3 | 09-02 21:55 | 이미지 | 마스터/슬레이브 판별 수정 · 로봇 전체 초기화 · HF 계정 |
| v0.4.4 | 09-09 17:10 | **세 레이어 전부** (112 파일) | 시뮬레이션 데몬 · SO-101 리더 · 로봇 페이지 재구성(검사·정렬·버스·버전) · **J6 캘리브레이션 ±120 교정** |
| v0.4.5 | 09-09 19:20 | **세 레이어 전부** (15 파일, wheel 판정은 bus 뿐이나 이미지 안 번들엔 7개 전부) | 서비스 켜기/끄기·부팅 시 시작(piper-unitd) · simd·so101d 깔되 꺼진 채 · 릴레이 해제가 팔로워를 놓음 · 이미지에 `piper_so101` · apply.sh 장치 그룹 검사 |
| v0.4.6 | 09-10 09:52 | **세 레이어 전부** (14 파일, wheel 판정 bus — 번들엔 7개 전부, 태그 도장) | 버전 카드(컨테이너·호스트·데몬) · 웹 업데이트(확인·받기·적용·되돌리기, piper-unitd 일시 유닛) · 번들에 piper-install.sh·CHANGELOG · apply.sh 가 unitd 재시작·적용본 기록 |
| v0.4.7 | 09-10 13:52 | **세 레이어 전부** (GHCR, 15 파일) | 시뮬 메시 패키징 · 새 버전 확인(GHCR 토큰) · 설치 진행 표시 · 원격 로그 뷰어 |
| v0.4.8 | 09-10 17:00 | **세 레이어 전부** (GHCR, 15 파일) | 키보드·마우스 시뮬 텔레옵(웹 리더) · 그리퍼 마찰(impratio) · 카메라 회전·재연결 · 바닥 격자 · 환경/팔 리셋 |
| v0.4.9~v0.4.12 | 09-10 | 이 표에 안 남았다 — 레이어는 CHANGELOG 참고 | 웹 [받기] 경로 수정(v0.4.9 진단 오류 → v0.4.10) · wheel 드리프트 경고 오탐(v0.4.11) · 버전 카드 라벨 세로 쪼개짐(v0.4.12) |
| v0.4.13 | 09-11 10:59 | **세 레이어 전부** (GHCR, 22 파일: 이미지 backend+frontend · wheel sim · 데몬 소스) | 시뮬 조종 창 블럭 옮기기(B)·도움말 패널 · 로그 페이지 통합(저널 탭 기본, 설정 탭 제거) · 추론 분석(셸 안·추론 옆·그래프 칸 예약) · 접속 주소 안내·`PIPER_WEB_PORT`·QnA · 그 사이 실린 PIPER Studio 개명·2열 고정 최소 폭·버전 카드 접힘 |
| v0.4.14 | 09-11 14:09 | **backend 이미지만** (GHCR, 4 파일 — apply.sh·compose 조각·stage) | GPU 없는 NUC 에 v0.4.13 을 깔아 보고: chown 처방 `&&` 뒤 sudo 누락 · GPU 없는 호스트는 `.env` COMPOSE_FILE 로 nogpu 조각(`!reset`, compose 2.24+) · release.sh 가 이미지에 실리는 호스트 코드(apply.sh·compose·udev·cam/rs/sim)를 backend 로 판정 — 이 릴리스 자체가 그 판정에 막혔었다 |
| v0.4.15 | 09-11 14:18 | **backend 이미지만** (GHCR, 1 파일 — apply.sh) | ⚠ v0.4.14 를 **처음** 깔면 아무것도 안 깔렸다 — apply.sh 가 매니페스트(바뀐 것)만 적용해 frontend 이미지·venv/wheel·데몬이 비었다. 없는 것은 번들에서 깐다(없는 이미지는 레지스트리 `:latest`). GHCR 경로의 첫 신규 설치(NUC)에서 드러남 |
| v0.4.16 | 09-11 14:36 | **backend 이미지 + wheel 7개** (GHCR, 9 파일 — apply.sh·piper-install.sh·pyproject 7) | NUC 2·3차 시도: 꺼낸 디렉토리에 남은 옛 wheel(`docker cp` 겹침) → 꺼내기 전 비우기 + 매니페스트 도장 wheel 만 설치 · 22.04 의 파이썬 3.10 을 거절하던 `requires-python >=3.11` → 코드 실측대로 3.10 으로(apply.sh 가 venv 전에 확인, README 전제 표) · 시스템에 없는 numpy·opencv 는 venv 에 |
| v0.4.17 | 09-11 15:27 | **backend 이미지 + wheel** robot·shm·sim·so101 (GHCR, 10 파일) | NUC 설치 뒤 시뮬 팔이 안 움직임 — 컨테이너(root) 릴레이가 만든 action 세그먼트가 0600 이라 호스트 simd 가 못 읽고 명령 스레드가 조용히 죽음 → 세그먼트 0644(fchmod) · simd/robotd 는 열기 실패 재시도 · apply.sh 가 켜져 있던 simd/so101d 도 재시작 · `piper-uninstall.sh`(데이터 보존, sudo 직접 안 씀, override 백업) · ⚠ 호스트가 먼저 만들어 두는 우회는 `fs.protected_regular` 로 불가 |
| v0.4.18 | 09-11 15:40 | **backend 이미지 + 데몬 소스·유닛** (GHCR, 2 파일 — piper-install.sh·unitd.py) | NUC 웹 [업데이트] "받기 스크립트가 없습니다 …/v*/" — README 대로 버전 없이 깔면 번들이 `latest/` 에만 있었다 → installer 가 매니페스트 version 으로 버전 이름 디렉토리에 풀고 `latest` 는 링크, unitd 는 옛 `latest/` 도 매니페스트 버전으로 읽음 · 문구 정직하게 |
| v0.4.19 | 09-11 17:40 | **backend 이미지 + cam wheel + 데몬 소스·유닛** (GHCR, 5 파일) | USB 웹캠 회색 카드 보정 "Not a RealSense id" — 동사가 rsd 에만, 라우터도 rsd 직행 → camerad 에 같은 절차(V4L2 이름: auto_exposure 3/1·white_balance_temperature·exposure_time_absolute ×100µs·gain, 없는 손잡이는 건너뜀 보고), 게이트웨이는 camera_manager 분기 경유 |
| v0.5.0 | 09-14 01:54 | **backend 이미지** (GHCR, 1 파일 — Dockerfile) | 릴리스마다 30 레이어 중 11개 390MB 가 새로 구워져 새로 내려감 → 버전 ARG/ENV 를 모든 RUN 뒤로(맨 위면 태그마다 아래 RUN 전부 캐시 미스), 백엔드 의존성 162MB 를 소스보다 먼저(pyproject 만 복사 → tomllib 로 목록 → 소스는 `--no-deps`). 패키지 160개·`pip check` 출력 v0.4.19 와 동일. ⚠ 이 한 번은 크게 받고(레이어 경계가 새로), 다음부터 버전만 바꾼 빌드 2초·레이어 32/32 재사용(실측). minor 는 사용자 결정. GHCR 정리: backend 는 v0.5.0·v0.4.19, frontend 는 v0.4.13(=latest) 만 남기고 59 버전 삭제(태그가 가리키는 하위 매니페스트는 보존, 지우면 pull 이 깨진다) — `delete:packages` 토큰 필요 |
| v0.5.1 | 09-15 11:27 | **backend·frontend 이미지 + wheel** robot·sim (GHCR, 19 파일) | 시뮬 파지가 손 10~20° 기울면 놓치던 것 → `cone="elliptic"`(올려 둔 impratio 가 그제야 유효)·그리퍼 kp 200→600·손가락 댐핑 2→5(마찰 1.5→2.0 은 측정상 무차이) · 탑뷰 0.9→0.6m(큐브 21→33px, ⚠ 이전 시뮬 데이터셋과 관측 다름) · 시뮬 카메라 위상 고정 14.8→15.0fps("14.7 Hz" 경고 제거) · 조종 창 키패드 EE 이동·`0` 그리퍼 토글(2초 램프)·마우스 끄기(클릭 없이 키로 시작)·[수집 정지] · `api.ts` 시간 제한 + GET 단일 비행(요청 적체로 정지 버튼이 브라우저 밖으로 못 나가던 사고)·ESC 정지 · robotd CAN sudoers 안내(`sudoers.d/piper-can`) · 학습 이미지 full/slim + env-check(GHCR 미배포). ⚠ 배포 뒤 NUC 확인: sudoers 처방이 먹어 can0 이 `bitrate 1000000 · ERROR-ACTIVE` 로 올라왔다 — 남은 증상은 포트 카드가 컨테이너에서 `ip` 를 읽어 빈칸이던 **표시 버그**(다음 릴리스에서 robotd 경유로 수정) |
| v0.5.2 | 09-15 13:28 | **backend 이미지 + robot wheel + 데몬 소스·유닛** (GHCR, 5 파일) | 컨테이너에는 `can0` 이 아예 없는데 게이트웨이가 CAN 을 직접 읽던 세 자리를 robotd 경유로 옮겼다: ① 포트 카드의 bitrate·버스 상태·Rx/Tx 가 빈칸이라 **초기화가 안 된 것처럼** 읽혔다(robotd 는 같은 순간 `bitrate 1000000 · ERROR-ACTIVE · 오류 0`), ② [DOWN] 이 `Cannot find device "can0"` 로 끝났다(UP 은 처음부터 `init_interface` RPC 였다 — 같은 줄의 두 버튼이 다른 기계에서 돌았다), ③ ⚠ 조작 시작 가드가 BUS-OFF 를 못 읽고 **조용히 통과**했다(막으라고 둔 가드가 배포판에서만 무력). robotd 에 `down_interface`·`unhealthy_reason` 동사 추가(`_METHODS` 포함). 남은 직접 호출은 이름 계산(`slot_to_can_name`)과 `bus_stats` 폴백뿐. NUC 검증: 번들의 `daemons/robotd.py` 화이트리스트·`piper_robot-0.5.2` wheel 모두 새 동사 포함 |
| v0.5.3 | 09-15 13:47 | **backend 이미지 + robot wheel** (GHCR, 2 파일) | 로봇 [연결] 이 `piper_sdk not installed` — 설치가 그 패키지를 **한 번도 확인하지 않았다.** venv 를 `--system-site-packages` 로 만들며 주석에 "piper-sdk 는 다시 안 깐다"(=시스템에 있겠지)라고 적어 뒀는데, 개발 머신엔 있었고 맨 우분투(NUC)엔 없었다. wheel 은 `--no-deps` 로 깔리고(선언은 설치에 영향 없음), 선언 검사 테스트는 `piper_*` 최상위 import 만 보며, `arm.connect` 는 서드파티를 늦게 import 한다 — 세 그물을 다 빠져나갔다. apply.sh 가 컨테이너와 **같은 핀**(`piper_sdk==0.6.1 python-can==4.6.1`)으로 직접 깔고 `--check` 항목에도 넣는다(선택 데몬 의존과 달리 실패를 경고로 안 넘긴다). 이미지 검증: 설치 줄·점검 줄 각 1, 옛 주석 0. NUC 은 릴리스 전에 손으로 같은 핀을 넣어 풀어 뒀다(늦은 import 라 robotd 재시작 불필요) |
| v0.5.17 | 10-10 (**재발행 1회**) | **backend 이미지** (GHCR + 사설, 3 파일) | **클라우드 GPU — 다른 계정이 먼저 만든 템플릿에 붙어 "템플릿 없음" 이던 것.** Vast 는 같은 내용의 `create template` 을 새로 만들지 않고 **먼저 만든 계정의 것**을 가리킨다(`Existing Template Found: 754853. User relationship added.`, `success: false`, `creator_id` 711347 ↔ 이 계정 698077). `search templates private=true` 는 "내 템플릿만" 이라 그걸 안 돌려줘 화면은 계속 "템플릿 없음" 이었고, 지워도 다시 만들면 또 붙는다. CLI 는 `success:false` 라 `msg` 만 찍고 **응답의 `hash_id` 를 버린다.** 판정은 `desc` 까지 보는 내용 해시라(실측) 템플릿 `desc` 에 **계정 id** 를 넣는다 — 계정마다 달라 남의 것에 안 붙고, 같은 계정은 늘 같은 문자열이라 다시 눌러도 자기 것에 맞는다(`templates.desc_for`; 이름·이미지·태그 불변, 계정 id 를 모르면 만들지 않는다). 남의 것에 붙어 쓰는 길은 **안 갔다** — 그 계정이 수정·삭제하면 우리 hash 가 조용히 낡는다. 겸해서 **사유가 숨던 것**: `vastai create template` 은 거절돼도 종료코드 0 으로 끝내고 사유를 stdout 에만 찍어, 게이트웨이가 "만들었지만 목록에 없습니다" 라고만 했다 → 목록에 없으면 CLI 가 한 말을 그대로 싣는다(성공 판정은 여전히 목록 조회). 검증: 전체 **2504 통과**(스킵 10) · 레지스트리 둘 ☑ · manifest `images=backend`·`wheels=""`·`daemons=""` · 이미지 안 `desc_for` 1·계정별 desc 상이·id 없으면 ValueError · **개발 계정(698077)에서 실측**: desc 에 계정 id 를 붙인 같은 요청이 `Template Created Successfully`(759053·759054), `private=true` 목록 2개, 준비도 초록 · GHCR digest `f0d024a37ce5…`(첫 발행 `aaed5a417aee…`). ⚠ **재발행**: 첫 발행은 사유 표시만 담았고(원인을 "제한 API 키" 로 짐작했는데 **틀렸다**), 사용자 지시로 같은 번호에 덮었다. R10 의 허용 조건(아무도 안 받음)은 **확인하지 못했고** 스크립트가 "이미 있다" 고 경고했다 — 첫 발행을 받은 호스트가 있으면 '최신입니다' 로 답해 스스로 안 받으니 그 기계는 `./piper-install.sh` 수동. 태그는 정정 커밋(`c66906b`) 위로 옮겼고 이력은 안 다시 썼다. ⚠ **안 한 것**: ① 라우터(`POST /api/cloud/templates`)를 "남의 것에 붙어 있던 계정" 에서 끝까지 돌려 보지 못했다 — 개발 계정은 조사 중 템플릿이 이미 생겨 `missing()` 이 비었다(요청 단위로만 확인). ② 문제를 보고한 팀 계정의 상태는 못 봤다. ③ 711347 이 누구인지 모른다 — 754853·754854 는 그 계정 것이고, 그 계정이 수정·삭제하면 거기 붙어 있던 다른 계정의 hash 가 낡는다(이번 수정은 새로 만드는 계정만 고친다). ⚠ 개발 머신 CLI 의 키 파일(`~/.config/vastai/vast_api_key`, 9/15)은 `Invalid user key` 401 이다 — 게이트웨이가 저장한 키는 유효하다. 데몬은 재시작되지 않는다(이미지만) |
| v0.5.16 | 10-10 | **backend·frontend 이미지** (GHCR + 사설, 3 파일) | **추론을 CPU 로 돌릴 수 있다 — GPU 없는 PC.** 로컬 추론의 `device`/`policy_device` 가 `cuda` 로 박혀 있고 화면에 선택지가 없어 GPU 없는 기계에서는 시작조차 못 했다. 추론 모드 카드에 **장치**(CUDA/CPU)가 생겼다 — 로컬은 wrapper `--device`, 서버(gRPC)는 `--policy-device`. 기본은 예전 그대로 cuda(GPU 기계 명령 불변), cpu 면 AMP 를 켜지 않는다. 선택은 브라우저에 기억하고 프리셋에는 안 넣는다(기계마다 다른 값). **GPU 없는 기계에서는 로컬 모드에 CPU 만 고를 수 있다** — 화면을 열 때 `GET /api/models/inference/devices` 로 한 번 묻고(폴링 없음), 없으면 CUDA 가 골라져 있어도 CPU 로 내리고 CUDA 항목은 선택 불가. 내려간 값은 저장하지 않는다. 서버 모드는 장치가 서버 기계의 것이라 막지 않는다. 판정 `resources.cuda_present()` 는 `/dev/nvidia0…`·`/dev/nvhost-gpu`·`nvidia-smi` 실행 파일의 **유무**만 본다(서브프로세스 없음). ⚠ `/proc/driver/nvidia` 는 못 쓴다 — 컨테이너의 `/proc` 이 호스트 커널의 것이라 GPU 를 안 준 컨테이너에서도 보인다(실측; 테스트가 이 경로를 코드에서 쓰지 못하게 막는다). ⚠ 새 GET 경로는 `GET /{model_id:path}` **앞**에 있어야 한다(뒤면 모델 id 로 먹힌다; 순서를 테스트가 고정한다). ACT CPU 속도(이 개발 머신, 코어 4개로 제한, 51.6M·fp32·640×480): 청크 하나 카메라 1대 84ms·2대 166ms·3대 278ms — 노트북(i5 10세대)은 2~3배 느릴 것으로 **추정**(직접 안 쟀다). ⚠ temporal ensemble 은 스텝마다 추론해 CPU 에서 못 따라간다; 래퍼는 추론을 별도 스레드에서 돌리고 `refill_threshold_pct`(기본 20%)로 미리 채우므로 끊기면 30~40%. 검증: 전체 **2495 통과**(스킵 10) · 프론트 빌드 ☑ · 레지스트리 넷 ☑ · manifest `images="backend frontend"`·`wheels=""`·`daemons=""` · 릴리스된 backend 이미지를 GPU 없이 돌려 `cuda_present=False`·엔드포인트 `{cuda: False}`·라우트 순서 True·`device=cpu` 일 때 `--device cpu` 에 `--use-amp` 없음 · frontend 이미지 번들에 `piper_inference_device`·`이 기계에서 GPU 를 찾지 못했습니다`·`models/inference/devices` · GHCR digest backend `54c7f4261e0e…`·frontend `79d92754d700…`. ⚠ **안 한 것**: 실제 체크포인트로 CPU 추론을 끝까지 돌려 보지 못했고(이 머신에 체크포인트가 없다) 브라우저에서 화면을 직접 보지 못했다. YOLO 데몬 셋의 `--device` 기본값 `cuda:0` 은 그대로라 GPU 없는 PC 에서 YOLO 는 같은 문제가 남는다. 데몬은 재시작되지 않는다(이미지만). 새 화면은 브라우저 새로고침 후 적용 |
| v0.5.15 | 10-10 | **backend 이미지** (GHCR + 사설, 2 파일) | **GPU 없는 기계에서 `vcodec=auto` 가 NVENC 를 고르던 것.** LeRobot 의 `auto` 는 `av.codec.Codec(name, "w")` 가 성공하면 "쓸 수 있다"고 보는데 그건 FFmpeg 빌드에 코덱이 **들어 있다**는 뜻이지 장치가 있다는 뜻이 아니다 — PyAV wheel 은 NVENC 를 품고 있어, GPU 없는 컨테이너에서 `auto → h264_nvenc` 가 되고 첫 프레임에서 `PermissionError: avcodec_open2(h264_nvenc)` 로 녹화가 죽었다(그대로 재현). `wrapper/hw_encoders.py` 가 후보를 **실제로 한 번 열어 보고** 열리는 하드웨어 인코더만 `auto` 후보로 삼는다 — 하나도 안 열리면 LeRobot 의 원래 폴백(libsvtav1). 판정은 프로세스당 한 번, `start_record.py` 가 try/except 로 설치(깨져도 녹화는 계속). 명시한 코덱은 건드리지 않는다. GPU 있는 기계는 그대로 `h264_nvenc`(이 개발 머신 RTX 5090 에서 확인). ⚠ 인텔 QSV·VAAPI 는 **아직 못 쓴다** — 이미지의 PyAV 에 그 인코더가 없고(시스템 ffmpeg 에는 있음), 이미지에 VA 드라이버(iHD)가 없고, 컨테이너에 `/dev/dri` 가 안 간다. 열리는 기계에서는 이 판정이 저절로 고른다. ⚠ **안 고친 것**: 추론 `device`/`policy_device` 가 `cuda` 고정(`routers/models.py`, UI 에 CPU 선택지 없음)·YOLO 데몬 셋의 `--device` 기본값 `cuda:0` — GPU 없는 PC 에서 로컬 추론·YOLO 가 시작되지 않을 수 있다(미검증). 검증: 전체 **2482 통과**(스킵 10) · 레지스트리 둘 ☑ · manifest `images=backend`·`wheels=""`·`daemons=""` · 릴리스된 이미지를 GPU 없이 돌려 `auto` 가 수정 전 `h264_nvenc` → 설치 뒤 `libsvtav1` 로 바뀌는 것 확인 · 이미지 안 `/app/wrapper/hw_encoders.py` 와 `start_record.py` 의 `install()` 호출 1 · GHCR digest `3cf35602929c…`. 데몬은 재시작되지 않는다(이미지만). 이 수정은 **다음 녹화부터** 적용된다 — 돌고 있는 녹화는 옛 코드 그대로다 |
| v0.5.14 | 10-10 | **backend 이미지 + wheel** cam (GHCR + 사설, 2 파일) | **비압축으로 fps 가 안 나오는 USB 카메라를 MJPG 로 연다.** GPU 없는 노트북(i5 10세대)에서 수집이 계속 멈췄다 — CPU 는 코어마다 20% 로 한가했고 코덱을 바꿔도 카메라가 5Hz 근처로 고정됐으며, 녹화의 프레임 대기(200ms = 5Hz)가 `새 프레임이 없습니다` 로 죽었다. camerad 가 카메라를 열 때 크기·fps 만 요청하고 **포맷은 안 정해** OpenCV 가 비압축(YUYV 등)을 고를 수 있었고 그건 USB 대역폭을 먹는다(1080p YUYV 는 5fps). `v4l2.choose_fourcc` — 요청 크기에서 **비압축으로는 요청 fps 가 안 나오고 MJPG 로는 나올 때만** MJPG(포맷은 크기·fps 보다 먼저 설정). 비압축이 이미 내는 카메라는 건드리지 않는다. 장치가 요청보다 낮게 열리면 camerad 로그에 남는다. ✔ **노트북에서 해결을 확인했다**(2026-10-10). ⚠ 다만 원인(비압축 포맷의 USB 대역폭)은 증상과 수정 결과에서 **추정**한 것이다 — 그 카메라의 `v4l2-ctl --list-formats-ext` 는 못 봤고, 이 개발 머신은 RealSense 뿐이라 UVC 웹캠으로 재현 못 한다. MJPG 로도 낮거나 한 USB 허브에 몰았다면 대역폭이라 코드로 못 푼다. 검증: 전체 **2474 통과**(스킵 10) · 레지스트리 둘 ☑ · manifest `images=backend`·`wheels=cam` · 이미지 안 cam wheel 에 `def choose_fourcc` 1·`hub.py` 호출 1 · import OK · GHCR digest `5fe87f4d67d1…`. ⚠ **camerad 가 재시작된다**(cam wheel) — 카메라 프레임이 한 번 끊긴다. ⚠ 번들 tar 안의 wheel 은 여전히 `piper_cam-0.1.0` 이름이다(이미지 안 wheel 은 0.5.14) |
| v0.5.13 | 10-08 (**재발행 2회**) | **backend·frontend 이미지 + wheel** robot + **데몬 소스** (GHCR + 사설, 9 파일) | **그리퍼 행정(70/100mm)을 팔마다 고른다.** 대형(100mm)이 소형 상한(68000µm)으로 정규화돼 팔로워가 "100 = 68mm" 에서 토크로 버텼고 손으로도 안 벌려졌다. 정규화 0..100 은 행정에 대한 비율이라 슬라이더·정책·안전 필터는 그대로 두고 **raw↔정규화 변환 세 곳**(상태 읽기·파킹·명령)만 팔별 상한을 쓴다. 저장은 robotd `gripper.json` 의 `strokes`(힘과 같은 파일 — 쓰는 곳을 `_write` 하나로 해 한쪽 저장이 다른 쪽을 안 지운다), 설정 없는 팔은 70mm 라 **쓰던 팔은 그대로**. `GET/POST /api/robots/gripper-stroke`(70|100 만, 추론·녹화·텔레옵 중 409), 팔 카드 설정 탭 선택(리더·팔로워 둘 다). ⚠ 100mm 상한은 **98000** — 대형 팔로워가 raw **99500** 까지 열린 것을 쟀고, 소형이 공칭 70 에 68000 으로 2mm 를 남기는 것과 같은 여유를 뒀다(`joints.GRIPPER_RAW_MAX` 한 줄, 테스트가 실측 끝 안쪽에 묶는다). **리더 열림 끝 맞추기**: 양쪽을 100mm 로 맞춰도 리더를 끝까지 열면 팔로워가 82% 에서 멈췄다 — 변환 버그가 아니라 리더가 그리퍼가 아니라 **티칭 핸들**이라 모델 행정과 무관하게 자기 끝(실측 `0x159` 최대 82860µm)에서 멈추고, 정규화가 비율이라 팔로워에게 "82%" 가 된 것이다. 리더 카드 설정 탭 [현재 열림을 끝으로 저장] → robotd `gripper.json` 의 `ends`(팔별, 모델 표보다 앞선다). **리더에서만**(팔로워는 400 — 측정한 물리 끝이면 매번 스톱을 누른다, 초기화는 어느 팔이든), 20~120mm 밖은 **거절**(클램프 아님 — 덜 열린 채 눌러 작은 상한이 저장되면 작은 움직임이 100 이 된다), 행정을 바꾸면 버려진다, 추론·녹화·텔레옵 중 409. 읽기는 상태 읽기와 **같은 소스**(`Arm._raw_state_locked` — `test_master_slave_probe` 의 신선도 검사가 그 헬퍼를 따라가도록 고쳤다, 규칙은 불변). ⚠ 리더가 가만히 있으면 SDK 가 **마지막에 받은 값**을 현재 열림으로 쓴다(끝까지 연 직후에 누른다). ⚠ 팔 펌웨어 `max_range_config`(70/100)는 **안 읽는다** — 펌웨어가 70 이면 소프트웨어를 100 으로 맞춰도 70mm 에서 막힐 수 있다. ⚠ 설정은 `can0` 같은 포트 이름에 묶인다(그리퍼 힘과 같다). **어댑터가 빠진 포트가 살아 있는 것처럼 보이던 것**: 등록부는 한 번 본 포트를 잊지 않아, 어댑터 4개가 USB 에서 빠진 뒤에도 카드가 남고 `arm.state` 폴백이 **마지막 스캔의 UP/DOWN 을 지금 링크처럼** 냈다 → robotd 가 답했는데 목록에 없으면 `present=false`·상태 비움(회색 "장치 없음"), 답이 없으면 `present=null`(모름 — 죽은 데몬을 "어댑터 없음"으로 읽지 않는다). 검증: 전체 **2471 통과** · 프론트 빌드 ☑ · 레지스트리 넷 ☑ · manifest v0.5.13 · 이미지·번들 wheel 안 `GRIPPER_RAW_MAX={70:68000,100:98000}`·`robotd` 번들에 `get/set_gripper_stroke`·`capture/clear_gripper_end`(3곳)·robot wheel 의 `arm.py` 에 `_raw_state_locked` 2곳·`read_gripper_raw`·`gripper_store.set_end`·라우트 `gripper-stroke/end`·프론트 번들에 `gripper-stroke/end`·`현재 열림을 끝으로 저장`·`장치 없음`. ⚠ **robotd 가 재시작된다**(robot wheel + 데몬 소스) — 팔 연결이 한 번 끊긴다. ⚠ **재발행 이유**: 1차(`721cd6d`) 직후 팔로워 실측으로 100mm 상한 100000 이 물리 끝(99500)보다 0.5mm 너머임을 알게 돼, 사용자 지시로 **같은 번호로 다시 끊었다**(정정 커밋 `91fecd7`·`2e73217` 위에 태그를 옮김, 이력은 안 다시 썼다). R10 의 허용 조건(아무도 안 받음)은 **확인하지 못했다** — 1차를 받은 호스트가 있으면 '최신입니다' 로 답해 스스로 안 받으니 그 기계는 `./piper-install.sh` 를 손으로 돌려야 한다. **2차 재발행**(`680d91e`): 리더 열림 끝 맞추기를 더했고, 1차 때 쓴 "리더가 104400 까지 읽혔다" 는 **틀린 근거**였다(`/joints/raw` 의 값 — CAN 프레임으로 직접 보면 재현되지 않는다) — 코드 주석·CHANGELOG·이 행에서 실측(82860)으로 정정했다. 이번에도 R10 의 허용 조건은 **확인하지 못했다**: 앞 발행(`721cd6d`·`2e73217`)을 받은 호스트가 있으면 '최신입니다' 로 답해 스스로 안 받는다 → `./piper-install.sh` 수동. GHCR digest 가 로컬 빌드와 일치(backend `e172118abf91…`·frontend `921127ef9938…`) — 앞 발행(`faabc528c321…`·`7080b92c74fd…`)과 **다르다**. ⚠ **1차에서 겪은 것**: `git push 2>&1 \| tail` 이 업스트림 없음으로 실패했는데 파이프 종료코드(tail 의 0) 때문에 체인이 이어져 **태그가 master 보다 먼저** 원격에 갔다 — 같은 커밋이라 fast-forward `git push origin master` 로 바로잡았다(master 에 업스트림이 설정돼 있지 않다). 다음엔 push 를 파이프에 물리지 말 것. ⚠ 번들 tar 안의 wheel 은 여전히 `piper_robot-0.1.0` 이름이다(v0.5.12 에 적은 것과 같은 건 — 이미지 안 wheel 은 0.5.13) |
| v0.5.12 | 10-07 | **backend·frontend 이미지 + wheel** robot + **데몬 소스** (GHCR + 사설, 12 파일) | **계정이 다르면 학습 템플릿이 아예 안 보이던 것.** Vast 템플릿은 만든 계정의 것이고 조회도 `private=true` 라, 다른 계정으로 들어오면 `piper-train full cu126` 이 목록에 없다 — 화면은 '템플릿 없음' 빨간불만 띄우고 **만드는 길을 안 줬다**. 이미지는 원래 문제가 아니었다(GHCR 익명 pull 200, 두 태그 다 확인) — 막던 것은 가시성 하나뿐. 길 셋 중 **계정마다 만든다**를 골랐다: 공개로 돌리면 조회가 `private=true` 라 남의 화면엔 여전히 안 뜨고 우리 수정이 남의 임대에 영향을 간다(`update template` 은 전체 치환·`hash_id` 가 바뀐다). 템플릿을 아예 안 쓰는 길(`create instance --image`)도 되지만 `--onstart` 가 파일명이라 올리는 단계가 늘고 검색 필터를 코드가 들어야 한다 — **중복된 진실을 없애는 길이라 장기적으로는 그쪽이 맞다**. 사양은 `services/cloud/templates.py` 한 곳, `POST /api/cloud/templates` 가 **없는 것만** 만든다(같은 이름이 둘이면 어느 `hash_id` 를 쓸지 알 수 없다), 하나 실패해도 나머지는 만든다. ⚠ 새 템플릿은 **굴러가는 태그**(`full-cu126`) — 날짜를 코드에 박으면 이미지를 다시 구울 때마다 **새 계정만 옛 이미지를 받는다**. 살아 있는 둘은 아직 날짜 태그라 다음에 손볼 때 같이 옮기는 편이 낫다(이번엔 안 건드렸다 — 도는 작업의 것이다). ⚠ **`create template` 은 `--raw` 로도 JSON 이 아니라 파이썬 repr 을 찍는다** — 파싱하다 **만들어진 템플릿을 실패로 보고**했고, 다시 누르면 같은 이름이 둘이 된다. 출력을 안 읽고 목록에 물어본다(어차피 `--template_hash` 가 믿는 자리). 버리는 이름으로 두 번 예행해 `--search_params` 가 저장된 `extra_filters` 를 **글자까지 재현**하는 것과 프로바이더 파싱(`image`·`tag`·`disk=40.0`·`variant`·`cuda`)을 확인하고 지웠다. 다른 세션: **그리퍼 힘**을 수집 전·추론 중에 정할 수 있고 세션에 남는다, 못 쓰는 팔에서는 슬라이더를 안 보여준다. 검증: 전체 **2422 통과** · 프론트 빌드 ☑ · 멱등(둘 다 있는 계정에서 `created=[]`) · 레지스트리 넷 다 ☑ · 번들에 `piper_robot` wheel + 데몬 소스. ⚠ **이번에 눈에 띈 것(이번 릴리스가 만든 것 아님)**: 이미지·번들의 wheel 버전이 전부 `0.1.0` 이다 — v0.5.11·v0.5.8 이미지도 같다. `staleness()` 가 `0.1.0` 을 '도장 전 빌드' 로 **빼므로 wheel 대조가 사실상 돌지 않는다**. 따로 봐야 한다 |
| v0.5.11 | 10-06 | **backend·frontend 이미지 + wheel** cam·rs + **데몬 소스** (GHCR + 사설, 15 파일) | **카메라별 캡처·출력 해상도** — 드롭다운 둘, 목록은 장치가 신고한 모드(camerad 는 V4L2 열거 ioctl, rsd 는 librealsense 프로파일). 출력 기본값 원본, 캡처 기본값 요청 그대로라 설정 안 한 카메라는 그대로다. 이유는 화각: AR0234 는 640×480 을 센서 가운데 1/3 을 잘라 만든다 → 1280×960 으로 받아 640×480 으로 내보내면 크기 같고 화각 넓다. 비율이 다르면 가운데를 잘라 맞추고 늘이지 않는다, 깊이는 최근접, **intrinsics 도 같은 자르기·축소**(정렬 검사 mm 보호). rsd 는 호출자 요청(`want`)과 캡처를 따로 들어 매번 재연결하지 않는다. 실측: AR0234 1280×960→640×480 전체 화각, D435 color 1280×720→640×480 가운데 4:3 · fx 603 · cx 331. **camerad 잃어버림 판정이 [끊기]로만 지워지던 것** — 다시 꽂아도 장치 감시가 주기마다 "없음"으로 되돌렸다(so101d·rsd 는 1b036f0 에서 고침). 검출 학습: 버튼이 꺼진 이유(라벨된 **이미지** 4장) 표시, 시작 전 학습 의존성 점검(`detect-train` extra). 검증: manifest v0.5.11 · `piper_cam-0.5.11` 에 `fit.py`·`list_modes`·잃어버림 해제·`modes` · `piper_rs-0.5.11` 에 `fit_intrinsics`·`_request` · 번들 camerad·rsd 에 `"modes"` 동사 · 게이트웨이 안 `set_resolution`·`_missing_train_deps` import · 레지스트리 넷 ☑. ⚠ **데몬이 재시작된다**(cam·rs wheel + 데몬 소스) |
| v0.5.10 | 09-29 | **backend·frontend 이미지 + wheel 일곱 전부 + 데몬 소스** (GHCR + 사설, 73 파일) | **이름을 `PIPER` 로** 썼다 — 105파일 254곳(창 제목·상태바·바탕화면 아이콘·진단 보고서·systemd 설명·문서·주석). 낱말만 바꿔 패키지·유닛·환경변수·식별자는 안 움직였다. **상태바 왼쪽 끝에 WeGo 로고**(누르면 홈페이지, 오른쪽 끝은 E-STOP 자리라 비워 둔다). 받은 로고가 4584px·124KB 인데 28px 로 그려져서 회색+알파 480px 로 줄였다(같은 그림, 20.7KB). ⚠ **검출 프리뷰의 노랑이 파랗게 나오던 것**(사용자 보고): `yolod` 는 세그먼트를 BGR 로 그대로 넘기는데 어댑터는 옛 전제(RGB)로 한 번 더 뒤집어 `imencode` 가 RGB 를 BGR 로 적었다. **같은 어긋남이 모델 입력도 망가뜨리고 있었다** — RT-DETR 은 RGB 로 학습됐는데 BGR 을 받았다(에러 없이 검출만 나빠져 오래 안 들켰다). 규약을 `as_rgb_bgr` 한 곳에 모았다. ⚠⚠ 그 자리의 **테스트가 버그를 지키고 있었다** — `plot()` 소스에 `"[..., ::-1]"` 이 있는지(결과가 아니라 **구현**)를 검사해서, 데몬 쪽 규약이 바뀐 뒤에도 초록이었다. ⚠ **wheel 일곱 전부 + 베이스 재빌드**: 대문자화가 모든 패키지의 주석과 `Dockerfile.base` 주석 한 줄(`# ── Piper 하드웨어 스택 ──`)을 건드렸다. 베이스는 내용 해시로 판정하므로 다시 구웠다 — **실측 비용은 147MB**(레이어 35개 중 21개 교체, 평소 증분 ~100MB)라 재발행하지 않았다. 고치려면 release.sh 가 **주석을 뺀** 해시를 보게 하면 된다. 검증: manifest v0.5.10 · wheel 7개 · 번들 `detector_loader.py` 에 `as_rgb_bgr` 있고 `plot()` 에 `::-1` **0개** · 이미지 안 `app.title == "PIPER Studio"` · `MESH_ENABLED=False` · 프론트 이미지에 로고 20726B · 레지스트리 넷 ☑ |
| v0.5.9 | 09-28 (**2차 발행**) | **backend·frontend 이미지 + wheel** sim + **데몬 소스** (GHCR + 사설, 17 파일) | **시뮬 탑뷰 높이를 사람이 정한다** (0.35~1.20m 슬라이더). fovy 고정이라 z 가 곧 줌이다 — 640×480 기준 큐브 4cm 가 0.4m 51px · 0.6m 33px · 1.0m 19px(v0.5.1 실측 21px 과 일치). ⚠ 기본 0.6m 는 **테이블 y 의 69% 만** 담아서 클릭으로 y ±0.31 밖에 물체를 못 놓았다(전체는 0.864m 부터). 높이는 가상환경에 저장되고 **데이터셋에도** 남는다. 수집·추론·루프 중 거절하되 **조종 중에는 허용** — `Activity.CAMERA_MOVE` 를 따로 둔 이유다(카메라가 올라가는 것은 팔에 아무 일도 안 한다). 실측: `cam_pos` 한 줄이라 **재컴파일도 렌더러 재생성도 없다**. **메시 가져오기를 껐다** — 모델링 도구의 일이고 실측으로 한 번도 안 쓰였다(자산 0·메시 가상환경 0·그런 데이터셋 0). 감춘 게 아니라 닫았다: 명세 거절 + 자산 API 라우터 전체 404 + 편집기에서 189줄 제거. 읽기·측정 코드와 그 테스트는 스위치 뒤에 남겼다. **출처 기록**: 사이드카에 스키마 판과 **바탕 MJCF 내용 해시**(번호가 그대로여도 파일이 바뀌면 다른 세계다 — v0.5.1 의 탑뷰 이동이 실제 사례), 학습 시작 때 `meta/piper_scene.json` 을 run 루트로 **인계**(체크포인트가 아는 건 repo_id 문자열 하나뿐이었다). **화면**: 물체를 옮기면 배치 캔버스가 잠기던 것(저장-안-됨과 세계-다름을 한 플래그로 셌다), 가상환경 카드에 배경이 아예 없던 것, 메뉴 제목 크기 넷, 에피소드 페이지 제목. ⚠ **2차 발행 이유**: 1차 직후 사용자 보고로 **한국어 바탕화면에 아이콘이 안 생기던 것**을 찾았다 — `xdg-user-dir` 가 없으면 `~/Desktop` 을 찍는데 한국어 데스크톱은 `바탕화면` 이라 "SSH 전용" 으로 판정하고 앱 메뉴만 만들었다. 이제 `user-dirs.dirs` 를 직접 읽고 흔한 이름을 훑는다. **.120·.44 둘 다 v0.5.8 이라 아무도 v0.5.9 를 안 받은 상태**였다(R10 의 허용 조건). 검증: manifest v0.5.9 · wheel 7개 전부 0.5.9 · 번들 `simd.py` 에 `camera_view` · 이미지 안 `MESH_ENABLED=False`·`CAMERA_Z_RANGE` · 사이드카 키에 `spec_version`·`sim` · `inherit_to_run` 있음 · 번들 `install-shortcuts.sh` 에 새 탐색 13곳 · 아이콘 스크립트 셋 동봉 · 레지스트리 넷 ☑ |
| v0.5.8 | 09-23 | **backend·frontend 이미지** (GHCR + 사설, 36 파일) | **게이트웨이에 로그인이 생겼다 — 켜야 걸린다.** `/api/ext/v1` 말고는 인증이 없어서 LAN 에서 :8000 에 닿는 누구나 데이터셋을 지우고 팔을 움직이고 Vast 키가 저장된 호스트에서는 GPU 를 빌려 **돈을 쓸 수 있었다**. 설정 → 보안에서 비밀번호를 정해야 켜진다(기본 잠김으로 하면 릴리스를 받는 순간 배포된 로봇이 전부 잠긴다 — 그건 안전 기능이 아니라 사고다). ⚠⚠ **E-stop 은 어떤 경우에도 인증을 안 탄다**: heartbeat 가 401 이면 estopd 가 브라우저 사망으로 읽어 **돌던 추론을 2.5초 뒤 SIGKILL** 한다. 관문은 순수 ASGI(BaseHTTPMiddleware 는 WS scope 를 못 봐 `/ws` 가 밖에 남고 MJPEG 와도 안 맞는다), 세션은 HMAC 쿠키(재시작마다 로그아웃되면 사람이 인증을 꺼 버린다), **표준 라이브러리만** (이미지가 `--no-deps` 라 새 의존성은 안 깔린다). **게이트웨이 이미지에 `ssh`·`scp` 가 없었다** — 로컬 학습은 ssh 를 안 타고 개발 머신은 저장소에서 직접 돌아 오래 안 걸렸는데, `vastai` 는 있어서 **준비 점검이 초록불**이었다(빌린 다음에야 죽는, 셋 다 없던 때보다 나쁜 조합). 베이스 `cu130-3 → cu130-4`, **파일 끝에** 붙여 앞 계층 다이제스트를 안 건드렸다(앞에 끼우면 호스트가 수 GB 를 다시 받는다). **허브에서 받은 데이터셋이 쓸모를 찾았다**: ① 영상이 전부 404 였다 — 경로 가드가 심볼릭을 따라가 판정해서(HF 캐시는 스냅샷이 `blobs/` 로의 링크) 항상 밖으로 나갔고, 화면은 그걸 **코덱이라 불렀다**(브라우저가 mp4 대신 404 JSON 을 파싱). ② 학습이 안 됐다 — lerobot 은 `HF_LEROBOT_HOME` 을 보는데 받는 자리는 허브 캐시고, 못 찾으면 허브에 코드베이스 태그를 묻다가 **태그 없는 저장소에서 폴백 없이 죽는다**. 로컬은 `--dataset.root` 로 우회. ③ **화면의 [업로드] 가 태그를 안 달고 있었다**(녹화 푸시는 lerobot 을 거쳐 달린다) — 그래서 화면으로 올린 데이터셋은 임대 GPU 에서 못 썼다. 이제 업로드가 `codebase_version` 으로 달고, 임대는 **빌리기 전에** 태그를 확인한다(안 막으면 기계 만들고 스택 6분 깔고 죽는다 — 돈을 쓴 뒤다). **허브 다운로드가 진행 상황을 말한다**(`n/N · % · 속도 · ETA`) — 예전엔 '다운로드 중...' 이 영원히 남았고(지우는 곳이 없었다) **완료를 세어 보지 않아** 받다 만 저장소를 완료라 했다(영상 하나가 안 왔는데 목록은 `meta/` 에서 읽어 멀쩡해 보였고 몇 주 뒤 드러났다). **카메라가 스캔은 되는데 등록만 안 되던 것**(.120): 데몬 venv 에 **OpenCV 가 없었다** — 열거는 cv2 없이 되고 여는 것만 `cv2.VideoCapture` 라 등록에서만 죽는다. `--system-site-packages` 의 '시스템' 은 venv 를 만든 파이썬(여기선 miniconda 3.13)이라 OS 의 cv2(3.10)는 영영 안 보였고, 검사는 venv **생성 분기 안**에 있어 안 돌았고, 못 깔아도 `warn` 이라 설치는 '다 됐다' 고 했다. **`apply.sh` 가 깨진 지점 스무 줄 뒤에서 죽던 것**: `set -e` 는 `&&` 왼쪽 실패를 봐주므로 venv 생성이 조용히 지나가고 한참 뒤 'pip 가 없다' 로 끝났다(전제 검사도 `import venv` 라 통과했다 — 진짜 관문은 `ensurepip`). 다른 세션: Swagger 문서를 프론트에서 닿는 자리로. 검증: 전체 **2349 통과** · 프론트 빌드 ☑ · 중간 커밋 넷 각각 통과 · 레지스트리 넷 다 ☑ · 번들 `apply.sh` 에 이번 수정 3곳 · 실기 실측(로그인 9단계 · 카메라 등록·프리뷰 640x480 · 로컬 학습 60스텝 13256프레임 · 허브 다운로드 0→10파일 100%). ⚠ **이미지만 올렸다** — 데몬 wheel·소스는 안 바뀌었다 ⚠ **2차 발행**(09-24 11:26, 사용자 결정 — 이 번호를 이미 적용한 호스트가 있는지 확인 못 함: .120 은 SSH 가 안 닿았다). 얹은 것: **바탕화면 아이콘** [PIPER Studio]·[PIPER Studio 진단](`install-shortcuts.sh`·`piper-studio.sh`·`piper-doctor.sh`, apply.sh 5절, uninstall 4b, 번들에 favicon) · README 에 **Google Colab 학습 안내** + Vast.ai 명시 · 현장 배포·운영 검토 문서(feature/field-deployment.md) · 탭 제목 `frontend` → PIPER Studio. v0.5.7 대비 43 파일, backend·frontend 이미지(GHCR + 사설), wheel·데몬 없음. 검증: manifest v0.5.8 · wheel 7개 0.5.8 · import OK · 번들에 install-shortcuts.sh·piper-studio.sh·piper-doctor.sh·piper-studio.svg · apply.sh 「5. 바로가기」· uninstall 「4b」· 프론트 `<title>PIPER Studio</title>` · 레지스트리 넷 다 ☑ · 전체 테스트 2,362 통과. 태그는 2차 커밋으로 옮겼다(v0.5.5 3차 때와 같은 방식). ⚠ 1차 v0.5.8 을 이미 적용한 호스트는 '최신입니다' 를 보므로 손으로 `./piper-install.sh` 를 한 번 돌려야 한다 |
| v0.5.7 | 09-21 | **backend·frontend 이미지 + wheel** sim·so101 + **데몬 소스** (GHCR + 사설, 22 파일) | **죽은 프로세스가 팔을 영영 잠그던 것.** .120 시뮬 팔이 "누가 이미 쥐고 있습니다"로 막혔는데 추론·녹화·릴레이 전부 정지 상태였다 — SIGKILL 당한 프로세스가 남긴 `/dev/shm` 명령 세그먼트이고 **존재 자체가 lease** 라 그 위에 못 연다. ⚠ [해제]·[연결]도 데몬 재시작도 안 먹었다: 남은 파일이 **root** 것이다(게이트웨이는 컨테이너 root + `ipc: host`, `/dev/shm` 은 sticky → 사용자 데몬의 unlink 가 EPERM). `A.unlink` 가 그걸 **False 로 삼켜** 브리지는 지운 줄 알았다. 실측: `.action` 은 root 소유로 2시간째 그대로, 옆 `.state` 는 같은 팔이 10ms 마다 쓰는 중. 고침 = **존재가 아니라 마지막 기록 시각**(5초, 데드맨 300ms 의 16배)으로 판정해 지우고 이어받는다 — 치울 수 있는 쪽(만든 쪽)에서. 덤: simd 기동 정리가 `".sim_" in n` 이라 **자기 팔 세그먼트를 한 번도 안 지웠다**(이름은 접두사 빠진 `sim_follower1.action`), 그 자리 테스트도 **문자열이 소스에 있는지**만 봐서 초록이었다. **SO-101 을 180° 돌려 놓고 쓰는 옵션**: 반 바퀴는 부호 다섯 중 **둘만** 바꾼다(요 joint1·롤 joint6). 피치 셋은 안 바뀐다 — **중력은 안 돌아간다**. so101d 세션(어댑터 열쇠)에 남고, 도는 중 변경은 409(앵커는 오프셋만 흡수하지 방향은 못 흡수한다), **녹화도 같은 표**를 탄다(릴레이만 고치면 데이터셋만 거울상이 되고 학습까지 가서야 보인다). **E-stop 이 상단바 맨 끝**으로(x 가 창 너비에만 달린다 · `shrink-0`). **README 를 밖에서 보는 사람 기준**으로 재작성. 다른 세션: 클라우드 학습 결과가 **로컬과 같은 폴더 모양**으로 앉는다(한 학습이 목록에 둘로 보이던 것 — 이름은 업로드된 `train_config.json` 의 `output_dir` 에 실려 온다). 검증: manifest v0.5.7 · wheel 7개 전부 0.5.7 · 번들 `daemons/simd.py` 에 `owns_segment` 2회·`so101d.py` 에 `set_flipped` · wheel 안 `pairs_for`·`set_flipped`·`_drop_action_segment` · 게이트웨이 안 `STALE_LEASE_S=5.0`, 뒤집힌 관절이 정확히 `[joint1, joint6]` · `piper_cam`/`piper_bus`/`piper_shm` import OK · 레지스트리 넷 다 ☑ · 재발행 경고 안 뜸. ⚠ **적용하면 데몬이 재시작된다**(sim·so101 wheel + 데몬 소스) |
| v0.5.6 | 09-21 | **backend·frontend 이미지** (GHCR + 사설, 14 파일). wheel·데몬 소스 없음 — 이번 변경이 전부 게이트웨이·화면 쪽이다 | **조명 경보를 카메라마다 끈다.** 손목은 팔과 같이 움직여 보는 장면이 계속 바뀌고 판정은 그걸 조명 변화로 읽는다 — 맞는 말이지만 쓸모가 없고, 쓸모없는 경보는 옆의 진짜 경보까지 묻는다(사용자 보고). ⚠ **끄는 것은 경보뿐** — 측정·발행·판정은 계속하므로 수집·추론 화면 표시와 뷰어는 그대로고, 다시 켜면 지금 이상한 것을 그 자리에서 말한다. 클라우드 쪽 셋(다른 세션): 저장한 **Vast 키가 CLI 호출 두 곳에 안 실려** 있어 등록 확인이 401 이고 등록 버튼이 엉뚱한 계정을 가리킬 수 있었다 · **중간 체크포인트 회수**(기본 꺼짐, Hub/scp 판단 앞에서 돈다 — 뒤면 늦다) · 조용한 3~4분이 **스택 설치**임을 화면이 말한다("접속 기다리는 중"이 아니었다). 릴리스: `release.sh` 가 **이미 나간 번호를 다시 끊으려 하면 굽기 전에** 말한다(R10). 검증: manifest v0.5.6 · `light_alarm` 기본 True·`to_dict` 에 실림 · `/api/cameras/light-alarm` 이 이미지 안에서 우리 문구로 404(라우트 있음) · `VastProvider.env` 공개 · `CloudJob.note` 존재 · 번들에 apply.sh · `VideoDecoder OK`(v0.5.5 의 NPP 수정이 살아 있다) · 레지스트리 둘 다 매니페스트 200 · **재발행 경고 안 뜸**(새 번호라 맞다). ⚠ 라우트 유무를 `app.routes` 열거로 보면 FastAPI 버전에 따라 `_IncludedRouter` 가 섞여 **있는 라우트도 없다고 나온다** — TestClient 로 불러서 본다 |
| v0.5.5 | 09-18 14:2x (**3차 발행**) | **backend·frontend 이미지** (GHCR + 사설, 80 파일) + wheel `sim` + 데몬 소스 | 빌린 GPU 로 학습하는 길을 **눌러서 되는 것**으로 만든 릴리스. **빌리는 곳과 학습하는 곳을 갈랐다** — 클라우드 GPU 페이지는 기계만 만들고, 학습 페이지가 빌려 둔 기계를 골라 학습을 건다(학습 설정은 한 곳에만). 돈 쪽으로 넷: ① **고아 스캐너**(기동 30초 뒤 + 10분마다, 경고만 — 자동 파기 안 함) ② 게이트웨이가 임대 도중 죽으면 기동 때 임대 주장을 비워 스캐너가 본다(실기 `kill -9` 로 확인, $0.586/h 로 살아 있었다) ③ **가중치 자동 회수** (Hub 면 파기 **뒤**, 없으면 `scp` 로 파기 **전** — 순서가 반대인 이유는 요금이다) ④ 빌려 둔 기계가 20분 넘게 놀면 지금까지 얼마 나갔는지와 함께 경고. 고르는 자리에서 말해 주는 것 둘: **bf16 이 cc 8.0 미만에서 죽지 않고 느려진다**(하필 제일 싼 V100 $0.125/h·2080Ti $0.095/h), GPU 호환 판정이 **고른 템플릿의 CUDA 빌드**를 따라간다(cu128 은 Blackwell 13종 얻고 맥스웰·파스칼·V100 8종 잃는다 — 상위집합이 아니다). 학습 이미지: **`.ready` 게이트**(접속된 것과 학습할 수 있는 것은 다르다 — 없을 때 3초 만에 죽었다, $0.0145), 기본값을 **slim** 으로(같은 4090 에서 full 14GB 는 8분 상한까지 pull 도 못 끝냈고 slim 은 6분 24초 완주), 스택 설치가 torch 를 **한 번만** 받는다(8155MB/7분59초 → 4240MB/3분53초), 새 이미지 GHCR push + Vast 템플릿 둘 갱신. 다른 세션 몫: 시뮬레이터 **장면 편집 페이지**(테이블 위 물건을 JSON 으로, 런타임 교체, 쓰는 중이면 거부), 스캔·모델링 **메시 가져오기**, 에피소드에 **어느 세계인지** 기록, 집기-놓기 **스크립트 데모**, 관절 모드 제거. 검증: manifest v0.5.5 · 이미지 안 wheel 7개 전부 0.5.5 · `piper_cam`/`piper_bus`/`piper_shm` import OK · 이미지 안에서 `is_orphan`·`Phase.FINISHED`·`.ready` 게이트 확인 · 레지스트리 매니페스트 200. ⚠ **같은 번호로 다시 끊었다**(11:21 → 12:53). 첫 발행이 사설 레지스트리로만 갔고, GHCR 에서 받는 호스트가 v0.5.4 를 최신이라 봤다 — 그 고침과 업데이트 카드 고침을 같은 v0.5.5 에 넣었다(아직 아무 호스트도 이 태그로 적용하지 않은 상태였다). ⚠ **오프라인 tar 경로의 wheel 은 버전이 안 박힌다**(`piper_sim-0.1.0`) — v0.5.3 도 같았던 기존 동작이고, 레지스트리 경로는 이미지 안 0.5.5 wheel 을 쓰므로 영향 없다. ⚠⚠ **세 번째로 같은 번호를 끊었다**(11:21 → 12:53 → 14:2x, 사용자 결정). 이번에 넣은 것: torchcodec 이 링크한 **NPP 런타임**(`libnppicc.so.13` 이 없어 .120 학습이 첫 배치에서 죽었다 — LeRobot 기본 영상 백엔드가 torchcodec 이라 영상을 읽는 모든 경로가 같은 자리에서 죽는다), Vast **API 키 입력 자리**, SSH 키 **"확인 못 했다"를 "등록 안 됐다"로** 말하던 것. 베이스 이미지가 `cu130-2 → cu130-3` 으로 바뀌었다(+516MB). ⚠ **이번 재발행은 앞의 둘과 조건이 다르다.** 앞의 재발행은 "아직 아무 호스트도 이 태그로 적용하지 않은" 상태였지만, 이번엔 **.120 이 이미 v0.5.5 를 적용한 뒤**였다. 그래서 그 기계의 "새 버전 확인"은 `v0.5.5 == v0.5.5` 로 **최신이라 답한다** — 스스로는 영영 안 받는다. 호스트에서 `./piper-install.sh` 를 손으로 한 번 더 돌려야 한다. **다음부터 이런 경우는 번호를 올린다**(적용한 호스트가 하나라도 있으면 같은 번호를 다시 쓰지 않는다). |
| v0.5.4 | 09-15 18:15 | **backend·frontend 이미지** (GHCR, 12 파일) | "개발 머신엔 있는데 배포판엔 없는 것" 이 하루에 세 얼굴로 나온 날. ① 카메라 프로파일 저장 500 — 이미지에 `piper_cam` 이 없는데 게이트웨이가 import 한다(프로파일 저장·적용, 조명 감시, 컨트롤 단위, 정렬 태그, 데이터셋 조명 지표). 이미지에 설치하고, 계약 테스트를 **import 에서 규칙을 끌어내도록 뒤집었다** — 사람이 "데몬 전용"이라 선언하는 대신 코드가 말한다. ② 데몬 호출 실패가 늘 "응답하지 않습니다" 였다 — 죽음·타임아웃·**동사를 모름**(옛 데몬, 재시작해도 안 변함)을 갈라 진짜 사유를 화면까지(camerad·rsd·robotd). ③ `apply.sh` 가 **건너뛴 릴리스의 데몬 소스를 따라잡는다**(`daemons_stale` + 스탬프 `$SRC/daemons/.version`, `--check` 도 보고) — NUC 이 v0.4.19·v0.5.2 를 건너뛰어 camerad 가 9월 1일자로 남아 회색 카드가 "camerad 가 응답하지 않습니다" 였다. 다른 세션 몫: 게이트웨이가 자기 SSH 키 생성(`/data/config/ssh`, `cryptography`), 설정 → 클라우드 탭, 학습 이미지 GHCR 공개 + Vast 템플릿 둘, `vastai` 를 게이트웨이 의존성으로(⚠ 딸린 패키지로 이미지가 무거워진다). 검증: manifest v0.5.4 · 이미지 안에서 `piper_cam`·`cryptography`·`vastai` import OK · 번들 apply.sh 에 `daemons_stale` 3회. ⚠ 이 적용은 데몬을 재시작한다(뒤처진 기계는 소스를 새로 푼다) |

⚠ **v0.4.4 는 체크포인트를 무효화한다.** J6 캘리브레이션을 `(-100000, 130000)` →
`(-120000, 120000)` 으로 고치면서 데이터셋 25 개 339,845 프레임을 재정규화했다
(`tools/migrate_j6_calibration.py`). **2026-09-04 이전에 학습된 체크포인트는 그대로
배포하면 j6 이 10~20° 어긋난다** — 재학습 대상이다. 배포 후 추론을 걸기 전에
체크포인트 학습 시각을 먼저 볼 것.

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
