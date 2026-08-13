"""`/dev/shm` 로봇팔 세그먼트 — 포맷의 단일 정의 (refactor/robot-transport.md "세그먼트 설계").

카메라(`piper_shm.segment`)와 같은 seqlock 을 쓰되 **방향이 둘이다.**

    /dev/shm/piper.arm.<iface>.state     robotd  → 소비자   (관절 상태)
    /dev/shm/piper.arm.<iface>.action    소비자 → robotd    (목표 위치)

이 포맷을 세 쪽이 공유한다 — robotd(발행/소비), LeRobot 프록시 드라이버,
게이트웨이(상태 조회). 한쪽만 고치면 **조용히 어긋난다**: 관절 값은 어떤 쓰레기가
들어와도 그럴듯해 보이므로 포맷 불일치가 로그에 안 남는다. 그래서 파일 하나에 못 박고
매직·버전을 검사한다.

## 흐르는 값은 **정규화된 값**이다

캘리브레이션의 단일 소유자는 robotd 다 (refactor/robot-transport.md 위험 #2).
프록시가 자기 캘리브레이션을 들면 `05-joint-calibration.md` 가 지적한 중복이
2곳에서 3곳으로 는다. 그래서 shm 에는 정규화된 값만 흐르고, 프록시의
`_normalize`/`_unnormalize` 는 항등이 된다.

## 카메라와 크기가 다르다

관절 한 벌은 40바이트다 — 카메라 프레임(6MB)과 달리 복사 비용이 나노초라
전송 방식 자체가 성능 문제가 되지 않는다. seqlock 을 쓰는 이유는 속도가 아니라
**찢어진 레코드를 막기 위해서다**: joint1~3 은 새 값, joint4~6 은 옛 값인 자세는
어느 쪽도 아닌 자세이고, 그게 그대로 명령이 되면 팔이 튄다.

## 정직한 제약

- x86-64 는 TSO 라 "레코드 기록 → seq 증가" 재배열이 없다.
  **ARM(Jetson 등)으로 옮기면 명시적 배리어가 필요하다.**
- 컨테이너가 `/dev/shm` 을 공유해야 한다 (`ipc: host`).
"""

from __future__ import annotations

import mmap
import os
import struct
import time
from pathlib import Path

MAGIC = 0x50495041          # "PIPA"
VERSION = 1

SHM_DIR = Path("/dev/shm")
NAME_PREFIX = "piper.arm."

# ⚠ **순서가 계약이다.** 레코드는 이름표 없이 값만 담으므로 이 순서가 곧 의미다.
# `lerobot_robot_piper.motors.PiperMotorsBus` 의 모터 이름과 같아야 한다
# (test_arm_segment.py 가 대조한다).
JOINTS: tuple[str, ...] = (
    "joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "gripper",
)

KIND_STATE = 1
KIND_ACTION = 2

# 헤더는 캐시라인(64B)에 맞춘다 — 슬롯 시작이 정렬돼야 한다.
HEADER_SIZE = 64
_HEADER_FMT = "<IIIIIIQQ"   # magic ver kind n_slots slot_bytes deadman_ms write_seq wall_ns
assert struct.calcsize(_HEADER_FMT) <= HEADER_SIZE

_SEQ_OFF = struct.calcsize("<IIIIII")        # write_seq 앞까지
_WALL_OFF = _SEQ_OFF + 8

# 슬롯 3개면 1kHz 라이터에도 리더가 한 바퀴 뒤처질 일이 사실상 없다.
DEFAULT_SLOTS = 3

# 상태 레코드: 관절 7개(정규화 float32) + 에러 비트 + 제어 모드 + CAN 수신 시각.
# `can_wall_ns` 를 실어야 소비자가 **상태의 신선도**를 판단할 수 있다 — 지금은
# piper_sdk 캐시를 읽으면서 그게 얼마나 오래된 값인지 알 방법이 없다.
_STATE_FMT = "<7fHBxQ"      # values, err_code, ctrl_mode, pad, can_wall_ns
STATE_BYTES = struct.calcsize(_STATE_FMT)

