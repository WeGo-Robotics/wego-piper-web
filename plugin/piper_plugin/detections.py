"""검출 페이로드 계약 — **생산자가 하나의 함수를 쓴다** (feature/plugins.md §13-3).

`piper:vision:<name>` 에 올라가는 JSON 의 모양과, LLM 프롬프트용 한 줄(`text`)을 여기서
만든다. 예전엔 검출 데몬 안에 있었다. 플러그인도 검출을 발행하므로 밖으로 꺼냈다 —
두 벌이 되면 화면과 프롬프트가 어긋난 채 디버깅하게 된다(같은 사실 두 곳 문제).

순수 함수다 — torch 없이 테스트된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def payload(
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
    publisher: str | None = None,
    model: str | None = None,
) -> dict:
    """검출 → 버스 JSON.

    좌표는 픽셀 단위 xyxy. 소비자(LLM 프롬프트)는 라벨·좌표를 텍스트로 쓰므로
    소수점은 의미 없는 정밀도다 — 반올림해서 페이로드를 줄인다.

    `speed` 는 단계별 ms(preprocess/inference/postprocess), `det_seq` 는 카메라별
    **검출 횟수** 카운터 — 화면 표시용이라 `text` 에는 넣지 않는다. `frame_seq` 는
    카메라 발행 카운터(~30fps)라 검출 속도(fps) 측정에 못 쓴다.

    `publisher`·`model` 은 발행자가 둘 이상일 때 화면이 "어느 검출기가 낸 것"을 가르는
    재료다 (§13-3). 없으면 빠진다 — 옛 소비자는 모르는 키를 무시한다.
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
    out = {
        "ts": wall_ns / 1e9,
        "cam": cam,
        "frame_seq": seq,
        "size": [w, h],
        "objects": objects,
    }
    if speed:
        out["speed_ms"] = {k: round(float(v), 1) for k, v in speed.items()}
        out["infer_ms"] = out["speed_ms"].get("inference")
    if det_seq is not None:
        out["det_seq"] = det_seq
    if publisher:
        out["publisher"] = publisher
    if model:
        out["model"] = model
    # LLM 프롬프트용 문자열을 **생산자가** 만든다 — UI·판단 스텝이 각자 조립하면
    # 프롬프트가 화면과 어긋난 채 디버깅하게 된다
    out["text"] = detections_text(out)
    return out


def detections_text(p: dict) -> str:
    """LLM user 프롬프트용 한 줄 요약 — 판단 스텝이 그대로 쓴다 (llm-integration §1)."""
    if not p["objects"]:
        return f"[{p['cam']}] 검출 없음"
    parts = [
        f"{o['label']}({o['conf']:.2f}) center=({o['center'][0]:.0f},{o['center'][1]:.0f})"
        for o in p["objects"]
    ]
    w, h = p["size"]
    return f"[{p['cam']} {w}x{h}] " + ", ".join(parts)


def from_result(cam: str, seq: int, wall_ns: int, shape: tuple[int, int], result,
                det_seq: int | None = None, publisher: str | None = None,
                model: str | None = None) -> dict:
    """ultralytics 모양의 결과(`result.boxes.xyxy/.conf/.cls`, `result.names`) → 페이로드.

    `detector_loader` 의 어댑터가 그 모양을 흉내 내므로 RT-DETR 결과도 그대로 들어온다.
    """
    boxes = result.boxes
    return payload(cam, seq, wall_ns, shape, result.names,
                   boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.int().tolist(),
                   speed=getattr(result, "speed", None), det_seq=det_seq,
                   publisher=publisher, model=model)


def model_meta(model, model_file: str, device: str, conf: float, fps: float,
               imgsz: int, cams: dict[str, str] | None = None) -> dict:
    """검출기 자기소개 — 화면이 "지금 무슨 모델이 돌고 있나"를 보여줄 재료.

    모델 객체에서 읽되, 못 읽어도 돌아야 한다 — 표시용 정보다. `cams`(alias→세그먼트)를
    싣는 이유: 화면이 "이 장면 캡처"를 하려면 검출 이름에서 원본 세그먼트로 돌아가야 한다.
    """
    meta = {"model": model_file.rsplit("/", 1)[-1], "device": device,
            "conf": conf, "fps": fps, "imgsz": imgsz, "cams": cams or {}}
    try:
        meta["task"] = getattr(model, "task", None)
        meta["classes"] = len(getattr(model, "names", None) or {})
        # GFLOPs 는 빠졌다 — 우리가 못 재는 값이고, 0 을 넣으면 화면이 조용히 거짓말한다
        meta["params"] = getattr(model, "n_params", None)
        meta["layers"] = getattr(model, "n_layers", None)
    except Exception as e:
        logger.warning("모델 정보 읽기 실패 (표시만 빠진다): %s", e)
    return meta


@dataclass
class Detections:
    """버스에서 읽은 검출 하나 — 플러그인이 다루기 편한 겉옷."""
    raw: dict
    objects: list[dict] = field(init=False)

    def __post_init__(self) -> None:
        self.objects = list(self.raw.get("objects") or [])

    @property
    def ts(self) -> float:
        return float(self.raw.get("ts") or 0.0)

    @property
    def text(self) -> str:
        return str(self.raw.get("text") or detections_text({**self.raw, "objects": self.objects}))

    def labels(self) -> list[str]:
        return [o["label"] for o in self.objects]

    def count(self, label: str) -> int:
        return sum(1 for o in self.objects if o["label"] == label)

    def where(self, label: str) -> list[dict]:
        return [o for o in self.objects if o["label"] == label]

    def __bool__(self) -> bool:
        return bool(self.objects)
