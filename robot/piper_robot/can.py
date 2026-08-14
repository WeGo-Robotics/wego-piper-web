"""CAN 인터페이스·USB 복구 — robotd 의 하드웨어 바닥층.

게이트웨이(`backend/`)를 import 하지 않는다. `backend/app/services/robot_manager.py`
에서 그대로 옮겨왔다 (refactor/daemon-inventory.md #2).

## 왜 호스트 배포가 강제되는가

`recover_usb_controllers` 가 `/sys/bus/pci/drivers/xhci_hcd/{unbind,bind}` 에 쓴다.
컨테이너에서 이걸 하려면 사실상 호스트 권한을 다 줘야 하므로, robotd 는
컨테이너가 아니라 호스트 systemd 유닛으로 간다.

## USB 컨트롤러가 통째로 사라지는 일이 있다

xHCI 컨트롤러가 죽으면 CAN 어댑터와 카메라가 **함께** 사라진다. 개별 장치를
다시 열어봐야 소용없고, 컨트롤러를 리바인딩해야 돌아온다.
"""

import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BITRATE = 1_000_000


def _is_root() -> bool:
    """이미 root면 sudo가 불필요하다 (컨테이너 실행 시 sudo 자체가 미설치)."""
    return os.geteuid() == 0


def _run_cmd(cmd: list[str], sudo: bool = False) -> tuple[int, str, str]:
    if sudo and not _is_root():
        cmd = ["sudo", "-n"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return -1, "", str(exc)


def slot_to_can_name(slot: str) -> str:
    role, num = slot.rsplit("_", 1)
    return f"can_{role}{num}"


# ── CAN 포트 스캔 ──

def iface_exists(iface: str) -> bool:
    """이 CAN 인터페이스가 아직 커널에 있는가.

    **USB-CAN 어댑터를 뽑으면 커널이 즉시 지운다** — 카메라의 `/dev/videoN` 과
    같은 결정적 신호다. 읽기 실패는 버스가 조용한 것일 수도 있어 그것만으로는
    "없어졌다"고 못 하지만, 이건 다르다.

    sysfs 조회라 사실상 공짜고 네트워크 네임스페이스 안에서만 뜻이 있다 —
    robotd 는 호스트에서 도므로 맞다 (게이트웨이 컨테이너는 브리지 네트워크라
    이걸 못 본다. 그래서 판정이 **데몬 쪽**에 있어야 한다).
    """
    return Path(f"/sys/class/net/{iface}").exists()


def _read_can_rx(iface: str) -> int:
    """sysfs에서 CAN RX 패킷 수 읽기 (non-blocking)."""
    try:
        return int(Path(f"/sys/class/net/{iface}/statistics/rx_packets").read_text().strip())
    except Exception:
        return 0


def sniff_can_ids(iface: str, duration: float = 1.2) -> dict:
    """raw CAN 소켓으로 버스를 잠깐 청취해 CAN ID 그룹별 빈도와 마스터/슬레이브 정황을 반환.

    Piper CAN ID 규약:
      - 0x2A1~0x2A8 : 슬레이브/standby 팔의 주기 피드백 (기본)
      - 0x2B1~0x2C8 : 마스터(示教输入臂)의 오프셋 피드백 (MasterSlaveConfig feedback_offset)
      - 0x150~0x15F : 마스터가 송신하는 관절 제어지령 (위치 변화 시)
      - 0x251~0x266 : 드라이버 고속/저속 피드백
    마스터 정황 = 0x2Bx/0x2Cx 피드백 또는 0x15x 제어지령 관측.
    """
    import socket
    import struct
    groups = {"slave_fb": 0, "master_fb": 0, "master_ctrl": 0, "driver": 0, "other": 0}
    ids: dict[int, int] = {}
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((iface,))
        s.settimeout(0.2)
        end = time.time() + duration
        while time.time() < end:
            try:
                frame = s.recv(16)
            except socket.timeout:
                continue
            cid = struct.unpack("=I", frame[:4])[0] & 0x1FFFFFFF
            ids[cid] = ids.get(cid, 0) + 1
            if 0x2A1 <= cid <= 0x2A8:
                groups["slave_fb"] += 1
            elif 0x2B1 <= cid <= 0x2C8:
                groups["master_fb"] += 1
            elif 0x150 <= cid <= 0x15F:
                groups["master_ctrl"] += 1
            elif 0x251 <= cid <= 0x266:
                groups["driver"] += 1
            else:
                groups["other"] += 1
        s.close()
    except Exception as exc:
        return {"iface": iface, "error": str(exc), "total": 0,
                "groups": groups, "ids": [], "master_detected": False, "has_traffic": False}
    master = groups["master_fb"] > 0 or groups["master_ctrl"] > 0
    return {
        "iface": iface,
        "total": sum(ids.values()),
        "has_traffic": bool(ids),
        "master_detected": master,
        "groups": groups,
        "ids": [f"{x:03X}" for x in sorted(ids)],
    }


def check_can_active(iface: str, interval: float = 0.3) -> bool:
    """CAN 포트에 실제 데이터가 오고 있는지 확인. (interval 초 간격으로 rx 증가 여부)"""
    rx1 = _read_can_rx(iface)
    import time
    time.sleep(interval)
    rx2 = _read_can_rx(iface)
    return rx2 > rx1


def scan_can_interfaces() -> list[dict]:
    rc, out, _ = _run_cmd(["ip", "-br", "link", "show", "type", "can"])
    if rc != 0 or not out.strip():
        return []
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        iface, state = parts[0], parts[1]
        bus_info = ""
        rc2, out2, _ = _run_cmd(["ethtool", "-i", iface], sudo=True)
        if rc2 == 0:
            for ln in out2.splitlines():
                if ln.strip().startswith("bus-info:"):
                    bus_info = ln.split(":", 1)[1].strip()
                    break
        rx = _read_can_rx(iface) if state == "UP" else 0
        result.append({"iface": iface, "bus_info": bus_info, "state": state, "rx_packets": rx})
    return result


def init_can_interface(iface: str, bitrate: int = DEFAULT_BITRATE) -> tuple[bool, str]:
    _run_cmd(["modprobe", "gs_usb"], sudo=True)
    _run_cmd(["ip", "link", "set", iface, "down"], sudo=True)
    rc, _, err = _run_cmd(
        ["ip", "link", "set", iface, "type", "can", "bitrate", str(bitrate)], sudo=True
    )
    if rc != 0:
        return False, f"set bitrate failed: {err}"
    rc, _, err = _run_cmd(["ip", "link", "set", iface, "up"], sudo=True)
    if rc != 0:
        return False, f"bring-up failed: {err}"
    return True, "OK"


def rename_can_interface(old_name: str, new_name: str) -> tuple[bool, str]:
    _run_cmd(["ip", "link", "set", old_name, "down"], sudo=True)
    rc, _, err = _run_cmd(["ip", "link", "set", old_name, "name", new_name], sudo=True)
    if rc != 0:
        return False, f"rename failed: {err}"
    rc, _, err = _run_cmd(["ip", "link", "set", new_name, "up"], sudo=True)
    if rc != 0:
        return False, f"bring-up failed: {err}"
    return True, "OK"


# ── USB 진단 / 복구 ──

XHCI_DRIVER_DIR = Path("/sys/bus/pci/drivers/xhci_hcd")


def get_usb_info() -> dict:
    """lsusb 출력(목록 + 트리)을 반환. root 권한 불필요."""
    rc1, flat, err1 = _run_cmd(["lsusb"])
    rc2, tree, err2 = _run_cmd(["lsusb", "-t"])
    return {
        "flat": flat if rc1 == 0 else f"(lsusb 실패: {err1})",
        "tree": tree if rc2 == 0 else f"(lsusb -t 실패: {err2})",
        "controllers": list_xhci_controllers(),
    }


def list_xhci_controllers() -> list[str]:
    """xhci_hcd 드라이버에 바인딩된 PCI 컨트롤러 주소 목록."""
    try:
        return sorted(
            n for n in (p.name for p in XHCI_DRIVER_DIR.iterdir())
            if n[:4].isalnum() and ":" in n and "." in n
        )
    except Exception:
        return []


def _sysfs_write(path: str, value: str) -> tuple[int, str, str]:
    """sudo tee로 sysfs에 기록 (값은 stdin으로 전달). root면 직접 쓴다."""
    if _is_root():
        try:
            Path(path).write_text(value)
            return 0, "", ""
        except Exception as exc:
            return -1, "", str(exc)
    cmd = ["sudo", "-n", "/usr/bin/tee", path]
    try:
        r = subprocess.run(cmd, input=value, capture_output=True, text=True, timeout=8)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return -1, "", str(exc)


def recover_usb_controllers(pci_addrs: list[str] | None = None) -> tuple[bool, str, list[str]]:
    """xHCI 컨트롤러를 unbind→bind 하여 강제 재열거.

    'HC died'로 USB 트리가 통째로 사라졌을 때 재부팅 없이 복구한다.
    pci_addrs 미지정 시 바인딩된 모든 xhci_hcd 컨트롤러 대상.
    반환: (성공여부, 메시지, 처리된 주소 목록)
    """
    addrs = pci_addrs or list_xhci_controllers()
    if not addrs:
        return False, "xhci_hcd 컨트롤러를 찾을 수 없습니다", []
    done, errors = [], []
    for addr in addrs:
        rc, _, err = _sysfs_write(str(XHCI_DRIVER_DIR / "unbind"), addr)
        if rc != 0:
            errors.append(f"{addr} unbind: {err or 'failed'}")
            continue
        time.sleep(1.0)
        rc, _, err = _sysfs_write(str(XHCI_DRIVER_DIR / "bind"), addr)
        if rc != 0:
            errors.append(f"{addr} bind: {err or 'failed'}")
            continue
        done.append(addr)
        logger.info("USB controller rebound: %s", addr)
    if errors:
        return bool(done), "; ".join(errors), done
    return True, "OK", done
