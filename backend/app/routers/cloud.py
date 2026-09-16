"""임대 GPU(Vast.ai) — 게이트웨이 키·준비도·오퍼 조회 (feature/vast-training.md §9-1·§9-2).

두 화면이 읽는다:

- **설정 → 클라우드** — 게이트웨이 SSH 키 (`/ssh-key*`). "빌리기 전까지".
- **LeRobot → 클라우드 GPU 의 RENT 탭** — 오퍼·템플릿·준비도. "무엇을 빌릴지 고른다".
- **같은 페이지의 인스턴스 탭** — 지금 도는 것·고아·파기 (`/instances`).

⚠ **아직 여기서 인스턴스를 띄우지는 않는다.** 끄는 쪽(`DELETE /instances/{id}`)이
먼저 온 것은 순서가 뒤바뀐 게 아니다 — 사람이 유일한 가드인 동안에는 **볼 수 있고
끌 수 있는 것**이 먼저다. 띄우는 엔드포인트는 조달 상태기계를 `TrainManager` 에
엮은 뒤에 붙는다.
"""

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query

from app.services.cloud import sshkey
from app.services.cloud.providers import vast
from app.services.cloud.providers.base import MIN_CUDA, OfferFilter
from app.services.training.jobs import job_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cloud", tags=["cloud"])

#: 우리가 만든 인스턴스의 라벨 접두사. ⚠ **레지스트리를 잃어도 남는 유일한
#: 단서**라 고아 판정이 이것에 달려 있다 — 바꾸면 옛 인스턴스를 못 알아본다.
ORPHAN_PREFIX = "piper-"

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


# ─────────────────────────────────────────────────────────────────────────────
# RENT 탭 — 오퍼·템플릿·준비도 (§9-2)
# ─────────────────────────────────────────────────────────────────────────────

#: 프로바이더는 상태가 없다(캐시는 모듈 안). 자격증명 저장소가 생기면(§9-1 의
#: `/credentials`) 여기서 키를 넘긴다 — 지금은 CLI 가 제 파일을 읽는다.
def _provider() -> vast.VastProvider:
    return vast.VastProvider()


def _to_http(exc: Exception) -> HTTPException:
    """예외를 사람이 읽을 응답으로. **CLI 없음은 고장이 아니다**(§9-1 과 같은 규칙)."""
    if isinstance(exc, FileNotFoundError):
        return HTTPException(409, NO_CLI)
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    if isinstance(exc, subprocess.TimeoutExpired):
        return HTTPException(504, "Vast 응답이 없습니다 — 잠시 뒤 새로고침해 주세요")
    return HTTPException(502, f"Vast 조회에 실패했습니다: {str(exc)[:300]}")


@router.get("/vast/offers")
async def vast_offers(
    gpu: list[str] = Query(["RTX 4090"]),
    num_gpus: int = Query(1, ge=1, le=8),
    min_cuda: float = Query(MIN_CUDA, ge=0, le=99),
    min_reliability: float = Query(0.98, ge=0, le=1),
    min_inet: float = Query(200.0, ge=0, le=100_000),
    min_cpu: float | None = Query(None, ge=0, le=512),
    max_price: float | None = Query(None, gt=0, le=1000),
    disk_gb: float = Query(40.0, ge=5, le=2000),
    limit: int = Query(50, ge=1, le=200),
    refresh: bool = False,
):
    """빌릴 수 있는 기계 목록.

    ⚠ **`dph_total` 이 아니라 `hourly` 를 화면에 써야 한다.** `dph_total` 에 섞인
    디스크는 검색 기본값(~5GB)이고 `hourly` 는 요청한 `disk_gb` 로 다시 계산한 값이다.
    호스트마다 `storage_cost` 가 4배까지 벌어지므로 일률적인 보정으로는 못 맞춘다.

    ⚠ 준비도가 빨간불이어도 **이 목록은 막지 않는다.** 세팅하기 전에 가격부터 보고
    싶은 게 사람이다 — 막는 것은 [빌리기] 버튼 하나뿐이다(§9-2).

    `gpu` 는 **여러 번 줄 수 있고**(`?gpu=RTX+4090&gpu=RTX+3060`), **빈 값이면 전체**다
    (`?gpu=`). 사용자가 "싹 다 보여줘" 라고 했다 — 한 기종만 고르게 하는 계약이면
    그 말을 못 들어준다.
    """
    f = OfferFilter(
        gpu_names=tuple(gpu), num_gpus=num_gpus, min_cuda=min_cuda,
        min_reliability=min_reliability, min_inet_down=min_inet,
        min_cpu_cores=min_cpu, max_price=max_price, disk_gb=disk_gb, limit=limit,
    )
    try:
        offers = await asyncio.to_thread(_provider().search, f, refresh=refresh)
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc
    return {"query": vast.build_query(f), "disk_gb": disk_gb,
            "offers": [asdict(o) for o in offers]}


