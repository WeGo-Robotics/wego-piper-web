#!/usr/bin/env python3
"""YOLO 사전 라벨 — 데이터셋의 미라벨 이미지를 모델로 훑어 라벨 초안을 쓴다.

feature/yolo-training.md 2단계. 게이트웨이는 torch 를 import 하지 않으므로
(yolod 와 같은 이유) 이 스크립트를 subprocess 로 부른다. 회차가 아니라
**데이터셋 단위 일괄**인 이유: torch import ~5초를 이미지마다 물 수 없다.

클래스 매핑은 **이름 완전 일치**다 — 모델이 내놓은 라벨("bottle")이
데이터셋 classes.json 에 그대로 있을 때만 박스가 남는다. 이전 커스텀
가중치로 돌리면 이름이 정확히 맞고, COCO 기본 모델이면 겹치는 이름만
채워진다. 애매한 유사 매칭은 조용히 틀린 라벨을 만든다 — 안 한다.

사용:
  python daemons/yolo_prelabel.py --dataset <dir> --model yolo11n.pt [--conf 0.25]
                                  [--overwrite] [--device cuda:0]

마지막 stdout 줄이 결과 JSON 이다 (게이트웨이가 파싱한다):
  {"labeled": 12, "boxes": 31, "no_match": 3, "targets": 15}
"""

import argparse
import json
import sys
from pathlib import Path


def txt_lines(
    classes: list[str],
    names: dict[int, str],
    clss: list[int],
    xywhn: list[list[float]],
) -> tuple[list[str], int]:
    """검출 → YOLO txt 줄들. 순수 함수 — torch 없이 테스트된다.

    (줄 목록, 이름 불일치로 버린 박스 수)를 돌려준다.
    좌표는 ultralytics `xywhn`(정규화 cx cy w h) 그대로 — 형식 정의는
    ultralytics 표준이지 우리 발명이 아니다 (backend write_label 과 같은 표준).
    """
    lines, dropped = [], 0
    for cls_id, box in zip(clss, xywhn):
        label = names.get(int(cls_id), str(int(cls_id)))
        try:
            idx = classes.index(label)
        except ValueError:
            dropped += 1
            continue
        # predict 의 정규화 값이 경계에서 1.0 을 살짝 넘을 수 있다 — 클램프
        vals = [min(1.0, max(0.0, float(v))) for v in box]
        lines.append(f"{idx} " + " ".join(f"{v:.6f}" for v in vals))
    return lines, dropped


def main() -> None:
    parser = argparse.ArgumentParser(description="YOLO prelabel (dataset batch)")
    parser.add_argument("--dataset", required=True, help="yolo_datasets/<name> 디렉토리")
    parser.add_argument("--model", default="yolo11n.pt")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--overwrite", action="store_true",
                        help="이미 라벨된 이미지도 다시 쓴다 (기본: 미라벨만)")
    args = parser.parse_args()

    ds = Path(args.dataset)
    classes = json.loads((ds / "classes.json").read_text())
    images_dir, labels_dir = ds / "images", ds / "labels"
    targets = [
        p for p in sorted(images_dir.glob("*.jpg"))
        if args.overwrite or not (labels_dir / p.name).with_suffix(".txt").exists()
    ]
    if not targets:
        print(json.dumps({"labeled": 0, "boxes": 0, "no_match": 0, "targets": 0}))
        return

    from detector_loader import load_detector

    model = load_detector(args.model)
    labeled = boxes_total = no_match = 0
    for i, img in enumerate(targets):
        result = model.predict(str(img), conf=args.conf, imgsz=args.imgsz,
                               device=args.device, verbose=False)[0]
        lines, dropped = txt_lines(
            classes, result.names,
            result.boxes.cls.int().tolist(), result.boxes.xywhn.tolist(),
        )
        no_match += dropped
        # 박스가 하나도 안 남으면 **안 쓴다** — "검출 없음"과 "배경으로 확인함"은
        # 다른 사실이고, 후자는 사람만 말할 수 있다.
        if lines:
            (labels_dir / img.name).with_suffix(".txt").write_text("\n".join(lines) + "\n")
            labeled += 1
            boxes_total += len(lines)
        if (i + 1) % 20 == 0:
            print(f"{i + 1}/{len(targets)}", file=sys.stderr, flush=True)

    print(json.dumps({"labeled": labeled, "boxes": boxes_total,
                      "no_match": no_match, "targets": len(targets)}))


if __name__ == "__main__":
    main()
