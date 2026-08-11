# 4. `_ERR_BITS` 프로세스 경계 넘어 복붙 (B급) — ☑ 완료 (c안)

> 문서는 (a) *"wrapper 에 두고 백엔드가 읽기"* 를 권했지만 **(c) 테스트 고정**을 택했다:
> 백엔드가 `wrapper/` 를 import 하려면 `sys.path` 조작이 필요하고(선례 없음),
> `_ERR_BITS` 는 비공개 이름이며, daemon-split 후 robotd 가 CAN 을 독점하면
> wrapper 쪽 사본이 사라져 그 결합을 되돌려야 한다.
> 드리프트 위험은 (a) 와 동일하게 없어진다 — [test_err_bits.py](../backend/tests/test_err_bits.py).

## 문제

Piper 팔의 `err_code` 비트 → 의미 매핑 12줄이 두 곳에 동일하게 존재한다.

- [robot_manager.py:518-524](../backend/app/services/robot_manager.py#L518-L524) (백엔드)
- [arm_controller.py:22-28](../wrapper/arm_controller.py#L22-L28) (wrapper)

```python
_ERR_BITS = {
    0: "joint1_comm", 1: "joint2_comm", 2: "joint3_comm",
    3: "joint4_comm", 4: "joint5_comm", 5: "joint6_comm",
    8: "joint1_angle_limit", 9: "joint2_angle_limit", 10: "joint3_angle_limit",
    11: "joint4_angle_limit", 12: "joint5_angle_limit", 13: "joint6_angle_limit",
}
```

wrapper 쪽 주석에 이미 *"백엔드 robot_manager._ERR_BITS와 동일"* 이라고 적혀 있다 —
복제라는 것을 알면서 둔 상태다.

비트 매핑이 틀리면 **에러 플래그가 조용히 잘못 표시된다**(엉뚱한 관절을 지목하거나
에러를 놓침). 안전 관련 표시이므로 어긋나면 실질적 위험이 있다.

## 제약

백엔드와 wrapper는 **다른 파이썬 프로세스 / 다른 인터프리터**에서 돈다
(`settings.local_python`, `settings.grpc_python`). 백엔드 패키지를 wrapper에서 import할 수 없다.
크래시 격리라는 설계 원칙상 그렇게 만들어서도 안 된다.

## 해결안 (택1)

### (a) wrapper에 두고 백엔드가 읽기 — 권장

`wrapper/piper_err_bits.py`(또는 기존 `arm_controller.py`)를 단일 소스로 두고,
백엔드가 `wrapper/` 경로를 `sys.path`에 넣어 import한다.
백엔드→wrapper 방향 import는 이미 `WRAPPER_PATH` 등으로 경로를 알고 있어 부담이 적다.

단점: 백엔드가 wrapper 모듈에 의존하게 된다. 다만 `arm_controller.py`는
[상단 import가 `logging`/`time`/`typing`뿐](../wrapper/arm_controller.py#L15-L17)이라
lerobot을 끌어오지 않는다 — 백엔드에서 import해도 부작용이 없다 (확인 완료).

### (b) JSON 데이터 파일

`shared/piper_err_bits.json`을 양쪽에서 읽는다. 의존 방향이 안 생기지만 파일이 하나 늘고
타입이 사라진다.

### (c) 그대로 두고 테스트로 고정

한쪽을 정본으로 정하고, 두 dict가 같은지 확인하는 테스트를 추가한다. 가장 변경이 작다.

## 함께 볼 것

[arm_controller.py:30-40](../wrapper/arm_controller.py#L30-L40)의 `_FAULT_FLAGS`는 현재
wrapper에만 있다. 백엔드에도 같은 개념이 필요해지면 같은 문제가 반복되므로 함께 옮길지 판단.

## 검증

- 팔을 연결한 상태에서 에러 플래그 조회 (`read_error`) 결과가 이전과 동일한지
- 일부러 관절 한계를 넘겨 에러를 유발하고 플래그 이름이 맞게 나오는지

## 상태

☑ 완료 (테스트로 고정)
