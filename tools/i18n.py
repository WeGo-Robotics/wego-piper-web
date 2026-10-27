#!/usr/bin/env python3
"""번역 파일 관리 — 문장을 뽑고, 빠진 것·낡은 것·자리표시자가 어긋난 것을 찾는다.

설계는 `feature/i18n.md`. 요약:

* 키는 **한국어 원문**이다. 번역 파일은 `frontend/src/i18n/locales/<코드>.json`
  (`{ "원문": "번역" }`). 파일을 놓으면 그게 곧 언어 추가다.
* 원문은 두 곳에서 뽑는다 — 프론트 `t('…')`(구문 트리, `frontend/scripts/i18n-extract.mjs`)와
  백엔드·데몬이 만드는 한국어 문장(파이썬 AST). 백엔드 문장은 화면이 받은 글자를
  키의 **모양**(`{cam_id} 가 연결돼 있지 않습니다`)에 맞춰 번역한다.

사용:
    python tools/i18n.py status              # 언어별 진행률
    python tools/i18n.py check               # en 이 비어 있거나 어긋나면 비정상 종료
    python tools/i18n.py sync                # 모든 번역 파일에 빠진 키를 빈 값으로 추가
    python tools/i18n.py sync --prune        # + 원문에서 사라진 키를 지운다
    python tools/i18n.py add ja              # 새 언어 파일을 만든다 (모든 키, 빈 값)
    python tools/i18n.py todo ja --limit 80  # 번역할 것을 JSON 으로 (원문 → "")
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import string
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "frontend" / "src" / "i18n" / "locales"
EXTRACTOR = ROOT / "frontend" / "scripts" / "i18n-extract.mjs"

# 이 언어는 항상 완결이어야 한다 (테스트가 강제). 나머지는 진행률만 본다.
REQUIRED = ("en",)

HANGUL = re.compile(r"[가-힣]")
PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)\}")
PLURAL_CATEGORIES = {"zero", "one", "two", "few", "many", "other"}

# 화면에 나갈 수 있는 문장을 만드는 곳. 로그·CLI 도움말만 있는 곳은 뺀다.
PY_ROOTS = [
    "backend/app", "daemons", "bus", "cam", "robot", "rs", "sim", "so101", "phase", "plugin",
]
PY_SKIP_PARTS = {"tests", "__pycache__", "egg-info", "node_modules"}
# 화면에 안 나가는 파일 — 통째로 뺀다 (한국어는 있지만 번역 대상이 아니다)
PY_SKIP_FILES = {
    # 에이전트가 읽는 markdown 꾸러미 — 클립보드로 나가고 화면에는 안 뜬다 (feature/agent-cli.md §4-1)
    "backend/app/services/agent_context.py",
    # piper-plugin CLI — 터미널·훅만 본다 (print·help 는 원래 빼고, --json 결과도 같은 자리다)
    "plugin/piper_plugin/__main__.py",
    # piper-mcp — 도구 설명·오류는 에이전트가 읽는 글이다 (feature/agent-cli.md §4-3)
    "plugin/piper_plugin/mcp.py",
    # 에피소드 묶음 — README·오류가 에이전트가 읽는 글이다 (feature/agent-cli.md §4-5)
    "backend/app/services/episode_bundle.py",
    # 웹 채팅의 시스템 프롬프트 — 에이전트가 읽는 글이다 (feature/agent-cli.md §4-6)
    "backend/app/services/agent_chat_prompt.py",
}
# 로그 줄은 화면에 안 나간다 (프로세스 로그 스트림은 번역하지 않는다)
LOG_CALLS = {
    "debug", "info", "warning", "warn", "error", "exception", "critical", "log",
    "print", "_log", "write",
}
# CLI 인자 설명·종료 메시지 — 터미널에서만 본다
CLI_CALLS = {"add_argument", "ArgumentParser", "add_parser", "SystemExit", "FastAPI"}


# ── 프론트 ──

def frontend_keys() -> tuple[dict[str, list[str]], list[dict]]:
    out = subprocess.run(
        ["node", str(EXTRACTOR)], capture_output=True, text=True, cwd=ROOT / "frontend",
        check=True,
    ).stdout
    data = json.loads(out)
    keys: dict[str, list[str]] = defaultdict(list)
    for k in data["keys"]:
        keys[k["text"]].append(f'{k["file"]}:{k["line"]}')
    return keys, data["dynamic"]


# ── 파이썬 ──

def _first_name(node: ast.AST) -> str | None:
    """식의 안쪽에서 사람이 알아볼 이름 하나. 번역자가 `{cam_id}` 를 보고 뜻을 짐작하게."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str) and sl.value.isidentifier():
            return sl.value
        return _first_name(node.value)
    if isinstance(node, ast.Call):
        # len(x) · str(x) · round(x, 1) 은 안쪽 x 의 이름을 쓴다
        for a in node.args:
            n = _first_name(a)
            if n:
                return n
        return _first_name(node.func)
    for child in ast.iter_child_nodes(node):
        n = _first_name(child)
        if n:
            return n
    return None


