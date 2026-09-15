"""임대 GPU(Vast.ai) 자격증명과 게이트웨이 키 (feature/vast-training.md §9-1).

설정 → 클라우드 탭이 읽는다. **여기는 "빌리기 전까지" 만** 책임진다 — 인스턴스·
비용·고아는 나중 `/cloud` 페이지(§8 W2·W3)다.
"""

import asyncio
import logging
import shutil
import subprocess

from fastapi import APIRouter, HTTPException

from app.services.cloud import sshkey

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cloud", tags=["cloud"])

#: CLI 가 없을 때의 문구. ⚠ **사용자 탓이 아니다** — 배포판은 컨테이너라 사람이
#: 깔 방법이 없다. 고쳐야 할 것은 이미지이지 사용자가 아니라고 말해야 한다.
NO_CLI = ("이 게이트웨이에 vastai CLI 가 없습니다 — 컨테이너 배포판은 이미지에 들어 있어야 "
          "합니다(이미지 갱신 필요). 저장소에서 직접 띄웠다면 `pip install vastai`.")


@router.get("/ssh-key")
async def get_ssh_key():
    """게이트웨이 공개키와 지문. ⚠ **비밀키는 안 나간다.** 없으면 없다고만 한다."""
    return sshkey.info()


@router.post("/ssh-key")
async def create_ssh_key():
    """없으면 만든다(멱등). 있으면 그대로 둔다 — 갈아엎으면 등록해 둔 것이 무효가 된다."""
    return await asyncio.to_thread(sshkey.ensure)


@router.post("/ssh-key/register")
async def register_ssh_key():
    """공개키를 Vast 계정에 등록하고 **지문으로 확인**한다.

    ⚠ 확인이 요점이다. `create ssh-key` 가 성공을 보고해도 계정에 우리 키가 있는지는
    별개이고, 없으면 증상은 인스턴스를 빌린 뒤 접속 실패로 늦게 나타난다.
    """
    if not shutil.which("vastai"):
        raise HTTPException(409, NO_CLI)
    state = await asyncio.to_thread(sshkey.ensure)
    pub, want = state["public_key"], state["fingerprint"]

    def _run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["vastai", *args], capture_output=True, text=True, timeout=60)

    out = await asyncio.to_thread(_run, ["create", "ssh-key", pub])
    if out.returncode != 0:
        raise HTTPException(502, f"등록에 실패했습니다: {(out.stderr or out.stdout).strip()[:300]}")

    listed = await asyncio.to_thread(_run, ["show", "ssh-keys", "--raw"])
    found = False
    if listed.returncode == 0:
        import json

        try:
            for row in json.loads(listed.stdout) or []:
                k = (row.get("public_key") or row.get("ssh_key") or "").strip()
                if k and sshkey.fingerprint(k) == want:
                    found = True
                    break
        except Exception as exc:                                    # noqa: BLE001
            logger.warning("등록 확인 실패(파싱): %s", exc)
    return {"registered": found, "fingerprint": want,
            "detail": None if found else "등록은 보고됐지만 계정 목록에서 같은 지문을 못 찾았습니다."}
