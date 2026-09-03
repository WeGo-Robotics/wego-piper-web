"""이름·설명 사이드카 — LeRobot 구조에 없는 것을 **옆에** 남긴다.

LeRobot 의 `meta/info.json`(데이터셋)과 `config.json`(체크포인트)에는 사람이
붙이는 이름·설명 자리가 없고, 그 파일들에 임의 키를 끼우면 LeRobot 도구가
재작성할 때 보존된다는 보장이 없다. 그래서 **별도 파일**로 둔다 —
`meta/piper_cameras.json`(카메라 매핑)·phase 라벨과 같은, 이 저장소의 확립된
사이드카 관례다. LeRobot 은 모르는 파일을 건드리지 않는다.

- 데이터셋: `<root>/meta/piper_notes.json` — meta/ 에 두면 허브 업로드
  (`hf upload <root> .`)에 자동으로 동반된다
- 모델(체크포인트): `<root>/piper_notes.json` — 체크포인트 디렉토리 바로 아래

허브 카드(README.md)는 업로드 직전에 이 사이드카로 만든다(`readme_for`) —
로컬과 허브가 같은 설명을 갖는다.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATASET_REL = Path("meta") / "piper_notes.json"
_MODEL_REL = Path("piper_notes.json")

EMPTY = {"name": "", "description": "", "updated_at": ""}


def _path(root: Path, kind: str) -> Path:
    return Path(root) / (_DATASET_REL if kind == "dataset" else _MODEL_REL)


def read_notes(root: Path, *, kind: str) -> dict:
    """사이드카를 읽는다. 없거나 깨졌으면 빈 값 — 목록 스캔이 부르므로 조용해야 한다."""
    try:
        d = json.loads(_path(root, kind).read_text())
        return {"name": str(d.get("name", "")),
                "description": str(d.get("description", "")),
                "updated_at": str(d.get("updated_at", ""))}
    except FileNotFoundError:
        return dict(EMPTY)
    except Exception as exc:
        logger.warning("notes 사이드카 파싱 실패 (%s): %s", root, exc)
        return dict(EMPTY)


def write_notes(root: Path, *, kind: str, name: str, description: str) -> dict:
    """사이드카를 쓴다. 데이터셋은 meta/ 가 있어야 한다 — 없는 곳에 만들면
    데이터셋이 아닌 디렉토리에 흔적이 남는다 (camera_sidecar 와 같은 규칙)."""
    p = _path(root, kind)
    if kind == "dataset" and not p.parent.exists():
        raise FileNotFoundError(f"데이터셋 meta/ 가 없습니다: {p.parent}")
    out = {"name": name.strip(), "description": description.strip(),
           "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return out


def readme_for(repo_id: str, notes: dict) -> str:
    """허브 카드 본문. 최소한만 — 카드는 허브에서 얼마든지 고칠 수 있고,
    여기서 길게 지어내면 그게 다 우리가 관리할 문장이 된다."""
    title = notes.get("name") or repo_id
    body = notes.get("description") or ""
    return (
        "---\n"
        "tags:\n"
        "- LeRobot\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )


def ensure_readme(root: Path, repo_id: str, notes: dict) -> bool:
    """업로드 직전 README.md 를 만든다. **있으면 안 건드린다** — 사람이 허브나
    로컬에서 다듬은 카드를 업로드가 조용히 덮으면 안 된다."""
    if not (notes.get("name") or notes.get("description")):
        return False
    readme = Path(root) / "README.md"
    if readme.exists():
        return False
    try:
        readme.write_text(readme_for(repo_id, notes))
        return True
    except Exception as exc:
        logger.warning("README 생성 실패 (%s): %s", root, exc)
        return False
