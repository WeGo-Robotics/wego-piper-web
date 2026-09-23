#!/usr/bin/env python3
"""데이터셋을 Hub 에 올리고 **코드베이스 태그를 단다.**

## ⚠ 왜 태그가 따로 필요한가

lerobot 은 로컬에 데이터셋이 없으면 Hub 에 `v3.0` 같은 **코드베이스 태그**를 물어보고,
태그가 없으면 `RevisionNotFoundError` 로 죽는다 — 다운로드가 시작조차 안 된다
(`datasets/utils.py::get_safe_version`, 폴백 없음).

허브로 가는 길이 둘인데 한쪽만 태그를 달고 있었다:

| 길 | 태그 |
|---|---|
| 녹화 중 푸시 (`lerobot-record` → `dataset.push_to_hub`) | ☑ `tag_version=True` 가 기본 |
| 화면의 [업로드] (`hf upload <폴더>`) | ☒ **폴더를 통째로 올릴 뿐이다** |

그래서 화면으로 올린 데이터셋은 **임대 GPU 에서 학습이 안 됐다** — 로컬은
`--dataset.root` 로 우회되지만 원격에는 그 경로를 줄 수 없다
(실기 2026-09-23, `wego-mink/sim_two_box_3_120`).

## ⚠ 업로드 자체는 CLI 에 맡긴다

`upload-large-folder` 의 재개·재시도를 우리가 다시 짜지 않는다. 이 스크립트가 하는 일은
**CLI 를 그대로 돌리고, 끝난 뒤 태그 한 줄을 더하는 것**뿐이다.

## ⚠ 태그는 업로드 **뒤에** 단다

없는 리비전은 가리킬 수 없다. 그리고 실패했으면 달지 않는다 — 올라가지도 않은 것을
"쓸 수 있다" 고 표시하면 안 된다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def codebase_version(ds_path: Path) -> str:
    """`meta/info.json` 이 말하는 버전. 못 읽으면 빈 문자열."""
    try:
        v = json.loads((ds_path / "meta" / "info.json").read_text()).get("codebase_version")
    except Exception as exc:                                        # noqa: BLE001
        print(f"  info.json 을 못 읽었습니다: {exc}", flush=True)
        return ""
    return str(v or "").strip()


def tag(repo_id: str, version: str) -> bool:
    """이미 있으면 다시 만들지 않는다 — 덮어쓰면 옛 리비전을 가리키던 태그가 움직인다."""
    from huggingface_hub import HfApi

    api = HfApi()
    try:
        refs = api.list_repo_refs(repo_id, repo_type="dataset")
        if any(t.name == version for t in refs.tags):
            print(f"  태그 {version} 는 이미 있습니다", flush=True)
            return True
        api.create_tag(repo_id, tag=version, repo_type="dataset")
    except Exception as exc:                                        # noqa: BLE001
        # ⚠ 업로드는 성공했다. 태그를 못 달았다고 실패로 만들면, 다 올라간 것을
        #   사람이 다시 올리게 된다. 사실만 말하고 0 으로 끝낸다.
        print(f"  ⚠ 태그를 못 달았습니다 ({exc}) — 임대 GPU 학습은 막힙니다", flush=True)
        return False
    print(f"  태그 {version} 를 달았습니다", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_id")
    ap.add_argument("path", type=Path)
    ap.add_argument("--hf-cli", required=True)
    ap.add_argument("--large", action="store_true")
    ap.add_argument("--private", action="store_true")
    a = ap.parse_args()

    if a.large:
        argv = [a.hf_cli, "upload-large-folder", a.repo_id, str(a.path), "--repo-type=dataset"]
    else:
        argv = [a.hf_cli, "upload", a.repo_id, str(a.path), ".", "--repo-type=dataset"]
    if a.private:
        argv.append("--private")

    print(f"업로드: {' '.join(argv)}", flush=True)
    rc = subprocess.run(argv).returncode
    if rc != 0:
        print(f"업로드 실패 (종료 코드 {rc}) — 태그를 달지 않습니다", flush=True)
        return rc

    version = codebase_version(a.path)
    if not version:
        print("  ⚠ codebase_version 을 몰라 태그를 못 답니다 — 임대 GPU 학습은 막힙니다",
              flush=True)
        return 0
    tag(a.repo_id, version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
