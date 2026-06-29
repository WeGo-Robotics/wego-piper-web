"""LeRobot subprocess 로그 이중 출력 차단.

이 디렉토리는 process_manager가 subprocess의 PYTHONPATH에 넣어주며,
Python 인터프리터 시작 시 site가 자동으로 이 모듈을 import한다.

원인: datasets / huggingface_hub / urllib3 등 일부 라이브러리 로거는
자기 핸들러를 달면서 propagate=True를 유지한다. LeRobot의 init_logging()이
root에 StreamHandler를 추가하면, 이 로거들의 메시지가
(자기 핸들러 + root 핸들러) 양쪽으로 한 번씩 = 두 번 출력된다.

propagate=False로 끄면 각 줄이 (자기 핸들러로) 정확히 한 번만 출력된다.
LeRobot 메트릭 라인은 root 로거라 영향 없음.
"""

import logging

# 자기 핸들러 + propagate=True 라서 이중 출력되는 로거들
_NOISY_LOGGERS = (
    "datasets",
    "huggingface_hub",
    "urllib3",
    "requests",
    "charset_normalizer",
)

for _name in _NOISY_LOGGERS:
    logging.getLogger(_name).propagate = False
