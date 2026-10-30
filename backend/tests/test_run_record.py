"""추론 실행 기록 — wrapper/run_record.py 와 두 래퍼·로그 화면 배선.

CSV 에는 무엇을 돌렸는지가 없었다. 2026-10-28 세 실행(SmolVLA·ACT-DINOv3·ACT)을 추론 시간과
task 문구로 거꾸로 짐작해야 했다. 여기 테스트는

1. 기록이 체크포인트·시작 조건·도중 변경·끝을 남기는가 — 추론을 세우지 않으면서
2. 두 래퍼가 리스너보다 **먼저** 열고 같은 CSV 이름을 쓰는가 — 로딩 중 쌓인 값이 빠지지 않게
3. 로그 화면이 기록을 보여 주고, CSV 를 지우면 같이 지우는가
"""

import asyncio
import json
import re
import sys
from pathlib import Path

from conftest import code_only

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
from run_record import RunRecord, settings  # noqa: E402


# ── 1. 기록 ──────────────────────────────────────────────────────────────

def test_the_record_sits_next_to_the_csv_and_tells_what_ran(tmp_path):
    t = iter([100.0, 101.5])
    rec = RunRecord(str(tmp_path / "piper_inference_20261028_213201.csv"), clock=lambda: next(t),
                    policy_type="act", checkpoint="/x/16-40-58_act/checkpoints/100000/pretrained_model",
                    args={"fps": 15, "config_overrides": {"n_action_steps": 50}})
    doc = json.loads((tmp_path / "piper_inference_20261028_213201.json").read_text())
    assert doc["csv"] == "piper_inference_20261028_213201.csv"
    assert doc["policy_type"] == "act" and doc["args"]["config_overrides"] == {"n_action_steps": 50}
    assert doc["changes"] == [] and "started" in doc and "ended" not in doc

    rec.update(actions_per_chunk=50)
    rec.change("max_velocity", 90)
    rec.change("manual_action", {"joint1.pos": 1.0})   # 조작 입력은 조건이 아니다
    rec.change("task", "pick the apple")
    rec.finish(1032)

    doc = json.loads(rec.path.read_text())
    assert doc["actions_per_chunk"] == 50
    # t 는 CSV timestamp 와 같은 시계 — 건네받은 clock 을 쓴다
    assert doc["changes"] == [{"t": 100.0, "key": "max_velocity", "value": 90},
                              {"t": 101.5, "key": "task", "value": "pick the apple"}]
    assert doc["steps"] == 1032 and "ended" in doc
    assert not list(tmp_path.glob("*.tmp"))


def test_a_disk_error_does_not_stop_inference(tmp_path):
    rec = RunRecord(str(tmp_path / "gone" / "piper_inference_x.csv"), policy_type="act")
    rec.change("max_velocity", 90)
    rec.finish(3)   # 예외 없이 지나가야 한다


def test_settings_are_the_public_attributes_only():
    class F:
        def __init__(self):
            self.lowpass_alpha, self.max_velocity, self._prev_sent = 0.5, 180.0, {"a": 1}
    assert settings(F()) == {"lowpass_alpha": 0.5, "max_velocity": 180.0}


# ── 2. 래퍼 배선 ──────────────────────────────────────────────────────────

def _main_and_listener(name: str) -> tuple[str, str]:
    src = code_only((_REPO / "wrapper" / name).read_text())
    listener = src[src.index("def param_listener"):]
    listener = listener[:listener.index("\ndef ", 1)]
    return src[src.index("def main"):], listener


def test_both_wrappers_open_the_record_before_the_listener_and_close_it():
    for name in ("lerobot_wrapper.py", "grpc_wrapper.py"):
        main, listener = _main_and_listener(name)
        # 리스너는 큐에 쌓인 값을 시작하자마자 꺼낸다 — 기록이 그 뒤에 열리면 시작값이 빠진다
        assert main.index("RunRecord(") < main.index("target=param_listener"), name
        assert "_run_record.change(key, value)" in listener, name
        assert "_run_record.finish(step)" in main, name
        # 기록과 CSV 가 같은 이름이어야 짝이 맞는다 — CSV 경로는 한 번만 정한다
        assert len(re.findall(r"_csv_path = ", main)) == 1, name
        assert re.search(r"RunRecord\(\s*_csv_path", main).start() < main.index("open(_csv_path"), name


def test_the_record_clock_matches_each_csv_timestamp():
    lw, _ = _main_and_listener("lerobot_wrapper.py")
    gw, _ = _main_and_listener("grpc_wrapper.py")
    assert '"timestamp": time.monotonic()' in lw and "clock=" not in lw[lw.index("RunRecord("):lw.index("Run record")]
    assert '"timestamp": f"{time.time()' in gw and "clock=time.time" in gw


# ── 3. 로그 화면 ──────────────────────────────────────────────────────────

def _logs(monkeypatch, tmp_path):
    from app.routers import logs
    monkeypatch.setattr(logs, "LOG_CATEGORIES",
                        {k: {**v, "dir": tmp_path} for k, v in logs.LOG_CATEGORIES.items()})
    return logs


def test_the_record_is_listed_and_names_the_chart(monkeypatch, tmp_path):
    logs = _logs(monkeypatch, tmp_path)
    csv = tmp_path / "piper_inference_20261028_213037.csv"
    csv.write_text("timestamp\n")
    assert logs._run_label(csv) == ""   # 기록이 없는 옛 CSV
    RunRecord(str(csv), policy_type="act_dinov3",
              checkpoint="/home/u/outputs/train/2026-10-28/14-47-00_act_dinov3/checkpoints/100000/pretrained_model")
    assert logs._run_label(csv) == " · act_dinov3 14-47-00_act_dinov3/100000"
    listed = asyncio.run(logs.list_logs(category="inference_run"))
    assert [f["name"] for f in listed] == ["piper_inference_20261028_213037.json"]


def test_deleting_the_csv_takes_its_record_and_chart_but_not_the_other_way(monkeypatch, tmp_path):
    logs = _logs(monkeypatch, tmp_path)
    for stem in ("piper_inference_a", "piper_inference_b"):
        for sfx in (".csv", ".html", ".json"):
            (tmp_path / f"{stem}{sfx}").write_text("x")

    asyncio.run(logs.delete_log("piper_inference_a.csv"))
    assert not list(tmp_path.glob("piper_inference_a.*"))

    asyncio.run(logs.delete_log("piper_inference_b.json"))
    assert sorted(p.name for p in tmp_path.glob("piper_inference_b.*")) == [
        "piper_inference_b.csv", "piper_inference_b.html"]
