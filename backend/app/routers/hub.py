import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.services import hub_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/hub", tags=["hub"])


def _account(info: dict) -> dict:
    """`whoami` 응답 → 화면이 쓰는 모양. **로그인 응답도 같은 걸 쓴다.**

    갈라 두면 로그인 직후 화면과 새로고침한 화면이 다른 것을 보여준다.
    """
    token = info.get("auth", {}).get("accessToken", {})
    return {
        "logged_in": True,
        "username": info.get("name", ""),
        "fullname": info.get("fullname", ""),
        "avatar_url": info.get("avatarUrl", ""),
        "orgs": [o.get("name", "") for o in info.get("orgs", [])],
        "token_name": token.get("displayName", ""),
        # "read" 토큰도 whoami 는 성공한다 — 그러면 로그인돼 보이는데
        # **학습 끝 업로드에서만** 실패한다. 그래서 권한을 같이 내린다.
        # (실측 2026-09-02: role 은 "write"/"read". fine-grained 는 다를 수
        #  있어 "read" 일 때만 경고한다.)
        "token_role": token.get("role", ""),
        # ⚠ 자리를 알려 준다. 컨테이너는 `/data/hf/token`, 저장소에서 띄우면
        #   `~/.cache/huggingface/token` 이다 — 호스트에서 `huggingface-cli login`
        #   을 해도 컨테이너는 그 파일을 못 본다.
        "token_path": str(hub_client.token_path()),
    }


@router.get("/whoami")
async def whoami():
    """HuggingFace 로그인 정보 조회."""
    import asyncio

    try:
        # HfApi 를 새로 만들지 않는다 — endpoint 설정이 한쪽에만 걸리면
        # 여기만 조용히 huggingface.co 를 보게 된다.
        # ⚠ whoami 는 **네트워크를 탄다.** 사내망에서 HF 가 느리면 그 시간만큼
        #   이벤트 루프가 서고, 대시보드가 이걸 부르기 시작하면서 노출이 늘었다.
        info = await asyncio.to_thread(hub_client.get_api().whoami)
        return _account(info)
    except Exception as e:
        logger.debug("HF whoami failed: %s", e)
        return {"logged_in": False, "username": "", "fullname": "",
                "token_path": str(hub_client.token_path()), "error": str(e)}


class LoginRequest(BaseModel):
    token: str


@router.post("/login")
async def hub_login(body: LoginRequest):
    """토큰을 검증하고 저장한다.

    ⚠ **토큰을 되돌려 보내지 않는다.** 한 번 넣은 값을 화면에서 다시 읽을 수 있게
    하면 그 화면이 자격증명 유출 경로가 된다. 계정 이름·권한만 돌려준다.
    """
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "토큰이 비었습니다")
    try:
        info = await asyncio.to_thread(hub_client.save_token, token)
    except Exception as e:
        # ⚠ 예외 문자열에 토큰이 섞여 나올 수 있다 — 가리고 내보낸다
        raise HTTPException(401, f"토큰이 거부됐습니다: {str(e).replace(token, '***')[:200]}")
    return _account(info)


@router.post("/logout")
async def hub_logout():
    """토큰 파일을 지운다. 없어도 성공이다 — 결과가 같으니 에러로 만들 이유가 없다."""
    removed = await asyncio.to_thread(hub_client.clear_token)
    return {"logged_in": False, "removed": removed,
            "token_path": str(hub_client.token_path())}


@router.get("/models")
async def list_hub_models(q: str = "", author: str = "lerobot", limit: int = 30):
    return await hub_client.search_models(q, author, limit)


@router.get("/models/{repo_id:path}")
async def hub_model_detail(repo_id: str):
    return await hub_client.get_model_info(repo_id)


@router.get("/datasets")
async def list_hub_datasets(q: str = "", author: str = "lerobot", limit: int = 30):
    return await hub_client.search_datasets(q, author, limit)


@router.get("/datasets/{repo_id:path}")
async def hub_dataset_detail(repo_id: str):
    return await hub_client.get_dataset_info(repo_id)


class DownloadRequest(BaseModel):
    repo_id: str
    repo_type: str = "model"  # "model" or "dataset"


@router.post("/download")
async def start_download(body: DownloadRequest):
    local_dir = str(
        settings.models_dir if body.repo_type == "model" else settings.datasets_dir
    )
    await hub_client.start_download(body.repo_id, body.repo_type, local_dir)
    return {"status": "started", "repo_id": body.repo_id}


@router.get("/download/status")
async def download_status(repo_id: str):
    return hub_client.get_download_status(repo_id)
