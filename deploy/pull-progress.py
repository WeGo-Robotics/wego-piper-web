#!/usr/bin/env python3
"""docker pull 에 진행률을 붙인다 — 터미널이 아니어도 (docs/install-troubleshooting.md).

`docker pull` 은 터미널이 아니면(웹 업데이트의 일시 유닛 → 저널, `tee`) 막대를 안
그리고, `apply.sh` 는 `-q` 라 아무것도 안 찍었다. 첫 설치는 베이스 7GB 라 몇 분을
말없이 기다리게 했다. 도커 API 의 `/images/create` 스트림을 유닉스 소켓으로 직접
읽어 레이어별 바이트를 합산한다 — 표준 라이브러리만 쓴다(호스트에 pip 이 없어도).

사용:  pull-progress.py IMAGE[:TAG] [--label 이름]
소켓이 안 열리면(권한 등) `docker pull` 로 물러난다 — 진행률만 잃고 설치는 된다.
"""

import base64
import http.client
import json
import os
import socket
import sys
import time

SOCK = os.environ.get("DOCKER_HOST_SOCK", "/var/run/docker.sock")


class NotFound(Exception):
    pass


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def split_image(ref: str) -> tuple[str, str]:
    """`host:5000/name:tag` → (`host:5000/name`, `tag`). 포트의 콜론과 태그의 콜론을 가른다."""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, "latest"


def registry_auth(image: str) -> str | None:
    """~/.docker/config.json 의 자격을 X-Registry-Auth 로. 없으면 None (사내 레지스트리)."""
    host = image.split("/", 1)[0] if "/" in image and ("." in image.split("/", 1)[0] or ":" in image.split("/", 1)[0]) else "https://index.docker.io/v1/"
    try:
        cfg = json.load(open(os.path.expanduser("~/.docker/config.json")))
    except Exception:
        return None
    for key, entry in (cfg.get("auths") or {}).items():
        if host in key and entry.get("auth"):
            user, _, pw = base64.b64decode(entry["auth"]).decode().partition(":")
            return base64.urlsafe_b64encode(json.dumps({"username": user, "password": pw, "serveraddress": key}).encode()).decode()
    return None


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}" if unit == "GB" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 60}분 {s % 60:02d}초" if s >= 60 else f"{s}초"


