"""버전 — "지금 도는 것이 무엇인가" (feature/version-update.md §2).

## 정본은 코드 밖에 있다

앱은 자기 버전을 모른다 — `package.json` 은 `0.0.0`, `pyproject` 는 `0.1.0` 이다.
릴리스 태그가 곧 버전이고, 그 사실은 세 곳에 남는다:

1. **`PIPER_VERSION` 환경변수** — release.sh 가 이미지에 박는다 (배포 기계)
2. **이미지 안 매니페스트** `/opt/piper-host/manifest.txt` — 같은 이미지라 읽을 수 있다
3. **`git describe --tags`** — 소스로 도는 기계

순서대로 처음 답하는 것을 쓴다. 셋 다 없으면 "unknown" — 지어내지 않는다.

## 바깥 소프트웨어는 출처가 셋이다

컨테이너 것은 여기서 `importlib.metadata` 로 잰다. 호스트 것(드라이버·docker·
redis·ollama)은 **unitd** 가 말한다. 데몬 venv 것(mujoco·pyrealsense2·piper_* wheel)은
**데몬의 자기 보고**에 실려 온다 — 컨테이너에서 호스트 venv 를 볼 길이 없다.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from importlib import metadata
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]
MANIFEST = Path(os.environ.get("PIPER_HOST_MANIFEST", "/opt/piper-host/manifest.txt"))

#: 컨테이너 안에서 재는 것 — 사람이 "어느 lerobot 이지?" 하고 물을 것들만
CONTAINER_DISTS = ("lerobot", "torch", "torchvision", "transformers", "piper-sdk",
                   "pyrealsense2", "opencv-python-headless", "ultralytics", "numpy",
                   "fastapi", "anthropic", "piper-bus", "piper-shm", "piper-robot",
                   "piper-so101")


def parse_manifest(text: str) -> dict[str, str]:
    """`key="value"` 줄들 → dict. release.sh 의 write_manifest 형식."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def running_version() -> dict:
    """{"version", "source", "built_at", "prev", "wheels"} — 정본을 **순서대로** 찾는다.

    ⚠ **`wheels` 가 없으면 이번 릴리스는 데몬 wheel 을 안 건드렸다는 뜻이다** —
    `release.sh` 가 바뀐 패키지만 골라 굽는다(`bus/shm/robot/cam/rs/so101/sim`
    이 안 바뀌면 `wheels=""`). 그래서 데몬이 예전 버전의 wheel 을 그대로 쓰는 건
    **정상**이다. v0.4.10 을 .120 에 실기로 올려 보고서야 드러났다: 화면이 이걸
    "게이트웨이 버전 == 이번에 올린 모든 wheel 버전" 으로 잘못 가정해서, wheel 을
    안 건드린 패치마다(v0.4.8~v0.4.10) 매번 "재시작을 못 받았다" 는 오탐을 냈다."""
    env = os.environ.get("PIPER_VERSION", "").strip()
    if env and env != "unknown":
        info = {"version": env, "source": "env"}
        if MANIFEST.exists():
            m = parse_manifest(MANIFEST.read_text())
            info.update(built_at=m.get("built_at"), prev=m.get("prev"), registry=m.get("registry"),
                        wheels=m.get("wheels"))
        return info
    if MANIFEST.exists():
        m = parse_manifest(MANIFEST.read_text())
        if m.get("version"):
            return {"version": m["version"], "source": "manifest",
                    "built_at": m.get("built_at"), "prev": m.get("prev"), "wheels": m.get("wheels"),
                    "registry": m.get("registry")}
    try:
        r = subprocess.run(["git", "describe", "--tags", "--dirty", "--always"],
                           cwd=REPO, capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return {"version": r.stdout.strip(), "source": "git"}
    except Exception as exc:
        logger.debug("git describe 실패: %s", exc)
    return {"version": "unknown", "source": None}


def cuda_of(torch_version: str | None) -> str | None:
    """`2.11.0+cu130` → `13.0`. torch 를 import 하지 않는다 — 무겁고, 여기서는 문자열이면 된다."""
    if not torch_version:
        return None
    m = re.search(r"\+cu(\d+)$", torch_version)
    if not m:
        return None
    d = m.group(1)
    return f"{d[:-1]}.{d[-1]}"


def container_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in CONTAINER_DISTS:
        try:
            out[name] = metadata.version(name)
        except Exception:
            continue
    cuda = cuda_of(out.get("torch"))
    if cuda:
        out["cuda"] = cuda
    return out


def collect() -> dict:
    """화면의 [버전] 카드 한 벌. 하나가 실패해도 나머지는 그린다."""
    from app.services import units

    info = {"gateway": running_version(), "container": container_versions(),
            "host": {}, "deploy": {}, "daemons": {}}
    try:
        if units.unitd_available():
            from piper_bus import contract as C
            h = units._bus().rpc_call(C.UNITD, "host_info", [], timeout=10) or {}
            info["host"] = h.get("versions", {})
            info["deploy"] = h.get("deploy", {})
    except Exception as exc:
        logger.debug("unitd host_info 실패: %s", exc)
    try:
        from piper_bus import contract as C
        for d in C.DAEMON_SOURCES:
            rep = units._bus().daemon_info(d)
            if rep is None:
                continue
            info["daemons"][d] = rep.get("versions", {})
    except Exception as exc:
        logger.debug("데몬 버전 수집 실패: %s", exc)
    return info


# ── 새 버전 확인 (feature/version-update.md §3) ─────────────────────────────

import time as _time

VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_check_cache: dict = {"at": 0.0, "result": None}
CHECK_CACHE_S = 600.0


def version_key(v: str) -> tuple[int, int, int]:
    m = VERSION_RE.match(v or "")
    return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)   # type: ignore[return-value]


