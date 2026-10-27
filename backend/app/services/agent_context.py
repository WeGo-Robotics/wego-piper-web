"""에이전트용 컨텍스트 꾸러미 — 상태 API 가 이미 아는 것을 한 장으로 (feature/agent-cli.md §4-1).

"추론이 왜 죽었나" 를 묻기 위해 사람이 버전·유닛·저널·미션 저널을 네 화면에서 모아
붙여넣던 일을 없앤다. 여기는 **모으기만** 한다 — 판정도 새 측정도 없다. 재료는 전부
다른 서비스가 이미 내놓는 것이고(버전 카드·서비스 패널·활동·E-stop·장치 감시·저널),
하나가 실패해도 나머지는 그린다(`errors` 에 사유 — `version.collect` 와 같은 태도).

## 비밀은 들어가지 않는다 — 구조와 검사 둘로

- 설정의 토큰·키 필드를 **읽어서 싣는 코드가 없다.** 상태 API 결과만 모은다.
- 그래도 저널 줄에 토큰이 찍혀 있을 수 있다. `redact()` 가 마지막에 설정값과 흔한
  토큰 모양(`Bearer …`·`hf_…`·`sk-ant-…`)을 `***` 로 바꾼다. 붙여넣기는 곧 유출이다.

## 모양

`collect()` 가 구조(dict)를 만들고 `render_markdown()` 이 글로 만든다. 절 순서는 위가
"지금", 아래가 "왜" — 머리·유닛·활동·E-stop·장치·저널·미션 저널·읽을 곳. 브라우저
알림함(`inbox`)은 프론트 상태라 서버가 모른다. 호출자가 넣어 주면 절 0 으로 앉는다.

⚠ 이 파일의 한국어는 화면 문구가 아니다 — 에이전트가 읽는 markdown 이라 번역하지
않는다. `tools/i18n.py` 의 `PY_SKIP_FILES` 가 이 파일을 뺀다.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_SINCE = "30 minutes ago"
DEFAULT_LINES = 200
MAX_LINES = 1000          # 유닛당 저널 상한 — 붙여넣기 한도 안에 두기 위해
MISSION_TAIL = 20
INBOX_MAX = 50
EVENT_CHARS = 300

#: 늘 가리키는 문서 + 도는 활동별로 더 가리키는 문서 (저장소 루트 기준 경로)
DOCS_ALWAYS = ("CLAUDE.md", "feature/services.md", "feature/agent-cli.md")
DOCS_BY_ACTIVITY = {
    "inference": ("feature/camera-resolution.md", "feature/policy-ui-spec.md"),
    "recording": ("feature/stream-based-cameras.md", "feature/camera-profiles.md"),
    "training": ("feature/cloud-training.md",),
    "policy_server": ("feature/cloud-training.md",),
    "teleop": ("feature/teleoperation.md",),
    "detector_train": ("feature/detector-training.md",),
    "plugin": ("feature/plugins.md", "plugins/README.md"),
}


# ── 수집기 — 하나가 실패해도 나머지는 그린다 ─────────────────────────────────


def _head() -> dict:
    from app.services import version

    info = version.collect()
    gw = info.get("gateway") or {}
    return {
        "version": gw.get("version"), "source": gw.get("source"), "built_at": gw.get("built_at"),
        "mode": version.update_mode(gw),          # image(배포 기계) | source(소스로 도는 기계)
        "host": socket.gethostname(),
        "staleness": info.get("staleness"),
        "daemons_reporting": sorted(info.get("daemons") or {}),
    }


def _units() -> dict:
    from app.services import units

    return {"units": [u.to_dict() for u in units.list_units()], "gateway": units.gateway_status()}


def _activity() -> dict:
    from app.services import exclusivity, inference_state
    from app.services.plugins import registry

    snap = exclusivity.snapshot()
    pol = inference_state.get()
    live = registry.live_mission()
    mission = None
    if live:
        st = registry.status(live)
        mission = {"plugin": live, "run_id": st.get("run_id"), "started_at": st.get("started_at"),
                   "state": st.get("state"), "kv": st.get("kv")}
    return {
        "running": list(snap.get("running", [])),
        "labels": dict(snap.get("labels", {})),
        "policy": {"type": pol.policy_type, "checkpoint": pol.checkpoint_path} if pol else None,
        "plugins": sorted(registry.running()),
        "mission": mission,
    }


def _estop() -> dict:
    from app.services.estop_bridge import estop_bridge

    return estop_bridge.status()


def _devices() -> dict:
    from app.services.camera_manager import camera_manager
    from app.services.device_watch import device_watch
    from app.services.robot_manager import robot_manager

    arms = [{"iface": a.iface, "role": a.role, "slot": a.slot, "side": a.side, "state": a.state,
             "connected": bool(a.connected), "transport": a.transport}
            for a in robot_manager.arms.values() if getattr(a, "ready", False)]
    cams = [{"id": c.id, "name": c.name, "stream": c.stream, "type": c.cam_type,
             "present": bool(getattr(c, "present", True)), "connected": bool(c.connected)}
            for c in camera_manager.cameras.values() if getattr(c, "ready", False)]
    return {"summary": device_watch.summary(), "alerts": device_watch.alerts(), "arms": arms, "cameras": cams}


def _journal(unit: str, lines: int, since: str) -> dict:
    from app.services import syslog

    out = syslog.fetch(unit, lines, "warning", since)
    return {k: out.get(k) for k in ("entries", "scanned", "truncated", "partial", "source", "via")}


def _tail_jsonl(path: Path, n: int) -> list[dict]:
    events = []
    for line in path.read_text().splitlines()[-n:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:   # 강제 종료로 잘린 마지막 줄
            pass
    return events


def _mission_tail() -> dict | None:
    from app.services.plugins import registry

    live = registry.live_mission()
    if live:
        run_id = registry.status(live).get("run_id")
        path = settings.plugins_dir / ".runtime" / live / "runs" / f"{run_id}.jsonl" if run_id else None
        events = _tail_jsonl(path, MISSION_TAIL) if path and path.is_file() else []
        return {"live": True, "plugin": live, "run_id": run_id, "events": events}
    runs = registry.runs()
    if not runs:
        return None
    latest = runs[0]
    found = registry.run_events(latest["id"], latest["plugin"])
    return {"live": False, "plugin": latest["plugin"], "run_id": latest["id"], "mtime": latest.get("mtime"),
            "events": (found[1] if found else [])[-MISSION_TAIL:]}


def _pointers(running: list[str], since: str, unit: str) -> dict:
    docs = list(DOCS_ALWAYS)
    for a in running:
        for d in DOCS_BY_ACTIVITY.get(a, ()):
            if d not in docs:
                docs.append(d)
    return {
        "docs": docs,
        "more_logs": f"/api/system/logs?unit={quote(unit)}&level=info&lines=1000&since={quote(since)}&format=text",
        "services": "/api/system/services", "version": "/api/system/version",
        "activity": "/api/activity", "swagger": "/docs",
    }


def _clean_inbox(items) -> list[dict]:
    out = []
    for m in list(items or [])[:INBOX_MAX]:
        if not isinstance(m, dict):
            continue
        at = m.get("at")
        out.append({"at": float(at) if isinstance(at, (int, float)) else None,
                    "level": str(m.get("level") or "info")[:8],
                    "text": str(m.get("text") or "")[:500],
                    "source": str(m.get("source") or "")[:80]})
    return out


def collect(*, since: str | None = None, unit: str = "all", lines: int = DEFAULT_LINES,
            inbox: list[dict] | None = None) -> dict:
    """구조. 절마다 따로 모아 실패는 `errors[절]` 에 적는다 — 저널을 못 읽어도 버전·활동은 나간다."""
    since = (since or "").strip() or DEFAULT_SINCE
    lines = max(1, min(int(lines or DEFAULT_LINES), MAX_LINES))
    data: dict = {"generated_at": time.time(), "since": since, "unit": unit, "lines": lines,
                  "sections": {}, "errors": {}}
    steps = (("head", _head), ("units", _units), ("activity", _activity), ("estop", _estop),
             ("devices", _devices), ("journal", lambda: _journal(unit, lines, since)), ("mission", _mission_tail))
    for key, fn in steps:
        try:
            data["sections"][key] = fn()
        except Exception as exc:
            logger.debug("컨텍스트 절 %s 수집 실패: %s", key, exc)
            data["errors"][key] = str(exc)
    running = (data["sections"].get("activity") or {}).get("running", [])
    data["sections"]["pointers"] = _pointers(running, since, unit)
    if inbox:
        data["sections"]["inbox"] = _clean_inbox(inbox)
    return data


# ── 비밀 지우기 ────────────────────────────────────────────────────────────────

_TOKEN_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{10,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
)


def _secret_values() -> list[str]:
    """설정에 든 비밀 **값** — 짧은 것(빈 값·자리표시자)은 지울 대상이 아니다."""
    out = []
    for name in ("api_token", "anthropic_api_key", "llm_api_key"):
        v = getattr(settings, name, "") or ""
        if len(v) >= 8:
            out.append(v)
    return sorted(out, key=len, reverse=True)


def redact(text: str) -> str:
    for v in _secret_values():
        text = text.replace(v, "***")
    for pat in _TOKEN_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", text)
    return text


def redact_data(data: dict) -> dict:
    return json.loads(redact(json.dumps(data, ensure_ascii=False, default=str)))


# ── markdown ───────────────────────────────────────────────────────────────────


def _ts(t) -> str:
    try:
        return datetime.fromtimestamp(float(t)).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _ago(seconds) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 90:
        return f"{s}초 전"
    if s < 5400:
        return f"{s // 60}분 전"
    if s < 172800:
        return f"{s // 3600}시간 전"
    return f"{s // 86400}일 전"


def _j(obj, limit: int = EVENT_CHARS) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def render_markdown(data: dict) -> str:
    s, errs = data["sections"], data["errors"]
    out: list[str] = [
        "# PIPER 에이전트용 컨텍스트", "",
        f"생성 {datetime.fromtimestamp(data['generated_at']).strftime('%Y-%m-%d %H:%M:%S')} · "
        f"저널 기준 `{data['since']}` 이후 · 유닛 `{data['unit']}` · 상한 {data['lines']}줄/유닛", "",
        "> 게이트웨이 상태 API 를 모은 것이다. 토큰·키는 지웠다. 위가 \"지금\", 아래가 \"왜\" — 더 볼 곳은 마지막 절.", "",
    ]
    if errs:
        out += ["⚠ 못 모은 절: " + ", ".join(f"{k}({v})" for k, v in errs.items()), ""]

    def section(num: int, title: str, key: str | None) -> bool:
        out.extend([f"## {num}. {title}", ""])
        if key and key not in s:          # 실패했거나(errors) 아예 안 모았다 — 둘 다 본문 대신 사유
            out.extend([f"(수집 실패: {errs.get(key, '수집되지 않았다')})", ""])
            return False
        return True

    if "inbox" in s:
        section(0, "브라우저 알림함", None)
        out.append("최신 먼저. 토스트가 사라진 뒤에도 사람이 지우기 전까지 남아 있던 것들.")
        for m in s["inbox"]:
            src = f" [{m['source']}]" if m.get("source") else ""
            when = _ts(m["at"]) if m.get("at") else "--:--:--"
            out.append(f"- {when} {m['level'].upper()}{src} {m['text']}")
        if not s["inbox"]:
            out.append("(비어 있음)")
        out.append("")

    if section(1, "머리", "head"):
        h = s["head"]
        built = f" · 빌드 {h['built_at']}" if h.get("built_at") else ""
        out.append(f"- 버전 **{h.get('version') or 'unknown'}** ({h.get('source') or '?'}) · 배포형 {h.get('mode')}"
                   f" · 호스트 {h.get('host')}{built}")
        st = h.get("staleness") or {}
        if st.get("ok", True):
            out.append("- 릴리스 대조: 정상 (wheel·데몬 소스가 적용된 버전과 맞다)")
        else:
            out.append(f"- 릴리스 대조: **어긋남** — wheel {st.get('wheels')} · 데몬 소스 {st.get('daemons')}")
        if h.get("daemons_reporting"):
            out.append(f"- 버전을 알린 데몬: {', '.join(h['daemons_reporting'])}")
        out.append("")

    if section(2, "유닛", "units"):
        u = s["units"]
        out += ["| 유닛 | 상태 | 기동 | 코드 | 설명 |", "|---|---|---|---|---|"]
        for x in u.get("units", []):
            state = "도는 중" if x.get("active") else ("안 깔림" if not x.get("installed", True) else "멈춤")
            code = "**낡음 — 더 새 코드가 있다**" if x.get("stale") else "최신"
            age = _ago(x.get("age_s")) if x.get("active") else "-"
            out.append(f"| {x.get('name')} | {state} | {age} | {code} | {x.get('description', '')} |")
        g = u.get("gateway") or {}
        gw_code = "**낡음 — 재시작하면 새 코드**" if g.get("stale") else "코드 최신"
        out += ["", f"- 게이트웨이(이 프로세스): pid {g.get('pid')} · 기동 {_ago(g.get('age_s'))} · {gw_code}", ""]

    if section(3, "활동", "activity"):
        a = s["activity"]
        labels = a.get("labels", {})
        running = a.get("running", [])
        out.append("- 도는 활동: " + (", ".join(f"{labels.get(r, r)}(`{r}`)" for r in running) if running else "없음"))
        if a.get("policy"):
            out.append(f"- 추론 정책: `{a['policy']['type']}` · 체크포인트 `{a['policy']['checkpoint']}`")
        if a.get("plugins"):
            out.append(f"- 도는 플러그인: {', '.join(a['plugins'])}")
        m = a.get("mission")
        if m:
            out.append(f"- 라이브 미션: **{m['plugin']}** run `{m.get('run_id')}` · 상태 {m.get('state')} · KV {_j(m.get('kv'))}")
        else:
            out.append("- 라이브 미션: 없음")
        out.append("")

    if section(4, "E-stop", "estop"):
        e = s["estop"]
        bus = "연결" if e.get("bus_available") else "**끊김**"
        out += [f"- 버스 {bus} · armed {e.get('armed')} · 타임아웃 {e.get('timeout_ms')}ms"
                f" · heartbeat 주기 {e.get('heartbeat_interval_ms')}ms",
                f"- 마지막 발동: {_j(e.get('last_trigger')) if e.get('last_trigger') else '없음'}", ""]

    if section(5, "장치", "devices"):
        d = s["devices"]
        sm = d.get("summary") or {}
        r, c = sm.get("robots", {}), sm.get("cameras", {})
        out.append(f"- 등록된 로봇 {r.get('ok', 0)}/{r.get('ok', 0) + r.get('warn', 0)} 있음 · "
                   f"카메라 {c.get('ok', 0)}/{c.get('ok', 0) + c.get('warn', 0)} 있음 · 경보 {sm.get('alerts', 0)}")
        for a in d.get("arms", []):
            tags = " ".join(f"{k}={v}" for k, v in (("role", a.get("role")), ("slot", a.get("slot")), ("side", a.get("side"))) if v)
            seen = "robotd 가 안다" if a.get("connected") else "**없음**"
            out.append(f"- 팔 `{a['iface']}` ({a.get('transport')}) {tags} · {seen} · {a.get('state')}")
        for cam in d.get("cameras", []):
            where = "**사라짐**" if not cam.get("present") else ("열림" if cam.get("connected") else "꽂힘(안 열림 — 정상)")
            out.append(f"- 카메라 `{cam.get('stream') or cam['id']}` {cam.get('name')} ({cam.get('type')}) · {where}")
        for al in d.get("alerts", []):
            out.append(f"- ⚠ 경보 [{al.get('kind')}/{al.get('reason')}] {al.get('text')}")
        out.append("")

    if section(6, "저널", "journal"):
        j = s["journal"]
        entries = j.get("entries") or []
        meta = f"warning 이상 · {j.get('scanned')}줄 훑음 · 경로 {j.get('via')}"
        if j.get("truncated"):
            meta += " · **더 있음** (lines 를 늘리거나 since 를 좁혀라)"
        if j.get("partial"):
            meta += " · 시간 예산 안에 훑은 만큼(최신부터)"
        out += [meta, ""]
        if not entries:
            out += ["(해당 기간에 경고 이상이 없다)", ""]
        by_unit: dict[str, list[dict]] = {}
        for e in entries:
            by_unit.setdefault(e.get("unit") or "?", []).append(e)
        for unit, rows in by_unit.items():
            out += [f"### {unit}", "```"]
            for e in rows:
                lvl = {"error": "ERR ", "warning": "WARN", "debug": "DBG "}.get(e.get("level"), "INF ")
                cnt = f" ×{e['count']}" if (e.get("count") or 1) > 1 else ""
                out.append(f"{_ts(e.get('t'))} {lvl} {e.get('msg', '')}{cnt}")
            out += ["```", ""]

    if section(7, "미션 저널", "mission"):
        m = s.get("mission")
        if not m:
            out += ["(미션 저널이 없다 — 미션 플러그인을 돌린 적이 없다)", ""]
        else:
            kind = "라이브" if m.get("live") else "마지막"
            out += [f"{kind} 미션 **{m['plugin']}** run `{m.get('run_id')}` — 끝 {len(m.get('events') or [])}줄", ""]
            for ev in m.get("events") or []:
                out.append(f"- {_j(ev)}")
            out.append("")

    section(8, "읽을 곳", None)
    p = s["pointers"]
    out.append("문서(저장소 루트 기준):")
    out += [f"- {d}" for d in p["docs"]]
    out += ["", "더 볼 때(같은 게이트웨이):",
            f"- 저널 전부(info 포함): `GET {p['more_logs']}`",
            f"- 유닛·낡은 코드: `GET {p['services']}` · 버전: `GET {p['version']}` · 활동: `GET {p['activity']}`",
            f"- 전체 API: `{p['swagger']}` (Swagger — 표면의 정본)", ""]
    return redact("\n".join(out))
