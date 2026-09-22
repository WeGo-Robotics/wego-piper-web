"""게이트웨이 전용 SSH 키 — **Piper Studio 가 직접 만든다.**

⚠ **왜 사람의 키를 빌려 쓰지 않나.** 배포판 게이트웨이는 컨테이너라 `~/.ssh` 가
아예 없다. 개발 머신의 키를 전제하면 저장소에서 띄웠을 때만 되는 기능이 된다 —
이 저장소가 반복해서 겪은 "소스에선 되고 배포판에선 안 되는" 모양이다.

⚠ **왜 `ssh-keygen` 이 아니라 라이브러리인가.** 처음 이유는 "이미지에 `openssh-client`
가 없다" 였고 **그 이유는 사라졌다** — 베이스 `cu130-4` 부터 들어 있다(원격 학습과
클라우드 회수가 `ssh`·`scp` 를 쓰므로). 그래도 `cryptography` 로 **프로세스 안에서**
만드는 쪽을 그대로 둔다: 바이너리가 있든 없든 결과가 같고, 테스트가 바이너리 없이 돈다.

키는 `config_dir/ssh/` 에 남는다. 마운트라 재설치를 넘어 살아남는다 — HF 토큰을
`/data` 에 두는 것과 같은 이유다.

⚠ **비밀키는 어떤 응답에도 나가지 않는다.** 이 모듈이 밖으로 주는 것은 공개키와
핑거프린트뿐이다.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: 키에 박히는 주석. Vast 계정 화면에서 "이게 누구 키인가" 를 읽을 유일한 단서다.
COMMENT = "piper-studio"


def key_dir() -> Path:
    # ⚠ 호출 때마다 읽는다 — 모듈 로드 시점에 굳히면 테스트가 자리를 못 바꾼다
    from app.core.config import settings

    return Path(settings.config_dir) / "ssh"


def private_path() -> Path:
    return key_dir() / "id_ed25519"


def public_path() -> Path:
    return key_dir() / "id_ed25519.pub"


def fingerprint(public_key: str) -> str:
    """OpenSSH 와 같은 모양의 지문 — `SHA256:...` (패딩 없는 base64).

    ⚠ 등록 확인에 이게 필요하다. "계정에 키가 하나라도 있으면 됐다" 로 보면 남의
    키에 거짓 통과하고, 증상은 인스턴스를 빌린 **뒤** 접속 실패로 늦게 나타난다.
    """
    parts = public_key.split()
    if len(parts) < 2:
        raise ValueError("공개키 모양이 아닙니다")
    blob = base64.b64decode(parts[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def info() -> dict:
    """지금 상태. **만들지 않는다** — 읽기가 파일을 만들면 놀란다."""
    pub = public_path()
    if not pub.is_file():
        return {"exists": False, "public_key": None, "fingerprint": None,
                "path": str(private_path()), "comment": COMMENT}
    text = pub.read_text().strip()
    return {"exists": True, "public_key": text, "fingerprint": fingerprint(text),
            "path": str(private_path()), "comment": COMMENT}


def ensure() -> dict:
    """없으면 만든다. **멱등** — 있으면 그대로 두고 상태만 돌려준다.

    ⚠ 이미 있는 키를 갈아엎지 않는다. 갈아엎으면 Vast 계정에 등록해 둔 것이
    조용히 무효가 되고, 그 사실은 다음에 빌릴 때야 드러난다.
    """
    if public_path().is_file() and private_path().is_file():
        return {**info(), "created": False}

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    d = key_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)

    key = ed25519.Ed25519PrivateKey.generate()
    private_path().write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption()))
    private_path().chmod(0o600)

    pub = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    public_path().write_text(f"{pub} {COMMENT}\n")
    public_path().chmod(0o644)

    logger.info("게이트웨이 SSH 키를 만들었습니다: %s", private_path())
    return {**info(), "created": True}
