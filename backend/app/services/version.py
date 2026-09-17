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
    info["staleness"] = staleness(info)
    return info


#: **우리가 굽는 wheel 일곱 개.** `deploy/stage-hostside.sh` 의 목록과 같아야 한다.
#: ⚠ 예전엔 `pkg.startswith("piper-")` 로 셌다. 그러면 서드파티 `piper-sdk`(AgileX 의 CAN
#:   SDK, apply.sh 가 PyPI 에서 0.6.1 로 깐다)까지 우리 wheel 로 세어, 제대로 설치된
#:   호스트가 기동할 때마다 "데몬 wheel 이 이번 릴리스와 다릅니다: robotd piper-sdk 0.6.1"
#:   이라고 **영영 거짓 경고**한다. 이름이 아니라 목록으로 센다.
OUR_WHEELS = frozenset({"piper-bus", "piper-shm", "piper-robot", "piper-cam",
                        "piper-rs", "piper-so101", "piper-sim"})


def staleness(info: dict | None = None) -> dict:
    """깔린 것이 적용된 릴리스와 맞나 — **판정은 여기 한 곳**이다.

    ⚠ 예전에는 이 규칙이 브라우저에만 있었다(VersionCard 의 `wheelMismatch`). 그러면
    버전 카드를 연 사람만 알고, 기동 로그에도 안 남고, API 로도 안 나온다. 그리고 그
    규칙은 **wheel 만** 봤다 — 2026-09-15 NUC 은 wheel 이 0.5.1 로 최신인데
    `daemons/camerad.py` 가 9월 1일자라 회색 카드가 죽었고, 화면은 아무 말도 안 했다.

    둘을 따로 본다:

    - `wheels` — **적용 릴리스와** 견준다. 번들은 릴리스마다 일곱 wheel 전부에 그 버전을 박아
      싣고 설치는 번들과 다른 것을 깐다 — 그러니 제대로 적용된 호스트는 전부 같다.
      ⚠ 예전에는 "이번 릴리스가 다시 구운 것"(매니페스트 `wheels=`)만 견줬다. 그 규칙은
      **건너뛴 릴리스를 영영 못 잡는다**: .120 은 게이트웨이가 v0.5.4 인데 wheel 이 전부
      0.4.7 이었고(시뮬 장면이 옛 바닥·옛 탑뷰였다) `wheels=` 가 비어 있어 아무 말도
      안 나왔다. `0.1.0` 은 도장 전 빌드라 뺀다.
    - `daemons` — 적용된 **데몬 소스**의 스탬프(`$SRC/daemons/.version`, apply.sh 가 풀 때마다
      적는다) 대 적용 릴리스(`VERSION`). 표시가 없으면 낡은 것으로 본다 — 스탬프가 생기기
      전에 깔린 호스트가 정확히 그 경우이고, 그게 이 사고의 그 기계다.

    저장소에서 띄운 게이트웨이는 `deploy` 가 비어 있어 소스 판정을 건너뛴다(배포본이 아니다).
    """
    info = info if info is not None else collect()
    gw = info.get("gateway") or {}
    deploy = info.get("deploy") or {}
    applied, stamp = deploy.get("current"), deploy.get("daemons_version")
    # ⚠ **적용 릴리스와 견준다.** 번들은 릴리스마다 일곱 wheel 전부에 그 버전을 박아 싣고,
    #   설치는 번들과 다른 것을 깐다 — 그러니 제대로 적용된 호스트는 전부 같아야 한다.
    #   예전에는 "이번 릴리스가 다시 구운 것"(매니페스트 `wheels=`)만 견줬는데, 그러면
    #   그 릴리스를 건너뛴 호스트를 영영 못 잡는다: .120 은 게이트웨이 v0.5.4 에 wheel 이
    #   전부 0.4.7 이었고(시뮬 장면이 옛 모습), `wheels=` 가 비어 있어 아무 말도 안 나왔다.
    want = str(applied or gw.get("version") or "").lstrip("v").split("-")[0]
    wheels: list[str] = []
    if want:
        for d, vs in (info.get("daemons") or {}).items():
            for pkg, ver in (vs or {}).items():
                if pkg in OUR_WHEELS and ver not in ("0.1.0", want):
                    wheels.append(f"{d} {pkg} {ver}")
    source = f"{stamp or '표시 없음'} ≠ {applied}" if applied and stamp != applied else None
    return {"wheels": sorted(wheels), "daemons": source,
            "ok": not wheels and source is None}


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