# 명령 레코드: 목표 관절 7개 + 발행 시각. `issued_wall_ns` 로 robotd 가
# **얼마나 묵은 명령인지** 보고 오래된 것을 버릴 수 있다.
_ACTION_FMT = "<7fQ"
ACTION_BYTES = struct.calcsize(_ACTION_FMT)

_SLOT_BYTES = {KIND_STATE: STATE_BYTES, KIND_ACTION: ACTION_BYTES}


class ArmSegmentError(RuntimeError):
    """포맷이 안 맞거나 세그먼트가 없다."""


def segment_name(iface: str, kind: int) -> str:
    """CAN 인터페이스 + 방향 → 세그먼트 이름.

    ⚠ **로봇 이름(`follower`)이 아니라 인터페이스(`can0`) 기준이다.**
    카메라를 장치 기준으로 이름 붙인 것과 같은 이유다 — robotd 는 어떤 실행에서
    어떤 역할로 쓰일지 모른 채 CAN 을 영구 소유하고 항상 발행한다.
    """
    suffix = {KIND_STATE: "state", KIND_ACTION: "action"}[kind]
    return f"{iface.replace('/', '_').strip('_')}.{suffix}"


def segment_path(name: str) -> Path:
    return SHM_DIR / f"{NAME_PREFIX}{name}"


def list_segments() -> list[str]:
    """살아 있는 팔 세그먼트. **존재 자체가 lease 다** — 누군가 그 CAN 을 쥐고 있다."""
    try:
        return sorted(p.name.removeprefix(NAME_PREFIX) for p in SHM_DIR.glob(f"{NAME_PREFIX}*"))
    except OSError:
        return []


def unlink(name: str) -> bool:
    """세그먼트 제거. **누락하면 `/dev/shm` 에 누수가 남는다.**"""
    try:
        segment_path(name).unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


def _total_bytes(kind: int, n_slots: int) -> int:
    return HEADER_SIZE + _SLOT_BYTES[kind] * n_slots


def _pack_header(kind: int, n_slots: int, deadman_ms: int) -> bytes:
    return struct.pack(
        _HEADER_FMT, MAGIC, VERSION, kind, n_slots, _SLOT_BYTES[kind], deadman_ms, 0, 0,
    ).ljust(HEADER_SIZE, b"\0")


def _read_header(buf) -> tuple[int, int, int, int]:
    """(kind, n_slots, deadman_ms, slot_bytes). 매직/버전이 다르면 **즉시** 실패한다.

    조용히 넘기면 엉뚱한 오프셋에서 float 를 읽어 **그럴듯한 쓰레기 자세**가 나온다.
    """
    magic, ver, kind, n_slots, slot_bytes, deadman, _seq, _wall = struct.unpack(
        _HEADER_FMT, buf[:struct.calcsize(_HEADER_FMT)]
    )
    if magic != MAGIC:
        raise ArmSegmentError(f"팔 세그먼트 매직이 다릅니다: {magic:#x} (기대 {MAGIC:#x})")
    if ver != VERSION:
        raise ArmSegmentError(f"팔 세그먼트 버전이 다릅니다: {ver} (기대 {VERSION})")
    if _SLOT_BYTES.get(kind) != slot_bytes:
        raise ArmSegmentError(f"슬롯 크기가 kind={kind} 와 안 맞습니다: {slot_bytes}")
    return kind, n_slots, deadman, slot_bytes


