"""체크포인트를 학습 단위로 묶는다.

한 학습이 체크포인트를 10~20개 남긴다. 목록 하나에 다 넣으면 금세 수십 개가
되고(실측: 학습 10개에 체크포인트 72개), 이름만으로는 어느 학습의 몇 번째인지
읽을 수가 없다.
"""

from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
SCANNER = Path(__file__).resolve().parents[1] / "app" / "services" / "model_scanner.py"


def test_the_scanner_says_which_run_a_checkpoint_belongs_to():
    """⚠ **묶는 규칙은 백엔드가 정한다.** `id` 를 화면에서 `/` 로 쪼개면 HF 허브
    모델(`PekingU/rtdetr_v2_r18vd`)까지 "PekingU 학습" 으로 묶인다 — 실제로 그렇게
    보였다. 학습 산출물만 `run` 을 갖는다."""
    src = SCANNER.read_text()
    for field in ('"run": run_id', '"checkpoint": step_label', '"step": step_num'):
        assert field in src, f"{field} 를 안 내보낸다"
    # ⚠ `last` 는 숫자가 아니다 — int() 가 터지면 스캔 전체가 죽는다
    assert "except ValueError" in src.split('"run": run_id', 1)[0][-600:], \
        "숫자가 아닌 체크포인트 이름에서 죽는다"


def test_hub_models_are_not_grouped_as_a_training_run(tmp_path):
    """`_scan_train_outputs` 는 `checkpoints/` 밑만 본다 — 허브 모델은 안 걸린다."""
    from app.services.model_scanner import _scan_train_outputs

    # 학습 산출물 하나
    ck = tmp_path / "2026-09-01" / "10-11-22_act" / "checkpoints" / "060000" / "pretrained_model"
    ck.mkdir(parents=True)
    (ck / "config.json").write_text('{"type": "act"}')
    # 허브 모델처럼 생긴 것 (checkpoints/ 가 없다)
    hub = tmp_path / "models--PekingU--rtdetr_v2_r18vd" / "snapshots" / "abc"
    hub.mkdir(parents=True)
    (hub / "config.json").write_text('{"model_type": "rt_detr"}')

    out = _scan_train_outputs(tmp_path)
    assert len(out) == 1, f"허브 모델까지 학습으로 셌다: {[m['id'] for m in out]}"
    assert out[0]["run"] == "2026-09-01/10-11-22_act"
    assert out[0]["checkpoint"] == "060000"
    assert out[0]["step"] == 60000


def test_last_has_no_step_number(tmp_path):
    """`last` 는 숫자가 아니다. 0 으로 두면 정렬에서 맨 끝으로 밀린다 —
    대개 그걸 쓰는데."""
    from app.services.model_scanner import _scan_train_outputs

    ck = tmp_path / "run_act" / "checkpoints" / "last" / "pretrained_model"
    ck.mkdir(parents=True)
    (ck / "config.json").write_text('{"type": "act"}')
    (out,) = _scan_train_outputs(tmp_path)
    assert out["checkpoint"] == "last"
    assert out["step"] is None


def test_checkpoints_sort_by_step_number_with_the_final_one_first(tmp_path, monkeypatch):
    """⚠ 문자열 정렬이면 `9000` 이 `10000` 뒤로 간다. 숫자로 정렬하고, 마지막 체크포인트
    (`last` 가 가리키는 것)를 맨 앞에 둔다 — 대개 그걸 쓴다. 고르는 화면(표)은 이 순서를 그대로 쓴다."""
    from app.core.config import Settings
    from app.services.model_scanner import scan_runs

    monkeypatch.setattr(Settings, "model_paths", property(lambda self: [tmp_path]))
    run = tmp_path / "2026-09-01" / "10-11-22_act"
    for step in ("9000", "10000", "11000"):
        pm = run / "checkpoints" / step / "pretrained_model"
        pm.mkdir(parents=True)
        (pm / "config.json").write_text('{"type": "act"}')
    (run / "checkpoints" / "last").symlink_to("10000")   # 마지막 저장이 가장 큰 스텝이 아닐 수도 있다
    [row] = scan_runs()
    assert [c["checkpoint"] for c in row["checkpoints"]] == ["10000", "11000", "9000"]
    assert row["checkpoints"][0]["final"]


def test_choosing_a_run_selects_a_checkpoint():
    """⚠ 학습만 고르고 체크포인트가 비어 있으면 "골랐는데 아무 일도 안 일어나는"
    상태가 된다 — 그게 고장으로 읽힌다. 표에서 행을 누르면 마지막 체크포인트를 고른다."""
    src = (_SRC / "components" / "WeightTable.tsx").read_text()
    assert "if (onPick) { if (!blocked) onPick(r.checkpoints[0], r) }" in src, "행을 눌러도 체크포인트가 안 잡힌다"


def test_search_finds_a_run_by_a_checkpoint_name():
    """⚠ 걸린 게 안 보이면 "검색해도 아무것도 안 나온다" 로 보인다. 저장소는 이제 학습 1행
    표라 접힌 묶음은 없지만, 체크포인트에만 붙인 이름은 행 안의 칩에 있다 — 그 이름으로도
    행이 걸려야 한다."""
    src = (_SRC / "components" / "WeightTable.tsx").read_text()
    assert "...r.checkpoints.map((c) => c.name)" in src, "체크포인트 이름으로 못 찾는다"
