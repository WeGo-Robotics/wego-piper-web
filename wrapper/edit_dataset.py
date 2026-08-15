"""`lerobot-edit-dataset` 래퍼 — 편집 뒤 사이드카 동기화까지 한 프로세스에서.

왜 게이트웨이 훅이 아니라 래퍼인가: 편집은 유닛으로 돌아 **게이트웨이 재시작에도
산다.** 완료 시점을 게이트웨이가 기다리는 구조면 재시작하는 순간 사이드카 동기화가
소리 없이 빠진다 — 같은 프로세스에 넣으면 그런 수명 문제가 없다
(start_record.py 와 같은 이유의 래핑이다).

동작:
1. 받은 인자를 그대로 `lerobot-edit-dataset` 에 넘긴다 (CLI 래핑 원칙 — 인자 계약은
   cli_mapping.build_edit_dataset_args 가 소유).
2. `delete_episodes` 가 성공하면 `_old` 백업의 사이드카(페이즈 라벨·신호·카메라)를
   번호 재매핑해 새 meta 로 가져온다 ([piper_phase.sidecar](../phase/piper_phase/sidecar.py)).
3. split/merge 는 사이드카를 **안 가져간다** — 산출물이 여러 개라 대응이 자명하지
   않다. 대신 로그로 알린다 (조용한 유실 금지).
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("edit_dataset")


def _flag(args: list[str], name: str) -> str | None:
    prefix = f"{name}="
    for a in args:
        if a.startswith(prefix):
            return a[len(prefix):]
    return None


def main() -> int:
    args = sys.argv[1:]
    cli = shutil.which("lerobot-edit-dataset")
    if not cli:
        logger.error("lerobot-edit-dataset 를 PATH 에서 찾을 수 없습니다")
        return 127

    rc = subprocess.call([cli, *args])
    if rc != 0:
        return rc

    op = _flag(args, "--operation.type")
    repo_id = _flag(args, "--repo_id")
    if op == "delete_episodes" and repo_id and not _flag(args, "--new_repo_id"):
        # in-place 삭제만 사이드카를 따라 옮긴다 — 새 repo 로 뽑는 경우 원본은 무사하다
        raw = _flag(args, "--operation.episode_indices") or "[]"
        deleted = [int(x) for x in json.loads(raw)]
        root = _flag(args, "--root")
        ds_root = Path(root) if root else \
            Path(os.environ.get("HF_LEROBOT_HOME",
                                Path.home() / ".cache/huggingface/lerobot")) / repo_id
        from piper_phase.sidecar import remap_after_delete

        moved = remap_after_delete(ds_root, deleted)
        if moved:
            logger.info("사이드카 동기화 완료: %s", ", ".join(moved))
    elif op in ("split", "merge"):
        logger.warning(
            "%s 은 사이드카(phase_labels/piper_cameras)를 가져가지 않습니다 — "
            "산출 데이터셋에서 재분석하세요", op,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