class _Namer:
    def __init__(self) -> None:
        self.used: dict[str, int] = {}

    def __call__(self, node: ast.AST) -> str:
        base = re.sub(r"\W", "_", _first_name(node) or "value").strip("_") or "value"
        if base[0].isdigit():
            base = "v" + base
        n = self.used.get(base, 0) + 1
        self.used[base] = n
        return base if n == 1 else f"{base}_{n}"


def _template(node: ast.AST, namer: _Namer | None = None) -> str | None:
    """식 하나를 `{이름}` 자리표시자가 든 템플릿으로. 정적으로 못 읽으면 None."""
    namer = namer or _Namer()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                out.append("{" + namer(v.value) + "}")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _template(node.left, namer), None
        right = _template(node.right, namer)
        l = left if left is not None else "{" + namer(node.left) + "}"
        r = right if right is not None else "{" + namer(node.right) + "}"
        return l + r
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
            and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
        return _percent(node.left.value, node.right, namer)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)):
        return _format_call(node, namer)
    return None


_PERCENT = re.compile(r"%(?:\((\w+)\))?[#0\- +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?([diouxXeEfFgGcrsa%])")


def _percent(fmt: str, right: ast.AST, namer: _Namer) -> str:
    args = list(right.elts) if isinstance(right, ast.Tuple) else [right]
    it = iter(args)

    def sub(m: re.Match[str]) -> str:
        if m.group(2) == "%":
            return "%"
        if m.group(1):
            return "{" + m.group(1) + "}"
        a = next(it, None)
        return "{" + (namer(a) if a is not None else "value") + "}"

    return _PERCENT.sub(sub, fmt)


def _format_call(call: ast.Call, namer: _Namer) -> str:
    fmt = call.func.value.value  # type: ignore[attr-defined]
    out, auto = [], 0
    for lit, field, _spec, _conv in string.Formatter().parse(fmt):
        out.append(lit)
        if field is None:
            continue
        if field == "" or field.isdigit():
            idx = auto if field == "" else int(field)
            auto += field == ""
            arg = call.args[idx] if idx < len(call.args) else None
            out.append("{" + (namer(arg) if arg is not None else "value") + "}")
        else:
            out.append("{" + re.sub(r"\W", "_", field.split(".")[0].split("[")[0]) + "}")
    return "".join(out)


def python_keys() -> dict[str, list[str]]:
    keys: dict[str, list[str]] = defaultdict(list)
    for root in PY_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if PY_SKIP_PARTS & set(path.parts) or any(p.endswith(".egg-info") for p in path.parts):
                continue
            if str(path.relative_to(ROOT)) in PY_SKIP_FILES:
                continue
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            _scan(tree, path.relative_to(ROOT), keys)
    return keys


