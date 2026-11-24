"""`/dev/shm` 자세 세그먼트 — 6자유도 자세 소스의 계약 (feature/pika.md §4).

    /dev/shm/piper.pose.<id>     자세 소스(장치 확장) → 소비자(게이트웨이 PoseLeader·확장 화면)

팔 세그먼트(`piper_shm.arm`)와 같은 seqlock 이되 **한 방향**이고 레코드가 다르다. 팔 7f 에 자세를
욱여넣지 않는 이유: 자릿수(xyz+rpy+그리퍼 = 7)는 맞지만 정규화 관절 규약을 믿는 소비자(녹화·조그·
파킹·장치 화면)가 팔로 오인한다. 이름 접두사가 다르면 그들 눈에 아예 안 띈다.

## 레코드 — 장치를 모른다

- 위치는 **m**, 자세는 쿼터니언 **xyzw**, 소스 자기 좌표계 그대로(원값). 정합·축 정렬·필터는 소비자 몫이다 —
  손 수집(`ee_delta_v1`) 데이터에 텔레옵용 필터가 구워지면 안 된다.
- `opening_mm` — 손잡이 벌림. 없는 소스는 NaN.
- `tracking_ok` — 소스가 "지금 자세를 믿어도 된다" 고 말한다. 소비자는 그래도 `age_s()` 를 따로 본다.
- `encoder_ok` — 벌림 값이 살아 있다.
- `trigger_seq` — 사람이 보낸 신호(PIKA 는 손잡이 두 번 쥐기)의 **누적 횟수**. 횟수라 읽기 사이에 두 번
  와도 안 놓친다. 소스가 재시작하면 0 부터 다시 센다 — 소비자는 줄어들면 새 기준으로 삼는다.

## 정직한 제약

팔 세그먼트와 같다 — x86-64 TSO 에 기대고, 컨테이너는 `ipc: host` 여야 한다.
"""

from __future__ import annotations

import math
import mmap
import os
import struct
import time
from pathlib import Path

MAGIC = 0x5049504F          # "PIPO"
VERSION = 1

SHM_DIR = Path("/dev/shm")
NAME_PREFIX = "piper.pose."

HEADER_SIZE = 64
_HEADER_FMT = "<IIIIIIQQ"   # magic ver kind n_slots slot_bytes reserved write_seq wall_ns
assert struct.calcsize(_HEADER_FMT) <= HEADER_SIZE
_SEQ_OFF = struct.calcsize("<IIIIII")
_WALL_OFF = _SEQ_OFF + 8

KIND_POSE = 1
DEFAULT_SLOTS = 4           # 소스가 250Hz 라 팔(1kHz 라이터에 3)보다 여유가 크다

# wall_ns · xyz(m) · 쿼터니언 xyzw · 벌림 mm · 플래그 · trigger_seq · (8바이트 정렬)
_REC_FMT = "<Q3d4dfII4x"
REC_BYTES = struct.calcsize(_REC_FMT)

FLAG_TRACKING_OK = 1 << 0
FLAG_ENCODER_OK = 1 << 1


class PoseSegmentError(RuntimeError):
    """포맷이 안 맞거나 세그먼트가 없다."""


def _check_id(pose_id: str) -> str:
    # 파일 이름이 된다 — 경로 조각이 섞이면 /dev/shm 밖을 가리킨다
    if not pose_id or "/" in pose_id or pose_id.startswith(".") or len(pose_id) > 64:
        raise PoseSegmentError(f"자세 id 모양이 아닙니다: {pose_id!r}")
    return pose_id


def segment_path(pose_id: str) -> Path:
    return SHM_DIR / f"{NAME_PREFIX}{_check_id(pose_id)}"


def list_segments() -> list[str]:
    """살아 있는 자세 세그먼트의 id."""
    try:
        return sorted(p.name.removeprefix(NAME_PREFIX) for p in SHM_DIR.glob(f"{NAME_PREFIX}*"))
    except OSError:
        return []


def unlink(pose_id: str) -> bool:
    try:
        segment_path(pose_id).unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


