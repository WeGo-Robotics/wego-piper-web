#!/usr/bin/env python3
"""piper-unitd — `piper-*` 유닛을 켜고 끄는 데몬 (feature/services.md).

## 왜 따로인가

배포 대상의 게이트웨이는 **컨테이너**라 `systemctl` 이 없다. 데몬에게 `restart`
RPC 를 보내는 게 지금까지의 전부였는데, 그건 "끄기"가 못 된다 — 스스로 죽어도
`Restart=always` 가 되살린다. "부팅 시 시작"(enable/disable)은 아예 길이 없다.

호스트 systemd 소켓을 컨테이너에 마운트하는 길도 있지만, 이 compose 는 특권과
`/dev` 를 **일부러** 뺐다 (docker-compose.yml). 그 결정을 되돌리지 않고, 호스트에
아주 작은 데몬 하나를 두어 버스로 부탁을 받는다. 소스로 도는 기계도 같은 길을
쓴다 — 통로가 하나여야 어느 환경에서 되고 어느 환경에서 안 되는 일이 없다.

## 무엇을 하는가 — 그리고 안 하는가

- 허용 목록은 `piper_bus.contract.UNIT_CATALOG` 뿐이다. 남의 유닛은 이름을
  알아도 못 만진다.
- **estopd 는 읽기 전용**(`UNIT_READONLY`). 안전장치에 원격 종료 경로를 다는 것은
  별개의 결정이다.
- **자기 자신은 끄지 않는다**(`UNIT_SELF`) — 끄면 이 화면의 켜기/끄기가 같이 죽는다.
- 활동(녹화·추론) 중에 robotd 를 끄면 에피소드가 깨진다 — 그 판단은 **게이트웨이**가
  한다(exclusivity). 여기는 활동을 모른다.

환경변수: `PIPER_REDIS_URL`
"""

import json
import logging
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from piper_bus import contract as C
from piper_bus.client import Bus, self_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] unitd: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("unitd")

REPO = Path(__file__).resolve().parents[1]

_running = True

ACTIONS = ("start", "stop", "restart", "enable", "disable")


def unit_name(name: str) -> str:
    """`simd` / `piper-simd` / `piper-simd.service` → `piper-simd.service`. 카탈로그에
    없으면 ValueError — 남의 유닛은 이름을 알아도 못 만진다."""
    short = name.removesuffix(".service").removeprefix("piper-")
    if short not in C.UNIT_CATALOG or "/" in short or ".." in short:
        raise ValueError(f"우리 유닛이 아닙니다: {name}")
    return f"piper-{short}.service"


def check_action(name: str, action: str) -> str:
    """허용되는 조합만 통과시킨다. 반환은 `piper-*.service` 이름."""
    unit = unit_name(name)
    short = unit.removeprefix("piper-").removesuffix(".service")
    if action not in ACTIONS:
        raise ValueError(f"모르는 동작입니다: {action}")
    if short in C.UNIT_READONLY:
        raise ValueError(f"{short} 는 안전장치라 웹에서 손대지 않습니다")
    if short == C.UNIT_SELF and action in ("stop", "disable", "restart"):
        raise ValueError(f"{short} 는 이 기능 자체입니다 — 끄면 켜기/끄기가 같이 죽습니다")
    return unit


def _systemctl(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, timeout=timeout)


def _parse_since(value: str) -> float:
    value = (value or "").strip()
    if not value or value == "n/a":
        return 0.0
    try:
        from datetime import datetime
        parts = value.split()
        return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


