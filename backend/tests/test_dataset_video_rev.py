"""영상 주소의 판 번호 — 같은 경로의 mp4 가 바뀌면 주소도 바뀌어야 한다.

삭제·녹화는 `videos/…/file-NNN.mp4` 를 **같은 경로에** 다시 쓴다. 크롬은 같은 주소의 영상 바이트를
페이지 메모리에 두고 새로고침 전까지 다시 쓴다 — 응답 헤더로는 못 막는다. 마지막 에피소드를 지우자
목차(moov)가 파일 끝으로 옮겨져 바이트가 통째로 밀렸고, 브라우저는 옛 목차로 새 바이트를 읽어 앞
에피소드들이 디코드 오류로 죽었다(실기 2026-10-29, `dualarm_sort_apple` 245 → 244. 헤드리스 크롬으로
재현 — 같은 페이지에서는 `<video>` 를 새로 만들어도 옛 길이를 읽고, 새로고침하면 정상).
"""

import json
import os
from pathlib import Path

PAGES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"


def test_the_rev_changes_when_the_dataset_is_rewritten(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.dataset_scanner import get_dataset

    info = tmp_path / "org" / "pick" / "meta" / "info.json"
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({"codebase_version": "v3.0", "total_episodes": 245}))
    os.utime(info, ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(settings, "lerobot_dir", tmp_path)
    before = get_dataset("org/pick")["rev"]

    info.write_text(json.dumps({"codebase_version": "v3.0", "total_episodes": 244}))
    assert get_dataset("org/pick")["rev"] != before, "다시 쓴 데이터셋이 같은 판 번호를 낸다"


def test_both_players_put_the_rev_in_the_video_url():
    src = (PAGES / "EpisodesPage.tsx").read_text()
    body, rest = src.split("const videoUrl = useCallback(", 1)[1].split("},", 1)
    assert "?v=${rev}" in body, "에피소드 뷰어의 영상 주소에 판 번호가 없다"
    assert "rev" in rest.split("]", 1)[0], "판 번호가 바뀌어도 주소를 다시 안 만든다"

    det = (PAGES / "DetectorTrainPage.tsx").read_text()
    assert "/videos/${cam}/${vm.chunk}/${vm.file}?v=${lrRev}" in det, "검출 학습 화면의 영상 주소에 판 번호가 없다"