class PoseWriter:
    """자세 소스가 쓴다. **락을 잡지 않는다** — 소스 콜백(250Hz)을 멈추지 않는 게 요점이다."""

    def __init__(self, pose_id: str, *, n_slots: int = DEFAULT_SLOTS) -> None:
        self.id = _check_id(pose_id)
        self.n_slots = n_slots
        total = HEADER_SIZE + REC_BYTES * n_slots
        self._fd = os.open(segment_path(pose_id), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            # ⚠ 만드는 쪽(확장 컨테이너, root)과 읽는 쪽(호스트 사용자·게이트웨이)이 다르다 —
            #   umask 가 깎지 못하게 못박는다. 이유는 piper_shm.arm 의 같은 자리에 길게 있다.
            os.fchmod(self._fd, 0o644)
            os.ftruncate(self._fd, total)
            self._buf = mmap.mmap(self._fd, total)
        except Exception:
            os.close(self._fd)
            raise
        self._buf[:HEADER_SIZE] = struct.pack(
            _HEADER_FMT, MAGIC, VERSION, KIND_POSE, n_slots, REC_BYTES, 0, 0, 0,
        ).ljust(HEADER_SIZE, b"\0")
        self._seq = 0

    def publish(self, pos: tuple[float, float, float], quat: tuple[float, float, float, float], *,
                opening_mm: float = math.nan, tracking_ok: bool = True, encoder_ok: bool = False,
                trigger_seq: int = 0, wall_ns: int | None = None) -> int:
        """자세 한 벌. `wall_ns` 는 **소스가 잰 시각**(없으면 지금) — 소비자가 지연을 잰다."""
        if len(pos) != 3 or len(quat) != 4:
            raise ValueError(f"위치 3·쿼터니언 4 가 필요합니다: {len(pos)}·{len(quat)}")
        flags = (FLAG_TRACKING_OK if tracking_ok else 0) | (FLAG_ENCODER_OK if encoder_ok else 0)
        payload = struct.pack(
            _REC_FMT, wall_ns if wall_ns is not None else time.time_ns(),
            *(float(v) for v in pos), *(float(v) for v in quat),
            float(opening_mm), flags, int(trigger_seq) & 0xFFFFFFFF,
        )
        nxt = self._seq + 1
        off = HEADER_SIZE + (nxt % self.n_slots) * REC_BYTES
        self._buf[off:off + REC_BYTES] = payload
        # 슬롯 기록이 먼저, seq 증가가 나중 (x86-64 TSO)
        self._buf[_SEQ_OFF:_SEQ_OFF + 8] = struct.pack("<Q", nxt)
        self._buf[_WALL_OFF:_WALL_OFF + 8] = struct.pack("<Q", time.time_ns())
        self._seq = nxt
        return nxt

    @property
    def orphaned(self) -> bool:
        """세그먼트 파일이 지워졌는가 — 지워진 뒤에도 열린 fd 로는 계속 써져서 조용히 깨진다."""
        try:
            return os.fstat(self._fd).st_nlink == 0
        except OSError:
            return True

    def close(self, unlink_segment: bool = True) -> None:
        try:
            self._buf.close()
        finally:
            os.close(self._fd)
            if unlink_segment:
                unlink(self.id)


class PoseReader:
    """소비자가 최신 자세를 읽는다. 라이터가 덮었으면 재시도한다 — 라이터를 기다리게 하지 않는다."""

    def __init__(self, pose_id: str, max_retries: int = 8) -> None:
        self.id = _check_id(pose_id)
        self.max_retries = max_retries
        path = segment_path(pose_id)
        if not path.exists():
            raise PoseSegmentError(f"자세 세그먼트가 없습니다: {path} (자세 소스가 떠 있나요?)")
        self._fd = os.open(path, os.O_RDONLY)
        try:
            size = os.fstat(self._fd).st_size
            self._buf = mmap.mmap(self._fd, size, prot=mmap.PROT_READ)
            magic, ver, kind, self.n_slots, slot_bytes, _r, _s, _w = struct.unpack(
                _HEADER_FMT, self._buf[:struct.calcsize(_HEADER_FMT)])
            # 조용히 넘기면 엉뚱한 오프셋에서 double 을 읽어 그럴듯한 쓰레기 자세가 나온다
            if magic != MAGIC:
                raise PoseSegmentError(f"자세 세그먼트 매직이 다릅니다: {magic:#x} (기대 {MAGIC:#x})")
            if ver != VERSION or kind != KIND_POSE or slot_bytes != REC_BYTES:
                raise PoseSegmentError(f"자세 세그먼트 포맷이 다릅니다: ver={ver} kind={kind} slot={slot_bytes}")
        except Exception:
            os.close(self._fd)
            raise
        self.retries = 0

    def seq(self) -> int:
        return struct.unpack("<Q", self._buf[_SEQ_OFF:_SEQ_OFF + 8])[0]

    def age_s(self) -> float:
        """마지막 기록이 몇 초 전인가 (기록 시각 기준 — 소스 시각은 레코드의 `wall_ns`)."""
        wall = struct.unpack("<Q", self._buf[_WALL_OFF:_WALL_OFF + 8])[0]
        return float("inf") if wall == 0 else max(0.0, (time.time_ns() - wall) / 1e9)

    @property
    def orphaned(self) -> bool:
        """라이터가 재시작하면 옛 mmap 은 **마지막 자세를 영원히** 낸다 — 이걸 보고 다시 연다."""
        try:
            return os.fstat(self._fd).st_nlink == 0
        except OSError:
            return True

    def read(self) -> dict | None:
        """최신 자세 또는 아직 없으면 `None`."""
        for _ in range(self.max_retries):
            s1 = self.seq()
            if s1 == 0:
                return None
            off = HEADER_SIZE + (s1 % self.n_slots) * REC_BYTES
            rec = struct.unpack(_REC_FMT, self._buf[off:off + REC_BYTES])
            if self.seq() - s1 < self.n_slots - 1:      # 라이터가 아직 안 덮었다
                wall_ns, x, y, z, qx, qy, qz, qw, opening, flags, trig = rec
                return {
                    "wall_ns": wall_ns, "pos": (x, y, z), "quat": (qx, qy, qz, qw),
                    "opening_mm": None if math.isnan(opening) else opening,
                    "tracking_ok": bool(flags & FLAG_TRACKING_OK),
                    "encoder_ok": bool(flags & FLAG_ENCODER_OK),
                    "trigger_seq": trig, "seq": s1,
                }
            self.retries += 1
        return None

    def close(self) -> None:
        try:
            self._buf.close()
        finally:
            os.close(self._fd)
