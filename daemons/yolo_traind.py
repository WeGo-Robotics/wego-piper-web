#!/usr/bin/env python3
"""piper-yolotrain — YOLO 커스텀 학습 유닛 (feature/yolo-training.md 3단계).

게이트웨이가 systemd-run 으로 띄운다. 게이트웨이가 재시작해도 학습은 살고,
**수확(best.pt → 가중치 디렉토리)까지 이 스크립트가 마지막 스텝으로 한다** —
게이트웨이 훅에 걸면 유닛이 살아남는 바로 그 재시작에서 수확이 빠진다
(에피소드 편집 wrapper 와 같은 이유).

진행 상태는 파일로 남긴다:
- `<datasets-root>/_training.json` — 시작/완료/실패 (게이트웨이가 읽는다)
- `<dataset>/runs/<run>/results.csv` — 학습 루프가 에폭마다 쓴다

val 분할은 **출처 그룹 단위**다. 같은 에피소드의 프레임이 train 과 val 에
나뉘면 사실상 같은 그림으로 검증해 mAP 가 부풀려진다 — 지표는 좋은데
현장에서 약한 모델이 나오는 함정.

사용:
  python daemons/yolo_traind.py --dataset <dir> --model PekingU/rtdetr_v2_r18vd \
      --epochs 50 --imgsz 640 --weights-out <yolo_models_dir> --run-name t0820
"""

import argparse
import json
import random
import shutil
import sys
import time
from pathlib import Path

VAL_RATIO = 0.1
SPLIT_SEED = 42


def group_key(source: dict | None) -> str:
    """이미지의 분할 그룹. 에피소드는 (dataset, episode) 로 묶인다 —
    프레임 단위 분할이 만드는 train/val 누수를 막는 핵심."""
    if not source:
        return "unknown"
    if source.get("type") == "episode":
        return f"ep:{source.get('dataset')}:{source.get('episode')}"
    if source.get("type") == "live":
        # 같은 카메라·같은 날 = 거의 같은 장면 구도
        day = time.strftime("%Y%m%d", time.localtime(source.get("at", 0)))
        return f"live:{source.get('cam')}:{day}"
    return f"file:{source.get('file')}"   # 업로드는 낱장 그룹


def split_by_group(
    files: list[str], groups: dict[str, str],
    val_ratio: float = VAL_RATIO, seed: int = SPLIT_SEED,
) -> tuple[list[str], list[str]]:
    """그룹 단위 train/val 분할. 순수 함수 — torch 없이 테스트된다.

    그룹이 하나뿐이면 어쩔 수 없이 파일 단위로 나눈다 (val 없이는 학습이
    안 돈다). val 은 최소 1장을 보장한다.
    """
    by_group: dict[str, list[str]] = {}
    for f in files:
        by_group.setdefault(groups.get(f, f), []).append(f)

    rng = random.Random(seed)
    keys = sorted(by_group)
    if len(keys) == 1:
        shuffled = list(files)
        rng.shuffle(shuffled)
        n_val = max(1, round(len(shuffled) * val_ratio))
        return shuffled[n_val:], shuffled[:n_val]

    rng.shuffle(keys)
    target = max(1, round(len(files) * val_ratio))
    val: list[str] = []
    for k in keys:
        if len(val) >= target:
            break
        # 한 그룹이 통째로 target 을 크게 넘으면 건너뛰고 더 작은 그룹을 찾는다
        if val and len(val) + len(by_group[k]) > target * 2:
            continue
        val.extend(by_group[k])
    if not val:                     # 전부 건너뛰었으면 가장 작은 그룹 하나
        val = by_group[min(keys, key=lambda k: len(by_group[k]))]
    val_set = set(val)
    train = [f for f in files if f not in val_set]
    if not train:                   # 극단: 그룹 2개에 한쪽이 전부
        train, val = val[1:], val[:1]
    return train, val


