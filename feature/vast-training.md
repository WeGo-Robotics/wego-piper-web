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

오퍼 검색은 이런 모양이다 (**2026-09-15 실측으로 필드명까지 확인** — 아래 숫자):

```
vastai search offers 'gpu_name=RTX_4090 cuda_vers>=12.4 reliability>0.98 inet_down>200 rentable=true' --raw
```

실측(2026-09-15, 위 필터 그대로): **오퍼 52개**, 최저 **$0.376/h** RTX 4090 ×1
(`cuda_max_good=13.2`, `inet_down=1895Mbps`, `reliability2=0.994`). 걸린 호스트의 드라이버가
전부 12.8 이상이라 **cu126 이미지로 충분**하다 — cu128 변종은 RTX 50 계열을 빌릴 때만.
⚠ 필터의 `reliability` 는 응답에서 `reliability2` 로 온다(파서가 이름을 맞춰야 한다).

### GPU 선택 기준 · 비용 감각 (아래는 추정, 위 실측이 자릿수를 확인해 준다)

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

⚠ **레지스트리에 올린 실제 크기는 위 표와 다르다** (2026-09-15 push 실측):
full **4.70GB** · slim **0.34GB**, 둘 다 레이어 14개. 표의 4.47/0.32GB 는
`docker save | gzip` 기준이고, Vast 는 tar 가 아니라 **GHCR 에서 당기므로** 이쪽이 진짜
pull 량이다. 태그는 `full-0.5.0-cu126-20260914`(digest `sha256:a48cd991…`)와
`slim-…`(`sha256:9a177760…`), 움직이는 `full-cu126`·`slim-cu126` 은 같은 다이제스트를 가리킨다.

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
- 태그는 `<변종>-<lerobot>-<cuda>-<날짜>` 로 고정(지금은 `full-0.5.0-cu126-20260916` — §12-7), 손으로 쓸 때는
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
- `/cloud` 페이지 (LeRobot 그룹, 라벨 "클라우드 GPU", 탭은 제목 옆) — 탭 **둘**:
  **RENT**(오퍼 목록 → 고르기, 기획 §9-2) · **인스턴스**(목록·비용·고아 배너·파기).
  **자격증명과 설정 도우미는 여기가 아니라 설정 → 클라우드 탭**이다(§9-1).
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

### 9-1. 설정 → 클라우드 탭 — "빌리기 전까지"

도우미를 **둘로 가른다.** 한 번 맞춰 두는 것(자격증명·연결)은 **설정**에, 매일 보는
것(인스턴스·비용·고아)은 `/cloud` 페이지에. 둘을 한 화면에 두면 매일 보는 화면에 다시 안
볼 설정이 섞인다.

근거는 옆에 있다. **저장소 탭(HF 계정)이 똑같은 모양**이다 — 토큰을 받아 검증하고, 저장
자리를 적어 주고, 권한을 말해 준다. Vast 키도 같은 물건이다. 반대 방향 선례도 있다:
로그는 설정 탭에서 독립 페이지로 빠졌다(`6388903`). **서비스 탭에는 넣지 않는다** — 그
탭은 *이 기계의* 유닛과 버전을 다루는 곳이고, 거기에 남의 클라우드 계정이 섞이면 "무엇이
이 기계의 사실인가"가 흐려진다.

#### 체크리스트 — `GET /api/cloud/readiness` 하나가 채운다

| # | 항목 | ✓ 판정 | ✗ 일 때 화면이 하는 일 |
|---|---|---|---|
| 0 | CLI | `vastai --version` | 배포판: "이미지 갱신 필요" · 소스: `pip install vastai` 한 줄 |
| 1 | 계정·결제 | 키 검증 응답의 `credit` | 콘솔 딥링크(`cloud.vast.ai`) |
| 2 | API 키 | `vastai show user` 성공 | 입력칸 + [연결 테스트] → 잔액·크레딧 |
| 3 | SSH 키 | `show ssh-keys` 에 **우리 키의 핑거프린트**가 있나 | 공개키 표시 + [Vast 계정에 등록] |
| 4 | 학습 이미지 | 태그가 GHCR 에서 **익명으로** 당겨지나 | 없는 태그 + 공개 전환 안내 |
| 5 | HF write | 있는 `_require_push_permission` | 설정 → 저장소로 보낸다 |

⚠ **CLI 는 "설치 안내"가 아니라 패키징 문제다.** 확인해 보니 `vastai` 는 `pyproject.toml`
에도 어느 Dockerfile 에도 **없다**(§8 W1 이 예고만 해 뒀다). 배포판 게이트웨이는 컨테이너라
사용자가 깔 방법이 자체가 없다 — 화면은 "깔아라"가 아니라 **"이 게이트웨이에 있는가"**를
말해야 하고, 없으면 고쳐야 할 것은 사용자가 아니라 이미지다.
**넣기로 정했다(2026-09-15).** ⚠ 다만 가볍지 않다 — `borb`(PDF)·`lxml`·`pillow`·
`fonttools`·`python-barcode`·`qrcode` 가 딸려 오고 `cryptography` 를 **정확히 핀**한다
(49.0.0). 이미지 환경에서 `pip install --dry-run` 으로 충돌이 없는 것은 확인했다.
무게가 문제가 되면 필요한 서너 호출만 REST 로 치는 선택지가 있다(CLI 래핑 원칙에서는
벗어난다).

⚠ **SSH 등록 확인은 핑거프린트를 본다.** `vastai create ssh-key "<공개키>"` 는 **계정 단위
한 번**이면 이후 인스턴스에 심긴다. 그런데 "키가 하나라도 있으면 ✓"로 판정하면 남의 키가
등록돼 있을 때 거짓 통과하고, 증상은 빌린 뒤 접속 실패로 나타난다. 이미 띄운 인스턴스를
고치는 길은 따로 있다 — `vastai attach ssh <instance_id> <key>`. 복구 버튼으로 둘 값어치가
있다. (2026-09-15 확인: 이 계정의 등록된 키는 **0개**다.)

#### 비밀 취급 — HF 선례를 그대로

