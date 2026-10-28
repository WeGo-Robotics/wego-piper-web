"""`python -m piper_plugin` — 터미널 개발 루프 (feature/plugins.md §7-1, §7-2).

    piper-plugin new  <폴더> [--template loop|mission|detector]   틀을 만든다
    piper-plugin check <폴더> [--api http://127.0.0.1:8000] [--json]   매니페스트·entry·(닿으면) 권한 범위
    piper-plugin run  <폴더> [--config JSON] [--camera key=stream]... [--arm key=iface]... [--api BASE] [--token T] [--dry-run]

`run` 은 게이트웨이가 켠 것과 **같은 진입점**으로 돈다 — 환경변수를 이 명령이 채울 뿐이다.
"터미널에선 됐는데 화면에선 안 된다"가 구조로 줄어든다.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import shutil
import sys
from pathlib import Path

from piper_plugin.env import Env
from piper_plugin.manifest import ManifestError, load_manifest

TEMPLATES = Path(__file__).resolve().parent / "templates"


def cmd_new(a) -> int:
    dst = Path(a.folder).resolve()
    if dst.exists() and any(dst.iterdir()):
        print(f"비어 있지 않다: {dst}", file=sys.stderr)
        return 2
    src = TEMPLATES / a.template
    if not src.is_dir():
        print(f"모르는 템플릿: {a.template} ({', '.join(p.name for p in TEMPLATES.iterdir())})", file=sys.stderr)
        return 2
    shutil.copytree(src, dst, dirs_exist_ok=True)
    for p in dst.rglob("*"):
        if p.is_file():
            p.write_text(p.read_text(encoding="utf-8").replace("__NAME__", dst.name), encoding="utf-8")
    print(f"만들었다: {dst}  (다음: piper-plugin check {dst} → run)")
    return 0


def check_result(folder: Path, api_base: str | None = None) -> dict:
    """`check` 의 판정을 구조로 — 사람용 출력과 `--json` 이 같은 판정을 쓴다 (feature/agent-cli.md §4-2).

    `ok` 가 전부다. `errors[].where` 는 manifest(entry 파일 부재 포함 — `load_manifest` 가 본다) |
    permissions. `scopes_checked` 가 False 면 권한 범위 이름은 게이트웨이가 시작할 때 검사한다
    (`--api` 를 주면 지금 본다).
    """
    out: dict = {"ok": True, "folder": str(folder), "errors": [], "scopes_checked": False}
    try:
        m = load_manifest(folder)
    except ManifestError as e:
        out["ok"] = False
        out["errors"].append({"where": "manifest", "msg": str(e)})
        return out
    out.update(name=m.name, version=m.version, title=m.title, entry=m.entry, python=m.python,
               cameras=[c.key for c in m.cameras], arms=[a.key for a in getattr(m, "arms", [])],
               permissions=list(m.permissions), mission=m.mission,
               gpu=m.resources.gpu, heartbeat=m.resources.heartbeat)
    if api_base:
        from piper_plugin.api import Api

        r = Api(api_base).get("/api/plugins/scopes")
        if r.ok:
            known = set((r.json() or {}).get("scopes", {})) | {"*"}
            bad = [s for s in m.permissions if s not in known]
            out["scopes_checked"] = True
            if bad:
                out["ok"] = False
                out["errors"].append({"where": "permissions", "msg": f"게이트웨이가 모르는 권한 범위: {bad}"})
        else:
            out["note"] = f"게이트웨이 {api_base} 에 닿지 못해 권한 범위는 못 봤다: {r.status}"
    return out


def cmd_check(a) -> int:
    out = check_result(Path(a.folder).resolve(), a.api)
    if a.json:
        # 기계가 읽는 결과 — 에이전트·훅이 ✓/✗ 글자를 파싱하지 않게
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["ok"] else 1
    broken = [e for e in out["errors"] if e["where"] == "manifest"]
    if broken:
        print(f"✗ {broken[0]['msg']}", file=sys.stderr)
        return 1
    print(f"✓ {out['name']} v{out['version']} — {out['title']}")
    print(f"  entry: {out['entry']}  python: {out['python']}  cameras: {out['cameras']}  arms: {out['arms']}")
    print(f"  permissions: {out['permissions'] or '(없음)'}  gpu: {out['gpu']}  heartbeat: {out['heartbeat']}")
    for e in out["errors"]:
        print(f"✗ {e['msg']}", file=sys.stderr)
    if out["scopes_checked"]:
        if not any(e["where"] == "permissions" for e in out["errors"]):
            print("  권한 범위: 게이트웨이와 맞다")
    elif a.api:
        print(f"  ({out.get('note', '')})")
    else:
        print("  권한 범위 이름은 게이트웨이가 검사한다 (--api 를 주면 지금 본다)")
    return 0 if out["ok"] else 1


def cmd_run(a) -> int:
    # 웹 채팅의 개발 모드가 띄운 셸이다 — 에이전트가 쓴 코드가 게이트웨이(REST)에 붙는 길은 막는다.
    # 드라이런(카메라·버스·모델만)은 된다. 실물은 사람이 플러그인 페이지에서 켠다 (feature/agent-cli.md §4-6).
    if os.environ.get("PIPER_AGENT_CHAT") == "1" and (a.api or a.token):
        print("✗ 웹 채팅의 개발 모드에서는 --api/--token 없이 드라이런만 돌릴 수 있다 — 설치·실물 실행은 사람이 플러그인 페이지에서 한다",
              file=sys.stderr)
        return 2
    folder = Path(a.folder).resolve()
    try:
        m = load_manifest(folder)
    except ManifestError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    config = dict(m.defaults())
    if a.config:
        config.update(json.loads(a.config))
    if a.dry_run:
        config["dry_run"] = True
    cameras = {}
    for spec in a.camera or []:
        k, _, v = spec.partition("=")
        cameras[k] = v or k
    arms = {}
    for spec in a.arm or []:
        k, _, v = spec.partition("=")
        arms[k] = v or k
    env = Env(name=m.name, dir=folder, runtime_dir=Path(a.runtime or (folder / ".runtime")),
              config=config, cameras=cameras, arms=arms, api_base=a.api or "", token=a.token or "",
              heartbeat=m.resources.heartbeat and bool(a.api), redis_url=os.environ.get("PIPER_REDIS_URL"))
    os.environ.update(env.to_environ())
    if not a.api:
        print("(게이트웨이 없이 돈다 — self.api 호출은 ApiUnavailable, 버스는 PIPER_REDIS_URL 그대로)")
    entry = folder / m.entry
    sys.argv = [str(entry)]
    sys.path.insert(0, str(folder))
    runpy.run_path(str(entry), run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="piper-plugin", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="틀을 만든다")
    n.add_argument("folder")
    n.add_argument("--template", default="loop", help="loop | mission | detector")
    n.set_defaults(fn=cmd_new)
    c = sub.add_parser("check", help="매니페스트를 검사한다")
    c.add_argument("folder")
    c.add_argument("--api", help="게이트웨이 주소 — 주면 권한 범위 이름까지 본다")
    c.add_argument("--json", action="store_true", help="결과를 JSON 한 줄로 (에이전트·훅이 읽는다)")
    c.set_defaults(fn=cmd_check)
    r = sub.add_parser("run", help="터미널에서 돌린다")
    r.add_argument("folder")
    r.add_argument("--config", help='설정 JSON (기본값 위에 덮는다) 예: \'{"conf":0.4}\'')
    r.add_argument("--camera", action="append", metavar="KEY=STREAM", help="카메라 슬롯 (반복 가능)")
    r.add_argument("--arm", action="append", metavar="KEY=IFACE", help="팔 슬롯 — 상태를 읽을 CAN 인터페이스 (반복 가능)")
    r.add_argument("--api", help="게이트웨이 주소. 없으면 REST 호출 불가")
    r.add_argument("--token", help="게이트웨이 토큰 (로그인이 꺼져 있으면 없어도 된다)")
    r.add_argument("--runtime", help="런타임 디렉토리 (기본 <폴더>/.runtime)")
    r.add_argument("--dry-run", action="store_true", help="MissionPlugin 의 dry_run 을 켠다")
    r.set_defaults(fn=cmd_run)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
