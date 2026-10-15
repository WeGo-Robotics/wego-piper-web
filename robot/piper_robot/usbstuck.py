"""USB 가 **커널 안에서 멈춘** 상태를 알아본다.

## 왜 이게 필요한가

`lsusb` 가 타임아웃이고 카메라·CAN 어댑터가 하나둘 사라지면 사람들은 "HC died" 를 의심하고
컨트롤러 재바인딩을 누른다. 그런데 **컨트롤러가 멀쩡한데 드라이버(`uvcvideo`)가 교착**된 경우가
있다(2026-10-14, 커널 7.0.0-31):

    kworker/…+usb_hub_wq    D  uvc_disconnect → uvc_unregister_video
    python3 (rsd 의 스레드)  D  uvc_ctrl_del_event       ← 위가 기다리는 mutex 를 쥔 쪽
    kworker/…+events        D  uvc_ctrl_status_event_work ← 위가 기다리는 쪽

세 개가 서로의 mutex 를 물고 도는 **순환 대기**다. D-state 스레드는 SIGKILL 로도 안 죽고,
`usb_hub_wq` 가 걸려 있어 USB 핫플러그 처리 전체가 멈춘다. 이때 컨트롤러를 재바인딩하면 그
요청마저 같은 곳에 걸려 — 그걸 부른 데몬(robotd)까지 멈춘다. **풀 수 있는 방법은 재부팅뿐이다.**

이 모듈은 그 상태를 `/proc` 만 읽어서 판정한다(root 불필요, 장치를 안 건드린다).

## 어떻게 판정하나

- 스레드가 `D`(커널 안에서 깨울 수 없는 대기) 상태이고
- 기다리는 곳이 USB/UVC 경로(`wchan` 이 `uvc_*`·`usb_*`·`xhci*`·`hub_*`, 또는 `usb_hub_wq` 워커)이며
- **연속한 두 번의 관찰에서 같은 스레드가 계속 그 상태**다.

D 상태는 정상 I/O 에서도 잠깐 생긴다 — 한 번 본 것으로는 멈췄다고 하지 않는다.

⚠ `/proc/<pid>/cmdline` 은 읽지 않는다. D-state 프로세스의 cmdline 읽기는 같이 멈출 수 있다.
`stat`·`wchan`·`comm` 은 안전하다.
"""

from __future__ import annotations

import glob
import os
import subprocess
import time
from collections.abc import Callable

# `wchan` 이 이 접두사로 시작하면 USB/UVC 경로에서 기다리는 것이다.
USB_WCHAN_PREFIXES = ("uvc_", "usb_", "xhci", "hub_", "v4l2_", "video_")
# 워커 스레드는 `wchan` 이 권한 때문에 `0`/`-` 로 보이는 일이 많다 — 이름에 워크큐가 박혀 있다
# (`kworker/0:0+usb_hub_wq`). 이게 D 면 USB 허브 이벤트 처리가 멈춘 것이다.
USB_COMM_PARTS = ("usb_hub_wq", "uvc", "xhci")

MAX_REPORTED = 6


def scan_blocked(proc_root: str = "/proc") -> dict[int, dict]:
    """지금 `D` 상태인 스레드들 `{tid: {pid, tid, comm, wchan}}`. USB 와 무관한 것도 포함한다."""
    out: dict[int, dict] = {}
    for tdir in glob.glob(os.path.join(proc_root, "[0-9]*", "task", "[0-9]*")):
        try:
            with open(os.path.join(tdir, "stat")) as f:
                stat = f.read()
        except OSError:
            continue                       # 읽는 사이에 스레드가 끝났다
        # `comm` 은 괄호 안이고 공백·괄호를 품을 수 있다 — 마지막 `)` 로 가른다
        lp, rp = stat.find("("), stat.rfind(")")
        if lp < 0 or rp < 0 or rp + 2 >= len(stat):
            continue
        if stat[rp + 2] != "D":
            continue
        try:
            with open(os.path.join(tdir, "wchan")) as f:
                wchan = f.read().strip()
        except OSError:
            wchan = ""
        parts = tdir.split(os.sep)
        try:
            pid, tid = int(parts[-3]), int(parts[-1])
        except ValueError:
            continue
        out[tid] = {"pid": pid, "tid": tid, "comm": stat[lp + 1:rp], "wchan": wchan}
    return out