def read_sources(ds: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    p = ds / "sources.jsonl"
    if p.is_file():
        for line in p.read_text().splitlines():
            try:
                rec = json.loads(line)
                out[rec["file"]] = rec
            except (ValueError, KeyError):
                continue
    return out


def write_split(ds: Path, run_dir: Path) -> tuple[list[Path], list[Path]]:
    """그룹 단위 train/val 분할. (train 파일들, val 파일들) 반환.

    ⚠ 예전에는 여기서 `data.yaml` 도 만들었다 — ultralytics 가 읽는 형식이다.
    학습을 직접 하게 되면서 필요 없어졌다. 목록 파일(`train.txt`/`val.txt`)은
    **남긴다**: 무엇으로 학습했는지 나중에 확인할 유일한 흔적이다.
    """
    labeled = [
        p.name for p in sorted((ds / "images").glob("*.jpg"))
        if (ds / "labels" / p.name).with_suffix(".txt").exists()
    ]
    if len(labeled) < 4:
        raise SystemExit(f"라벨된 이미지가 {len(labeled)}장 — 최소 4장 필요")
    sources = read_sources(ds)
    groups = {f: group_key(sources.get(f)) for f in labeled}
    train, val = split_by_group(labeled, groups)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "train.txt").write_text("\n".join(str(ds / "images" / f) for f in train) + "\n")
    (run_dir / "val.txt").write_text("\n".join(str(ds / "images" / f) for f in val) + "\n")
    return [ds / "images" / f for f in train], [ds / "images" / f for f in val]


def main() -> None:
    parser = argparse.ArgumentParser(description="YOLO custom training unit")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model", default="PekingU/rtdetr_v2_r18vd")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--weights-out", required=True, help="완성 가중치를 놓을 디렉토리")
    parser.add_argument("--run-name", required=True)
    args = parser.parse_args()

    ds = Path(args.dataset)
    root = ds.parent
    status_file = root / "_training.json"
    run_dir = ds / "runs" / args.run_name

    def status(state: str, **extra) -> None:
        status_file.write_text(json.dumps({
            "state": state, "dataset": ds.name, "run_name": args.run_name,
            "base_model": args.model, "epochs": args.epochs, "imgsz": args.imgsz,
            "at": time.time(), **extra,
        }, ensure_ascii=False))

    classes = json.loads((ds / "classes.json").read_text())
    train_files, val_files = write_split(ds, run_dir)
    n_train, n_val = len(train_files), len(val_files)
    print(f"분할: train {n_train} / val {n_val} (그룹 단위)", flush=True)
    status("running", train=n_train, val=n_val)

    try:
        import rtdetr_train

        # ⚠ **결과물이 디렉토리다.** `.pt` 단일 파일은 ultralytics 형식이고, 그걸
        #   열려면 AGPL 라이브러리가 필요하다. HF 형식은 config+safetensors+전처리기
        #   가 한 디렉토리에 들어간다 — `load_detector` 가 경로로 그대로 연다.
        out_root = Path(args.weights_out)
        stamp = time.strftime("%m%d-%H%M")
        weight_name = f"{ds.name}-{stamp}"
        weight_dir = out_root / weight_name

        metrics = rtdetr_train.train(
            model_id=args.model, train_files=train_files, val_files=val_files,
            labels_dir=ds / "labels", classes=classes,
            epochs=args.epochs, batch=args.batch, device=args.device,
            out_dir=weight_dir, results_csv=run_dir / "results.csv",
            log=lambda m: print(m, flush=True),
        )

        # 곁 JSON — 카탈로그가 드롭다운 설명에 쓴다 (4단계).
        # ⚠ 디렉토리 **안**에 둔다. 밖에 두면 가중치를 지울 때 남는다.
        (weight_dir / "piper_meta.json").write_text(json.dumps({
            "dataset": ds.name, "base_model": args.model, "epochs": args.epochs,
            "imgsz": args.imgsz, "classes": classes,
            "train": n_train, "val": n_val, **metrics,
        }, ensure_ascii=False))

        status("done", weight=weight_name, **metrics)
        print(f"수확: {weight_name} (mAP50 {metrics['map50']})", flush=True)

        # runs/ 정리 — 결과는 가중치+곁 JSON 으로 옮겨졌다
        shutil.rmtree(run_dir, ignore_errors=True)
    except SystemExit:
        raise
    except Exception as e:
        status("failed", error=str(e)[:500])
        print(f"학습 실패: {e}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    main()
