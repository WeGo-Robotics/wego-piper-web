"""독립 실행 진입점 — 게이트웨이·에디터 UI 없이 데이터셋을 라벨링한다.

    python -m piper_phase /path/to/dataset                 # 분석 + 사이드카 저장 + 요약
    python -m piper_phase /path/to/dataset --episodes 0 3  # 일부만 (사이드카는 병합 저장)
    python -m piper_phase /path/to/dataset --set hold_gap=-8 --no-save
    python -m piper_phase /path/to/dataset --json          # 요약을 JSON 으로 (스크립트용)

백엔드 `/api/phase/*` 와 같은 코드(`piper_phase.labeler`)를 쓴다 — 결과도 같다.
에피소드 에디터/뷰어는 이 사이드카를 **읽는 쪽**이지 실행 전제가 아니다.
"""

import argparse
import json
import sys
from dataclasses import asdict, fields
from pathlib import Path


def _parse_params(pairs: list[str], fps: float):
    """`--set key=val` 검증 + 타입 변환. 알 수 없는 키는 **조용히 무시하지 않는다.**"""
    from piper_phase.fsm import Params

    field_types = {f.name: f.type for f in fields(Params)}
    values: dict = {"fps": fps}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set 형식은 key=value 입니다: {pair!r}")
        if key not in field_types:
            known = ", ".join(sorted(field_types))
            raise SystemExit(f"알 수 없는 파라미터: {key} (가능: {known})")
        values[key] = int(float(raw)) if field_types[key] in (int, "int") else float(raw)
    return Params(**values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m piper_phase",
        description="에피소드 phase 자동 라벨링 (사이드카 저장, 원본 불변)",
    )
    parser.add_argument("dataset", type=Path, help="데이터셋 루트 (data/ 와 meta/ 포함)")
    parser.add_argument("--episodes", type=int, nargs="+", default=None,
                        help="이 에피소드만 재분석 (사이드카는 병합 저장)")
    parser.add_argument("--set", dest="params", action="append", default=[],
                        metavar="KEY=VAL", help="라벨러 파라미터 오버라이드 (반복 가능)")
    parser.add_argument("--no-save", action="store_true", help="사이드카를 쓰지 않고 요약만")
    parser.add_argument("--json", action="store_true", help="요약을 JSON 으로 출력")
    args = parser.parse_args(argv)

    try:
        from piper_phase import labeler
    except ImportError as e:  # pandas/pyarrow 미설치
        raise SystemExit(f'의존성 누락: {e}\n→ pip install -e "phase/[labeler]"') from e

    ds = args.dataset.resolve()
    if not (ds / "data").is_dir():
        raise SystemExit(f"data/ 가 없습니다 — 데이터셋 루트가 맞습니까: {ds}")

    p = _parse_params(args.params, fps=labeler.dataset_fps(ds))
    result = labeler.analyze(ds, p, args.episodes)
    summary = labeler.summary({**result, "_signals": None})

    saved = None
    if not args.no_save:
        saved = labeler.save(ds, result)

    if args.json:
        print(json.dumps({"dataset": str(ds), "params": asdict(p), **summary},
                         ensure_ascii=False))
        return 0

    dist = " ".join(f"{c}사이클×{n}" for c, n in summary["cycle_distribution"].items())
    print(f"{ds.name}: {summary['episodes']} 에피소드, {dist} (중앙값 {summary['median_cycles']})")
    for f in summary["outliers"]:
        print(f"  ⚠ ep {f['episode']}: {', '.join(f['reasons'])}")
    if not summary["outliers"]:
        print("  이상 에피소드 없음")
    if saved:
        print(f"  사이드카: {saved[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