class UnitHub:
    def list(self) -> list[dict]:
        """카탈로그의 유닛 전부 — **설치 안 된 것도** 낸다(`installed: False`).
        화면이 "왜 simd 가 없지"를 보게 하려면 없는 것도 줄로 보여야 한다."""
        out: list[dict] = []
        for short, (desc, kind) in C.UNIT_CATALOG.items():
            unit = f"piper-{short}.service"
            r = _systemctl("show", unit, "--property=ActiveState",
                           "--property=UnitFileState", "--property=ActiveEnterTimestamp",
                           "--property=MainPID", "--property=LoadState")
            props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
            load = props.get("LoadState", "")
            installed = load not in ("not-found", "") and props.get("UnitFileState", "") != ""
            ufs = props.get("UnitFileState", "")
            try:
                pid = int(props.get("MainPID", "0")) or None
            except ValueError:
                pid = None
            out.append({
                "name": f"piper-{short}", "short": short, "description": desc, "kind": kind,
                "installed": installed,
                "active": props.get("ActiveState") == "active",
                "state": props.get("ActiveState", "unknown"),
                # enabled/disabled 외(static·masked·indirect)는 "모른다" — 체크박스를 안 그린다
                "enabled": True if ufs == "enabled" else False if ufs == "disabled" else None,
                "since": _parse_since(props.get("ActiveEnterTimestamp", "")),
                "pid": pid,
                "readonly": short in C.UNIT_READONLY,
                "self": short == C.UNIT_SELF,
            })
        return out

    def control(self, name: str, action: str) -> dict:
        unit = check_action(name, action)
        r = _systemctl(action, unit, timeout=30)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip() or f"종료 코드 {r.returncode}"
            raise RuntimeError(f"{action} {unit} 실패: {err}")
        logger.warning("%s %s", action, unit)
        return {"unit": unit, "action": action, "ok": True}


    # ── 호스트 사실 — 컨테이너 게이트웨이가 볼 수 없는 것 (feature/version-update.md §2) ──

    _host_cache: dict = {"at": 0.0, "info": None}

    def host_info(self) -> dict:
        """호스트의 바깥 소프트웨어 버전과 배포 디렉토리. 60초 캐시 — 버전은 안 바뀐다.

        ⚠ 드라이버는 `nvidia-smi` 가 아니라 `/proc/driver/nvidia/version` 으로 읽는다.
        nvidia-smi 는 드라이버가 걸리면 D-state 로 멈춰 이 데몬의 RPC 루프까지
        먹는다 — 게이트웨이 자원 패널이 그걸로 한 번 통째로 굳었다.
        """
        now = time.monotonic()
        if self._host_cache["info"] is not None and now - self._host_cache["at"] < 60:
            return self._host_cache["info"]
        info = {"versions": {}, "deploy": {}}
        v = info["versions"]

        def run(cmd: list[str]) -> str:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                return (r.stdout or "").strip()
            except Exception:
                return ""

        import re
        m = re.search(r"version (\S+?),", run(["docker", "--version"]))
        if m:
            v["docker"] = m.group(1)
        m = re.search(r"v=(\S+)", run(["redis-server", "--version"]))
        if m:
            v["redis"] = m.group(1)
        v["python"] = sys.version.split()[0]
        try:
            # "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  580.173.02  Release Build"
            # — 점이 든 첫 숫자열이 버전이다 (x86_64 는 점이 없어 안 걸린다)
            txt = Path("/proc/driver/nvidia/version").read_text()
            m = re.search(r"NVRM version:.*?(\d+\.\d+(?:\.\d+)*)", txt)
            if m:
                v["nvidia-driver"] = m.group(1)
        except OSError:
            pass
        try:
            import json
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=1) as r:
                v["ollama"] = json.load(r).get("version", "")
        except Exception:
            pass
        # 배포 디렉토리 — piper-install.sh 가 버전마다 꺼내 두고 apply.sh 가 적용본을 기록한다
        work = Path(os.environ.get("PIPER_WORK", Path.home() / "piper-web-deploy"))
        versions: list[dict] = []
        for mf in sorted(work.glob("v*/manifest.txt")):
            entry = {"version": mf.parent.name}
            for line in mf.read_text().splitlines():
                if "=" in line:
                    k, val = line.split("=", 1)
                    entry[k.strip()] = val.strip().strip('"')
            versions.append(entry)
        current = work / "current" / "VERSION"
        info["deploy"] = {
            "work": str(work), "versions": versions,
            "current": current.read_text().strip() if current.exists() else None,
            "applied_at": current.stat().st_mtime if current.exists() else None,
        }
        self._host_cache.update(at=now, info=info)
        return info


    # ── 업데이트 — 일시 유닛 (feature/version-update.md §4) ──────────────────
    #
    # 게이트웨이가 직접 돌릴 수 없다: 절차의 마지막(compose up -d / 유닛 재시작)이
    # 게이트웨이 자신을 갈아치운다. `systemd-run` 일시 유닛은 소유자가 systemd 라
    # 호출자가 죽어도 산다. 로그는 저널에 남는다.

    UPDATE_UNIT = "piper-update"

    def _work(self) -> Path:
        return Path(os.environ.get("PIPER_WORK", Path.home() / "piper-web-deploy"))

    def _registry(self) -> str | None:
        """가장 최근 번들의 매니페스트가 말하는 레지스트리. 없으면 None(ghcr 기본)."""
        mfs = sorted(self._work().glob("v*/manifest.txt"), key=lambda p: version_key(p.parent.name))
        for mf in reversed(mfs):
            for line in mf.read_text().splitlines():
                if line.startswith("registry="):
                    return line.split("=", 1)[1].strip().strip('"') or None
        return None

    def update(self, version: str, stage: str, mode: str = "image") -> dict:
        """받기(pull) 또는 적용(apply)을 일시 유닛으로 띄운다. 즉시 돌아온다.

        - image/pull  : `REPO/piper-install.sh <ver> --pull-only` (받고 꺼내기만)
        - image/apply : `<WORK>/<ver>/apply.sh`  (받아 둔 것만 — 없으면 거절)
        - source/apply: `REPO/deploy/update-source.sh <ver>`
        활동 중인지의 판단은 **게이트웨이**가 하고 온다 — 여기는 활동을 모른다.
        """
        if not is_version(version):
            raise ValueError(f"버전 모양이 아닙니다: {version}")
        if stage not in ("pull", "apply") or mode not in ("image", "source"):
            raise ValueError(f"모르는 단계입니다: {mode}/{stage}")
        if self.update_status().get("active"):
            raise RuntimeError("업데이트가 이미 돌고 있습니다 — 끝나기를 기다리세요")
        env = {"PIPER_WORK": str(self._work())}
        if mode == "image":
            reg = self._registry()
            if reg:
                env["PIPER_IMAGE"] = f"{reg}/piper-web-backend"
            if stage == "pull":
                # ⚠ `REPO` 는 두 가지 모양으로 온다. 배포된 번들에서 돌 때는
                # 번들 루트라 `stage-hostside.sh` 가 놓은 대로 최상위에 있고,
                # 저장소를 직접 체크아웃해 돌릴 때(개발·이 테스트)는 저장소
                # 루트라 `deploy/` 밑에 있다 — 둘 다 본다. 실기: .120 에서
                # 웹 [받기] 가 최상위만 찾는 옛 코드로 "이 번들이 낡았다" 를
                # 잘못 보고했다(번들 레이아웃과 안 맞았다).
                script = REPO / "piper-install.sh"
                if not script.exists():
                    script = REPO / "deploy" / "piper-install.sh"
                if not script.exists():
                    raise RuntimeError(f"받기 스크립트가 없습니다: {REPO / 'piper-install.sh'} — 이 번들이 낡았다")
                cmd = [str(script), version, "--pull-only"]
            else:
                script = self._work() / version / "apply.sh"
                if not script.exists():
                    raise RuntimeError(f"{version} 을 아직 받지 않았습니다 — 먼저 [받기]")
                cmd = [str(script)]
        else:
            script = REPO / "deploy" / "update-source.sh"
            if stage == "pull":
                return {"unit": None, "skipped": "소스 기계는 받기 단계가 없다 — 적용이 fetch 한다"}
            cmd = [str(script), version]
        # 지난 실행의 유닛을 비운다 — `--remain-after-exit` 로 남겨 두므로(끝난 뒤에도
        # 상태·저널을 읽으려고) 같은 이름으로 다시 띄우려면 먼저 내려야 한다.
        # ⚠ `--collect` 를 쓰면 끝나는 순간 유닛이 사라져 "끝났나"를 알 길이 없다(실측).
        _systemctl("stop", f"{self.UPDATE_UNIT}.service")
        _systemctl("reset-failed", f"{self.UPDATE_UNIT}.service")
        run = ["systemd-run", "--user", "--unit", self.UPDATE_UNIT, "--remain-after-exit",
               "--description", f"piper-web {stage} {version}",
               "--property", f"WorkingDirectory={REPO}"]
        for k, v in env.items():
            run += ["--setenv", f"{k}={v}"]
        r = subprocess.run(run + cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise RuntimeError(f"업데이트 유닛을 못 띄웠습니다: {(r.stderr or r.stdout).strip()}")
        state = {"version": version, "stage": stage, "mode": mode, "started": time.time()}
        self._work().mkdir(parents=True, exist_ok=True)
        (self._work() / ".update.json").write_text(json.dumps(state))
        logger.warning("업데이트 %s %s (%s) 시작", stage, version, mode)
        return {"unit": self.UPDATE_UNIT, **state}

    def update_status(self) -> dict:
        """일시 유닛의 상태 + 저널 꼬리 + 전제 미비(sudo) 줄. 없으면 idle."""
        r = _systemctl("show", f"{self.UPDATE_UNIT}.service", "--property=ActiveState",
                       "--property=SubState", "--property=Result", "--property=ExecMainStatus",
                       "--property=LoadState")
        props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        state: dict = {}
        try:
            state = json.loads((self._work() / ".update.json").read_text())
        except Exception:
            pass
        loaded = props.get("LoadState") not in ("not-found", "")
        sub = props.get("SubState", "")
        # RemainAfterExit 라 끝난 뒤에도 ActiveState=active 다 — 돌고 있는지는 SubState 가 말한다
        active = loaded and (props.get("ActiveState") == "activating" or sub in ("running", "start", "start-pre", "start-post", "stop", "stop-sigterm", "stop-post"))
        finished = bool(state) and loaded and not active and sub in ("exited", "failed", "dead")
        log = ""
        if state:
            since = [f"--since=@{int(state.get('started', 0))}"] if state.get("started") else ["-n", "300"]
            j = subprocess.run(["journalctl", "--user", "-u", self.UPDATE_UNIT, "-o", "cat", "--no-pager", *since],
                               capture_output=True, text=True, timeout=10)
            import re
            log = re.sub(r"\x1b\[[0-9;]*m", "", j.stdout)      # 색 코드는 화면에 글자로 보인다
        result = props.get("Result", "")
        return {
            "active": active, "result": result if loaded else None, "exit": props.get("ExecMainStatus") if loaded else None,
            "ok": finished and result == "success" and props.get("ExecMainStatus") == "0",
            "finished": finished, "sub": sub, "log": log[-12000:],
            "need_sudo": parse_need_sudo(log), **state,
        }

    def logs(self, unit: str = "all", lines: int = 300, level: str = "info", since: str | None = None) -> dict:
        return fetch_logs(unit, lines, level, since)

    def notes(self, version: str) -> str:
        """받아 둔 번들의 CHANGELOG 에서 그 버전의 절만. 없으면 빈 문자열."""
        if not is_version(version):
            raise ValueError(f"버전 모양이 아닙니다: {version}")
        for cand in (self._work() / version / "CHANGELOG.md", REPO / "CHANGELOG.md"):
            if cand.exists():
                return changelog_section(cand.read_text(), version)
        return ""


def is_version(v: str) -> bool:
    import re
    return bool(re.match(r"^v\d+\.\d+\.\d+$", v or ""))


def version_key(v: str) -> tuple:
    import re
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", v or "")
    return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)


