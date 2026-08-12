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

# 슬롯 수. 3이면 30fps 라이터(33ms)에 1080p 복사(0.25ms)가 겹칠 일이 사실상 없다.
DEFAULT_SLOTS = 3

# dtype 코드 — numpy 의존 없이 헤더에 담기 위해 정수로 둔다.
DTYPE_UINT8 = 1
_DTYPE_ITEMSIZE = {DTYPE_UINT8: 1}


def segment_for_camera(cam_id: str) -> str:
    """카메라 장치 id → 세그먼트 이름.

    ⚠ **LeRobot 카메라 키(`top`)가 아니라 장치 기준이다.**
    발행자(camerad/rsd)는 어떤 실행에 어떤 키로 쓰일지 모른 채 **항상** 발행한다.
    키를 이름으로 쓰면 발행자가 매핑을 알아야 하고, 매핑이 바뀔 때마다
    세그먼트를 다시 만들어야 한다 — 데몬 모델과 맞지 않는다.

    소비자는 `{"top": {"type": "shm", "segment": "rs_250122070363_color"}}` 처럼
    **키는 dict 키로, 세그먼트는 장치로** 지정한다.

        rs:250122070363:color  →  rs_250122070363_color
        /dev/video0            →  dev_video0
    """
    return cam_id.replace(":", "_").replace("/", "_").strip("_")


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

    def pack_header(self, write_seq: int = 0, wall_ns: int = 0) -> bytes:
        return struct.pack(
            _HEADER_FMT, MAGIC, VERSION, self.width, self.height, self.channels,
            self.dtype, self.n_slots, self.slot_bytes, write_seq, wall_ns,
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
    """세그먼트를 만들고 헤더를 쓴다. 이미 있으면 크기를 맞춰 재사용한다."""
    path = segment_path(name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.ftruncate(fd, layout.total_bytes)
        buf = mmap.mmap(fd, layout.total_bytes)
    except Exception:
        os.close(fd)
        raise
    buf[:HEADER_SIZE] = layout.pack_header()
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