@router.get("/gpus")
async def cloud_gpus(
    num_gpus: int = Query(1, ge=1, le=8),
    min_cuda: float = Query(MIN_CUDA, ge=0, le=99),
    min_reliability: float = Query(0.98, ge=0, le=1),
    min_inet: float = Query(200.0, ge=0, le=100_000),
    min_cpu: float | None = Query(None, ge=0, le=512),
    max_price: float | None = Query(None, gt=0, le=1000),
    disk_gb: float = Query(40.0, ge=5, le=2000),
    refresh: bool = False,
):
    """고를 수 있는 **GPU 기종** — 선택지를 채우는 목록.

    ⚠ **표와 같은 필터를 받는다.** 카탈로그가 자기 조건으로 만들어지면 "선택지에는
    있는데 표는 0행" 이 된다. 그래서 `gpu` 만 빼고 같은 파라미터를 받는다.

    ⚠ **고정 목록을 두지 않는다.** 예전에 화면에 7개를 박아 뒀는데 사용자가 쓰려는
    3060 이 없었고, 대신 RTX 5090(sm_120 — 우리 cu126 이미지로는 못 돈다)이 들어
    있었다. 박아 둔 목록은 이렇게 조용히 틀린다.

    실패하면 빈 목록을 준다 — 화면이 지금 오퍼에 보이는 기종과 합쳐 쓰므로 선택
    자체가 막히지는 않는다.
    """
    f = OfferFilter(
        gpu_names=(), num_gpus=num_gpus, min_cuda=min_cuda,
        min_reliability=min_reliability, min_inet_down=min_inet,
        min_cpu_cores=min_cpu, max_price=max_price, disk_gb=disk_gb,
    )
    try:
        rows = await asyncio.to_thread(_provider().catalog, f, refresh=refresh)
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("GPU 카탈로그 조회 실패: %s", exc)
        return {"gpus": [], "disk_gb": disk_gb,
                "detail": f"기종 목록을 불러오지 못했습니다: {str(exc)[:200]}"}
    return {"gpus": [asdict(g) for g in rows], "disk_gb": disk_gb, "detail": None}


@router.get("/instances")
async def cloud_instances():
    """지금 살아 있는 인스턴스 — **돈이 나가고 있는 것들**.

    ⚠ **고아를 같이 표시한다.** 우리 라벨(`piper-`)이 붙었는데 레지스트리가 모르는
    것이 고아다 — 게이트웨이가 죽었다 살아났거나 레코드를 잃었을 때 생긴다(§6-3).
    라벨이 레지스트리를 잃고도 남는 유일한 단서다.

    ⚠ **자동으로 파기하지 않는다.** 다른 기계의 게이트웨이가 돌리는 학습일 수 있다
    (§10 결정 5) — 남의 것을 끄는 쪽이 더 나쁜 실수다. 보여 주고 버튼을 주는
    데까지가 우리 몫이다.
    """
    try:
        rows = await asyncio.to_thread(_provider().list_instances)
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc

    known = {int(r.instance_id) for r in job_registry.list()
             if str(r.instance_id or "").isdigit()}
    out = []
    for i in rows:
        d = asdict(i)
        d["orphan"] = i.label.startswith(ORPHAN_PREFIX) and i.id not in known
        out.append(d)
    # 고아를 먼저 — 사람이 봐야 할 것이 위로 온다
    out.sort(key=lambda d: (not d["orphan"], d["id"]))
    return {"instances": out,
            "orphans": sum(1 for d in out if d["orphan"]),
            "known": sorted(known)}


@router.delete("/instances/{instance_id}")
async def destroy_instance(instance_id: int):
    """인스턴스를 파기한다. **확인될 때만 성공이라고 말한다.**

    ⚠ 실측 함정 둘이 프로바이더 안에 박혀 있다 — `-y` 가 없으면 프롬프트에서 멈춰
    `Aborted.` 를 찍고 **종료코드 0** 으로 끝나고(살아 있는데 성공으로 읽힌다),
    `destroy -y --raw` 는 **빈 출력**이라 응답으로는 판정할 수 없다. 그래서 판정은
    목록으로 한다.

    ⚠ 확인이 안 되면 **200 으로 넘어가지 않는다.** 조용한 성공이 이 경로에서 가장
    비싼 거짓말이다 — 사람이 껐다고 믿고 자리를 뜬다.
    """
    try:
        gone = await asyncio.to_thread(_provider().destroy, int(instance_id))
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc
    if not gone:
        raise HTTPException(
            502, f"인스턴스 {instance_id} 가 사라진 것을 확인하지 못했습니다 — "
                 f"과금이 계속될 수 있습니다. `vastai show instances` 로 확인하세요.")
    return {"destroyed": True, "instance_id": int(instance_id)}


