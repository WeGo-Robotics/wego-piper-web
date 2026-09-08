"""수집 페이지를 떠났다 돌아와도 에피소드 제어가 그대로 보여야 한다.

## ⚠ 실기 보고 (2026-09-09)

재녹화를 눌러 리셋 대기(녹색 "준비 완료" 버튼)가 뜬 상태에서 다른 페이지를
갔다 오니 녹색 버튼이 없었다. 녹화는 멀쩡히 리셋 대기 중이었다 — 화면만
몰랐다.

원인: 버튼은 `status.phase === 'resetting'` 으로 그리는데 `status` 는 WS
`record_status` 푸시로만 채워졌고, 그 푸시는 **단계가 바뀔 때만** 온다.
리셋 대기 중엔 사람이 버튼을 누르기 전까지 단계가 안 바뀌므로, 페이지가
다시 마운트되면 `status` 가 null 인 채로 영영 머문다. 마운트 때 REST
미러(`/recording/status`)로 `recordState` 만 복원하고 `status` 는 빠뜨렸던 것.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "frontend" / "src" / "pages" / "RecordingPage.tsx"


def test_remounting_the_page_restores_the_episode_phase_from_rest():
    """마운트 때 `/recording/status` 로 `recordState` 와 **`status` 둘 다** 채운다.
    WS 는 전이만 알리고, 전이가 없는 대기 상태는 REST 미러가 유일한 출처다."""
    src = PAGE.read_text()
    mount = src.split("api.get<RecordStatusData>('/recording/status')", 1)[1][:400]
    assert "setRecordState(" in mount
    assert "setStatus(s)" in mount, "마운트가 status(phase) 를 복원하지 않는다 — 녹색 버튼이 사라진다"


def test_the_rest_mirror_carries_the_phase_the_button_needs():
    """REST 미러가 WS 푸시와 같은 모양(phase 포함)이어야 프론트가 복원할 수
    있다 — `get_status()` 와 `on_record_status` 가 같은 dict 를 나른다."""
    from app.services.record_manager import record_manager

    keys = set(record_manager.get_status())
    assert {"state", "current_episode", "total_episodes", "phase", "progress"} <= keys
