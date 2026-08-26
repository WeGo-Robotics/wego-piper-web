# ACT-Aux — 행동 + 작업 단계(stage)를 함께 내는 ACT 변형

> **◐ 1~2단계 완료** (패키지·yaml·굽기·학습 실행 확인). 남은 것: wrapper 텔레메트리(4), 워밍스타트·보정 도구(5), A/B(3).
>
> 기존 ACT([lerobot/policies/act](https://github.com/huggingface/lerobot/tree/main/src/lerobot/policies/act))는
> **한 줄도 건드리지 않는다.** 별도 설치 패키지 `act_aux/` 에 `act_aux` 라는 새 정책 타입으로 둔다.
> 골격은 **실측 검증했다** (§2.3, §4.1) — 플러그인 등록 → `--policy.type=act_aux` 파싱 →
> 학습 forward(aux 손실 포함) → 추론 + stage 출력 → `type: act_aux` 저장/재로드까지 통과.
>
> 배경 검토는 이 문서 착수 전 대화에 있다. 요지: 보조 헤드 제안 자체는 타당하고,
> [01-phase-annotation.md](01-phase-annotation.md) 의 FSM 라벨이 곧 학습 라벨이라 공짜다.
> 다만 제안서의 `h = act_encoder(images, qpos)` 는 실제 ACT 에 없는 단일 벡터이고(§3.1),
> progress/success/failure 헤드는 라벨이 없어 이번 범위 밖이다(§9).

---

## 0. 요약

| 구성 | 내용 |
|---|---|
| 정책 타입 | `act_aux` — `ACTConfig`/`ACTPolicy` **상속**. ACT 는 그대로, 변형은 새 패키지 |
| 위치 | `act_aux/` (배포명 **`lerobot_policy_act_aux`**). `bus/`·`phase/` 와 같은 설치 패키지 |
| 왜 그 이름인가 | LeRobot 이 `lerobot_policy_*` 배포를 **자동 import** 한다 — 포크·몽키패치 없이 꽂힌다 (§2) |
| 모델 | 트랜스포머 인코더 출력에 **forward hook** 으로 접근 → 풀링 → stage 헤드. ACT forward 복사 없음 (§3) |
| 출력 | `action chunk` + `stage`(7클래스 로짓). progress 는 **안 한다** (§9) |
| 라벨 | `meta/phase_labels.json` 세그먼트 → LeRobot **subtask**(`subtask_index` 컬럼 + `meta/subtasks.parquet`)로 **굽기** (§4). 임의 컬럼은 전처리가 버린다 |
| 학습 | `build_train_args` 변경 없음 — `policies/act_aux.yaml` 한 장이면 화면·CLI 가 붙는다 (§5) |
| 추론 | wrapper 에 `last_aux` 읽는 guarded 10줄 → 텔레메트리 `stage` (§6) |
| 성공 기준 | 같은 seed 바닐라 ACT 대비 **action L1 악화 없음** + 홀드아웃 에피소드 stage 정확도 (§8) |

---

## 1. 어디에 두나 — 결정과 기각

**`act_aux/` 최상위 설치 패키지, 배포명 `lerobot_policy_act_aux`.**

```
act_aux/
├── pyproject.toml                 # name = "lerobot_policy_act_aux"  ← 접두사가 규약이다
└── lerobot_policy_act_aux/
    ├── __init__.py                # config import (= 등록). 이것만으로 플러그인이 완성된다
    ├── configuration_act_aux.py   # ActAuxConfig(ACTConfig)  @register_subclass("act_aux")
    ├── modeling_act_aux.py        # ActAuxPolicy(ACTPolicy)
    ├── processor_act_aux.py       # make_act_aux_pre_post_processors → ACT 것 그대로 위임
    └── bake.py                    # phase_labels.json → task_stage 컬럼 굽기 (§4)
```

기각한 자리 셋:

| 자리 | 왜 아닌가 |
|---|---|
| `vendor/` | 외부 저장소 **스냅샷** 전용이다 ([vendor/README.md](../vendor/README.md)). 우리가 쓰는 코드는 `bus/`·`phase/`·`robot/` 처럼 최상위 패키지다 |
| `wrapper/` | 학습은 `python -m lerobot.scripts.lerobot_train` 별도 프로세스([cli_mapping.py:329](../backend/app/core/cli_mapping.py#L329))라 `wrapper/` 를 import 할 수 없다. 학습·추론이 **같은 코드**를 써야 하므로 설치 패키지여야 한다 — `piper_phase` 가 같은 이유로 패키지가 됐다 |
| lerobot 포크 / `lerobot_bootstrap` 몽키패치 | LeRobot 을 올릴 때마다 따라 고쳐야 한다. 플러그인 규약이 있는데 쓸 이유가 없다 |

---

## 2. LeRobot 0.5.0 의 접점 (실측)

### 2.1 자동 탐색

[lerobot_train.py:549-551](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/scripts/lerobot_train.py#L549-L551):
`main()` 이 **draccus 파싱보다 먼저** `register_third_party_plugins()` 를 부른다.
이 함수는 설치된 배포 중 이름이 `lerobot_policy_` 로 시작하는 것을 전부 import 한다
([import_utils.py:146-174](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/utils/import_utils.py#L146-L174)).
import 만 되면 `@PreTrainedConfig.register_subclass("act_aux")` 가 실행돼 `--policy.type=act_aux` 가 풀린다.

### 2.2 이름 규약 — 반드시 지킨다

팩토리의 if/elif 체인에 없는 타입은 폴백으로 **이름에서 클래스를 유도**한다
([factory.py:531-562](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/policies/factory.py#L531-L562), [:565-590](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/policies/factory.py#L565-L590)):

| 것 | 규칙 | 우리 값 |
|---|---|---|
| config 클래스 | `…Config` 로 끝나야 한다 | `ActAuxConfig` |
| 정책 클래스 | `Config` → `Policy` 치환 | `ActAuxPolicy` |
| 모델 모듈 | `configuration_` → `modeling_` 치환 | `modeling_act_aux` |
| 프로세서 모듈·함수 | `configuration_` → `processor_`, `make_{type}_pre_post_processors` | `processor_act_aux.make_act_aux_pre_post_processors` |

이름 하나 틀리면 "Policy type 'act_aux' is not available" 로 죽는다. 테스트로 박는다 (§7).

### 2.3 스크래치 검증 결과

위 구조 그대로 최소 구현을 만들어 돌렸다 (ACT 원본 무수정, `pip install` 없이 `sys.path` 만):

```
choices has act_aux: True
policy cls: <class 'lerobot_policy_act_aux.modeling_act_aux.ActAuxPolicy'>
parsed: ActAuxConfig act_aux 0.2          ← --policy.type=act_aux --policy.stage_loss_weight=0.2
processors ok: DataProcessorPipeline      ← ACT 프로세서 위임
train loss: 79.54 {'l1_loss': 0.86, 'kld_loss': 7.83, 'stage_ce': 1.94, 'stage_acc': 0.0}
chunk: (2, 20, 7) aux: 6 0.173            ← predict_action_chunk 그대로 + last_aux
saved type: act_aux / reload ok: ActAuxPolicy
```

(무작위 초기화라 KL 이 손실을 지배한다 — `kl_weight=10` 의 정상 동작이고 stage 와 무관하다.)

---

## 3. 모델

### 3.1 어디서 특징을 꺼내나 — forward hook

ACT 에는 "현재 관측 벡터 `h`" 가 없다. 트랜스포머 인코더 출력 `encoder_out` 은
`(S, B, 512)` 토큰 시퀀스 — `[latent, robot_state, 카메라별 15×20 특징 토큰 …]` 이다
([modeling_act.py:459-491](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/policies/act/modeling_act.py#L459-L491)).
그리고 `ACT.forward` 는 이걸 밖으로 내주지 않는다.

꺼내는 방법 둘:

| 방법 | 장점 | 단점 |
|---|---|---|
| **`self.model.encoder` 에 forward hook** | ACT 코드 **0줄 복사**. 상류가 바뀌어도 `encoder` 모듈이 있는 한 산다 | CLS 토큰을 못 넣는다 (입력을 못 바꾸므로) |
| `ACT.forward` 130줄 복사 + CLS 토큰 | 문헌 그대로 | 상류 드리프트. `gen_policy_spec` 식 대조 테스트가 또 필요 |

**hook 으로 간다.** 풀링은 config `pool` 로 고른다 — `mean`(전 토큰 평균, 기본) /
`state`(robot_state 토큰 = `encoder_out[1]`). CLS 토큰은 §8 A/B 에서 `mean` 이 부족할 때만 2단계로.

⚠ **VAE 인코더(`vae_encoder`)에 붙이면 안 된다.** 학습 때는 정답 액션을 보고 만들어지고
추론 때는 `latent_sample = zeros` 다 ([:452](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/policies/act/modeling_act.py#L452)). 제안서의 "ACT latent 512" 는 `dim_model`(512)이지 `latent_dim`(32)이 아니다.

### 3.2 헤드·손실

```python
class ActAuxPolicy(ACTPolicy):
    config_class = ActAuxConfig
    name = "act_aux"

    def __init__(self, config, **kw):
        super().__init__(config, **kw)                       # ACT 전부 그대로
        d = config.dim_model
        self.stage_head = nn.Sequential(nn.Linear(d, 256), nn.ReLU(), nn.Dropout(0.1),
                                        nn.Linear(256, config.n_stages))
        self.model.encoder.register_forward_hook(lambda m, i, o: setattr(self, "_enc_out", o))
        self.last_aux = None                                 # wrapper 가 읽는다 (§6)

    def _pooled(self):                                       # (S,B,D) → (B,D)
        e = self._enc_out
        return e.mean(0) if self.config.pool == "mean" else e[1]

    def forward(self, batch):
        loss, ld = super().forward(batch)                    # l1 + kl_weight·kld
        tgt = self._targets(batch["subtask"])               # 문자열 → stage_names 인덱스, 모르면 -1
        logits = self.stage_head(self._pooled())
        ce = F.cross_entropy(logits, tgt, weight=self._class_w, ignore_index=-1)
        ld["stage_ce"] = ce.item()
        ld["stage_acc"] = (logits.argmax(-1) == tgt).float().mean().item()
        return loss + self.config.stage_loss_weight * ce, ld

    @torch.no_grad()
    def predict_action_chunk(self, batch):
        actions = super().predict_action_chunk(batch)        # 시그니처 불변 → wrapper 무수정
        p = self.stage_head(self._pooled()).softmax(-1)[0]
        self.last_aux = {"stage": int(p.argmax()), "stage_p": float(p.max()), "probs": p.tolist(),
                         **self._confidence(p)}                   # margin/entropy/mc_std — §3.4
        return actions
```

config 필드 (전부 `ACTConfig` 위에 추가, 기본값 필수 — draccus):

| 필드 | 기본 | 뜻 |
|---|---|---|
| `stage_names` | `PHASE_NAMES` 사본 | 클래스 = subtask 이름 순서. 체크포인트 config 에 저장되므로 추론이 piper_phase 없이 이름을 안다. `backend/tests/test_act_aux_contract.py` 가 piper_phase 와 대조 |
| `stage_loss_weight` | 0.1 | λ. **0 이면 바닐라 ACT 와 수치 동일** — A/B 대조군 |
| `stage_balance` | True | 학습 중 본 라벨 빈도로 √역빈도 가중 자동 계산 (§4.3) |
| `stage_class_weights` | None | 주면 자동 계산보다 우선 |
| `pool` | `"mean"` | `mean` / `state` |
| `temperature` | 1.0 | 사후 보정 스칼라 T (§3.4). 보정 도구가 `meta/act_aux.json` 에 쓰고 학습/추론이 읽는다 |
| `mc_samples` | 0 | 0 이면 끔. N>0 이면 헤드만 N회 dropout 재실행해 분산을 낸다 (§3.4) |
| `label_smoothing` | 0.1 | CE 의 확률 포화를 막는다 — §3.4 의 값들이 매끄러워진다 |

`forward` 에서 배치에 `subtask` 가 없으면 **KeyError 로 죽인다.** 조용히 건너뛰면
"act_aux 로 학습했는데 stage 가 항상 6" 같은 사고가 된다. 바닐라로 학습하려면 `policy.type=act` 를 쓴다.
타깃은 `batch["subtask"]`(문자열 목록)를 `stage_names` 인덱스로 바꾼 것이고, 목록에 없는 이름(`_unlabeled`)은 -1 로 무시한다.

### 3.3 손실 크기 감각

ACT 손실은 정규화 액션 L1(수렴 후 0.05~0.3) + `kl_weight=10`×KLD. 7클래스 CE 는 시작 1.95, 수렴 후 0.1~0.3.
λ=0.1 이면 CE 항이 L1 과 같은 자릿수라 출발점으로 무난하지만 **근거는 A/B 뿐이다** (§8).
`loss_dict` 에 `stage_ce`/`stage_acc` 를 따로 넣어 학습 화면에서 L1 과 분리해 본다 (§5.3).

---

### 3.4 confidence — YOLO 의 conf 와 같은 것, 그리고 그보다 나은 것

YOLO 의 confidence(objectness × class prob)처럼 stage 헤드도 `softmax.max()` 를 `stage_p` 로 낸다.
둘 다 **보정되지 않은** 확률이다 — CE 가 확률을 1.0 으로 밀어붙이므로 학습에 없던 장면
(조명·물체 위치 이탈)에서도 0.95 를 낸다. "0.91 이니 믿자" 는 성립하지 않는다.
바닐라 ACT 는 이런 값이 아예 없다 (VAE 분산은 학습 전용, 추론 땐 latent=0).

`last_aux` 에 같이 내는 값 — 전부 인코더 1회 위에서 계산되므로 추론 비용이 안 는다:

| 키 | 계산 | 뜻 | 비용 |
|---|---|---|---|
| `stage_p` | `max(p)` | YOLO conf 상당 | 0 |
| `margin` | `p[top1] − p[top2]` | 두 단계 **사이**에서 애매함. 임계치엔 `stage_p` 보다 낫다 | 0 |
| `entropy` | `−Σ p log p` | 어느 단계로도 안 보임 | 0 |
| `mc_std` | 헤드만 N회 dropout 켜고 재실행, top1 확률의 표준편차 | 모델이 **모르는 장면**(epistemic). softmax 가 못 잡는 OOD 를 잡는다. 인코더는 1회, 헤드는 2층이라 N=20 도 공짜 | `mc_samples>0` 일 때만 |

그리고 **temperature scaling**: 홀드아웃 에피소드(§8)에서 스칼라 T 하나를 NLL 최소화로 피팅해
`softmax(logits / T)` 로 낸다. "0.8 이면 80% 쯤 맞는다" 를 성립시키는 표준 사후 보정이다.
`act_aux/tools/calibrate.py <체크포인트> <데이터셋>` 이 T 와 reliability diagram(ECE) 을 `meta/act_aux.json` 에 쓴다.
학습 중엔 `label_smoothing=0.1` 로 포화를 미리 막는다.

해석 주의 둘:

1. **단계 분류의 확신이지, 행동이 맞다거나 성공한다는 확신이 아니다.** success 헤드(§9)와 다른 것이다.
2. **세그먼트 경계에서 낮은 건 정상이고 맞는 동작이다.** FSM 라벨이 전이 구간에서 히스테리시스로
   몇 프레임 밀려 있으니 APPROACH→ALIGN 경계의 0.55 는 모델이 틀린 게 아니라 라벨이 본질적으로 애매한 것이다.

그래서 소비자(오케스트레이터·UI) 쪽 규칙은 히스테리시스다 — **`stage_p > 0.8 and margin > 0.5` 일 때만 stage 갱신, 아니면 직전 stage 유지.**
`mc_std` 가 크면 "장면 자체를 모른다" 로 따로 표시한다. 임계값은 §8 의 reliability diagram 을 보고 정하지 손으로 안 적는다.

---

## 4. 데이터 — `task_stage` 컬럼 굽기

### 4.1 왜 사이드카를 직접 못 읽나 — 그리고 왜 임의 컬럼도 안 되나

`LeRobotDataset.__getitem__` 은 parquet 컬럼을 전부 배치에 넣지만
([lerobot_dataset.py:1085](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/datasets/lerobot_dataset.py#L1085)),
**`meta/info.json` features 에 선언된 컬럼만** HF 스키마에 산다. 그리고 `lerobot-train` 이
데이터셋을 직접 만들므로 로더에 끼어들 자리가 없다. → 새 데이터셋으로 굽는다.

**실측으로 하나 더 드러났다:** 로더를 통과해도 학습 전처리 파이프라인이 배치를 transition 으로
바꿀 때 **화이트리스트**만 남긴다 — `*_is_pad`, `task`, `subtask`, `index`, `task_index`, `episode_index`
([converters.py:157-175](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/processor/converters.py#L157-L175)).
처음 만든 `task_stage` 컬럼은 굽기 검증(로더)은 통과했지만 정책 forward 에서 KeyError 로 죽었다.

그래서 **LeRobot `subtask`** 를 쓴다. v3 의 정식 프레임별 하위작업 개념이다: `subtask_index` int64
컬럼 + `meta/subtasks.parquet`(이름↔번호) → 로더가 배치에 `subtask`(문자열)를 넣어주고, 화이트리스트를
통과하며, `dataset_to_policy_features` 가 건너뛰어 **정책 입력 feature 에 섞이지 않는다**
(`observation.*` 로 이름 붙였다면 STATE 로 잡혀 정규화되고 추론 관측에도 요구됐을 것이다).
몽키패치 없이 되는 유일한 경로였다.

### 4.2 [01-phase-annotation §5](01-phase-annotation.md) 굽기의 **경량 변형**

| | §5 (8번째 슬롯) | 이 문서 |
|---|---|---|
| 바꾸는 것 | `observation.state` `[7]→[8]` + stats 전부 | **`subtask_index: int64 [1]` 컬럼 + `meta/subtasks.parquet`**. `observation.state` 불변 |
| refactor #8 의존 | 있음 ("7 이 박힌 자리") | **없음** — 관측 차원이 안 바뀐다 |
| 추론 측 공급자 | 온라인 FSM 필수 (§6.1) | **불필요** — 출력이지 입력이 아니다 |
| 이름 | `{name}_phase` | `{name}_stage` |

같은 코드 경로(`piper_phase.labeler` 의 사이드카 읽기, 비디오 하드링크, 전용 ProcessManager)를
그대로 쓰되 변환 함수만 다르다. 나중에 §5 굽기를 만들 때 **둘을 옵션 하나로 합친다**
(`--state-slot` / `--stage-column`) — 지금은 컬럼 추가만.

굽기 규칙 (`python -m lerobot_policy_act_aux.bake <org/name | 경로> [--reviewed-only] [--force]`):
- 프레임 값 = `segments` 에서 `[start, end]` 포함 구간의 코드. 세그먼트 밖 프레임과 `--reviewed-only` 로
  제외한 에피소드는 **`_unlabeled`** subtask(마지막 행). `-1` 은 쓰지 않는다 — 로더가 `iloc[-1]` 로
  마지막 이름을 조용히 돌려준다. 정책은 `stage_names` 에 없는 이름을 -1 로 바꿔 무시한다.
- `meta/info.json` `features.subtask_index = {dtype: int64, shape: [1]}`; `meta/subtasks.parquet` 는
  `tasks.parquet` 와 같은 꼴(index=이름, 컬럼=`subtask_index`).
- data parquet 의 `huggingface` 스키마 메타데이터도 같이 고친다 — 안 고치면 로더가 옛 feature 목록으로 읽는다.
- `meta/stats.json`·`meta/episodes/*.parquet` 에 `subtask_index` 통계 추가 (정규화엔 안 쓰이지만 features↔stats 키를 어긋나게 두지 않는다).
- 비디오는 하드링크(→심링크→복사), `images/`(녹화 캐시) 는 건너뛴다. 원본 `phase_labels.json` 을 함께 복사하고
  `meta/act_aux.json` 에 stage 이름·클래스 빈도·권장 가중을 남긴다.
- 끝에 `LeRobotDataset` 으로 실제 열어 `subtask` 가 나오는지 본다. 여기서 안 열리면 학습에서 안 열린다.
- 홀드아웃은 굽기가 아니라 학습 `--dataset.episodes=[...]` 로.

실측(`min_cube_071410` → `_stage`, 39MB): IDLE 761 · APPROACH 11229 · ALIGN 1656 · GRASP 2260 · HOLD 12690 · RELEASE 2197 · DONE 556, unlabeled 0.

### 4.3 라벨 분포 — 가중 CE 가 필요하다

위 실측: HOLD·APPROACH 가 76%, IDLE·DONE 은 각 2%. `stage_balance=True`(기본)면 정책이 **학습 중 본 라벨
빈도**(`stage_counts` 버퍼, 체크포인트에 저장)로 √역빈도·평균 1 가중을 워밍업(`stage_balance_warmup` 배치) 뒤
자동 계산한다. 정책은 데이터셋 메타를 읽을 수 없으므로 굽기 시점이 아니라 학습 중에 센다.
`stage_class_weights` 를 주면 그 값이 우선. 굽기가 `meta/act_aux.json` 에 같은 식의 권장값을 남긴다.

### 4.4 DONE 은 남긴다

FSM 은 온라인에서 `DONE` 을 못 낸다(미래 참조, §3.4). 출력 헤드는 **영상만 보고** DONE 을 낼 수 있다 —
[episode-orchestrator](episode-orchestrator.md) 가 원하는 "미션 완료" 신호다. 7클래스 그대로.

---

### 4.5 수집·에디터와의 관계 — 수집은 그대로, 에디터엔 셋 ☑

녹화는 바꿀 게 없다. 라벨은 녹화 **뒤** `observation.state`/`action` 에서 FSM 으로 뽑는다.

```
녹화 → 에디터: 페이즈 분석 → 구간 검토·수정(저장 시 reviewed=true) → [ACT-Aux용 굽기] → 학습(act_aux)
```

에디터 쪽에 넣은 것:

| | 왜 | 어디 |
|---|---|---|
| **구운 사본 표시 + 읽기 전용** | 사본에서 구간을 고치면 원본엔 반영 안 되고 다음 bake 에 조용히 덮인다 | 스캐너 `baked_info()` 가 `meta/act_aux.json` 을 보고 `baked` 를 실어준다. 에디터는 목록에 "· 구운 사본", 분석/편집 막음 |
| **재굽기 배지** | 원본에서 에피소드를 고치거나 지운 뒤 `_stage` 가 옛 스냅샷인 걸 잊는다 | bake 가 원본 `phase_labels.json` 의 sha256 을 기록, 스캐너가 지금 해시와 비교 → `stale` |
| **굽기 버튼** | 분석·검토는 화면인데 마지막 한 단계만 터미널이면 안 쓴다 | `POST /api/phase/{id}/bake` — `settings.grpc_python -m lerobot_policy_act_aux.bake` subprocess (백엔드는 lerobot 을 import 하지 않는다). 검토 안 된 에피소드가 있으면 `--reviewed-only` 를 물어본다 |
| **학습 화면 필터** | act_aux + 원본을 고르면 첫 배치에서 죽는다 | `policies/act_aux.yaml` `train.requires_features: [subtask_index]` → 화면이 그 feature 없는 데이터셋을 숨긴다. 정책 이름은 TSX 에 안 적는다 |

## 5. 학습 경로

### 5.1 코드 변경 없음

`build_train_args` 는 `--policy.type=act_aux` 와 `--policy.<k>=<v>` 를 이미 만든다
([cli_mapping.py:361-367](../backend/app/core/cli_mapping.py#L361-L367)). 학습 화면의 필드는
`policies/act_aux.yaml` `train.fields` 가 정한다 ([policy-ui-spec](policy-ui-spec.md)).

### 5.2 설치 요건 — 세 곳

| 환경 | 조치 |
|---|---|
| 로컬 `settings.grpc_python` | `pip install -e act_aux/` ☑ |
| 컨테이너 | [backend/Dockerfile](../backend/Dockerfile) 의 `phase/` 옆에 `act_aux/` ☑ |
| 원격 학습(SSH 러너) | [cloud-training](cloud-training.md) 의 환경 준비 절차에 추가. **빠지면 원격에서만 "not available" 로 죽는다** — 러너가 시작 전 `python -c "import lerobot_policy_act_aux"` 로 확인 |

### 5.3 메트릭

lerobot-train 은 `loss_dict` 를 **wandb 로만** 보내고 로그 줄(`step: … loss: …`)에는 안 찍는다. 그래서 정책이
`stage_log_freq`(기본 200) forward 마다 `act_aux stage_ce:0.412 stage_acc:0.812` 한 줄을 직접 찍는다.
[training/metrics.py](../backend/app/services/training/metrics.py) 가 이 줄을 줍는 것은 4단계에서.

### 5.4 바닐라 ACT 체크포인트에서 시작

`--policy.path=<act 체크포인트>` 는 config `type: act` 라 `ActAuxPolicy` 로 안 열린다.
`act_aux/tools/from_act.py`: `config.json` 의 `type` 을 `act_aux` 로 바꾸고 새 필드를 기본값으로 채워
새 폴더에 쓴다. 가중치는 그대로 — `from_pretrained(strict=False)` 가 기본이라
([pretrained.py:87](file:///home/sw-han/miniconda3/lib/python3.13/site-packages/lerobot/policies/pretrained.py#L87))
헤드만 무작위로 시작한다. 2단계.

---

## 6. 추론 경로

### 6.1 wrapper — guarded 10줄

클래스 로드는 [policies/act_aux.yaml](../policies/act.yaml) 의 `runtime.model` 로 이미 된다
(`POLICY_IMPORTS` 가 yaml 에서 온다, [lerobot_wrapper.py:242-250](../wrapper/lerobot_wrapper.py#L242-L250)).
`lerobot_bootstrap` 의 `_CONFIG_IMPORTS` 도 yaml 에서 오므로 config 등록도 따라온다.

바꿀 곳은 [lerobot_wrapper.py:533](../wrapper/lerobot_wrapper.py#L533) 직후 하나:

```python
action_chunk = policy.predict_action_chunk(observation)
aux = getattr(policy, "last_aux", None)          # act_aux 가 아니면 None
if aux is not None:
    with _action_lock:
        _latest_aux = aux
```

텔레메트리([:703-716](../wrapper/lerobot_wrapper.py#L703-L716))에 `"stage": PHASE_NAMES[idx], "stage_p": p` 추가.
이름 테이블은 체크포인트 옆 `act_aux.json`(굽기가 쓴 것을 학습이 복사) → 없으면 `piper_phase.PHASE_NAMES`.
gRPC 경로([grpc_wrapper.py](../wrapper/grpc_wrapper.py))는 정책 서버가 액션만 돌려주므로 **이번엔 제외** — 로컬 추론만.

### 6.2 갱신 주기 — 프레임마다가 아니다

wrapper 는 큐가 `refill_threshold_pct` 까지 줄 때만 추론한다
([:661-665](../wrapper/lerobot_wrapper.py#L661-L665)). stage 는 **청크당 한 번**(15fps·apc 20~50 → 1~3초) 갱신된다.
상위 FSM/오케스트레이터엔 충분하다. 제안서의 "프레임별 출렁임" 은 이 구조에선 생기지 않고, 반대로 **stale** 이 문제다.
매 프레임이 필요해지면 백본×2캠을 매 프레임 돌리는 비용이 붙는다 — 그때 별도 결정.
`temporal_ensemble_coeff` 모드에서는 매 스텝 추론이라 프레임별로 나온다.

### 6.3 UI·버스

- InferencePage 텔레메트리 카드에 `stage / p` 한 줄 (ROADMAP 의 "페이즈 텔레메트리" 자리).
- 오케스트레이터·외부 API 가 읽을 버스 키는 [episode-orchestrator](episode-orchestrator.md) 가 정한 뒤 — 여기서 정하지 않는다.

---

## 7. 저장소 배선

새로 만드는 것:

| 파일 | 내용 |
|---|---|
| `act_aux/pyproject.toml`, `act_aux/lerobot_policy_act_aux/*` | §1 |
| `act_aux/lerobot_policy_act_aux/bake.py` + `python -m lerobot_policy_act_aux.bake <ds>` | §4 ☑ |
| `act_aux/tools/calibrate.py` | §3.4 — T 피팅 + ECE, `meta/act_aux.json` 갱신 |
| `policies/act_aux.yaml` | `type: act_aux`, `runtime.model: [lerobot_policy_act_aux.modeling_act_aux, ActAuxPolicy]`, `train.fields` = ACT 것 + `stage_loss_weight`·`pool`, `encoder_probe: false`(1단계), `language: false` |
| `act_aux/tests/test_act_aux.py` ☑ | 이름 규약(§2.2), `stage_loss_weight=0` 이면 바닐라와 손실 동일, 배치에 `subtask` 없으면 죽는지, 추론 `last_aux`, 저장/재로드 |
| `backend/tests/test_act_aux_contract.py` ☑ | `DEFAULT_STAGE_NAMES` == `piper_phase.PHASE_NAMES`, yaml runtime 경로 import, 목록 위치 |

고치는 것:

| 파일 | 변경 |
|---|---|
| [lerobot_wrapper.py](../wrapper/lerobot_wrapper.py) | §6.1 |
| [training/metrics.py](../backend/app/services/training/metrics.py) | §5.3 |
| [backend/Dockerfile](../backend/Dockerfile) | §5.2 |
| [test_policy_schema_sync.py](../backend/tests/test_policy_schema_sync.py), [test_policy_spec.py](../backend/tests/test_policy_spec.py) ☑ | 지원 목록 골든에 `act_aux` 추가 (의도적으로 막고 있던 테스트) |
| `tools/gen_policy_spec.py` → `act_aux.yaml` `default` 채우기 | config 클래스에서 읽는다. 백엔드 파이썬에도 패키지가 있어야 `test_defaults_match_lerobot` 이 본다 |
| [feature/README.md](README.md), [ROADMAP.md](../ROADMAP.md) | 항목 추가 |

---

## 8. 검증 — "도움이 된다" 는 가설이다

같은 데이터셋(`min_cube_071410_stage`)·같은 seed·같은 스텝으로 셋:

| 런 | 타입 | λ | 보는 것 |
|---|---|---|---|
| A | `act` | — | 기준 |
| B | `act_aux` | 0 | A 와 **수치 동일**해야 한다 (배선 검증) |
| C | `act_aux` | 0.1 | `l1_loss` 곡선 A 대비, 홀드아웃 stage 정확도 |

- 홀드아웃으로 `calibrate.py` 를 돌려 **reliability diagram(ECE)** 을 본다 — `stage_p` 가 적중률과 맞는지, §3.4 의 임계값(0.8/0.5)이 이 데이터에서 맞는지는 여기서 정한다.
- 홀드아웃은 **에피소드 단위**로 뗀다(프레임 단위면 인접 프레임이 새서 정확도가 부풀려진다). 굽기 옵션 `--holdout-episodes`.
- 실기: 10 에피소드씩 성공률 A vs C. 텔레메트리 stage 를 사람이 본 단계와 대조.
- C 의 L1 이 A 보다 나쁘면 λ 를 0.03 으로, 그래도 나쁘면 "representation regularization" 은 이 태스크에선 기각 — stage 헤드는 **감시 출력으로만** 가치가 있다고 결론짓고 λ 를 작게 고정한다.

---

## 9. 하지 않는 것

| 항목 | 이유 |
|---|---|
| `progress` 0~1 헤드 | 사이클 3회짜리 태스크에서 단조 진행도는 결국 세그먼트 내 시간 보간 — 제안서가 경고한 `frame/length` 와 같다. "완료 사이클 수" 정수가 정직한 대안이며 필요할 때 클래스 하나로 추가 |
| success / failure 헤드 | 데모 50개가 전부 성공. 라벨이 없다. FSM 의 `GRASP→APPROACH`(빈손)가 유일한 실패 흔적 |
| stage 를 관측에 되먹임 | [§2.5 규칙 ④](01-phase-annotation.md) — 되먹임 오류에 회복 경로가 없다. 입력 슬롯(§2)과 출력 헤드(이 문서)는 **역할이 다르다**: 정책이 행동하는 데 쓰는 phase = 입력(온라인 FSM 공급), 상위가 감시하는 데 쓰는 phase = 출력 |
| CLS 토큰 | ACT forward 복사가 필요. §8 에서 `mean`/`state` 풀링이 부족할 때만 |
| 프레임별 stage | §6.2 |
| gRPC 정책 서버 경로 | 프로토콜이 액션만 나른다. 로컬 추론에서 가치 확인 뒤 |
| 엔코더 프로브 tap | ACT 와 같은 백본이라 같은 tap 이 되지만, [encoder_probe.py](../wrapper/encoder_probe.py) 가 지금 수정 중이라 끝난 뒤 |

---

## 10. 순서

| 단계 | 내용 | 검증 |
|---|---|---|
| 1 ☑ | `act_aux/` 패키지 + `act_aux.yaml` + 테스트. 학습 목록에 뜨고 `stage_loss_weight=0` 으로 바닐라와 동일 | `test_act_aux` ☑, 런 B |
| 2 ☑ | `bake.py` — `min_cube_071410` → `_stage` | 굽기 + 로더 검증 ☑, `lerobot-train` 30스텝 실행 ☑ (`--policy.type=act_aux`, 체크포인트 `type: act_aux`) |
| 3 | 런 A/B/C | §8 |
| 4 | wrapper 텔레메트리 + InferencePage 한 줄 | 실기에서 stage 가 눈으로 본 단계와 맞는가 |
| 5 | `from_act.py`(워밍스타트), Dockerfile·원격 러너 설치 | 컨테이너·원격에서 런 C 재현 |

1·2 는 독립이라 병렬 가능. 4 는 3 의 결과와 무관하게 진행해도 된다 (출력 배선은 λ 와 무관).

---

## 11. 위험

| 위험 | 대응 |
|---|---|
| 상류 ACT 가 `self.model.encoder` 이름을 바꾼다 | `__init__` 에서 `hasattr` 검사 후 명확한 에러. lerobot 은 0.5.0 으로 고정돼 있다([Dockerfile:50](../backend/Dockerfile#L50)) |
| draccus 가 `ACTConfig` 서브클래스 등록을 거부 | §2.3 에서 통과 확인 — `get_known_choices()` 에 `act_aux` 포함 |
| 원격 학습 머신에 패키지 없음 | §5.2 사전 검사 |
| `task_stage` stats 누락으로 로더가 죽음 | §4.2 — 굽기 테스트가 `LeRobotDataset` 로드까지 한다 |
| 굽기 라벨의 `n_stages` 와 config 불일치 | `meta/act_aux.json` ↔ config 대조, 학습 시작 시 assert |
| `_enc_out` 이 stale (hook 이 안 돈 경로에서 헤드 호출) | `predict_action_chunk`/`forward` 안에서만 읽는다. 외부에서 `_pooled()` 호출 금지 |