@router.get("/templates")
async def cloud_templates(refresh: bool = False):
    """우리가 올려 둔 학습 템플릿.

    ⚠ `private=true` 없이 부르면 공개 2048개가 오고 **우리 건 하나도 없다**(§상태).
    프로바이더가 그 질의를 쥐고 있다 — 여기서 다시 쓰지 않는다.
    """
    try:
        rows = await asyncio.to_thread(_provider().templates, refresh=refresh)
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc
    return {"templates": [asdict(t) for t in rows]}


async def _ssh_registered(want: str | None) -> bool | None:
    """계정에 **같은 지문**이 있나. 모르면 `None` — 거짓 통과를 만들지 않는다."""
    if not want or not shutil.which("vastai"):
        return None

    def _go() -> bool | None:
        out = subprocess.run(["vastai", "show", "ssh-keys", "--raw"],
                             capture_output=True, text=True, timeout=45)
        if out.returncode != 0:
            return None
        try:
            rows = json.loads(out.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            k = (row.get("public_key") or row.get("ssh_key") or "").strip()
            if k:
                try:
                    if sshkey.fingerprint(k) == want:
                        return True
                except ValueError:
                    continue
        return False

    try:
        return await asyncio.to_thread(_go)
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("SSH 키 등록 확인 실패: %s", exc)
        return None


@router.get("/readiness")
async def readiness():
    """빌리기 전에 초록불이어야 하는 것들. **한 항목이 넘어져도 나머지는 답한다.**

    ⚠ 이 응답은 목록을 막는 데 쓰지 않는다. [빌리기] 버튼 하나만 막고, 무엇이
    모자란지 말해 준다 — 그래야 세팅 전에도 가격을 볼 수 있다(§9-2).
    """
    cli = shutil.which("vastai") is not None
    key = sshkey.info()

    account: dict | None = None
    account_error: str | None = None
    templates: list[dict] = []
    if cli:
        try:
            account = await asyncio.to_thread(_provider().whoami)
        except Exception as exc:                                    # noqa: BLE001
            account_error = str(exc)[:200]
        try:
            templates = [asdict(t) for t in await asyncio.to_thread(_provider().templates)]
        except Exception as exc:                                    # noqa: BLE001
            logger.warning("템플릿 조회 실패: %s", exc)

    registered = await _ssh_registered(key.get("fingerprint")) if cli else None

    checks = {
        "cli": {"ok": cli, "detail": None if cli else NO_CLI},
        "api_key": {"ok": account is not None,
                    "detail": account_error or (None if account else "API 키가 설정되지 않았습니다")},
        "ssh_key": {"ok": bool(key.get("exists")) and registered is True,
                    "exists": bool(key.get("exists")),
                    "registered": registered,
                    "fingerprint": key.get("fingerprint"),
                    "detail": None if registered else
                              ("설정 → 클라우드 에서 키를 만들어 주세요" if not key.get("exists")
                               else "설정 → 클라우드 에서 [Vast 계정에 등록] 을 눌러 주세요")},
        "template": {"ok": bool(templates),
                     "detail": None if templates else "학습 템플릿을 찾지 못했습니다"},
    }
    # ⚠ 잔액은 `balance` 가 아니라 `credit` 이다 — 실측에서 balance 0 · credit 25.0 이었다.
    #   balance 를 읽으면 멀쩡한 계정이 "$0 · 최대 0시간" 으로 전부 막힌다.
    #
    # ⚠ **계정 이메일은 내보내지 않는다.** 게이트웨이는 `/api/ext/v1` 말고는 인증이 없어
    #   LAN 에서 :8000 에 닿는 누구나 이 응답을 읽는다. 화면이 쓰는 것은 크레딧뿐이고
    #   (잔여 시간 계산) 이메일은 아무 데도 안 쓴다 — 안 쓰는 것을 내보낼 이유가 없다.
    return {"checks": checks,
            "ready": all(c["ok"] for c in checks.values()),
            "credit": account["credit"] if account else None,
            "templates": templates}
