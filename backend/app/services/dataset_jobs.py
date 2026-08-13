"""데이터셋 백그라운드 작업 프로세스.

라우터가 아니라 서비스에 두는 이유:
`exclusivity`가 이 상태를 읽어야 하는데, services → routers 방향 import는 순환이 된다.

- `upload_pm`: Hub 업로드 + 디코딩 캐시 생성 (둘 다 느린 디스크/네트워크 작업이라 공유)
- `edit_pm`:   데이터셋 편집 (CLI 래핑)

`edit_pm`이 따로 있는 이유: 편집이 전역 `process_manager`(추론용)를 쓰고 있어서,
추론 중 편집을 걸면 같은 인스턴스의 프로세스 핸들을 덮어썼다.
"""

from app.services.systemd_process import make_process

# ⚠ 유닛으로 띄우면 **게이트웨이를 재시작해도 산다.** Hub 업로드는 수십 분 걸리는데,
# 지금까지는 서버를 건드리는 순간 화면에서 사라졌다(프로세스는 계속 돌면서).
# 설정이 `local` 이면 예전처럼 자식 프로세스다 — `make_process` 가 판단한다.

# Hub 업로드 + 디코딩 캐시 (추론/학습과 독립)
upload_pm = make_process("piper-xfer-upload")

# 데이터셋 편집 (추론과 분리)
edit_pm = make_process("piper-xfer-edit")

# 페이즈 분석/굽기 (업로드와 분리)
#
# decode-cache 가 upload_pm 을 공유해서 "다른 작업이 진행 중" 409 로 서로를 막는다.
# 페이즈 분석은 수 분 걸릴 수 있고 업로드 중에도 돌 수 있어야 하므로 전용으로 둔다
# (feature/01-phase-annotation.md §5.3).
phase_pm = make_process("piper-xfer-phase")
