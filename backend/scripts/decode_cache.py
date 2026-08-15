"""데이터셋 mp4 → 프레임 이미지 디코딩 캐시.

`settings.grpc_python` 으로 별도 프로세스에서 실행된다 (cv2·pyarrow 는 그쪽 환경에 있다).
원래 datasets.py 라우터 안의 인라인 `-c` 문자열이었는데, 두 가지를 고치면서 파일로 뺐다
(feature/episode-editor.md §3):

- **멀티 chunk**: 예전 코드는 `chunk-000/file-000` 하나만 열어서, chunk 가 나뉜
  데이터셋이면 **에피소드 절반이 조용히 빠졌다**. 이제 비디오 키별 mp4 전부를
  정렬해 이어 읽는다 (piper_phase.labeler.load_frames 가 parquet 에서 겪은 같은 함정).
- **JPEG/축소 옵션**: 뷰어용. PNG 원본 해상도는 31k 프레임 × 2캠에 수 GB 가 나온다.
  `--format jpeg --max-dim 320` 이면 수백 MB 로 준다. 기본값은 기존 그대로
  PNG 원본 해상도 (LeRobot 공식 캐시 형식).

출력: `{ds}/images/{video_key}/episode-{ep:06d}/frame-{idx:06d}.{png|jpg}`
프레임 번호는 **에피소드 내 상대** 번호다.
"""

import argparse
import json
from pathlib import Path

import cv2


def episode_lengths(ds: Path) -> dict[int, int]:
    """에피소드별 프레임 수. meta/episodes(parquet 디렉토리) → episodes.jsonl 폴백."""
    lengths: dict[int, int] = {}
    ep_dir = ds / "meta" / "episodes"
    files = sorted(ep_dir.rglob("*.parquet")) if ep_dir.is_dir() else []
    if files:
        import pyarrow.parquet as pq
        for f in files:
            df = pq.read_table(f).to_pandas().reset_index()
            for _, row in df.iterrows():
                lengths[int(row["episode_index"])] = int(row["length"])
        return lengths
    jsonl = ds / "meta" / "episodes.jsonl"
    if jsonl.exists():
        for line in jsonl.read_text().splitlines():
            try:
                row = json.loads(line)
                lengths[int(row["episode_index"])] = int(row["length"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return lengths


class VideoChain:
    """비디오 키의 mp4 파일들을 정렬 순서로 이어 읽는다 — chunk 경계를 숨긴다."""

    def __init__(self, files: list[Path]):
        self.files = files
        self.idx = 0
        self.cap = cv2.VideoCapture(str(files[0])) if files else None

    def read(self):
        while self.cap is not None:
            ret, frame = self.cap.read()
            if ret:
                return frame
            self.cap.release()
            self.idx += 1
            if self.idx >= len(self.files):
                self.cap = None
                break
            self.cap = cv2.VideoCapture(str(self.files[self.idx]))
        return None

    def release(self):
        if self.cap is not None:
            self.cap.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="decode dataset videos to frame cache")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--format", choices=("png", "jpeg"), default="png")
    parser.add_argument("--max-dim", type=int, default=0, help="긴 변 축소 (0=원본)")
    parser.add_argument("--quality", type=int, default=85, help="JPEG 품질")
    args = parser.parse_args()

    ds = args.dataset
    ext = "jpg" if args.format == "jpeg" else "png"
    imwrite_params = [cv2.IMWRITE_JPEG_QUALITY, args.quality] if ext == "jpg" else []

    info = json.loads((ds / "meta" / "info.json").read_text())
    n_eps = info.get("total_episodes", 0)
    vid_keys = [k for k in info.get("features", {}) if k.startswith("observation.images.")]
    lengths = episode_lengths(ds)
    print(f"Dataset: {n_eps} episodes, {len(vid_keys)} cameras: {vid_keys}, "
          f"format={ext} max_dim={args.max_dim}", flush=True)

    for vk in vid_keys:
        files = sorted((ds / "videos" / vk).rglob("*.mp4"))
        if not files:
            print(f"  SKIP {vk}: no mp4 under videos/{vk}", flush=True)
            continue
        # 요청한 포맷의 캐시가 이미 있으면 스킵 (다른 포맷과는 공존한다)
        probe = ds / "images" / vk / "episode-000000" / f"frame-000000.{ext}"
        if probe.exists():
            print(f"  SKIP {vk}: {ext} cache exists", flush=True)
            continue

        chain = VideoChain(files)
        print(f"  {vk}: {len(files)} file(s)", flush=True)
        written = 0
        for ep_idx in range(n_eps):
            ep_len = lengths.get(ep_idx, 0)
            if ep_len == 0:
                continue
            out_dir = ds / "images" / vk / f"episode-{ep_idx:06d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            for fi in range(ep_len):
                frame = chain.read()
                if frame is None:
                    print(f"  WARN {vk}: video ended at episode {ep_idx} frame {fi} "
                          f"(meta says {ep_len})", flush=True)
                    break
                if args.max_dim > 0:
                    h, w = frame.shape[:2]
                    long_side = max(h, w)
                    if long_side > args.max_dim:
                        scale = args.max_dim / long_side
                        frame = cv2.resize(frame, (round(w * scale), round(h * scale)),
                                           interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(out_dir / f"frame-{fi:06d}.{ext}"), frame, imwrite_params)
                written += 1
            if (ep_idx + 1) % 10 == 0 or ep_idx == n_eps - 1:
                print(f"    {vk}: episode {ep_idx + 1}/{n_eps} ({written} frames)", flush=True)
        chain.release()
    print("Decode cache complete", flush=True)


if __name__ == "__main__":
    main()
