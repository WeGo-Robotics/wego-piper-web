"""모터 정의와 캘리브레이션 — 직접 드라이버와 **같은 값이어야 한다.**

## 왜 여기 복사본이 있나

`observation_features`/`action_features` 가 `bus.motors` 에서 파생되므로 이 값이
갈리면 프록시로 녹화한 데이터셋과 직접 드라이버로 학습한 정책의 **계약이 조용히
어긋난다.** 원래는 `lerobot_robot_piper` 한 곳에서 가져오는 게 맞다.

그런데 `lerobot_robot_piper` 는 **별도 저장소**(`WeGo-Robotics/lerobot_robot_piper`)이고
이 리포의 `vendor/lerobot_robot_piper/` 는 설치되지 않는 사본이다. 상류에 상수를
빼는 작업은 그쪽 저장소의 일이라 여기서 할 수 없다.

## 그래서 무엇이 이걸 지키나

`backend/tests/test_robot_transport.py` 가 **설치된** `lerobot_robot_piper` 의
소스를 파싱해 아래 값과 대조한다. 상류가 캘리브레이션을 바꾸면 테스트가 터진다 —
말없이 갈리는 것보다 낫다. 상류에 상수가 생기면 이 파일은 그걸 import 하면 된다.
"""

from lerobot.motors import Motor, MotorCalibration, MotorNormMode

MOTORS = {
    "joint1": Motor(1, "AGILEX-M", MotorNormMode.RANGE_M100_100),
    "joint2": Motor(2, "AGILEX-M", MotorNormMode.RANGE_M100_100),
    "joint3": Motor(3, "AGILEX-M", MotorNormMode.RANGE_M100_100),
    "joint4": Motor(4, "AGILEX-S", MotorNormMode.RANGE_M100_100),
    "joint5": Motor(5, "AGILEX-S", MotorNormMode.RANGE_M100_100),
    "joint6": Motor(6, "AGILEX-S", MotorNormMode.RANGE_M100_100),
    "gripper": Motor(7, "AGILEX-S", MotorNormMode.RANGE_0_100),
}

CALIBRATION = {
    "joint1": MotorCalibration(1, 0, 0, -150000, 150000),
    "joint2": MotorCalibration(2, 0, 0, 0, 180000),
    "joint3": MotorCalibration(3, 0, 0, -170000, 0),
    "joint4": MotorCalibration(4, 0, 0, -100000, 100000),
    "joint5": MotorCalibration(5, 0, 0, -65000, 65000),
    "joint6": MotorCalibration(6, 0, 0, -120000, 120000),
    "gripper": MotorCalibration(7, 0, 0, 0, 68000),
}
