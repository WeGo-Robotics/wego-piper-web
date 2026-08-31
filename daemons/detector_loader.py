"""검출 모델 → 모델 객체. 세 데몬이 공유하는 한 조각.

## 왜 ultralytics 가 아닌가

`ultralytics` 는 **AGPL-3.0** 이다. 이 저장소를 배포하는 순간 그것과 결합한
결과물 전체가 AGPL 이 되고, 비상업 조건을 붙이는 것도 불가능해진다(AGPL §7 은
추가 제약을 금지한다). 그래서 검출기를 `transformers` 의 RT-DETR 로 바꿨다 —
**Apache-2.0** 이고, 가중치(`PekingU/rtdetr_*`)도 Apache-2.0 이다.

⚠ **RT-DETR 아키텍처가 Apache 인 것과 가중치가 Apache 인 것은 다르다.** 예전
카탈로그의 `rtdetr-l.pt` 는 아키텍처만 Apache 이고 **가중치는 ultralytics 배포본**
이라 AGPL 이었다. 여기서는 HF 허브의 원 배포본을 쓴다.

## 왜 어댑터인가

`yolod`·`yolo_prelabel` 이 ultralytics 의 모양(`model.predict(...) → result.boxes
.xyxy/.conf/.cls`, `result.names`)에 맞춰 쓰여 있다. 그 모양을 그대로 흉내 내면
호출부를 안 고쳐도 된다 — 검출기를 갈아끼우는 일에 데몬 로직까지 건드리면
회귀 위험만 커진다.
"""

DEFAULT_MODEL = "PekingU/rtdetr_v2_r18vd"


class _Boxes:
    """ultralytics `result.boxes` 흉내. `.tolist()`·`.int()` 가 불리므로 텐서로 둔다."""

    __slots__ = ("xyxy", "conf", "cls")

    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = xyxy, conf, cls


class _Result:
    __slots__ = ("names", "boxes")

    def __init__(self, names, boxes):
        self.names, self.boxes = names, boxes


class RTDetr:
    """`transformers` RT-DETR 을 ultralytics 모양으로 감싼다."""

    def __init__(self, model_id: str, device: str = "cpu"):
        import torch
        from transformers import AutoModelForObjectDetection, AutoImageProcessor

        self._torch = torch
        self.model_id = model_id
        self.processor = AutoImageProcessor.from_pretrained(model_id)
        self.model = AutoModelForObjectDetection.from_pretrained(model_id).eval()
        self.to(device)
        # ultralytics 는 `model.names` 로 클래스표를 준다. 같은 이름으로 맞춘다.
        self.names = {int(k): v for k, v in self.model.config.id2label.items()}

    def to(self, device: str):
        self.device = device or "cpu"
        self.model.to(self.device)
        return self

    def predict(self, image, conf: float = 0.25, device: str | None = None,
                verbose: bool = False, **_ignored) -> list["_Result"]:
        """⚠ `imgsz` 는 받기만 하고 쓰지 않는다. RT-DETR 은 전처리기가 자기
        입력 크기로 맞추므로, 밖에서 정한 값을 억지로 넣으면 정확도만 떨어진다.
        인자를 지우지 않는 이유는 호출부를 안 고치기 위해서다.
        """
        torch = self._torch
        if device and device != self.device:
            self.to(device)
        # ⚠ 호출부가 **넘기는 것이 두 가지다.** `yolod` 는 numpy 프레임을,
        #   `yolo_prelabel` 은 **파일 경로 문자열**을 준다. ultralytics 는 둘 다
        #   받았으므로 여기서도 둘 다 받는다.
        if isinstance(image, (str, bytes)) or hasattr(image, "__fspath__"):
            from PIL import Image
            image = Image.open(image).convert("RGB")
        # ⚠ 호출부는 `frame[..., ::-1]`(BGR→RGB)을 넘긴다. 그건 **음수 stride** 라
        #   `torch.from_numpy` 가 거부한다("negative stride ... not supported").
        #   ultralytics 는 안에서 삼켰으므로 여기서도 삼킨다 — 이 차이 때문에
        #   호출부를 고치게 하면 어댑터를 둔 의미가 없다.
        if hasattr(image, "strides") and any(st < 0 for st in image.strides):
            import numpy as np
            image = np.ascontiguousarray(image)
        inputs = self.processor(images=image, return_tensors="pt").to(self.device)
        with torch.no_grad():
            out = self.model(**inputs)
        # 원본 크기로 되돌려 좌표를 낸다. PIL 은 (w,h), numpy 는 (h,w,...) 다 —
        # 뒤바꾸면 좌표가 통째로 어긋나는데 그림 없이는 알아채기 어렵다.
        h, w = (image.shape[0], image.shape[1]) if hasattr(image, "shape") \
            else (image.height, image.width)
        r = self.processor.post_process_object_detection(
            out, target_sizes=torch.tensor([[h, w]], device=self.device),
            threshold=conf)[0]
        # ⚠ **리스트로 돌려준다.** ultralytics 는 이미지 배치별 결과 리스트를 주고,
        #   호출부가 `model.predict(...)[0]` 으로 첫 장을 꺼낸다. 하나만 돌려주면
        #   `TypeError: '_Result' object is not subscriptable` 로 죽는다 —
        #   실기에서 그렇게 걸렸다. 모양을 흉내 내는 어댑터의 일이다.
        return [_Result(self.names,
                        _Boxes(r["boxes"].cpu(), r["scores"].cpu(), r["labels"].cpu()))]


def load_detector(weights: str, device: str = "cpu"):
    """모델 이름 → 모델 객체.

    ⚠ **세 데몬(검출·사전라벨·학습)이 같은 규칙을 써야 한다.** 한 곳만 고치면
    데모에서는 되는데 학습에서는 안 되는 식으로 갈린다.

    ⚠ `.pt` 는 **거절한다.** ultralytics 가중치 형식이고, 그것을 열려면 AGPL
    라이브러리가 필요하다. 조용히 무시하면 "왜 내 가중치가 안 먹지"로 돌아온다.
    """
    if weights.endswith(".pt"):
        raise ValueError(
            f"{weights}: ultralytics(.pt) 가중치는 더 이상 지원하지 않습니다 "
            f"(AGPL). RT-DETR 모델 이름을 쓰세요 — 예: {DEFAULT_MODEL}")
    return RTDetr(weights or DEFAULT_MODEL, device)
