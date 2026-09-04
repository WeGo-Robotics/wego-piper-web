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
import re
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


# 버스가 건강한 상태. 이거 말고는 프레임이 안 나가거나 곧 안 나간다.
CAN_HEALTHY = "ERROR-ACTIVE"


def can_state(iface: str) -> str | None:
    """버스 상태(`ERROR-ACTIVE` / `ERROR-PASSIVE` / `BUS-OFF`). 못 읽으면 None.

    ⚠ **이게 필요한 이유: SDK 가 전송 실패를 예외로 안 준다.** 팔 전원이 꺼져
    있어도 `JointCtrl`·`EndPoseCtrl` 은 조용히 돌아오고 로그에만
    `SEND_MESSAGE_FAILED` 가 남는다 — 화면에는 "성공"으로 보인다. 실기에서
    그렇게 5번을 성공으로 보고했다.

    아무도 ACK 하지 않으면 컨트롤러가 `ERROR-PASSIVE` 로 내려간다. 그게 "팔이
    안 듣고 있다"의 결정적 증거다 — `tx_errors` 는 그때도 0 이었다.

    sysfs 에는 없다(netlink 속성이라 `ip` 를 거쳐야 한다). 3~4ms 라 명령마다는
    괜찮지만 **프레임마다 부르면 안 된다.**
    """
    try:
        out = subprocess.run(["ip", "-details", "link", "show", iface],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return None
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else None


#: `ip -details -statistics` 의 오류 카운터 이름 (그 줄의 순서 그대로).
ERROR_COUNTERS = ("restarts", "bus_errors", "arbitration_lost",
                  "error_warning", "error_passive", "bus_off")


def error_counters(iface: str) -> dict[str, int]:
    """CAN 오류 카운터. 못 읽으면 빈 dict.

    ⚠ **`can_state()` 와 다른 것을 본다.** 그쪽은 *지금* 나쁜지 보고, 이건
      *얼마나 자주* 나빴는지 본다. 순간값만 보면 잠깐 error-passive 로
      내려갔다 돌아오는 버스를 영영 못 잡는다 — 실측(can3)에서 누적
      **34,794회**였는데 물어보는 순간에는 늘 ERROR-ACTIVE 였다.

      카운터는 인터페이스를 다시 열 때 0 으로 돌아간다. 그래서 **절대값끼리
      비교하면 안 된다** — 같은 시각에 올라온 것끼리만 뜻이 있다(실측: can2 와
      can3 이 1초 차이로 올라왔고 각각 0 과 34,794였다).
    """
    try:
        out = subprocess.run(["ip", "-details", "-statistics", "link", "show", iface],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return {}
    m = re.search(r"re-started bus-errors arbit-lost error-warn error-pass bus-off\s*\n\s*"
                  r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", out)
    if not m:
        return {}
    return dict(zip(ERROR_COUNTERS, (int(g) for g in m.groups())))


def _stat(iface: str, name: str) -> int | None:
    try:
        return int(Path(f"/sys/class/net/{iface}/statistics/{name}").read_text().strip())
    except Exception:
        return None


def adapter_serial(iface: str) -> str | None:
    """이 인터페이스를 만드는 **USB 어댑터의 시리얼.**

    ⚠ **팔의 시리얼이 아니다.** Piper 는 CAN 으로 시리얼을 신고하지 않는다 —
    SDK 프로토콜에 그런 필드가 없다. 이것은 "어느 케이블에 물려 있었나" 이고,
    배선을 안 바꾸는 한 그 팔을 가리킨다. 팔 자체의 식별은 사람이 적어야 한다.
    """
    try:
        dev = Path(f"/sys/class/net/{iface}/device").resolve()
        return (dev.parent / "serial").read_text().strip() or None
    except Exception:
        return None


def bus_stats(iface: str) -> dict:
    """이 버스의 지금 상태 + 누적 오류 + 트래픽. 화면의 버스 상태 탭이 쓴다.

    ⚠ **`ip` 를 한 번만 부른다.** 예전에는 상태·비트레이트·카운터를 따로 물어
    인터페이스마다 세 번이었다 — 데몬이 2초마다 네 버스를 재면 초당 여섯 번의
    프로세스 생성이다. 한 출력에 셋이 다 들어 있으므로 나눌 이유가 없었다.

    ⚠ **카운터만 보여주면 오독한다.** 인터페이스를 다시 열면 0 으로 돌아가므로
    절대값끼리 비교하면 안 된다 — 실측: can2 와 can3 이 1초 차이로 올라왔는데
    각각 0 과 34,794 였다. 그래서 **트래픽(rx_packets)을 같이 낸다.** 백만
    프레임당 오류로 환산하면 가동 시간이 달라도 비교가 된다.
    """
    try:
        out = subprocess.run(["ip", "-details", "-statistics", "link", "show", iface],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        out = ""
    m = re.search(r"can state (\S+)", out)
    state = m.group(1) if m else None
    mb = re.search(r"bitrate (\d+)", out)
    mc = re.search(r"re-started bus-errors arbit-lost error-warn error-pass bus-off\s*\n\s*"
                   r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", out)
    counters = dict(zip(ERROR_COUNTERS, (int(g) for g in mc.groups()))) if mc else {}
    return {
        "iface": iface,
        "state": state,
        "healthy": state == CAN_HEALTHY,
        "bitrate": int(mb.group(1)) if mb else None,
        "counters": counters,
        "errors_total": sum(counters.values()) if counters else None,
        "rx_packets": _stat(iface, "rx_packets"),
        "tx_packets": _stat(iface, "tx_packets"),
        "rx_errors": _stat(iface, "rx_errors"),
        "tx_errors": _stat(iface, "tx_errors"),
        "rx_dropped": _stat(iface, "rx_dropped"),
        "tx_dropped": _stat(iface, "tx_dropped"),
    }


def can_unhealthy_reason(iface: str) -> str | None:
    """버스가 나쁘면 사람이 읽을 사유, 괜찮으면 None."""
    state = can_state(iface)
    if state is None or state == CAN_HEALTHY:
        return None
    if state == "BUS-OFF":
        return f"{iface} 버스가 꺼졌습니다(BUS-OFF) — 팔 전원과 CAN 케이블을 보세요"
    return (f"{iface} 버스가 {state} 입니다 — 명령이 안 나가고 있습니다. "
            "팔 전원과 CAN 케이블을 보세요")


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


def reset_bus(iface: str, bitrate: int = DEFAULT_BITRATE) -> dict:
    """CAN 버스를 내렸다 올려 컨트롤러를 다시 세운다.

    ⚠ **오류 카운터는 안 지워진다.** gs_usb 에서 down/up 을 해도 누적값이
    그대로다(실측: 130,730,379 → 130,730,379). 그래서 "초기화" 가 카운터를
    0 으로 만든다고 말하면 거짓말이다 — 대신 부르는 쪽이 **기준선**을 잡아
    "이 시점 이후" 로 보게 한다.

    ⚠ **`restart-ms` 도 못 쓴다.** 이 어댑터는 BUS-OFF 자동 복구를 지원하지
    않는다(`Device doesn't support restart from Bus Off`). 그래서 BUS-OFF 가
    나면 사람이 이 버튼을 눌러야 한다 — 그게 이 기능이 있는 이유의 절반이다.

    ⚠ **연결이 끊긴다.** 이 인터페이스를 쥔 SDK 핸들은 낡은 것이 되므로, 부르는
    쪽이 팔을 다시 연결해야 한다.
    """
    ok, msg = init_can_interface(iface, bitrate)
    return {"ok": ok, "error": None if ok else msg, "iface": iface,
            "state": can_state(iface), "counters": error_counters(iface)}


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
