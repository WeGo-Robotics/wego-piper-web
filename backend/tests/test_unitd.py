"""서비스 켜기/끄기·부팅 시 시작 (feature/services.md).

컨테이너 게이트웨이는 systemctl 이 없다 — 켜고 끄는 손은 호스트의 `piper-unitd` 다.
여기서 지키는 것: 카탈로그가 한 곳(계약)이고 유닛 파일과 어긋나지 않는다, estopd 는
읽기 전용이다, unitd 는 자기 자신을 못 끈다, 설치는 "깔되 켜지 않는다"를 지킨다,
배포 번들이 so101·sim wheel 을 싣는다.
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _unitd():
    spec = importlib.util.spec_from_file_location("unitd", REPO / "daemons" / "unitd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_unit_file_is_in_the_catalog_and_the_catalog_has_a_unit_file():
    """카탈로그(`UNIT_CATALOG`)는 화면 [서비스] 가 그리는 표이자 unitd 의 허용 목록이다.
    유닛 파일을 만들고 여기 안 넣으면 화면에 안 보이고 못 켠다. 반대로 카탈로그에만
    있고 유닛 파일이 없으면 "설치 안 됨"이 영원히 뜬다. 게이트웨이·프론트는 화면
    자체라 예외다."""
    from piper_bus import contract as C
    files = {p.name.removeprefix("piper-").removesuffix(".service")
             for p in (REPO / "deploy" / "systemd").glob("piper-*.service")}
    files -= {"gateway", "frontend"}
    assert files == set(C.UNIT_CATALOG), f"유닛 파일 ↔ 카탈로그 불일치: {files ^ set(C.UNIT_CATALOG)}"
    assert C.ESTOPD in C.UNIT_READONLY and C.UNIT_SELF == C.UNITD
    assert C.UNITD in C.DAEMON_SOURCES, "unitd 의 낡은 코드 감시가 안 붙는다"
    for d in (C.SIMD, C.SO101D):
        assert C.UNIT_CATALOG[d][1] == "optional", f"{d} 는 선택 데몬이어야 한다"


def test_unitd_touches_only_our_units_and_never_the_estop_or_itself():
    """⚠ 안전장치에 원격 종료 경로를 다는 것은 별개의 결정이다 — estopd 는 상태만.
    unitd 가 자신을 끄면 화면의 켜기/끄기가 같이 죽는다. 남의 유닛은 이름을 알아도
    못 만진다."""
    u = _unitd()
    assert u.check_action("simd", "stop") == "piper-simd.service"
    assert u.check_action("piper-so101d", "enable") == "piper-so101d.service"
    assert u.check_action("piper-robotd.service", "restart") == "piper-robotd.service"
    with pytest.raises(ValueError, match="안전장치"):
        u.check_action("estopd", "stop")
    with pytest.raises(ValueError, match="안전장치"):
        u.check_action("estopd", "restart")
    with pytest.raises(ValueError, match="기능 자체"):
        u.check_action("unitd", "stop")
    assert u.check_action("unitd", "enable") == "piper-unitd.service"
    for bad in ("nginx", "piper-../x", "sshd.service"):
        with pytest.raises(ValueError, match="우리 유닛"):
            u.check_action(bad, "start")
    with pytest.raises(ValueError, match="모르는 동작"):
        u.check_action("simd", "mask")


def test_the_gateway_reads_the_same_catalog_and_routes_control_through_unitd():
    """게이트웨이의 목록·소스 표가 계약에서 나온다 — 따로 적으면 so101d·simd 를
    빠뜨려 "모르는 유닛"이 된다(실제로 그랬다). 켜기/끄기는 unitd 를 거치고, 끄기·
    재시작만 활동 게이트를 건다 — 켜기와 부팅 시 시작은 지금 도는 것에 영향이 없다."""
    src = (REPO / "backend" / "app" / "services" / "units.py").read_text()
    assert "C.DAEMON_SOURCES.items()" in src and "C.UNIT_CATALOG.items()" in src
    assert 'rpc_call(C.UNITD, "control"' in src and 'rpc_call(C.UNITD, "list"' in src
    router = (REPO / "backend" / "app" / "routers" / "system.py").read_text()
    ctl = router.split("async def control_service", 1)[1].split("\n@router", 1)[0]
    assert 'in ("stop", "restart"):' in ctl and "require_idle" in ctl
    assert "control_unit" in ctl


def test_optional_daemons_are_installed_but_not_started_and_a_redeploy_keeps_the_choice():
    """"깔되 켜지 않는다": simd·so101d 는 유닛만 깔리고 사람이 웹에서 켠다. 재배포는
    그 선택을 지킨다 — enable 돼 있으면 재시작만, 아니면 그대로. 기본 목록에 넣어
    무조건 켜면 시뮬 데몬이 실기 로봇 호스트에서도 돈다."""
    sh = (REPO / "deploy" / "install-daemons.sh").read_text()
    assert "--optional" in sh and "is-enabled --quiet" in sh
    assert "DAEMONS=(estopd robotd camerad rsd unitd)" in sh, "unitd 가 기본 목록에 없다"
    opt = sh.split("선택 데몬:", 1)[1].split("done", 1)[0]
    assert "restart" in opt and "enable --now" not in opt, "선택 데몬을 무조건 켠다"
    for script in ("deploy/install.sh", "deploy/apply.sh"):
        calls = [ln for ln in (REPO / script).read_text().splitlines()
                 if "install-daemons.sh" in ln and not ln.strip().startswith("#")]
        assert calls and all("unitd" in ln and "--optional simd so101d" in ln for ln in calls), \
            f"{script} 가 unitd 를 안 깔거나 simd·so101d 를 켠 채로 깐다"


def test_the_release_bundle_ships_so101_and_sim_wheels_and_their_outside_deps_are_optional():
    """호스트 wheel 목록과 릴리스 변경 판정표가 같은 패키지를 봐야 한다 — 한쪽만
    고치면 wheel 이 안 실리거나 릴리스가 안 뜬다. mujoco 는 플랫폼 wheel 이라 번들에
    못 싣고 apply.sh 가 PyPI 에서 깐다 — **실패해도 핵심 데몬 설치는 끝난다.**"""
    stage = (REPO / "deploy" / "stage-hostside.sh").read_text()
    staged = set(re.search(r"for p in ([a-z0-9 ]+); do", stage).group(1).split())
    release = (REPO / "deploy" / "release.sh").read_text()
    mapped = set(re.findall(r"WHEEL_PKGS\+=\((\w+)\)", release))
    assert staged == mapped, f"wheel 목록 불일치: {staged ^ mapped}"
    assert {"so101", "sim"} <= staged
    apply = (REPO / "deploy" / "apply.sh").read_text()
    dep = apply.split("mujoco feetech-servo-sdk", 1)[1][:400]
    assert "warn" in dep and "exit" not in dep, "선택 의존 실패가 설치를 멈춘다"


def test_the_services_panel_offers_power_and_boot_toggles_without_blocking_confirms():
    src = (REPO / "frontend" / "src" / "components" / "ServicesPanel.tsx").read_text()
    assert "부팅 시 시작" in src and "'켜기'" in src and "'끄기'" in src
    assert "/system/services/control" in src
    assert "window.confirm(" not in src, "window.confirm 은 heartbeat 를 막는다"
    assert "piper-unitd" in src, "unitd 가 없을 때의 이유가 화면에 없다"