class _Writer:
    """seqlock 라이터. **락을 잡지 않는다** — 제어 루프를 멈추지 않는 게 요점이다."""

    _kind: int
    _fmt: str

    def __init__(self, iface: str, *, n_slots: int = DEFAULT_SLOTS,
                 deadman_ms: int = 0) -> None:
        self.iface = iface
        self.name = segment_name(iface, self._kind)
        self.n_slots = n_slots
        self.slot_bytes = _SLOT_BYTES[self._kind]
        total = _total_bytes(self._kind, n_slots)
        path = segment_path(self.name)
        self._fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.ftruncate(self._fd, total)
            self._buf = mmap.mmap(self._fd, total)
        except Exception:
            os.close(self._fd)
            raise
        self._buf[:HEADER_SIZE] = _pack_header(self._kind, n_slots, deadman_ms)
        self._seq = 0

    def _write_slot(self, payload: bytes) -> int:
        nxt = self._seq + 1
        off = HEADER_SIZE + (nxt % self.n_slots) * self.slot_bytes
        self._buf[off:off + self.slot_bytes] = payload
        # 슬롯 기록이 먼저, seq 증가가 나중 (x86-64 TSO)
        self._buf[_SEQ_OFF:_SEQ_OFF + 8] = struct.pack("<Q", nxt)
        self._buf[_WALL_OFF:_WALL_OFF + 8] = struct.pack("<Q", time.time_ns())
        self._seq = nxt
        return nxt

    @property
    def seq(self) -> int:
        return self._seq

    def close(self, unlink_segment: bool = True) -> None:
        """⚠ `unlink_segment=False` 로 두면 `/dev/shm` 에 누수가 남는다."""
        try:
            self._buf.close()
        finally:
            os.close(self._fd)
            if unlink_segment:
                unlink(self.name)