def newest_tag(tags) -> str | None:
    """`vX.Y.Z` 만 센다 — `latest`·손으로 민 이름은 버전이 아니다."""
    good = [t for t in tags if VERSION_RE.match(t)]
    return max(good, key=version_key) if good else None


def update_mode(running: dict | None = None) -> str:
    """image(배포 기계) | source(소스로 도는 기계) — 정본의 출처가 말해 준다."""
    running = running or running_version()
    return "source" if running.get("source") == "git" else "image"


def _registry_tags(registry: str, image: str = "piper-web-backend", timeout: float = 4.0) -> list[str]:
    """`registry`: 사설 평문(`host:port`, 포트 있음 — registry.sh, 인증 없음) 또는
    공개 HTTPS(`host[/namespace]`, 포트 없음 — ghcr.io/<조직> 등, v2 토큰 인증 필요).
    구분은 `release.sh`의 push 분기와 같은 규칙(포트 유무)을 쓴다."""
    import json
    import re
    import urllib.error
    import urllib.request

    host, _, namespace = registry.partition("/")
    private = bool(re.search(r":\d+$", host))
    repo = f"{namespace}/{image}" if namespace else image
    url = f"{'http' if private else 'https'}://{host}/v2/{repo}/tags/list"

    def _get(req_url: str, token: str | None = None) -> list[str]:
        req = urllib.request.Request(req_url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return list(json.load(r).get("tags") or [])

    try:
        return _get(url)
    except urllib.error.HTTPError as exc:
        if private or exc.code != 401:
            raise
        # 공개 레지스트리는 익명 pull 토큰이 필요하다 — 401 의 WWW-Authenticate 챌린지
        # 그대로 따른다(Docker Registry v2 스펙, GHCR·Docker Hub 공통).
        challenge = exc.headers.get("WWW-Authenticate", "")
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.get("realm")
        if not realm:
            raise
        qs = "&".join(f"{k}={v}" for k, v in params.items() if k != "realm")
        with urllib.request.urlopen(f"{realm}?{qs}", timeout=timeout) as r:
            token = json.load(r).get("token")
        return _get(url, token)


def _remote_git_tags(timeout: float = 8.0) -> list[str]:
    r = subprocess.run(["git", "ls-remote", "--tags", "--refs", "origin"],
                       cwd=REPO, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "git ls-remote 실패").strip())
    return [line.rsplit("refs/tags/", 1)[1] for line in r.stdout.splitlines() if "refs/tags/" in line]


def check_update(force: bool = False) -> dict:
    """{"current", "latest", "available", "mode", "checked_at", "error"}. 10분 캐시 —
    자동으로 **받지 않는다**, 배지만. 실패는 error 로 말한다(지어내지 않는다)."""
    now = _time.time()
    if not force and _check_cache["result"] and now - _check_cache["at"] < CHECK_CACHE_S:
        return _check_cache["result"]
    running = running_version()
    mode = update_mode(running)
    current = running.get("version") or "unknown"
    out = {"current": current, "latest": None, "available": False, "mode": mode,
           "checked_at": now, "error": None, "registry": running.get("registry")}
    try:
        if mode == "image":
            reg = running.get("registry")
            if not reg:
                raise RuntimeError("매니페스트에 레지스트리가 없습니다 — 어디서 받았는지 모른다")
            tags = _registry_tags(reg)
        else:
            tags = _remote_git_tags()
        latest = newest_tag(tags)
        out["latest"] = latest
        cur_key = version_key(current.split("-", 1)[0])       # `v0.4.5-1-gabc-dirty` → v0.4.5
        out["available"] = bool(latest and version_key(latest) > cur_key)
    except Exception as exc:
        out["error"] = str(exc)
    _check_cache.update(at=now, result=out)
    return out
