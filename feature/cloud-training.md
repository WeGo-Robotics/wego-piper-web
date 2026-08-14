# 클라우드 GPU 학습 지원

외부 클라우드 GPU에서 `lerobot-train`을 돌리고, 웹에서 그 학습을 로컬 학습과 똑같이
보고/멈추고/체크포인트를 회수한다.

## 왜 필요한가

| 지금 | 클라우드 이후 |
|---|---|
| 학습 중 GPU 점유 → 추론 시작 시 409 | ☑ **된다.** 원격 학습이면 배타 가드가 비켜준다 — 러너의 `occupies_local_gpu` 를 [`exclusivity._contends()`](../backend/app/services/exclusivity.py) 가 본다. 학습은 여전히 "실행 중"으로 보이되 추론을 안 막는다 |
| 로컬 GPU 1장 = 실험 1개 직렬 | 실험 N개 병렬 |
| SSH 세션 종료 시 학습이 같이 죽음 (linger 문제) | 원격 job은 로컬 세션과 무관 |
| 서버 리로드 시 상태만 복원, stdout 재연결 불가 ([train_manager.py:172-195](../backend/app/services/train_manager.py#L172-L195)) | 로그를 원격에서 커서로 다시 읽으면 됨 |
| 24GB VRAM에 맞춰 batch/모델 크기를 타협 | A100/H100 80GB로 SmolVLA·Pi0 제대로 학습 |

즉 이 기능의 본질은 "**학습과 로봇을 분리**"하는 것이다. 부수 효과로 기존 두 가지 고질병
(세션 종료 시 학습 사망, 서버 리로드 시 학습 상태 유실)이 구조적으로 사라진다.

---

## 1. 지금 구조가 클라우드와 어긋나는 지점

먼저 이걸 정리해야 설계가 나온다. 전부 실제 코드 지점이다.

### (1) `TrainManager`가 곧 `ProcessManager`다 — 실행 방식이 박혀 있음

[train_manager.py:87](../backend/app/services/train_manager.py#L87)에서 `self.pm = ProcessManager()`를
직접 들고, `state`/`is_running`/`stop`이 전부 로컬 subprocess에 위임된다. 원격 job에는
PID도 SIGTERM도 stdout 파이프도 없다.

**하지만** 메트릭 파서([train_manager.py:27-31](../backend/app/services/train_manager.py#L27-L31))와
히스토리([train_manager.py:46-79](../backend/app/services/train_manager.py#L46-L79))는 실행 방식과
무관하다 — `lerobot-train`의 로그 한 줄만 있으면 된다. **이 둘을 분리하는 것이 리팩터의 전부다.**

### (2) 싱글톤 + 단일 job 가정

[train_manager.py:216](../backend/app/services/train_manager.py#L216)의 `train_manager = TrainManager()`
하나에 `metrics`/`history`/`output_dir`이 매달려 있다. 클라우드는 동시 N개가 기본이므로
job별 상태가 필요하다. 영향 범위:

- [training.py:202-221](../backend/app/routers/training.py#L202-L221) `/checkpoints`가 `train_manager.output_dir`
  단일 값을 본다
- [ws.py:101-119](../backend/app/routers/ws.py#L101-L119) `train_log`/`train_state`/`train_metrics`에
  job 식별자가 없다
- [ws.py:180](../backend/app/routers/ws.py#L180) 접속 시 `train_state` 하나만 밀어줌
- [TrainingPage.tsx:133-138](../frontend/src/pages/TrainingPage.tsx#L133-L138)이 그걸 그대로 단일 상태로 받음

### (3) 세션 복원이 PID 파일 기반

[train_manager.py:82](../backend/app/services/train_manager.py#L82)의 `/tmp/piper_train_pid` +
`os.kill(pid, 0)`. 원격 job에는 적용 불가이고, `/tmp`라 재부팅 시 날아가며, PID 재사용
위험도 있다. 클라우드에는 **영속 job 레지스트리**가 따로 필요하다.

### (4) `build_train_args`가 로컬 파일시스템을 만진다

[cli_mapping.py:176-275](../backend/app/core/cli_mapping.py#L176-L275)에서:

- L178 `args = [settings.grpc_python, "-m", ...]` — **로컬 conda 파이썬 절대경로**가 첫 인자
- L235 pretrained의 `policy_preprocessor.json`을 **읽어서** rename_map 추출
- L255-269 pretrained의 `config.json`을 **써서** state/action 차원 오버라이드
- `--dataset.repo_id`, `--output_dir`이 로컬 경로 전제

원격에 그대로 보내면 인터프리터 경로부터 틀린다. 인자 생성을 **경로 해석과 분리**해야 한다.

### (5) GPU 경합 체크가 클라우드에도 걸린다

[training.py:112-116](../backend/app/routers/training.py#L112-L116)이 추론 실행 중이면 학습을
막는다. 로컬 GPU 기준으론 맞지만, **클라우드 학습에는 걸리면 안 된다** — 오히려 동시 실행이
이 기능의 목적이다. 러너 종류에 따라 분기해야 한다.

### (6) AMP는 CLI가 아니라 환경변수

[training.py:100-106](../backend/app/routers/training.py#L100-L106)의 `ACCELERATE_MIXED_PRECISION`.
러너 인터페이스가 `cmd`만 받으면 이게 조용히 빠진다 → `env`도 1급 필드로 넘겨야 한다.

---

## 2. 설계 — 러너 추상화

`pages.ts`(커밋 `181ace1`) 선례와 같은 방식: **한 사실은 한 곳에**.

```
backend/app/services/training/
  spec.py        TrainJobSpec / JobStatus / JobRecord  (실행 방식 무관 데이터)
  metrics.py     _METRIC_RE, TrainMetrics, TrainHistory  ← 현 train_manager에서 이사
  registry.py    jobs.json 영속화, 재연결, 폴링 루프
  runners/
    base.py      TrainRunner Protocol
    local.py     현 TrainManager (동작 동일)
    ssh.py       SSH+tmux 범용 러너
    hf_jobs.py   HF Jobs
  providers/
    base.py      CloudProvider Protocol (인스턴스 생성/파기/가격)
    vast.py  runpod.py  lambda_labs.py  static_ssh.py
```

### 두 개의 인터페이스로 나눈다

프로바이더 대부분은 "**SSH 되는 GPU 박스를 빌려주는 것**"이 전부다. 학습을 어떻게 돌리는지는
동일하다. 그래서 *인스턴스 조달*과 *학습 실행*을 분리하면 SSH 러너 하나로 4개 이상을 덮는다.

```python
class TrainRunner(Protocol):
    kind: str                 # "local" | "ssh" | "hf_jobs" | ...
    occupies_local_gpu: bool  # (5)번 경합 체크 분기용

    async def start(self, spec: TrainJobSpec) -> str: ...        # → job_id
    async def stop(self, job_id: str) -> None: ...
    async def poll(self, job_id: str) -> JobStatus: ...          # 상태 + 비용 + 잔여
    async def logs(self, job_id: str, cursor: int) -> tuple[list[str], int]: ...
    async def fetch_artifacts(self, job_id: str, dest: Path) -> None: ...

class CloudProvider(Protocol):     # SSH 계열만 사용
    async def provision(self, gpu: str, image: str) -> Instance: ...
    async def terminate(self, instance_id: str) -> None: ...
    async def list_instances(self) -> list[Instance]: ...   # 고아 인스턴스 탐지용
    async def price(self, gpu: str) -> float: ...           # USD/h
```

핵심은 `logs()`가 **커서 기반 pull**이라는 점이다. 로컬의 push(stdout 콜백)와 원격의 pull을
레지스트리 폴링 루프가 흡수해서, 그 위쪽(메트릭 파서 → WS 브로드캐스트)은 **완전히 동일한
코드를 탄다**. 로그 한 줄이 어디서 왔든 `_METRIC_RE`는 신경 쓰지 않는다.

> ☑ **4단계는 이 프로토콜을 기다리지 않고 먼저 붙었다.** 지금
> [`SSHRunner`](../backend/app/services/training/runners/ssh.py) 는 위의 job_id 기반
> 프로토콜이 아니라 **지금 있는 `TrainRunner`**(push 콜백)를 구현한다. 원격 pull 을
> push 로 바꾸는 자리는 `ssh -t tail -f` 를 읽는 스레드 하나다 — `SystemdProcess` 가
> journald 를 그렇게 읽는 것과 같은 모양이라, 이음매를 새로 만들 필요가 없었다.
> 위 프로토콜(=job_id·poll·cursor)은 **프로바이더가 여럿이 될 때** 필요하다.
> 지금은 "이미 켜져 있는 SSH 박스" 하나뿐이라 인스턴스 수명 자체가 없다.
>
> 실기 확인에서 **정확히 이 점이 값을 했다**: 원격에서 온 로그 280줄이 로컬과 같은
> 파서를 지나 `step=300 / progress 1.0` 까지 그대로 올라왔다. 위쪽 코드는 한 줄도
> 안 고쳤다.

### `TrainJobSpec` — 경로가 아니라 의미를 담는다

(4)번을 풀려면 spec에 로컬 절대경로 대신 **참조**를 담고, 인자 조립은 러너가 한다.

```python
@dataclass
class TrainJobSpec:
    dataset: DatasetRef        # repo_id + (로컬경로 | hub) — 러너가 해석
    policy_type: str
    pretrained: ModelRef | None
    policy_params: dict
    steps: int; batch_size: int; ...
    env: dict[str, str]        # ACCELERATE_MIXED_PRECISION 등 — (6)번
    output: OutputRef          # 로컬 dir | hub repo_id
```

`build_train_args`는 인터프리터를 인자로 받도록 바꾼다:
`build_train_args(params, python="/opt/venv/bin/python")` — 기본값은 `settings.grpc_python`이라
로컬 동작은 그대로다. 파일시스템을 만지는 부분(L235, L255-269)은 `LocalRunner` 전처리로 옮기고,
원격 러너는 `state_dim`/`action_dim`을 `--policy.*` 오버라이드로 전달하거나
**미지원으로 명시 거부**한다 (조용히 무시되면 잘못된 차원으로 몇 시간을 태운다).

---

## 3. 세션 관리

"학습을 걸어두고 브라우저를 닫아도 된다"가 요구사항의 핵심이다.

### 잡 레지스트리

`config_dir/cloud_jobs.json` (0600). `/tmp`가 아니라 `settings.config_dir` — 재부팅 생존.

```json
{
  "job_id": "j-20260811-1432-a3f",
  "runner": "ssh", "provider": "vast", "instance_id": "1234567",
  "state": "running",
  "spec": { ...TrainJobSpec... },
  "created_at": "2026-08-11T14:32:00+09:00",
  "log_cursor": 184320,
  "last_metrics": { "step": 42000, "loss": 0.31, ... },
  "cost": { "rate_usd_h": 0.44, "accrued_usd": 3.21, "budget_usd": 20.0 },
  "artifacts": { "hub_repo": "wego/act_pick_v3", "fetched": false }
}
```

쓰기는 **atomic**으로 (temp write + `os.replace`). 학습 6시간 중 전원이 나가도 레지스트리가
반쪽짜리로 남으면 안 된다.

### 재연결(reattach)

`lifespan`([main.py:42-47](../backend/app/main.py#L42-L47))에서 로컬 복원 옆에 클라우드 복원을 추가:

1. 레지스트리에서 `running`/`starting` job 로드
2. job별로 러너에 `poll()` → 살아 있으면 상태 갱신, 죽었으면 종료 처리 + 아티팩트 회수 시도
3. `log_cursor`부터 로그를 이어 받아 메트릭 파서에 흘림 → **히스토리 재구축**
   (로컬은 stdout 재연결이 불가했지만, 원격은 로그가 서버 쪽에 남아 있으므로 가능하다.
   이게 클라우드의 숨은 이득이다.)
4. 폴링 태스크 재기동

폴링 주기는 상태 10~30초 / 로그 3~5초 정도. 프로바이더 API에 rate limit이 있으니
job 수에 비례해 늘리고, WS 클라이언트가 0명이면 로그 폴링을 늦춘다(메트릭 폴링은 유지).

### WS 프로토콜 확장

기존 `train_*` 메시지는 **단일 job 가정**이라 그대로 두면 클라우드 job 2개가 서로를 덮어쓴다.

```jsonc
{ "type": "train_log",     "job_id": "j-...", "data": "..." }
{ "type": "train_metrics", "job_id": "j-...", "data": {...} }
{ "type": "train_state",   "job_id": "j-...", "data": "running" }
{ "type": "job_list",      "data": [ ...JobRecord 요약... ] }   // 신규
```

로컬 job은 `job_id: "local"`을 부여해 같은 경로를 타게 한다. 프론트는
[TrainingPage.tsx:133-138](../frontend/src/pages/TrainingPage.tsx#L133-L138)에서
`job_id`가 현재 보고 있는 job과 일치할 때만 반영하도록 고친다.

### 로그 볼륨

6시간 학습이면 `log_freq=200` 기준 메트릭 줄만 수천 개, 그 외 로그까지 합치면 수만 줄이다.
전부 WS로 밀면 브라우저가 죽는다. 서버에 job별 링버퍼(최근 N줄)를 두고, WS로는 신규분만,
전체는 `GET /api/cloud/jobs/{id}/logs?from=` 로 페이지네이션한다. 원본 전문은 원격
(또는 회수한 파일)에 둔다.

---

## 4. 프로바이더별 접근 방법

### 계층 분류 — 구현 비용이 완전히 다르다

| 계층 | 프로바이더 | 접근 | 구현 비용 |
|---|---|---|---|
| **A. SSH 박스** | Vast.ai, RunPod(Pod), Lambda Labs, 사내 GPU 서버 | 인스턴스 생성 API → SSH → tmux/nohup → `tail -f` | 러너 1개 + 프로바이더당 ~150줄 |
| **B. 관리형 Job** | HF Jobs, Modal, SageMaker, Vertex AI | 각 SDK로 job 제출, 로그/아티팩트 모델이 제각각 | 프로바이더당 러너 1개 |

**A를 먼저 만든다.** SSH 러너 하나로 4곳이 열리고, 그중 "사내 GPU 서버(static_ssh)"는
과금도 API 키도 없어서 **개발/테스트용 무료 대조군**이 된다. 프로바이더 없이 러너만
검증할 수 있다는 게 크다.

### 비교

| 프로바이더 | 인증 | 과금 | 중단 위험 | 데이터 | 로그 | 메모 |
|---|---|---|---|---|---|---|
| **사내 SSH** | SSH 키 | 없음 | 없음 | rsync | tmux pipe | 1순위 개발 대상 |
| **Vast.ai** | API key | 초 단위, 최저가 | **높음** (경매형 중단) | rsync / HF | SSH | 싸지만 체크포인트 회수 필수 |
| **RunPod** | API key (REST) | 초 단위 | 중간 (Community < Secure) | Network Volume / HF | SSH | 볼륨 재사용으로 재업로드 회피 |
| **Lambda Labs** | API key | 시간 단위 | 낮음 | rsync / HF | SSH | **재고 부족 잦음** → 가용성 사전 확인 필수 |
| **HF Jobs** | HF token (이미 있음) | 초 단위 | 낮음 | **Hub 네이티브** | API | LeRobot과 생태계가 같아 마찰 최소. 유료 플랜 필요 · **API 형태 확인 필요** |
| **Modal** | token | 초 단위 | 낮음 | Volume | SDK 스트리밍 | 이미지를 파이썬으로 정의 → 재현성 좋음 |
| **SageMaker / Vertex** | IAM/SA | 인스턴스+오버헤드 | 스팟 옵션 | S3 / GCS | CloudWatch / Logging | 사내가 이미 AWS/GCP면 결제·보안 심사가 쉬움 |

각 프로바이더 어댑터가 반드시 채워야 하는 항목(이걸 `providers/base.py`의 필수 필드로 강제):

1. 인증 방식과 키 발급 위치(콘솔 URL)
2. GPU 카탈로그 + 시간당 단가 + 가용성 조회
3. 인스턴스 생성 → SSH 접속 가능까지의 대기/폴링
4. 이미지 지정 방법 (아래 §8)
5. 종료 방법 + **종료 확인**
6. 중단(preemption) 통보가 있는지 — 있으면 hook으로 체크포인트 긴급 푸시

### 이미지 / 환경 재현

원격 lerobot 버전이 로컬과 다르면 **학습된 체크포인트가 로컬 추론에서 안 열릴 수 있다.**
이 저장소는 `vendor/`에 piper 로봇 패키지를 함께 들고 있으므로, 학습 전용 이미지를 하나
만들어 레지스트리에 올리고 태그를 고정하는 편이 안전하다
(`backend/Dockerfile`의 학습 부분만 떼어낸 슬림 이미지). 최소한 lerobot 커밋 해시를
job 레코드에 기록해서, 나중에 "이 체크포인트는 어느 버전으로 학습했나"를 추적할 수 있게 한다.

---

## 5. 데이터 전송

가장 오래 걸리고 가장 자주 실패하는 부분이다. 데이터셋이 수십 GB다.

**기본 경로는 HF Hub 경유**를 추천한다. 이유:

- 이미 구현돼 있다 — [datasets.py:168-189](../backend/app/routers/datasets.py#L168-L189)의
  `hf upload-large-folder`, 진행 로그도 WS로 나간다([ws.py:155-169](../backend/app/routers/ws.py#L155-L169))
- 프로바이더에 무관하다 (SSH든 HF Jobs든 원격에서 `repo_id`만 주면 끝)
- 재개 가능하고, 인스턴스를 새로 띄워도 재업로드가 없다
- private repo로 올리면 외부 노출도 막힌다

보조 경로로 SSH 직접 rsync(사내 서버·초대용량), 그리고 사내 정책상 HF 업로드가 막히면
S3/R2. 어느 쪽이든 필요한 것:

- **업로드 필요 여부 판정** — 로컬 데이터셋 해시(파일 목록+크기+mtime)를 캐시해서, 이미
  올라간 것과 같으면 건너뛴다. 매번 재업로드하면 학습보다 업로드가 오래 걸린다.
- **선(先)업로드 UI** — 예상 크기/시간 표시. `dataset_scanner._dir_size`
  ([dataset_scanner.py:18](../backend/app/services/dataset_scanner.py#L18)) 재사용.
- **업로드 실패 시 인스턴스를 띄우지 않는다** — 순서가 뒤집히면 빈 GPU가 과금된다.
  (업로드 → 검증 → provision → 학습 순서를 강제)

---

## 6. 체크포인트 회수

**학습이 끝나면 인스턴스는 사라진다.** 회수 실패 = 몇 시간 + 몇 달러 증발.

현재 코드는 정반대로 되어 있다 — [cli_mapping.py:197-200](../backend/app/core/cli_mapping.py#L197-L200)에서
`policy_repo_id`가 없으면 `--policy.push_to_hub=false`를 강제한다. 로컬에선 맞지만
클라우드에선 **회수 경로를 지우는 설정**이다. 러너별로 갈라야 한다.

- 클라우드 job은 `policy_repo_id`를 **필수**로 하거나(기본값 자동 생성:
  `{user}/{dataset}_{policy}_{날짜}`), 주기적 회수를 켠다
- `save_freq`마다 원격에서 체크포인트를 Hub에 올리는 래핑이 필요할 수 있다
  (LeRobot의 `push_to_hub`가 최종본만 올리는지 체크포인트마다인지 **확인 필요**).
  Vast.ai처럼 중단 위험이 높은 곳에서는 이게 필수다.
- 회수한 모델은 `settings.models_dir` 아래로 → `model_scanner`가 자동으로 잡는다
  → 기존 추론/모델 페이지가 손대지 않아도 동작한다
- `GET /api/training/checkpoints`([training.py:202-221](../backend/app/routers/training.py#L202-L221))는
  로컬 디렉토리를 훑는다. 클라우드 job은 원격 목록 또는 Hub 파일 목록을 반환하도록 러너에 위임

---

## 7. 비용 가드 — "돈 E-stop"

이 저장소는 이미 안전 정지를 **독립 watchdog 프로세스**로 만들어 뒀다. 클라우드는 같은
문제의 금전판이다: **웹서버가 죽어도 원격 인스턴스는 계속 과금된다.**

로컬에서만 관리하면 안 되고, 인스턴스 안에도 자폭 장치를 심어야 한다:

1. **`max_runtime` 강제 종료** — 인스턴스 부팅 스크립트에 `shutdown -h +N` 또는 감시 셸.
   서버·인터넷·사람이 전부 사라져도 N시간 뒤 종료된다. **1순위이자 가장 확실한 방어.**
2. **학습 종료 감지 후 자동 terminate** — 프로세스 exit → 아티팩트 푸시 완료 확인 → 종료
3. **예산 상한(USD)** — job 생성 시 입력. `rate × 경과시간`이 상한에 닿으면 정지
4. **heartbeat 자폭(옵션, 기본 off)** — 웹서버 heartbeat가 끊기면 종료. 다만 인터넷이
   잠깐 끊겼다고 6시간짜리 학습을 죽이면 손해라 기본은 끄고, 짧은 실험용으로만 켠다
5. **고아 인스턴스 스캐너** — `list_instances()`로 조회해 레지스트리에 없는 piper 태그
   인스턴스를 찾아내 UI에 경고. 종료 API가 실패했는데 성공으로 처리된 경우를 잡는다
6. **종료 확인** — `terminate()` 후 실제로 사라졌는지 재조회. 실패는 조용히 넘기지 말고
   빨간 배너로 띄운다

UI에는 항상 **현재 시간당 요금 / 누적 비용 / 예상 총액**을 띄운다.

> 확인 모달은 반드시 논블로킹 React 모달로 만든다. `window.confirm`은 이벤트 루프를 막아
> E-stop heartbeat를 끊고, 로컬에서 추론 중이면 2초 타임아웃으로 추론이 강제 종료된다.
> (같은 사고가 이미 한 번 있었다.)

---

## 8. 시크릿 관리

- 저장: `config_dir/cloud_credentials.json`, 파일 권한 0600. `.env`(gitignore됨)에
  `PIPER_*` 로 넣는 경로도 함께 지원 — 도커/CI에서 유용
- **프론트에 원문을 절대 반환하지 않는다.** `sk-****abcd` 형태 마스킹만
- **CLI 미리보기에 키가 새면 안 된다** — `/api/training/preview`
  ([training.py:181-199](../backend/app/routers/training.py#L181-L199))는 현재 커맨드 문자열을
  그대로 프론트에 준다. 클라우드 preview에서 토큰이 인자에 들어가면 화면·로그·복사버튼으로
  유출된다. **키는 커맨드가 아니라 원격 env/파일로만 전달**하고, preview는 마스킹한다
- 로그 브로드캐스트에도 마스킹 필터를 건다 (프로바이더 CLI가 키를 에코하는 경우가 있다)
- HF 토큰은 이미 쓰고 있으므로 그것과 저장 위치·마스킹 규칙을 통일

---

## 9. 가이드 페이지

요구사항: "가입하고 사용 설정하는 가이드". 죽은 문서가 아니라 **체크리스트로 동작하는 페이지**로
만드는 게 목표다.

### 데이터 소스는 백엔드 프로바이더 레지스트리

가이드 텍스트를 TSX에 하드코딩하면 프로바이더 정의가 프론트/백엔드 두 곳으로 갈라진다
(이 저장소가 refactor 목록에서 계속 정리하고 있는 바로 그 문제). 프로바이더 메타데이터를
백엔드 한 곳에 두고 `GET /api/cloud/providers`로 내려준다:

```python
PROVIDERS = {
  "vast": ProviderInfo(
    label="Vast.ai",
    console_url="https://cloud.vast.ai/",
    auth_fields=[AuthField("api_key", "API Key", secret=True)],
    signup_steps=[...],          # 단계별 텍스트 + 링크
    gpu_catalog=[...],
    caveats=["경매형이라 인스턴스가 중단될 수 있음 — 체크포인트 자동 푸시 권장"],
  ),
  ...
}
```

프론트는 `frontend/src/pages/CloudGuidePage.tsx` 하나에서 렌더만 하고,
[pages.ts](../frontend/src/config/pages.ts#L44)에 한 줄 추가하면 내비/라우트/카드가 따라온다.
(마크다운 렌더러가 의존성에 없다 — 구조화 JSON을 렌더하면 새 패키지가 필요 없다.)

### 페이지 구성

1. **비교 표** — 어디를 고를지부터. 가격/중단위험/난이도/추천 용도
2. **프로바이더별 아코디언**
   - 가입 → 결제수단 등록 → 키 발급 위치(스크린샷 대신 콘솔 딥링크)
   - 키 입력 폼 → **[연결 테스트]** 버튼: 키 유효성 · 잔액 · GPU 가용성 확인
   - **[예상 비용 계산]** — 데이터셋/스텝수 넣으면 대략 시간·금액
   - 주의사항 (중단, 최소 과금 단위, 리전, 데이터 반출)
3. **첫 학습 따라하기** — 데이터셋 업로드 → job 생성 → 모니터링 → 회수, 각 단계에
   현재 상태 체크마크
4. **문제 해결** — 인스턴스 안 뜸/OOM/업로드 실패/체크포인트 없음

외부 서비스 UI는 자주 바뀐다. 스크린샷은 넣지 말고 콘솔 링크 + 텍스트로 유지비를 낮춘다.

---

## 10. 학습 페이지 UI 변경

- **실행 위치 선택** — `로컬 GPU / 사내 서버 / Vast.ai / ...` 드롭다운. 선택에 따라
  GPU 종류·예산·이미지 필드가 나타난다
- **job 목록 패널** — 현재는 단일 학습 전제. 실행 중/완료 job 리스트에서 골라 보는 구조로
  ([TrainingPage.tsx](../frontend/src/pages/TrainingPage.tsx)는 718줄이므로 job 목록·상세는
  컴포넌트로 분리)
- **비용 배지** — 시간당/누적/예상
- **설정 저장 위치** — 지금은 localStorage(`piper_train_settings`)다. job은 서버 상태이므로
  레지스트리가 단일 소스이고, localStorage는 "다음에 만들 job의 폼 기본값"으로만 남긴다
- **`/cloud` 페이지** — 자격증명 관리 + 인스턴스 현황 + 고아 인스턴스 경고
- **`/cloud/guide` 페이지** — §9

---

## 11. 작업 분해

각 단계가 **독립적으로 배포 가능하고, 이전 단계의 동작을 바꾸지 않도록** 잘랐다.

| # | 작업 | 산출물 | 위험 | 선행 |
|---|---|---|---|---|
| 0 | 메트릭 파서·히스토리를 `training/metrics.py`로 분리 | ☑ 완료 | 낮음 | — |
| 1 | `TrainRunner` Protocol + `LocalRunner`로 현 동작 이식 | ☑ 완료 | 중 | 0 |
| 2 | `build_train_args`에서 인터프리터/파일시스템 의존 분리 | ☑ 완료 (**미리보기 부작용 버그도 수정**) | 중 | — |
| 3 | ☑ job 레지스트리 + 다중 job + WS `job_id` | 로컬도 job 목록으로 보임. **버스 위**(파일 아님)라 서버 재시작에도 남는다 | **높음** (WS 계약 변경) | 1 |
| 4 | ☑ [`SSHRunner`](../backend/app/services/training/runners/ssh.py) — 원격 tmux 세션 | 사내 서버로 학습. **실기 확인**: 300스텝 완주 · 재부착 시 로그 138줄 되읽기 | 높음 | 1,2,3 |
| 5 | 데이터셋 업로드 연동 + 사전 검증 | 원격이 데이터를 봄. **4 가 지금 가정하는 것이 이것이다** | 중 | 4 |
| 6 | 체크포인트 회수 + `models_dir` 안착 | 모델이 돌아옴 | 중 | 4 |
| 7 | 비용 가드 (max_runtime 자폭 · 예산 · 고아 스캐너) | 돈 안 샘 | 중 | 4 |
| 8 | 유료 프로바이더 1개 (Vast.ai 또는 RunPod) | 실제 클라우드 | 중 | 4~7 |
| 9 | 자격증명 저장/마스킹 + `/cloud` 페이지 | 키 관리 | 중 | 8 |
| 10 | 가이드 페이지 + 연결 테스트 | 온보딩 | 낮음 | 9 |
| 11 | 두 번째 프로바이더 (HF Jobs — 계층 B) | 추상화 검증 | 중 | 8 |
| 12 | 알림 (완료/실패/예산 초과) | 방치 가능 | 낮음 | 3 |

**0~2는 클라우드와 무관한 순수 리팩터**다. 여기까지는 지금 당장 해도 손해가 없고,
`refactor/` 목록의 문제의식과도 결이 같다. 4번(사내 SSH)까지 가면 과금 없이 전 구조를
검증할 수 있으므로, **8번(유료 프로바이더)은 그 뒤로 미루는 것이 맞다.**

---

## 12. 추가로 나올 일거리

설계에는 안 들어가지만 실제로 해야 하는 것들.

**데이터/보안**
- 사내 데이터셋을 외부로 반출하는 것 자체가 정책 검토 대상이다. private repo라도 "외부
  업로드"다 — 착수 전에 확인받을 것. 사내 SSH 러너만 쓰는 선택지가 답일 수도 있다
- 리전 선택 (데이터 소재지 요구가 있으면)
- 학습 종료 후 원격 데이터 삭제 여부

**정확성**
- lerobot 버전 고정 + job 레코드에 커밋 해시 기록 (§4)
- GPU별 batch size 권장값 — 24GB에서 맞춘 값을 80GB에 그대로 쓰면 GPU가 논다.
  반대로 80GB 설정을 24GB에 쓰면 OOM. 프로바이더 GPU 카탈로그에 권장 batch를 붙인다
- `state_dim`/`action_dim` 오버라이드는 원격에서 미지원 → **명시적 에러** (조용한 무시 금지)
- 로컬 학습과 클라우드 학습의 loss curve 비교 뷰 (환경 차이 검증용)

**운영**
- 학습 큐 — 실험 여러 개를 순차 실행 (GPU 1대를 예약해두고 3개 돌리는 편이 쌀 때가 있다)
- resume — 스팟 중단 후 마지막 체크포인트에서 이어서. `--resume=true`는
  `--policy.path`와 함께 못 쓴다([cli_mapping.py:207-209](../backend/app/core/cli_mapping.py#L207-L209))는
  기존 제약이 원격에서도 그대로 걸린다
- 완료/실패/예산초과 알림 (웹 푸시 · Slack · 이메일). 6시간짜리를 화면 보고 기다릴 수 없다
- wandb 연동 강화 — 이미 옵션이 있다. 클라우드에선 인스턴스가 사라져도 로그가 남는
  유일한 경로라 사실상 필수. 기본 on을 검토
- 오프라인/네트워크 단절 시 UI 동작 (job이 죽은 게 아니라 조회가 안 되는 것뿐임을 구분해
  표시. 여기서 착각하면 사용자가 멀쩡한 학습을 죽인다)
- 타임존 — 원격은 UTC, 로컬은 KST. 레코드는 tz-aware ISO로 저장하고 표시만 로컬

**테스트**
- `FakeRunner` — 프로바이더 API 없이 상태 전이·재연결·비용 로직을 테스트. 3번(레지스트리)
  단계에서 같이 만들어야 나중에 회귀 잡기가 가능하다
- 프로바이더 어댑터는 녹화된 응답(fixture)으로 테스트. 실제 API를 CI에서 때리지 않는다
- 킬 시나리오: 서버 강제 종료 → 재기동 → 재연결 되는가 / 인스턴스 강제 삭제 → 감지하는가

**문서**
- `CLAUDE.md`에 러너 개념과 새 환경변수 추가
- `docs/`에 운영 런북 (인스턴스가 안 죽을 때 수동 정리 절차)

---

## 13. 먼저 정해야 할 것

착수 전에 답이 필요하다. 답에 따라 범위가 크게 달라진다.

1. ~~**데이터 반출이 허용되는가?**~~ → **허용됨 (HF Hub 포함, 전 경로 가능).**
   0~12단계 전체가 범위 안이다. 다만 **순서는 안 바뀐다** — `static_ssh`를 먼저 만드는 이유는
   정책 회피가 아니라 *"과금 없이 러너 구조를 검증하는 무료 대조군"* 이기 때문이다.
   [../ROADMAP.md](../ROADMAP.md)의 "데이터 집계" 절도 함께 볼 것
2. **어느 프로바이더를 먼저?** 최저가(Vast.ai) vs 안정성(Lambda/RunPod) vs
   생태계 일치(HF Jobs). 사내가 이미 AWS/GCP를 쓰면 결제·보안 심사가 이미 끝나 있어
   그쪽이 실제로는 가장 빠를 수 있다
3. **동시 실행 job 수 상한** — 1개면 3번 단계가 크게 가벼워진다. 2개 이상이면
   레지스트리·WS 계약 변경이 불가피하다
4. **체크포인트 회수 기본값** — Hub 자동 푸시(외부 반출) vs 다운로드(느림)
5. **예산 상한 기본값** — job당 USD 얼마에서 자동 정지할 것인가

---

## 검증

- 프론트엔드 변경은 반드시 `cd frontend && npm run build`
  (`npx tsc --noEmit`은 루트 tsconfig가 참조 전용이라 no-op)
- 0~2단계는 **로컬 학습을 실제로 한 번 돌려** 메트릭 그래프·체크포인트 목록·중지가
  전부 이전과 같은지 확인
- 3단계는 서버 재시작 후 재연결과 job 목록 유지 확인
- 4단계 이후는 사내 SSH로 짧은 학습(steps=500)을 끝까지: 업로드 → 학습 → 회수 →
  회수된 체크포인트로 로컬 추론까지 한 바퀴
- 7단계는 **의도적으로 서버를 죽이고** 인스턴스가 스스로 종료되는지 확인.
  이걸 검증하지 않으면 가드가 있다고 믿을 수 없다

## 상태

◐ 0~2단계 완료 — 3단계(job 레지스트리)는 Redis 이후
