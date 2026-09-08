"""SO-101 (Feetech STS3215 시리얼 버스) — so101d 의 코어 (feature/so101d.md).

LeRobot 을 import 하지 않는다 — 캘리브레이션은 JSON 파일로만 호환한다.
"""

from piper_so101.calibration import MotorCal, load_calibration  # noqa: F401
from piper_so101.joints import SO101_JOINTS, from_record, to_record  # noqa: F401