[`hub_client.save_token`](../backend/app/services/hub_client.py) 이 정한 규칙을 그대로 쓴다:
**검증에 성공했을 때만 저장**(오타 하나가 몇 시간 뒤 업로드에서야 드러나는 걸 막는다),
`chmod 0600`, 그리고 **저장 자리를 화면에 적는다**(컨테이너와 개발 머신이 달라 "로그인했는데
왜 안 되지"가 났던 자리).

- 키를 화면으로 **되돌려 보내지 않는다** — 마스킹과 잔액만. 비밀키는 어떤 응답에도 안 나가고 공개키만 나간다
- **CLI preview 에 안 싣는다** — `/api/training/preview` 는 명령 문자열을 그대로 화면에 준다.
  키는 인자가 아니라 `VAST_API_KEY` env 로만 subprocess 에 간다(§7)
- 로그 마스킹 필터에 Vast 키 모양을 더한다 — `vastai` 가 키를 에코할 수 있다
- ⚠ `.env` 로 온 키가 우선이고 **화면에서 못 지운다.** 그 사실을 말해 주지 않으면
  "지웠는데 왜 남아 있지"가 난다
- 스코프 키(`vastai create api-key --name --permission_file`)가 가능한 것은 확인했지만 **지금은
  안 쓴다.** 최소 권한은 W3 시크릿 마감에서 — 먼저 되는 것을 만든다

#### 엔드포인트 — 새 이름을 만들지 않는다

W1·W2 가 예고한 그대로다: `GET /api/cloud/providers`(프로바이더 메타는 백엔드 한 곳 —
TSX 에 박으면 정의가 둘로 갈라진다, cloud-training §9) · `GET /api/cloud/readiness` ·
`PUT|DELETE /api/cloud/credentials` · `GET /api/cloud/ssh-key`(공개키만) ·
`POST /api/cloud/ssh-key/register`.

#### 테스트로 못 박을 것

비밀키가 **어떤 응답에도** 안 나가는가 · 검증 실패면 저장 안 하는가 · 마스킹이 Vast 키
모양을 지우는가 · readiness 가 CLI 없음을 "고장"이 아니라 "설치 필요"로 말하는가 ·
`.env` 우선 규칙 · SSH 확인이 **핑거프린트**를 보는가.

#### 순서

⚠ 3번은 **W1 산출물**(게이트웨이 전용 키 `/data/config/ssh/`)에 달려 있다. 그 전까지 V0 은
개발 머신 키를 손으로 등록해 돈다(`vastai create ssh-key "$(cat ~/.ssh/id_ed25519.pub)"`).
0·1·2·4·5 만으로도 탭은 쓸모가 있으므로 W1 앞에 먼저 낼 수 있다. W4 의 도우미 9항목 중
**첫 조각(0~5)**이 여기고, 나머지(첫 학습 따라하기·문제 해결)는 `/cloud` 에 남는다.

### 9-2. `/cloud` → RENT 탭 — "무엇을 빌릴지 고른다"

설정 탭이 *빌리기 전까지*라면 여기는 **고르는 화면**이다. 자격증명은 §9-1 에 그대로 두고
이 페이지는 **쓰는 쪽**만 맡는다.

#### 자리

[pages.ts](../frontend/src/config/pages.ts) 에 엔트리 하나. `/training` **바로 뒤** —
메뉴 순서가 곧 작업 순서다 (수집 → 데이터셋 → 학습 → **클라우드 GPU** → 모델).

```ts
{ path: '/cloud', label: '클라우드 GPU', component: CloudPage, nav: true, group: 'LeRobot', icon: '☁️' }
```

탭은 [RobotsPage](../frontend/src/pages/RobotsPage.tsx#L798) 관용구 그대로 h1 과 **같은 줄**.
`RENT` 와 `인스턴스` **둘을 처음부터** 만든다 — 인스턴스 탭이 이번엔 빈 껍데기여도, 나중에
탭을 끼워 넣으며 레이아웃을 다시 흔드는 것보다 낫다.

#### 준비도는 목록을 막지 않는다

`GET /api/cloud/readiness` → `{cli, api_key, ssh_key:{exists,registered}, balance, templates}`.

빨간불이어도 **표는 그린다.** 세팅하기 전에 가격부터 보고 싶은 게 사람이다. 막는 건
[빌리기] 버튼 하나뿐이고, 옆에 무엇이 모자란지와 설정 탭 링크를 붙인다. 지금 이 계정에서
빨간 건 **SSH 키 계정 등록 0개** 하나다.

#### 백엔드 — W2 계약을 그대로 앞당긴다

```
services/cloud/providers/base.py   Offer(dataclass) · CloudProvider(Protocol)   ← §8 W2 그대로
services/cloud/providers/vast.py   VastProvider.search(f) → [Offer]
                                   = vastai search offers '<q>' --raw  (VAST_API_KEY 는 env 로)
routers/cloud.py  += GET /api/cloud/vast/offers   ?gpu=&max_price=&min_cuda=&disk_gb=&refresh=1
                  += GET /api/cloud/templates     (private=true — 위 ⚠ 참조)
                  += GET /api/cloud/readiness
```

두 가지가 중요하다:

- **쿼리 빌더는 화이트리스트.** UI 값이 vast 질의 **문자열로 연결**되므로 필드명·연산자는
  고정 표에서만 고르고 값은 타입 검사 후 포맷한다. 사용자 문자열이 그대로 붙는 경로를
  만들지 않는다.
- **60초 TTL 캐시 + `?refresh=1`.** `search offers` 는 실측 2~4초다. [api.ts](../frontend/src/services/api.ts)
  의 GET 단일비행은 *동시* 요청만 접지 탭을 오갈 때마다 부르는 건 못 막는다. 가격은 초
  단위로 변하지 않으니 **자동 폴링은 넣지 않고** 새로고침 버튼을 준다.

#### ★ 가격 — `dph_total` 은 우리 가격이 아니다

오퍼 하나(2026-09-16 실측)로 검산하면:

```
dph_base            0.3467     ← GPU 만
storage_total_cost  0.0060     ← 디스크, 그런데 약 5GB 기준
dph_total           0.3527     = 0.3467 + 0.0060  ✓
storage_cost        0.8667     $/GB/월
```

`dph_total` 에 들어간 디스크는 **검색 기본값(~5GB)** 이다. 우리 템플릿은
`recommended_disk_space: 40` 이다. 화면에 쓸 값은:

```
시간당 = dph_base + storage_cost × disk_gb / 730
       = 0.3467  + 0.8667 × 40 / 730
       = 0.3467  + 0.0475  = $0.394/h      ← 표시된 0.3527 보다 12% 비싸다
```

**전송비는 따로**, 시간이 아니라 GB 로 붙는다 — `inet_down_cost` $0.0156/GB(인스턴스로
올릴 때) · `inet_up_cost` $0.0169/GB(체크포인트 내릴 때). 시간당에 섞지 말고 "+ 전송 ~$0.1"
처럼 **별도 줄**로 둔다.

→ 그래서 디스크 슬라이더(기본 40GB)는 필터가 아니라 **가격 입력**이다. 바꾸면 표 전체의
$/h 가 다시 계산돼야 한다.

#### 표 — 열과 그 근거

오퍼 하나에 필드가 **100개**다. 판단에 쓰이는 것만 열로 올린다:

| 열 | 필드 | 왜 |
|---|---|---|
| GPU | `num_gpus`×`gpu_name`, `gpu_ram` | 정체 |
| **$/h** | 위 계산식 | 유일한 비용 축 |
| 가성비 | `dlperf_per_dphtotal` | **기본 정렬** |
| CUDA | `cuda_max_good` | 이미지 호환 |
| 신뢰도 | `reliability2` | 중간에 죽는가 |
| 네트워크 | `inet_down` / `inet_up` | 데이터셋 업로드가 이걸로 갈린다 |
| CPU | `cpu_cores_effective` | 데이터로더 |
| 디스크 | `disk_space`, `disk_bw` | 40GB 필요 |
| 위치 | `geolocation` | 업로드 RTT |
| 잔여 | `duration` | 학습 중 회수 |

#### GPU 고르기 — 고정 목록을 두지 않는다

⚠ **처음 구현이 GPU 7개를 화면에 박아 뒀고, 그게 틀렸다.** 사용자가 쓰려는 **3060 이
없었고**, 대신 **RTX 5090 이 들어 있었다** — Vast 에서 가장 흔한 GPU(오퍼 237개)지만
`sm_120` 이라 우리 cu126 이미지로는 **애초에 못 돈다**. 박아 둔 목록은 이렇게 조용히
썩는다. 그래서 목록은 서버가 만든다 (`GET /api/cloud/gpus`).

**질의 둘로 만든다.** 둘 다 표가 쓰는 필터 그대로에서 항만 뺀 것이라, 선택지와 표가
같은 세계를 본다:

| | 질의 | 실측 |
|---|---|---|
| **A** | 표의 필터에서 GPU 이름만 뺀 것 | 234행 · **55종** — 지금 진짜 고를 수 있는 것 |
| **B** | 거기서 `cuda_vers` 까지 뺀 것 | 412행 · 70종 |
| **B − A** | | **정확히 Blackwell 15종** (RTX 50 전 계열 · RTX PRO · B200/B300) |

⚠ **넓게 훑는 방법은 버렸다.** 처음엔 `rentable=true` 를 정렬 각도 3번으로 훑어 80종을
모았다. 그런데 한 질의는 서버에서 **512개로 잘리고**, 같은 질의를 두 번 돌리면 결과가
갈린다(같은 기종 18행 중 id 가 10개만 겹침). "각도를 13개 더 돌려도 0종 추가" 는
수렴의 증거가 아니라 **그날 뽑기**였다. 반대로 필터를 건 질의는 512 아래로 내려와
잘리지 않고, 이름 집합이 3회 연속 **완전히 동일**했다. 표본을 넓히는 것보다 **범위를
좁히는 쪽**이 정확하다. 8.5초 → 4.8초는 덤이다.

⚠ **첫 행을 믿지 않는다.** 같은 기종 안에서 호스트마다 값이 갈린다 — 실측 RTX 4080S
4대 중 `gpu_ram` 이 16376(2대·실물)과 **32760(1대·거짓)** 으로 섞여 나온다. 최빈값으로
집계하고 동률이면 작은 쪽을 쓴다(거짓 보고는 대개 크게 부풀린다).

⚠ **오퍼 수는 확정값이 아니다.** 같은 질의 3회에 총 행수가 234~238 로 흔들린다. 필드
이름을 `offers_seen` 으로 짓고 화면도 "18대쯤" 이라고 적는다.

#### 세대 — `cuda_vers` 로는 못 보고, 로컬 torch 로 확인하면 틀린다

우리 이미지가 돌릴 수 있는 GPU 세대는 **이미지 자신에게 물어야 한다.** 이 머신의
RTX 5090 에 실제 이미지를 물려 받은 출력(2026-09-16):

```
$ docker run --rm --gpus all ghcr.io/wego-robotics/piper-train:full-cu126 \
      python -c "import torch; torch.cuda.get_arch_list()"
NVIDIA GeForce RTX 5090 with CUDA capability sm_120 is not compatible ...
The current PyTorch install supports CUDA capabilities
    sm_50 sm_60 sm_70 sm_75 sm_80 sm_86 sm_90
```

⚠ **로컬 파이썬의 torch 로 확인하면 틀린다.** 이 머신 torch 는 2.10+**cu128** 이라 arch
list 가 `sm_70~sm_120` 이다 — 맥스웰·파스칼이 빠지고 Blackwell 이 들어 있는 **다른
집합**이다. 처음에 그 값을 근거로 하한을 `CC_MIN=700` 으로 잡았다가 구형 10종(GTX
10xx · Tesla P100 · Titan Xp …)을 **없는 죄로 막을 뻔했다**. 실제 하한은 **500** 이다.

⚠ **`cuda_max_good` 으로는 세대를 못 본다.** Blackwell 호스트는 12.8~13.3 을 보고해
`MIN_CUDA` 검사를 여유롭게 통과한다 — 즉 못 도는 기계에 경고가 **하나도 안 붙는다**.
`compute_cap` 을 따로 봐야 한다.

⚠ **`compute_cap` 에는 실재하지 않는 값이 섞여 있다** — Quadro P2000=140, P4000=243
(실물은 둘 다 파스칼 cc 6.1). 구간만 보고 "너무 구형" 이라 하면 **틀린 답으로 사용자를
막는다**. 알려진 값 집합(`KNOWN_CC`) 밖이면 `unknown` 으로 두고 아무 말도 안 한다.

⚠ **`cuda_vers` 가 Blackwell 을 조용히 걸러내고 있었다.** Vast 의 `cuda_vers` 는 드라이버
최대치가 아니라 *그 기계가 실제로 돌릴 수 있는 CUDA* 다 — 실측 `gpu_name=RTX_5090` 은
`>=12.7` 까지 0개, `>=12.8` 에서 46개다. 그래서 우리 필터가 이미 전부 거르고 있었고,
**그게 문제였다**: 목록을 넓히면 사용자가 RTX 5090 을 고르는 순간 시장에 46대가 있는데도
0행이 뜨고 이유를 아무 데도 안 적어 준다. 그래서 카탈로그가 행마다 `available` 과
`reason` 을 싣고, 빈 표도 그 사유를 그대로 말한다.

⚠ **cu128 은 상위집합이 아니다.** Blackwell 을 얻는 대신 맥스웰·파스칼을 잃는다
(PyTorch 가 2.8~2.9 에서 그 세대를 뺐다). 어느 한 이미지로 70종을 다 덮을 수 없고,
지금 판정은 **기본 템플릿(cu126) 기준**이다.

#### VRAM — 3060(12GB)은 넉넉하다

ACT 기본값(batch 8 · chunk 100 · dim 512 · ResNet18 · 480×640)으로 실측한 학습 스텝
피크(forward+backward+AdamW):

| 구성 | bs4 | bs8 | bs16 |
|---|---|---|---|
| 2캠 · state 7 | 2.45 | **3.91** | 7.73 |
| 3캠 · state 14 | 3.49 | **6.22** | 11.62 |

(GiB 예약. CUDA 컨텍스트 0.4~0.6 GiB 별도.) 즉 **3캠 기본 배치가 약 6.8 GiB** 라
12GB 인 3060 은 여유가 있다. 추론은 1 GiB 미만이라 더 여유다. 4GB(GTX 1650)는 기본
배치로 바로 OOM 이다. bf16 오토캐스트는 메모리를 5% 도 못 줄인다 — **속도 대책이지
메모리 대책이 아니다**.

#### 필터와 경고 — 실측 빈도로 순위를 매긴다

기본값은 **템플릿의 `extra_filters` 를 그대로** 쓴다 (이미 검증된 문자열, 두 템플릿 동일).
프리셋 셋: `[템플릿 기본]` `[최저가]` `[가성비]`. 기본 정렬은 **최저가가 아니라
`dlperf_per_dphtotal`** — 최저가 정렬은 CUDA 12.2 짜리를 맨 위로 올린다.

행 배지는 60개 표본에서 **실제로 걸리는 것만**:

| 경고 | 빈도 | 판정 |
|---|---|---|
| `cpu_cores_effective` < 4 | **3/60** | ⚠ 넣는다 — 데이터로더가 굶는다 |
| `cuda_max_good` < 12.4 | 2/60 | ⚠ 넣는다 (최저가 정렬 시 맨 위로 온다) |
| `duration` 짧음 | 최소 5일 · 중앙 65일 | 열로만. 배지는 안 만든다 |
| `verification` | **60/60 verified** | ✗ **안 만든다** — 4090 에선 무의미 |

마지막 줄이 요점이다. 짐작으로 배지를 넣으면 아무도 안 보는 열이 하나 는다.

#### 고르기 → 확인

행의 버튼은 [빌리기]가 아니라 **[선택]**. 고르면 하단 고정 요약 바:

```
RTX 4090 · Iceland  |  템플릿 full ▾  |  디스크 40GB  |  $0.394/h + 전송
잔액 $25 → 최대 63시간        예산 상한 [$10]        [빌리기]
```

확인은 **논블로킹 React 모달** — `window.confirm` 은 heartbeat 를 막아 로컬 추론을 E-stop
시킨다 (§6 · 실제 사고 전례).

⚠ **이번 범위는 여기까지다** — 목록·필터·선택·확인 모달. 실제 `create` 는 W3 과 함께 간다.
인스턴스를 띄우면 **반드시 파기까지** 있어야 하고(§6 의 고아 스캐너·예산 상한), 그건 이
페이지보다 큰 작업이다. 모달의 [빌리기]는 그때까지 비활성 + "다음 단계" 표기.

#### 템플릿 선택

셀렉트에 한 줄 설명을 붙인다 — **full** 4.70GB pull, 받으면 바로 시작 / **slim** 0.34GB
pull, 부팅 때 스택 설치. 네트워크 600Mbps 급이면 full 이 유리하다 (4.7GB ≈ 1분). 기본
**full**.

#### 테스트로 못 박을 것

- **오퍼 파서 fixture** — 실 JSON 을 `tests/fixtures/vast_offers.json` 으로 저장한다. W0 이
  남긴 숙제이기도 하고, 필드 100개짜리를 손으로 흉내 내면 틀린다
- 가격 계산식 — 위 검산(`0.3467 + 0.0475 = 0.394`)을 그대로 단위 테스트로
- 쿼리 빌더 — 알 수 없는 필드는 거부하는가
- 페이지 등록은 `test_page_registry` 가 이미 잡는다 (추가 작업 없음)
- 프론트 검증은 `cd frontend && npm run build`

#### 안 하는 것

- **자동 가격 폴링** — 가격은 안 변하고 호출은 4초다
- **입찰(`min_bid`·interruptible)** — 뺏기면 체크포인트를 잃는다. on-demand 만 (결정 4)
- **무한 스크롤** — 상위 50개. 60개짜리 결과에 페이징은 군더더기
- **자격증명 UI 중복** — 설정 → 클라우드 하나뿐 (결정 9)

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
| 9 | 자격증명·도우미를 어디에 두나 | **설정 → 클라우드 탭**(§9-1). `/cloud` 페이지는 인스턴스·비용·고아만 — 한 번 맞추는 것과 매일 보는 것을 가른다 |
| 10 | RENT 탭의 범위 | **고르는 데까지**(§9-2) — 목록·필터·선택·확인 모달. `create` 는 파기·예산 가드와 한 몸이라 W3 과 같이 간다 |

## 11. W0 완주 절차서 — 실제로 빌려서 한 바퀴 (2026-09-16 기획)

목표는 좋은 모델이 아니라 **배관이 통하는지**다: 렌트 → 이미지 → 데이터셋 → 학습 시작 →
정상 종료 → 가중치 회수 → 로컬 추론.

### 11-0. 먼저 알아야 할 것 — 자동 가드가 **0개**다

| 있어야 할 것 | 지금 | 근거 |
|---|---|---|
| 학습 프로세스 상한 | ✗ | `_build_script` 에 `timeout` 없음 |
| E-stop 이 원격을 끊음 | ✗ | `SSHRunner.occupies_local_gpu = False`, `pid` 는 항상 None |
| 브라우저 닫으면 정지 | ✗ | 녹화와 반대다 — 원격 학습은 안 죽는다 |
| 예산 상한 | ✗ | RENT 탭 입력칸은 **화면에 찍히기만 한다** ([빌리기]는 disabled) |
| 인스턴스 자동 파기 | ✗ | 백엔드에 `destroy` 코드가 한 줄도 없다 (§9-2 가 일부러 미뤘다) |

→ **사람이 유일한 가드다.** 감시 지점 셋과 벽시계 타이머를 먼저 정하고 시작한다.

### 11-1. 실물로 확인된 함정 다섯

전부 이미지·코드에서 직접 돌려 본 것이다 (추정 아님).

1. ⚠ **SSH 세션에는 `/opt/venv/bin` 이 없다.** 이미지 `ENV PATH` 는 `/opt/venv/bin:…` 인데
   로그인 셸은 `/usr/local/sbin:/usr/local/bin:…` 으로 리셋된다 — `lerobot-train` 도
   `import lerobot` 도 실패한다. 손으로 치는 모든 명령은 **절대경로**를 쓴다.
   `env-check.sh` 도 `command -v python` 에 의존하므로 `PATH=/opt/venv/bin:$PATH` 를 앞에
   붙여야 한다. 안 그러면 torch 없는 python 으로 재서 **멀쩡한 기계를 `arch_ok=false` 로
   파기시킨다.**
2. ⚠ **인터프리터 우회는 심볼릭 링크가 아니라 래퍼 스크립트다.** 실측:
   `ln -sf /opt/venv/bin/python <어딘가>` → `ModuleNotFoundError: No module named 'lerobot'`
   (venv 가 *해석된* 경로 옆에서 `pyvenv.cfg` 를 찾는다). `#!/bin/sh` + `exec /opt/venv/bin/python "$@"`
   래퍼는 통과. **가장 자연스러운 우회가 조용히 실패하는 자리다.**
   ⚠ 그리고 링크를 만든 **뒤** 그 경로에 `>` 로 쓰면 링크를 따라가 실제 CPython 을 덮어쓴다 —
   래퍼가 자기 자신을 exec 하는 무한 루프가 되고 `python -V` 조차 안 돈다. **쓰기 전에 `rm -f`.**
3. ⚠ **tmux 서버가 환경을 얼린다.** 실측: 서버 기동 *전* 설정한 변수는 새 세션에 보이고(1),
   *후* 설정한 것은 **안 보인다**(0). 그래서 HF 토큰은 env 가 아니라 **파일**로 심는다
   (`/root/.cache/huggingface/token`). 덤으로 토큰이 `ps`/argv 에 안 남는다.
4. ⚠ **즉사가 정상 완주와 구별되지 않는다.** [`ssh.py:242`](../backend/app/services/training/runners/ssh.py#L242)
   의 `_start_log_stream()` 은 기본이 `tail -n 0 -f` 다(`restore()` 만 `from_start=True`).
   1초 안에 죽는 실패(exit 127 · draccus 인자 오류 · unknown policy type)는 파일에 에러가
   다 쓰여 있어도 **새 줄이 없어** `_EXIT_MARK` 를 못 타고 `_finish(IDLE)` 로 끝난다.
   `IDLE` 은 **정상 완주와 같은 값**이다.
   → 시작 직후 **원격 로그 파일을 직접 읽는다.** 화면 상태를 성공으로 읽지 않는다.
5. ⚠ **`/api/training/jobs` 의 `runner` 필드는 화석이다.** 지금 실측: `runner:"systemd"` ·
   `state:"idle"` 인데 `metrics.state:"running", step:20000` 로 얼어 있다(어제 값).
   `_sync_record()` 가 재기동 때 안 돌기 때문이다. **이 필드로 러너를 판정하지 마라** —
   시작 전에 `DELETE /api/training/jobs/local` 로 치우고, 판정은 시작 **후** 교차 확인으로.

### 11-2. 돈 안 드는 관문 — 여기가 초록이어야 인스턴스를 만든다

§5 의 규율("업로드 검증 전에는 provision 하지 않는다")을 앞으로 더 당긴다.

| # | 관문 | 통과 조건 |
|---|---|---|
| 0 | 출발선 기록 | `show instances` 가 **빈 배열**(타입 검사 포함) · `credit` · 청구 건수 셋을 적는다 |
| 1 | SSH 키 지문 | 계정 등록 키 = 게이트웨이 키 `SHA256:uPtx…` (✅ 확인됨) |
| 2 | 데이터셋 업로드 | ☑ **통과(2026-09-16)** — `sim_data2` 토큰 조회 200 · `private=false` · 실질 7개 파일이 **바이트까지 일치**(영상 4.91MB·7.17MB 실물, LFS 포인터 아님) |
| 3 | ⭐ **로컬 CPU 드라이런** | ☑ **통과(2026-09-16)** — 아래 11-2-a |

⭐ **3번이 가성비의 핵심이다.** 인자 조립 · act_aux 로드 · HF 다운로드 · **torchcodec 영상
디코드** · 푸시 경로가 전부 지상에서 검증된다. 통과하면 임대 인스턴스가 새로 증명할 것은
**GPU 커널과 SSH 배관 둘뿐**이다.

⚠ **데이터셋 존재 확인은 반드시 토큰을 붙인다.** 비인증 조회는 "없음"·"private"·"실패" 를
전부 뭉갠다. 그리고 지금 `GET /api/hub/datasets/{repo}` 는 **404 를 500 으로 바꾼다**
(`hub_dataset_detail` 이 `HfHubHTTPError` 를 안 잡는다) — 이 경로로는 "안 올라감" 과
"Hub 장애" 를 구별할 수 없다. 작은 수정거리다.

#### 11-2-a. CPU 드라이런 결과 — §5 의 숙제가 풀렸다

`sim_data2` · ACT · **2스텝** · batch 2 · `device=cpu` · `amp=off` 로, **앱 자신의
`POST /api/training/preview` 가 만든 인자** 그대로 로컬 full 이미지에서 돌렸다.
`argv[0]` 만 `/opt/venv/bin/python` 으로 바꿨다(그게 곧 W1 이 고칠 한 자리다).
HF 캐시는 **비운 채**로 줘서 다운로드 경로도 함께 지나갔다.

```
데이터셋 Hub 다운로드 ✓ (4초)   영상 코덱 libsvtav1 ✓   ACT 52M 파라미터 ✓
step:1 loss:91.383 / step:2 loss:72.037 ✓   End of training ✓
Model pushed to https://huggingface.co/wego-hansu/w0-dryrun-throwaway ✓   (푸시 ~32초)
```

**⚠ `push_to_hub` 는 커밋을 여러 번 낸다 — 파일 9개 · 커밋 4개.**

| 커밋 | 올라가는 것 |
|---|---|
| `initial commit` | repo 생성 |
| `Upload policy weights, train config and readme` | `config.json` · `model.safetensors` · `train_config.json` · `README.md` |
| `Upload DataProcessorPipeline` | `policy_preprocessor.json` + `..._step_3_normalizer_processor.safetensors` |
| `Upload DataProcessorPipeline` | `policy_postprocessor.json` + `..._step_0_unnormalizer_processor.safetensors` |

→ **합격 조건은 "파일이 생겼다" 가 아니라 파일 9개 · 커밋 4개다.** 프로세서 커밋 둘이
빠지면 가중치는 멀쩡히 받아지고 **추론도 정상적으로 뜨는데 정규화 없이 돈다** — 이
경로에서 가장 큰 거짓 통과다.

⚠ **가중치가 `private=false` 로 나갔다.** 드라이런은 무해하지만 실전에서는
`--policy.private=true` 에 해당하는 설정을 확인하고 돌려야 한다.

⚠ **아직 안 풀린 조각**: 중간 체크포인트도 매번 푸시되는지는 여전히 모른다. 2스텝에
`save_freq=2` 라 체크포인트와 종료가 같은 순간이었다. **Phase 2(5000스텝 ·
`save_freq=1000`)가 그걸 가른다.**

### 11-3. 무엇으로 도나

| | 고른 것 | 왜 |
|---|---|---|
| 데이터셋 | **`wego-hansu/sim_data2`** (25ep · 10403프레임 · 2캠 · **13M**) | 사용자 지정(2026-09-16). 영상 2스트림(cam0·cam1)이라 torchcodec/NPP 경로를 지난다. ⚠ 이름이 비슷한 `sim_data` 는 **영상이 없어**(state 전용) 그 경로를 건너뛴다 — 헷갈리면 안 된다 |
| 이미지 | **full** | 전송 4.70GB → 후보 기계에서 pull **0.5~2.6분 · $0.0005**. 부팅 설치 실패 경로가 통째로 사라진다. ⚠ §8 W0 의 "slim 먼저" 는 회선을 모르던 때의 판단이다 |
| GPU | **RTX 3060** `$0.064/h` (`compute_cap 860` = sm_86) | 이미지 arch list 에 문자 그대로 있다 — 변수가 하나 준다. 한국 소재도 $0.071/h |
| 스텝 | 500 (`save_freq=100` · `log_freq=25`) | ⚠ 폼 기본값은 `steps=100000` · `save_freq=20000` 이고 **localStorage 에서 복원된다**. 500스텝 · batch 8 = 약 **0.38 에포크**(10403프레임) — 배관 확인에 충분하다 |

⚠ **중국 본토(CN) 오퍼는 고르지 않는다** — `huggingface.co` 가 막혀 다운로드도 푸시도 실패한다.
최저가 정렬 맨 위가 그 함정일 때가 있다.

### 11-4. 절차 요약

```
[돈 0]  0 출발선 · 1 키지문 · 2 업로드 대조 · 3 ⭐CPU 드라이런
        ↓  (전부 초록일 때만)
[과금]  4 오퍼 3개 선정 → 5 create (--template_hash · --disk 40 · --ssh --direct
          --label · --cancel-unavail)          ⏰ 타이머 시작
        6 running 대기                          ⏰ 감시① 8분
        7 ssh alias (⚠ IdentityFile 필수)
        8 ⭐원격 준비: 래퍼(rm -f 먼저) + 토큰 파일 + tmux 비우기 → 한 줄로 동시 증명
        9 게이트웨이를 원격 러너로 (SSH 확인이 재기동 **전**에 끝나 있어야 한다)
       10 학습 시작 — preview 로 인자 눈으로 확인
       11 ⭐교차 확인                            ⏰ 감시② 3분
       12 완주 판정 (로그 3줄 + Hub **파일 9개 · 커밋 4개**)
       13 (조건부) scp 보험
       14 ⭐파기 — 세 겹 확인
       15 회수 → 로컬 추론   ← **여기까지가 종료선**
       16 뒷정리 (⚠ 반드시 파기 **뒤에**)
```

**11번 교차 확인이 이 테스트의 심장이다.** 넷을 다 봐야 "클라우드에서 GPU 로 돈다" 가 증명된다:
① 원격 로그에 `step=` 메트릭이 흐른다 ② `ssh vast 'tmux ls'` 에 세션이 있다
③ **로컬 `nvidia-smi` 가 비어 있고** 원격에 python+VRAM 이 보인다 ④ 로그에 `Switching to` 가 없다.
각각 조용한 로컬 폴백 / 원격 미기동 / CPU 폴백을 잡는다 — **셋 다 화면은 초록이고 loss 도 내려간다.**

⚠ **`policy_repo_id` 를 비우면 [`cli_mapping.py:401`](../backend/app/core/cli_mapping.py#L401) 이
`--policy.push_to_hub=false` 를 강제한다** — 학습은 멀쩡히 끝나고 **회수 경로만 사라진다.**

### 11-5. 중단 규칙 — 감시 지점 셋과 벽시계

- ⏰ **감시① create 후 8분** — `running` 이 아니거나 `exited`/`offline` 이면 파기하고 2순위로.
  순수 pull 은 계산상 1분 안쪽이다. 남의 느린 호스트를 시계 켜 둔 채 디버깅하지 않는다.
- ⏰ **감시② 시작 후 3분** — 원격 로그 첫 줄이 없으면 즉사다(화면은 IDLE). 로그를 읽고
  15분 안에 원인이 안 잡히면 파기하고 **지상에서 고친다**.
- ⏰ **벽시계 45분** — 결과와 무관하게 파기. 휴대폰 타이머를 create 와 동시에 맞춘다.
- **자리를 비우기 전에 반드시 파기한다.** "잠깐 두고 보자" 가 없는 구조다 — 점심·퇴근·잠은
  전부 파기 사유다.
- ⚠ **[중지] 를 눌렀다고 멈춘 게 아니다.** `SSHRunner.stop()` 은 `tmux kill-session` 의
  returncode 를 **한 번도 안 보고** 예외를 warning 으로 삼킨 뒤 무조건 `_finish(IDLE)` 한다.
  중지 직후 `tmux ls` 로 세션 부재를 눈으로 확인한다.
- **파기 확인은 세 겹**: ① `.success == true` 를 **파싱**(종료코드 금지 — CLI 는 오류도 0 으로
  내고 본문에 `{"error": true}` 를 싣는다) ② 목록이 **빈 배열**인지 타입까지 검사
  ③ 청구 건수 마감 — 이게 Vast 서버의 정산이라 CLI 파싱 사고와 무관한 최종 심판이다.

### 11-6. 코드 수정 — 필수 0줄, 권장 1줄

우회가 전부 원격 셸과 입력칸에서 끝나므로 **W0 한 바퀴는 코드 변경 없이 돈다.**

권장 1줄은 절약이 아니라 **진단**을 위한 것이다:
`ssh.py:242` `self._start_log_stream()` → `self._start_log_stream(from_start=True)`.
`start()` 가 `: > {log}` 로 로그를 비우므로 이전 실행분이 섞이지 않는다(부작용 없음).

⚠ **`settings.grpc_python` 을 `.env` 로 덮는 우회는 쓰지 마라** — 정책서버·yolod·래퍼·녹화·
편집 등 **7곳이 같은 값을 쓴다**. 대신 원격에 래퍼를 심는다(11-1 ②).

W1 으로 넘길 진짜 수정 넷:
1. `routers/training.py:204` — 원격이면 `build_train_args(params, python=…)`.
   ⚠ 배선은 **이미 있다** (`build_train_args` 가 `python=` 를 받는다). 기본값은 `python` 이
   아니라 **`/opt/venv/bin/python`** 이어야 한다 — 로그인 셸 PATH 에 없다.
2. HF 토큰 전달 — ⚠ **env 로는 tmux 를 못 넘는다.** 러너가 토큰 **파일**을 심어야 한다.
3. `cli_mapping.py:401` — 원격이면 `policy_repo_id` 필수 + 자동 생성.
4. `manager.get_status()` — 현재 러너를 **실시간으로** 싣는다(레코드 필드는 화석이다).
   조용한 로컬 폴백이 이 테스트에서 가장 비싼 실패다.

### 11-7. 기록할 숫자 (다음 단계 기본값의 근거)

create→running 시간 · pull 실측 시간 · `ssh-url` 이 직결인지 프록시인지 · 데이터셋 다운로드
시간 · 500스텝 소요와 `updt_s` · `push_to_hub` 가 최종본만인지 `save_freq` 마다인지 ·
Hub 에 올라간 파일 목록 · 총 청구액.

### 11-8. 2단계로 나눈다 — 같은 데이터셋, 길이만 다르게

데이터셋이 `sim_data2` 하나로 정해졌으므로(사용자 지정) 단계를 **데이터셋 크기가 아니라
학습 길이**로 가른다. 13MB 라 다운로드는 어느 쪽이든 1초 안쪽이고, 더 작은 것을 쓸
비용상의 이유가 없다.

| | 스텝 | 목적 | 예상 |
|---|---|---|---|
| **Phase 1** | 500 (`save_freq=100`) | **배관만** — 한 바퀴가 도는가 | **$0.03~0.05** |
| **Phase 2** | 5000 (`save_freq=1000`) | **숫자를 잰다** — it/s · loss 추세 · 체크포인트 반복 푸시 | **$0.10~0.20** |

Phase 1 에 회색이 하나라도 있으면 Phase 2 로 가지 않는다 — 같은 실수를 몇 배 비싸게 배운다.

⚠ Phase 2 가 따로 필요한 이유는 **`push_to_hub` 의 시점**이다. 500스텝·`save_freq=100`
이면 체크포인트가 5번 생기지만 그게 매번 Hub 로 가는지 최종본만 가는지는 아직 모른다
(§5 가 남긴 숙제). 5000스텝이면 그 차이가 분명히 드러난다.

## 12. W0 완주 — 실행 결과 (2026-09-16)

**한 바퀴 돌았다.** 렌트 → 템플릿 → 데이터셋 → 학습 → 정상 종료 → 가중치 회수 →
로컬 로드까지. 총 **$0.054** (크레딧 25.0 → 24.946), 인스턴스 3개 전부 파기 확인.

### 12-1. ⚠ 템플릿으로는 SSH 가 아예 안 됐다 — 함정 둘

1차 시도가 7단계에서 죽었다. 인스턴스 sshd 로그가 원인을 정확히 말해 줬다:

```
Authentication refused: bad ownership or modes for file /root/.ssh/authorized_keys
Failed publickey for root ... SHA256:uPtxSECJTpjsfo/423Vxo2MZU8sMjdCy67MXH7jJUnw
```

**키는 맞았다** — sshd 가 우리 지문을 그대로 찍었다. 막은 것은 **파일 권한**이다.
그리고 그걸 넘기니 두 번째 벽이 나왔다. 둘 다 이미지가 고쳐야 한다:

| # | 증상 | 원인 | 고침 |
|---|---|---|---|
| ① | `Permission denied (publickey)` | Vast 가 심은 `authorized_keys` 의 권한이 우리 이미지의 sshd(`StrictModes yes` 기본)를 통과 못 한다 | `chmod 700 /root/.ssh` · `chmod 600 …/authorized_keys` |
| ② | `open terminal failed: not a terminal` / `duplicate session: ssh_tmux` | Vast 가 `/root/.bashrc` 에서 **모든 SSH 세션을 tmux 로 감싼다**. bash 는 sshd 로 불릴 때 비대화식이어도 `.bashrc` 를 읽으므로 `ssh host 'cmd'` 가 통째로 막힌다 | `touch /root/.no_auto_tmux` (Vast 가 문서화한 공식 스위치) |

`/root/.bashrc:20` 실물:
```bash
if [ ! -e "$HOME/.no_auto_tmux" ] && [[ -z "$TMUX" ]] && [ "$SSH_CONNECTION" != "" ] …; then
    tmux attach-session -t ssh_tmux || tmux new-session -s ssh_tmux; exit;
```

⚠ **`SSHRunner` 는 `ssh host 'cmd'` 로만 동작한다.** ②를 안 끄면 원격 학습이 **한 줄도**
못 돈다. 둘 다 `bootstrap.sh` 에 넣어야 할 두 줄이다 — 이번엔 `--onstart-cmd` 로 우회했다.

⚠ 진단 중에 알아낸 것: `vastai execute` 는 **정지된 인스턴스에만** 되고 `chmod` 는
화이트리스트 밖이다 — **살아 있는 인스턴스는 못 고친다.** 파기하고 다시 띄우는 수밖에 없다.

### 12-2. 절차서가 예고한 함정이 그대로 터졌다

- **A12 적중** — `vastai destroy instance <id>` 를 `-y` 없이 치면 `[y/N]` 에서 막혀
  `Aborted.` 로 끝나고 **인스턴스는 살아 있다.** 미리 안 적어 뒀으면 "파기했다" 로
  넘어갈 뻔했다. 추가 실측: `destroy -y --raw` 는 **빈 출력**을 낸다 — 응답 파싱으로는
  판정할 수 없고, 목록·라벨·청구 세 겹이 필요하다.
- **감시① 적중** — 2차 인스턴스가 12분째 `Pulling fs layer` 에 멈췄다(1차는 같은
  이미지를 1분에 받았다). 규율대로 파기하고 다음 오퍼로 갔다. $0.003 에 끝났다.
- **인터프리터 래퍼 적중** — 심볼릭 링크는 쓰지 않았다(실측으로 실패를 확인해 뒀다).

### 12-3. 실측 숫자 (다음 단계 기본값의 근거)

| | |
|---|---|
| 기계 | RTX 3060 · `compute_cap 8.6` · 12GB · **$0.0644/h** · New Jersey ↓2028Mbps |
| slim pull + bootstrap | pull 수십 초 + **bootstrap 182초** → 총 ~5분 만에 학습 가능 |
| 설치된 스택 | `lerobot 0.5.0 · torch 2.11.0+cu126 · torchcodec 0.11.0+cu126` |
| 학습 속도 | **6.3 step/s** (`updt_s 0.147` · `data_s 0.013`, batch 8, 2캠 480×640) |
| 500스텝 | **94초** (05:18:06 → 05:19:40) · loss 21.5 → **2.435** · `epch 0.38` |
| GPU 메모리 | **2790 MiB** — 12GB 카드에 한참 여유 |
| 체크포인트 | 5개(`000100`~`000500`) + `last` = **2.9GB**. `last/pretrained_model` 198MB vs `training_state` **394MB** |
| 회수 | Hub 파일 9개 · 커밋 4개 · `model.safetensors` 207MB |
| 총 비용 | **$0.054** (3회 시도 합계) |

### 12-4. ⚠ `push_to_hub` 는 **끝에 한 번만** 올린다

§5 가 남긴 숙제의 답이다. `save_freq=100` 으로 체크포인트가 **5개** 생겼는데 Hub 커밋은
전부 `05:19:40~46` — **종료 시점 한 묶음뿐**이다.

→ **인스턴스가 중간에 죽으면 Hub 에는 아무것도 없다.** §5 의 `scp` 보험은 선택이 아니라
필수 경로다. interruptible 을 열 수 없는 이유이기도 하다(결정 4가 옳았다).

### 12-5. 거짓 통과를 막은 확인들

- **4중 교차 확인**(11단계)이 전부 초록: 원격 로그 메트릭 · `tmux ls` 세션 ·
  **로컬 `nvidia-smi` 비어 있음** + 원격에 `/opt/venv/bin/python 2790MiB` · `Switching to` 0건.
- **`train_config.json` 이 영수증**: `device: cuda` · `steps: 500` · `batch_size: 8` ·
  `dataset: wego-hansu/sim_data2`.
- **정규화 통계가 실물**: 로컬에서 `make_pre_post_processors` 로 열어 보니
  `NormalizerProcessorStep.action.count = 10403` — **`sim_data2` 의 프레임 수와 정확히 일치**.
  프로세서가 빈 값으로 온 게 아니라는 가장 강한 증거다(가장 큰 거짓 통과 구멍이 막혔다).
- 정책 로드: **51.6M 파라미터** — 원격의 `num_learnable_params=51599239` 와 일치.

### 12-6. 다음 (W1 로 넘길 것)

1. **이미지에 두 줄** — `bootstrap.sh` 에 권한 교정 + `no_auto_tmux`. 이게 없으면
   템플릿으로 뜬 인스턴스에 아무도 접속할 수 없다. **최우선.**
2. `routers/training.py:204` — 원격이면 `build_train_args(python="/opt/venv/bin/python")`.
   이번엔 원격에 래퍼를 심어 우회했다.
3. HF 토큰 전달 — 러너가 **토큰 파일**을 심는다(env 는 tmux 를 못 넘는다).
4. `cli_mapping.py:401` — 원격이면 `policy_repo_id` 필수 + 자동 생성.
5. `ssh.py:242` → `from_start=True` (즉사와 정상 완주가 화면에서 같은 값이다).
6. `hub_dataset_detail` 이 404 를 500 으로 바꾼다 — 사전 검증이 "안 올라감" 과 "Hub 장애" 를
   구별 못 한다.
7. **Phase 2 는 아직이다** — 5000스텝으로 loss 추세와 it/s 를 재는 회차.

### 12-7. 이미지에 고침을 실었다 (2026-09-16) — 그리고 템플릿을 한 번 날렸다

§12-1 의 두 줄이 이미지 안으로 들어갔다. 그게 없으면 이 템플릿으로 띄운 인스턴스에
**아무도 접속할 수 없다.**

| | |
|---|---|
| 새 태그 | `full-0.5.0-cu126-20260916` · `slim-0.5.0-cu126-20260916` (+ 이동 태그 `full-cu126`·`slim-cu126`) |
| 익명 pull | ☑ 네 태그 모두 200 — Vast 가 받을 수 있다 |
| 드리프트 | ☑ **없음** — 스택 레이어가 캐시돼 `lerobot 0.5.0 · torch 2.11.0+cu126 · torchvision 0.26.0+cu126 · torchcodec 0.11.0+cu126`, arch `sm_50 … sm_90` 이 §12-3 실측과 **문자 그대로 같다** |
| 교정 동작 | ☑ 새 이미지에서 `777/666 → 700/600` · `no_auto_tmux` 생성 · 2회차 정지 없음 |

⚠ **재빌드가 무해한 것은 이번이 운이 좋았기 때문이다.** `install-stack.sh` 는 lerobot 만
핀하고 transitive 의존성은 떠 있다. 이번엔 pip 레이어가 캐시에서 재사용돼 같은 이미지가
나왔지만, 캐시가 없는 기계에서 구우면 **다른 것이 나올 수 있다.** 그래서 구운 뒤에는
항상 스택 버전과 `get_arch_list()` 를 위 표와 대조한다 — 그게 이 표가 여기 있는 이유다.

#### ⚠ `vastai update template` 은 patch 가 아니라 **전체 교체**다

`--image_tag` 하나만 주고 태그를 바꾸려다 **템플릿 두 개를 망가뜨렸다.** 안 넘긴 필드가
전부 지워진다:

| 필드 | 이전 | `--image_tag` 만 준 뒤 |
|---|---|---|
| `image` | `ghcr.io/wego-robotics/piper-train` | **None** |
| `onstart` | `/opt/piper/bootstrap.sh` | **None** |
| `runtype` | `ssh` (+`ssh_direct`) | **`args`** |
| `recommended_disk_space` | 40 | **None** |
| `extra_filters` | RTX 4090 · CUDA · 신뢰도 · 회선 | **기본값으로 초기화** |

그 상태로는 렌트가 아예 안 된다(이미지가 없다). 기록해 둔 값으로 전부 다시 넘겨
복구했고, **응답이 아니라 `search templates` 목록으로** 확인했다.

→ **템플릿을 고칠 때는 언제나 전체 필드를 다시 넘긴다.** 그리고 고친 뒤 목록을 다시 읽어
`image`·`onstart`·`runtype`·`disk_space`·필터가 살아 있는지 본다.

⚠ **`hash_id` 는 고칠 때마다 바뀐다.** `id` 는 그대로다(728456/728457). `create instance
--template_hash` 에 쓰는 것은 **해시**이므로, 어딘가에 적어 둔 해시는 템플릿을 한 번
고치는 순간 낡는다 — 그래서 아래를 문서에 박지 말고 **쓰기 직전에 조회**하는 편이 낫다.

```
vastai search templates 'private=true' --raw | jq -r '.[] | "\(.id) \(.name) \(.hash_id) \(.tag)"'
```

2026-09-16 현재: full `a24b94acfacb286811fecb5a1b68aeb3` · slim `6cb80da3a4b63e37f9f0f6636aff2654`.

#### 다음 — Phase 2 가 증명할 것

Phase 1 은 `--onstart-cmd` **우회로** 통과했다. 그래서 이미지에 넣은 두 줄이 실제로 일하는지는
아직 증명되지 않았다. Phase 2(5000스텝 · `save_freq=1000`)가 확인할 것은 둘이다:

1. **우회 없이, 템플릿만으로 SSH 가 되는가** — 오늘 고친 것의 유일한 증명이다.
2. **`push_to_hub` 가 정말 끝에 한 번뿐인가** — 체크포인트가 5개 생기므로 §12-4 의 관찰이
   확정된다. 그러면 `scp` 보험은 선택이 아니라 **필수 경로**다.

### 12-8. Phase 2 — 이미지 수정이 증명됐고, 푸시 시점이 확정됐다 (2026-09-16)

Phase 1 은 `--onstart-cmd` **우회로** 통과했다. 그래서 §12-1 의 두 줄이 실제로 일하는지는
증명되지 않은 상태였다. Phase 2 는 **우회 없이 템플릿만으로** 같은 길을 갔다.

| | Phase 1 (사람이 손으로) | Phase 2 (코드·이미지가) |
|---|---|---|
| SSH | `--onstart-cmd` 우회 · `Permission denied` 5회 · 진단 20분 | **running 직후 첫 시도 `SSH_OK`** |
| 인터프리터 | 래퍼를 손으로 심음 | `args[0] = /opt/venv/bin/python` (코드가 채움) |
| HF 토큰 | `scp` 로 파일 심음 | `export HF_TOKEN` 1줄 · 스크립트 권한 **600** |
| 화면 | 로컬과 구별 불가 | `runner=ssh · remote=True` |
| 스크립트 뒷정리 | 없음 | **끝나고 스스로 지워짐** (토큰이 안 남는다) |

인스턴스에서 확인한 bootstrap 의 결과: `/root/.ssh` **700** · `authorized_keys` **600** ·
`.no_auto_tmux` 생성 · 배경 루프 동작. `.ready` 는 빌드 시각(full 의 조기탈출 경로)이라
교정이 **그 위**에 있어야 한다는 §12-1 의 배치가 맞았다.

#### ⚠ `push_to_hub` 는 끝에 한 번뿐이다 — 확정

`save_freq=1000` · 5000스텝이라 체크포인트가 **5개**(`001000`~`005000`) 생겼다. 그런데
Hub 커밋은:

```
08:11:06  initial commit
08:11:09  Upload policy weights, train config and readme
08:11:11  Upload DataProcessorPipeline
08:11:12  Upload DataProcessorPipeline
```

**전부 종료 시점 한 묶음이다.** 중간 체크포인트는 한 번도 안 올라갔다.

→ **인스턴스가 중간에 죽으면 Hub 에는 아무것도 없다.** §5 의 `scp` 보험은 선택이 아니라
**필수 경로**다. 그리고 §10 결정 4(interruptible 금지)가 옳았다 — 뺏기면 전부 잃는다.
회수할 것은 `last/pretrained_model` **198MB** 뿐이다(`training_state` 394MB 는 추론에 안 쓴다).

#### 숫자

| | |
|---|---|
| 기계 | RTX 3060 · New Jersey ↓2001Mbps · **$0.102/h** |
| 5000스텝 | 07:57:14 → 08:11:06 = **약 14분** · `updt_s 0.145` (Phase 1 과 동일) |
| loss | 9.33(step 100) → **1.93** 부근 |
| GPU 메모리 | **1600 MiB** (12GB 중) |
| 체크포인트 | 5개 + `last` = **2.9GB** |
| 회수 검증 | 파일 9개 · 커밋 4개 · `device=cuda steps=5000` · 정규화 `action.count=10403` (sim_data2 프레임 수와 일치) |
| 비용 | Phase 2 **$0.028** · 누적 **$0.082** (credit 25.0 → 24.917) |

### 12-9. W2 — 손으로 하던 것이 전부 코드로 갔다 (2026-09-16~17)

§11-0 의 표가 "자동 가드 0개" 였다. 그게 [빌리기]가 비활성이던 **이유**였지 디자인이
아니었다 — 끄는 코드가 없었다. 이제 상한이 셋이다:

| 층 | 무엇이 | 사라져도 되는 것 |
|---|---|---|
| 가장 안쪽 | 학습 스크립트의 `timeout` | 사람 · 게이트웨이 · 인터넷 |
| 바깥 | 예산/시간 틱(30초) | 사람 |
| 마감 | `finally` 의 파기 | — 어느 경로로 끝나든 지난다 |

#### 실물 검증 — 두 번이 서로 다른 절반을 증명했다

화면에서 버튼으로 두 번 돌렸다. **둘 다 필요했다.**

| | 호스트 | 결과 | 증명한 것 |
|---|---|---|---|
| 1차 | 느림(pull 정체) | `ssh_wait` 12분 상한 → **`destroyed`** | **실패 경로에서 기계가 정말 사라진다** |
| 2차 | Norway · diskBW 3286 | `ssh_wait` → `training` → `destroyed` | 학습·푸시·회수까지 전 구간 |

2차 타임라인: 조달 → **6분 20초** 뒤 학습 시작 → **2분 30초** 학습 → Hub 에 파일 9개
(00:37:10) → **21초 뒤 파기**(00:37:31). 푸시를 확인한 **뒤에** 껐다.

⚠ **행복 경로만 봤다면 아무것도 증명 못 했을 것이다.** 가짜 프로바이더로도 통과하는
경로다. 느린 호스트에 걸린 1차가 이 조각의 존재 이유(실패해도 끈다)를 실물로 확인해 줬다.

#### 이번에 사라진 손 작업

두 번의 수동 렌트(§12-1~12-8)에서 사람이 하던 것이 **하나도 없었다**:

오퍼 id 를 CLI 에 넣기 · `--cancel-unavail` 잊지 않기 · `~/.ssh/config` 쓰기 ·
인터프리터 래퍼 심기 · 토큰 `scp` · `-y` 붙여 파기 · 목록으로 확인.

#### 코드에 박은 실측 함정

| 함정 | 어디에 |
|---|---|
| `destroy` 는 `-y` 없으면 프롬프트에서 중단(종료코드 0) | `VastProvider.destroy` |
| `destroy -y --raw` 는 **빈 출력** — 응답으로 판정 불가 | 목록으로만 판정 |
| CLI 오류가 종료코드 0 + `{"error": true}` | `list_instances` 타입 검사 |
| `--cancel-unavail` 없으면 정지 인스턴스가 조용히 과금 | `create` |
| `running` ≠ 접속 가능 (권한 문제로 거부됨) | `wait_for_ssh` 가 **실제로 붙어 본다** |
| 상한이 알림만 하면 장식 | `run_until_done` 이 **중지시킨다** |
| 러너를 안 되돌리면 다음 로컬 학습이 죽은 호스트로 | `rent.start` 의 `finally` |

#### 비용

세션 전체 인스턴스 6개 · **$0.1003** (credit 25.0 → 24.8997). 전부 파기 확인.

#### ⚠ 아직 남은 것

- **동시 1개.** `MAX_CONCURRENT_JOBS=1` 이고 `TrainManager` 도 러너를 하나만 든다(W5).

### 12-10. 회수 보험 — 실물로 발동시켜 확인했다 (2026-09-17)

§12-4 가 확정한 사실(푸시는 학습이 끝날 때 **한 번뿐**) 때문에, 중간에 죽은 회차는
Hub 에 아무것도 없다. `scp` 보험은 그 경우의 **유일한 경로**다.

#### 확인 전에 읽다가 잡은 버그

보험을 발동시키려면 학습을 중간에 멈춰야 하는데, 그 경로가 **보험을 앞지르고 있었다**:

```
stop_now():  학습 정지 → 곧바로 파기          ← 여기
조달 흐름:   (틱 30초 뒤) … → retrieving → 회수 → 파기
```

중지가 먼저 기계를 없애므로 `scp` 는 **사라진 호스트**를 향한다. 푸시도 안 된 회차라면
결과가 통째로 날아간다 — 보험을 넣은 바로 그 경우에 보험이 안 듣는 구조였다.

고친 방식:

- 조달 흐름이 **살아 있으면 맡기고 기다린다**(`shield` + 90초). 흐름이 회수한 뒤 파기한다.
- 흐름이 없거나 시간 안에 못 끝내면 **직접 파기**한다 — 기다리다 못 끄는 것이 제일 나쁘다.
- **틱을 둘로 갈랐다**: 상한은 30초마다(요금이 시간당이라 오차 $0.0008), "끝났나" 는
  3초마다. 끝난 뒤의 시간은 빈 기계에 내는 돈이고, [중지]를 눌렀다면 회수가 그만큼 늦어진다.

⚠ **이 버그는 이 시나리오에서만 드러난다.** 푸시가 성공하는 회차에서는 보험이 아예
안 돌기 때문이다 — 행복 경로만 돌렸다면 영영 몰랐을 것이다.

#### 발동시킨 방법과 결과

Hub 에 **없는** 저장소 이름(`w2-rescue-probe-act`)으로 4000스텝을 걸고 `save_freq=50` 으로
체크포인트를 일찍 만든 뒤, step 250 에서 [중지].

```
01:09:13  학습 시작 (조달 5분)
01:10:15  step 250 · 체크포인트 5개
01:10:24  [중지]
          → Hub 확인 404 (repo 없음) → "받는 쪽으로 기운다"
          → scp 198MB
          → 파기
```

| 확인 | 결과 |
|---|---|
| 받은 것 | 파일 **7개 · 198MB** — 가중치 · 프로세서 둘 · `train_config.json` |
| 안 받은 것 | `training_state` **394MB** ☑ (추론에 안 쓰고 egress 과금 두 배) |
| 화면 | `/api/models` 에 `is_policy=true · policy_type=act` |
| 로드 | **51.6M** 파라미터 · `device=cuda` · 정규화 `action.count=10403`(sim_data2 프레임 수) |
| 인스턴스 | 0개 |
| 비용 | **$0.0156** |

⚠ **파일이 온 것과 쓸 수 있는 것은 다르다.** 스캐너는 HF 캐시 모양(`models--org--name/
snapshots/<hash>/config.json`)만 읽는다 — 아무 데나 두면 디스크에는 있는데 화면에 안 뜨고,
그건 이름만 회수한 것이다. 그래서 그 모양으로 떨어뜨리고, 확인도 **로드까지** 했다.

#### 세션 마감 (2026-09-16~17)

인스턴스 **7개 · $0.1086** (credit 25.0 → 24.8914). 전부 파기 확인.

### 12-11. 고아 스캐너 — 서버가 스스로 본다 (2026-09-17)

§6-3 이 요구한 마지막 칸이다. 인스턴스 탭은 진작 고아를 표시했는데, **고아가 생기는
상황이 곧 아무도 그 탭을 안 보는 상황**이다 — 게이트웨이가 죽었다 살아났거나 배포로
재시작한 직후. 그동안 요금은 계속 나간다.

`asyncio` 태스크 하나가 기동 30초 뒤부터 10분마다 훑는다
(`services/cloud/sweeper.py`, `main.py` 의 lifespan 에서 띄운다).

#### ⚠ 켜기 전에 나온 버그: `known` 이 **늘 비어 있었다**

`JobRecord.instance_id` 는 자리만 있고 **쓰는 코드가 없었다.** 그래서 고아 판정의
반대쪽(`known`)이 항상 공집합이었고, `piper-` 가 붙은 것은 **지금 학습 중인 기계까지
전부 고아**였다. 탭을 열 때만 보이는 동안에는 눈에 안 띄었지만, 10분마다 빨간 경보를
내는 스캐너에게는 치명적이다 — **늘 울리는 알람은 꺼진 알람이다.**

고친 자리는 `rent._remember()` 다. 인스턴스가 생기면 번호를 레지스트리에 남기고,
`DESTROYED`·`ORPHAN` 에서 **비운다**. 특히 `ORPHAN` 에서 비우는 것이 중요하다 —
파기를 확인 못 한 기계는 "우리가 관리 중" 이 아니라 **사람이 봐야 할 것**이라,
번호를 남겨두면 스캐너가 아는 척하고 조용해진다.

판정은 라우터와 스캐너가 **같은 함수**(`sweeper.known_instances()`)를 쓴다. 정의가
둘이면 탭과 배너가 다른 말을 하고, 그러면 사람은 둘 다 안 믿는다.

#### 실기 확인 (인스턴스 51263527, RTX 3060 $0.058/h, 2분)

| 본 것 | 결과 |
|---|---|
| 우리 라벨 + 레지스트리 모름 | ☑ 고아로 잡힘 — 로그에 요금과 `vastai destroy instance 51263527` 까지 |
| 스캔 뒤에도 살아 있나 | ☑ **파기 안 함** (§10 결정 5) — 목록에 그대로 |
| 레지스트리에 등록하면 | ☑ 조용해짐 (거짓 경보 없음) |
| 파기 뒤 | ☑ 목록 비고 "정리됐습니다" |

#### 시끄러워지지 않게 한 것

- **로그**: 고아가 있는 동안 스캔마다 남긴다(재시작 뒤 로그를 보는 사람이 "지금도
  나가는 중" 을 알아야 한다). 반면 **같은 오류**는 처음만 경고하고 그다음은 debug —
  API 키가 없는 설치에서 10분마다 같은 문장으로 로그가 덮이면 안 된다.
- **화면**: 전이에서만 민다(`device_alert` 와 같은 규칙). 알림 id 를 인스턴스 번호로
  고정해서, 고아 하나가 알림함에 스무 줄로 쌓이지 않는다.
- `vastai` 가 없으면 조용히 건너뛴다 — 대부분의 설치가 그렇고, 그건 고장이 아니다.

#### 여기에 [파기] 버튼을 안 단 이유

자동 파기를 안 하는 이유와 같다(§10 결정 5). 라벨이 `piper-` 라고 해서 이 게이트웨이의
것이라는 보장이 없다 — 다른 기계가 돌리는 6시간짜리 학습일 수 있고, 토스트에서 한 번
잘못 누르면 그게 사라진다. 끄는 것은 인스턴스 탭에서 목록을 보고 한다.


### 12-12. 회수가 자동이 됐다 — 두 경로, 서로 반대 순서 (2026-09-17)

§12-10 이 `scp` 보험을 넣었지만 절반만 끝난 상태였다. **푸시가 성공한 회차는 아무것도
안 받았다** — 사람이 저장소 페이지에서 [다운로드]를 눌러야 로컬에 왔다. 학습을 걸어
두고 자리를 뜬 사람 입장에서는 **다 됐는데 아직 못 쓰는** 상태이고, 그 클릭을 잊으면
다음 날 추론을 돌리려다 그제서야 안다.

#### ⚠ 두 경로의 순서가 서로 반대다

```
학습 끝
  ├ Hub 에 없다 → `scp` ──────→ 기계가 살아 있어야 한다 → **파기 전**
  └ Hub 에 있다 → Hub 다운로드 → 기계를 안 탄다        → **파기 후**
```

Hub 다운로드를 파기 전에 하면 200MB 를 내려받는 동안 빌린 GPU 요금을 그대로 낸다
(4090 $0.4/h 에서 2분이면 $0.013). 그보다 나쁜 것은 **파기가 그만큼 늦어지는 것**이다 —
이 기능 전체에서 가장 중요한 불변식이 "반드시 파기한다" 인데 그걸 회선 속도에 묶을
이유가 없다. 그래서 `retrieve.py` 가 판정과 순서를 한 곳에서 들고, `rent._go()` 는
**러너를 되돌린 뒤** `after_destroy()` 를 부른다.

테스트가 두 순서를 양쪽에서 못 박는다 — 하나는 "파기보다 먼저", 다른 하나는
"파기보다 나중". 둘을 바꿔 달면 하나는 빈 호스트를 향하고 하나는 돈을 더 낸다.

#### 받는 자리를 `cache_dir` 로 못 박았다

`snapshot_download` 의 기본 자리는 `HF_HUB_CACHE` 인데 스캐너가 보는 곳은
`settings.models_dir` 이다. 이 기계에서는 우연히 같지만 `PIPER_MODELS_DIR` 을 옮기면
갈라져 — **파일은 받았는데 화면에 안 뜨는** 상태가 된다.

#### ⚠ 판정은 `model.safetensors` 로 한다

디렉토리만 보면 받다 만 자리·`config.json` 만 있는 자리·`.incomplete` 만 남은 자리가
전부 "있음" 으로 읽힌다. 그러면 다시 안 받고, 사람은 못 쓰는 폴더를 본다.

#### ⚠ 같은 저장소로 두 번째를 돌렸을 때

`pushed()` 가 파일 목록만 보면 함정이 하나 열린다. 같은 `policy_repo_id` 로 두 번째를
돌렸는데 **이번 회차가 중간에 죽으면**, Hub 에 있는 것은 지난번 가중치다. 파일만 보면
"올라갔다" 가 되어 `scp` 보험을 건너뛰고 — 이번 결과를 영영 잃은 채(푸시는 종료 시점
한 번뿐이다) **지난번 것을 받아 놓고 성공이라 말한다.** 이 경로에서 제일 나쁜 거짓말이다.

그래서 `last_modified` 를 **이번 학습을 건 시각**과 비교한다. 오래됐으면 이번 것이
아니므로 `scp` 로 간다. 시각을 모르면 역시 `scp` 쪽으로 기운다 — 모를 때 받는 쪽으로
기우는 것은 이 파일의 일관된 규칙이다(전송비는 몇 센트, 잃은 학습은 몇 시간).

⚠ 기준 시각은 **벽시계**여야 한다. `Budget.started_at` 은 `time.monotonic()` 이라
Hub 의 UTC 시각과 비교할 수 없다 — 그대로 쓰면 항상 "오래된 것" 으로 읽힌다.

#### 화면: 빌려 놓고 아무것도 못 보던 것

`GET /api/cloud/rent` 를 보는 화면이 **하나도 없었다.** 학습 로그는 학습 페이지에,
기계는 인스턴스 탭에 있었지만 그 사이 단계(만드는 중 · SSH 대기 · 회수 · 파기)는
어디에도 안 나왔다 — 6분씩 걸리는 구간이라 사람은 "먹통인가" 를 먼저 의심한다.
`CloudRentProgress` 를 탭 **바깥**에 뒀다: 고르던 중에 시작해 놓고 인스턴스 탭으로
넘어가도 같은 자리에 있다.

그리고 **파기됨 ≠ 끝남**이다. 기계를 끈 뒤에도 `pulling` 구간이 남으므로 회수 상태를
같이 준다.

#### ⚠ 덤으로 잡은 것: 빈칸으로 끝나던 오류

이번 확인 중에 `create instance` 가 **종료코드 0 · 빈 stdout** 으로 끝났다. 그때 온
문장이 `vastai 응답을 읽지 못했습니다: ` — **콜론 뒤가 빈칸**이었다. 왜 안 빌려졌는지
알 길이 없으면 그냥 다시 누르게 되고, 그게 돈 드는 재시도다. 이제 stdout 이 비면
stderr 를 쓰고, 그것도 비면 "출력이 비었습니다" 라고 말한다.


### 12-13. 접속된 것과 학습할 수 있는 것은 다르다 (2026-09-17)

#### 먼저, 내가 틀렸던 것

확인 중에 slim 템플릿으로 돌렸다가 3초 만에 죽었다 — `No module named 'lerobot'`.
이미지를 열어 보니 slim(1.32GB)에 정말 lerobot 이 없었고, 거기서 **"slim 으로는 학습을
못 한다"** 고 결론짓고 `POST /rent` 가 거절하게 막았다. **틀린 결론이었다.**

`deploy/train/Dockerfile` 머리말이 이미 답을 적고 있었다:

> full  스택(torch·lerobot) 포함. pull 이 크다(수 GB). 부팅 즉시 학습 가능.
> slim  스택 없음. pull 은 작고, 첫 부팅 때 bootstrap.sh 가 PyPI/PyTorch 에서 받아 깐다.

슬림은 **같은 `install-stack.sh` 로 같은 버전**을 깐다 — 다른 것은 "언제" 뿐이다.
사실("이미지에 lerobot 이 없다")은 맞았는데 결론("그러므로 학습 불가")이 틀렸고, 하필
그 결론이 **이미지를 줄이는 유일한 길을 막고** 있었다. 되돌렸다.

#### 진짜 버그: 아무도 `.ready` 를 안 봤다

`bootstrap.sh` 머리말은 이렇게 적고 있었다:

> 끝나면 /opt/piper/.ready 에 한 줄을 남긴다. **SSHRunner(W1)는 이 파일을 보고 학습을
> 시작한다** — 없으면 "아직 설치 중" 이지 "고장" 이 아니다.

그런데 백엔드 어디에도 `.ready` 를 읽는 코드가 없었다. 설계는 있고 구현이 없는 자리였고,
그래서 `wait_for_ssh` 가 통과하자마자 학습이 걸렸다 — slim 이 아직 torch 를 받는 중에.
빌린 값(기계·이미지·접속)을 다 치르고 3초 만에 죽는다. **$0.0145.**

이제 `procure.wait_for_stack()` 이 SSH 다음, 학습 앞에 선다.

| | |
|---|---|
| 판정 | **종료 코드** — `.ready` 의 문구를 뒤지면 `bootstrap.sh` 한 글자에 묶인다 |
| 왕복 | 한 번에 "됐나" 와 "왜 안 됐나"(`bootstrap.log` 끝 5줄)를 같이 — 다시 붙는 동안에도 요금이 나간다 |
| 상한 | `STACK_WAIT_S=900` — 이 구간의 기다림은 낭비가 아니다. slim 은 여기서 torch 를 받는다 |
| 실패 | 사유와 함께 `/opt/piper/bootstrap.log` 경로를 그대로 준다 |
| 일시 실패 | ssh 가 한 번 튕겨도 버리지 않는다 — 빌린 기계다 |

⚠ 이 검사를 `runners.ssh.available()` 에 **넣지 않았다.** 그건 사내 박스
(`PIPER_TRAIN_SSH_HOST`)에도 쓰이는데 거기엔 `/opt/piper` 가 없다 — 넣으면 임대와
상관없는 원격 학습이 통째로 막힌다. 테스트가 그 자리를 지킨다.

#### 실기 한 바퀴 — slim 으로 **첫 시도에 완주** (2026-09-17)

게이트를 넣고 slim 으로 돌렸더니 처음으로 끝까지 갔다. 그전 세 번은 전부 이미지
받다가 상한에 걸렸다(그중 둘은 느린 호스트, 하나는 같은 호스트 + full 14GB).

| 구간 | 걸린 시간 | 비고 |
|---|---|---|
| 만들기 | 2초 | |
| pull + 부팅 + **스택 설치** | **4분 23초** | SSH 는 62초에 열렸다 — 나머지 3분 20초가 게이트가 잡아 준 구간이다 |
| 학습 (250스텝) | 1분 25초 | code=0 |
| 회수 판정 → 파기 | **4초** | Hub 에 있으므로 `scp` 안 씀 |
| **Hub 다운로드** | **9초** | ⚠ **기계가 없어진 뒤다** — 이 9초에는 요금이 안 나간다 |
| **합계** | **6분 24초 · $0.0621** | |

결과: 로컬에 9개 파일 206.7MB, 모델 목록에 `wego-hansu/w3-slim-autopull-act` 가
`act · state 7 · action 7 · cam0/cam1` 로 떴다. **아무도 아무것도 안 눌렀다.**

⚠ 62초에 SSH 가 열렸다는 점을 보라. 게이트가 없었다면 정확히 거기서 학습을 걸었을
것이고, 스택은 3분 20초 뒤에야 준비됐다 — 그 사이에 건 학습이 $0.0145 를 태운
그 실패다.

⚠ 그리고 `destroyed/waiting → destroyed/done` 이 화면에 보인다. **파기됨이 끝남이
아니라는 것**을 그 9초가 증명한다(§12-12 의 회수 칸이 그래서 있다).

#### 이미지 크기 — 왜 이게 그 문제의 답인가

| | pull | 같은 호스트(4090 · 5.9Gbps)에서 |
|---|---|---|
| full | 14GB | 8분에도 `Retrying in 2 seconds` — 상한에 걸려 포기 |
| slim | 1.32GB | **16초에 pull 완료 · 62초에 SSH** |

같은 기계다. 즉 `SSH_WAIT_S=480` 이 빠듯했던 게 아니라 **14GB 가 문제였다.** 이미지를
새로 구울 필요가 없다 — 작은 이미지는 이미 있었고, 그걸 쓸 수 없게 만든 것이 빠진
`.ready` 게이트였다. 대신 값은 뒤로 옮겨 간다: pull 이 짧아지는 대신 부팅 때 PyPI 에서
스택을 받는다. 어느 쪽이 빠른지는 호스트가 정한다(Dockerfile 머리말이 처음부터 그렇게
적고 있다).

#### 그래서 화면 기본값을 slim 으로 바꿨다

예전 기본값은 full 이었고 주석은 "네트워크가 빠르면 4.7GB pull 이 부팅 설치보다 낫다"
였다. 정작 **네트워크가 빠른 호스트에서 14GB 가 안 끝났다** — 회선이 아니라 크기가
문제였다. full 의 가장 좋았던 기록(6분 20초)과 비교해도 slim(4분 23초)이 빠르다.

⚠ 이 기본값은 **게이트가 생긴 뒤에만 안전하다.** 게이트 없이 기본값만 바꾸면 처음 쓰는
사람이 그대로 눌러서 3초 만에 죽는다 — 테스트가 둘을 같이 확인한다.

⚠ 그리고 템플릿 **설명을 화면이 버리고 있었다.** 백엔드가 "스택 포함, 부팅 즉시 학습" /
"부팅 때 bootstrap.sh 가 스택 설치" 를 주는데 목록에는 `full`·`slim` 네 글자만 떴다 —
그 차이가 임대 시간과 요금을 가르는데 고르는 사람은 알 길이 없었다.

#### torch 를 두 번 받던 것을 한 번으로 (2026-09-17)

`install-stack.sh` 는 lerobot 을 먼저 깔고(그때 PyPI 기본 CUDA 빌드 torch 가 딸려 온다)
그걸 지운 뒤 원하는 CUDA 빌드로 다시 깔았다. 설치본 기준 **torch 1.7GB + nvidia 런타임
4.0GB** 인데, slim 인스턴스는 그 값을 부팅마다 두 번 치렀다.

절차를 뒤집었다:

| | 예전 | 지금 |
|---|---|---|
| 1 | `pip install lerobot[smolvla]` (torch 딸려 옴) | **torch 셋을 CUDA 인덱스에서** — 한 번만 |
| 2 | `pip uninstall torch…` + nvidia-* 전부 걷기 | `pip install --no-deps lerobot` (휠만) |
| 3 | CUDA 인덱스에서 torch 셋 **다시** | 의존성을 **메타데이터에서 뽑아** torch 셋만 빼고 |

⚠ **`--no-deps` 가 핵심이다.** lerobot 0.5.0 의 핀은 `torch<2.11.0` 이라, 의존성을 그대로
두면 pip 이 방금 깐 2.11.0 을 끌어내린다. 우리가 2.11.0 을 쓰는 이유는 추론 기계와 같아야
체크포인트가 거기서 열리기 때문이다 — 그 핀은 **의도적으로** 넘긴다.

⚠ **의존성 목록을 손으로 적지 않는다.** lerobot 0.5.0 의 요구사항은 139줄이고, 그중
기본 + `smolvla` extra 를 추려 torch 셋을 뺀 것이 25줄이다. 손으로 적으면 버전을 올릴
때마다 반드시 어긋난다 — `importlib.metadata.requires()` 로 뽑는다.

⚠ **제약 파일로 downgrade 를 막는다.** 이행 의존성 139개 중 누군가 torch 를 끌면 pip 은
조용히 내린다. 그러면 받은 것을 또 버리고, 더 나쁘게는 추론 기계와 버전이 갈린다.
제약이 있으면 조용히 내려가는 대신 **거기서 실패한다.**

#### 실측 — 같은 이미지·같은 회선에서 두 절차를 다 돌렸다

| | 옛 절차 | 새 절차 |
|---|---|---|
| 받은 총량 | **8155 MB** | **4240 MB** (−48%) |
| 걸린 시간(로컬) | **7분 59초** | **3분 53초** (−51%) |
| torch 휠 | 두 번 (PyPI 기본 cu128 → 버림 → cu126) | **한 번** (830.5MB, cu126) |
| 결과 | lerobot 0.5.0 · torch 2.11.0+cu126 | **같음** |

옛 절차가 받았다 버리는 것이 눈에 보인다 — `nvidia_cublas_cu12-12.8.4.1`(594MB) 처럼
**cu128** 짜리를 받아 놓고 곧바로 지운 뒤 cu126 으로 다시 받는다.

임대 인스턴스에서 이 구간은 실측 201초였다(§12-13). 절반이면 slim 의 준비 시간이
4분 23초에서 3분 안쪽으로 내려간다 — 임대료로 직접 환산되는 시간이다.

#### ⚠ 확인을 "쓸 것" 까지 넓혔다

네 개를 `import` 하는 것과 `lerobot-train` 이 도는 것은 다르다. 의존성 하나가 빠지면
`import lerobot` 은 멀쩡하고 **기계를 빌린 뒤에** 죽는다 — 오늘 실제로 그렇게 죽었다.
이제 `import lerobot.scripts.lerobot_train` 까지 열어 보고, 받은 torch 가 원하는 버전인지
**단언**한다.

#### ⚠ 무서워 보이는 pip 줄에 설명을 붙였다

```
ERROR: ... lerobot 0.5.0 requires torch<2.11.0 ... but you have torch 2.11.0+cu126
```

의도한 것이다(추론 기계와 맞추려고 넘긴 핀). pip 은 0 으로 끝나고 진짜 판정은 그 아래
확인이다. **설명을 그 줄 바로 옆에 둔다** — 파일 머리말에만 적으면 `bootstrap.log` 만
보는 사람은 못 본다.

⚠ 고치다 **NPP 블록을 한 번 지웠다.** `torchcodec` 의 CUDA 빌드가 `libnppicc` 를
링크하는데 torch 의존성에는 NPP 가 없어서, 없으면 데이터셋 영상을 한 프레임도 못 읽는다
(첫 빌드가 실제로 거기서 죽었다). 되살렸고, 테스트가 그 자리를 지킨다.


### 12-14. 게이트웨이가 죽으면 그 기계는 아무도 관리하지 않는다 (2026-09-17)

§12-11 에서 고아 스캐너를 넣었는데, **그 스캐너가 있어야 할 바로 그 경우**가 막혀
있었다. W3 의 "파괴적 검증" 항목을 실제로 따져 보다가 나왔다.

크래시를 재현해 본 결과:

```
임대 태스크 살아있나: False   ← 아무도 관리 안 함
스캐너가 아는 인스턴스: {99999} ← 그런데 "관리 중" 으로 봄
고아로 잡히나: []             ← 경고 안 뜸 → 조용히 과금
```

원인은 §12-11 에서 내가 정한 `known` 의 뜻이다. "레지스트리에 `instance_id` 가 적힌
것" 으로 뒀는데 — 그 기록은 **프로세스가 죽어도 Redis 에 남는다.** 반면 그것을 관리하던
`rent._task` 는 asyncio 태스크라 프로세스와 함께 **반드시** 죽는다. 학습은 tmux 에 남아
재부착되지만(`restore_running_process`) 임대에는 그런 경로가 **없다.**

즉 불변식이 하나 있는데 코드가 그걸 몰랐다:

> **게이트웨이가 재기동했다면, 그 순간 관리 중인 임대는 하나도 없다.**

그래서 기동 때 `sweeper.release_claims()` 가 임대 주장을 전부 비운다. 그러면 살아 있는
`piper-*` 기계는 전부 고아로 보이고 — 사실 그게 맞다 — 스캐너가 본래 할 일을 한다.

⚠ **비우는 것과 끄는 것은 다르다.** `release_claims()` 는 "아는 척을 그만두는 것" 일
뿐이고, 파기는 여전히 사람이 인스턴스 탭에서 한다(§10 결정 5). 주장을 비웠다고 그것이
남의 기계일 가능성이 사라지지는 않는다.

⚠ 순서도 테스트가 고정한다 — 비우기가 **스캐너보다 먼저**여야 한다. 스캔이 먼저 돌면
그 회차는 여전히 "관리 중" 으로 보고 넘어간다.

#### 파괴적 검증 — 실제로 `kill -9` 했다

slim 을 빌려 학습이 도는 것을 확인하고(스택 설치 201초) 관리 프로세스를 `kill -9` 했다.

| 확인한 것 | 결과 |
|---|---|
| 기계는 살아남나 | ☑ `51272008 running $0.5859/h` — **과금 계속** |
| 임대 태스크 | ☑ 사라짐 (`busy=False`, 메모리 job `None`) |
| 재기동한 게이트웨이가 찾나 | ☑ 고아로 잡힘 |
| 자동으로 끄나 | ☑ **안 끈다** (§10 결정 5) |
| 사람이 끄면 | ☑ 확인까지 되고 목록이 빈다 |

#### ⚠ 그런데 그 과정에서 더 이른 곳의 버그가 나왔다

크래시 직후 레코드를 보니 `instance_id` 가 **이미 빈 문자열**이었다. `release_claims()`
가 비운 것이 아니라, 그 전에 사라져 있었다:

```python
async def start(...):          # TrainManager
    self.registry.delete(self.job_id)    # ← 옛 로그를 치우려고 레코드를 통째로 지운다
```

임대 번호가 **같은 레코드**에 실려 있어서 학습이 시작되는 순간 함께 사라진다.

⚠ 다행히 화면이 당장 거짓말을 하지는 않았다 — `known_instances()` 가 메모리의
`rent.current()` 로 보완하기 때문이다(같은 프로세스라면). 그래서 "학습 중인 기계를
고아라고 부르는" 사고는 안 났다. 다만 레코드는 **"이 학습이 어느 기계에서 돌았나"** 를
남기는 유일한 자리인데 그게 비어 있었다 — 끝난 뒤엔 아무도 답할 수 없다. 학습 시작
뒤에 다시 심는다.

⚠ 그리고 이것 때문에 `release_claims()` 가 덮는 창이 생각보다 **좁다**: 크래시가
학습 시작 **뒤**에 나면 주장은 어차피 없어서 스캐너가 원래 찾았고, 비우기가 필요한
것은 `creating`·`ssh_wait` 구간에서 죽은 경우다. 좁아도 그 구간은 실재하고(pull 에
6분이 걸린다), 그때 죽으면 아무도 안 찾는다.


### 12-15. bf16 — **죽지 않아서** 위험한 경우 (2026-09-17)

학습 기본 AMP 가 bf16 인데(`routers/training.py`·`RentRequest.amp`) 하드웨어 bf16 은
암페어(cc 8.0)부터다. torch 소스에서 확인한 판정:

```python
def is_bf16_supported(including_emulation: bool = True):   # ← 기본이 True 다
    if cuda_version is not None and get_device_properties(device).major >= 8:
        return True
    if not including_emulation:
        return False
    return _check_bf16_tensor_supported(device)            # ← 에뮬레이션으로 True
```

즉 cc 8.0 미만에서 **학습은 멀쩡히 시작하고 조용히 느려진다.** 죽었으면 사람이 바로
알았을 텐데, 그냥 몇 시간을 더 낸다. 그래서 `too_old`·`too_new` 와 달리 이건 "못 돈다"
가 아니라 **"돌긴 도는데 손해"** 라서 경고가 유일한 방어다.

⚠ 그리고 **하필 그게 제일 싼 기계들이다.** 실측(2026-09-17):

| GPU | cc | 시간당 | 판정 |
|---|---|---|---|
| Tesla V100 | 700 | **$0.125** | ok — 이미지로는 멀쩡히 돈다 |
| RTX 2080 Ti | 750 | **$0.095** | ok |
| RTX 3060 | 860 | $0.058 | 경고 없음 (암페어) |
| RTX 4090 | 890 | $0.57 | 경고 없음 |

싼 것을 고르러 온 사람이 정확히 저기로 간다. 이제 오퍼 목록의 그 행에 붙는다:

> bf16 하드웨어가 없습니다 (cc 8.0 미만) — 죽지 않고 에뮬레이션으로 느려집니다.
> 이 세대는 fp16 이 맞습니다

⚠ "느립니다" 로 끝내지 않는다. 튜링은 **fp16 텐서코어는 있고 bf16 만 없다** — 할 수
있는 일을 같이 적어야 경고가 쓸모 있다.

⚠ **모르는 세대에는 안 붙인다.** 실측의 `cc=140`·`243` 은 실물이 파스칼이라 bf16 이
없지만, 이 파일의 규칙이 "알 수 없는 값을 단정하지 않는다"(`gpu_support`)이고 한쪽만
예외로 두면 두 판정이 서로 다른 말을 하게 된다. 세대를 모른다는 사실 자체는
`support='unknown'` 으로 이미 화면에 간다.

#### ⚠ 곁가지: `too_old` 는 **cu126 에서만** 뜰 수 없다

`KNOWN_CC` 의 최솟값이 500 이고 cu126 의 하한도 500 이라, cu126 기준으로는
`compute_cap < 하한` 인 **알려진** 값이 없다 — 그래서 `too_old` 가지가 안 열린다.
(§12-16 에서 판정이 템플릿을 따라가게 된 뒤로는 cu128 에서 8종이 여기로 떨어진다.)


### 12-16. 판정이 고른 템플릿을 따라간다 (2026-09-17)

GPU 호환 판정이 **cu126 으로 못 박혀** 있었다. `build-train.sh --cuda cu128` 이 이미
있으니 명령 한 번이면 cu128 템플릿이 생기는데, 그 순간 표가 **조용히 거짓말**을 하게
돼 있었다 — Blackwell 을 "커널 없음" 이라 하고 맥스웰·파스칼을 "ok" 라 한다.

#### 실측 — 두 이미지에 직접 물어봤다

`get_arch_list()` 는 GPU 가 붙어 있어야 답한다(`if not is_available(): return []`).
5090 이 달린 컨테이너에서 띄워 찍었다:

```
cu126 (이미지가 거부하며 스스로 출력): sm_50 sm_60 sm_70 sm_75 sm_80 sm_86 sm_90
cu128 (torch 2.11.0+cu128):           sm_75 sm_80 sm_86 sm_90 sm_100 sm_120
```

⚠ **cu128 에 `sm_70` 이 없다.** 코드 주석이 "cu128 은 sm_70~sm_120" 이라 적고 있었는데
그건 이 머신의 torch **2.10** 기준이었다 — 2.11 의 cu128 은 튜링(7.5)부터라 **V100 까지
잃는다.** 맥스웰·파스칼만 잃는 줄 알았던 것이 한 종 더 있었다.

#### 살아 있는 게이트웨이에 같은 질문을 두 번

| | cu126 | cu128 |
|---|---|---|
| ok | 52종 | **57종** |
| too_new (커널 없음) | 13종 | 0 |
| too_old | 0 | **8종** |
| Tesla V100 (cc 700) | ok | **too_old** |
| RTX 5090 (cc 1200) | **too_new** | ok |

바꾸면 Blackwell 13종을 얻고 구형 8종을 잃는다. **어느 쪽도 전부를 덮지 못한다.**

#### 붙인 것

- `ARCH` 표 — 빌드별 커널 범위. **이미지가 스스로 찍은 값만** 넣는다. 모르는 빌드는
  넣지 않고 `unknown` 으로 답한다 — 때려 맞힌 범위는 조용히 틀리고 대가는 "빌렸는데
  안 도는 기계" 다.
- 경고 문구도 빌드를 따라간다. 고정 판정 시절엔 문구도 고정이라 **cu128 을 고른
  사람에게 "cu128 로 구우세요"** 라고 말했다.
- **캐시 키에 빌드가 들어간다.** 판정이 빌드를 타는데 키가 그대로면 템플릿을 바꿔도
  cu126 판정이 그대로 나온다 — 화면은 바뀐 줄 아는데 내용은 안 바뀐다.
- bf16 판정만 빌드를 **안** 탄다. 하드웨어 사실이라 cu128 로 다시 구워도 튜링에 bf16
  텐서코어가 생기지는 않는다.

#### ⚠ 이걸로 Blackwell 을 빌릴 수 있게 된 것은 아니다

cu128 에서 5090 이 `ok` 로 바뀌어도 **오퍼 목록에는 안 나온다.** 질의의 `cuda_vers`
항이 Blackwell 호스트를 이미 걸러내기 때문이다(§9-2 실측 — 카탈로그가 질의 둘을 쓰는
이유가 그거다). 지금 카탈로그는 그 경우 "지금 필터 조건을 만족하는 기계가 없습니다"
라고 말하는데, 진짜 이유는 필터가 아니라 **그 항**이다. 실제로 빌리려면 질의 쪽을
따로 손봐야 한다 — 드라이버 하한을 무엇으로 볼지 다시 정하는 일이라 여기서 안 했다.


### 12-17. [빌리기]가 숨기고 있던 값들 (2026-09-17)

예산 상한 옆에는 이렇게 적혀 있었다:

> ⚠ 상한을 **항상 보낸다.** 서버가 기본값을 갖고 있어도, 화면이 보여 준 숫자와
> 실제로 걸리는 숫자가 다르면 그 화면은 거짓말이다.

그런데 **학습 설정에는 그 규칙을 안 지키고 있었다.** RENT 탭이 보내는 것은
`offer_id · template_hash · disk_gb · budget · max_hours · dataset · repo` 뿐이고,
나머지는 `RentRequest` 의 기본값이 조용히 들어갔다:

```
policy_type=act · batch_size=8 · steps=5000 · save_freq=1000 · num_workers=4 · amp=bf16
```

즉 [빌리기]를 누르면 **5000스텝·배치 8로 도는데 그 숫자가 화면 어디에도 없었다.**

#### 고친 방식 — 숫자를 베껴 적지 않는다

정책·스텝·배치·저장주기·AMP 를 **보여 주고 고칠 수 있게** 하고, 화면이 보인 값을 그대로
보낸다. 학습 페이지의 폼을 통째로 옮기지는 않았다 — 폼이 둘이 되면 반드시 어긋난다.

그리고 확인 창이 **실제로 도는 명령**을 그대로 띄운다:

```
/opt/venv/bin/python -m lerobot.scripts.lerobot_train --dataset.repo_id=… \
  --policy.type=act --policy.repo_id=… --batch_size=8 --steps=250 \
  --log_freq=100 --save_freq=250 --num_workers=4 --policy.device=cuda
```

⚠ 이 문자열은 **서버가 `[빌리기]`와 같은 함수(`_rent_train_args`)로** 만든다. 화면이
숫자를 베껴 적으면 언젠가 어긋나고, 인자 빌더를 두 벌로 두면 **반드시** 갈린다 —
실제로 갈릴 뻔한 자리가 있었다: `TrainPreviewRequest` 와 `RentRequest` 의 기본값이
다르다(steps 100000 대 5000 · save_freq 20000 대 1000). 기존 `/training/preview` 를
가져다 썼으면 그 차이가 그대로 화면에 나왔을 것이다.

⚠ **환경변수는 안 돌려준다.** `_train_env()` 에는 HF 토큰이 들어 있다 — 미리보기 한
번에 키가 화면으로 새면 학습 설정을 감춘 것보다 큰 사고다. 응답은 `args`·`command`
둘뿐이고 테스트가 그것을 고정한다.

#### 곁다리로 닫힌 것: bf16 경고가 반쪽이었다

§12-15 가 "이 세대는 fp16 이 맞습니다" 라고 말해 놓고 **바꿀 방법을 안 줬다.** 이제
AMP 가 화면에 있으니 경고를 읽고 그 자리에서 고칠 수 있다.

#### ⚠ 이것은 W4 가 아니다

학습 페이지는 여전히 클라우드를 모른다. 데이터셋 드롭다운·명령 미리보기·체크포인트
목록·로스 곡선·job 이력은 전부 학습 페이지에 있고, RENT 탭에는 없다. 지금 한 것은
**"모르고 5000스텝 도는" 것을 막은 것**까지다. 실행 위치를 학습 페이지로 모으는 일은
그대로 남아 있다(§8 W4).


### 12-18. W4 의 전제 — 임대가 학습 설정을 흘리고 있었다 (2026-09-17)

학습 페이지를 임대와 잇기 전에 세어 봤다. 학습 페이지는 **22개 필드**를 보내는데
`RentRequest` 는 **9개**만 받고 있었다. Pydantic 은 나머지를 조용히 버린다.

버려진 것 중에 **`pretrained_path`** 가 있었다. 즉 학습 페이지에서 파인튜닝을 걸어
임대로 보냈다면 **말없이 처음부터** 학습이 되고, 그걸 아는 시점은 몇 시간 뒤 결과를
열어 봤을 때다. §12-17 에서 고친 "화면이 안 보여 주는 값" 과 같은 부류인데, 이쪽은
13배 크다.

#### 구조로 막았다

- `RentRequest` 가 `TrainStartRequest` 를 **상속한다.** 따로 적어 두면 한쪽에만 필드가
  붙고 다른 쪽에서 사라진다 — 상속이면 애초에 못 갈린다.
- 인자 조립도 `/training/start` 와 **같은 함수**(`train_cli_params`)를 쓴다.
- 다만 **스텝 기본값은 낮게 유지한다**(5000). 학습 페이지 기본값은 10만이고, 그걸
  물려받으면 값을 안 보낸 호출 하나가 **시간당 과금되는 기계에서** 10만 스텝을 돈다.

#### ⚠ 그런데 원격에서 말이 안 되는 설정이 있다

`/training/start` 는 `pretrained_path` 로 `resolve_rename_map()` 과
`apply_dim_overrides()` 를 부른다 — **이 기계의** 체크포인트를 읽고 고치는 일이다.
빌린 기계에는 그 경로가 없다.

그래서 `_rent_train_args` 는 파일시스템을 안 만지고, `/rent` 는 **빌리기 전에** 거절한다:

| `pretrained_path` | 결과 |
|---|---|
| `lerobot/act_aloha` (Hub 이름) | ☑ `--policy.path=` 로 그대로 실린다 |
| `/home/…/checkpoints/last` (이 기계 경로) | ☒ 400 — 빌리기 전에 거절 |

⚠ Hub 이름을 로컬 경로로 오해해 막으면 **쓸 수 있는 것을 못 쓰게 된다.** 그래서 판정은
`/`·`./`·`~` 로 시작하거나 실제로 이 기계에 존재하는 경우로 좁혔다.

⚠ 그냥 뒀으면 어떻게 됐나: 몇 분 뒤 `No such file or directory` 로 죽거나, 더 나쁘게는
lerobot 이 그 문자열을 Hub 저장소 이름으로 읽어 **엉뚱한 가중치를 받아** 학습한다.


### 12-19. W4 — 학습 페이지에서 빌려 돌린다 (2026-09-17)

여태 두 화면이 갈라져 있었다. 학습 페이지에는 데이터셋 드롭다운·정책·스텝·미리보기·
체크포인트·로스 곡선·job 이력이 있고, RENT 탭에는 기계와 예산이 있었다. 빌려서 돌리려면
**RENT 탭의 빈 입력 둘에 저장소 이름을 다시 타이핑**해야 했다.

#### 붙인 것: 실행 위치

학습 페이지의 [시작] 버튼 **바로 위**에 `[이 기계] [GPU 빌려서]` 를 뒀다.

⚠ 위쪽 폼 어딘가에 두면 "어디서 도는지" 를 모른 채 누르게 된다 — 임대는 그 한 번이 곧
돈이다. 테스트가 둘 사이에 다른 버튼이 끼지 못하게 막는다.

칸은 **둘이다 — [로컬] [클라우드].**

⚠ 그런데 "로컬" 이 늘 로컬은 아니다. 러너는 서버 설정(`PIPER_TRAIN_SSH_HOST`)이
정하므로, 그게 채워진 기계에서는 [로컬]이 사실 **사내 SSH 박스**로 간다. 칸을 셋으로
늘리지는 않았다 — 회차마다 러너를 고르는 것은 서버가 아직 못 하는 일이고, 못 하는 것을
고를 수 있는 것처럼 보이게 하는 편이 더 나쁘다. 대신 그 설정이 있는 기계에서만 한 줄이
뜬다: "이 게이트웨이는 사내 서버(SSH)로 보냅니다 — 이 기계의 GPU 가 아닙니다".

#### ⚠ 학습 설정은 **한 벌**이다

`[GPU 빌려서]` 를 골라도 데이터셋·정책·스텝·배치는 **학습 페이지의 폼 그대로** 간다:

```js
api.post('/cloud/rent', { ...trainParams(), ...rentPick })
```

임대용 학습 폼을 따로 만들지 않았다. 그게 정확히 §12-17 에서 고친 병이다 — 폼이 둘이면
반드시 어긋나고, 어긋난 쪽이 조용히 이긴다.

실기로 확인한 명령(전에는 9개 필드만 살아남았다):

```
--policy.path=lerobot/act_aloha --batch_size=16 --steps=3000 --save_freq=500
--eval_freq=0 --seed=7 --optimizer.type=adamw --optimizer.lr=0.0001
--wandb.enable=false --use_policy_training_preset=true
```

#### ⚠ 직접 고친 CLI 는 빌려서 못 돌린다 — 그리고 그렇게 **말한다**

`/cloud/rent` 는 인자를 스스로 조립하므로 손으로 고친 CLI 가 반영될 자리가 없다.
조용히 무시하면 **사용자가 고친 것과 다른 명령**이 임대 GPU 에서 돈다. 그래서 그 경우
[GPU 빌려서] 를 막고 이유를 붙인다.

#### 고르는 자리에 경고를 띄운다

오퍼 한 줄마다 `warnings` 를 그대로 붙였다 — bf16 에뮬레이션(§12-15), 커널 없음(§12-16),
CPU 부족. 확인 창에만 있으면 이미 마음을 정한 뒤다. 못 도는 기종은 선택지에서 **끄되
숨기지 않는다**(없는 셈 치면 "왜 안 보이지" 가 된다).

#### ⚠ 아직 확인 안 된 것

브라우저에서 실제로 클릭해 임대까지 도는 것은 **안 해 봤다.** API 경로
(`POST /cloud/rent` → 조달 → 학습 → 회수 → 파기)는 오늘 실기로 완주했고(§12-13),
이번에 추가된 것은 화면이 본문을 조립하는 부분이라 `/rent/preview` 로 실기 확인했다.


### 12-20. 빌리는 곳과 학습하는 곳을 갈랐다 (2026-09-17)

| | 전 | 후 |
|---|---|---|
| 클라우드 페이지 | 기계 + 학습 설정 + [빌리기] (한 묶음) | **기계만** 만든다 |
| 학습 페이지 | 클라우드를 모름 | 빌려 둔 기계를 **골라서** 학습 |
| 파기 | `finally` 가 반드시 | **사람이** 인스턴스 탭에서 |

#### ⚠ 이 분리가 건드리는 것은 안전장치다

한 묶음일 때는 `finally` 가 파기를 보장했다. 가르면 **학습이 끝나도 기계가 남는다** —
그래서 셋을 같이 손봤다.

**① 고아 판정.** 사람이 빌려 둔 기계는 관리하는 태스크가 없는 것이 **정상**이다. 그대로
두면 스캐너가 10분마다 빨간 경보를 울리고, 늘 울리는 알람은 꺼진 알람이다. 라벨을
`piper-box-` 로 나누고 고아에서 뺐다.

⚠ **왜 라벨인가.** 레지스트리에 적으면 게이트웨이가 죽는 순간 잃는다(§12-14 가 기동 때
임대 주장을 비우는 이유가 그거다). 그런데 "내가 이 기계를 빌렸다" 는 **사람의 결정**이라
프로세스보다 오래 살아야 한다 — 라벨은 Vast 가 들고 있으므로 우리가 무엇을 잊든 남는다.

**② 유휴 경고.** 빈 기계도 요금은 똑같이 나간다. 20분 넘게 학습 없이 떠 있으면 로그와
화면에 **지금까지 얼마 나갔는지와 함께** 말한다. 학습 중에는 시계를 지운다 — 안 지우면
6시간짜리 학습이 끝난 직후 "6시간째 유휴" 라고 말한다.

**③ 파기 금지.** `CloudJob.owns_instance=False` 면 `finish()` 가 **파기를 건너뛴다.**
여기가 제일 위험한 자리였다 — 사람이 [중지]를 누르면 `stop_now()` 가 `finish()` 를
부르고, 그대로 뒀으면 **남의 기계를 껐다.** 새 칸 `Phase.FINISHED` 가 "학습은 끝났고
기계는 그대로" 를 뜻한다. `DESTROYED` 로 적으면 살아서 과금 중인 기계를 "파기됨" 이라고
말하는 셈이라, 이 화면에서 제일 비싼 거짓말이 된다.

테스트가 **양쪽 불변식**을 다 잡는다 — 한 묶음은 어느 칸에서 끝나든 파기하고, 남의
기계는 어느 칸에서 끝나든 안 끈다.

#### 화면

- 학습 페이지: `piper-box-` 이면서 `running` 인 것만 고르게 한다. 한 묶음으로 도는
  기계(`piper-<job>`)는 자기 학습이 끝나면 스스로 파기되므로, 거기 다른 학습을 걸면
  **도중에 기계가 사라진다.**
- 고르는 자리에서 "학습이 끝나도 이 기계는 꺼지지 않습니다 — $X/h 가 계속 나갑니다" 를
  말한다. 확인 창에만 있으면 이미 시작한 뒤다.
- 빌릴 기계가 없으면 **여기서 빌리게 하지 않는다.** 빌리는 곳이 두 군데면 "끄는 책임"
  도 갈라진다 — 대신 클라우드 페이지로 가는 링크를 준다.
- 확인 창의 명령 미리보기도 목적지를 탄다(`/cloud/rent/preview` vs `/training/preview`) —
  인터프리터가 달라서, 안 그러면 **안 도는 명령**을 보여 준다.


### 12-21. 이미지를 다시 구워 올렸다 (2026-09-18)

`install-stack.sh` 의 고침(§12-13 의 다운로드 절반)은 **이미지 안의 스크립트**를 쓰므로
다시 굽지 않으면 실제 임대에 아무 효과가 없다.

| | |
|---|---|
| 새 태그 | `full-0.5.0-cu126-20260918` · `slim-0.5.0-cu126-20260918` (+ 이동 태그) |
| 익명 pull | ☑ 네 태그 모두 200 |
| 드리프트 | ☑ **없음** — `lerobot 0.5.0 · torch 2.11.0+cu126 · torchvision 0.26.0+cu126 · torchcodec 0.11.0+cu126`, arch `sm_50 … sm_90` — §12-3 과 문자 그대로 같다 |
| 학습 진입점 | ☑ `import lerobot.scripts.lerobot_train` 통과 |
| 템플릿 | ☑ 둘 다 새 태그 · **모든 필드 그대로** (`hash_id` 는 바뀜, `id` 는 그대로) |

⚠ **이번 드리프트 확인은 형식이 아니었다.** `install-stack.sh` 를 고쳤으므로 pip 레이어
캐시가 무효고, 의존성이 **새로 풀렸다** — §12-7 이 "캐시가 없는 기계에서 구우면 다른 것이
나올 수 있다" 고 적어 둔 바로 그 경우다. 그런데도 버전이 한 글자도 안 달라졌다.

#### ⚠ 템플릿을 고치기 전에 **버릴 템플릿으로 먼저 시험했다**

§12-7 에서 `--image_tag` 만 주고 고쳤다가 템플릿 둘을 날린 적이 있다(전체 교체다). 이번에
무서웠던 것은 `extra_filters` 였다 — 저장된 값은 JSON 인데 CLI 가 받는 `--search_params`
는 질의 문자열이라, 형태가 다르면 필터가 기본값으로 초기화된다.

그래서 **`piper-probe-DELETEME` 를 하나 만들어** 같은 `--search_params` 로 어떤
`extra_filters` 가 생기는지 봤다 — 실제 템플릿의 값과 **바이트까지 같았다.** 그 뒤에야
진짜 템플릿을 고쳤고, 고친 뒤 응답이 아니라 **목록으로** 전 필드를 대조했다.

#### ⚠ 실기가 잡은 것: 판정이 두 곳에 있었다

새 흐름으로 기계를 만들자마자 인스턴스 탭이 `orphan=True` 로 칠했다. §12-20 에서
`find_orphans` 는 `piper-box-` 를 뺐는데 **라우터가 자기 판정 사본을 들고 있었다.**
사람이 방금 일부러 빌린 기계가 만들어지자마자 고아로 뜬 것이다. `is_orphan()` 하나로
합쳤다 — 정의가 둘이면 탭과 배너가 다른 말을 하고, 그러면 사람은 둘 다 안 믿는다.


### 12-22. API 키를 넣을 화면이 아예 없었다 (2026-09-18)

`.44`·`.120` 에서 RENT 표가 보이길래 "키가 이미지에 박힌 것 아니냐" 는 의심이 나왔다.
확인해 보니 둘 다 아니었다.

**① 이미지에는 키가 없다.** 열어서 봤다 — `~/.config/vastai/vast_api_key`·`.env`·환경변수
어디에도 없고 `find / -name vast_api_key` 도 빈손이다. 있을 수가 없었다: **앱에 키를 쓰거나
읽는 코드 자체가 없었다.**

**② 표가 보이는 것은 키와 무관하다.** Vast 의 오퍼 검색은 **공개 API** 다. 빈 `HOME` 에
빈 키로 돌려 확인했다:

```
search offers → 정상 조회
show user     → {"error": true, "status_code": 403, "msg": "This action requires login."}
```

즉 기계 목록과 가격은 로그인 없이 누구나 본다. 계정이 필요한 것(잔액·인스턴스·빌리기)만
키를 탄다.

#### 진짜 문제는 반대쪽이었다 — **넣을 방법이 없었다**

개발 머신이 되는 이유는 예전에 사람이 `vastai set api-key` 를 해 둬서 CLI 의 제 파일이
있기 때문이다. **배포판은 컨테이너라 그 파일이 없고, 설정 화면도 없었다.** 코드가 그렇게
적고 있었다 — "자격증명 저장소가 생기면(§9-1 의 `/credentials`) 여기서 키를 넘긴다".

| | |
|---|---|
| 저장 자리 | `config_dir/cloud/vast_api_key` — SSH 키와 **같은 데이터 볼륨**, 재설치를 넘어 남는다 |
| 권한 | `0600` (만들고 권한을 준 **뒤** 쓴다 — 쓰고 나서 chmod 하면 그 사이에 읽힌다) |
| 검증 | **먼저 써 보고 저장한다.** 틀린 키를 "설정됨" 으로 두면 표시는 초록인데 아무것도 안 되는, 제일 헷갈리는 상태가 된다 |
| 우선순위 | `.env` 의 `VAST_API_KEY` 가 **이긴다.** 운영자가 배포 수단으로 심은 값을 화면이 덮으면, 그는 자기 키가 왜 안 먹는지 알 길이 없다 |
| CLI 전달 | **env 로.** `ps` 로 남의 argv 를 읽을 수 있는 기계에서 argv 는 비밀을 두는 자리가 아니다 |

⚠ **키는 나오는 길이 없다.** 응답은 `configured`·`source`·끝 네 자리뿐이고, 화면의 입력칸은
`type=password` 에 저장된 값을 되채우지 않으며 저장 뒤 비운다. 게이트웨이는 `/api/ext/v1`
말고는 인증이 없어서, LAN 에서 이 포트에 닿는 누구나 응답을 읽는다 — 한 번 돌려주면 그
응답이 곧 유출 경로다. 테스트가 그 자리를 지킨다(응답에 키를 담게 고치면 실패한다).

⚠ 틀린 키는 **400** 이다. `_to_http` 를 그대로 쓰면 502 가 나오는데 그건 "게이트웨이가
고장" 이라는 뜻이라 사람이 엉뚱한 곳을 본다 — 실제로는 붙여넣기를 다시 해야 한다.

#### ⚠ 남은 위험: 게이트웨이에는 인증이 없다

키가 새는 것과 별개로, **키가 설정된 기계라면 LAN 에서 그 화면에 닿는 누구나 [빌리기]를
눌러 돈을 쓸 수 있다.** 이건 클라우드 기능만의 문제가 아니라 게이트웨이 전체의 성질이라
여기서 고치지 않았다 — 적어 둔다.


### 12-23. "확인 못 했다" 를 "안 돼 있다" 로 말하고 있었다 (2026-09-18)

> 빌리려면 먼저: SSH 키 — 설정 → 클라우드 에서 [Vast 계정에 등록] 을 눌러 주세요

키는 **이미 등록돼 있었다.** 그런데 이 줄이 계속 떴고 [빌리기]는 막혀 있었다. 그 버튼을
눌러도 같은 자리로 돌아온다 — **빠져나갈 수 없는 안내**였다.

#### 판정은 셋인데 화면은 둘로 말했다

`_ssh_registered()` 는 `True`(등록됨) · `False`(없음) · **`None`(확인 못 했다)** 을 돌려준다.
docstring 도 "모르면 `None` — 거짓 통과를 만들지 않는다" 라고 적고 있었다. 그런데 준비도가
그걸 이렇게 접었다:

```python
"ok": bool(exists) and registered is True        # None → False
"detail": ... "[Vast 계정에 등록] 을 눌러 주세요"   # None 도 이 문구
```

거짓 통과는 피했는데 **거짓 실패**를 만들었다. 그리고 실패의 처방이 틀려서, 사람은 아무
효과도 없는 버튼을 반복해서 누르게 된다.

#### 왜 확인을 못 했나 — 그것도 이제 화면이 말한다

실측: `vastai show ssh-keys` 가 **401 Invalid user key** 로 죽는다. 같은 키로
`vastai show user` 는 **통과한다**(크레딧까지 나온다). 즉 SSH 키 문제가 아니라 **API 키의
권한/role 문제**인데, 화면은 SSH 키를 가리키고 있었다 — 고칠 곳과 가리키는 곳이 달랐다.

이제 사유를 그대로 싣는다:

> 등록 여부를 확인하지 못했습니다 (목록이 배열이 아닙니다: {'error': True,
> 'status_code': 401, 'msg': 'Invalid user key'}) — 이미 등록했다면 그대로 두세요.
> 빌린 뒤 접속이 안 되면 여기를 의심하세요

#### ⚠ 모를 때는 막지 않는다

막는 쪽이 더 나쁘다 — 제대로 해 둔 사람이 **영영** 못 빌린다. 우리가 확인을 못 한 것은
그 사람 잘못이 아니고, 틀렸을 때의 대가는 실패한 임대 한 번인데 그건 파기 보장이 이미
받아 준다. 반대로 **정말 없을 때(`False`)는 계속 막는다** — 그때는 빌려 봐야 접속이 안 돼
돈만 나간다. 테스트가 셋을 다 고정한다.

#### ⚠ 고치다 한 번 크게 잘못했다

`s[s.index(시작):s.index(끝)]` 로 함수를 잘라내려 했는데 **끝이 시작보다 앞에 있어서** 그
슬라이스가 빈 문자열이 됐고, `str.replace("", 새것, 1)` 이 새 블록을 **파일 맨 앞**에 끼워
넣었다. 라우터가 `async def` 로 시작하는 파일이 됐다. ruff 의 E402(임포트가 맨 위가 아니다)
가 그걸 잡아 줬다 — 문법은 멀쩡했으므로 임포트만 봤으면 못 봤다. 되돌리고 `assert end >
start` 를 붙인 뒤 다시 했다.


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
만들어 로컬에서 구움.

**2026-09-15 — W0 준비가 대부분 끝났다.**

| | |
|---|---|
| 학습 이미지 | ☑ **GHCR push 완료** — `ghcr.io/wego-robotics/piper-train` 네 태그(full·slim × 고정·이동) |
| 이미지 공개 | ☑ **공개 전환 완료** — 네 태그 모두 **익명 pull 확인** |
| Vast 계정 | ☑ API 키 설정(`vastai` 1.7.0) · 크레딧 **$25** |
| 오퍼 필터 | ☑ §2 문자열 그대로 **52개** · 최저 $0.376/h RTX 4090 — 필드명까지 확인 |
| 게이트웨이 SSH 키 | ☑ **Piper Studio 가 만든다** — 설정 → 클라우드 탭(`1cdbca4`). ☑ 계정 등록됨 — 지문 확인(2026-09-16) |
| 설정 도우미 | ◐ 기획 §9-1 · **키 부분 구현** — `GET/POST /api/cloud/ssh-key` + 등록(지문 확인) |
| 템플릿 | ☑ **둘 생성** — `piper-train full cu126`(id 728456) · `slim cu126`(id 728457). `runtype=ssh` + `ssh_direct`, onstart `/opt/piper/bootstrap.sh`, disk 40GB |

⚠ **`vastai search templates` 는 기본이 공개 템플릿만이다.** 질의 없이 부르면 2048개가
나오는데 **내 것은 하나도 없다** — 만든 템플릿은 `private: True` 라서다. `private=true` 나
`creator_id=<id>` 를 줘야 나온다. 이걸 모르면 "템플릿이 사라졌다" 로 읽고, W2 의 프로바이더
층이 같은 조회로 자기 것을 못 찾는다.

⚠ 템플릿의 필터는 Vast 쪽 표현으로 **번역돼 저장된다** — `cuda_vers>=12.4` 가
`{"cuda_max_good": {"gte": "12.4"}}` 로 들어갔다. 오퍼 응답의 필드명(`reliability2`)과 또
다르므로, 파서를 쓸 때 셋을 헷갈리면 안 된다.

**2026-09-16 — RENT 탭 기획(§9-2) · 오퍼 응답 실측.**

`gpu_name=RTX_4090 rentable=true`(§2 보다 느슨한 질의)로 **60개**를 받아 필드를 뜯었다.
오퍼 하나에 **필드 100개**. 여기서 나온 사실 셋이 §9-2 를 바꿨다:

- ⚠ **`dph_total` 을 그대로 쓰면 값이 틀린다.** `dph_total = dph_base + storage_total_cost`
  인데 그 디스크가 **검색 기본값(~5GB)** 이다. 우리 템플릿은 40GB — 다시 계산하면
  `0.3467 + 0.8667×40/730 = $0.394/h`, 표시값보다 **12% 비싸다**. 전송비는 별도로 GB 당
  (`inet_down_cost` $0.0156/GB · `inet_up_cost` $0.0169/GB).
- **`cpu_cores_effective` 가 0 인 오퍼가 3/60.** 중앙값은 32. GPU 만 보고 고르면 데이터로더가
  굶는 기계를 집는다 → 경고 배지.
- **`verification` 은 60/60 전부 `verified`.** 4090 급에선 배지가 무의미하다 — **안 만든다.**
  `duration` 도 최소 5일·중앙 65일이라 가드가 아니라 열로만 둔다.

짐작으로 배지를 만들었다면 아무도 안 보는 열이 둘 늘 뻔했다. 필드 표본을 먼저 뜬 값이다.

**같은 날 — §9-2 구현 완료(고르는 데까지).** 게이트웨이 재기동 후 실물 확인.

| | |
|---|---|
| 프로바이더 | ☑ `providers/base.py`(Offer·OfferFilter·경고) · `providers/vast.py`(질의·파서·60초 캐시) |
| 엔드포인트 | ☑ `GET /api/cloud/vast/offers` · `/templates` · `/readiness` — 살아있는 게이트웨이에서 200 |
| 화면 | ☑ `/cloud` — LeRobot 그룹, 탭 `RENT`·`인스턴스`(빈 껍데기) |
| fixture | ☑ `backend/tests/fixtures/vast_offers.json` — 실물 13개(경고 걸리는 표본 포함) |
| 테스트 | ☑ `test_cloud_offers.py` 21개 · 탭 위치 테스트를 `CloudPage` 까지 확장 · 전체 1885 통과 |
| 준비도 | ☑ **전부 초록** — SSH 키 지문이 계정에서 확인됨 · 크레딧 $25 |
| 빌리기 | ☐ **일부러 비활성** — 파기·예산 가드(W3)와 같이 켠다 |

구현하면서 실물이 알려 준 것 다섯:

- ⚠ **`balance` 가 아니라 `credit`.** 실측 계정이 `balance: 0, credit: 25.0` 이다.
  `balance` 를 읽었으면 멀쩡한 계정이 "$0 · 최대 0시간" 으로 화면 전체가 막혔다.
- ⚠ **보정률은 오퍼마다 다르다.** `storage_cost` 가 호스트마다 벌어져 같은 40GB 라도
  어떤 오퍼는 +3%, 어떤 오퍼는 +11% 다. **일률적인 곱셈으로는 못 맞춘다** — 오퍼마다
  계산해야 한다. 표에 `GPU 몫 + 디스크 몫` 을 쪼개 보이는 이유다.
- ⚠ **CLI 는 오류를 반환코드 0 으로 낸다.** 키가 틀리면 종료코드는 0 인데 본문이
  `{"error": true, "msg": "Invalid user key"}` 다. 반환코드만 보면 그 오류가 **빈 목록**으로
  둔갑해 "오퍼가 없습니다" 가 된다. 파서가 본문의 `error` 를 본다.
- ☑ **`VAST_API_KEY` 가 저장된 키를 이긴다**(가짜 키로 확인). 그래서 키는 argv 가 아니라
  env 로 간다 — argv 는 같은 호스트의 다른 프로세스가 `ps` 로 읽는다.
- ⚠ **속도와 비용은 다른 필드다.** `inet_down`(Mbps)과 `inet_down_cost`($/GB) — 업로드에
  걸리는 *시간*은 앞이 정하고 *청구서*는 뒤가 정한다. 표에 둘 다 있어야 한다.

**같은 날 — GPU 선택지를 전부 열었다(사용자 요청).** "GPU 에 3060 도 있어야지. 싹 다
보여줘." 고정 7개를 걷어내고 서버 카탈로그(`GET /api/cloud/gpus`)로 바꿨다. 자세한 것은
§9-2 의 「GPU 고르기」·「세대」·「VRAM」 절. 요약:

| | |
|---|---|
| 선택지 | ☑ **70종** — 고를 수 있음 55 · 없음 15(사유 표기) · `전체(제한 없음)` 옵션 |
| 3060 | ☑ cc 860 · 12GB · 오퍼 18대쯤 · **$0.061/h** — 한국 소재 기계도 있다 |
| 다중 선택 | ☑ 백엔드 지원(`gpu_name in [A,B]`) · 화면은 아직 단일 + 전체 |
| 세대 판정 | ☑ `CC_MIN=500`~`CC_MAX=1000` — **이미지가 찍은 arch list** 근거 |
| 못 도는 기종 | ☑ 숨기지 않고 사유를 적는다 — 빈 표도 이유를 말한다 |
| 테스트 | ☑ `test_cloud_offers.py` 55개(라우터 포함) · 전체 1920 통과 |

⚠ **아직 안 한 것** — 실측으로 확인됐지만 이번 범위 밖이다:

1. ~~bf16 이 cc 800 미만에서 조용히 에뮬레이션으로 내려간다.~~ → **§12-15 에서 했다.**
2. **판정이 템플릿에 따라 달라져야 한다.** 지금은 cu126 고정으로 본다. 사용자가 slim/full
   말고 cu128 이미지를 고르면 Blackwell 이 풀리고 대신 구형이 막힌다.

다음: 첫 인스턴스에서 `env-check.sh` → W0 의 나머지(데이터셋 업로드 → 원격 학습 → 회수 →
로컬 추론). 그다음이 W3(파기·예산 가드)이고, 그게 들어와야 [빌리기]를 켠다.
