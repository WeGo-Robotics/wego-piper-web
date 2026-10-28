# custom_policies — Piper 커스텀 정책

LeRobot 정책을 **상속**해 우리가 고친 정책들. 상류(`lerobot.policies.*`)는 건드리지 않고, LeRobot 의
서드파티 플러그인 규약(배포명 `lerobot_policy_*` → `lerobot-train` 이 자동 import)으로 꽂는다.

| 타입 | 화면 이름 | 바탕 | 문서 |
|---|---|---|---|
| `act_s` | ACT-S | ACT + stage 헤드 | [feature/act-s.md](../feature/act-s.md) |
| `smolvla_s` | SmolVLA-S | SmolVLA + stage 헤드 | [feature/smolvla-s.md](../feature/smolvla-s.md) |
| `act_dinov3` | ACT-DINOv3 | ACT, 비전 백본만 DINOv3(timm) | [feature/act-dinov3.md](../feature/act-dinov3.md) |

```bash
pip install -e custom_policies/              # 학습·추론 파이썬 양쪽 (install.sh 가 한다)
python -m lerobot_policy_piper.bake <org/name>   # 학습 데이터 굽기 → <name>_stage (화면의 굽기 버튼과 같다)
python -m lerobot_policy_piper.delta <org/name>  # 액션을 관절 이동 변화량으로 → <name>_delta (feature/act-delta.md)
python -m pytest custom_policies/tests
```

## 정책 하나 더하기

배포는 **하나**다 — 설치 목록(install.sh · Dockerfile 둘)은 안 고친다.

1. `lerobot_policy_piper/<type>/` 에 `configuration_<type>.py` · `modeling_<type>.py` · `processor_<type>.py`.
   이름이 규약이다 — 팩토리가 `XxxConfig` → `XxxPolicy`, `configuration_` → `modeling_`/`processor_`,
   `make_<type>_pre_post_processors` 를 이름에서 유도한다 (act-s.md §2.2)
2. `lerobot_policy_piper/__init__.py` 에 config import 한 줄 (= 등록)
3. `policies/<type>.yaml` — 화면·학습 인자·wrapper 의 클래스 경로가 여기서 온다
4. `backend/app/core/policies.py` 의 `_ORDER` 에 자리, `tests/` 에 이름 규약 테스트
