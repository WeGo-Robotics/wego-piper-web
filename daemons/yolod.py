#!/usr/bin/env python3
"""piper-yolod — shm 프레임을 YOLO 로 훑어 검출을 버스에 발행.

분리수거(YOLO→LLM→VLA) 파이프라인의 "인식" 칸 (feature/episode-orchestrator.md,
feature/llm-integration.md §1). **상시 데몬이 아니라 정책 서버와 같은
"필요할 때 켜는 유닛"이다** — 시나리오 시작 시 켜고 끝나면 끈다. 회차마다
subprocess 로 부르면 torch import ~5초를 매번 무는데, 30분 무인 루프에서는
그게 반복되므로 모델을 물고 있는 쪽이 맞다.

## 배선 — 전부 기존 계약 위에 있다

- **입력**: camerad/rsd 가 발행하는 `/dev/shm` 프레임 (`piper_shm.Subscriber`).
  다중 소비자 설계라 장치 소유권 충돌이 없다 — 카메라를 새로 열지 않는다.
  세그먼트는 **RGB** 다 (wrapper 프리뷰가 RGB2BGR 변환 후 저장하는 것과 같은 사실).
- **출력**: 버스 `piper:vision:<cam>` 최신값(JSON, TTL — preview 와 같은 이유로
  큐가 아니다) + 어노테이트 프리뷰 `put_preview("yolo_<cam>")` — UI 는 기존
  프리뷰 경로를 그대로 쓴다.
- **판단**: 오케스트레이터 스텝이 `bus.get_detections()` 로 최신 장면을 읽고
  `ts` 신선도를 검사한 뒤 llm_client.judge() 에 텍스트로 넘긴다.
  LLM 코드는 여기 없다 — 인식과 판단의 분업 (시나리오 확정 사항).

사용:
  python daemons/yolod.py --cam top=rs_335122271186_color --cam hand=rs_250122070363_color
  python daemons/yolod.py --cam rs_335122271186_color --once   # 스모크 1회

환경변수: `PIPER_REDIS_URL`
"""

import argparse
import logging
import signal
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] yolod: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("yolod")

_running = True


def _payload(
    cam: str,
    seq: int,
    wall_ns: int,
    shape: tuple[int, int],
    names: dict[int, str],
    xyxy: list[list[float]],
    confs: list[float],
    clss: list[int],
    speed: dict | None = None,
    det_seq: int | None = None,
) -> dict:
    """검출 → 버스 JSON. 순수 함수 — torch 없이 테스트된다.

    좌표는 픽셀 단위 xyxy. 소비자(LLM 프롬프트)는 라벨·좌표를 텍스트로 쓰므로
    소수점은 의미 없는 정밀도다 — 반올림해서 페이로드를 줄인다.

    `speed` 는 ultralytics 의 단계별 ms(preprocess/inference/postprocess),
    `det_seq` 는 카메라별 **검출 횟수** 카운터 — 화면 표시용이라 `text`(LLM
    프롬프트)에는 넣지 않는다. `frame_seq` 는 카메라 발행 카운터(~30fps)라
    검출 속도(fps) 측정에 못 쓴다 — UI 가 그걸로 나눠서 fps 가 널뛰었다.
    """
    h, w = shape
    objects = []
    for box, conf, cls in zip(xyxy, confs, clss):
        x1, y1, x2, y2 = box
        objects.append({
            "label": names.get(int(cls), str(int(cls))),
            "conf": round(float(conf), 3),
            "bbox": [round(v, 1) for v in box],
            "center": [round((x1 + x2) / 2, 1), round((y1 + y2) / 2, 1)],
        })
    # 큰 것부터 — LLM 프롬프트에서 잘려도 주된 물체가 남는다
    objects.sort(key=lambda o: -(o["bbox"][2] - o["bbox"][0]) * (o["bbox"][3] - o["bbox"][1]))
    payload = {
        "ts": wall_ns / 1e9,
        "cam": cam,
        "frame_seq": seq,
        "size": [w, h],
        "objects": objects,
    }
    if speed:
        payload["speed_ms"] = {k: round(float(v), 1) for k, v in speed.items()}
        payload["infer_ms"] = payload["speed_ms"].get("inference")
    if det_seq is not None:
        payload["det_seq"] = det_seq
    # LLM 프롬프트용 문자열을 **생산자가** 만든다 — UI·판단 스텝이 각자 조립하면
    # 프롬프트가 화면과 어긋난 채 디버깅하게 된다 (같은 사실 두 곳 문제)
    payload["text"] = detections_text(payload)
    return payload


