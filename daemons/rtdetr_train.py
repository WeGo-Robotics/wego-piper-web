"""RT-DETR 미세조정 — ultralytics `model.train()` 을 대신한다.

## 왜 직접 쓰나

`ultralytics` 는 AGPL-3.0 이라 배포물 전체를 물들인다. RT-DETR 아키텍처 자체는
Apache-2.0 이고 `transformers` 에 구현이 있으므로, 학습 루프만 우리가 갖는다.

## 계약

`yolo_traind` 가 넘기는 것: 이미지 파일 목록(train/val), 클래스 이름, 하이퍼파라미터.
돌려주는 것: 지표 dict. 가중치는 `out_dir` 에 **HF 형식 디렉토리**로 저장한다 —
`.pt` 단일 파일이 아니다(그 형식은 ultralytics 것이다).

⚠ **라벨 형식은 그대로 YOLO txt(`cls cx cy w h`, 정규화)다.** 그건 ultralytics
코드가 아니라 사실상의 표준 텍스트 형식이라 바꿀 이유가 없다 — 이미 라벨해 둔
데이터셋을 버리게 된다.
"""

import csv
import json
from pathlib import Path

import torch


def _read_label(txt: Path, w: int, h: int) -> list[dict]:
    """YOLO 정규화 `cls cx cy w h` → COCO 절대 `[x, y, w, h]`.

    ⚠ 중심좌표→좌상단 변환과 정규화 해제를 한 곳에서 한다. 두 군데로 나뉘면
    한쪽만 고쳐서 상자가 통째로 어긋나는데, 숫자만 보고는 알아채기 어렵다.
    """
    anns = []
    if not txt.is_file():
        return anns
    for line in txt.read_text().split("\n"):
        parts = line.split()
        if len(parts) != 5:
            continue
        c, cx, cy, bw, bh = int(parts[0]), *(float(v) for v in parts[1:])
        x, y = (cx - bw / 2) * w, (cy - bh / 2) * h
        aw, ah = bw * w, bh * h
        if aw <= 0 or ah <= 0:
            continue
        anns.append({"bbox": [x, y, aw, ah], "category_id": c,
                     "area": aw * ah, "iscrowd": 0})
    return anns


class _Dataset(torch.utils.data.Dataset):
    def __init__(self, files: list[Path], labels_dir: Path):
        self.files, self.labels_dir = files, labels_dir

    def __len__(self) -> int:
        return len(self.files)

    def __getitem__(self, i: int):
        from PIL import Image

        f = self.files[i]
        img = Image.open(f).convert("RGB")
        anns = _read_label((self.labels_dir / f.name).with_suffix(".txt"),
                           img.width, img.height)
        return img, {"image_id": i, "annotations": anns}


def _collate(processor):
    def fn(batch):
        images = [b[0] for b in batch]
        targets = [b[1] for b in batch]
        return processor(images=images, annotations=targets, return_tensors="pt")
    return fn


def _evaluate(model, loader, processor, device) -> dict:
    """val mAP. ⚠ 지표는 **torchmetrics** 에 맡긴다 — 손으로 짜면 그럴듯하게 틀린다."""
    from torchmetrics.detection import MeanAveragePrecision

    metric = MeanAveragePrecision(box_format="xyxy")
    model.eval()
    with torch.no_grad():
        for batch in loader:
            pixel_values = batch["pixel_values"].to(device)
            out = model(pixel_values=pixel_values)
            sizes = torch.tensor(
                [[int(l["orig_size"][0]), int(l["orig_size"][1])] for l in batch["labels"]],
                device=device)
            preds = processor.post_process_object_detection(
                out, target_sizes=sizes, threshold=0.001)
            targets = []
            for l, (h, w) in zip(batch["labels"], sizes.tolist()):
                # 정답도 정규화 cxcywh 로 들어온다 — 예측과 같은 좌표계로 맞춘다
                b = l["boxes"]
                xyxy = torch.stack([(b[:, 0] - b[:, 2] / 2) * w, (b[:, 1] - b[:, 3] / 2) * h,
                                    (b[:, 0] + b[:, 2] / 2) * w, (b[:, 1] + b[:, 3] / 2) * h], -1)
                targets.append({"boxes": xyxy.to(device), "labels": l["class_labels"].to(device)})
            metric.update([{k: v.to(device) for k, v in p.items()} for p in preds], targets)
    r = metric.compute()
    return {"map50": round(float(r["map_50"]), 4), "map50_95": round(float(r["map"]), 4)}


