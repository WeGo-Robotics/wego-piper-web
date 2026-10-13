"""다국어 — 번역 도구와 번역 파일의 계약 (feature/i18n.md).

두 가지를 지킨다:

1. **번역 로직**(`frontend/src/i18n/core.ts`)이 약속대로 도는가 — 자리표시자 치환, 복수형,
   백엔드 문장을 키의 모양에 맞추는 일. 브라우저 없이 Node 로 직접 부른다.
2. **번역 파일**이 원문과 맞는가 — 영어는 항상 완결이어야 하고(빠진 문장 = 영어 화면에
   한국어가 섞임), 어느 언어든 자리표시자가 원문과 어긋나면 안 된다(런타임에 `{name}` 이
   그대로 화면에 뜬다).

원문을 고치거나 문장을 더하면 이 테스트가 깨진다. 고치는 법은 메시지에 있다:
`python tools/i18n.py sync` 로 빈 칸을 만들고 채운다.
"""

import ast
import importlib.util
import json
import shutil
import subprocess
import textwrap
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "frontend" / "src" / "i18n" / "core.ts"


def _load_tool():
    spec = importlib.util.spec_from_file_location("piper_i18n_tool", ROOT / "tools" / "i18n.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool = _load_tool()

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node 가 없다")


def _run_core(js: str):
    """`core.ts` 를 Node 로 읽어 `js` 를 돌리고 그 결과(JSON)를 돌려준다. Node 22.6+ 의 타입 제거를 쓴다."""
    prog = textwrap.dedent(
        f"""
        import * as c from {json.dumps(CORE.as_uri())}
        const out = (() => {{ {js} }})()
        process.stdout.write(JSON.stringify(out))
        """
    )
    res = subprocess.run(
        ["node", "--input-type=module", "-e", prog],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        pytest.skip(f"node 가 core.ts 를 못 읽는다(타입 제거 미지원?): {res.stderr[:200]}")
    return json.loads(res.stdout)


# ── 1. 번역 로직 ──

@needs_node
def test_interpolate_replaces_named_placeholders():
    out = _run_core(
        """
        return [
          c.interpolate('{n}개 선택', { n: 3 }),
          c.interpolate('{a} / {b}', { a: 'x' }),          // 빠진 인자는 그대로 둔다
          c.interpolate('{{literal}} {n}', { n: 1 }),
          c.interpolate('예: {"a": 1} {n}', { n: 2 }),       // 식별자 모양이 아니면 건드리지 않는다
        ]
        """
    )
    assert out == ["3개 선택", "x / {b}", "{literal} 1", '예: {"a": 1} 2']


@needs_node
def test_plural_picks_by_language_rules():
    out = _run_core(
        """
        const e = { one: '{n} episode', other: '{n} episodes' }
        return [
          c.pick(e, 1, 'en'), c.pick(e, 2, 'en'), c.pick(e, 0, 'en'),
          c.pick({ other: '{n} 件' }, 1, 'ja'),               // 일본어는 other 하나
          c.pick({ one: 'a' }, 5, 'en') ?? null,              // other 가 없으면 못 고른다
          c.pick('plain', 3, 'en'),
        ]
        """
    )
    assert out == ["{n} episode", "{n} episodes", "{n} episodes", "{n} 件", None, "plain"]


@needs_node
def test_empty_translation_counts_as_missing():
    out = _run_core(
        """
        return [c.isTranslated(''), c.isTranslated('x'), c.isTranslated({ other: '' }),
                c.isTranslated({ one: 'a', other: 'b' }), c.isTranslated(undefined)]
        """
    )
    assert out == [False, True, False, True, False]


@needs_node
def test_backend_sentence_matches_the_key_shape():
    """백엔드는 한국어 문장을 그대로 만든다 — 번역 파일의 `{이름}` 키가 패턴이다."""
    out = _run_core(
        """
        const cat = {
          '{cam_id} 가 연결돼 있지 않습니다': '{cam_id} is not connected',
          '{what} 실패': '{what} failed',
          '카메라 {id} 실패': 'Camera {id} failed',
          '값 ({n}) [확인]': 'Value ({n}) [check]',
          '비어 있음 {x}': '',                                // 빈 번역은 패턴이 아니다
        }
        const m = c.buildMatchers(cat, 'en')
        const hit = (t) => { const r = c.matchSentence(t, m); return r ? [r.entry, r.params] : null }
        return {
          shape: hit('cam3 가 연결돼 있지 않습니다'),
          specific_wins: hit('카메라 3 실패'),               // 더 구체적인(글자가 많은) 키가 이긴다
          generic: hit('저장 실패'),
          escaped: hit('값 (7) [확인]'),
          none: hit('전혀 다른 문장'),
          empty: hit('비어 있음 1'),
          multiline: hit('a\\nb 실패'),
        }
        """
    )
    assert out["shape"] == ["{cam_id} is not connected", {"cam_id": "cam3"}]
    assert out["specific_wins"] == ["Camera {id} failed", {"id": "3"}]
    assert out["generic"] == ["{what} failed", {"what": "저장"}]
    assert out["escaped"] == ["Value ({n}) [check]", {"n": "7"}]
    assert out["none"] is None
    assert out["empty"] is None
    assert out["multiline"] == ["{what} failed", {"what": "a\nb"}]


# ── 2. 파이썬 문장 추출 ──

def _keys_of(src: str) -> list[str]:
    found: dict[str, list[str]] = defaultdict(list)
    tool._scan(ast.parse(textwrap.dedent(src)), Path("x.py"), found)
    return sorted(found)


def test_python_extraction_turns_fstrings_into_templates():
    keys = _keys_of(
        """
        def f(cam_id, n, e, cam):
            raise HTTPException(404, f"{cam_id} 가 연결돼 있지 않습니다")
            return {"error": f"응답을 읽지 못했습니다: {e}"}
            return {"ok": False, "error": "프레임을 받지 못했습니다"}
            x = "이미 " + cam.name + " 를 조종 중입니다"
            y = "%d 개 중 %s" % (n, e)
            z = "{} 를 열 수 없습니다".format(cam)
        """
    )
    assert "{cam_id} 가 연결돼 있지 않습니다" in keys
    assert "응답을 읽지 못했습니다: {e}" in keys
    assert "프레임을 받지 못했습니다" in keys
    assert "이미 {name} 를 조종 중입니다" in keys
    assert "{n} 개 중 {e}" in keys
    assert "{cam} 를 열 수 없습니다" in keys


def test_python_extraction_skips_logs_docstrings_and_cli_help():
    keys = _keys_of(
        '''
        """모듈 설명입니다."""
        import argparse, logging
        log = logging.getLogger(__name__)

        def f(x):
            """함수 설명입니다."""
            log.warning("로그 문장입니다 %s", x)
            print("출력 문장입니다")
            p = argparse.ArgumentParser(description="명령줄 설명")
            p.add_argument("--a", help="인자 설명")
            raise SystemExit("종료 메시지")
            return "화면 문장입니다"
        '''
    )
    assert keys == ["화면 문장입니다"]


# ── 3. 번역 파일 ──

def _keys():
    return tool.all_keys()


def test_every_locale_file_is_well_formed():
    """어느 언어든 — 자리표시자가 원문과 어긋나면 런타임에 `{name}` 이 화면에 뜬다."""
    keys = _keys()
    for code in tool.locale_codes():
        p = tool.problems(code, tool.load_catalog(code), keys)
        assert not p["broken"], f"{code}.json: " + "; ".join(p["broken"][:5])


def test_required_languages_are_complete():
    """영어는 **항상 완결**이다 — 빠진 문장이 영어 화면에 한국어로 섞이지 않게."""
    keys = _keys()
    for code in tool.REQUIRED:
        cat = tool.load_catalog(code)
        p = tool.problems(code, cat, keys)
        missing = [f"{k!r} ← {keys[k][0]}" for k in p["missing"][:10]]
        assert not p["missing"], (
            f"{code}.json 에 번역이 없는 문장 {len(p['missing'])}개 — "
            f"`python tools/i18n.py sync` 후 채운다:\n  " + "\n  ".join(missing)
        )
        assert not p["stale"], (
            f"{code}.json 에 원문에서 사라진 키 {len(p['stale'])}개 — "
            f"`python tools/i18n.py sync --prune`:\n  " + "\n  ".join(repr(k) for k in p["stale"][:10])
        )


def test_the_extractor_finds_both_sides():
    """추출이 조용히 0개를 돌려주면 위 테스트가 전부 공허하게 통과한다."""
    keys = _keys()
    where = [w for ws in keys.values() for w in ws]
    assert any(w.startswith("frontend/src/") for w in where), "프론트 문장을 못 찾았다"
    assert any(w.startswith(("backend/", "robot/", "cam/", "daemons/")) for w in where), "백엔드 문장을 못 찾았다"
    assert len(keys) > 500