def parse_need_sudo(log: str) -> list[str]:
    """apply.sh 가 "아래를 먼저 실행하세요" 뒤에 찍는 `sudo …` 줄들 — 스크립트는 sudo 를
    직접 안 쓰므로 이 줄이 곧 사람이 할 일이다."""
    out: list[str] = []
    for line in (log or "").splitlines():
        s = line.strip()
        if s.startswith("sudo "):
            out.append(s)
    return out


def changelog_section(text: str, version: str) -> str:
    """`## vX.Y.Z` 절 하나. 다음 `## ` 전까지."""
    marker = f"## {version}"
    i = text.find(marker)
    if i < 0:
        return ""
    j = text.find("\n## ", i + len(marker))
    return text[i:j if j > 0 else None].strip()


# ── 로그 — 데몬 저널을 웹에서 (feature/version-update.md 의 이웃, 사용자 요청 2026-09-10) ──
#
# 컨테이너 게이트웨이는 호스트 저널을 못 읽는다 — unitd 가 읽어 준다. 데몬은 stdout 으로
# 찍으므로 journald 우선순위는 전부 6(info)이다 — 레벨은 **메시지 토큰**에서 가른다:
# 파이썬 로깅 `[ERROR]`·`[WARNING]`, uvicorn `ERROR:`·`WARNING:`, systemd 의 "Failed" 줄.

