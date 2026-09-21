"""회수한 학습 결과를 **로컬 학습과 똑같은 모양**으로 놓는다.

## ⚠ 모양이 다르면 한 학습이 둘로 갈라진다

로컬 학습은 lerobot 이 자기 규칙으로 자리를 정한다(실측):

    ~/outputs/train/2026-09-18/09-52-01_act/
        checkpoints/020000/pretrained_model/
        checkpoints/last -> 020000

예전 회수는 이걸 안 따르고 HF 캐시 밑에 **제 이름을 지어** 넣었다 — 중간 체크포인트는
`<models_dir>/<repo 이름>/checkpoints/...`, 최종본은 `models--org--name/snapshots/<hash>/`.
한 번의 학습 결과가 화면에서 **두 덩어리로 갈라졌고**, 그중 하나는 "직접 받은 모델" 쪽에
떨어져 로컬 학습 목록에서 보이지 않았다. 묶는 화면을 새로 만들 문제가 아니라 **받는
자리가 틀린** 문제다.

## ⚠ 이름은 우리가 짓지 않는다 — 원격이 이미 지어 놨다

원격도 같은 lerobot 이라 경로가 같은 모양이다:

    /root/outputs/train/2026-09-18/02-13-23_act/checkpoints/020000/pretrained_model

`scp` 는 이 경로를 이미 손에 들고 있었고(예전 코드는 step 만 떼고 앞을 버렸다), Hub 로
올라간 가중치조차 `train_config.json` 안에 `output_dir: outputs/train/2026-09-18/02-13-23_act`
를 품고 온다(실측). 그래서 **기계에 물어볼 필요가 없다** — 크래시로 기계가 이미 죽었어도
주소는 가중치에 붙어서 온다. 제로패딩(`020000`)도 원격이 붙인 것이지 우리가 지을 이름이
아니다. 여기서 이름을 지어내는 순간 로컬과 갈라진다.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: 원격 lerobot 이 쓰는 뿌리. 경로에서 학습 이름을 떼어낼 때의 기준점이기도 하다.
REMOTE_ROOT = "/root/outputs/train"

#: 로컬·원격 공통 구조. 스캐너가 읽는 모양이다
#: (`**/checkpoints/{step}/pretrained_model/config.json`, `model_scanner._scan_train_outputs`).
CKPTS = "checkpoints"
WANTED = "pretrained_model"
LAST = "last"
CONFIG = "train_config.json"

#: `outputs/train/<날짜>/<시각>_<정책>` 에서 뒤쪽만 떼어낼 때 쓰는 표식.
#: 로컬 `train_config.json` 은 상대경로(`outputs/train/...`), 원격 `ls` 는 절대경로
#: (`/root/outputs/train/...`) 라 양쪽 다 이 표식으로 자른다.
_MARK = "outputs/train/"


def _writable(p: Path) -> bool:
    """`p` 에 쓸 수 있나. **아직 없는 경로도 판정한다** — 있는 조상까지 올라가 본다."""
    probe = p
    while not probe.exists():
        if probe.parent == probe:
            return False
        probe = probe.parent
    return os.access(probe, os.W_OK)


def train_root() -> Path:
    """로컬 학습 결과의 뿌리. **스캔 경로 안에서 고른다.**

    ⚠ 스캔 밖에 쓰면 파일은 받았는데 화면에 안 뜬다 — 회수했다고 믿는데 못 쓰는
    상태가 된다. 그래서 새 설정을 만들지 않고 이미 스캔 중인 경로 중에서 고른다.
    `/root/...`·`/app/...` 처럼 이 프로세스가 못 쓰는 것이 섞여 있으므로 쓰기 가능한
    첫 번째를 쓴다(컨테이너용 경로가 목록에 같이 들어 있다).
    """
    from app.core.config import settings

    for p in settings.model_paths:
        if p.name == "train" and p.parent.name == "outputs" and _writable(p):
            return p
    return Path.home() / "outputs" / "train"


def ensure_scanned(root: Path) -> None:
    """`root` 가 스캔되는지 보장한다. 안 되면 스캔 경로에 넣는다.

    ⚠ `train_root()` 는 보통 스캔 경로 중에서 고르므로 아무 일도 안 한다. 마지막
    폴백(`~/outputs/train`)으로 떨어졌을 때만 실제로 추가된다 — 그 경우가 바로
    "받아 놓고 화면에 안 뜨는" 상황이다.
    """
    from app.core.config import settings

    root = Path(root)
    for p in settings.model_paths:
        if root == p or root.is_relative_to(p):
            return
    settings.add_model_path(str(root))
    logger.info("모델 스캔 경로에 추가했습니다(없으면 받아 놓고 화면에 안 뜬다): %s", root)


def resolve_root(root: Path | None = None, *, ensure: bool = False) -> Path:
    """쓸 뿌리를 한 번에 정한다. **회수가 부르는 입구는 여기 하나다.**

    ⚠ `ensure` 는 **쓰기 직전에만** 켠다. 읽기만 하면서 켜면 설정을 건드리게 되고,
    뿌리를 명시로 받은 경우(테스트·특수 경로)에는 애초에 아무것도 안 한다 — 테스트가
    사용자 `model_paths.json` 을 더럽히면 안 된다.
    """
    if root is not None:
        return Path(root)
    r = train_root()
    if ensure:
        ensure_scanned(r)
    return r


def fallback_run(repo_id: str) -> str:
    """원격 경로도 `train_config.json` 도 못 읽었을 때 쓸 학습 이름. **마지막 수단이다.**"""
    return (repo_id or "").split("/")[-1] or "cloud-run"


def run_name(s: str) -> str:
    """경로에서 **학습 이름**(`2026-09-18/02-13-23_act`)만 떼어낸다.

    원격 절대경로(`/root/outputs/train/…/checkpoints/020000/pretrained_model`)와
    `train_config.json` 의 상대경로(`outputs/train/…`) 둘 다 받는다. 체크포인트 아래를
    가리키는 경로면 `checkpoints/` 앞에서 끊는다.
    """
    s = (s or "").strip().rstrip("/")
    if not s:
        return ""
    head, sep, _ = s.partition(f"/{CKPTS}/")
    if sep:
        s = head
    i = s.rfind(_MARK)
    if i >= 0:
        return s[i + len(_MARK):].strip("/")
    # 표식이 없는 모양(직접 준 `--output_dir`). 날짜/시각 두 칸 관례만 지켜 준다.
    parts = Path(s).parts
    return "/".join(parts[-2:]) if len(parts) >= 2 else s


def run_from_config(path: Path) -> str:
    """`train_config.json` 이 품고 온 자기 주소를 읽는다. 못 읽으면 빈 문자열.

    ⚠ **이게 Hub 경로의 핵심이다.** 기계가 이미 파기됐거나 크래시로 죽었어도, 받은
    가중치 안에 `output_dir` 이 들어 있어서 원래 학습 이름을 복원할 수 있다.
    """
    try:
        cfg = json.loads(Path(path).read_text())
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("%s 를 못 읽었습니다: %s", path, exc)
        return ""
    return run_name(str(cfg.get("output_dir") or ""))


def checkpoint_dir(run: str, step: str, root: Path | None = None) -> Path:
    """`<뿌리>/<학습 이름>/checkpoints/<step>/pretrained_model`.

    ⚠ 로컬 학습이 만드는 것과 **글자 하나까지 같은** 자리다. 여기서 한 칸이라도
    달라지면 스캐너가 다른 학습으로 읽는다.
    """
    base = Path(root) if root is not None else train_root()
    return base / run / CKPTS / step / WANTED