class _Reader:
    """seqlock 리더. 라이터가 덮었으면 **재시도한다** — 라이터를 기다리게 하지 않는다."""

    _kind: int
    _fmt: str

    def __init__(self, iface: str, max_retries: int = 8) -> None:
        self.iface = iface
        self.name = segment_name(iface, self._kind)
        self.max_retries = max_retries
        path = segment_path(self.name)
        if not path.exists():
            raise ArmSegmentError(f"팔 세그먼트가 없습니다: {path} (robotd 가 떠 있나요?)")
        self._fd = os.open(path, os.O_RDONLY)
        try:
            size = os.fstat(self._fd).st_size
            self._buf = mmap.mmap(self._fd, size, prot=mmap.PROT_READ)
            kind, self.n_slots, self.deadman_ms, self.slot_bytes = _read_header(self._buf)
        except Exception:
            os.close(self._fd)
            raise
        if kind != self._kind:
            self._buf.close()
            os.close(self._fd)
            raise ArmSegmentError(f"세그먼트 방향이 다릅니다: kind={kind} (기대 {self._kind})")
        self._last_seq = 0
        self.retries = 0        # 진단용 — 비정상적으로 높으면 라이터가 너무 빠르다

    def seq(self) -> int:
        return struct.unpack("<Q", self._buf[_SEQ_OFF:_SEQ_OFF + 8])[0]

    def wall_ns(self) -> int:
        return struct.unpack("<Q", self._buf[_WALL_OFF:_WALL_OFF + 8])[0]

    def age_s(self) -> float:
        """마지막 기록이 몇 초 전인가. 데드맨·신선도 판정에 쓴다."""
        wall = self.wall_ns()
        return float("inf") if wall == 0 else max(0.0, (time.time_ns() - wall) / 1e9)

    def _read_slot(self) -> tuple[tuple, int] | None:
        for _ in range(self.max_retries):
            s1 = self.seq()
            if s1 == 0:
                return None
            off = HEADER_SIZE + (s1 % self.n_slots) * self.slot_bytes
            rec = struct.unpack(self._fmt, self._buf[off:off + self.slot_bytes])
            if self.seq() - s1 < self.n_slots - 1:      # 라이터가 아직 안 덮었다
                self._last_seq = s1
                return rec, s1
            self.retries += 1
        return None

    def read(self) -> dict | None:
        raise NotImplementedError

    def read_new(self, timeout_s: float = 1.0, poll_s: float = 0.001) -> dict | None:
        """**새** 레코드를 기다렸다 읽는다. 알림은 `write_seq` 폴링이다.

        ⚠ 기반 클래스에 둔다 — 예전에 `StateReader` 에만 있어서 명령을 소비하는
        쪽이 `AttributeError` 로 매번 재접속했다. 방향이 둘인 세그먼트라
        **양쪽 다 필요한 동작은 양쪽 다 가져야 한다.**
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if self.seq() > self._last_seq:
                got = self.read()
                if got is not None:
                    return got
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_s)

    def close(self) -> None:
        try:
            self._buf.close()
        finally:
            os.close(self._fd)


class StateWriter(_Writer):
    """robotd 가 CAN 캐시를 읽어 관절 상태를 흘린다."""

    _kind = KIND_STATE
    _fmt = _STATE_FMT

    def publish(self, values: dict[str, float], err_code: int = 0,
                ctrl_mode: int = 0, can_wall_ns: int | None = None) -> int:
        """관절 한 벌 발행. 반환값은 새 `write_seq`.

        키가 빠져 있으면 **조용히 0으로 채우지 않는다** — 0은 정규화 좌표에서
        "가운데"라 그럴듯해 보이고, 그게 명령으로 되돌아오면 팔이 움직인다.
        """
        missing = [j for j in JOINTS if j not in values]
        if missing:
            raise ValueError(f"관절 값이 빠졌습니다: {missing}")
        payload = struct.pack(
            self._fmt, *(float(values[j]) for j in JOINTS),
            int(err_code) & 0xFFFF, int(ctrl_mode) & 0xFF,
            can_wall_ns if can_wall_ns is not None else time.time_ns(),
        )
        return self._write_slot(payload)


class StateReader(_Reader):
    """프록시 드라이버·게이트웨이가 최신 관절 상태를 읽는다."""

    _kind = KIND_STATE
    _fmt = _STATE_FMT

    def read(self) -> dict | None:
        """`{values, err_code, ctrl_mode, can_wall_ns, seq}` 또는 아직 없으면 `None`."""
        got = self._read_slot()
        if got is None:
            return None
        rec, seq = got
        return {
            "values": dict(zip(JOINTS, rec[:len(JOINTS)], strict=True)),
            "err_code": rec[len(JOINTS)],
            "ctrl_mode": rec[len(JOINTS) + 1],
            "can_wall_ns": rec[len(JOINTS) + 2],
            "seq": seq,
        }

class ActionWriter(_Writer):
    """소비자(LeRobot 프록시)가 목표 위치를 쓴다.

    `deadman_ms` 는 **소비자가 선언하는 자기 제어 주기의 상한**이다. robotd 는
    그 시간 동안 seq 가 안 늘면 소비자가 죽었다고 보고 팔을 세운다 —
    추론 프로세스가 hang·크래시·OOM 어느 쪽으로 죽어도 걸린다.
    """

    _kind = KIND_ACTION
    _fmt = _ACTION_FMT

    def publish(self, values: dict[str, float]) -> int:
        missing = [j for j in JOINTS if j not in values]
        if missing:
            raise ValueError(f"목표 관절 값이 빠졌습니다: {missing}")
        payload = struct.pack(
            self._fmt, *(float(values[j]) for j in JOINTS), time.time_ns()
        )
        return self._write_slot(payload)


class ActionReader(_Reader):
    """robotd 가 목표 위치를 읽어 CAN 으로 보낸다."""

    _kind = KIND_ACTION
    _fmt = _ACTION_FMT

    def read(self) -> dict | None:
        got = self._read_slot()
        if got is None:
            return None
        rec, seq = got
        return {
            "values": dict(zip(JOINTS, rec[:len(JOINTS)], strict=True)),
            "issued_wall_ns": rec[len(JOINTS)],
            "seq": seq,
        }

    def is_stale(self) -> bool:
        """데드맨 판정. `deadman_ms` 가 0이면 **끄지 않고 항상 살아있다고 본다** —
        판정 불가와 안전을 헷갈리면 안 되므로, 데드맨을 원하면 소비자가 선언해야 한다."""
        return bool(self.deadman_ms) and self.age_s() * 1000.0 > self.deadman_ms