LOG_LINE_CAP = 5000
LOG_BUDGET_S = 8.0     # 저널 훑기 시간 예산 — RPC 타임아웃(30초) 안에 답한다
# journalctl -g (PCRE, 대소문자 구분) — 레벨 토큰과 트레이스백의 줄 모양
LOG_GREP_ERROR = r"\[(ERROR|CRITICAL)\]|^(ERROR|CRITICAL):|Traceback \(most recent|^\s+File \"|^[A-Za-z_.]*(Error|Exception)\b|Failed with result"
LOG_GREP_WARNING = r"\[(WARNING|ERROR|CRITICAL)\]|^(WARNING|ERROR|CRITICAL):|Traceback \(most recent|^\s+File \"|^[A-Za-z_.]*(Error|Exception)\b|Failed with result"
LEVEL_RANK = {"error": 3, "warning": 2, "info": 1, "debug": 0}
_TB_LINE = re.compile(r"^\s+File \"|^[A-Za-z_.]*(Error|Exception)\b")
_TOKEN = re.compile(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]|^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s")


def infer_level(msg: str, prio: str | int | None) -> str:
    m = _TOKEN.search(msg or "")
    if m:
        tok = (m.group(1) or m.group(2)).lower()
        return {"critical": "error", "warn": "warning"}.get(tok, tok)
    m0 = msg or ""
    # 트레이스백의 줄들은 **그 자체로** 오류다 — journald 가 -g 로 골라 보내면 "Traceback"
    # 첫 줄이 창 밖에 있을 수 있어 묶기만으로는 info 로 되돌아간다(실측: 24줄 걸리고 0줄).
    if ("Traceback (most recent call last)" in m0 or "Failed with result" in m0
            or _TB_LINE.match(m0)):
        return "error"
    try:
        p = int(prio) if prio is not None else 6
    except (TypeError, ValueError):
        p = 6
    return "error" if p <= 3 else "warning" if p == 4 else "debug" if p >= 7 else "info"


