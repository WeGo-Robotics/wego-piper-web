"""`/dev/shm` 프레임 세그먼트 — 포맷의 단일 정의.

`piper_bus` 와 같은 이유로 최상위에 둔다: 이 포맷을 **세 쪽이 공유**한다.

- 발행자: `camerad` (지금은 게이트웨이의 `camera_manager` 가 임시로 겸한다)
- 소비자: LeRobot 카메라 플러그인 `lerobot_camera_pipershm` (별도 인터프리터)
- 조회: 게이트웨이 (프리뷰·상태)

한쪽만 고치면 조용히 어긋나는 종류의 값이라 파일 하나에 못 박는다
(refactor/camera-transport.md "세그먼트 설계").

## 왜 shm 인가

base64-JPEG 를 ZMQ 로 흘리는 안은 실측에서 왕복 비용이 컸고, 무엇보다 **JPEG 이중압축**이
된다 — 데이터셋에 raw 가 아니라 한 번 손실된 프레임이 들어간다.
shm 은 memcpy 1회로 끝나고 픽셀이 원본 그대로 남는다.

## 정직한 제약

- **zero-copy 가 아니다.** 리더가 자기 버퍼로 복사한다(seqlock 을 성립시키려면 필요).
- **컨테이너가 `/dev/shm` 을 공유해야 한다** (`ipc: host`).
- x86-64 는 TSO 라 "프레임 기록 → seq 증가" 재배열이 없다.
  **ARM(Jetson 등)으로 옮기면 명시적 배리어가 필요하다.**
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = 0x50495043          # "PIPC"
VERSION = 1

SHM_DIR = Path("/dev/shm")
NAME_PREFIX = "piper.cam."

# 헤더는 캐시라인(64B)에 맞춘다 — 슬롯 시작이 정렬돼야 memcpy 가 빠르다.
HEADER_SIZE = 64
_HEADER_FMT = "<IIIIIIIQQQ"     # magic ver w h ch dtype n_slots slot_bytes write_seq wall_ns
assert struct.calcsize(_HEADER_FMT) <= HEADER_SIZE

# **발행자 pid** — 헤더 끝의 빈 칸(52~56)이다. 앞부분 포맷은 그대로라 옛 리더도 그대로 읽는다
# (그래서 VERSION 을 올리지 않았다). 옛 발행자가 만든 세그먼트는 이 칸이 0 이다.
#
# 이름이 사용자 몫이 된 뒤로 **접두사로는 누구 것인지 알 수 없다**(`dev_`·`rs_` 규칙이 사라졌다).
# 시작할 때 "죽은 발행자의 잔재"를 치우는 근거가 이 pid 다 (`dead_segments`).
_PID_OFF = struct.calcsize(_HEADER_FMT)
_PID_FMT = "<I"
assert _PID_OFF + struct.calcsize(_PID_FMT) <= HEADER_SIZE

# 슬롯 수. 3이면 30fps 라이터(33ms)에 1080p 복사(0.25ms)가 겹칠 일이 사실상 없다.
DEFAULT_SLOTS = 3

# dtype 코드 — numpy 의존 없이 헤더에 담기 위해 정수로 둔다.
DTYPE_UINT8 = 1
_DTYPE_ITEMSIZE = {DTYPE_UINT8: 1}


def rule_segment_name(cam_id: str) -> str:
    """장치 id → **규칙** 이름. 이름 지정(`set_segment_name`)을 보지 않는다.

        rs:250122070363:color  →  rs_250122070363_color
        /dev/video0            →  dev_video0

    이름이 지정되지 않은 카메라(스캔 중인 미등록 장치, 옛 게이트웨이가 연결한 카메라)가
    발행되는 이름이다.
    """
    return cam_id.replace(":", "_").replace("/", "_").strip("_")


# 카메라 id → 세그먼트 이름. **프로세스마다 따로** 있다 — 게이트웨이는 데몬이 확인해 준
# 이름을, 데몬은 게이트웨이가 정해 준 이름을 든다.
_NAME_OVERRIDES: dict[str, str] = {}


def set_segment_name(cam_id: str, name: str) -> None:
    """이 카메라가 발행되는(또는 발행될) 세그먼트 이름을 정한다.

    같은 프로세스의 다른 카메라가 이미 그 이름이면 `ValueError` — 한 세그먼트에는 라이터가
    하나여야 한다(단일 라이터 seqlock). 두 발행자가 같은 이름에 쓰면 `write_seq` 가 깨져
    리더가 찢어진 프레임을 본다.
    """
    for other, taken in _NAME_OVERRIDES.items():
        if other != cam_id and taken == name:
            raise ValueError(f"세그먼트 이름 '{name}' 은(는) 다른 카메라({other})가 쓰고 있습니다")
    _NAME_OVERRIDES[cam_id] = name


def clear_segment_name(cam_id: str) -> None:
    """이름 지정을 푼다 — 규칙 이름으로 돌아간다."""
    _NAME_OVERRIDES.pop(cam_id, None)


def clear_segment_names() -> None:
    _NAME_OVERRIDES.clear()


def segment_overrides() -> dict[str, str]:
    return dict(_NAME_OVERRIDES)


def segment_for_camera(cam_id: str) -> str:
    """카메라 장치 id → 세그먼트 이름.

    ⚠ **LeRobot 카메라 키(`top`)가 아니라 장치 기준이다.** 발행자(camerad/rsd)는 어떤
    실행에 어떤 키로 쓰일지 모른 채 **항상** 발행한다. 키를 이름으로 쓰면 발행자가 매핑을
    알아야 하고, 매핑이 바뀔 때마다 세그먼트를 다시 만들어야 한다 — 데몬 모델과 맞지 않는다.

    이름은 두 층이다:

    1. **지정된 이름** — 사용자가 카메라에 붙인 스트림 이름(`top`). 게이트웨이가 데몬에
       정해 주고(`set_segment_name`), 데몬이 그 이름으로 발행한 것을 확인하면 게이트웨이도
       같은 이름을 든다. 그래서 이 함수를 부르는 모든 곳이 **고칠 것 없이** 맞는 이름을 본다.
    2. **규칙 이름** — 지정이 없으면 장치 id 에서 규칙으로(`rule_segment_name`).

    소비자는 `{"top": {"type": "shm", "segment": "<이 함수의 값>"}}` 처럼
    **키는 dict 키로, 세그먼트는 장치로** 지정한다.

    이미 세그먼트 이름인 값을 넣으면 그대로 돌아온다(멱등) — 이름 규칙이 `:`·`/` 와
    앞뒤 `_` 를 막으므로 규칙이 건드릴 글자가 없다.
    """
    return _NAME_OVERRIDES.get(cam_id) or rule_segment_name(cam_id)


def segment_path(name: str) -> Path:
    """카메라 이름 → `/dev/shm` 경로. 카메라당 하나라 수명이 독립적이다."""
    return SHM_DIR / f"{NAME_PREFIX}{name}"


def segment_name(path: str | Path) -> str:
    return Path(path).name.removeprefix(NAME_PREFIX)


def list_segments() -> list[str]:
    """살아 있는 세그먼트의 카메라 이름.

    **세그먼트의 존재 자체가 lease 다** — 그 카메라를 누군가 잡고 있다는 뜻이다
    (refactor/daemon-split.md 4단계의 장치 소유권 프로토콜).
    """
    try:
        return sorted(
            p.name.removeprefix(NAME_PREFIX)
            for p in SHM_DIR.glob(f"{NAME_PREFIX}*")
        )
    except OSError:
        return []


@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    channels: int = 3
    dtype: int = DTYPE_UINT8
    n_slots: int = DEFAULT_SLOTS

    @property
    def slot_bytes(self) -> int:
        return self.width * self.height * self.channels * _DTYPE_ITEMSIZE[self.dtype]

    @property
    def total_bytes(self) -> int:
        return HEADER_SIZE + self.slot_bytes * self.n_slots

    def pack_header(self, write_seq: int = 0, wall_ns: int = 0, pid: int = 0) -> bytes:
        return (
            struct.pack(
                _HEADER_FMT, MAGIC, VERSION, self.width, self.height, self.channels,
                self.dtype, self.n_slots, self.slot_bytes, write_seq, wall_ns,
            )
            + struct.pack(_PID_FMT, pid)
        ).ljust(HEADER_SIZE, b"\0")


class SegmentError(RuntimeError):
    """포맷이 안 맞거나 세그먼트가 없다."""


def read_header(buf: mmap.mmap) -> tuple[Layout, int, int]:
    """(레이아웃, write_seq, wall_ns). 매직/버전이 다르면 즉시 실패한다.

    조용히 넘기면 엉뚱한 크기로 읽어 쓰레기 프레임이 정책에 들어간다.
    """
    magic, ver, w, h, ch, dtype, n_slots, slot_bytes, seq, wall = struct.unpack(
        _HEADER_FMT, buf[:struct.calcsize(_HEADER_FMT)]
    )
    if magic != MAGIC:
        raise SegmentError(f"세그먼트 매직이 다릅니다: {magic:#x} (기대 {MAGIC:#x})")
    if ver != VERSION:
        raise SegmentError(f"세그먼트 버전이 다릅니다: {ver} (기대 {VERSION})")
    layout = Layout(width=w, height=h, channels=ch, dtype=dtype, n_slots=n_slots)
    if layout.slot_bytes != slot_bytes:
        raise SegmentError("헤더의 slot_bytes 가 크기 계산과 다릅니다")
    return layout, seq, wall


def create(name: str, layout: Layout) -> tuple[int, mmap.mmap]:
    """세그먼트를 만들고 헤더를 쓴다. 이미 있고 **크기가 같으면** 그 파일을 재사용한다.

    ⚠ **크기가 다른 파일을 제자리에서 줄이지 않는다.** 옛 크기로 매핑해 둔 소비자가 잘린 꼬리를
    읽는 순간 SIGBUS 로 **프로세스째** 죽는다 — 예외가 아니라 신호라 잡을 수 없다. RealSense 는
    해상도를 바꿀 때 세그먼트를 일부러 남기므로(`_ensure_streams`) 다음 프레임이 같은 이름으로
    여기에 온다. 2026-10-29 실기: top_1 을 1920x1080 → 640x480 으로 바꾸자 미리보기를 읽던
    게이트웨이가 코어덤프했다(녹화·추론 중이었다면 heartbeat 가 끊겨 E-stop).

    그래서 새 파일을 옆 이름으로 다 만든 뒤 `rename` 으로 갈아 끼운다. 옛 매핑은 옛 inode 를
    그대로 읽다가 `orphaned` 로 바뀐 것을 안다. 없던 세그먼트도 같은 길로 만든다 — 크기 0 인
    파일이 잠깐이라도 보이면 그 순간 여는 쪽이 `cannot mmap an empty file` 로 실패한다.
    """
    path = segment_path(name)
    try:
        reuse = path.stat().st_size == layout.total_bytes
    except FileNotFoundError:
        reuse = False
    # 점으로 시작 — `list_segments` 의 glob 에 안 잡힌다
    target = path if reuse else path.with_name(f".{path.name}.{os.getpid()}.new")
    fd = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.ftruncate(fd, layout.total_bytes)
        buf = mmap.mmap(fd, layout.total_bytes)
        buf[:HEADER_SIZE] = layout.pack_header(pid=os.getpid())
        if target != path:
            os.replace(target, path)
    except Exception:
        os.close(fd)
        if target != path:
            target.unlink(missing_ok=True)
        raise
    return fd, buf


def open_ro(name: str) -> tuple[int, mmap.mmap, Layout]:
    """읽기 전용으로 연다. 소비자(LeRobot 플러그인)가 쓴다."""
    path = segment_path(name)
    if not path.exists():
        raise SegmentError(f"세그먼트가 없습니다: {path} (camerad 가 떠 있나요?)")
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        buf = mmap.mmap(fd, size, prot=mmap.PROT_READ)
        layout, _, _ = read_header(buf)
    except Exception:
        os.close(fd)
        raise
    if layout.total_bytes != size:
        buf.close()
        os.close(fd)
        raise SegmentError(f"세그먼트 크기 불일치: {size} != {layout.total_bytes}")
    return fd, buf, layout


def unlink(name: str) -> bool:
    """세그먼트 제거. **누락하면 `/dev/shm` 에 누수가 남는다.**"""
    try:
        segment_path(name).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def has_readers(name: str) -> bool:
    """이 세그먼트를 **소비자로** 열어 둔 곳이 있는가 (`Subscriber(observer=False)`).

    읽는 쪽은 `PROT_READ` 로 매핑만 할 뿐 자기 존재를 알리지 않는다 — 그래서 소비자가 열 때
    세그먼트 파일에 `flock(LOCK_SH)` 를 건다. 여기서 `LOCK_EX|LOCK_NB` 로 **떠본다**: 막히면 누군가
    잡고 있는 것이다. 커널이 프로세스가 죽으면 락을 풀므로 하트비트도 카운터도 필요 없다.

    ⚠ **데이터 경로의 락이 아니다.** 프레임을 쓰고 읽는 것은 여전히 락 없는 seqlock 이다 —
    발행자는 이 표지를 기다리지 않고, 읽기도 막지 않는다. 이건 "누가 붙어 있나" 를 주기적으로
    확인하는 용도뿐이다.

    모르면 `True`(있다) — 닫지 않는 쪽이 안전하다. 세그먼트가 없으면 `False`.
    """
    import fcntl

    try:
        fd = os.open(segment_path(name), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def owner_pid(name: str) -> int:
    """세그먼트를 만든 발행자의 pid. 모르면 0 (옛 발행자가 만들었거나 읽을 수 없다)."""
    try:
        with open(segment_path(name), "rb") as f:
            head = f.read(_PID_OFF + struct.calcsize(_PID_FMT))
        if len(head) < _PID_OFF + struct.calcsize(_PID_FMT):
            return 0
        return struct.unpack_from(_PID_FMT, head, _PID_OFF)[0]
    except OSError:
        return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # 다른 사용자의 프로세스 — 살아 있다
    except OSError:
        return False
    return True


def dead_segments(legacy_prefix: str | None = None) -> list[str]:
    """**발행자가 죽은** 세그먼트 이름들 — 시작할 때 치울 잔재.

    예전에는 데몬이 "내 접두사(`dev_`·`rs_`)로 시작하는 것"을 치웠다. 이름이 사용자 몫이 된
    뒤로는 접두사가 소유를 말해 주지 못한다. 그래서 **헤더의 발행자 pid** 로 본다:

    - pid 가 있고 **살아 있으면** 남의 것이다 — 건드리지 않는다(다른 데몬이 발행 중이다).
    - pid 가 있고 **죽었으면** 잔재다 — 소비자가 열면 **멈춘 화면**을 본다.
    - pid 가 없으면(0, 옛 발행자) 소유를 알 수 없다. `legacy_prefix` 로 시작하는 것만
      잔재로 본다 — 옛 규칙 이름이다. 접두사가 없으면 안 건드린다.

    ⚠ pid 재사용으로 죽은 발행자의 pid 를 무관한 프로세스가 물려받으면 잔재로 안 잡힌다.
    드문 일이고, 그 세그먼트는 `wall_ns` 가 멈춰 있어 감시가 "멈췄다"로 말해 준다.
    """
    out = []
    for name in list_segments():
        pid = owner_pid(name)
        if pid:
            if not pid_alive(pid):
                out.append(name)
        elif legacy_prefix and name.startswith(legacy_prefix):
            out.append(name)
    return out
