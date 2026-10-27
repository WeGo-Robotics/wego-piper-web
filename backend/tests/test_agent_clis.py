"""로컬 AI CLI 감지·선택·레시피 (feature/agent-cli.md §4-7) — 표 하나를 게이트웨이와 unitd 가 같이 쓴다."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import agent_link as AL
from piper_bus import agent_clis as A

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    A.reset_cache()
    AL._pref_path().unlink(missing_ok=True)
    yield
    A.reset_cache()
    AL._pref_path().unlink(missing_ok=True)


def test_detect_finds_binaries_on_the_path_and_reads_versions(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\necho '2.1.307 (Claude Code)'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))          # 사용자 디렉터리 후보가 전부 비게
    items = A.detect(ttl=0)
    assert [i["id"] for i in items] == A.known_ids()
    by = {i["id"]: i for i in items}
    assert by["claude"]["installed"] and by["claude"]["path"] == str(fake) and by["claude"]["version"] == "2.1.307 (Claude Code)"
    assert not by["codex"]["installed"] and by["codex"]["path"] is None and by["codex"]["version"] is None
    assert A.default_preferred(items) == "claude"
    assert A.default_preferred([{**i, "installed": i["id"] == "gemini"} for i in items]) == "gemini"
    assert A.default_preferred([{**i, "installed": False} for i in items]) == "claude"
    # 유닛의 좁은 PATH 밖(~/.local/bin)도 본다
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "codex").write_text("#!/bin/sh\necho codex-cli 0.9.0\n")
    (local / "codex").chmod(0o755)
    assert {i["id"]: i["installed"] for i in A.detect(ttl=0)}["codex"] is True


def test_recipes_carry_placeholders_and_never_a_token_value():
    r = A.recipes()
    assert set(r) == set(A.known_ids())
    for cid, rec in r.items():
        text = json.dumps(rec, ensure_ascii=False)
        assert "{question}" in rec["ask"] and "{origin}" in rec["ask"]
        if rec["mcp"] is None:
            assert cid == "aider"
        else:
            assert "{origin}" in rec["mcp"]["config"] and rec["mcp"]["config_path"]
            assert "<TOKEN>" in text or "${PIPER_API_TOKEN" in text
    # unitd 도 같은 표로 호스트를 본다 — 컨테이너 게이트웨이가 호스트 CLI 를 못 보기 때문
    assert "agent_clis" in (REPO / "daemons" / "unitd.py").read_text()


def test_agent_link_merges_gateway_and_host_and_remembers_the_choice(monkeypatch):
    from app.services import units

    def fake_detect(ttl=60.0):
        return [{"id": k["id"], "name": k["name"], "vendor": k["vendor"], "bin": k["bin"],
                 "installed": k["id"] == "claude", "path": "/x/claude" if k["id"] == "claude" else None,
                 "version": "2.1" if k["id"] == "claude" else None} for k in A.KNOWN]

    class Bus:
        def rpc_call(self, unit, method, args, timeout=10):
            return {"versions": {}, "clis": [{"id": "codex", "name": "Codex CLI", "vendor": "OpenAI", "bin": "codex",
                                              "installed": True, "path": "/host/codex", "version": "0.9"}]}

    monkeypatch.setattr(A, "detect", fake_detect)
    monkeypatch.setattr(units, "unitd_available", lambda: True)
    monkeypatch.setattr(units, "_bus", lambda: Bus())
    client = TestClient(app)
    body = client.get("/api/system/agent-link").json()
    by = {c["id"]: c for c in body["clis"]}
    assert by["claude"]["where"] == "gateway" and by["codex"]["where"] == "host" and by["codex"]["version"] == "0.9"
    assert by["gemini"]["installed"] is False and by["gemini"]["where"] is None
    assert body["preferred"] == "claude" and body["preferred_saved"] is False and body["host_supported"] is True
    assert body["recipes"]["codex"]["mcp"]["config_path"] and body["recipes"]["aider"]["mcp"] is None
    assert client.put("/api/system/agent-link", json={"preferred": "codex"}).status_code == 200
    body = client.get("/api/system/agent-link").json()
    assert body["preferred"] == "codex" and body["preferred_saved"] is True
    assert client.put("/api/system/agent-link", json={"preferred": "skynet"}).status_code == 400
    # 옛 unitd(clis 를 모른다)면 호스트 쪽은 "모른다" 로 — 지어내지 않는다
    monkeypatch.setattr(units, "_bus", lambda: type("B", (), {"rpc_call": lambda *a, **k: {"versions": {}}})())
    body = client.get("/api/system/agent-link").json()
    assert body["host_supported"] is False and {c["id"]: c["installed"] for c in body["clis"]}["codex"] is False