def _scan(tree: ast.Module, rel: Path, keys: dict[str, list[str]]) -> None:
    parent: dict[ast.AST, ast.AST] = {}
    docstrings: set[int] = set()
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[c] = n
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(n.body[0].value, ast.Constant):
            docstrings.add(id(n.body[0].value))

    def skipped(node: ast.AST) -> bool:
        if id(node) in docstrings:
            return True
        cur: ast.AST | None = parent.get(node)
        while cur is not None:
            if isinstance(cur, ast.Call):
                f = cur.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if name in LOG_CALLS or name in CLI_CALLS:
                    return True
            if isinstance(cur, ast.keyword) and cur.arg in {"help", "epilog"}:
                return True
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                return False
            cur = parent.get(cur)
        return False

    def wanted(node: ast.AST) -> bool:
        """`a + b` 의 `a` 만 따로 잡지 않는다 — 가장 바깥 식이 한 문장이다."""
        p = parent.get(node)
        if isinstance(p, ast.JoinedStr) or isinstance(p, ast.FormattedValue):
            return False
        if isinstance(p, ast.BinOp) and isinstance(p.op, (ast.Add, ast.Mod)):
            return False
        if isinstance(p, ast.Attribute) and p.attr == "format" and p.value is node \
                and isinstance(parent.get(p), ast.Call):
            return False
        return True

    for n in ast.walk(tree):
        if not isinstance(n, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
            continue
        if isinstance(n, ast.Constant) and not isinstance(n.value, str):
            continue
        if isinstance(n, ast.BinOp) and not isinstance(n.op, (ast.Add, ast.Mod)):
            continue
        if not wanted(n) or skipped(n):
            continue
        tpl = _template(n)            # 호출은 `"…".format(…)` 만 문장이다 — 나머지는 None
        if tpl is None or not HANGUL.search(tpl):
            continue
        # `"…" + x` 에서 한글이 상수 쪽에 있어야 문장이다. `x + y` 는 위에서 걸러진다.
        keys[tpl].append(f"{rel}:{n.lineno}")


# ── 번역 파일 ──

def load_catalog(code: str) -> dict:
    path = LOCALES / f"{code}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def locale_codes() -> list[str]:
    return sorted(p.stem for p in LOCALES.glob("*.json"))


def save_catalog(code: str, catalog: dict) -> None:
    LOCALES.mkdir(parents=True, exist_ok=True)
    body = json.dumps(dict(sorted(catalog.items())), ensure_ascii=False, indent=2)
    (LOCALES / f"{code}.json").write_text(body + "\n")


def placeholders(s: str) -> set[str]:
    return set(PLACEHOLDER.findall(s))


def all_keys() -> dict[str, list[str]]:
    keys, _ = frontend_keys()
    merged: dict[str, list[str]] = defaultdict(list, {k: list(v) for k, v in keys.items()})
    for k, where in python_keys().items():
        merged[k].extend(where)
    return merged


def is_filled(v) -> bool:
    if isinstance(v, str):
        return v != ""
    if isinstance(v, dict):
        return any(isinstance(x, str) and x for x in v.values())
    return False


def problems(code: str, catalog: dict, keys: dict[str, list[str]]) -> dict[str, list[str]]:
    """`missing`(비었거나 없음) · `stale`(원문에서 사라짐) · `broken`(구조·자리표시자 오류)."""
    missing = [k for k in keys if not is_filled(catalog.get(k))]
    stale = [k for k in catalog if k not in keys]
    broken: list[str] = []
    for k, v in catalog.items():
        if not is_filled(v):
            continue
        want = placeholders(k)
        if isinstance(v, str):
            if placeholders(v) != want:
                broken.append(f"{k!r}: 자리표시자 {sorted(placeholders(v))} ≠ {sorted(want)}")
        elif isinstance(v, dict):
            bad = set(v) - PLURAL_CATEGORIES
            if bad:
                broken.append(f"{k!r}: 모르는 복수형 범주 {sorted(bad)}")
            if "other" not in v or not v["other"]:
                broken.append(f"{k!r}: 복수형에 `other` 가 없다")
            for cat, s in v.items():
                if isinstance(s, str) and s and not placeholders(s) <= want:
                    broken.append(f"{k!r}[{cat}]: 원문에 없는 자리표시자 {sorted(placeholders(s) - want)}")
        else:
            broken.append(f"{k!r}: 값은 문자열이거나 복수형 객체여야 한다")
    return {"missing": missing, "stale": stale, "broken": broken}


# ── 명령 ──

def cmd_status(_a) -> int:
    keys = all_keys()
    print(f"원문 {len(keys)}개 (프론트·백엔드 합산)")
    for code in locale_codes():
        p = problems(code, load_catalog(code), keys)
        done = len(keys) - len(p["missing"])
        mark = "필수" if code in REQUIRED else "선택"
        print(f"  {code:<8} {done:>5}/{len(keys)} ({done * 100 // max(len(keys), 1):>3}%)  "
              f"빠짐 {len(p['missing'])}  낡음 {len(p['stale'])}  오류 {len(p['broken'])}  [{mark}]")
    return 0


def cmd_check(a) -> int:
    keys = all_keys()
    bad = False
    for code in locale_codes():
        p = problems(code, load_catalog(code), keys)
        required = code in REQUIRED or code in (a.lang or [])
        if p["broken"]:
            bad = True
            print(f"[{code}] 오류 {len(p['broken'])}개:")
            for line in p["broken"][:20]:
                print("   ", line)
        if required and (p["missing"] or p["stale"]):
            bad = True
            print(f"[{code}] 빠진 번역 {len(p['missing'])}개 · 낡은 키 {len(p['stale'])}개")
            for k in p["missing"][:15]:
                print("    빠짐:", repr(k), "←", keys[k][0])
            for k in p["stale"][:15]:
                print("    낡음:", repr(k))
            print("    → python tools/i18n.py sync 로 빈 칸을 만들고 채운다")
    if not bad:
        print("번역 파일 정상")
    return 1 if bad else 0


def cmd_sync(a) -> int:
    keys = all_keys()
    for code in locale_codes():
        cat = load_catalog(code)
        added = [k for k in keys if k not in cat]
        for k in added:
            cat[k] = ""
        gone = [k for k in cat if k not in keys]
        if a.prune:
            for k in gone:
                del cat[k]
        save_catalog(code, cat)
        print(f"{code}: +{len(added)} 추가" + (f", -{len(gone)} 삭제" if a.prune else f", 낡음 {len(gone)}"))
    return 0


def cmd_add(a) -> int:
    if not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]+)?", a.code):
        print("언어 코드가 이상하다 (예: ja, zh, zh-Hant)")
        return 2
    if (LOCALES / f"{a.code}.json").exists():
        print(f"{a.code}.json 이 이미 있다 — `sync` 를 쓴다")
        return 2
    keys = all_keys()
    save_catalog(a.code, {k: "" for k in keys})
    print(f"{a.code}.json 생성: {len(keys)}개 키(빈 값). `todo {a.code}` 로 번역할 것을 본다.")
    return 0


def cmd_todo(a) -> int:
    keys = all_keys()
    cat = load_catalog(a.code)
    todo = [k for k in sorted(keys) if not is_filled(cat.get(k))]
    chunk = todo[a.offset: a.offset + a.limit] if a.limit else todo[a.offset:]
    json.dump({k: "" for k in chunk}, sys.stdout, ensure_ascii=False, indent=2)
    print(f"\n# {len(chunk)}/{len(todo)} 개", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    c = sub.add_parser("check")
    c.add_argument("--lang", action="append", help="필수로 취급할 언어 (기본: en)")
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("sync")
    s.add_argument("--prune", action="store_true")
    s.set_defaults(fn=cmd_sync)
    ad = sub.add_parser("add")
    ad.add_argument("code")
    ad.set_defaults(fn=cmd_add)
    t = sub.add_parser("todo")
    t.add_argument("code")
    t.add_argument("--limit", type=int, default=0)
    t.add_argument("--offset", type=int, default=0)
    t.set_defaults(fn=cmd_todo)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
