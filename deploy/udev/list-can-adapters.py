#!/usr/bin/env python3
"""CAN 어댑터의 시리얼 ↔ 현재 인터페이스 이름을 보여준다.

`99-piper-can.rules` 를 다시 만들 때 쓴다. 팔을 다른 어댑터로 옮겨 물렸으면
규칙의 시리얼도 같이 바꿔야 하는데, 그때 무엇이 무엇인지 알려면 이게 필요하다.

펌웨어까지 같이 찍는 이유: 시리얼만으로는 **어느 팔인지** 알 수 없다.
펌웨어가 다르면 그게 가장 빠른 구분법이다 (실제로 이걸로 뒤바뀜을 잡아냈다).
"""

import subprocess
from pathlib import Path

USB = Path("/sys/bus/usb/devices")


def _attr(node: Path, name: str) -> str:
    try:
        return (node / name).read_text().strip()
    except OSError:
        return ""


def main() -> int:
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

    print("\n규칙 한 줄 형식:")
    print('  SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="1d50", '
          'ATTRS{idProduct}=="606f", ATTRS{serial}=="<시리얼>", NAME="can_<역할><번호>"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
