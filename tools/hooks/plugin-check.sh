#!/usr/bin/env bash
# Claude Code PostToolUse(Edit|Write) 훅 — 플러그인 폴더 안의 파일을 고치면 매니페스트를 바로 검사한다
# (feature/agent-cli.md §4-2). 고친 직후 틀린 매니페스트를 알게 하려는 것이지 편집을 막으려는 것이 아니다:
#
#   - 플러그인 폴더(plugin.yaml 이 있는 가장 가까운 상위) 밖의 파일이면 조용히 0
#   - piper-plugin 이 깔려 있지 않으면 조용히 0 — 훅이 개발 환경을 요구하면 안 된다
#   - 검사 실패면 exit 2 + stderr 에 사유. Claude Code 는 2 를 "에이전트에게 되돌려 줄 말" 로 다룬다
#
# stdin 은 훅 JSON 이고 `tool_input.file_path` 만 본다. jq 를 가정하지 않는다 (python3 만).
set -u

payload=$(cat)
file=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print((d.get("tool_input") or {}).get("file_path") or "")
except Exception:
    print("")
' 2>/dev/null)
[ -n "$file" ] || exit 0

dir=$(dirname -- "$file")
while [ -n "$dir" ] && [ "$dir" != "/" ] && [ "$dir" != "." ]; do
  if [ -f "$dir/plugin.yaml" ]; then
    if command -v piper-plugin >/dev/null 2>&1; then
      cmd=(piper-plugin)
    elif python3 -c 'import piper_plugin' >/dev/null 2>&1; then
      cmd=(python3 -m piper_plugin)
    else
      exit 0
    fi
    out=$("${cmd[@]}" check --json "$dir" 2>&1)
    rc=$?
    [ "$rc" -eq 0 ] && exit 0
    {
      printf 'piper-plugin check 실패 — %s\n' "$dir"
      printf '%s' "$out" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    for e in json.loads(raw.strip().splitlines()[-1]).get("errors", []):
        print("  [%s] %s" % (e.get("where"), e.get("msg")))
except Exception:
    print(raw)
'
      printf '고친 뒤 다시 저장하면 다시 검사한다. 계약은 plugins/README.md, 순서는 .claude/skills/piper-plugin.\n'
    } >&2
    exit 2
  fi
  dir=$(dirname -- "$dir")
done
exit 0
