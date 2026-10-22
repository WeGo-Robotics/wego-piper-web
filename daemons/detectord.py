#!/usr/bin/env python3
"""piper-detectord — shm 프레임을 검출 모델로 훑어 검출을 버스에 발행.

분리수거(검출→LLM→VLA) 파이프라인의 "인식" 칸 (feature/episode-orchestrator.md,
feature/llm-integration.md §1). **상시 데몬이 아니라 정책 서버와 같은
"필요할 때 켜는 유닛"이다** — 시나리오 시작 시 켜고 끝나면 끈다. 회차마다
subprocess 로 부르면 torch import ~5초를 매번 무는데, 30분 무인 루프에서는
그게 반복되므로 모델을 물고 있는 쪽이 맞다.

## 배선 — 전부 기존 계약 위에 있다

- **입력**: camerad/rsd 가 발행하는 `/dev/shm` 프레임 (`piper_shm.Subscriber`).
  다중 소비자 설계라 장치 소유권 충돌이 없다 — 카메라를 새로 열지 않는다.
  세그먼트는 **BGR** 다 (rsd 는 `to_bgr` 후 발행, camerad 는 OpenCV 그대로 —
  shm_snapshot.py 의 실사고 교정 주석과 같은 사실. 예전 "RGB" 주석은 틀렸었다).
- **출력**: 버스 `piper:vision:<cam>` 최신값(JSON, TTL — preview 와 같은 이유로
  큐가 아니다) + 어노테이트 프리뷰 `put_preview("det_<cam>")` — UI 는 기존
  프리뷰 경로를 그대로 쓴다.
- **판단**: 오케스트레이터 스텝이 `bus.get_detections()` 로 최신 장면을 읽고
  `ts` 신선도를 검사한 뒤 llm_client.judge() 에 텍스트로 넘긴다.
  LLM 코드는 여기 없다 — 인식과 판단의 분업 (시나리오 확정 사항).

사용:
  python daemons/detectord.py --cam top=rs_335122271186_color --cam hand=rs_250122070363_color
  python daemons/detectord.py --cam rs_335122271186_color --once   # 스모크 1회

환경변수: `PIPER_REDIS_URL`
"""

import argparse
import logging
import signal
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] detectord: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("detectord")

_running = True


# 페이로드·프롬프트 한 줄·자기소개는 SDK 가 정본이다 — 플러그인도 같은 함수로 발행한다
# (feature/plugins.md §13-3). 이름은 테스트·호출부 호환으로 남긴다.
from piper_plugin.detections import detections_text, model_meta as _model_meta, payload as _payload  # noqa: E402,F401


def _parse_cams(specs: list[str]) -> dict[str, str]:
    """`alias=cam_id` 또는 `cam_id` → {alias: cam_id}."""
    out = {}
    for spec in specs:
        alias, sep, cam_id = spec.partition("=")
        out[alias] = cam_id if sep else alias
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="detection publisher (shm → bus)")
    parser.add_argument("--cam", action="append", required=True, metavar="ALIAS=CAM_ID",
                        help="구독할 카메라 (반복 가능). ALIAS 는 버스 키 이름")
    parser.add_argument("--model", default="PekingU/rtdetr_v2_r18vd")
    parser.add_argument("--fps", type=float, default=5.0, help="검출 주기 (프레임 주기와 무관)")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--imgsz", type=int, default=640,
                        help="추론 입력 크기 (긴 변, letterbox). 프리뷰 해상도와 무관")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--no-preview", action="store_true", help="어노테이트 프리뷰 발행 끄기")
    parser.add_argument("--once", action="store_true", help="1회 검출 후 종료 (스모크)")
    args = parser.parse_args()
    cams = _parse_cams(args.cam)

    # 무거운 import 는 여기서 — _payload/detections_text 는 torch 없이 테스트된다
    import cv2
    import numpy as np  # noqa: F401  (프레임 배열 처리에 필요)
    from piper_bus.client import Bus
    from piper_shm import Subscriber, segment_for_camera
    from detector_loader import load_detector

    def _signal(sig, _):
        global _running
        logger.info("signal %s — 종료", sig)
        _running = False

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    logger.info("모델 로드: %s (device=%s)", args.model, args.device)
    model = load_detector(args.model)
    bus = Bus()
    meta = _model_meta(model, args.model, args.device, args.conf, args.fps,
                       args.imgsz, cams)
    subs: dict[str, Subscriber] = {}
    det_counts: dict[str, int] = {alias: 0 for alias in cams}
    interval = 1.0 / max(args.fps, 0.1)

    def _sub(alias: str) -> Subscriber | None:
        """세그먼트가 아직/다시 없으면 None — 루프가 재시도한다."""
        try:
            return Subscriber(segment_for_camera(cams[alias]))
        except Exception as e:
            logger.warning("%s(%s) 구독 실패: %s", alias, cams[alias], e)
            return None

    while _running:
        t0 = time.monotonic()
        bus.put_detector_meta(meta)  # TTL 갱신 — 죽으면 화면에서도 사라진다
        for alias in cams:
            sub = subs.get(alias) or _sub(alias)
            if sub is None:
                continue
            subs[alias] = sub
            try:
                got = sub.read()
            except Exception as e:
                # 해상도 변경 등으로 세그먼트가 재생성되면 다시 붙는다
                logger.warning("%s 읽기 실패, 재구독: %s", alias, e)
                subs.pop(alias, None)
                continue
            if got is None:
                continue
            frame, seq, wall_ns = got

            # ⚠ 세그먼트는 **BGR** 이다 — ultralytics 의 numpy 입력 규약과 같아
            #   변환 없이 그대로 넣는다. 예전엔 "세그먼트는 RGB" 전제로 여기서
            #   채널을 뒤집었는데, 그 전제가 틀렸다: rsd 는 to_bgr 를 거쳐
            #   발행하고(piper_rs/hub.py) camerad 는 OpenCV 프레임 그대로다.
            #   같은 오해가 스냅샷 경로에서 "노란 물체가 파랗게" 사고를 이미
            #   냈다(shm_snapshot.py). 학습 캡처(segment_jpeg)는 BGR 로 바르게
            #   저장되므로, 여기서 뒤집으면 학습과 추론이 다른 색을 본다.
            result = model.predict(
                frame, conf=args.conf, imgsz=args.imgsz,
                device=args.device, verbose=False,
            )[0]
            det_counts[alias] += 1
            payload = _payload(
                alias, seq, wall_ns, frame.shape[:2],
                result.names,
                result.boxes.xyxy.tolist(),
                result.boxes.conf.tolist(),
                result.boxes.cls.int().tolist(),
                speed=getattr(result, "speed", None),
                det_seq=det_counts[alias],
            )
            bus.put_detections(alias, payload)

            if not args.no_preview:
                # result.plot() 은 BGR 어노테이트 이미지 — 그대로 JPEG 로
                ok, jpeg = cv2.imencode(".jpg", result.plot(),
                                        [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    bus.put_preview(f"det_{alias}", jpeg.tobytes())

            if args.once:
                print(detections_text(payload), flush=True)

        if args.once:
            break
        elapsed = time.monotonic() - t0
        if elapsed < interval:
            time.sleep(interval - elapsed)

    for sub in subs.values():
        sub.close()
    logger.info("종료")


if __name__ == "__main__":
    main()
