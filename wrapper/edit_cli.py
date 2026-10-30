"""`lerobot-edit-dataset` 을 영상 remux 를 건 채 돈다 — 인자는 CLI 와 같다 (video_remux.py).

edit_dataset.py 가 `delete_episodes` 일 때 CLI 대신 이걸 띄운다. 따로 프로세스인 이유는 CLI 를
부르던 것과 같다: LeRobot 이 죽어도 종료 코드로 받아 사이드카 동기화를 건너뛴다.
"""

import sys

import video_remux

video_remux.install()

from lerobot.scripts.lerobot_edit_dataset import main  # noqa: E402 — 패치가 먼저다

if __name__ == "__main__":
    sys.argv[0] = "lerobot-edit-dataset"
    sys.exit(main())
