"""에피소드를 바꾸면 캠 화면도 따라가야 한다.

⚠ **LeRobot v3 는 여러 에피소드를 한 chunk mp4 에 이어 붙인다.** 실측(bolt_two1):
에피소드 50개가 전부 `chunk-000/file-000.mp4` 하나이고, 에피소드 경계는 파일 안의
`from_timestamp`/`to_timestamp` 로만 구분된다.

그래서 옆 에피소드로 옮겨도 `<video src>` 가 **한 글자도 안 바뀐다.** 재로드가 없으니
`loadedmetadata` 도 안 오고, 위치 잡기를 그 이벤트에만 맡기면 그래프만 바뀌고 화면은
이전 에피소드에 머문다 — 아무 에러도 안 난다.
"""

import re
from pathlib import Path

from conftest import code_only

PAGE = (Path(__file__).resolve().parents[2]
        / "frontend" / "src" / "pages" / "EpisodesPage.tsx")


def _src() -> str:
    return PAGE.read_text()


def test_the_video_url_cannot_tell_episodes_apart():
    """URL 이 에피소드에 안 걸려 있음을 고정한다 — 이 사실이 버그의 원인이다.

    언젠가 URL 에 에피소드가 들어가면 이 테스트가 깨지고, 그때는 아래 seek 효과가
    필요 없어질 수도 있다. 그 판단을 하라고 여기서 잡는다.
    """
    body = _src().split("const videoUrl = useCallback(", 1)[1].split("},", 1)[0]
    assert "videos/${cam}/${m.chunk}/${m.file}" in body
    assert "${ep}" not in body, "URL 이 에피소드별로 갈리면 이 테스트를 다시 판단하라"


def test_switching_episode_seeks_the_video():
    """에피소드가 바뀌면(=videoMeta 가 바뀌면) 위치를 직접 옮긴다."""
    src = code_only(_src())
    effect = re.search(
        r"useEffect\(\(\) => \{\s*if \(videoActive\) seekVideos\(frameRef\.current\)\s*\},"
        r"\s*\[videoActive, seekVideos\]\)", src)
    assert effect, "에피소드 전환 시 seek 하는 효과가 없다"


def test_the_seek_effect_reruns_when_the_episode_changes():
    """`seekVideos` 는 `videoMeta` 를 물고 있어야 한다 — 그래야 에피소드마다 새로 돈다.

    의존성이 끊기면 효과가 다시 돌지 않아 첫 에피소드에 고정된다.
    """
    src = _src()
    seek = src.split("const seekVideos = useCallback(", 1)[1]
    deps = seek.split("}, [", 1)[1].split("]", 1)[0]
    assert "videoMeta" in deps, f"seekVideos 의존성에 videoMeta 가 없다: [{deps}]"

    meta = src.split("const videoMeta = useMemo", 1)[1]
    meta_deps = meta.split("}, [", 1)[1].split("]", 1)[0]
    assert "ep" in [d.strip() for d in meta_deps.split(",")], \
        f"videoMeta 가 에피소드에 안 걸려 있다: [{meta_deps}]"


def test_a_file_change_still_falls_back_to_the_load_event():
    """다른 chunk 파일로 넘어가면 readyState 가 0 이라 seek 이 무시된다 —
    `onLoadedMetadata` 가 이어받아야 한다. 둘 다 있어야 두 경우가 모두 덮인다."""
    src = _src()
    assert "v.readyState >= 1" in src, "로딩 중 seek 을 막는 가드가 없다"
    assert "onLoadedMetadata" in src, "파일 교체 시 위치를 잡을 곳이 없다"