def detections_text(payload: dict) -> str:
    """LLM user 프롬프트용 한 줄 요약 — 판단 스텝이 그대로 쓴다 (llm-integration §1)."""
    if not payload["objects"]:
        return f"[{payload['cam']}] 검출 없음"
    parts = [
        f"{o['label']}({o['conf']:.2f}) center=({o['center'][0]:.0f},{o['center'][1]:.0f})"
        for o in payload["objects"]
    ]
    w, h = payload["size"]
    return f"[{payload['cam']} {w}x{h}] " + ", ".join(parts)


def _model_meta(model, model_file: str, device: str, conf: float, fps: float) -> dict:
    """yolod 자기소개 — 화면이 "지금 무슨 모델이 돌고 있나"를 보여줄 재료.

    ultralytics 객체에서 읽되, 못 읽어도 데몬은 돌아야 한다 — 표시용 정보다.
    """
    # 커스텀 가중치는 절대경로로 들어온다 — 화면에는 파일명이면 충분하다
    meta = {"model": model_file.rsplit("/", 1)[-1], "device": device, "conf": conf, "fps": fps}
    try:
        meta["task"] = getattr(model, "task", None)
        meta["classes"] = len(getattr(model, "names", None) or {})
        layers, params, _grads, gflops = model.info(verbose=False)
        meta.update(layers=layers, params=params, gflops=round(gflops, 1))
    except Exception as e:
        logger.warning("모델 정보 읽기 실패 (표시만 빠진다): %s", e)
    return meta


def _parse_cams(specs: list[str]) -> dict[str, str]:
    """`alias=cam_id` 또는 `cam_id` → {alias: cam_id}."""
    out = {}
    for spec in specs:
        alias, sep, cam_id = spec.partition("=")
        out[alias] = cam_id if sep else alias
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="YOLO detection publisher (shm → bus)")
    parser.add_argument("--cam", action="append", required=True, metavar="ALIAS=CAM_ID",
                        help="구독할 카메라 (반복 가능). ALIAS 는 버스 키 이름")
    parser.add_argument("--model", default="yolo11n.pt")
    parser.add_argument("--fps", type=float, default=5.0, help="검출 주기 (프레임 주기와 무관)")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--no-preview", action="store_true", help="어노테이트 프리뷰 발행 끄기")
    parser.add_argument("--once", action="store_true", help="1회 검출 후 종료 (스모크)")
    args = parser.parse_args()
    cams = _parse_cams(args.cam)

    # 무거운 import 는 여기서 — _payload/detections_text 는 torch 없이 테스트된다
    import cv2
    import numpy as np  # noqa: F401  (ultralytics 가 요구)
    from piper_bus.client import Bus
    from piper_shm import Subscriber, segment_for_camera
    from ultralytics import YOLO

    def _signal(sig, _):
        global _running
        logger.info("signal %s — 종료", sig)
        _running = False

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    logger.info("모델 로드: %s (device=%s)", args.model, args.device)
    model = YOLO(args.model)
    bus = Bus()
    meta = _model_meta(model, args.model, args.device, args.conf, args.fps)
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
        bus.put_yolo_meta(meta)  # TTL 갱신 — 죽으면 화면에서도 사라진다
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

            # 세그먼트는 RGB, ultralytics 의 numpy 입력 규약은 BGR
            result = model.predict(
                frame[..., ::-1], conf=args.conf, device=args.device, verbose=False
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
                    bus.put_preview(f"yolo_{alias}", jpeg.tobytes())

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
