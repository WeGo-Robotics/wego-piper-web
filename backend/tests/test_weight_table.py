"""가중치 표 — 학습 하나가 한 행 (`GET /api/models/runs`, model_scanner.scan_runs).

select 한 줄로는 학습 36개·체크포인트 207개(실측)가 날짜로만 읽혔다. 표는 학습 단위로
묶고, 사람이 고르는 데 필요한 것(이름·데이터셋·스텝·출발점)을 한 줄에 싣는다.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services import notes_sidecar as ns

REPO = Path(__file__).resolve().parents[2]


def _ckpt(run: Path, step: str, *, policy="act", pretrained=None, dataset="org/pick") -> Path:
    pm = run / "checkpoints" / step / "pretrained_model"
    pm.mkdir(parents=True)
    (pm / "config.json").write_text(json.dumps({"type": policy}))
    (pm / "model.safetensors").write_bytes(b"w" * 8)
    (pm / "train_config.json").write_text(json.dumps({
        "steps": 20000, "batch_size": 64, "optimizer": {"lr": 1e-4},
        "dataset": {"repo_id": dataset}, "policy": {"type": policy, "pretrained_path": pretrained}}))
    return pm


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "model_paths", property(lambda self: [tmp_path]))
    return tmp_path


def test_last_is_folded_into_the_step_it_points_to(root):
    """⚠ `checkpoints/last` 는 마지막 스텝 폴더로 가는 **링크**다. 따로 세면 같은 가중치가
    두 번 보인다. 한 칸으로 합치고, 고르는 id 는 스텝 쪽 — `last` 는 학습이 이어지면
    다른 폴더를 가리킨다. 예전에 고른 `…/last` 도 그 칸으로 알아봐야 한다."""
    from app.services.model_scanner import scan_runs

    run = root / "2026-10-23" / "16-25-04_act"
    for step in ("010000", "020000"):
        _ckpt(run, step)
    (run / "checkpoints" / "last").symlink_to("020000")

    [row] = scan_runs()
    assert row["kind"] == "run" and row["run"] == "2026-10-23/16-25-04_act"
    assert [c["checkpoint"] for c in row["checkpoints"]] == ["020000", "010000"], "last 가 따로 셌다"
    final = row["checkpoints"][0]
    assert final["final"] and final["id"] == "2026-10-23/16-25-04_act/020000"
    assert "2026-10-23/16-25-04_act/last" in final["ids"]
    assert (row["steps"], row["batch_size"], row["lr"], row["dataset"]) == (20000, 64, 1e-4, "org/pick")
    assert row["base"] is None, "처음부터 학습인데 출발점이 있다"


def test_the_base_is_found_through_a_derived_copy(root, tmp_path_factory):
    """길이를 맞추거나 워밍스타트한 학습은 `derived_pretrained/…` **사본**에서 출발한다
    (core/base_align.py). 사본은 원본 파일로 가는 링크라, 가중치를 따라가면 원본이 나온다.
    경로 그대로 보이면 사람은 `models--lerobot--smolvla_base-s7a7-5fcdaf89` 를 읽는다."""
    from app.services.model_scanner import scan_runs

    hub = tmp_path_factory.mktemp("hub") / "models--lerobot--smolvla_base" / "snapshots" / "abc"
    hub.mkdir(parents=True)
    (hub / "model.safetensors").write_bytes(b"w")
    derived = tmp_path_factory.mktemp("derived") / "models--lerobot--smolvla_base-s7a7-5fcdaf89"
    derived.mkdir()
    (derived / "model.safetensors").symlink_to(hub / "model.safetensors")

    parent = root / "2026-10-22" / "11-30-34_act"
    _ckpt(parent, "020000")
    (parent / "checkpoints" / "last").symlink_to("020000")
    # 로컬 학습에서 이어 받은 것 — `last` 경로로 적혀 있어도 그 학습의 스텝으로 찾는다
    child = root / "2026-10-22" / "15-33-42_act"
    _ckpt(child, "005000", pretrained=str(parent / "checkpoints" / "last" / "pretrained_model"))
    warm = root / "2026-10-27" / "11-43-50_act"
    _ckpt(warm, "005000", pretrained=str(derived))

    rows = {r["key"]: r for r in scan_runs()}
    assert rows["2026-10-27/11-43-50_act"]["base"]["label"] == "lerobot/smolvla_base"
    b = rows["2026-10-22/15-33-42_act"]["base"]
    assert (b["run"], b["checkpoint"]) == ("2026-10-22/11-30-34_act", "020000")


def test_the_shown_name_prefers_the_run_then_a_named_checkpoint(root):
    """이름 — 학습 폼 제목(run 루트)이 먼저, 없으면 모델 페이지에서 체크포인트에 붙인 이름.
    `notes` 는 run 사이드카 **그대로**다 — 표가 이름만 고칠 때 설명을 지우지 않게."""
    from app.services.model_scanner import scan_runs

    run = root / "2026-09-05" / "12-15-45_act"
    _ckpt(run, "040000")
    pm = _ckpt(run, "050000")
    ns.write_notes(pm, kind="model", name="볼트 분류", description="")
    [row] = scan_runs()
    assert row["name"] == "볼트 분류" and not row["notes"].get("name"), "체크포인트 이름이 run 사이드카인 척했다"

    ns.write_notes(run, kind="model", name="볼트 분류 v2", description="주간 조명")
    [row] = scan_runs()
    assert row["name"] == "볼트 분류 v2" and row["notes"]["description"] == "주간 조명"
    own = {c["checkpoint"]: c["name"] for c in row["checkpoints"]}
    assert own["050000"] == "볼트 분류", "체크포인트에 따로 붙인 이름이 사라졌다"


def test_run_notes_go_to_the_run_root_and_only_for_known_runs(root):
    """표의 이름 편집은 학습 폼과 **같은 파일**(run 루트 piper_notes.json)에 쓴다.
    스캔에 없는 학습이면 404 — 임의 경로에 사이드카를 남기지 않는다.
    ⚠ `/runs…` 는 `/{model_id:path}` 캐치올보다 위에 있어야 한다."""
    from app.main import app

    run = root / "2026-10-26" / "18-20-01_act"
    _ckpt(run, "020000")
    c = TestClient(app)
    assert c.get("/api/models/runs").json()[0]["run"] == "2026-10-26/18-20-01_act"
    r = c.put("/api/models/runs/2026-10-26/18-20-01_act/notes", json={"name": "볼트 너트", "description": ""})
    assert r.status_code == 200
    assert ns.read_notes(run, kind="model")["name"] == "볼트 너트"
    assert c.get("/api/models/runs/2026-10-26/18-20-01_act/notes").json()["name"] == "볼트 너트"
    assert c.put("/api/models/runs/../../etc/notes", json={"name": "x"}).status_code == 404


def test_the_three_places_that_pick_weights_open_the_table():
    """추론·학습(Fine-tune)은 표를 창으로 띄우고, 저장소는 표가 본문이다."""
    src = REPO / "frontend" / "src"
    for page in ("InferencePage.tsx", "TrainingPage.tsx"):
        assert "<WeightTableModal" in (src / "pages" / page).read_text(), f"{page} 에 표 창이 없다"
    assert "<WeightTable " in (src / "pages" / "ModelsPage.tsx").read_text()
    modal = (src / "components" / "WeightTableModal.tsx").read_text()
    # Esc 는 전역 E-stop 이다 — 창을 닫으려던 손이 로봇을 세우면 안 된다
    assert "'Escape'" not in modal and "window.confirm(" not in modal


def test_the_training_date_is_when_it_started_not_the_last_save(root):
    """⚠ 폴더 시각은 체크포인트가 추가될 때마다 밀린다 — 11:43 에 시작한 학습이 14:38(마지막
    저장)로 보였다. LeRobot 자동 경로는 이름이 곧 시작 시각이라 거기서 읽는다."""
    from app.services.model_scanner import scan_runs

    run = root / "2026-10-27" / "11-43-50_act"
    _ckpt(run, "020000")
    [row] = scan_runs()
    assert row["created"] == "2026-10-27T11:43:50"


def test_the_episode_page_picks_its_dataset_in_a_window():
    """에피소드 페이지는 데이터셋을 고른다. select 에는 ID 만 보여 이름·길이·카메라가 안 읽혔다 —
    select 를 없애고 지금 것의 이름만 두고, [선택하기] 창에서 표 | 썸네일로 고른다.
    고른 것이 없으면 가장 최근 데이터셋을 연다."""
    src = REPO / "frontend" / "src"
    page = (src / "pages" / "EpisodesPage.tsx").read_text()
    assert "<DatasetPickerModal" in page and "t('선택하기')" in page
    head = page.split("{/* ⚠ select 를 두지 않는다", 1)[1].split("{datasetPicker && (", 1)[0]
    assert "<select" not in head, "머리줄에 select 가 남았다"
    assert "t('선택된 데이터셋 없음')" in head
    assert "autoPicked" in page, "고른 것이 없을 때 최근 데이터셋을 안 연다"
    listing = (src / "components" / "DatasetBrowser.tsx").read_text()
    assert "'thumbs'" in listing and "/thumbnail?v=" in listing and 'loading="lazy"' in listing
    # 이름만 고친다 — 설명은 사이드카에 있던 그대로 보낸다
    assert "description: d.notes?.description ?? ''" in listing
    for f in ("DatasetPickerModal.tsx", "DatasetBrowser.tsx"):
        body = (src / "components" / f).read_text()
        assert "'Escape'" not in body and "window.confirm(" not in body


def test_the_storage_lists_datasets_the_same_way_and_opens_details_in_a_window():
    """저장소의 데이터셋도 에피소드 페이지와 같은 목록(표 | 썸네일)이고, 누르면 상세가 창으로 뜬다.
    목록 카드에 있던 [삭제]·[Hub 업로드] 는 상세 창으로 옮겼다."""
    page = (REPO / "frontend" / "src" / "pages" / "DatasetsPage.tsx").read_text()
    assert "<DatasetBrowser " in page
    assert "lg:grid-cols-3" not in page, "왼쪽 목록 + 오른쪽 상세 격자가 남았다"
    window = page.split("{tab === 'local' && detail && (", 1)[1]
    assert "fixed inset-0" in window and "handleDelete(detail.id)" in window and "handleUpload(detail.id)" in window
    picker = (REPO / "frontend" / "src" / "components" / "DatasetPickerModal.tsx").read_text()
    assert "<DatasetBrowser " in picker, "에피소드 페이지와 저장소가 다른 목록을 쓴다"


def test_the_storage_opens_model_details_in_a_window():
    """저장소의 모델도 데이터셋과 같다 — 표에서 누르면 상세가 창으로 뜬다(행 아래로 펼치지 않는다).
    창 안에서 체크포인트를 바꿔 볼 수 있고, 학습 이름·요약·체크포인트 상세가 다 있다."""
    page = (REPO / "frontend" / "src" / "pages" / "ModelsPage.tsx").read_text()
    assert "onOpen={(row, ckpt) => setOpen(" in page and "renderDetail" not in page
    window = page.split("{tab === 'local' && open && (() => {", 1)[1]
    assert "fixed inset-0" in window
    for part in ("/models/runs/${row.run}/notes", "<RunSummary ", "<CheckpointChip ", "<CheckpointDetail "):
        assert part in window, f"상세 창에 {part} 가 없다"
    # 지운 뒤에도 창이 옛 목록을 붙들지 않는다 — 다시 읽은 목록에서 찾는다
    assert "runs.find((r) => r.key === open.key)" in window
    table = (REPO / "frontend" / "src" / "components" / "WeightTable.tsx").read_text()
    assert "else if (onOpen) onOpen(r, r.checkpoints[0].id)" in table


def test_inference_shows_only_the_chosen_checkpoint_and_a_pick_button():
    """추론도 에피소드 페이지와 같다 — select 를 없애고 고른 것의 이름만, 고르는 것은 [선택하기] 창(표)."""
    page = (REPO / "frontend" / "src" / "pages" / "InferencePage.tsx").read_text()
    card = page.split("{/* 모델 */}", 1)[1].split("{/* 모델 요구사항", 1)[0]
    assert "<select" not in card and "CheckpointPicker" not in page
    assert "<PickerField " in card and "t('선택된 체크포인트 없음 — 눌러서 고르세요')" in card and "<WeightTableModal" in card
    assert not (REPO / "frontend" / "src" / "components" / "CheckpointPicker.tsx").exists()


def test_fine_tune_shows_only_the_chosen_base_and_a_pick_button():
    """학습의 Fine-tune 도 추론과 같다 — select 를 없애고 고른 것의 이름만, [선택하기] 창에서 고른다.
    ⚠ 예전 select 는 저장된 경로가 목록에 없으면 "처음부터 학습" 으로 **보이면서** 그 경로로 학습을
    걸었다. 이제는 경로를 그대로 보이고 목록에 없다고 말하며, 처음부터로 되돌리는 버튼이 있다."""
    page = (REPO / "frontend" / "src" / "pages" / "TrainingPage.tsx").read_text()
    field = page.split("t('Fine-tune 모델 (선택)')", 1)[1].split("{weightTable && (", 1)[0]
    assert "<select" not in field
    assert "<CheckpointLabel model={baseModel}" in field and "<PickerField " in field
    # ⚠ 글자만 두면 안내인지 값인지, 눌러야 하는지가 안 읽혔다 — 입력칸 모양의 상자, 비면 회색 안내문
    assert "placeholder={t('선택 안 함 — 처음부터 학습')}" in field
    assert "t('고르지 않아도 됩니다" in field, "고르지 않아도 된다는 말이 없다"
    assert "t('목록에 없는 경로입니다" in field, "목록에 없는 경로를 처음부터처럼 보인다"
    assert "onClear={() => { setPretrainedPath('')" in field, "처음부터 학습으로 되돌릴 길이 없다"
    inference = (REPO / "frontend" / "src" / "pages" / "InferencePage.tsx").read_text()
    assert "<CheckpointLabel model={selectedModelData}" in inference, "추론과 학습이 다른 표시를 쓴다"


def test_the_picker_field_looks_and_acts_like_an_input():
    """select 를 없앤 칸 — 다른 입력칸과 같은 테두리 상자이고, 상자 전체가 누르는 자리다."""
    src = (REPO / "frontend" / "src" / "components" / "PickerField.tsx").read_text()
    assert 'role="button"' in src and "onClick={onOpen}" in src
    assert "border border-neutral-700 bg-neutral-900" in src, "입력칸과 모양이 다르다"
    # ✕ 는 지우기만 — 창을 같이 열면 안 된다
    assert "e.stopPropagation(); onClear()" in src


def test_the_training_dataset_is_picked_in_the_same_window():
    """학습의 데이터셋 칸도 select 를 없애고 [선택하기] 창(표 | 썸네일)에서 고른다.
    ⚠ 창에는 정책이 받을 수 있는 것만 넘긴다(`shown`) — 필요한 feature 가 없는 데이터셋은 여기서도 숨는다."""
    page = (REPO / "frontend" / "src" / "pages" / "TrainingPage.tsx").read_text()
    card = page.split("{/* 데이터셋 */}", 1)[1].split("{/* 정책 */}", 1)[0]
    assert "<select" not in card
    assert "<PickerField " in card and "<DatasetPickerModal datasets={shown}" in card
    assert "t('목록에 없는 데이터셋입니다" in card
    # 목록을 읽기 전에는 "없다" 고 하지 않는다 — 학습 폼은 열자마자 저장된 값을 갖고 있다
    assert "datasets.length > 0 &&" in card


def test_the_training_policy_is_picked_in_a_window_with_descriptions():
    """학습의 정책도 select 를 없애고 창에서 고른다 — 정책마다 설명과 고르기 전에 알아야 할 것
    (작업 문장을 받나·구운 데이터셋이 필요한가·무엇에서 이어 받나·처음부터면 무엇에서 시작하나)을 보인다.
    ⚠ 내용은 정책 스펙에서 온다 — 화면이 정책 이름으로 분기해 따로 적지 않는다."""
    page = (REPO / "frontend" / "src" / "pages" / "TrainingPage.tsx").read_text()
    card = page.split("{/* 정책 */}", 1)[1].split("Fine-tune 모델 (선택)", 1)[0]
    assert "<select" not in card and "<PolicyPickerModal policies={trainable}" in card
    # 정책을 바꾸는 길은 하나 — 안 맞는 이어 학습 체크포인트를 비우는 그 함수
    assert "handlePolicyChange(type)" in card
    modal = (REPO / "frontend" / "src" / "components" / "PolicyPickerModal.tsx").read_text()
    for field in ("p.description", "p.language", "p.requires_features", "p.warm_start_from", "p.scratch_note", "p.policy_base"):
        assert field in modal, f"창이 {field} 를 안 보인다"
    import re
    assert not re.search(r"""(type|policy\w*) === ['"]""", modal), "화면이 정책 이름으로 분기한다"
    assert "'Escape'" not in modal and "window.confirm(" not in modal
