# 외부 학습 서버 — 임대 GPU (Vast.ai)

[cloud-training.md](cloud-training.md) 5~8단계의 구체화. "이미 켜져 있는 SSH 박스에서 학습"까지는
실기 검증이 끝났다 (300스텝 완주 · 게이트웨이 재시작 후 재부착). 이 문서는 그 다음 —
**필요할 때 빌리고 끝나면 사라지는 서버**를 다룬다. 프로바이더 1호는 Vast.ai(§2).

핵심 결론을 먼저: **러너를 새로 만들지 않는다.** 남은 일은 학습 실행이 아니라
① 서버의 수명(조달·파기), ② 데이터·모델 왕복, ③ 돈이 새지 않는 구조다.

## 1. 검토 결과 — 어디까지 와 있나 (2026-09-01 기준)

이 기능의 어려운 절반은 이미 코드에 있다. 전부 실기 또는 테스트로 확인된 것:

| 조각 | 상태 | 근거 |
|---|---|---|
| 원격 실행 (tmux 세션 · 로그 tail · 종료 마커) | ☑ | [ssh.py](../backend/app/services/training/runners/ssh.py) — 실기 300스텝 완주 |
| 게이트웨이 재시작 후 재부착 + 로그 되읽기 | ☑ | [ssh.py `restore()`](../backend/app/services/training/runners/ssh.py#L313) — 재부착 시 138줄 복원 |
| job 레지스트리 (버스 위 → 재시작 생존) | ☑ | [jobs.py](../backend/app/services/training/jobs.py) — `provider`/`instance_id` 필드 자리까지 있음 ([L73-75](../backend/app/services/training/jobs.py#L73-L75)) |
| WS `job_id` · 로그 링버퍼 · REST 페이지네이션 | ☑ | [training.py `/jobs`](../backend/app/routers/training.py#L206) |
| 배타 가드 우회 — 원격 학습은 추론을 안 막는다 | ☑ | [exclusivity.py `_contends()`](../backend/app/services/exclusivity.py#L188) |
| 데이터셋 HF 업로드 (진행 로그 WS 송출) | ☑ | [datasets.py `upload_to_hub`](../backend/app/routers/datasets.py#L310) — `hf upload-large-folder` |
| 모델 HF 다운로드 → `models_dir` 안착 | ☑ | [hub.py `/download`](../backend/app/routers/hub.py#L58) → `model_scanner` 가 자동으로 잡는다 |
| 인자 조립의 경로·인터프리터 분리 | ☑ | [`build_train_args(python=...)`](../backend/app/core/cli_mapping.py#L367) — cloud-training 2단계 산출물 |
| **인스턴스 조달·파기 (수명 관리)** | ☐ | 이 문서 §3 |
| **환경 재현 (학습 이미지)** | ☐ | §4 |
| **체크포인트 회수 자동화** | ☐ | §5 — `push_to_hub=false` 강제가 함정 |
| **비용 가드** | ☐ | §6 |
| **시크릿 (Vast 키 · HF 토큰)** | ☐ | §7 |

> ⚠ cloud-training.md 하단 "상태" 절이 낡아 있었다 (0~2 완료로 적혀 있으나 실제는 0~4 —
> 작업 표의 ☑ 와 [ROADMAP 3b-3.5](../ROADMAP.md)가 근거). 이번 검토에서 바로잡았다.

## 2. Vast.ai 의 제약 → 그대로 설계 결정이 된다

| 확인된 제약 | 강제되는 결정 |
|---|---|
| 인스턴스는 소모품 — 호스트가 바뀌면 로컬 디스크·설치 전부 증발 | 환경은 **Docker 이미지로 고정.** 인스턴스에는 상태를 두지 않는다 |
| Volume 은 물리 호스트에 묶인다 | 영속 저장소로 **부적합** → 데이터·체크포인트는 HF Hub 왕복 (이미 있는 경로, §5) |
| 호스트 NVIDIA 드라이버는 손댈 수 없다 | 이미지 CUDA 를 보수적으로 고정(12.4~12.6)하고 **오퍼 검색에서 `cuda_vers>=` 로 거른다.** 드라이버 버전을 외우는 게 아니라 필터가 답 |
| 경매형(interruptible)은 싸지만 중단된다 | 초기에는 **on-demand 만.** resume 파이프라인 검증 전까지 interruptible 금지 |
| 공식 CLI(`vastai`) 존재 | 이 저장소의 원칙(CLI 래핑, subprocess) 그대로 붙는다 — SDK 의존 없음 |
| 임대 인스턴스 = 남의 하드웨어 | 반입 비밀은 **스코프 최소 HF 토큰 하나**로 제한 (§7) |

오퍼 검색은 이런 모양이다 (정확한 필드명은 V0 에서 확인):

```
vastai search offers 'gpu_name=RTX_4090 cuda_vers>=12.4 reliability>0.98 inet_down>200'
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
TrainManager ─ TrainRunner ─► SSHRunner        ← 그대로 재사용 (실기 검증됨)
                                  ▲ user@host:port
                             VastProvider (신규) ← 조달·파기·가격·목록만
```

`CloudProvider` 인터페이스는 [cloud-training §2](cloud-training.md) 정의 그대로
(`provision / terminate / list_instances / price`). 러너와 프로바이더를 분리해 뒀기 때문에
**Vast 어댑터는 "SSH 되는 박스를 내놓는 것"까지만 책임진다** — 학습 실행·모니터링·재부착은
이미 있는 코드가 한다.

### SSHRunner 에 필요한 최소 확장 3개 (전부 배선 수준)

1. **포트** — [`_ssh_argv`](../backend/app/services/training/runners/ssh.py#L65) 가 `-p` 를
   못 받는다. Vast 는 임의 포트를 준다. spec 또는 생성자에 port 추가.
   (V0 에서는 `~/.ssh/config` 의 Host alias 로 **코드 0** 우회 가능)
2. **원격 인터프리터** — [`start_training`](../backend/app/routers/training.py#L155) 이
   `build_train_args` 를 기본 python(**로컬 conda 절대경로**)으로 부른다. 사내 박스는 경로가
   우연히 같아 살았지만 이미지에서는 다르다. 원격 러너일 때 `python=` 을 넘긴다 —
   `build_train_args` 는 이미 받는다. 새 설정 `PIPER_TRAIN_REMOTE_PYTHON` (기본 `python`).
3. **원격 env** — `HF_TOKEN`(+`HF_ENDPOINT`, 옵션 `WANDB_API_KEY`)을 `spec.env` 로. 지금은
   [AMP 하나만 간다](../backend/app/routers/training.py#L118). SSHRunner 가 `injected_env()` 를
   안 쓰는 이유(버스 주소 등은 *이 기계의* 사실)는 그대로 유효 — **필요한 것만 명시적으로.**

### 인스턴스 수명 상태기계

```
searching → creating → ssh_wait → (기존) training → retrieving → destroying → destroyed
                                        │ 실패·중지 포함 어느 경로로 끝나든
                                        └────────────► destroy 는 반드시 지난다
```

- `destroy` 후 **재조회로 소멸 확인** — 실패는 조용히 넘기지 않고 빨간 배너
  (cloud-training §7-6).
- 수명 상태는 `JobRecord` 에 그대로 얹는다 — `provider`/`instance_id` 필드가 이미 있고,
  레지스트리는 버스 위라 게이트웨이가 재시작해도 남는다. **재기동 시 레지스트리에
  `instance_id` 가 있으면 프로바이더에 생사를 물어 재부착 또는 고아 처리** — 로컬 복원과
  같은 자리([manager.py `restore_running_process`](../backend/app/services/training/manager.py#L193))에 분기 하나.

## 4. 학습 이미지 — 한 장이면 된다

| 내용물 | 이유 |
|---|---|
| 베이스: PyTorch CUDA 12.x runtime (보수적 버전) | 최신 CUDA 고집 = 쓸 수 있는 호스트 축소. ACT 성능 차이는 없다 |
| `lerobot[smolvla]==0.5.0` + `transformers==5.3.0` | [**Dockerfile.base:64 와 같은 핀.**](../backend/Dockerfile.base#L64) 버전이 다르면 체크포인트가 로컬 추론에서 안 열릴 수 있다 (cloud-training §4) |
| `lerobot_policy_act_aux` | ⚠ **act_aux 를 원격에서 학습하려면 필수** — `lerobot-train` 이 접두사로 자동 import 한다 ([Dockerfile:21](../backend/Dockerfile#L21)). 빼먹으면 "모르는 정책"으로 죽는다 |
| tmux · ffmpeg | tmux 는 SSHRunner 의 전제. ffmpeg 는 데이터셋 디코딩 |
| robot/카메라 vendor 패키지 **없음** | 학습은 데이터셋만 본다 — CAN·RealSense 코드가 원격에 갈 이유가 없다 |

- 파일: `deploy/train.Dockerfile` (Dockerfile.base 에서 파생한 슬림판).
  빌드·푸시는 기존 [deploy/registry.sh](../deploy/registry.sh) · [build-base.sh](../deploy/build-base.sh) 흐름에 편승.
  단 Vast 가 pull 하려면 **공개 레지스트리(Docker Hub/GHCR)** 또는 인증 설정 필요 — V0 에서 결정.
- 태그는 날짜+버전으로 고정하고 **job 레코드에 이미지 태그를 기록** — "이 체크포인트는
  어느 환경에서 학습됐나"를 나중에 추적할 수 있게.

## 5. 왕복 전송 — 전부 기존 경로 재사용

```
[로컬] upload_to_hub (있음) ──► HF private dataset repo ──► 원격 lerobot-train 이 pull
[원격] --policy.repo_id 로 Hub 푸시 ──► HF private model repo ──► /api/hub/download (있음) ──► models_dir → model_scanner → 추론 페이지
```

- **순서 강제: 업로드 검증 전에는 provision 하지 않는다.** 뒤집히면 빈 GPU 가 과금된다
  (cloud-training §5). 재업로드 회피(해시 비교)도 같은 절의 설계를 따른다.
- 원격에는 `--dataset.repo_id` 만 주면 된다 — LeRobot 이 알아서 받는다 (`HF_TOKEN` 필요, §3-3).
- **회수의 함정**: [cli_mapping.py:398](../backend/app/core/cli_mapping.py#L398) 이
  `policy_repo_id` 없으면 `--policy.push_to_hub=false` 를 강제한다. 로컬에선 맞고
  임대 서버에선 **회수 경로를 지우는 설정**이다. 원격 러너일 때는 `policy_repo_id` 를
  필수로 하고 기본값을 자동 생성한다: `{user}/{robot_id}_{dataset}_{policy}_{날짜}`.
- 학습 종료 → 자동으로 기존 다운로드 경로 실행 → 회수 확인 후에만 destroy.
- ⚠ `push_to_hub` 가 최종본만 올리는지 `save_freq` 마다인지 **V0 에서 확인** —
  on-demand 만 쓰는 동안은 치명적이지 않지만 interruptible 을 열려면 필수 지식이다.

## 6. 비용 가드 — "돈 E-stop" 을 Vast 에 맞게

층위별로, 안쪽부터:

1. **`timeout $((MAX_H*3600)) lerobot-train ...`** — 스크립트 수준 상한. 사람·게이트웨이·
   인터넷이 전부 사라져도 학습 프로세스는 반드시 끝난다. 종료 마커(`_EXIT_MARK`, 이미 있음)가
   찍히므로 아래 2번이 이어받는다.
2. **종료 마커 수신 → 회수 → destroy** — 정상 경로. 상태기계의 `finally` 자리(§3).
3. **고아 스캐너** — 기동 시 + 주기적으로 `vastai show instances` 와 레지스트리를 대조.
   레지스트리에 없는 인스턴스 = 종료 API 가 실패했는데 성공으로 처리된 경우 → 경고 배너 + 파기.
4. **예산 상한** — job 생성 시 USD 입력, `rate × 경과` 가 닿으면 정지+파기. `JobRecord.cost`.

> ⚠ **인스턴스 안에서의 자폭(`vastai destroy` from inside)은 하지 않는다.** 그 방법은 계정
> API 키를 남의 하드웨어에 두는 일이다. 1+3 조합이 같은 역할을 한다 — "스스로 죽는" 대신
> "**서버가 반드시 눈치채는**" 구조.
>
> 확인 모달은 논블로킹 React 모달로 — `window.confirm` 은 heartbeat 를 막아 로컬 추론을
> E-stop 시킨다 (실제 사고 전례, cloud-training §7).

UI 에는 항상 시간당 요금 / 누적 / 예상 총액. wandb 는 인스턴스가 사라져도 로그가 남는
유일한 외부 경로라 원격 학습에서는 기본 on 을 검토 (옵션은 이미 있다).

## 7. 시크릿

- `PIPER_VAST_API_KEY` — `backend/.env`. 프론트에는 마스킹만, CLI preview 에 싣지 않는다
  ([cloud-training §8](cloud-training.md) 규칙 그대로 — `/api/training/preview` 는 명령
  문자열을 화면에 그대로 준다).
- **원격에 가는 유일한 비밀 = HF 토큰.** 계정 토큰을 넘기지 않고 **fine-grained 토큰을
  새로 발급**한다: 해당 dataset repo read + 해당 policy repo write 만. 실험 시즌이 끝나면 revoke.
- 로그 브로드캐스트 마스킹 필터 — `vastai` CLI 가 키를 에코하는 경우 대비.

## 8. 단계 — 각 단계가 그 자체로 쓸모 있게

| # | 작업 | 산출물 | 코드 변경 | 선행 |
|---|---|---|---|---|
| **V0** | **수동 완주 1회** — 이미지 빌드·푸시 → `vastai` CLI 로 4090 1대 → `~/.ssh/config` alias 를 `PIPER_TRAIN_SSH_HOST` 에 → **웹에서** 시작·모니터링·중지 → 회수 → destroy | `deploy/train.Dockerfile` + 실측 기록 (pull·업로드 시간, 시세, `push_to_hub` 동작) | **0** (설정만) | — |
| V1 | SSHRunner 확장 3개 (§3) — 포트 · 원격 python · env 주입 | alias 트릭 없이 코드 경로로 | 소 | V0 |
| V2 | `VastProvider` (CLI 래핑: search/create/wait-ssh/destroy/list) + 수명 상태기계 + 재기동 시 인스턴스 재부착 + 고아 스캐너 | 웹 버튼으로 빌리고 반납 | 중 | V1 |
| V3 | 회수 마감 (`policy_repo_id` 러너 분기 · 자동 다운로드) + 비용 가드 4층 + 시크릿 저장·마스킹 | **방치 가능한 학습** | 중 | V2 |
| V4 | UI (실행 위치 선택 · 오퍼 피커 · 비용 배지) + 가이드 페이지 (cloud-training §9 방식) | 온보딩 | 중 | V3 |
| V5 | 동시 N개 — `MAX_CONCURRENT_JOBS` 해제 + TrainManager 다중화 | 실험 병렬 | **대** | V3 |

**V0 이 이 기획의 절반이다.** 이미지 pull 시간·업로드 시간·실제 시세·`cuda_vers` 분포 같은
"기본값을 정하는 숫자"는 실측 없이 못 정한다. 그리고 **당장 필요한 실험 10회는 V0 상태로도
돈다** — 인스턴스 생성·파기만 손으로 하고, 학습 시작·모니터링·중지·그래프는 웹이 이미 한다.

V5 를 마지막에 둔 이유: 레지스트리·WS 는 이미 N개 준비가 됐지만
[TrainManager 는 싱글톤](../backend/app/services/training/manager.py#L229)(러너·트래커 1벌)이라
다중화가 실제 작업이다. 병렬 실험은 좋지만, **하나가 안 새는 것**이 먼저다.

## 9. 먼저 정해야 할 것 (cloud-training §13 의 남은 답)

1. **프로바이더 1호 = Vast.ai 확정?** — 이 문서의 전제. RunPod 으로 바뀌면 §2 표만 다시 쓰면
   된다 (러너·전송·가드는 프로바이더 무관).
2. **동시 job 수** — V5 전까지 1 유지를 제안.
3. **예산 기본값** — job 당 $10 제안 (ACT 추정치의 5배 여유).
4. **interruptible** — 초기 제외 제안. resume 검증(§5 의 `push_to_hub` 확인 포함) 후 재검토.
5. **HF repo 소속** — 개인 계정 vs 조직 계정. 데이터 집계 경로([ROADMAP](../ROADMAP.md)
   "데이터 집계" 절)와 같은 곳이어야 한다.

## 검증

- **V0**: 짧은 학습(steps=500)으로 한 바퀴 전부 — 업로드 → 원격 학습(웹 그래프 확인) →
  회수 → **회수된 체크포인트로 로컬 추론까지.** 여기까지 안 되면 이후 단계는 무의미.
- **V2**: 학습 중 게이트웨이 kill → 재기동 → 재부착 확인. 레지스트리를 지우고 재기동 →
  고아 스캐너가 인스턴스를 찾는가. destroy 후 재조회 확인.
- **V3**: **의도적으로 게이트웨이를 죽인 채** timeout 이 학습을 끝내고, 재기동한 게이트웨이가
  인스턴스를 회수·파기하는가. 검증하지 않은 가드는 없는 것과 같다 (cloud-training 검증 절).
- 프론트 변경은 `cd frontend && npm run build` (`npx tsc --noEmit` 은 no-op).

## 상태

☐ 미착수 — V0 부터.