def is_usb_path(task: dict) -> bool:
    wchan = task.get("wchan") or ""
    if wchan.startswith(USB_WCHAN_PREFIXES) or "uvc" in wchan:
        return True
    comm = task.get("comm") or ""
    return any(part in comm for part in USB_COMM_PARTS)


class StuckTracker:
    """연속한 관찰에서 **계속** 막혀 있는 USB 스레드만 골라낸다.

    관찰 사이의 간격은 부르는 쪽이 정한다(감시 루프는 2초). 한 번 본 D 는 "의심"이고
    두 번 이어서 보면 "멈췄다" 다 — 정상 I/O 의 잠깐 D 를 사고로 읽지 않는다.
    """

    def __init__(self, scan: Callable[[], dict[int, dict]] = scan_blocked) -> None:
        self._scan = scan
        self._prev: set[int] = set()

    def observe(self) -> list[dict]:
        now = self._scan()
        usb = {tid: t for tid, t in now.items() if is_usb_path(t)}
        stuck = [t for tid, t in usb.items() if tid in self._prev]
        self._prev = set(usb)
        return sorted(stuck, key=lambda t: (t["comm"], t["tid"]))[:MAX_REPORTED]


def stuck_now(gap_s: float = 1.0, scan: Callable[[], dict[int, dict]] = scan_blocked,
              sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """한 번 불러서 확정한다 — 두 번 읽는다(진단 화면용). 감시 루프는 `StuckTracker` 를 쓴다."""
    tracker = StuckTracker(scan)
    tracker.observe()
    sleep(gap_s)
    return tracker.observe()


def kernel_hints(timeout_s: float = 3.0, lines: int = 400) -> list[str]:
    """커널 로그의 hung task 줄(`uvc`·`usb`). **읽을 수 있을 때만** — 못 읽으면 빈 목록.

    저널 읽기 권한(`systemd-journal` 그룹)이 없거나 컨테이너면 못 읽는다. 판정은 `/proc` 이
    하고 이건 근거를 보태는 용도다.
    """
    try:
        p = subprocess.Popen(
            ["journalctl", "-k", "--no-pager", "-n", str(lines), "-o", "cat"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            out, _ = p.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            p.kill()
            return []                      # 기다리지 않는다 — D-state 면 영영 안 끝난다
    except Exception:
        return []
    hits = [ln.strip() for ln in out.splitlines()
            if "blocked for more than" in ln and ("uvc" in ln or "usb" in ln or "kworker" in ln)
            or ("uvc_" in ln and "+0x" in ln)]
    return hits[-MAX_REPORTED:]


def describe(tasks: list[dict]) -> str:
    """사람이 읽는 한 줄 — **무엇을 하라**가 먼저다."""
    if not tasks:
        return ""
    names = ", ".join(
        f"{t['comm']}({t['wchan']})" if t.get("wchan") not in ("", "0", "-") else t["comm"]
        for t in tasks[:3])
    return (
        "USB 가 커널 안에서 멈췄습니다 (uvcvideo 교착). 카메라·CAN 어댑터가 사라지거나 응답하지 "
        "않을 수 있고, 프로그램으로는 풀 수 없습니다 — 컴퓨터를 재부팅하세요. "
        f"USB 컨트롤러 복구는 누르지 마세요: 그 요청까지 같은 곳에서 멈춥니다. (멈춘 곳: {names})"
    )


def report(tasks: list[dict], hints: list[str] | None = None) -> dict:
    """게이트웨이·화면이 읽는 모양."""
    return {
        "kernel_stuck": bool(tasks),
        "stuck": tasks,
        "advice": describe(tasks) or None,
        "kernel_log": hints or [],
    }
