"""`python -m piper_plugin` — 터미널 개발 루프 (feature/plugins.md §7-1, §7-2).

    piper-plugin new  <폴더> [--template loop|mission|detector]   틀을 만든다
    piper-plugin check <폴더> [--api http://127.0.0.1:8000]       매니페스트·entry·(닿으면) 권한 범위
    piper-plugin run  <폴더> [--config JSON] [--camera key=stream]... [--api BASE] [--token T] [--dry-run]

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


def cmd_check(a) -> int:
    folder = Path(a.folder).resolve()
    try:
        m = load_manifest(folder)
    except ManifestError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"✓ {m.name} v{m.version} — {m.title}")
    print(f"  entry: {m.entry}  python: {m.python}  cameras: {[c.key for c in m.cameras]}")
    print(f"  permissions: {m.permissions or '(없음)'}  gpu: {m.resources.gpu}  heartbeat: {m.resources.heartbeat}")
    if a.api:
        from piper_plugin.api import Api

        r = Api(a.api).get("/api/plugins/scopes")
        if r.ok:
            known = set((r.json() or {}).get("scopes", {})) | {"*"}
            bad = [s for s in m.permissions if s not in known]
            if bad:
                print(f"✗ 게이트웨이가 모르는 권한 범위: {bad}", file=sys.stderr)
                return 1
            print("  권한 범위: 게이트웨이와 맞다")
        else:
            print(f"  (게이트웨이 {a.api} 에 닿지 못해 권한 범위는 못 봤다: {r.status})")
    else:
        print("  권한 범위 이름은 게이트웨이가 검사한다 (--api 를 주면 지금 본다)")
    return 0


def cmd_run(a) -> int:
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
    env = Env(name=m.name, dir=folder, runtime_dir=Path(a.runtime or (folder / ".runtime")),
              config=config, cameras=cameras, api_base=a.api or "", token=a.token or "",
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
    c.set_defaults(fn=cmd_check)
    r = sub.add_parser("run", help="터미널에서 돌린다")
    r.add_argument("folder")
    r.add_argument("--config", help='설정 JSON (기본값 위에 덮는다) 예: \'{"conf":0.4}\'')
    r.add_argument("--camera", action="append", metavar="KEY=STREAM", help="카메라 슬롯 (반복 가능)")
    r.add_argument("--api", help="게이트웨이 주소. 없으면 REST 호출 불가")
    r.add_argument("--token", help="게이트웨이 토큰 (로그인이 꺼져 있으면 없어도 된다)")
    r.add_argument("--runtime", help="런타임 디렉토리 (기본 <폴더>/.runtime)")
    r.add_argument("--dry-run", action="store_true", help="MissionPlugin 의 dry_run 을 켠다")
    r.set_defaults(fn=cmd_run)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