def _pick_workers(log, want: int = 2) -> int:
    """`/dev/shm` 이 좁으면 워커를 끈다. 느린 것은 알아챌 수 있지만 멈춘 것은 못 알아챈다."""
    import shutil as _sh

    try:
        free = _sh.disk_usage("/dev/shm").total
    except OSError:
        return 0
    if free < 256 * 1024 * 1024:
        log(f"⚠ /dev/shm 이 {free // 1024 // 1024}MB 뿐 — DataLoader 워커를 끕니다"
            " (도커라면 `--ipc=host` 나 `--shm-size=1g` 를 주세요)")
        return 0
    return want


def train(*, model_id: str, train_files: list[Path], val_files: list[Path],
          labels_dir: Path, classes: list[str], epochs: int, batch: int,
          device: str, out_dir: Path, results_csv: Path, log=print) -> dict:
    """미세조정하고 **가장 좋은 에폭**을 `out_dir` 에 남긴다."""
    from transformers import AutoImageProcessor, AutoModelForObjectDetection

    processor = AutoImageProcessor.from_pretrained(model_id)
    # ⚠ `ignore_mismatched_sizes` 가 없으면 COCO 80 클래스 헤드와 우리 클래스 수가
    #   달라 로드가 통째로 실패한다. 헤드만 새로 초기화하고 나머지는 물려받는다.
    model = AutoModelForObjectDetection.from_pretrained(
        model_id, num_labels=len(classes),
        id2label={i: c for i, c in enumerate(classes)},
        label2id={c: i for i, c in enumerate(classes)},
        ignore_mismatched_sizes=True).to(device)

    collate = _collate(processor)
    # ⚠ **`/dev/shm` 이 작으면 워커가 조용히 멈춘다.** 도커 기본값은 64MB 이고
    #   DataLoader 워커는 공유 메모리로 텐서를 주고받는다 — 모자라면 에러도 로그도
    #   없이 영원히 걸린다(실측: 7분간 GPU 0%, 출력 0바이트). compose 는 `ipc: host`
    #   라 실배포에서는 32GB 지만, 누가 그 줄을 지우거나 손으로 `docker run` 하면
    #   그 실패가 재현된다. 그때는 **느린 편이 멈추는 것보다 낫다.**
    workers = _pick_workers(log)
    tl = torch.utils.data.DataLoader(_Dataset(train_files, labels_dir), batch_size=batch,
                                     shuffle=True, collate_fn=collate, num_workers=workers)
    vl = torch.utils.data.DataLoader(_Dataset(val_files, labels_dir), batch_size=batch,
                                     shuffle=False, collate_fn=collate, num_workers=workers)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(epochs, 1))

    results_csv.parent.mkdir(parents=True, exist_ok=True)
    with results_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["epoch", "train_loss", "map50", "map50_95"])

        best = {"map50": -1.0, "map50_95": 0.0}
        for ep in range(1, epochs + 1):
            model.train()
            total, n = 0.0, 0
            for b in tl:
                opt.zero_grad()
                out = model(pixel_values=b["pixel_values"].to(device),
                            labels=[{k: v.to(device) for k, v in l.items()}
                                    for l in b["labels"]])
                out.loss.backward()
                # ⚠ 클리핑이 없으면 초반 몇 스텝에서 loss 가 NaN 으로 튄다
                torch.nn.utils.clip_grad_norm_(model.parameters(), 0.1)
                opt.step()
                total += float(out.loss); n += 1
            sched.step()
            m = _evaluate(model, vl, processor, device)
            loss = round(total / max(n, 1), 4)
            w.writerow([ep, loss, m["map50"], m["map50_95"]]); fh.flush()
            log(f"epoch {ep}/{epochs}  loss {loss}  mAP50 {m['map50']}  mAP50-95 {m['map50_95']}")

            # ⚠ **마지막이 아니라 가장 좋은 것을 남긴다.** 데이터가 적으면 뒤로 갈수록
            #   과적합해서, 마지막 에폭이 최고인 경우가 오히려 드물다.
            if m["map50"] > best["map50"]:
                best = m
                out_dir.mkdir(parents=True, exist_ok=True)
                model.save_pretrained(out_dir)
                processor.save_pretrained(out_dir)
                log(f"  → 최고 갱신, 저장: {out_dir.name}")

    if best["map50"] < 0:
        raise RuntimeError("한 에폭도 완료하지 못했습니다")
    return best
