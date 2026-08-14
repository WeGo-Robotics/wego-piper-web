#!/usr/bin/env python3
"""`policies/*.yaml` 의 학습 기본값을 **LeRobot config 클래스에서** 채운다.

## 왜 손으로 안 적나

지금 값들은 사람이 LeRobot config 를 보고 옮긴 것이다 — 옛 `POLICY_TRAIN_SCHEMAS`
주석에 그렇게 적혀 있다. YAML 로 형식만 바꾸면 드리프트가 한 층 위로 올라갈 뿐이다.
LeRobot 을 올릴 때 기본값이 바뀌면 **아무도 모른다.**

## 의도적 이탈은 지운다

`override.value` 가 있으면 그 값을 유지한다. 다만 상류가 바뀌면
`tests/test_policy_spec.py` 가 알려준다 — "false→true 로 바뀌었으니 override 를
다시 보라". **베끼기와 이탈을 구분하는 것**이 이 스크립트의 존재 이유다.

사용법:
    python tools/gen_policy_spec.py            # 확인만 (변경 없음)
    python tools/gen_policy_spec.py --write    # 파일에 반영
"""

import argparse
import dataclasses
import re
import importlib
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
POLICY_DIR = REPO / "policies"


def lerobot_defaults(config_path: list[str]) -> dict:
    """`[모듈, 클래스]` → `{필드: 기본값}`. 못 읽으면 빈 dict."""
    if len(config_path) != 2:
        return {}
    module_name, class_name = config_path
    try:
        cls = getattr(importlib.import_module(module_name), class_name)
    except Exception as exc:
        print(f"  ⚠ {module_name}.{class_name} 를 못 읽었다: {exc}")
        return {}
    out = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            out[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            try:
                out[f.name] = f.default_factory()            # type: ignore[misc]
            except Exception:
                pass
    return out


def refresh(path: Path, write: bool = False) -> tuple[list[str], bool]:
    """한 파일. `(변경 내역, 바뀐 게 있나)`."""
    data = yaml.safe_load(path.read_text()) or {}
    fields = (data.get("train") or {}).get("fields") or []
    upstream = lerobot_defaults((data.get("runtime") or {}).get("config") or [])
    if not upstream:
        return [f"{path.name}: config 클래스를 못 읽어 건너뜀"], False

    notes, changed = [], False
    for f in fields:
        if not f.get("from_lerobot"):
            continue
        key = f["key"]
        if key not in upstream:
            notes.append(f"  ✗ {key}: LeRobot 에 그런 필드가 없다 — 오타이거나 상류에서 사라졌다")
            continue
        want = upstream[key]
        if f.get("default") != want:
            notes.append(f"  · {key}: {f.get('default')!r} → {want!r}")
            changed = True
            if write:
                patch(path, key, want)
        if (ov := f.get("override")) is not None and ov.get("value") == want:
            notes.append(f"  ? {key}: override 값이 상류와 같아졌다 — 이탈을 지워도 된다")

    return notes, changed


def patch(path: Path, key: str, want) -> bool:
    """`default:` 한 칸만 바꾼다. **파일을 통째로 다시 쓰지 않는다** —
    `yaml.safe_dump` 로 재출력하면 주석이 전부 날아가고, 주석이 달린다는 게
    YAML 을 고른 이유다. 흐름 스타일(`{...}`)과 블록 스타일 둘 다 처리한다."""
    text = path.read_text()
    literal = str(want).lower() if isinstance(want, bool) else repr(want).strip("'")

    # 흐름 스타일 한 줄: `- { key: chunk_size, ..., default: 100 }`
    flow = re.compile(rf"^(\s*-\s*\{{[^}}\n]*\bkey:\s*{re.escape(key)}\b[^}}\n]*?)"
                      rf"(,\s*default:\s*[^,}}\n]+)?(\s*\}})\s*$", re.M)
    if (m := flow.search(text)) is not None:
        path.write_text(flow.sub(rf"\g<1>, default: {literal}\g<3>", text, count=1))
        return True

    # 블록 스타일: `- key: load_vlm_weights` 아래의 `default:`
    block = re.compile(rf"^(\s*)- key: {re.escape(key)}\b(.*?)(?=^\s*- |\Z)", re.M | re.S)
    if (m := block.search(text)) is not None:
        chunk = m.group(0)
        if re.search(r"^\s+default:.*$", chunk, re.M):
            new_chunk = re.sub(r"^(\s+)default:.*$", rf"\g<1>default: {literal}",
                               chunk, count=1, flags=re.M)
        else:
            new_chunk = chunk.rstrip("\n") + f"\n{m.group(1)}  default: {literal}\n"
        path.write_text(text.replace(chunk, new_chunk, 1))
        return True

    print(f"  ⚠ {path.name}: {key} 항목을 못 찾아 손대지 못했다")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="파일에 반영 (기본은 확인만)")
    args = ap.parse_args()

    dirty = False
    for path in sorted(POLICY_DIR.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        notes, changed = refresh(path, write=args.write)
        if notes:
            print(path.name)
            print("\n".join(notes))
        dirty = dirty or changed

    if dirty and not args.write:
        print("\n상류와 다른 값이 있다. `--write` 로 반영하거나, 의도된 차이면 "
              "`override.value` + `reason` 을 적어라.")
        return 1
    print("\n동기화됨." if not dirty else "\n반영 완료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