def parse_journal_lines(text: str) -> list[dict]:
    """`journalctl -o json` 줄들 → [{t, unit, level, msg}]. 깨진 줄은 건너뛴다."""
    out: list[dict] = []
    in_tb: dict[str, bool] = {}
    for line in (text or "").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):                       # 비-UTF8 은 바이트 배열로 온다
            msg = bytes(b for b in msg if isinstance(b, int)).decode(errors="replace")
        msg = msg or ""
        try:
            t = int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        except (TypeError, ValueError):
            t = 0.0
        unit = str(e.get("_SYSTEMD_USER_UNIT") or e.get("_SYSTEMD_UNIT") or e.get("UNIT") or "")
        unit = unit.removeprefix("piper-").removesuffix(".service")
        if unit in ("init.scope", ""):
            unit = "systemd"                     # 사용자 관리자 자신의 줄 (Started/Failed …)
        level = infer_level(msg, e.get("PRIORITY"))
        # 트레이스백은 한 덩어리다 — 첫 줄만 error 로 두면 "오류만" 에서 프레임·예외 줄이
        # 빠져 무엇이 터졌는지 못 본다. 다음 레벨 토큰 줄이 나올 때까지 error 로 잇는다.
        if "Traceback (most recent call last)" in msg:
            in_tb[unit] = True
        elif in_tb.get(unit):
            if _TOKEN.search(msg):
                in_tb[unit] = False
            else:
                level = "error"
        out.append({"t": t, "unit": unit, "level": level, "msg": msg})
    return out


