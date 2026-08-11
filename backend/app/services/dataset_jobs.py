"""데이터셋 백그라운드 작업 프로세스.

라우터가 아니라 서비스에 두는 이유:
`exclusivity`가 이 상태를 읽어야 하는데, services → routers 방향 import는 순환이 된다.

- `upload_pm`: Hub 업로드 + 디코딩 캐시 생성 (둘 다 느린 디스크/네트워크 작업이라 공유)
- `edit_pm`:   데이터셋 편집 (CLI 래핑)

`edit_pm`이 따로 있는 이유: 편집이 전역 `process_manager`(추론용)를 쓰고 있어서,
추론 중 편집을 걸면 같은 인스턴스의 프로세스 핸들을 덮어썼다.
"""

from app.services.process_manager import ProcessManager

# Hub 업로드 + 디코딩 캐시 (추론/학습과 독립)
upload_pm = ProcessManager()

# 데이터셋 편집 (추론과 분리)
edit_pm = ProcessManager()
