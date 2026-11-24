"""장치 확장 — 이미지 하나를 웹에서 받아 사용자 유닛으로 켠다 (feature/pika.md §2).

unitd 만 쓴다. 게이트웨이(컨테이너)는 docker 도 systemd 도 없다 — 버스로 부탁한다.

## 무엇을 믿고 무엇을 안 믿나

- **이미지를 고르는 사람이 곧 장치 권한을 주는 사람이다.** 그래서 1차 관문은 허용 레지스트리다
  (`ghcr.io/wego-robotics/` + `PIPER_EXT_REGISTRIES`). 이미 이 기계에 있는 이미지는 받지 않고 쓴다 —
  docker 에 접근할 수 있는 사람이 이미 올려 둔 것이다(개발 빌드). 어느 쪽이든 라벨 매니페스트가 없으면 거절한다.
- **매니페스트는 ExecStart 를 못 쓴다.** 유닛은 아래 고정 템플릿이고, 매니페스트가 고르는 것은 장치 어휘뿐이다.
  `privileged`·`cap_add`·임의 마운트는 어휘에 없다. 늘 붙는 것: `network none`, `ipc host`, Redis 소켓,
  데이터 디렉토리 → `/data`.
- 유닛은 **검사한 이미지 ID** 로 돈다 — 태그가 나중에 다른 이미지를 가리켜도 검사 안 한 것이 돌지 않는다.

## 자리

| 것 | 자리 | 왜 |
|---|---|---|
| 등록부 | `~/.config/piper-web/extensions.json` | unitd(호스트 사용자) 것. 데이터 루트의 `config/` 는 게이트웨이 컨테이너(root)가 만들어 사용자가 못 쓴다 |
| 유닛 | `~/.config/systemd/user/piper-ext-<name>.service` | 다른 데몬과 같은 사용자 유닛 — 서비스 화면·로그·부팅 시 시작이 그대로 된다 |
| 데이터 | `<데이터 루트>/ext/<name>` → 컨테이너 `/data` | 데이터 루트는 설치 사용자 소유다(apply.sh). 게이트웨이는 `/data/ext/<name>` 로 본다 |
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

LABEL = "io.wego.piper.extension"
CONTRACT = 1
DEFAULT_REGISTRIES = ("ghcr.io/wego-robotics/",)

# 매니페스트 장치 어휘 → cgroup 규칙. hidraw 는 major 가 동적이라 유닛을 쓸 때 /proc/devices 에서 읽는다.
DEVICE_RULES = {
    "usb-serial": ["c 188:* rmw"],        # ttyUSB (CH340 등)
    "usb-raw": ["c 189:* rmw"],           # /dev/bus/usb — libusb
    "hidraw": None,
}

# 확장의 RPC 허브가 이 이름이면 핵심 데몬의 요청을 가로챈다
_CORE_HUBS = {"rsd", "camerad", "robotd", "estopd", "so101d", "simd", "unitd"}

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_HUB = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
_PROVIDE_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
# 첫 글자가 영숫자여야 한다 — `-` 로 시작하면 docker 가 옵션으로 읽는다
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}(:[A-Za-z0-9._-]{1,128})?(@sha256:[a-f0-9]{64})?$")


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def registry_path() -> Path:
    return Path(os.environ.get("PIPER_EXT_REGISTRY") or _home() / ".config" / "piper-web" / "extensions.json")


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config") / "systemd" / "user"


def data_root() -> Path:
    if os.environ.get("PIPER_DATA_ROOT"):
        return Path(os.environ["PIPER_DATA_ROOT"])
    return Path("/srv/piper-data") if Path("/srv/piper-data").is_dir() else _home() / ".local" / "share" / "piper-web"


def ext_dir(name: str) -> Path:
    return data_root() / "ext" / name


def unit_short(name: str) -> str:
    """서비스 화면·unitd 가 쓰는 짧은 이름 — `piper-` 뒤."""
    return f"ext-{name}"


def allowed_registries() -> tuple[str, ...]:
    extra = tuple(p.strip().rstrip("/") + "/" for p in os.environ.get("PIPER_EXT_REGISTRIES", "").split(",") if p.strip())
    return DEFAULT_REGISTRIES + extra


# ── 등록부 ──────────────────────────────────────────────────────────────────

_lock = threading.Lock()


def load() -> dict[str, dict]:
    try:
        v = json.loads(registry_path().read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(reg: dict[str, dict]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2))
    tmp.replace(p)


def units() -> dict[str, str]:
    """등록된 확장의 유닛 짧은 이름 → 제목. unitd 의 허용 목록에 합쳐진다."""
    return {unit_short(n): e.get("manifest", {}).get("title") or n for n, e in load().items()}


# ── 매니페스트 ───────────────────────────────────────────────────────────────

def validate_manifest(m, *, taken_hubs: set[str] = frozenset()) -> dict:
    """라벨 매니페스트를 검사해 정리한 사본을 돌려준다. 어긋나면 ValueError (사람이 읽을 문장)."""
    if not isinstance(m, dict):
        raise ValueError("매니페스트가 JSON 객체가 아닙니다")
    name = m.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"확장 이름 모양이 아닙니다(소문자·숫자·-, 2~31자): {name!r}")
    if m.get("contract") != CONTRACT:
        raise ValueError(f"모르는 계약 버전입니다: {m.get('contract')!r} (이 PIPER STUDIO 는 {CONTRACT})")
    devices = m.get("devices", [])
    if not isinstance(devices, list) or not all(isinstance(d, str) for d in devices):
        raise ValueError("devices 는 문자열 목록이어야 합니다")
    unknown = [d for d in devices if d not in DEVICE_RULES]
    if unknown:
        raise ValueError(f"모르는 장치 어휘입니다: {unknown} (허용: {sorted(DEVICE_RULES)})")
    hub = m.get("rpc_hub", name)
    if not isinstance(hub, str) or not _HUB.match(hub):
        raise ValueError(f"RPC 허브 이름 모양이 아닙니다: {hub!r}")
    if hub in _CORE_HUBS:
        raise ValueError(f"RPC 허브 이름이 핵심 데몬과 겹칩니다: {hub}")
    if hub in taken_hubs:
        raise ValueError(f"RPC 허브 이름을 다른 확장이 이미 씁니다: {hub}")
    provides = m.get("provides", {}) or {}
    if not isinstance(provides, dict):
        raise ValueError("provides 는 객체여야 합니다")
    clean_provides: dict[str, list[str]] = {}
    for kind, ids in provides.items():
        if kind not in ("pose", "grip") or not isinstance(ids, list) or not all(
                isinstance(i, str) and _PROVIDE_ID.match(i) for i in ids):
            raise ValueError(f"provides.{kind} 가 계약 모양이 아닙니다")
        clean_provides[kind] = list(ids)
    ui = m.get("ui")
    if ui not in (None, "server"):
        raise ValueError(f"ui 는 'server' 만 됩니다: {ui!r}")
    title = m.get("title") or name
    if not isinstance(title, str) or len(title) > 60:
        raise ValueError("title 은 60자 이하 문자열이어야 합니다")
    features = [f for f in (m.get("features") or []) if isinstance(f, str)]
    return {"name": name, "title": title, "contract": CONTRACT, "devices": list(dict.fromkeys(devices)),
            "provides": clean_provides, "rpc_hub": hub, "features": features, "ui": ui}


def check_image_ref(image: str) -> str:
    image = (image or "").strip()
    if not _IMAGE.match(image):
        raise ValueError(f"이미지 주소 모양이 아닙니다: {image!r}")
    return image


def registry_allowed(image: str) -> bool:
    return any(image.startswith(p) for p in allowed_registries())


# ── docker ──────────────────────────────────────────────────────────────────

def _docker() -> str:
    d = shutil.which("docker")
    if not d:
        raise RuntimeError("docker 가 없습니다 — 장치 확장은 docker 가 있는 호스트에서만 됩니다")
    return d


def inspect(image: str) -> dict | None:
    """로컬 이미지의 {id, labels}. 없으면 None."""
    r = subprocess.run([_docker(), "image", "inspect", image, "--format", "{{json .}}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    info = json.loads(r.stdout)
    return {"id": info["Id"], "labels": (info.get("Config") or {}).get("Labels") or {},
            "tags": info.get("RepoTags") or []}


def read_manifest(info: dict) -> dict:
    raw = info["labels"].get(LABEL)
    if not raw:
        raise ValueError(f"PIPER STUDIO 장치 확장 이미지가 아닙니다 — 라벨 {LABEL} 이 없습니다")
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError(f"라벨 {LABEL} 이 JSON 이 아닙니다") from None


def hidraw_major() -> int | None:
    try:
        for line in Path("/proc/devices").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "hidraw":
                return int(parts[0])
    except (OSError, ValueError):
        pass
    return None


# ── 유닛 템플릿 ──────────────────────────────────────────────────────────────

REDIS_SOCK = Path("/run/redis/redis-server.sock")


def render_unit(entry: dict, *, docker: str, hidraw: int | None) -> str:
    """고정 템플릿 — 글은 `ext_unit.py` 에 있다(사람이 `systemctl --user cat` 으로 읽는 글이라 번역 대상이 아니다)."""
    import ext_unit

    return ext_unit.render(entry, docker=docker, hidraw=hidraw, rules=DEVICE_RULES, redis_sock=REDIS_SOCK)


def _systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)


def write_unit(entry: dict) -> bool:
    """유닛 파일을 쓴다. 바뀌었으면 True (daemon-reload 가 필요하다)."""
    text = render_unit(entry, docker=_docker(), hidraw=hidraw_major())
    path = unit_dir() / f"piper-{unit_short(entry['manifest']['name'])}.service"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return False
    path.write_text(text)
    return True


def refresh_units() -> int:
    """unitd 가 뜰 때 — 등록된 확장의 유닛을 다시 쓴다(hidraw major 는 부팅마다 바뀔 수 있다). 바뀐 수."""
    changed = 0
    for entry in load().values():
        try:
            changed += write_unit(entry)
        except Exception:
            continue
    if changed:
        _systemctl("daemon-reload")
    return changed


# ── 추가·제거 ────────────────────────────────────────────────────────────────

class Installer:
    """추가는 몇 분 걸린다(받기) — unitd 의 RPC 루프를 막지 않게 스레드로 돌리고 진행은 `status()` 로 읽는다."""

    def __init__(self) -> None:
        self._job: dict = {"state": "idle"}
        self._thread: threading.Thread | None = None

    def status(self) -> dict:
        with _lock:
            return dict(self._job, lines=list(self._job.get("lines", []))[-80:])

    def _log(self, line: str) -> None:
        with _lock:
            self._job.setdefault("lines", []).append(line)

    def add(self, image: str) -> dict:
        image = check_image_ref(image)
        with _lock:
            if self._job.get("state") == "running":
                raise RuntimeError("확장 추가가 이미 돌고 있습니다 — 끝나기를 기다리세요")
            self._job = {"state": "running", "image": image, "started": time.time(), "lines": []}
        self._thread = threading.Thread(target=self._run, args=(image,), name="ext-add", daemon=True)
        self._thread.start()
        return self.status()

    def _run(self, image: str) -> None:
        try:
            result = install(image, log=self._log)
            with _lock:
                self._job.update(state="done", name=result["name"], finished=time.time())
        except Exception as exc:
            with _lock:
                self._job.update(state="failed", error=str(exc), finished=time.time())


def install(image: str, *, log=lambda _l: None) -> dict:
    """받기 → 라벨 검사 → 유닛 쓰기 → 켜기 → 등록. 실패하면 ValueError/RuntimeError (사람이 읽을 문장)."""
    image = check_image_ref(image)
    docker = _docker()
    info = inspect(image)
    pulled = info is None
    if info is None:
        if not registry_allowed(image):
            raise ValueError(f"허용 레지스트리가 아닙니다: {image} — 허용: {', '.join(allowed_registries())}")
        log(f"받는 중: {image}")
        proc = subprocess.Popen([docker, "pull", image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            log(line.rstrip())
        if proc.wait() != 0:
            raise RuntimeError(f"받기 실패: {image}")
        info = inspect(image)
        if info is None:
            raise RuntimeError(f"받았는데 이미지가 안 보입니다: {image}")
    else:
        log(f"이 기계에 있는 이미지를 씁니다: {image}")
    raw = read_manifest(info)
    reg = load()
    name = raw.get("name") if isinstance(raw, dict) else None
    taken = {e["manifest"]["rpc_hub"] for n, e in reg.items() if n != name}
    m = validate_manifest(raw, taken_hubs=taken)
    if m["devices"] and not REDIS_SOCK.exists():
        log(f"⚠ Redis 유닉스 소켓이 없습니다({REDIS_SOCK}) — 확장이 버스에 못 붙습니다 (docker-compose.yml 의 안내)")
    data = ext_dir(m["name"])
    data.mkdir(parents=True, exist_ok=True)
    entry = {"image": image, "image_id": info["id"], "manifest": m, "data_dir": str(data),
             # 우리가 받은 것만 제거 때 지운다 — 이 기계에서 빌드한 개발 이미지를 확장과 같이 지우면 안 된다
             "pulled": pulled or reg.get(m["name"], {}).get("pulled", False),
             "added": reg.get(m["name"], {}).get("added") or time.time(), "updated": time.time()}
    unit = f"piper-{unit_short(m['name'])}.service"
    if m["name"] in reg:
        log(f"이미 있는 확장을 바꿉니다: {m['name']}")
    write_unit(entry)
    _systemctl("daemon-reload")
    with _lock:
        reg = load()
        reg[m["name"]] = entry
        _save(reg)
    for action in ("enable", "restart"):
        r = _systemctl(action, unit, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"{action} {unit} 실패: {(r.stderr or r.stdout).strip()}")
    log(f"켰습니다: {unit}")
    return {"name": m["name"], "unit": unit}


def remove(name: str, *, purge: bool = False) -> dict:
    """끄고 유닛·등록·이미지를 지운다. `purge` 면 데이터(캘리브레이션·배정)도."""
    reg = load()
    if name not in reg:
        raise ValueError(f"등록된 확장이 아닙니다: {name}")
    entry = reg[name]
    unit = f"piper-{unit_short(name)}.service"
    _systemctl("disable", "--now", unit, timeout=60)
    try:
        (unit_dir() / unit).unlink()
    except FileNotFoundError:
        pass
    _systemctl("daemon-reload")
    _systemctl("reset-failed", unit)
    docker = _docker()
    purged = False
    data = Path(entry["data_dir"])
    if purge and data.is_dir():
        # 안의 파일은 컨테이너(root)가 만든 것이 섞여 사용자가 못 지운다 — 그 이미지로 지운다(네트워크 없이)
        subprocess.run([docker, "run", "--rm", "--network", "none", "-v", f"{data}:/x", "--entrypoint", "sh",
                        entry["image_id"], "-c", "rm -rf /x/* /x/.[!.]* 2>/dev/null; true"],
                       capture_output=True, text=True, timeout=120)
        try:
            data.rmdir()
            purged = True
        except OSError:
            purged = False
    if entry.get("pulled"):
        # 다른 것이 쓰고 있거나 태그가 여럿이면 docker 가 거절한다 — 그대로 둔다
        subprocess.run([docker, "image", "rm", entry["image_id"]], capture_output=True, text=True, timeout=60)
    with _lock:
        reg = load()
        reg.pop(name, None)
        _save(reg)
    return {"name": name, "unit": unit, "purged": purged, "data_dir": str(data)}


def listing() -> list[dict]:
    """게이트웨이에 보이는 모양 — 데이터는 데이터 루트 상대 경로로도 준다(컨테이너는 `/data` 로 본다)."""
    out = []
    root = data_root()
    for name, e in sorted(load().items()):
        data = Path(e["data_dir"])
        try:
            rel = str(data.relative_to(root))
        except ValueError:
            rel = None
        out.append({"name": name, "title": e["manifest"]["title"], "image": e["image"],
                    "image_id": e["image_id"], "manifest": e["manifest"], "unit": f"piper-{unit_short(name)}",
                    "data_dir": str(data), "data_rel": rel, "added": e.get("added"), "updated": e.get("updated")})
    return out