def log_units(name: str) -> list[str]:
    """웹이 고른 이름 → journalctl -u 인자. 카탈로그 + gateway·frontend·update, `all` 은 glob."""
    if name == "all":
        # glob(`piper-*`)보다 명시 목록이 빠르다 — journald 가 유닛 색인으로 바로 간다(실측 1초 차)
        return [f"piper-{s}.service"
                for s in (*C.UNIT_CATALOG, *C.LOG_EXTRA_UNITS)]
    short = name.removeprefix("piper-").removesuffix(".service")
    if short in C.UNIT_CATALOG or short in C.LOG_EXTRA_UNITS:
        return [f"piper-{short}.service"]
    raise ValueError(f"모르는 유닛입니다: {name}")


_SINCE = re.compile(r"^(\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?|\d+ ?(min|minutes?|hours?|days?) ago|today|yesterday)$")


def fetch_logs(unit: str, lines: int = 300, level: str = "info", since: str | None = None,
               docker_container: str | None = "piper-web-backend") -> dict:
    """저널(또는 배포 호스트의 gateway 컨테이너 로그)을 읽어 레벨로 거른다.
    거를수록 더 읽는다 — 오류 30줄을 보려면 info 3천 줄을 봐야 할 수 있다."""
    if level not in LEVEL_RANK:
        raise ValueError(f"모르는 레벨입니다: {level}")
    if since and not _SINCE.match(since):
        raise ValueError(f"시각 모양이 아닙니다: {since}")
    lines = max(1, min(int(lines), LOG_LINE_CAP))
    units = log_units(unit)
    # 거를 때는 journald 에게 먼저 거르게 한다(`-g`, C 로 전 구간) — 게이트웨이 접근 로그가
    # 시간당 수천 줄이라 "마지막 5000줄" 창으로는 어제 오류가 안 잡혔다(실측). 패턴은
    # 레벨 토큰 + 트레이스백 줄(들여쓴 File·예외 클래스·Failed) — 묶기가 되게.
    filtered = level not in ("info", "debug")
    want = lines * 10 if filtered else lines
    entries: list[dict] = []
    source = "journal"
    partial = False
    unit_exists = _systemctl("show", units[0], "--property=LoadState").stdout.strip() not in ("LoadState=not-found", "")
    if units[0] == "piper-gateway.service" and not unit_exists and docker_container:
        # 배포 호스트 — 게이트웨이는 컨테이너다
        source = "docker"
        cmd = ["docker", "logs", "--timestamps", "--tail", str(want)]
        if since:
            cmd += ["--since", since.replace(" ago", "").replace("min", "m").replace("hour", "h").replace("day", "d").replace("s", "").replace(" ", "")] if "ago" in since else []
        r = subprocess.run(cmd + [docker_container], capture_output=True, text=True, timeout=15)
        for line in (r.stdout + r.stderr).splitlines():
            ts, _, msg = line.partition(" ")
            try:
                from datetime import datetime
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                t, msg = 0.0, line
            entries.append({"t": t, "unit": "gateway", "level": infer_level(msg, 6), "msg": msg})
    else:
        # ⚠ `-r`(최신부터) 로 스트리밍하고 **시간 예산** 안에 온 만큼만 쓴다. `-g` 는
        #   게이트웨이 접근 로그(시간당 수천 줄)를 PCRE 로 거꾸로 훑는데, "오류만 · 2일"이
        #   16초 걸렸다(실측) — RPC 타임아웃(30초)에 걸리면 400 이고 아무것도 못 본다.
        #   최신 오류가 먼저 오므로 부분 결과도 쓸모가 있다. `partial` 로 말한다.
        cmd = ["journalctl", "--user", "-o", "json", "--no-pager", "-r", "-n", str(want)]
        for x in units:
            cmd += ["-u", x]
        if since:
            cmd += ["--since", since]
        if filtered:
            cmd += ["-g", LOG_GREP_WARNING if level == "warning" else LOG_GREP_ERROR]
        text, partial = _journal_stream(cmd, want, LOG_BUDGET_S)
        entries = list(reversed(parse_journal_lines(text)))
    need = LEVEL_RANK[level]
    kept = collapse_repeats([e for e in entries if LEVEL_RANK.get(e["level"], 1) >= need])
    return {"entries": kept[-lines:], "scanned": len(entries), "source": source,
            "truncated": len(kept) > lines, "partial": partial}


