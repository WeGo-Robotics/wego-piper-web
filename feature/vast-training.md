# 외부 학습 서버 — 임대 GPU (Vast.ai)

[cloud-training.md](cloud-training.md) 5~8단계의 구체화. "이미 켜져 있는 SSH 박스에서 학습"까지는
실기 검증이 끝났다 (300스텝 완주 · 게이트웨이 재시작 후 재부착). 이 문서는 그 다음 —
**필요할 때 빌리고 끝나면 사라지는 서버**를 다룬다. 프로바이더 1호는 Vast.ai(§2).

핵심 결론을 먼저: **러너를 새로 만들지 않는다.** 남은 일은 학습 실행이 아니라
① 서버의 수명(조달·파기), ② 데이터·모델 왕복, ③ 돈이 새지 않는 구조, 그리고
④ **사람이 가입부터 첫 학습까지 막히지 않게 하는 도우미(가이드) 화면**이다.

## 1. 검토 결과 — 어디까지 와 있나 (2026-09-14 갱신)

이 기능의 어려운 절반은 이미 코드에 있다. 전부 실기 또는 테스트로 확인된 것:

| 조각 | 상태 | 근거 |
|---|---|---|
| 원격 실행 (tmux 세션 · 로그 tail · 종료 마커) | ☑ | [ssh.py](../backend/app/services/training/runners/ssh.py) — 실기 300스텝 완주 |
| 게이트웨이 재시작 후 재부착 + 로그 되읽기 | ☑ | [ssh.py `restore()`](../backend/app/services/training/runners/ssh.py#L313) — 재부착 시 138줄 복원 |
| job 레지스트리 (버스 위 → 재시작 생존) | ☑ | [jobs.py](../backend/app/services/training/jobs.py) — `provider`/`instance_id` 필드 자리까지 있음 |
| WS `job_id` · 로그 링버퍼 · REST 페이지네이션 · job 선택 UI | ☑ | [training.py `/jobs`](../backend/app/routers/training.py), [TrainingPage](../frontend/src/pages/TrainingPage.tsx) 의 job `<select>` (2개 이상일 때 나타남) |
| 배타 가드 우회 — 원격 학습은 추론을 안 막는다 | ☑ | [exclusivity.py `_contends()`](../backend/app/services/exclusivity.py#L188) |
| 데이터셋 HF 업로드 (진행 로그 WS 송출) | ☑ | [datasets.py `upload_to_hub`](../backend/app/routers/datasets.py) — `hf upload-large-folder` |
| 모델 HF 다운로드 → `models_dir` 안착 | ☑ | [hub.py `/download`](../backend/app/routers/hub.py) → `model_scanner` 가 자동으로 잡는다 |
| 인자 조립의 경로·인터프리터 분리 | ☑ | [`build_train_args(python=...)`](../backend/app/core/cli_mapping.py#L370) — cloud-training 2단계 산출물 |
| **HF 로그인·네임스페이스 피커·push 권한 사전 검사** | ☑ (09-02, [hf-account.md](hf-account.md)) | 설정 → 저장소 탭, `POST /api/hub/login`, [`_require_push_permission`](../backend/app/routers/training.py#L131) — 토큰이 read 전용이면 **시작 전에** 막는다 |
| 이미지 레이어 캐시 (릴리스마다 390MB 재전송 → 몇 MB) | ☑ (v0.5.0) | 학습 이미지도 같은 규칙으로 굽는다 (§4) |
| **컨테이너 배포판에 `ssh`·`tmux`·`vastai` 가 없다** | ☐ **새로 드러남** | `docker run piper-web-backend:v0.5.0 command -v ssh` → 없음. 개발 머신은 게이트웨이가 저장소에서 직접 돌아 호스트의 ssh 를 쓸 뿐이다. **고객 기계(NUC)에서는 SSH 러너가 "ssh 가 없습니다" 로 끝난다** — §3-4 |
| 인스턴스 조달·파기 (수명 관리) | ☐ | §3 |
| 환경 재현 (학습 이미지) | ◐ 이미지 두 종 + 환경 체크 만듦, push·실측 전 | §4 — [deploy/train/](../deploy/train/), [build-train.sh](../deploy/build-train.sh) |
| 체크포인트 회수 자동화 | ☐ | §5 — `push_to_hub=false` 강제가 함정 |
| 비용 가드 | ☐ | §6 |
| 시크릿 (Vast 키 · HF 토큰) | ☐ | §7 |
| 도우미(가이드) 화면 | ☐ | §9 |

> 09-01 검토 뒤 바뀐 것: HF 로그인이 붙었고(원격에 줄 토큰이 생겼다), 이미지 레이어
> 구조가 정리됐고(학습 이미지를 같은 방식으로 굽는다), 컨테이너에 ssh 가 없다는 전제가
> 드러났다. 나머지 §2~§7 의 판단은 그대로 유효하다.

## 2. Vast.ai 의 제약 → 그대로 설계 결정이 된다

| 확인된 제약 | 강제되는 결정 |
|---|---|
| 인스턴스는 소모품 — 호스트가 바뀌면 로컬 디스크·설치 전부 증발 | 환경은 **Docker 이미지로 고정.** 인스턴스에는 상태를 두지 않는다 |
| Volume 은 물리 호스트에 묶인다 | 영속 저장소로 **부적합** → 데이터·체크포인트는 HF Hub 왕복 (이미 있는 경로, §5) |
| 호스트 NVIDIA 드라이버는 손댈 수 없다 | 이미지 CUDA 를 보수적으로 고정(12.4~12.6)하고 **오퍼 검색에서 `cuda_vers>=` 로 거른다.** 드라이버 버전을 외우는 게 아니라 필터가 답 |
| 경매형(interruptible)은 싸지만 중단된다 | 초기에는 **on-demand 만.** resume 파이프라인 검증 전까지 interruptible 금지 |
| 공식 CLI(`vastai`, PyPI 1.7.0) 존재 · `--raw` 로 JSON | 이 저장소의 원칙(CLI 래핑, subprocess) 그대로 붙는다 — SDK 의존 없음. 응답 JSON 을 fixture 로 녹화해 테스트한다 |
| 임대 인스턴스 = 남의 하드웨어 | 반입 비밀은 **스코프 최소 HF 토큰 하나**로 제한 (§7) |
| SSH 는 프록시(`sshN.vast.ai:<임의 포트>`) 또는 직결, 사용자는 `root` | 러너가 **host·port·user** 를 받아야 한다 (§3-1). 계정에 등록한 공개키가 인스턴스에 심긴다 → 게이트웨이의 키를 계정에 등록하는 절차가 도우미에 들어간다 |

오퍼 검색은 이런 모양이다 (정확한 필드명은 V0 에서 확인):

```
vastai search offers 'gpu_name=RTX_4090 cuda_vers>=12.4 reliability>0.98 inet_down>200 rentable=true' --raw
```

### GPU 선택 기준 · 비용 감각 (실측 전 추정 — V0 이 확정한다)

| 대상 | GPU | 추정 |
|---|---|---|
| ACT (지금의 주 용도) | RTX 3090/4090 24GB | ~$0.3–0.6/h · 40k 스텝 2–4h → **회당 $1–2. 실험 10회 ≈ $10–20** |
| SmolVLA 파인튜닝 | 4090 24GB~ | ACT 와 비슷한 자릿수 |
| Pi0 · 큰 VLA | A100/H100 80GB | 이때만 상위 GPU — cloud-training 첫 표의 그 줄 |

ACT 에 A100/H100 은 낭비다. 비싼 GPU 를 잘 고르는 UI 보다 **싼 GPU 를 안 새게 반납하는
구조(§6)** 가 먼저다.

## 3. 설계 — 검증된 이음매는 그대로, 조달만 끼운다

```
TrainManager ─ TrainRunner ─► SSHRunner(target)   ← 그대로 재사용 (실기 검증됨)
                                  ▲ SSHTarget(host, port, user, key)
                             CloudTrainJob (신규) ← 수명 상태기계: 조달 → 학습 → 회수 → 파기
                                  │
                             VastProvider (신규) ← search / create / wait_ssh / status / destroy / list
```

`CloudProvider` 인터페이스는 [cloud-training §2](cloud-training.md) 정의를 따르되 실제로 필요한
동사로 좁힌다(§8 W2). 러너와 프로바이더를 분리해 뒀기 때문에 **Vast 어댑터는 "SSH 되는
박스를 내놓는 것"까지만 책임진다** — 학습 실행·모니터링·재부착은 이미 있는 코드가 한다.

### SSHRunner 에 필요한 최소 확장 — 전부 배선 수준

1. **접속 대상** — [`_ssh_argv`](../backend/app/services/training/runners/ssh.py#L65) 가 `host` 문자열
   하나만 받는다. `SSHTarget(host, port=22, user="", key_path="", known_hosts="")` 로 바꾸고
   `-p`·`-i`·`-o UserKnownHostsFile=` 를 붙인다. 사내 박스는 `host` 만 채우면 지금과 같다.
   (V0 에서는 `~/.ssh/config` 의 Host alias 로 **코드 0** 우회 가능)
2. **원격 인터프리터** — [`start_training`](../backend/app/routers/training.py#L204) 이
   `build_train_args` 를 기본 python(**로컬 절대경로**)으로 부른다. 사내 박스는 경로가
   우연히 같아 살았지만 이미지에서는 다르다. 원격이면 `python=settings.train_remote_python`
   (새 설정 `PIPER_TRAIN_REMOTE_PYTHON`, 기본 `python`).
3. **원격 env** — `HF_TOKEN`(+`HF_ENDPOINT`, 옵션 `WANDB_API_KEY`)을 `spec.env` 로. 지금은
   [AMP 하나만 간다](../backend/app/routers/training.py#L208). `injected_env()` 를 안 쓰는
   이유(버스 주소 등은 *이 기계의* 사실)는 그대로 — **필요한 것만 명시적으로.**
4. **컨테이너에 ssh 를 넣는다** — 새로 드러난 전제. 배포판 게이트웨이는 컨테이너(root)라
   호스트의 `~/.ssh` 도 `ssh` 바이너리도 없다.
   - `openssh-client` 를 [Dockerfile.base](../backend/Dockerfile.base#L39) apt 목록에 (베이스
     태그 `cu130-3`, `backend/BASE_VERSION` 과 같이). `vastai` 는 회사 코드 옆 앱 이미지에 pip.
   - 키는 **`/data/config/ssh/`** (마운트라 재설치에도 남는다 — hf-account 의 토큰과 같은 이유).
     게이트웨이가 처음 필요할 때 ed25519 를 만들고, 공개키를 `GET /api/cloud/ssh-key` 로 보여
     준다 → 도우미가 "이 키를 Vast 계정에 등록하세요"(또는 `vastai create ssh-key`) 로 안내.
     비밀키는 화면에 절대 안 나간다.
   - `known_hosts` 도 그 디렉토리. `StrictHostKeyChecking=accept-new` 는 그대로 (인스턴스마다
     호스트키가 다르다 — 첫 접속을 받아들이고 이후 변조만 잡는다).
   - 개발 머신(저장소에서 직접 실행)은 지금처럼 `~/.ssh` 를 쓴다 — `key_path` 가 비면 ssh 기본.

### 인스턴스 수명 상태기계

```
searching → creating → ssh_wait → training → retrieving → destroying → destroyed
                                     │ 실패·중지·예산초과·시간초과 — 어느 경로로 끝나든
                                     └──────────────────────────► destroy 는 반드시 지난다
                                                                     └─ 재조회 실패 → orphan (빨간 배너)
```

- 상태는 `JobRecord` 에 얹는다 — 새 필드 `lifecycle`, `instance{offer_id, instance_id, gpu, rate_usd_h,
  ssh_host, ssh_port, image}`, `cost{rate_usd_h, accrued_usd, budget_usd, max_hours}`,
  `artifacts{hub_repo, fetched, local_dir}`. `_from_dict` 가 모르는 필드를 버리므로 옛 레코드와
  섞여도 안 죽는다. 레지스트리는 버스 위라 게이트웨이가 재시작해도 남는다.
- `destroy` 후 **재조회로 소멸 확인** — 실패는 조용히 넘기지 않고 빨간 배너 (cloud-training §7-6).
- **재기동 시** 레지스트리에 `instance_id` 가 있으면 프로바이더에 생사를 묻는다 → 살아 있으면
  기록된 `ssh_host/port` 로 `SSHRunner.restore()` 하고 상태기계를 `training` 부터 잇는다,
  죽었으면 `destroyed` 로 마감 — 로컬 복원과 같은 자리
  ([manager.py `restore_running_process`](../backend/app/services/training/manager.py#L206))에 분기 하나.
- 상태기계는 게이트웨이 안의 asyncio 태스크다. 틱(30초)마다 비용을 누적하고 예산·시간 상한을
  본다. 게이트웨이가 죽어 있는 동안은 아무도 안 보지만 §6-1 의 `timeout` 이 학습을 끝내고,
  재기동한 게이트웨이가 마감한다.

### 실행 위치는 job 마다 고른다

지금 [`_default_runner()`](../backend/app/services/training/manager.py#L30) 는 **설정**으로 러너를
한 번 고른다(`PIPER_TRAIN_SSH_HOST` 가 있으면 SSH). 임대 서버는 "이번 학습만 클라우드로" 가
자연스러우므로 시작 요청에 `where: "local" | "ssh" | "vast"` 가 붙고, `TrainManager` 는 job 마다
러너를 만든다(`runner_factory`). 동시 상한 `MAX_CONCURRENT_JOBS=1` 은 유지 — 하나가 안 새는 것이 먼저다(§8 W5).

## 4. 학습 이미지 — 한 장이면 된다

| 내용물 | 이유 |
|---|---|
| 베이스: PyTorch CUDA 12.x runtime (보수적 버전) | 최신 CUDA 고집 = 쓸 수 있는 호스트 축소. ACT 성능 차이는 없다. ⚠ 우리 배포 이미지는 torch 2.11+**cu130** 인데 Vast 호스트에 CUDA 13 은 드물다 — 학습 이미지는 lerobot 이 요구하는 torch(<2.11, cu124/126)를 그대로 쓴다. 체크포인트는 state dict 라 torch 마이너 차이는 문제없지만 **V0 에서 "원격 학습 → 로컬 추론"으로 확인한다** |
| `lerobot[smolvla]==0.5.0` + `transformers==5.3.0` | [**Dockerfile.base:64 와 같은 핀.**](../backend/Dockerfile.base#L64) 버전이 다르면 체크포인트가 로컬 추론에서 안 열릴 수 있다 (cloud-training §4) |
| `lerobot_policy_act_aux` | ⚠ **act_aux 를 원격에서 학습하려면 필수** — `lerobot-train` 이 접두사로 자동 import 한다. 빼먹으면 "모르는 정책"으로 죽는다 |
| tmux · ffmpeg | tmux 는 SSHRunner 의 전제. ffmpeg 는 데이터셋 디코딩 |
| robot/카메라 vendor 패키지 **없음** | 학습은 데이터셋만 본다 — CAN·RealSense 코드가 원격에 갈 이유가 없다 |

### 변종이 둘 — 포함형(full)과 다운로드형(slim)

"torch 를 이미지에 넣을까, 인스턴스에서 받을까"는 **회선이 정한다.** 같은 5GB 를 GHCR 에서
이미지로 당기느냐, PyPI/PyTorch 인덱스에서 wheel 로 받느냐의 차이인데, 호스트마다 두 경로의
속도가 다르다(사무실 회선 실측: PyTorch 인덱스 15MB/s · PyPI 31MB/s · HF 19MB/s — 같은 기계에서도
목적지마다 두 배 차이). 그래서 둘 다 굽고, 인스턴스 안에서 재서 고른다.

| | full | slim |
|---|---|---|
| 담긴 것 | 스택(lerobot·torch) + act_aux + 스크립트 | act_aux + 스크립트 (스택 없음) |
| pull (실측, 0.5.0-cu126) | **압축 4.47GB** (풀면 4.7GB) | **압축 0.32GB** (풀면 1.0GB) |
| 부팅 | 즉시 학습 가능 | `bootstrap.sh` 가 `install-stack.sh` 로 스택을 깐다 — **wheel 8.2GB 받기 + 설치, 사무실 회선 317초** |
| 유리한 때 | GHCR 회선이 좋을 때 · 같은 호스트를 다시 빌릴 때(레이어 캐시) | PyPI 회선이 좋을 때 · 호스트 캐시를 기대 못 할 때 |

⚠ slim 이 받는 8.2GB 중 **3.5GB 는 버려지는 양**이다 — lerobot 이 자기 핀(torch<2.11)대로 torch 2.10 과
그 CUDA 런타임을 먼저 끌어오고, 추론 기계와 맞추려 걷어낸 뒤 2.11 을 다시 받는다. "추론 기계와
같은 버전"을 포기하고 lerobot 핀대로 2.10 을 쓰면 한 번의 pip 해석으로 끝나 4.7GB 로 준다(§10 결정 8).

두 변종은 **같은 `install-stack.sh`** 로 같은 버전을 깐다 — 다른 것은 "언제"뿐이라 학습 결과의
환경은 같다. 핀은 그 스크립트 한 곳이고 `test_train_image.py` 가 [Dockerfile.base](../backend/Dockerfile.base) 와 대조한다.

- 파일: [deploy/train/Dockerfile](../deploy/train/Dockerfile)(`--build-arg VARIANT=full|slim`, `TORCH_CUDA=cu126`)
  · [install-stack.sh](../deploy/train/install-stack.sh) · [bootstrap.sh](../deploy/train/bootstrap.sh)(멱등, 끝나면 `/opt/piper/.ready`)
  · [env-check.sh](../deploy/train/env-check.sh) · [deploy/build-train.sh](../deploy/build-train.sh)(둘 다 굽고 `--push` 일 때만 올린다).
  **레이어 규칙은 v0.5.0 의 것 그대로** — 스택 5GB 위, act_aux·스크립트·버전 ENV 아래.
- **환경 체크 `env-check.sh`** — 인스턴스 안에서(또는 아무 SSH 박스에서 curl 로) 돌린다. GPU·드라이버·
  sm 세대와 torch 커널 대조(RTX 50 은 cu126 이 조용히 죽는다 → cu128 이미지) · 디스크·/dev/shm · 도구 ·
  스택 유무, 그리고 **실제 쓰는 네 경로의 회선**(PyTorch 인덱스·PyPI·GHCR·HF, 100MB 씩)을 재서
  full/slim 을 추천한다. 마지막 줄 `ENVCHECK {…}` 는 W2 의 프로바이더 층이 읽는다.
- `.ready` 마커가 **러너와의 계약**이다(W1): 있으면 학습 시작, 없으면 "아직 설치 중". `PIPER_TRAIN_IMAGE`
  ENV 로 이미지 태그를 안에 박아 job 레코드가 "어느 환경에서 학습했나"를 읽는다.
- 레지스트리: **GHCR 공개 패키지 `ghcr.io/wego-robotics/piper-train`** 으로 제안. Vast 가 인증 없이
  pull 한다. 회사 코드는 act_aux(순수 파이썬, 정책 정의)뿐이라 공개해도 잃는 게 없다 — 이게
  싫으면 Vast 에 레지스트리 자격증명을 등록해야 한다(§10 결정 2).
- 태그는 `<변종>-<lerobot>-<cuda>-<날짜>` 로 고정(예 `full-0.5.0-cu126-20260914`), 손으로 쓸 때는
  움직이는 `full-cu126`/`slim-cu126`. 오퍼 필터의 `inet_down` 이 pull 시간을 정한다(V0 실측).
- ⚠ 베이스는 `python:3.13-slim` 이라 lerobot 의존성 evdev(sdist)에 gcc 와 `linux/input.h` 가
  필요하다 — `build-essential linux-libc-dev` 를 넣었다. 첫 빌드에서 이걸로 죽었다.
- ⚠ **torchcodec 의 CUDA 빌드는 NPP(`libnppicc`)를 링크하는데 torch wheel 의 의존성에 NPP 가 없다.**
  `import torchcodec` 이 죽으면 lerobot 이 데이터셋 영상을 한 프레임도 못 읽는다. 학습 이미지는
  `nvidia-npp-cu12` 를 깐다(두 번째 빌드가 여기서 죽었다). **추론 기계의 베이스 이미지도 같은
  증상**(`libnppicc.so.13` 없음 — cu13 은 `nvidia-npp`)이라 별도 수정 대상이다.

## 5. 왕복 전송 — 전부 기존 경로 재사용

```
[로컬] upload_to_hub (있음) ──► HF private dataset repo ──► 원격 lerobot-train 이 pull
[원격] --policy.repo_id 로 Hub 푸시 ──► HF private model repo ──► /api/hub/download (있음) ──► models_dir → model_scanner → 추론 페이지
```

- **순서 강제: 업로드 검증 전에는 provision 하지 않는다.** 뒤집히면 빈 GPU 가 과금된다
  (cloud-training §5). 시작 요청에서 `dataset_repo_id` 가 Hub 에 있는지(`get_dataset_info`)
  먼저 확인하고, 없으면 "먼저 업로드하세요" 로 거절 — 데이터셋 페이지의 업로드 버튼으로 보낸다.
- 원격에는 `--dataset.repo_id` 만 주면 된다 — LeRobot 이 알아서 받는다 (`HF_TOKEN` 필요, §3-3).
- **회수의 함정**: [cli_mapping.py:401](../backend/app/core/cli_mapping.py#L401) 이
  `policy_repo_id` 없으면 `--policy.push_to_hub=false` 를 강제한다. 로컬에선 맞고
  임대 서버에선 **회수 경로를 지우는 설정**이다. 원격 러너일 때는 `policy_repo_id` 를
  필수로 하고 기본값을 자동 생성한다: `{네임스페이스}/{데이터셋이름}_{policy}_{YYYYMMDD}` —
  네임스페이스는 hf-account 의 피커(개인/조직)가 이미 준다.
- 학습 종료(마커 code 0) → 기존 다운로드 경로 실행 → `models_dir/<이름>/config.json` 이 생긴 것을
  확인한 뒤에만 destroy.
- **푸시가 실패했을 때의 보험**: 마커 code≠0 이거나 Hub 에 파일이 없으면 destroy 전에
  `scp -r <원격 outputs>/checkpoints/last` 로 직접 끌어온다 — ssh 가 이미 있으니 공짜다.
  몇 시간짜리 결과를 푸시 한 번 실패로 잃지 않는다.
- ⚠ `push_to_hub` 가 최종본만 올리는지 `save_freq` 마다인지 **V0 에서 확인** —
  on-demand 만 쓰는 동안은 치명적이지 않지만 interruptible 을 열려면 필수 지식이다.

## 6. 비용 가드 — "돈 E-stop" 을 Vast 에 맞게

층위별로, 안쪽부터:

1. **`timeout $((MAX_H*3600)) lerobot-train ...`** — 스크립트 수준 상한(`_build_script` 한 줄).
   사람·게이트웨이·인터넷이 전부 사라져도 학습 프로세스는 반드시 끝난다. 종료 마커(`_EXIT_MARK`,
   이미 있음)가 찍히므로 아래 2번이 이어받는다.
2. **종료 마커 수신 → 회수 → destroy** — 정상 경로. 상태기계의 `finally` 자리(§3).
3. **고아 스캐너** — 기동 시 + 10분마다 `vastai show instances --raw` 와 레지스트리를 대조.
   인스턴스는 `--label piper-<job_id>` 로 만들어 우리 것을 구별한다. 레지스트리에 없는 `piper-*`
   = 종료 API 가 실패했는데 성공으로 처리된 경우 → 경고 배너 + [파기] 버튼. 자동 파기는
   안 한다(다른 기계의 게이트웨이가 돌리는 학습일 수 있다 — 결정 5).
4. **예산 상한** — job 생성 시 USD 입력(기본 $10), `rate × 경과` 가 닿으면 정지+파기. `JobRecord.cost`.

> ⚠ **인스턴스 안에서의 자폭(`vastai destroy` from inside)은 하지 않는다.** 그 방법은 계정
> API 키를 남의 하드웨어에 두는 일이다. 1+3 조합이 같은 역할을 한다 — "스스로 죽는" 대신
> "**서버가 반드시 눈치채는**" 구조.
>
> 확인 모달은 논블로킹 React 모달로 — `window.confirm` 은 heartbeat 를 막아 로컬 추론을
> E-stop 시킨다 (실제 사고 전례, cloud-training §7).

UI 에는 항상 시간당 요금 / 누적 / 예상 총액. wandb 는 인스턴스가 사라져도 로그가 남는
유일한 외부 경로라 원격 학습에서는 기본 on 을 검토 (옵션은 이미 있다).

## 7. 시크릿

- `PIPER_VAST_API_KEY` — `.env` 로도, 설정 화면으로도. 화면에서 넣으면 `/data/config/cloud_credentials.json`
  (0600) — HF 토큰과 같은 규칙: **검증(`vastai show user`)에 성공했을 때만 저장, 화면엔 마스킹만.**
  CLI preview 에 싣지 않는다 (`/api/training/preview` 는 명령 문자열을 화면에 그대로 준다 —
  키는 인자가 아니라 `VAST_API_KEY` env 로 subprocess 에 준다).
- **원격에 가는 유일한 비밀 = HF 토큰.** V0 은 로그인 토큰 그대로 보내도 되지만, W3 부터는
  "원격용 HF 토큰" 칸을 따로 두고 **fine-grained 토큰을 새로 발급**하도록 안내한다: 해당
  dataset repo read + 해당 policy repo write 만. 실험 시즌이 끝나면 revoke.
- 로그 브로드캐스트 마스킹 필터 — `hf_…`·`ghp_…`·Vast 키 모양을 `****` 로. `vastai` CLI 가
  키를 에코하는 경우 대비. `_intercept_log` 한 곳에 건다.

## 8. 작업 분해 — 각 단계가 그 자체로 쓸모 있게

| # | 작업 | 산출물 | 코드 | 선행 | 규모 |
|---|---|---|---|---|---|
| **W0** | **수동 완주 1회** | 실측 숫자 + 학습 이미지 | 0 (설정만) | — | 1~2일 (실측 대기 포함) |
| W1 | 러너 확장 + 컨테이너 ssh | alias 트릭 없이 코드 경로로, NUC 에서도 SSH 러너가 뜬다 | 소 | W0 | 1일 |
| W2 | `VastProvider` + 수명 상태기계 + 재부착 + 고아 스캐너 | 웹 버튼으로 빌리고 반납 | 중 | W1 | 3~4일 |
| W3 | 회수 마감 + 비용 가드 4층 + 시크릿 | **방치 가능한 학습** | 중 | W2 | 2일 |
| W4 | UI + 도우미(가이드) | 온보딩 — 처음 쓰는 사람이 혼자 첫 학습까지 | 중 | W3 | 3일 |
| W5 | 동시 N개 | 실험 병렬 | **대** | W3 | 미정 |

**W0 이 이 기획의 절반이다.** 이미지 pull 시간·업로드 시간·실제 시세·`cuda_vers` 분포 같은
"기본값을 정하는 숫자"는 실측 없이 못 정한다. 그리고 **당장 필요한 실험 10회는 W0 상태로도
돈다** — 인스턴스 생성·파기만 손으로 하고, 학습 시작·모니터링·중지·그래프는 웹이 이미 한다.

### W0 — 수동 완주 (코드 0)

사람이 하는 것과 기록할 것을 나눈다.

1. 계정: Vast.ai 가입 · 결제수단 · API 키 발급 · **개발 머신의 공개키를 계정 SSH keys 에 등록**.
   `pipx install vastai`, `vastai set api-key …`.
2. 학습 이미지: ☑ 만들어 둠 — `./deploy/build-train.sh` 가 full·slim 을 굽는다(§4). push 는
   `--push` (GHCR 패키지를 public 으로). 첫 인스턴스는 **slim 으로 띄워** `/opt/piper/env-check.sh`
   를 돌린다 — 그 호스트의 회선·GPU 세대를 보고 다음부터 어느 변종·어느 CUDA 를 쓸지 정한다.
3. 데이터: 실험용 짧은 데이터셋을 데이터셋 페이지에서 HF 로 업로드 (있는 기능).
4. 조달: `vastai search offers '…' --raw` → 4090 하나 → `vastai create instance <id> --image ghcr.io/wego-robotics/piper-train:<태그> --disk 40 --ssh --direct --label piper-v0`
   → `vastai ssh-url <id>` → `~/.ssh/config` 에 `Host vast` alias (HostName·Port·User root).
5. 게이트웨이(개발 머신, 저장소에서 직접 실행): `.env` 에 `PIPER_TRAIN_SSH_HOST=vast`,
   재시작 → **웹에서** 학습 시작(`dataset_repo_id`=업로드한 것, `policy_repo_id` 채움, steps=500)
   → 그래프·로그 확인 → 중지/재개도 한 번.
6. 회수: 저장소 페이지에서 `policy_repo_id` 다운로드 → 모델 페이지에 뜨는지 → **로컬 추론 시작**까지.
7. 파기: `vastai destroy instance <id>` → `vastai show instances --raw` 로 없어졌는지.

기록할 숫자(→ `docs/vast-v0.md`, 기본값의 근거가 된다): 이미지 pull 시간 · 데이터셋 다운로드 시간 ·
$/h 와 오퍼 수 · `cuda_vers` 분포 · 500스텝 시간 · `push_to_hub` 시점(최종/체크포인트마다) ·
`ssh-url` 형태(프록시/직결) · CLI `--raw` 의 실제 JSON 필드명(→ fixture).

### W1 — 러너 확장 + 컨테이너 ssh (코드 소)

| 변경 | 파일 |
|---|---|
| `SSHTarget` dataclass, `_ssh_argv(target, …)` 에 `-p`·`-i`·`UserKnownHostsFile` | `runners/ssh.py` |
| `_build_script`: `timeout` 래핑(`spec.max_hours`), `HF_TOKEN` 등 env 는 이미 `spec.env` 로 감 | `runners/ssh.py`, `spec.py` |
| `train_remote_python`·`train_ssh_port`·`train_ssh_user`·`train_ssh_key` 설정 | `core/config.py` |
| 시작 요청에 `where`, 원격이면 `python=` 넘김 + `policy_repo_id` 필수(자동 기본값) + `HF_TOKEN` 주입 | `routers/training.py` |
| `TrainManager(runner_factory)` — job 마다 러너 생성, 설정 기본값은 지금과 같게 | `training/manager.py` |
| 로그 마스킹 필터 | `training/manager.py::_intercept_log` |
| `openssh-client` (베이스 `cu130-3`), `vastai` pip (앱 이미지) | `Dockerfile.base`, `BASE_VERSION`, `Dockerfile` |
| `/data/config/ssh/` 키 생성 + `GET /api/cloud/ssh-key`(공개키만) | 새 `services/cloud/sshkey.py`, `routers/cloud.py` |

테스트(소스 검사 + 단위): argv 에 포트·키가 들어가는가 · 스크립트에 `timeout` 과 종료 마커가 있는가 ·
원격이면 첫 인자가 로컬 절대경로가 **아닌가** · `policy_repo_id` 없는 원격 시작이 400 인가 ·
마스킹이 `hf_`·`ghp_` 를 지우는가 · 베이스 Dockerfile 에 `openssh-client` 가 있는가 · 공개키
엔드포인트가 비밀키를 안 돌려주는가.

### W2 — VastProvider + 수명 상태기계 (코드 중)

```
backend/app/services/cloud/
  providers/base.py   CloudProvider Protocol: search(filter)→[Offer] · create(offer_id, image, disk_gb, label, env)→Instance
                      · wait_ssh(instance_id, timeout)→SSHTarget · status(instance_id) · destroy(instance_id)→bool(소멸 확인)
                      · list_instances()→[Instance] · whoami()→{balance, …}
  providers/vast.py   `vastai --raw` subprocess 래핑. 키는 env `VAST_API_KEY`. 응답 파싱은 fixture 로 테스트
  providers/fake.py   테스트용 — 시나리오(정상·ssh 안 뜸·파기 실패)를 주입
  job.py              CloudTrainJob 상태기계(asyncio 태스크) + JobRecord 갱신 + 재기동 복원
  orphans.py          기동 시·10분마다 list_instances ↔ 레지스트리 대조 → 배너 상태
```

- REST (`routers/cloud.py`): `GET /api/cloud/providers` · `GET /api/cloud/vast/offers?gpu=&max_price=`
  · `GET /api/cloud/instances` · `DELETE /api/cloud/instances/{id}` · `GET /api/cloud/readiness`
  (키·SSH 키 등록·HF 로그인·이미지 존재를 한 번에 — 도우미가 읽는다).
- 학습 시작 `where="vast"` + `vast{offer_id | gpu, budget_usd, max_hours, image}` → `CloudTrainJob` 이
  provision 하고 `SSHRunner(target)` 로 기존 `TrainManager.start` 를 부른다.
- WS: 기존 `train_state`/`job_list` 에 `lifecycle`·`cost` 가 실린다(레코드 필드라 자동).
- 재기동: `restore_running_process` 의 분기(§3).

테스트(FakeProvider + 러너 스텁): 정상 경로가 `destroyed` 로 끝나는가 · `ssh_wait` 시간초과 → `destroyed`
· 학습 실패 → `destroyed` · 예산 초과 → 정지+파기 · 파기 실패 → `orphan` + 배너 · 재기동 중
`instance_id` 살아 있음 → `training` 재개, 죽음 → `destroyed` · 고아 스캐너가 레지스트리 밖 `piper-*` 를 잡는가.

### W3 — 회수·비용·시크릿 마감

- 회수: 마커 → `hub download`(있음) → `config.json` 확인 → destroy. 실패 시 `scp` 보험(§5).
- 비용: 틱마다 누적, 배지 데이터, 예산·시간 상한.
- 시크릿: `PUT /api/cloud/credentials`(검증 후 저장, 마스킹 반환), `DELETE`. `.env` 값이 있으면 그것이 우선.
- **검증은 파괴적으로**: 학습 중 게이트웨이 `kill -9` → `timeout` 이 학습을 끝내는가 → 재기동한
  게이트웨이가 회수·파기하는가. 레지스트리를 지우고 재기동 → 고아 스캐너가 찾는가.

### W4 — UI + 도우미(가이드)

- 학습 페이지: **실행 위치** 세그먼트(로컬 GPU / 사내 서버 / Vast.ai) → Vast 면 서브폼(GPU·오퍼
  목록·예산·최대 시간·이미지 태그) · 인스턴스 스텝바(searching→…→destroyed) · 비용 배지.
  [TrainingPage.tsx](../frontend/src/pages/TrainingPage.tsx) 가 936줄이라 `TrainWhereForm`·`CloudJobStatus` 로 분리.
- `/cloud` 페이지 (LeRobot 그룹, 라벨 "클라우드", 탭은 제목 옆): **인스턴스** (목록·비용·고아 배너·파기)
  · **자격증명** (Vast 키, 원격용 HF 토큰, 우리 공개키 복사) · **도우미**.
- **도우미 = 체크리스트로 동작하는 가이드** (cloud-training §9 방식). 항목마다 백엔드
  `readiness` 가 ✓/✗ 를 채운다 — 죽은 문서가 아니라 지금 상태:
  1. Vast 계정 · 결제수단 (콘솔 딥링크)
  2. API 키 발급 → 붙여넣기 → [연결 테스트] (잔액 표시)
  3. 게이트웨이 공개키를 Vast 계정에 등록 (복사 버튼 · 확인은 `vastai show ssh-keys`)
  4. HF 로그인 · write 권한 (있는 설정 → 저장소 로 연결)
  5. 학습 이미지 태그 존재 확인
  6. 데이터셋이 Hub 에 있는가 (없으면 데이터셋 페이지 업로드로)
  7. [예상 비용 계산] — GPU·스텝·batch 넣으면 시간·금액 (W0 실측 계수)
  8. 첫 학습 따라하기 (steps=500) → 회수 → 추론
  9. 문제 해결 — 오퍼 없음 / pull 느림 / OOM / 푸시 실패(scp 보험) / 고아 인스턴스
  프로바이더 메타(콘솔 URL·단계 텍스트·주의)는 백엔드 `PROVIDERS` 한 곳, 프론트는 렌더만.
- 프론트 검증은 `cd frontend && npm run build`.

### W5 — 동시 N개

레지스트리·WS 는 이미 N개 준비가 됐지만 [TrainManager 는 싱글톤](../backend/app/services/training/manager.py#L255)
(러너·트래커 1벌)이라 다중화가 실제 작업이다. 병렬 실험은 좋지만, **하나가 안 새는 것**이 먼저다.

## 9. 도우미가 있어야 하는 이유

외부 서비스 세 곳(Vast · HF · GHCR)에 걸친 선결 조건이 8개다. 하나라도 빠지면 증상은 항상
*나중에*(몇 시간 학습 끝에 푸시 실패, 빈 GPU 과금) 나타난다. 그래서 "가입하고 설정하는
가이드"는 문서가 아니라 **시작 버튼을 누르기 전에 전부 초록불인지 보여주는 화면**이어야 한다.
`readiness` 하나로 도우미와 시작 전 검사가 같은 사실을 본다.

## 10. 먼저 정해야 할 것 — 제안 답을 달았다

| # | 결정 | 제안 |
|---|---|---|
| 1 | 프로바이더 1호 = Vast.ai? | **예.** RunPod 으로 바뀌면 §2 표와 `providers/vast.py` 만 다시 쓴다 (러너·전송·가드는 무관) |
| 2 | 학습 이미지 공개(GHCR public)? | **공개.** 담기는 회사 코드는 act_aux 뿐. 비공개면 Vast 에 레지스트리 자격증명 등록 절차가 도우미에 하나 더 |
| 3 | 원격에 줄 HF 토큰 | W0 은 로그인 토큰 그대로, W3 부터 fine-grained 전용 토큰 칸 |
| 4 | 예산·시간 기본값 | job 당 **$10 · 6시간**, on-demand 만. interruptible 은 resume 검증 뒤 |
| 5 | 고아 인스턴스 | **경고 + 버튼.** 자동 파기는 안 한다 |
| 6 | 동시 job 수 | W5 전까지 **1** |
| 7 | HF repo 소속(개인/조직) | hf-account 의 네임스페이스 피커를 따른다 — 데이터 집계 경로와 같은 곳 |
| 8 | 학습 이미지의 torch — 추론 기계와 같은 2.11 vs lerobot 핀대로 2.10 | 지금은 **2.11(같은 버전)**. slim 부팅이 3.5GB 를 더 받는 대가. 체크포인트는 state dict 라 2.10 이어도 열리므로, V0 에서 "2.10 학습 → 2.11 추론"이 되면 2.10 으로 바꿔 4.7GB 로 줄인다 |

## 검증

- **W0**: 짧은 학습(steps=500)으로 한 바퀴 전부 — 업로드 → 원격 학습(웹 그래프 확인) →
  회수 → **회수된 체크포인트로 로컬 추론까지.** 여기까지 안 되면 이후 단계는 무의미.
- **W1**: NUC(컨테이너 배포)에서 `available()` 이 "ssh 가 없습니다" 가 **아니고** 사내 박스로 학습이 뜬다.
- **W2**: 학습 중 게이트웨이 kill → 재기동 → 재부착 확인. 레지스트리를 지우고 재기동 →
  고아 스캐너가 인스턴스를 찾는가. destroy 후 재조회 확인.
- **W3**: **의도적으로 게이트웨이를 죽인 채** timeout 이 학습을 끝내고, 재기동한 게이트웨이가
  인스턴스를 회수·파기하는가. 검증하지 않은 가드는 없는 것과 같다 (cloud-training 검증 절).
- 프론트 변경은 `cd frontend && npm run build` (`npx tsc --noEmit` 은 no-op).

## 상태

◐ W0 진행 중 — 2026-09-14 계획 상세화(§8 W0~W5) · 학습 이미지 두 종과 환경 체크 스크립트를
만들어 로컬에서 구움(push·Vast 실측 전). 다음은 계정·키 발급 뒤 첫 인스턴스에서 `env-check.sh`.
