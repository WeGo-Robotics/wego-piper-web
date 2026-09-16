"""Vast.ai 프로바이더 — CLI 를 감싼다 (feature/vast-training.md §9-2).

⚠ **왜 SDK 가 아니라 CLI 인가.** `vastai` 파이썬 패키지는 게이트웨이 인터프리터에
import 되지 않는다(실측). 이 저장소가 LeRobot 에 쓰는 것과 같은 선택이다 — CLI 를
감싸면 업그레이드가 우리 프로세스를 깨뜨리지 않는다.

⚠ **API 키는 argv 가 아니라 env 로.** `VAST_API_KEY` 가 CLI 의 저장된 키를 이기는
것을 확인했다(가짜 키를 넣으니 `Invalid user key`). argv 는 같은 호스트의 다른
프로세스가 `ps` 로 읽는다. 키를 안 주면 CLI 가 제 파일
(`~/.config/vastai/vast_api_key`)을 읽으므로, 사람이 `vastai set api-key` 로 맞춰 둔
머신에서는 그대로 돈다.

⚠ **이름이 셋 다르다** — 질의는 `cuda_vers`, 템플릿에 저장될 땐 `cuda_max_good`,
오퍼 응답도 `cuda_max_good`. 신뢰도는 질의가 `reliability`, 응답이 `reliability2`.
셋을 헷갈리면 필터는 도는데 파서가 0 을 읽는다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time

from collections import Counter

from .base import (
    GpuModel, Instance, Offer, OfferFilter, SSHTarget, Template,
    gpu_support, hourly_total, warnings_for,
)

logger = logging.getLogger(__name__)

#: 오퍼 조회를 몇 초 동안 재사용하나. ⚠ `search offers` 는 실측 2~4초다. api.ts 의
#: GET 단일비행은 *동시* 요청만 접지, 탭을 오갈 때마다 부르는 건 못 막는다. 가격은
#: 초 단위로 변하지 않으므로 자동 폴링 대신 이 캐시 + 새로고침 버튼으로 간다.
CACHE_TTL = 60.0

#: CLI 한 번에 허용하는 시간. 검색이 느린 날이 있다.
TIMEOUT = 45

#: 카탈로그 TTL. 바뀌는 것은 가격이 아니라 **기종 목록**이라 분 단위로는 안 변한다.
#: (실측: 같은 질의 3회에서 행 수는 234~238 로 흔들려도 **이름 집합은 완전히 동일**했다.)
CATALOG_TTL = 600

#: 카탈로그 전용 시간 제한. 실측 질의당 2.5초라 45초는 고장난 날 손해만 키운다.
CATALOG_TIMEOUT = 20

#: 캐시 항목 상한. 키가 질의 문자열이라 사용자가 디스크·가격·CPU 칸을 만질 때마다
#: 새 항목이 생긴다 — 두면 무한히 자란다.
CACHE_MAX = 64

_GPU_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,40}$")
_cache: dict[str, tuple[float, object]] = {}


def cli_available() -> bool:
    return shutil.which("vastai") is not None


def build_query(f: OfferFilter, *, with_gpu: bool = True, with_cuda: bool = True) -> str:
    """필터 → Vast 질의 문자열. **여기가 주입 경계다.**

    ⚠ 필드명과 연산자는 이 함수 안에만 있고, 값은 전부 `int()`/`float()` 를 거친다.
    문자열인 GPU 이름만 모양을 따로 검사한다 — 사용자 문자열이 그대로 붙는 경로를
    만들지 않는다. 질의는 공백으로 나뉘므로 이름의 공백은 밑줄로 바꾼다.

    ⚠ **카탈로그도 이 함수를 쓴다.** 항을 빼는 스위치를 여기 두는 이유다 — 카탈로그가
    자기 질의를 따로 만들면 두 화면의 필터가 조용히 갈라지고, 그게 곧 "선택지에는
    있는데 표는 0행" 이다.
    """
    names = [n.strip() for n in (f.gpu_names or ()) if n and n.strip()]
    for n in names:
        if not _GPU_NAME_RE.match(n):
            raise ValueError(f"GPU 이름 모양이 아닙니다: {n!r}")
    # 공백은 밑줄로 — 질의가 공백으로 나뉘므로 "RTX 4090" 은 토큰 둘이 된다
    tokens = [n.replace(" ", "_") for n in names]

    parts = []
    if not with_gpu:
        tokens = []
    if len(tokens) == 1:
        parts.append(f"gpu_name={tokens[0]}")
    elif tokens:
        # ⚠ Vast 의 목록 문법(실측: `gpu_name in [RTX_4090,RTX_3060]` 가 91개를 준다).
        #   이름은 위에서 모양 검사를 통과한 것뿐이라 대괄호 안이 안전하다.
        parts.append(f"gpu_name in [{','.join(tokens)}]")
    # 비어 있으면 아무 토큰도 안 넣는다 = 전체 (실측: 기본 필터만으로 241개·56종)

    parts += [f"num_gpus={int(f.num_gpus)}"]
    if with_cuda:
        # ⚠ Vast 의 `cuda_vers` 는 드라이버 최대치가 아니라 **이 기계가 실제로 돌릴 수
        #   있는 CUDA** 다(실측: RTX_5090 은 >=12.7 까지 0개, >=12.8 에서 46개). 그래서
        #   이 항 하나가 Blackwell 전체를 조용히 걸러낸다 — 카탈로그는 이 항을 빼고
        #   한 번 더 물어서 "왜 안 보이는지" 를 말할 수 있게 한다.
        parts.append(f"cuda_vers>={float(f.min_cuda):g}")
    parts += [
        f"reliability>{float(f.min_reliability):g}",
        f"inet_down>{float(f.min_inet_down):g}",
        # 여유 디스크가 요청보다 작은 기계는 애초에 못 쓴다 — 필터이자 가격 입력(§9-2)
        f"disk_space>={float(f.disk_gb):g}",
        "rentable=true",
    ]
    if f.min_cpu_cores is not None:
        parts.append(f"cpu_cores_effective>={float(f.min_cpu_cores):g}")
    if f.max_price is not None:
        # ⚠ Vast 쪽 상한은 `dph_total` 기준이다. 우리가 표시하는 시간당은 디스크를
        #   다시 얹어 이보다 크므로, 넉넉히 거르고 최종 정렬·표시는 우리 값으로 한다.
        parts.append(f"dph_total<={float(f.max_price):g}")
    return " ".join(parts)


def parse_offer(raw: dict, disk_gb: float) -> Offer:
    """오퍼 한 줄. 필드 100개 중 판단에 쓰이는 것만 꺼낸다."""
    hourly, storage = hourly_total(raw, disk_gb)
    cpu = float(raw.get("cpu_cores_effective") or 0.0)
    cuda = float(raw.get("cuda_max_good") or 0.0)
    disk_space = float(raw.get("disk_space") or 0.0)
    cc = raw.get("compute_cap")
    return Offer(
        id=int(raw["id"]),
        machine_id=int(raw.get("machine_id") or 0),
        gpu_name=str(raw.get("gpu_name") or "?"),
        num_gpus=int(raw.get("num_gpus") or 1),
        gpu_ram_gb=round(float(raw.get("gpu_ram") or 0.0) / 1024.0, 1),
        hourly=hourly,
        dph_base=float(raw.get("dph_base") or 0.0),
        storage_hourly=storage,
        disk_gb=disk_gb,
        dph_total=float(raw.get("dph_total") or 0.0),
        inet_down_cost_per_gb=float(raw.get("inet_down_cost") or 0.0),
        inet_up_cost_per_gb=float(raw.get("inet_up_cost") or 0.0),
        inet_down_mbps=float(raw.get("inet_down") or 0.0),
        inet_up_mbps=float(raw.get("inet_up") or 0.0),
        cuda_max_good=cuda,
        # ⚠ 응답 필드는 `reliability2` 다 (질의의 `reliability` 와 다른 이름)
        reliability=float(raw.get("reliability2") or 0.0),
        dlperf=float(raw.get("dlperf") or 0.0),
        dlperf_per_dph=float(raw.get("dlperf_per_dphtotal") or 0.0),
        cpu_cores=cpu,
        cpu_ram_gb=round(float(raw.get("cpu_ram") or 0.0) / 1024.0, 1),
        disk_space_gb=disk_space,
        disk_bw=float(raw.get("disk_bw") or 0.0),
        geolocation=str(raw.get("geolocation") or "?"),
        duration_days=round(float(raw.get("duration") or 0.0) / 86400.0, 1),
        verified=str(raw.get("verification") or "") == "verified",
        rentable=bool(raw.get("rentable")),
        compute_cap=cc,
        support=gpu_support(cc),
        warnings=warnings_for(cpu_cores=cpu, cuda_max_good=cuda,
                              disk_space_gb=disk_space, disk_gb=disk_gb,
                              compute_cap=cc),
    )


def parse_template(raw: dict) -> Template:
    """템플릿 한 벌. 변종은 **태그**에서 읽는다 — 이름은 사람이 고칠 수 있다."""
    tag = raw.get("tag") or raw.get("default_tag") or ""
    name = str(raw.get("name") or "")
    hay = f"{tag} {name}".lower()
    variant = "full" if "full" in hay else "slim" if "slim" in hay else ""
    return Template(
        id=int(raw.get("id") or 0),
        name=name,
        image=str(raw.get("image") or ""),
        tag=str(tag) or None,
        disk_gb=float(raw.get("recommended_disk_space") or 0.0),
        description=str(raw.get("desc") or ""),
        variant=variant,
    )


class VastProvider:
    """`CloudProvider` 중 지금 구현된 만큼 — 조회뿐이다.

    ⚠ 인스턴스를 띄우는 `create` 는 여기 없다. 파기·예산 가드와 한 몸이라(§6)
    W3 과 같이 간다. 지금 넣으면 "빌릴 수는 있는데 끌 수는 없는" 상태가 된다.
    """

    name = "vast"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    # ── CLI ──────────────────────────────────────────────────────────────
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self._api_key:
            env["VAST_API_KEY"] = self._api_key
        return env

    def _raw(self, args: list[str], *, timeout: int = TIMEOUT) -> object:
        """`vastai <args> --raw` → 파싱된 JSON. 실패는 `RuntimeError` 로 올린다."""
        if not cli_available():
            raise FileNotFoundError("vastai")
        out = subprocess.run(["vastai", *args, "--raw"], capture_output=True,
                             text=True, timeout=timeout, env=self._env())
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).strip()[:300] or "vastai 실패")
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"vastai 응답을 읽지 못했습니다: {out.stdout[:200]}") from exc
        # ⚠ CLI 는 오류도 0 으로 끝내고 본문에 실어 보낸다 (실측: `{"error": true, ...}`)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data.get("msg") or "vastai 오류"))
        return data

    def _cached(self, key: str, make, ttl: float = CACHE_TTL):
        """⚠ TTL 은 **인자**다. 상수 하나를 공유하면 카탈로그를 위해 늘릴 때 가격표까지
        같이 늙는다 — 10분 묵은 $/h 는 틀린 값이다."""
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = make()
        _cache[key] = (time.monotonic(), value)
        while len(_cache) > CACHE_MAX:                  # 오래된 것부터 버린다
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
        return value

    # ── 조회 ─────────────────────────────────────────────────────────────
    def search(self, f: OfferFilter, *, refresh: bool = False) -> list[Offer]:
        q = build_query(f)
        key = f"offers:{q}:{f.disk_gb:g}"   # q 가 GPU 목록을 이미 담는다
        if refresh:
            _cache.pop(key, None)

        def _go() -> list[Offer]:
            rows = self._raw(["search", "offers", q]) or []
            offers = [parse_offer(r, f.disk_gb) for r in rows if isinstance(r, dict)]
            # 기본 정렬은 **최저가가 아니라 가성비**다 — 최저가 정렬은 CUDA 가 모자란
            # 기계를 맨 위로 올린다(§9-2).
            offers.sort(key=lambda o: o.dlperf_per_dph, reverse=True)
            return offers[: int(f.limit)]

        return self._cached(key, _go)

    def templates(self, *, refresh: bool = False) -> list[Template]:
        """**우리** 템플릿. ⚠ `private=true` 가 없으면 공개 2048개가 오고 우리 건 없다."""
        if refresh:
            _cache.pop("templates", None)

        def _go() -> list[Template]:
            rows = self._raw(["search", "templates", "private=true"]) or []
            out = [parse_template(r) for r in rows if isinstance(r, dict)]
            # full 을 먼저 — 네트워크가 빠르면 4.7GB pull 이 부팅 설치보다 낫다
            out.sort(key=lambda t: (t.variant != "full", t.name))
            return out

        return self._cached("templates", _go)

    def catalog(self, f: OfferFilter, *, refresh: bool = False) -> list[GpuModel]:
        """빌릴 수 있는 **GPU 기종** — 사용자가 "싹 다 보여줘" 라고 한 그 목록이다.

        질의 **둘**을 쓴다. 둘 다 표가 쓰는 필터 그대로에서 항만 뺀 것이라, 선택지와
        표가 같은 세계를 본다:

        - **A** = 표의 필터에서 GPU 이름만 뺀 것 → *지금 진짜 고를 수 있는* 기종
        - **B** = 거기서 `cuda_vers` 까지 뺀 것 → 그 항이 **숨기고 있던** 기종

        `B - A` 가 곧 "시장엔 있는데 우리 조건으론 안 나오는" 것들이다. 실측
        (2026-09-16): A=234행·55종, B=412행·70종, 차이가 **정확히 Blackwell 15종**
        (RTX 50 전 계열 · RTX PRO · B200/B300)이었다.

        ⚠ **왜 넓게 훑지 않나.** 처음엔 `rentable=true` 를 정렬 각도 3번으로 훑어
        80종을 모았다. 그런데 한 질의는 서버에서 **512개로 잘리고**, 같은 질의를 두 번
        돌리면 결과가 갈린다 — "각도를 더 늘려도 0종 추가" 는 수렴이 아니라 그날
        뽑기였다. 반면 필터를 건 질의는 512 아래로 내려와 **잘리지 않고**, 이름 집합이
        3회 연속 동일했다. 표본을 넓히는 것보다 **범위를 좁히는 쪽**이 정확하다.

        ⚠ **씨앗 목록을 코드에 박지 않는다.** 박아 두면 그게 썩는다 — 바로 그래서
        3060 이 빠지고 (우리 이미지로 못 도는) RTX 5090 이 들어 있는 고정 7개가
        생겼었다. 조회가 실패하면 빈 목록을 주고, 화면이 지금 오퍼에 보이는 기종과
        합쳐 쓴다.
        """
        qa = build_query(f, with_gpu=False)
        qb = build_query(f, with_gpu=False, with_cuda=False)
        key = f"catalog:{qa}|{qb}|{f.disk_gb:g}"
        if refresh:
            _cache.pop(key, None)

        def _scan(q: str) -> dict[str, dict]:
            agg: dict[str, dict] = {}
            rows = self._raw(["search", "offers", q, "--limit", "2000"],
                             timeout=CATALOG_TIMEOUT) or []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                name = r.get("gpu_name")
                if not name:
                    continue
                hourly, _ = hourly_total(r, f.disk_gb)
                cur = agg.setdefault(name, {"n": 0, "min": hourly,
                                            "cc_votes": Counter(), "vram_votes": Counter()})
                cur["n"] += 1
                cur["min"] = min(cur["min"], hourly)
                # ⚠ **첫 행을 믿지 않는다.** 같은 기종 안에서 호스트마다 값이 갈린다 —
                #   실측: RTX 4080S 4대 중 `gpu_ram` 이 16376(2대·실물)과 32760(1대·거짓)
                #   으로 섞여 나온다. 첫 행을 쓰면 라벨이 동전 던지기로 거짓말한다.
                if r.get("compute_cap"):
                    cur["cc_votes"][r["compute_cap"]] += 1
                if r.get("gpu_ram"):
                    cur["vram_votes"][round(float(r["gpu_ram"]) / 1024.0, 1)] += 1
            # ⚠ 512 면 잘린 것이다 — 그때는 이 목록이 "전부" 가 아니라 표본이다
            agg["__truncated__"] = {"v": len(rows) >= 512}
            return agg

        def _go() -> list[GpuModel]:
            a = _scan(qa)
            b = _scan(qb)
            a.pop("__truncated__", None)
            b.pop("__truncated__", None)

            out: list[GpuModel] = []
            def _mode(votes: Counter, default=None):
                """최빈값. 동률이면 **작은 쪽** — 거짓 보고는 대개 크게 부풀린다."""
                if not votes:
                    return default
                top = max(votes.values())
                return min(k for k, n in votes.items() if n == top)

            for name in set(a) | set(b):
                src = a.get(name) or b[name]
                cc = _mode(src["cc_votes"])
                support = gpu_support(cc)
                available = name in a
                reason = None
                if not available:
                    reason = ("우리 이미지(cu126)에 이 GPU 커널이 없습니다 — cu128 로 다시 구워야 합니다"
                              if support == "too_new" else
                              "지금 필터 조건을 만족하는 기계가 없습니다")
                out.append(GpuModel(
                    name=name, compute_cap=cc, support=support,
                    vram_gb=_mode(src["vram_votes"], 0.0),
                    offers_seen=a[name]["n"] if available else 0,
                    min_hourly=src["min"], available=available, reason=reason))
            # 고를 수 있는 것 먼저, 그 안에서 싼 것 먼저
            out.sort(key=lambda g: (not g.available, g.min_hourly))
            return out

        return self._cached(key, _go, ttl=CATALOG_TTL)

    # ── 수명 ─────────────────────────────────────────────────────────────
    def _parse_instance(self, raw: dict) -> Instance:
        # ⚠ CLI 1.7 이 실제로 읽는 상태 필드는 `actual_status` 다. `cur_state`·
        #   `intended_status` 로 판단하면 "곧 그렇게 될 것" 을 "그렇다" 로 읽는다.
        host, port = raw.get("ssh_host"), raw.get("ssh_port")
        return Instance(
            id=int(raw.get("id") or 0),
            label=str(raw.get("label") or ""),
            status=str(raw.get("actual_status") or "unknown"),
            gpu_name=str(raw.get("gpu_name") or "?"),
            rate_usd_h=float(raw.get("dph_total") or 0.0),
            ssh=SSHTarget(host=str(host), port=int(port)) if host and port else None,
            image=str(raw.get("image_uuid") or ""),
            message=str(raw.get("status_msg") or "").strip()[:200],
        )

    def create(self, offer_id: int, *, template_hash: str, disk_gb: float,
               label: str) -> Instance:
        """오퍼 하나를 빌린다. **여기서부터 돈이 나간다.**

        ⚠ `--cancel-unavail` 이 없으면 스케줄에 실패했을 때 **정지된 인스턴스가 조용히
        만들어지고 스토리지 과금이 계속된다.** 실패는 실패로 끝나야 한다.

        ⚠ `--disk` 를 명시한다. 템플릿의 `recommended_disk_space` 가 적용되지 않아
        기본값으로 뜨면 이미지 전개가 안 들어가 pull 이 실패한다.

        ⚠ `--label` 은 장식이 아니다. 레지스트리를 잃었을 때 **우리 것을 알아보는 유일한
        단서**이고, 그게 고아 스캐너가 서는 자리다(§6-3).
        """
        out = self._raw(["create", "instance", str(int(offer_id)),
                         "--template_hash", template_hash,
                         "--disk", f"{float(disk_gb):g}",
                         "--ssh", "--direct",
                         "--label", label,
                         "--cancel-unavail"])
        if not isinstance(out, dict) or not out.get("success"):
            raise RuntimeError(f"인스턴스를 만들지 못했습니다: {str(out)[:200]}")
        cid = out.get("new_contract")
        if not isinstance(cid, int):
            raise RuntimeError(f"인스턴스 id 를 받지 못했습니다: {str(out)[:200]}")
        got = self.status(cid)
        return got or Instance(id=cid, label=label, status="created", gpu_name="?",
                               rate_usd_h=0.0, ssh=None)

    def list_instances(self) -> list[Instance]:
        """지금 살아 있는 인스턴스 전부.

        ⚠ **타입을 검사한다.** CLI 는 오류도 종료코드 0 으로 내고 본문에
        `{"error": true, ...}` 를 싣는다 — 그걸 그대로 받으면 **오류가 빈 목록으로
        둔갑하고**, 빈 목록은 "아무것도 안 돌고 있다" 로 읽힌다. 돈이 걸린 판단에서
        가장 위험한 착각이다.
        """
        rows = self._raw(["show", "instances"])
        if not isinstance(rows, list):
            raise RuntimeError(f"인스턴스 목록이 배열이 아닙니다: {str(rows)[:200]}")
        return [self._parse_instance(r) for r in rows if isinstance(r, dict)]

    def status(self, instance_id: int) -> Instance | None:
        """하나만. 없으면 `None` — 파기됐다는 뜻이다."""
        for i in self.list_instances():
            if i.id == int(instance_id):
                return i
        return None

    def destroy(self, instance_id: int) -> bool:
        """파기하고 **목록으로 확인한다.** 확인될 때만 True.

        ⚠ **`-y` 가 없으면 파기되지 않는다.** 실측: 프롬프트(`[y/N]`)에서 멈췄다가
        `Aborted.` 를 찍고 **종료코드 0** 으로 끝난다 — `&& echo 완료` 가 완료를 찍고,
        인스턴스는 멀쩡히 살아서 과금된다.

        ⚠ **응답으로 판정하지 않는다.** `destroy -y --raw` 는 실측에서 **빈 출력**을
        낸다. 파싱할 것이 없다. 그래서 진실은 목록에서만 온다.
        """
        try:
            self._raw(["destroy", "instance", str(int(instance_id)), "-y"])
        except Exception as exc:                                    # noqa: BLE001
            # 응답이 비어 파싱에 실패하는 것은 **정상 경로다.** 여기서 멈추지 않고
            # 목록으로 확인하러 간다 — 진짜 실패였다면 아래에서 걸린다.
            logger.info("destroy 응답을 못 읽었다(정상일 수 있다): %s", str(exc)[:120])
        gone = self.status(instance_id) is None
        if not gone:
            logger.error("인스턴스 %s 가 파기되지 않았다 — 과금이 계속된다", instance_id)
        return gone

    def whoami(self) -> dict:
        """계정과 **쓸 수 있는 돈**.

        ⚠ `balance` 가 아니라 `credit` 이다. 실측에서 `balance: 0, credit: 25.0` 이었다 —
        `balance` 를 읽으면 화면이 "$0 · 최대 0시간" 이 되어 멀쩡한 계정이 전부 막힌다.
        """
        d = self._raw(["show", "user"])
        if not isinstance(d, dict):
            raise RuntimeError("계정 정보를 읽지 못했습니다")
        return {
            "id": d.get("id"),
            "email": d.get("email"),
            "credit": float(d.get("credit") or 0.0),
            "balance": float(d.get("balance") or 0.0),
        }