def _journal_stream(cmd: list[str], want: int, budget_s: float) -> tuple[str, bool]:
    """journalctl 을 띄워 줄 단위로 읽는다 — `want` 줄이 차거나 예산이 다하면 멈춘다.
    반환: (읽은 텍스트, 예산에 걸렸는가)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    deadline = time.monotonic() + budget_s
    lines: list[str] = []
    partial = False
    try:
        while len(lines) < want:
            left = deadline - time.monotonic()
            if left <= 0:
                partial = True
                break
            ready, _, _ = select.select([proc.stdout], [], [], min(left, 0.5))
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        proc.wait(timeout=2)
    return "".join(lines), partial


def collapse_repeats(rows: list[dict]) -> list[dict]:
    """같은 유닛의 같은 줄이 연달아 오면 하나로(`count`, 마지막 시각 `t_last`) — 프론트
    개발 서버가 게이트웨이 재시작 사이에 `ECONNREFUSED` 를 24번 찍어 "오류만" 창을
    통째로 채웠다(실측). 세는 것이 지우는 것보다 정직하다."""
    out: list[dict] = []
    for e in rows:
        if out and out[-1]["unit"] == e["unit"] and out[-1]["msg"] == e["msg"]:
            out[-1]["count"] = out[-1].get("count", 1) + 1
            out[-1]["t_last"] = e["t"]
            continue
        out.append(dict(e, count=1, t_last=e["t"]))
    return out


_METHODS = {"list", "control", "host_info", "update", "update_status", "notes", "logs"}


def serve(bus: Bus, hub: UnitHub) -> None:
    global _running
    logger.info("유닛 관리 데몬 시작")
    last_beat = 0.0
    while _running:
        now = time.monotonic()
        if now - last_beat > C.DAEMON_ALIVE_TTL_MS / 3000:
            try:
                bus.mark_alive(C.UNITD, info=self_report(REPO, C.DAEMON_SOURCES[C.UNITD]))
            except Exception as exc:
                logger.warning("생존 표시 실패: %s", exc)
            last_beat = now
        try:
            req = bus.rpc_next_request(C.UNITD, timeout=1)
        except Exception as exc:
            logger.warning("요청 수신 오류: %s", exc)
            time.sleep(0.5)
            continue
        if req is None:
            continue
        rid, method, args = req.get("id", ""), req.get("method", ""), req.get("args", [])
        if method == "restart":
            bus.rpc_reply(rid, True, result="restarting")
            logger.warning("재시작 요청 — 종료")
            _running = False
            continue
        if method not in _METHODS:
            bus.rpc_reply(rid, False, error=f"알 수 없는 메서드: {method}")
            continue
        try:
            bus.rpc_reply(rid, True, result=getattr(hub, method)(*args))
        except Exception as exc:
            logger.warning("%s 실패: %s", method, exc)
            bus.rpc_reply(rid, False, error=str(exc))


def main() -> int:
    def _bye(signum, _frame):
        global _running
        _running = False
        logger.info("신호 %s — 종료", signum)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    bus = Bus()
    if not bus.ping():
        logger.error("Redis 에 연결할 수 없습니다 (%s)", os.environ.get("PIPER_REDIS_URL", "기본값"))
        return 1
    if _systemctl("--version").returncode != 0:
        logger.error("사용자 systemd 에 접속할 수 없습니다 — 이 데몬은 호스트에서만 뜻이 있다")
        return 1
    serve(bus, UnitHub())
    logger.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
