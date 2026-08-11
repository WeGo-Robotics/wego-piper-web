# 5. 관절 캘리브레이션 `cal` dict 2회 중복 (B급)

## 문제

같은 파일 안에서 동일한 캘리브레이션 dict가 두 번 인라인으로 선언된다.

- [robot_manager.py:441-445](../backend/app/services/robot_manager.py#L441-L445) — `read_joints_normalized()` 안
- [robot_manager.py:471-475](../backend/app/services/robot_manager.py#L471-L475) — `go_parking()` 안

```python
cal = {
    "joint1": (-150000, 150000), "joint2": (0, 180000), "joint3": (-170000, 0),
    "joint4": (-100000, 100000), "joint5": (-65000, 65000), "joint6": (-100000, 130000),
    "gripper": (0, 68000),
}
```

변환 로직도 각 함수에 인라인이고, 서로 **역함수 관계**인데 따로 적혀 있다:

```python
# read_joints_normalized (raw → 정규화)
norm[name] = round(((value - mn) / (mx - mn)) * 100, 2)          # gripper: 0..100
norm[name] = round(((value - mn) / (mx - mn)) * 200 - 100, 2)    # 그 외: -100..100

# go_parking (정규화 → raw)
raw[name] = int(mn + (value / 100) * (mx - mn))                  # gripper
raw[name] = int(mn + ((value + 100) / 200) * (mx - mn))          # 그 외
```

한쪽 범위만 고치면 정규화/역정규화가 어긋나 **팔이 엉뚱한 위치로 간다.** 파킹 동작이라
바닥을 긁거나 관절 한계를 칠 수 있다.

## 해결안

모듈 상수 + 헬퍼 한 쌍으로 묶는다:

```python
# 정규화 기준 (tables.py의 calibration 기준, 간이 선형 매핑)
# AGILEX-M [-150000,150000] → [-100,100], gripper는 [0,100]
JOINT_CALIBRATION: dict[str, tuple[int, int]] = {
    "joint1": (-150000, 150000), ...
}

def normalize_joint(name: str, raw: float) -> float: ...
def denormalize_joint(name: str, norm: float) -> int: ...
```

두 함수 모두 gripper 여부 분기를 한 곳에서만 처리한다.

## 주의

- 반올림 위치가 다르다: 정규화는 `round(..., 2)`, 역정규화는 `int(...)`. 헬퍼로 옮기되
  **기존 반올림 동작을 그대로 유지**해야 한다 (파킹 목표값이 1 LSB라도 달라지면 안 됨).
- `go_parking`은 `INITIALIZE_POSITION`(lerobot_robot_piper.motors.tables) 또는
  커스텀 파킹 위치를 정규화 값으로 받는다. 입력 형식이 dict이고 키가 관절명인지 확인.
- `wrapper/parking_controller.py`도 정규화 스케일(-100~100)을 전제로 동작한다
  ([SETTLE_THRESHOLD](../wrapper/parking_controller.py#L30)). 스케일 정의를 바꾸면 안 된다.

## 검증

- 팔 연결 후 `read_joints_normalized()` 값이 변경 전과 동일한지 (수치 비교)
- `go_parking()` 실행 → 이전과 같은 위치로 가는지. **팔이 실제로 움직이므로 주변 확인 후 실행.**

## 상태

☐ 미착수
