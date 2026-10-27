"""이어 학습 base 의 state/action 길이를 데이터셋에 맞춘 **사본**을 만든다.

⚠ **표준 라이브러리만 쓴다.** 이 파일은 원격 학습 스크립트에 **원문 그대로** 실려 간다
(`services/training/runners/ssh.py`) — 임대 서버에는 우리 코드가 없다. `app.*` 를 import
하면 거기서 `ModuleNotFoundError` 로 죽는다. 로컬(`cli_mapping.align_pretrained_to_dataset`)과
원격이 **같은 판정**을 쓰게 하려는 것이므로, 판정을 고치면 이 한 파일만 고친다.

왜 필요한가 — LeRobot 은 이어 학습이면 base 의 `input_features` 를 그대로 두고
`output_features` 만 데이터셋에서 덮어쓴다. SO100 용 `smolvla_base`(6)를 7축 데이터셋으로
이어 학습하면 체크포인트가 state 6 / action 7 / 통계 7 로 어긋나 저장되고, 추론에서
`(6) must match (7)` 로 죽는다 (실기 2026-10-14. 학습은 에러 없이 끝까지 간다).

같은 사본이 **워밍스타트**도 맡는다 (`retyped_config`) — `smolvla` 체크포인트를 SmolVLA-S 로,
`act` 를 ACT-S 로 이어 학습하려면 config 의 `type` 만 바꿔 열면 된다. 둘 다 원래 길이 맞춤과
같은 문제(base 는 공유 캐시라 못 고친다, 원격도 같은 판정)라 한 자리에 둔다.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

# 원격 학습에서 워밍스타트 지시를 받는 환경변수 — 학습 스크립트가 `export` 한다 (`_remote_main`)
WARM_START_ENV = "PIPER_WARM_START"


def dataset_dims(info: dict) -> tuple[int, int]:
    """`meta/info.json` 의 `observation.state`·`action` 길이. 못 읽으면 `(0, 0)`."""
    feats = info.get("features", {}) if isinstance(info, dict) else {}

    def _len(key: str) -> int:
        shape = (feats.get(key) or {}).get("shape") or []
        return int(shape[0]) if len(shape) == 1 else 0

    return _len("observation.state"), _len("action")


def aligned_config(cfg: dict, want_state: int, want_action: int) -> tuple[dict | None, str]:
    """`config.json` 을 데이터셋 길이에 맞춘 새 dict. 맞추지 않으면 `(None, 사유)`.

    사유가 **빈 문자열**이면 정상적으로 할 일이 없는 경우(이미 같거나, 대상 정책이 아님)이고,
    문장이면 맞춰야 했을 수 있는데 못 맞춘 경우라 호출부가 로그에 남긴다. 문장을 비교해서
    분기하지 않도록 이 구분을 값(빈 것/아닌 것)으로 둔다.

    ⚠ **패딩하는 정책만 대상이다.** config 에 `max_state_dim`/`max_action_dim` 이 있는
    정책(smolvla·pi0 계열)은 입력을 그 길이로 0 패딩하므로 가중치가 데이터셋 차원에 묶이지
    않는다 — 가중치 폭이 `[*, 32]` 인 것을 실측했다. ACT 같은 정책은 가중치가 차원에 묶여
    있어 config 만 바꾸면 `load_state_dict` 에서 죽는다 — 그 경우는 건드리지 않는다.
    """
    max_state, max_action = cfg.get("max_state_dim"), cfg.get("max_action_dim")
    if not (max_state and max_action):
        return None, ""   # 가중치가 길이에 묶인 정책 — 대상이 아니다
    if not (want_state and want_action):
        return None, "데이터셋의 state/action 길이를 읽지 못했습니다"
    if want_state > max_state or want_action > max_action:
        return None, (f"데이터셋 길이(state {want_state}, action {want_action})가 모델 최대"
                      f"({max_state}, {max_action})를 넘어 맞추지 않습니다")
    state_ft = (cfg.get("input_features") or {}).get("observation.state")
    action_ft = (cfg.get("output_features") or {}).get("action")
    if not (state_ft and action_ft):
        return None, "config 에 state/action feature 가 없습니다"
    if state_ft.get("shape") == [want_state] and action_ft.get("shape") == [want_action]:
        return None, ""   # 이미 같다
    new = json.loads(json.dumps(cfg))
    new["input_features"]["observation.state"]["shape"] = [want_state]
    new["output_features"]["action"]["shape"] = [want_action]
    return new, ""


def retyped_config(cfg: dict, warm_start: dict | None) -> dict | None:
    """워밍스타트 — base 를 **다른 정책 타입으로** 여는 config. 할 일이 없으면 None.

    `warm_start = {"to": "smolvla_s", "from": ["smolvla"], "fill": {...}}`. base 의 `type` 이
    `from` 에 있을 때만 `type` 을 `to` 로 바꾸고, `fill` 중 **base config 에 없는 키만** 넣는다 —
    새 정책이 더한 필드(stage 헤드 설정)다. base 에 있는 키는 base 가 이긴다: 이어 학습은
    아키텍처를 체크포인트에서 가져온다는 규칙 그대로다(학습 인자는 `--policy.*` 를 버린다).

    가중치는 그대로 실리고(LeRobot `from_pretrained(strict=False)`) 새 정책이 더한 층만 무작위로
    시작한다. 실측: smolvla_base → SmolVLA-S 는 505개 중 500개, ACT → ACT-S 는 239개 중 234개가
    실리고 빈 것은 stage 헤드뿐이다. 버려지는 가중치는 0개다.

    ⚠ `type` 을 안 바꾸고 이어 학습하면 **바닐라로** 학습된다 — LeRobot 이 type 을 체크포인트에서
    가져오기 때문이다. 화면은 SmolVLA-S 를 골랐는데 SmolVLA 가 학습되는 것이 그 실패다.
    """
    if not warm_start or not warm_start.get("to"):
        return None
    if cfg.get("type") not in (warm_start.get("from") or []):
        return None
    new = json.loads(json.dumps(cfg))
    new["type"] = warm_start["to"]
    for k, v in (warm_start.get("fill") or {}).items():
        new.setdefault(k, v)
    return new


def make_copy(src: Path, cfg: dict, dest_root: Path, want_state: int, want_action: int,
              warm_start: dict | None = None) -> Path:
    """`src` 와 같되 `config.json` 만 `cfg` 인 사본 디렉터리. 가중치·프로세서는 링크다.

    ⚠ **원본을 고치지 않는다.** HF 캐시 스냅샷이면 `config.json` 이 blob 심볼릭 링크라
    그 자리에서 고치면 공유 캐시가 오염된다 — 6축 데이터셋으로 다음 학습을 걸 때 7 이
    새어 나간다. 같은 입력이면 같은 경로라 다시 만들어도 같다.

    워밍스타트 사본은 이름에 대상 타입이 붙고 키에 지시 전체가 들어간다 — 길이만 맞춘 사본과
    같은 자리를 쓰면 한쪽이 다른 쪽 config 를 덮는다.
    """
    # 스냅샷이면 디렉터리 이름이 커밋 해시뿐이라 모델 저장소 이름을 쓴다
    base_name = src.parent.parent.name if src.parent.name == "snapshots" else src.name
    label = re.sub(r"[^\w.-]+", "_", base_name)
    ident = f"{src.resolve()}|{want_state}|{want_action}"
    tag = ""
    if warm_start:
        ident += "|" + json.dumps(warm_start, sort_keys=True)
        tag = "-" + re.sub(r"[^\w.-]+", "_", str(warm_start.get("to", "")))
    key = hashlib.sha1(ident.encode()).hexdigest()[:8]
    dest = dest_root / f"{label}-s{want_state}a{want_action}{tag}-{key}"
    dest.mkdir(parents=True, exist_ok=True)
    for stale in dest.iterdir():  # 링크와 config.json 뿐이다 — 대상 파일은 건드리지 않는다
        stale.unlink()
    for entry in src.iterdir():
        if entry.name != "config.json":
            (dest / entry.name).symlink_to(entry.resolve())
    (dest / "config.json").write_text(json.dumps(cfg, indent=4))
    return dest


def align(src: Path, info: dict, dest_root: Path,
          warm_start: dict | None = None) -> tuple[str, str]:
    """`(쓸 경로, 한 줄 설명)`. 맞출 필요가 없으면 `src` 그대로, 설명은 빈 문자열이다.

    `warm_start` 는 `retyped_config` 의 지시다 — 길이 맞춤과 한 사본에 같이 담는다.
    """
    config_path = src / "config.json"
    try:
        cfg = json.loads(config_path.read_text())
    except Exception as exc:                                        # noqa: BLE001
        return str(src), f"base 의 config.json 을 읽지 못했습니다: {exc}"
    want_state, want_action = dataset_dims(info)
    new, why = aligned_config(cfg, want_state, want_action)
    retyped = retyped_config(new or cfg, warm_start)
    if retyped is not None:
        dest = make_copy(src, retyped, dest_root, want_state, want_action, warm_start)
        dims = f", state {want_state}, action {want_action}" if new is not None else ""
        note = (f"{cfg.get('type')} base 를 {retyped['type']} 로 여는 사본을 씁니다 "
                f"(워밍스타트 — 새 정책이 더한 층만 무작위로 시작{dims}): {dest}")
        return str(dest), (f"{note} / {why}" if why else note)
    if new is None:
        return str(src), why
    dest = make_copy(src, new, dest_root, want_state, want_action)
    return str(dest), (f"base 의 state/action 길이를 데이터셋에 맞춘 사본을 씁니다 "
                       f"(state {want_state}, action {want_action}): {dest}")


# ── 원격 진입점 ──────────────────────────────────────────────────────────────
# `python - <base> <dataset> <dest_root>` 로 불린다. 마지막 stdout 줄이 쓸 경로다 —
# 무엇이 잘못돼도 **base 를 그대로** 찍어 학습이 멈추지 않게 한다. 대신 이유를 stderr
# (= 학습 로그)에 남긴다. 조용히 건너뛰면 몇 시간 뒤에 어긋난 체크포인트로 알게 된다.
# 워밍스타트 지시는 인자가 아니라 환경변수(`WARM_START_ENV`, JSON)로 온다 — 학습 스크립트가
# 이 줄보다 먼저 `export` 하므로 러너가 명령을 다시 조립할 필요가 없다.


def _remote_main(argv: list[str]) -> int:
    base, dataset, dest_root = argv[0], argv[1], Path(argv[2])
    try:
        warm_start = json.loads(os.environ.get(WARM_START_ENV) or "null")
        src = Path(base)
        if not src.is_dir():
            from huggingface_hub import snapshot_download

            src = Path(snapshot_download(base))
        # ⚠ 데이터셋 길이를 못 읽어도 **워밍스타트는 한다.** 길이 맞춤이 빠지면 추론에서 죽는
        #   체크포인트가 나오지만, type 바꾸기가 빠지면 고른 정책이 아니라 바닐라가 학습된다.
        try:
            ds = Path(dataset)
            if ds.is_dir():  # 로컬 루트 (테스트·직접 실행)
                info_path = ds / "meta" / "info.json"
            else:
                from huggingface_hub import hf_hub_download

                info_path = Path(hf_hub_download(dataset, "meta/info.json", repo_type="dataset"))
            info = json.loads(info_path.read_text())
        except Exception as exc:                                    # noqa: BLE001
            if not warm_start:
                raise
            print(f"[piper] 데이터셋 길이를 못 읽어 길이 맞춤 없이 워밍스타트만 합니다 "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
            info = {}
        path, note = align(src, info, dest_root, warm_start)
        if note:
            print(f"[piper] {note}", file=sys.stderr)
        print(path)
    except Exception as exc:                                        # noqa: BLE001
        what = "base 길이 맞춤과 워밍스타트를" if os.environ.get(WARM_START_ENV) else "base 길이 맞춤을"
        print(f"[piper] {what} 건너뜁니다 ({type(exc).__name__}: {exc})", file=sys.stderr)
        print(base)
    return 0


if __name__ == "__main__":
    sys.exit(_remote_main(sys.argv[1:]))
