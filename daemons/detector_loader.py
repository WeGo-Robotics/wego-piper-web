"""검출 가중치 → 모델 객체. 세 데몬이 공유하는 한 조각."""
def load_detector(weights: str):
    """가중치 파일 이름으로 로더 클래스를 고른다.

    ⚠ `YOLO()` 는 RT-DETR 가중치를 못 연다 — 아키텍처마다 클래스가 따로다.
    카탈로그에 모델을 추가할 때 여기를 같이 안 고치면, 화면에서는 고를 수 있는데
    시작만 하면 죽는 선택지가 생긴다.

    **세 데몬(검출·사전라벨·학습)이 같은 규칙을 써야 한다** — 한 곳만 고치면
    데모에서는 되는데 학습에서는 안 되는 식으로 갈린다.
    """
    from ultralytics import RTDETR, YOLO

    stem = weights.rsplit("/", 1)[-1].lower()
    return RTDETR(weights) if stem.startswith("rtdetr") else YOLO(weights)
