#!/usr/bin/env python3
"""barcode — 카메라 프레임에서 바코드·QR 을 읽어 검출로 버스에 올린다.

디코더는 zxing-cpp(Apache-2.0) — 1D(Code128·EAN·UPC·Code39·ITF …)와 2D(QR·DataMatrix·PDF417·Aztec)를
한 번에 읽는다. OpenCV 내장 디코더는 같은 시험 이미지 다섯 장(Code128·EAN-13·QR·DataMatrix·Code39)에서
QR 하나만 읽어 쓰지 않는다. `python: venv` 라 상세의 [의존 설치] 가 먼저다.

- **입력**: 카메라 슬롯 `watch` 의 `/dev/shm` 프레임(**BGR**). 카메라를 열지 않는다.
- **출력**: 검출 `barcode` — 물체 하나가 코드 하나, `label` 이 읽은 글자, `format` 이 형식이다.
  다른 플러그인은 `self.detections("barcode")`(또는 `kind: detections` 입력 슬롯)로, LLM 은 `text` 로 읽는다.
  프리뷰 `det_barcode` 에 테두리와 글자를 그린다.

    piper-plugin run plugins/barcode --camera watch=<스트림> --config '{"formats": "Code128, QRCode"}'
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from piper_plugin import Plugin
from piper_plugin.detections import payload

try:
    import zxingcpp
except ImportError:          # 터미널의 시스템 파이썬 — setup 이 사람 말로 멈춘다
    zxingcpp = None


def read_codes(frame, formats=None) -> list[dict]:
    """프레임(BGR 또는 회색) → `[{text, format, corners}]`. `corners` 는 픽셀 네 점(좌상·우상·우하·좌하)."""
    out = []
    # ⚠ 3.x 바인딩은 docstring 과 달리 `formats=None` 을 받지 않는다 — 전부 읽을 땐 인자를 빼야 한다
    kw = {"formats": formats} if formats is not None else {}
    for r in zxingcpp.read_barcodes(frame, **kw):
        if not r.valid or not r.text:
            continue
        p = r.position
        out.append({"text": r.text, "format": str(r.format),
                    "corners": [[q.x, q.y] for q in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)]})
    return out


def codes_payload(cam: str, seq: int, wall_ns: int, shape: tuple[int, int], codes: list[dict],
                  det_seq: int | None = None, publisher: str | None = None) -> dict:
    """읽은 코드 → 검출 페이로드. 모양은 검출기와 같은 `payload()` 가 만든다 — 화면·LLM 이 따로 배우지 않는다.

    디코딩은 체크섬을 통과해야 나오므로 `conf` 는 1.0 이다. `format` 은 계약 밖 키라 덧붙인다(옛 소비자는 무시).
    """
    xyxy = []
    for c in codes:
        xs = [x for x, _ in c["corners"]]
        ys = [y for _, y in c["corners"]]
        xyxy.append([float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))])
    out = payload(cam, seq, wall_ns, shape, {i: c["text"] for i, c in enumerate(codes)},
                  xyxy, [1.0] * len(codes), list(range(len(codes))),
                  det_seq=det_seq, publisher=publisher, model="zxing-cpp")
    fmt = {c["text"]: c["format"] for c in codes}
    for o in out["objects"]:
        o["format"] = fmt[o["label"]]
    return out


def draw(frame, codes: list[dict]):
    img = frame.copy()
    for c in codes:
        pts = np.array(c["corners"], np.int32)
        cv2.polylines(img, [pts], True, (0, 255, 0), 2)
        x, y = pts.min(axis=0)
        # putText 는 ASCII 만 그린다 — 한글은 ? 로 나온다. 원문은 검출 페이로드에 있다
        cv2.putText(img, f"{c['format']}: {c['text'][:40]}", (int(x), max(int(y) - 6, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    return img


class Barcode(Plugin):
    def setup(self):
        if zxingcpp is None:
            self.stop("zxing-cpp 가 없다 — 플러그인 상세의 [의존 설치] 를 먼저 누르라")
        spec = str(self.config.get("formats") or "").strip()
        try:
            self.formats = zxingcpp.barcode_formats_from_str(spec) if spec else None
        except ValueError as e:
            self.stop(f"읽을 형식을 모른다: {spec} ({e})")
        self.cam = self.camera("watch")
        self.interval = 1.0 / max(float(self.config["fps"]), 0.1)
        self.repeat_s = float(self.config["repeat_s"])
        self.preview = bool(self.config.get("preview", True))
        self.recent: dict[str, float] = {}       # 글자 → 마지막으로 본 시각 — 같은 코드를 프레임마다 알리지 않는다
        self.reads = 0
        self.det_seq = 0
        self._last = time.monotonic()
        self.log.info("읽을 형식: %s", self.formats or "전부")
        self.state(phase="scanning", reads=0, last="")

    def loop(self):
        t0 = time.monotonic()
        got = self.cam.read_new(timeout_s=1.0)
        if got is None:
            self.state(phase="waiting_camera")
            return
        frame, seq, wall_ns = got
        codes = read_codes(frame, self.formats)
        self.det_seq += 1
        self.publish_detections(self.name, frame.shape[:2], payload=codes_payload(
            self.name, seq, wall_ns, frame.shape[:2], codes, det_seq=self.det_seq, publisher=self.name))
        if self.preview:
            self.publish_preview(f"det_{self.name}", draw(frame, codes) if codes else frame)

        now = time.monotonic()
        for c in codes:
            seen = self.recent.get(c["text"])
            if seen is None or now - seen > self.repeat_s:
                self.reads += 1
                self.log.info("읽음: %s %s", c["format"], c["text"])
                self.state(last=f"{c['format']} {c['text']}", reads=self.reads)
            self.recent[c["text"]] = now
        self.recent = {k: t for k, t in self.recent.items() if now - t <= self.repeat_s}
        self.state(phase="scanning", scan_fps=round(1.0 / max(now - self._last, 1e-3), 1))
        self._last = now
        rest = self.interval - (now - t0)
        if rest > 0:
            self.sleep(rest)


if __name__ == "__main__":
    raise SystemExit(Barcode.main())
