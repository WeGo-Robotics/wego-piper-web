"""장치 확장의 사용자 유닛 템플릿 (feature/pika.md §2) — unitd_ext 가 쓴다.

따로 둔 이유: 여기 글은 유닛 파일의 주석이다. 사람이 `systemctl --user cat piper-ext-<name>` 으로 읽고 화면에는 안
나간다 — 번역 추출(tools/i18n.py `PY_SKIP_FILES`)에서 뺀다.
"""

from __future__ import annotations

from pathlib import Path


def render(entry: dict, *, docker: str, hidraw: int | None, rules: dict, redis_sock: Path) -> str:
    """고정 템플릿. 매니페스트에서 오는 것은 이름·제목·허브·장치 어휘뿐이고 전부 검사를 지났다(unitd_ext.validate_manifest)."""
    m = entry["manifest"]
    name = m["name"]
    cname = f"piper-ext-{name}"
    data = entry["data_dir"]
    # 한 줄 = 옵션 하나(+값) — 사람이 `systemctl --user cat` 으로 읽는 파일이다
    lines: list[list[str]] = [
        [docker, "run", "--rm", "--name", cname],
        # DDS 를 밖으로 안 낸다 — 같은 LAN 의 로봇들 토픽이 섞인다. 버스는 소켓, 데이터는 shm 이다
        ["--network", "none"], ["--ipc", "host"],
        ["-v", f"{data}:/data"],
        ["-v", f"{redis_sock.parent}:{redis_sock.parent}"],
        ["-e", f"PIPER_REDIS_URL=unix://{redis_sock}?db=0"],
        ["-e", f"PIPER_EXT_NAME={name}"], ["-e", f"PIPER_EXT_IMAGE={entry['image']}"],
        ["-e", f"PIPER_EXT_RPC_HUB={m['rpc_hub']}"],
        ["--label", f"io.wego.piper.ext={name}"],
    ]
    cg: list[str] = []
    for d in m["devices"]:
        if d == "hidraw":
            if hidraw is not None:
                cg.append(f"c {hidraw}:* rmw")
        else:
            cg += rules[d]
    if m["devices"]:
        # 핫플러그 — 뒤에 꽂힌 장치 노드도 보이게 /dev 를 통째로 보이되, 열 수 있는 것은 아래 cgroup 규칙뿐이다
        lines += [["-v", "/dev:/dev"], ["-v", "/run/udev:/run/udev:ro"]]
        lines += [[f"--device-cgroup-rule={r}"] for r in cg]
    lines.append([entry["image_id"]])

    def q(a: str) -> str:
        if "%" in a or '"' in a or "\\" in a or "\n" in a:
            raise ValueError(f"유닛에 쓸 수 없는 글자가 있습니다: {a!r}")
        return f'"{a}"' if (" " in a or "*" in a) else a

    exec_start = " \\\n    ".join(" ".join(q(a) for a in line) for line in lines)
    title = m["title"].replace("\n", " ")
    return f"""# {cname} — 장치 확장 '{title}' (feature/pika.md §2).
# ⚠ unitd 가 만들었다 — 손으로 고치지 않는다. 확장을 다시 추가하거나 unitd 가 다시 뜰 때 덮어쓴다.
# 이미지: {entry['image']}

[Unit]
Description=PIPER extension: {title}
StartLimitIntervalSec=0

[Service]
Type=simple
# 지난 실행이 남긴 컨테이너(--rm 이 못 지운 것)를 치운다 — 이름이 겹치면 run 이 실패한다
ExecStartPre=-{docker} rm -f {cname}
ExecStart={exec_start}
ExecStop={docker} stop -t 10 {cname}
TimeoutStopSec=20
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""
