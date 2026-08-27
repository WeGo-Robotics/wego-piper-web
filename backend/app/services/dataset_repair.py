"""끊긴 녹화가 남긴 **반쪽 데이터셋**을 진단하고 색인을 되살린다.

LeRobot 은 에피소드 메타를 **10개씩 모아서** parquet 에 쓴다
(`LeRobotDatasetMetadata.metadata_buffer_size = 10`, 주석에 "in batches for
efficiency"). 반면 `info.json` 의 `total_episodes` 는 **에피소드마다** 쓴다.

정상 종료면 `_close_writer()` 가 버퍼를 비우고 끝나지만, SIGKILL 이면 그 호출이
없다. 그래서 끊긴 순간 버퍼에 있던 **최대 9개가 목록에서 증발한다.**

화면 증상이 정확히 이것이었다: **개수는 뜨는데 각 에피소드에 못 들어간다.**
개수는 `info.json` 에서, 목록은 parquet 에서 오기 때문이다.

⚠ 프레임 자체는 대개 살아 있다 — `data/` parquet 과 mp4 는 다른 경로로 쓰인다.
   그래서 **색인만 다시 지으면 전부 되살아난다.** 어제 실제로는 복구 가능한
   데이터셋을 지우고 다시 찍었다.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_EPISODES_REL = "meta/episodes"
_DATA_REL = "data"


def _info(ds_path: Path) -> dict:
    p = ds_path / "meta" / "info.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def _episode_files(ds_path: Path) -> list[Path]:
    return sorted((ds_path / _EPISODES_REL).rglob("*.parquet"))


def _data_files(ds_path: Path) -> list[Path]:
    return sorted((ds_path / _DATA_REL).rglob("*.parquet"))


def check(ds_path: Path) -> dict[str, Any]:
    """세 곳이 같은 이야기를 하는지 본다.

    `info.json` 의 개수 · `meta/episodes` 행수 · `data` 의 고유 에피소드 번호.
    끊긴 녹화는 첫째만 크고 나머지가 작다.

    ⚠ 조용히 반쪽만 보여주는 것이 제일 나쁘다 — 사용자는 데이터가 없어진 줄 알고
      지운다. 그래서 화면이 읽을 수 있게 숫자를 그대로 돌려준다.
    """
    import pandas as pd

    info = _info(ds_path)
    declared = int(info.get("total_episodes") or 0)

    indexed: set[int] = set()
    for f in _episode_files(ds_path):
        try:
            indexed |= set(pd.read_parquet(f, columns=["episode_index"])["episode_index"].tolist())
        except Exception as exc:
            logger.warning("에피소드 색인 읽기 실패 (%s): %s", f, exc)

    stored: set[int] = set()
    for f in _data_files(ds_path):
        try:
            stored |= set(pd.read_parquet(f, columns=["episode_index"])["episode_index"].tolist())
        except Exception as exc:
            logger.warning("데이터 읽기 실패 (%s): %s", f, exc)

    # 색인에 없는데 프레임은 있는 것 = 되살릴 수 있는 것
    recoverable = sorted(stored - indexed)
    return {
        "ok": not recoverable and declared == len(indexed),
        "declared_episodes": declared,      # info.json — 화면이 개수로 보여주는 값
        "indexed_episodes": len(indexed),   # meta/episodes — 목록에 실제로 보이는 것
        "stored_episodes": len(stored),     # data — 프레임이 남아 있는 것
        "recoverable": recoverable,
        # 프레임조차 없으면 되살릴 수 없다. 숫자만 큰 경우다.
        "unrecoverable": max(0, declared - len(stored)),
    }


def _video_keys(info: dict) -> list[str]:
    return [k for k, v in (info.get("features") or {}).items()
            if isinstance(v, dict) and v.get("dtype") == "video"]


def rebuild_index(ds_path: Path, *, dry_run: bool = True) -> dict[str, Any]:
    """`data/` 에 남아 있는 프레임으로 `meta/episodes` 를 다시 짓는다.

    유도하는 값과 근거:

      length              해당 에피소드의 행 수
      dataset_from/to     파일 안에서의 행 범위
      tasks               `task_index` → `tasks` 표
      videos/*/timestamp  **앞 에피소드의 끝에서 이어진다.** 영상은 한 파일에
                          연속으로 붙으므로 `to = from + length/fps` 다.
                          실측 검증: ep0 length=311, fps=15 → 20.7333s,
                          메타의 `to_timestamp` 와 소수점까지 일치했다.

    ⚠ **있는 행은 절대 안 건드린다.** 빠진 것만 채운다 — 정상 부분을 다시 쓰다가
      틀리면 멀쩡한 데이터셋까지 잃는다.
    ⚠ 쓰기 전에 원본을 `.bak` 으로 남긴다. 되살리는 도구가 지우는 도구가 되면 안 된다.
    """
    import pandas as pd

    info = _info(ds_path)
    fps = info.get("fps")
    if not fps:
        return {"ok": False, "error": "info.json 에 fps 가 없습니다 — 타임스탬프를 못 만듭니다"}

    state = check(ds_path)
    missing = state["recoverable"]
    if not missing:
        return {"ok": True, "restored": [], "note": "되살릴 것이 없습니다", **state}

    ep_files = _episode_files(ds_path)
    if not ep_files:
        return {"ok": False, "error": "meta/episodes 파일이 아예 없습니다 — 지원 범위 밖입니다"}
    target = ep_files[-1]
    existing = pd.read_parquet(target)

    data_files = _data_files(ds_path)
    if not data_files:
        return {"ok": False, "error": "data parquet 이 없습니다 — 프레임이 남아 있지 않습니다"}

    # task_index → 문자열
    tasks_map: dict[int, str] = {}
    tp = ds_path / "meta" / "tasks.parquet"
    if tp.is_file():
        t = pd.read_parquet(tp)
        col = "task" if "task" in t.columns else t.columns[0]
        tasks_map = {i: str(v) for i, v in enumerate(t[col].tolist())}

    vkeys = _video_keys(info)
    rows: list[dict] = []

    for path in data_files:
        df = pd.read_parquet(path)
        if "episode_index" not in df.columns:
            continue
        chunk_idx, file_idx = _indices_from(path)
        for ep in sorted(set(df["episode_index"].tolist()) & set(missing)):
            sel = df.index[df["episode_index"] == ep]
            row: dict[str, Any] = {
                "episode_index": int(ep),
                "length": int(len(sel)),
                "data/chunk_index": chunk_idx,
                "data/file_index": file_idx,
                "dataset_from_index": int(sel.min()),
                "dataset_to_index": int(sel.max()) + 1,
            }
            ti = df.loc[sel, "task_index"].iloc[0] if "task_index" in df.columns else None
            row["tasks"] = [tasks_map.get(int(ti), "")] if ti is not None else []
            rows.append(row)

    if not rows:
        return {"ok": False, "error": "프레임에서 에피소드를 못 읽었습니다", **state}

    rows.sort(key=lambda r: r["episode_index"])
    _fill_timestamps(rows, existing, vkeys, float(fps))

    if dry_run:
        return {"ok": True, "dry_run": True,
                "restored": [r["episode_index"] for r in rows], **state}

    merged = pd.concat([existing, pd.DataFrame(rows)], ignore_index=True)
    merged = merged.sort_values("episode_index").reset_index(drop=True)
    backup = target.with_suffix(target.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(target, backup)
    merged.to_parquet(target, index=False)
    logger.warning("에피소드 색인 복구: %s — %d개 (%s)",
                   ds_path.name, len(rows), backup.name)
    return {"ok": True, "restored": [r["episode_index"] for r in rows],
            "backup": str(backup), **check(ds_path)}


def _indices_from(path: Path) -> tuple[int, int]:
    """`.../chunk-000/file-002.parquet` → (0, 2). 규약이 다르면 0 으로 둔다."""
    def num(name: str, prefix: str) -> int:
        try:
            return int(name.split(prefix, 1)[1].split(".")[0])
        except Exception:
            return 0
    return num(path.parent.name, "chunk-"), num(path.name, "file-")


def _fill_timestamps(rows: list[dict], existing, vkeys: list[str], fps: float) -> None:
    """영상 타임스탬프는 **앞 에피소드의 끝에서 이어붙인다.**

    같은 영상 파일 안에서 에피소드가 연속하므로, 직전까지의 길이 합이 곧 시작
    시각이다. 기존 행이 있으면 거기서 이어받아 **이미 맞는 값과 어긋나지 않게** 한다.
    """
    for key in vkeys:
        cursor = 0.0
        chunk = file = 0
        if len(existing) and f"videos/{key}/to_timestamp" in existing.columns:
            last = existing.sort_values("episode_index").iloc[-1]
            cursor = float(last[f"videos/{key}/to_timestamp"])
            chunk = int(last.get(f"videos/{key}/chunk_index", 0) or 0)
            file = int(last.get(f"videos/{key}/file_index", 0) or 0)
        for r in rows:
            r[f"videos/{key}/chunk_index"] = chunk
            r[f"videos/{key}/file_index"] = file
            r[f"videos/{key}/from_timestamp"] = cursor
            cursor += r["length"] / fps
            r[f"videos/{key}/to_timestamp"] = cursor
