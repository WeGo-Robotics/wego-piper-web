#!/usr/bin/env python3
"""detector — shm 프레임을 모델로 훑어 검출을 버스에 발행 (feature/plugins.md §13).

검출 데몬 `daemons/detectord.py` 가 하던 일 그대로다. 다른 점은 자리다 — 플러그인이라
프로세스 소유·GPU 배타·E-stop·화면 카드가 공통 길을 탄다. 번들 플러그인은 ultralytics 를
쓰지 않는다(AGPL) — 로더는 `piper_plugin.detector`(RT-DETR).

- **입력**: camerad/rsd 가 발행하는 `/dev/shm` 프레임(**BGR**). 카메라를 열지 않는다.
- **출력**: 버스 `piper:vision:<alias>`(검출 JSON, `text` 가 LLM 프롬프트 재료) +
  `piper:vision_meta:detector`(자기소개) + 프리뷰 `det_<alias>`.

    piper-plugin run plugins/detector --camera top=<스트림> --config '{"device":"cpu"}'
"""

from __future__ import annotations

import time

from piper_plugin import Plugin
from piper_plugin.detections import model_meta


def parse_cams(spec: str) -> dict[str, str]:
    """`alias=stream, alias2=stream2` 또는 `stream` → {alias: stream}. 검출 데몬의 `--cam` 과 같다."""
    out: dict[str, str] = {}
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        alias, sep, stream = part.partition("=")
        out[alias.strip()] = stream.strip() if sep else alias.strip()
    return out


class Detector(Plugin):
    def setup(self):
        cams = parse_cams(self.config.get("cams") or "")
        if not cams:
            cams = {k: v for k, v in self.cameras.items() if v}
        if not cams:
            self.stop("구독할 카메라가 없다 — cams 설정 또는 카메라 슬롯을 채우라")
        self.cams = cams
        self.model_name = str(self.config["model"])
        self.device = str(self.config.get("device") or "cpu")
        self.conf = float(self.config["conf"])
        self.imgsz = int(self.config["imgsz"])
        self.interval = 1.0 / max(float(self.config["fps"]), 0.1)
        self.preview = bool(self.config.get("preview", True))
        self.log.info("모델 로드: %s (device=%s)", self.model_name, self.device)
        self.model = self.detector(self.model_name, device=self.device)
        self.meta = model_meta(self.model, self.model_name, self.device, self.conf,
                               float(self.config["fps"]), self.imgsz, self.cams)
        self.readers = {alias: self.camera(stream) for alias, stream in self.cams.items()}
        self.counts = {alias: 0 for alias in self.cams}
        self._last = time.monotonic()
        self.state(phase="detecting", cams=list(self.cams))

    def loop(self):
        t0 = time.monotonic()
        self.publish_meta(self.meta)            # TTL 갱신 — 죽으면 화면에서도 사라진다
        for alias, cam in self.readers.items():
            got = cam.read_new(timeout_s=0.0)
            if got is None:
                continue
            frame, seq, wall_ns = got
            # ⚠ 세그먼트는 **BGR** 이다 — 로더가 RGB 로 바꿔 모델에 넣고, plot() 은 BGR 을 돌려준다
            result = self.model.predict(frame, conf=self.conf, imgsz=self.imgsz,
                                        device=self.device, verbose=False)[0]
            self.counts[alias] += 1
            self.publish_detections(alias, frame.shape[:2], result, seq=seq, wall_ns=wall_ns,
                                    model=self.meta["model"])
            if self.preview:
                self.publish_preview(f"det_{alias}", result.plot())
        now = time.monotonic()
        self.state(det_fps=round(1.0 / max(now - self._last, 1e-3), 1), detections=dict(self.counts))
        self._last = now
        rest = self.interval - (now - t0)
        if rest > 0:
            self.sleep(rest)


if __name__ == "__main__":
    raise SystemExit(Detector.main())
