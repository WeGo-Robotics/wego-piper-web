#!/usr/bin/env python3
"""CAN 어댑터의 시리얼 ↔ 현재 인터페이스 이름을 보여준다.

`99-piper-can.rules` 를 다시 만들 때 쓴다. 팔을 다른 어댑터로 옮겨 물렸으면
규칙의 시리얼도 같이 바꿔야 하는데, 그때 무엇이 무엇인지 알려면 이게 필요하다.

⚠ **시리얼만으로는 어느 팔인지 알 수 없다.** 펌웨어는 보통 두 대가 같으므로
구분법이 못 된다 (한 번 달랐던 건 우연이다). 확실한 건 **팔을 움직여 보는 것**뿐이다:

    python3 deploy/udev/list-can-adapters.py --watch

를 띄워놓고 한쪽 팔을 손으로 움직이면, 그 팔이 어느 인터페이스인지 찍힌다.
udev 규칙을 쓰기 **전에** 이걸로 확인한다 — 등록은 그 뒤 일이다.
"""

import subprocess
from pathlib import Path

USB = Path("/sys/bus/usb/devices")


def _attr(node: Path, name: str) -> str:
    try:
        return (node / name).read_text().strip()
    except OSError:
        return ""


def watch() -> int:
    """어느 인터페이스의 팔이 움직이는지 보여준다.

    robotd 를 거친다 — CAN 은 데몬이 독점하므로 여기서 직접 열면 충돌한다.
    """
    import time

    from piper_bus import contract as C
    from piper_bus.client import Bus

    bus = Bus()
    ifaces = [d["iface"] for d in bus.rpc_call(C.ROBOTD, "scan", [], timeout=30) or []]
    for i in ifaces:
        bus.rpc_call(C.ROBOTD, "connect", [i], timeout=30)
    if not ifaces:
        print("robotd 가 CAN 을 못 찾았습니다. 데몬이 떠 있나요?")
        return 1

    base = {i: bus.rpc_call(C.ROBOTD, "read_joints_raw", [i], timeout=15) for i in ifaces}
    # ⚠ flush 한다 — 파이프로 넘기면 버퍼링돼서 "아무것도 안 뜬다"로 보인다
    print(f"팔을 손으로 움직이세요. 감시 중: {', '.join(ifaces)}   (Ctrl-C 로 종료)",
          flush=True)
    try:
        while True:
            time.sleep(0.2)
            for i in ifaces:
                now = bus.rpc_call(C.ROBOTD, "read_joints_raw", [i], timeout=15)
                if not now or not base[i]:
                    base[i] = now
                    continue
                delta = max(abs(a - b) for a, b in zip(now, base[i]))
                if delta > 5000:          # 손으로 밀면 쉽게 넘는 값
                    print(f"  ▶ {i} 이 움직였습니다 (Δ{delta})", flush=True)
                    base[i] = now
    except KeyboardInterrupt:
        return 0


def main() -> int:
    import sys

    if "--watch" in sys.argv:
        return watch()
    # 인터페이스 → USB 노드
    iface_of: dict[str, str] = {}
    for net in Path("/sys/class/net").iterdir():
        dev = net / "device"
        if not dev.exists():
            continue
        # .../usb3/3-11/3-11.1/3-11.1:1.0/net/can0 → 3-11.1
        for part in dev.resolve().parts:
            if part.count("-") == 1 and part[0].isdigit() and ":" not in part:
                iface_of[part] = net.name

    rows = []
    for node in sorted(USB.iterdir()):
        if _attr(node, "idVendor") != "1d50" or _attr(node, "idProduct") != "606f":
            continue
        rows.append((node.name, _attr(node, "serial"), iface_of.get(node.name, "-")))

    if not rows:
        print("CAN 어댑터(1d50:606f)를 못 찾았습니다.")
        return 1

    print(f"{'USB':<10} {'시리얼':<26} {'현재 이름':<16} 상태")
    for usb, serial, iface in rows:
        state = ""
        if iface != "-":
            out = subprocess.run(["ip", "-br", "link", "show", iface],
                                 capture_output=True, text=True).stdout.split()
            state = out[1] if len(out) > 1 else ""
        print(f"{usb:<10} {serial:<26} {iface:<16} {state}")

    print("\n⚠ 어느 팔인지는 이 표로 알 수 없습니다 — `--watch` 로 움직여서 확인하세요.")
    print("\n규칙 한 줄 형식:")
    print('  SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="1d50", '
          'ATTRS{idProduct}=="606f", ATTRS{serial}=="<시리얼>", NAME="can_<역할><번호>"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