class Progress:
    """레이어별 상태를 모아 한 줄로. 순수 — 이벤트를 먹고 문장을 낸다.

    도커 29(containerd 저장소)가 실제로 내는 어휘(실측): `Pulling from <repo>` 는 id 가
    **태그**라 레이어가 아니다. 레이어는 `Pulling fs layer` → (`Downloading`
    current/total — 이미 내려받은 blob 이면 아예 없다) → `Download complete` →
    `Extracting` current(바이트, units) → `Pull complete`. 이미 있으면 레이어 이벤트
    없이 `Status: Image is up to date` 만 온다.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.layers: dict[str, dict] = {}
        self.started = time.monotonic()
        self.error: str | None = None
        self.final: str = ""

    def feed(self, ev: dict) -> None:
        if ev.get("error"):
            self.error = ev["error"]
            return
        status, lid = ev.get("status", ""), ev.get("id")
        if status.startswith(("Status:", "Digest:")):
            self.final = status
            return
        if not lid or status.startswith("Pulling from"):
            return
        L = self.layers.setdefault(lid, {"cur": 0, "total": 0, "ext": 0, "state": "wait"})
        d = ev.get("progressDetail") or {}
        if status == "Downloading":
            L["state"] = "down"
            L["cur"] = d.get("current", L["cur"])
            L["total"] = d.get("total") or L["total"]
        elif status in ("Verifying Checksum", "Download complete"):
            L["state"] = "downloaded"
            if L["total"]:
                L["cur"] = L["total"]
        elif status == "Extracting":
            L["state"] = "extract"
            L["ext"] = d.get("current", L["ext"]) or L["ext"]
        elif status == "Pull complete":
            L["state"] = "done"
            if L["total"]:
                L["cur"] = L["total"]
        elif status == "Already exists":
            L["state"] = "exists"
        elif status in ("Pulling fs layer", "Waiting"):
            L["state"] = "wait"

    def summary(self) -> dict:
        real = [L for L in self.layers.values() if L["state"] != "exists"]
        return {"layers": len(real), "done": sum(1 for L in real if L["state"] == "done"),
                "cur": sum(L["cur"] for L in real), "known": sum(L["total"] for L in real),
                "unknown": sum(1 for L in real if not L["total"]),
                "ext": sum(L["ext"] for L in real),
                "exists": len(self.layers) - len(real)}

    def line(self) -> str:
        s = self.summary()
        el = max(time.monotonic() - self.started, 0.001)
        if s["layers"] == 0:
            return f"{self.label}: 레이어 확인 중"
        head = f"{self.label}: {s['done']}/{s['layers']} 레이어"
        if s["cur"]:
            rate = s["cur"] / el
            if s["known"] and not s["unknown"]:
                pct = 100.0 * s["cur"] / s["known"]
                left = (s["known"] - s["cur"]) / rate if rate > 0 else 0.0
                eta = f" · 남은 ~{fmt_secs(left)}" if 0 < pct < 100 else ""
                return f"{head} · {fmt_bytes(s['cur'])} / {fmt_bytes(s['known'])} ({pct:.0f}%) · {fmt_bytes(rate)}/s{eta}"
            # 도커 29(containerd 저장소)는 Downloading 에 total 을 안 실어 준다(실측) —
            # 퍼센트를 지어내지 않고 받은 양·속도·레이어만 말한다
            return f"{head} · {fmt_bytes(s['cur'])} 받음 · {fmt_bytes(rate)}/s (전체 크기는 도커가 안 알려 줌)"
        if s["ext"]:
            return f"{head} · 풀기 {fmt_bytes(s['ext'])}"
        return f"{head} · 준비 중"

    def finish_line(self) -> str:
        s = self.summary()
        el = time.monotonic() - self.started
        if s["layers"] == 0:
            if "up to date" in self.final or s["exists"]:
                return f"{self.label}: 이미 있음" + (f" (레이어 {s['exists']}개)" if s["exists"] else "")
            return f"{self.label}: 끝 — {self.final or '레이어 없음'}"
        got = fmt_bytes(s["cur"]) if s["cur"] else (f"풀기 {fmt_bytes(s['ext'])}" if s["ext"] else f"{s['done']}개 레이어")
        return f"{self.label}: 받음 — {got}, {fmt_secs(el)}" + (f" (이미 있던 레이어 {s['exists']}개)" if s["exists"] else "")


def stream(image: str, tag: str):
    conn = _UnixConn(SOCK)
    headers = {}
    auth = registry_auth(image)
    if auth:
        headers["X-Registry-Auth"] = auth
    from urllib.parse import quote
    # ⚠ API 버전을 박지 않는다 — docker 29 는 1.44 미만을 거절했다(실측 "client version 1.43 is
    #   too old"). 버전 없는 경로는 데몬이 지원하는 최신으로 받는다.
    conn.request("POST", f"/images/create?fromImage={quote(image, safe='')}&tag={quote(tag, safe='')}", headers=headers)
    resp = conn.getresponse()
    if resp.status == 404:
        msg = resp.read().decode(errors="replace")
        try:
            msg = json.loads(msg).get("message", msg)
        except ValueError:
            pass
        raise NotFound(msg)
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}: {resp.read().decode(errors='replace')[:200]}")
    buf = b""
    while True:
        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    label = None
    if "--label" in sys.argv:
        label = sys.argv[sys.argv.index("--label") + 1]
        args = [a for a in args if a != label]
    if not args:
        print("사용: pull-progress.py IMAGE[:TAG] [--label 이름]", file=sys.stderr)
        return 2
    ref = args[0]
    image, tag = split_image(ref)
    p = Progress(label or ref)
    tty = sys.stdout.isatty()
    # 터미널은 제자리 갱신(0.5초), 저널은 한 줄씩(5초). PULL_PROGRESS_EVERY 로 바꾼다.
    every = float(os.environ.get("PULL_PROGRESS_EVERY", "0.5" if tty else "5"))
    last = 0.0
    try:
        for ev in stream(image, tag):
            p.feed(ev)
            if p.error:
                break
            now = time.monotonic()
            if now - last >= every:
                last = now
                if tty:
                    sys.stdout.write("\r\033[K  " + p.line())
                else:
                    print("  " + p.line())
                sys.stdout.flush()
    except NotFound as exc:
        print(f"  ✗ {p.label}: {exc}")
        return 1
    except (OSError, RuntimeError) as exc:
        # 소켓이 안 열리거나(권한) API 가 거절 — 진행률만 포기하고 docker 에 맡긴다
        print(f"  (진행률 없이 받습니다 — {exc})", file=sys.stderr)
        os.execvp("docker", ["docker", "pull", ref])
    if tty:
        sys.stdout.write("\r\033[K")
    if p.error:
        print(f"  ✗ {p.label}: {p.error}")
        return 1
    print("  " + p.finish_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
