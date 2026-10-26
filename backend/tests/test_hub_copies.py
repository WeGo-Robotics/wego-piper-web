"""HF 허브 캐시 데이터셋의 사본·사이드카 — 실기 사고(2026-10-25, `wego-mink/singlearm_apple_random_v2`).

1. 스냅샷은 커밋 해시 이름만 — 원본 옆에 놓인 `<해시>_stage` 가 원본 이름을 가로채지 않는다
2. 허브에서 받은 구운·델타 사본에도 표시가 붙는다 — 편집기 잠금·[델타 변환] 숨김이 거기 걸려 있다
3. 굽기·델타 사본은 LeRobot 캐시의 `<org>/<이름>_stage|_delta` 로 — 그 이름으로 다시 찾을 수 있어야 한다
4. 페이즈 라벨 저장이 링크를 따라 **공유 캐시의 blob 을 고치지 않는다**
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from conftest import code_only

from app.core.hf_layout import latest_snapshot

_REPO = Path(__file__).resolve().parents[2]


def test_only_commit_hashes_are_snapshots(tmp_path):
    snaps = tmp_path / "datasets--org--x" / "snapshots"
    real = snaps / ("7" * 40)
    real.mkdir(parents=True)
    time.sleep(0.01)
    (snaps / ("7" * 40 + "_stage")).mkdir()                    # 더 최근이다
    assert latest_snapshot(tmp_path / "datasets--org--x") == real
    # 해시 이름이 하나도 없으면 예전처럼 아무거나
    other = tmp_path / "datasets--org--y" / "snapshots" / "manual"
    other.mkdir(parents=True)
    assert latest_snapshot(tmp_path / "datasets--org--y") == other


def test_hub_entries_carry_the_copy_marks():
    from app.services import dataset_scanner as S

    src = code_only(Path(S.__file__).read_text())
    hub = src[src.index("def _scan_hub_datasets"):src.index("def _scan_lerobot_datasets")]
    assert '"baked": baked_info(snapshot)' in hub and '"delta": delta_info(snapshot)' in hub


def test_bake_and_delta_write_their_copies_into_the_lerobot_cache():
    src = code_only((_REPO / "backend/app/routers/phase.py").read_text())
    bake = src[src.index("async def bake("):]
    assert '"--out", str(settings.lerobot_dir / org / f"{name}_stage")' in bake
    src = code_only((_REPO / "backend/app/routers/datasets.py").read_text())
    assert 'out = settings.lerobot_dir / org / f"{name}_delta"' in src


def test_label_writes_replace_the_link_instead_of_the_blob(tmp_path):
    sys.path.insert(0, str(_REPO / "phase"))
    from piper_phase import labeler as PL

    blob = tmp_path / "blobs" / "d6b1"
    blob.parent.mkdir()
    blob.write_text('{"episodes": {}}')
    before = hashlib.sha256(blob.read_bytes()).hexdigest()
    meta = tmp_path / "snap" / "meta"
    meta.mkdir(parents=True)
    link = meta / "phase_labels.json"
    os.symlink(os.path.relpath(blob, meta), link)

    PL.replace_text(link, '{"episodes": {"0": {}}}')
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == before, "공유 캐시의 blob 을 고쳤다"
    assert not link.is_symlink() and json.loads(link.read_text()) == {"episodes": {"0": {}}}
    assert not list(meta.glob(".*.tmp")), "임시 파일이 남았다"


def test_every_label_write_goes_through_the_replacing_helper():
    """편집기의 구간 저장·분석 저장 둘 다 — 한쪽만 고치면 그쪽으로 blob 이 새어 고쳐진다."""
    router = code_only((_REPO / "backend/app/routers/phase.py").read_text())
    labeler = code_only((_REPO / "phase/piper_phase/labeler.py").read_text())
    assert "labels_path.write_text(" not in router and "PL.replace_text(labels_path" in router
    assert not re.search(r"(labels_path|signals_path)\.write_text\(|to_parquet\(signals_path", labeler)


def test_the_editor_does_not_offer_reviewed_only_when_nothing_is_reviewed():
    """검토 0개에서 "예" 를 누르면 라벨 없는 데이터셋이 된다 — 묻지 않는다 (굽기도 거절한다)."""
    src = (_REPO / "frontend/src/pages/EpisodesPage.tsx").read_text()
    assert "const reviewedOnly = unreviewed > 0 && unreviewed < eps.length" in src
