# 에피소드 뷰어 트러블슈팅 — 영상이 안 보일 때

뷰어는 **동영상**이 기본이고 프레임 캐시는 폴백이다([episode-editor.md](../feature/episode-editor.md) §3).
그래서 "영상이 안 보인다" 는 서로 다른 세 가지일 수 있다.

## 0. 어느 쪽이 안 되는지부터 가른다

| 화면 | 지금 무엇을 보고 있나 |
|---|---|
| 영상이 나오고 재생된다 | 동영상 모드 — 정상 |
| **"브라우저가 이 비디오를 재생하지 못했습니다"** 배너 | 동영상 실패 → 프레임 캐시로 넘어갔다 (§2) |
| **"디코딩 캐시가 없습니다"** 배너 | 프레임 캐시 모드인데 캐시가 없다 (§3) |
| 아무것도 없다 | 에피소드를 안 골랐거나 `videoMeta` 가 없다 (§1) |

## 1. 서버 쪽은 이렇게 한 번에 가른다

```bash
D=wego-mink/sim_two_box_3_120     # 데이터셋 id

# ① 영상이 서빙되나 — 200 과 파일 크기가 나와야 한다
curl -s -o /dev/null -w "%{http_code} %{size_download}B %{content_type}\n" \
  "http://<호스트>/api/datasets/$D/videos/cam0/0/0"

# ② 브라우저가 쓰는 Range 도 되나 — 206 이어야 한다
curl -s -D- -o /dev/null -H "Range: bytes=0-1023" \
  "http://<호스트>/api/datasets/$D/videos/cam0/0/0" | head -3

# ③ 에피소드별 진입 시각이 메타에 있나 — 없으면 뷰어가 프레임 캐시로 떨어진다
curl -s "http://<호스트>/api/datasets/$D" \
  | python3 -c "import sys,json; e=json.load(sys.stdin)['episodes'][0]; \
    print([k for k in e if k.startswith('videos/')])"
```

③ 이 비어 있으면 **구버전 메타**다 — 뷰어는 `videos/<키>/from_timestamp` 가 있어야
동영상 모드로 간다. 그때는 프레임 캐시(§3)가 유일한 길이다.

⚠ **v3.0 레이아웃은 지원한다.** `videos/{video_key}/chunk-{c}/file-{f}.mp4` 로, 한 파일에
에피소드 여럿이 들어 있고 진입은 `from_timestamp` 로 한다. 서빙 경로는 `meta/info.json`
의 `video_path` 를 그대로 읽으므로 v2.1·v3.0 둘 다 된다.

## 2. "재생하지 못했습니다" 배너 — **코덱이라고 단정하지 마라**

배너는 브라우저가 돌려준 코드(`DECODE` / `SRC_NOT_SUPPORTED`)와 메시지를 그대로
보여 준다. 그 둘은 **코덱 말고도** 뜬다 — 그리고 실기에서 실제로 난 원인은 코덱이
아니었다.

### 2-1. ⚠⚠ 허브에서 받은 데이터셋이 통째로 404 였다 (2026-09-23)

증상은 `SRC_NOT_SUPPORTED` · `MEDIA_ELEMENT_ERROR: Format error` 였다. 코덱처럼 보이지만
**브라우저가 mp4 대신 404 JSON 을 받아서** 그걸 미디어로 파싱하려다 난 것이다.

원인은 비디오 서빙의 경로 가드였다:

```python
path = (ds_path / template.format(...)).resolve()          # ← 심볼릭을 따라간다
if not str(path).startswith(str(ds_path.resolve())): 404   # ← 그래서 항상 밖으로 나간다
```

HF 캐시는 실체를 `blobs/` 에 두고 스냅샷은 **링크**다:

```
snapshots/<hash>/videos/…/file-000.mp4  →  ../../../../../blobs/<sha>
```

`.resolve()` 하는 순간 `blobs/` 로 나가므로 `startswith` 가 항상 거짓 → **허브에서 받은
모든 데이터셋의 모든 영상이 404.** 막아야 했던 것은 템플릿 안의 `..` 이지 데이터셋 안의
심볼릭이 아니다. 지금은 `os.path.normpath` 로 **심볼릭을 따라가기 전에** 판정한다 —
`..` 은 여전히 404 다(테스트가 둘을 같이 못 박는다).

⚠ **같은 데이터셋이라도 자리에 따라 증상이 갈린다.** 이걸로 진단이 한참 헤맸다:

| 자리 | 파일 모양 | 영상 |
|---|---|---|
| `~/.cache/huggingface/hub/datasets--…/snapshots/` (허브 다운로드) | blobs 로의 **심볼릭** | ☒ 404 |
| `/srv/piper-data/hf/lerobot/<org>/<이름>` (배포 호스트에 직접 놓은 것) | **실파일** | ☑ 정상 |

그래서 배포 호스트(.120)에서는 같은 데이터셋이 멀쩡히 서빙됐고, 개발 기계에서만 안 됐다.
**"어느 게이트웨이를 보고 있나" 를 먼저 확인한다** — 브라우저 주소창이 답이다
(`:5173` 은 개발 서버, `:80` 은 배포본).

### 2-2. 받다 만 데이터셋

같은 건에서 **cam0 의 mp4 자체가 없었다.** 다운로드가 중간에 끊긴 것이다:

```bash
DS=~/.cache/huggingface/hub/datasets--<org>--<이름>
ls -l $DS/snapshots/*/videos/*/chunk-000/      # 비어 있으면 그 캠은 안 받아졌다
ls    $DS/blobs/*.incomplete                   # 끊긴 흔적 (0 bytes 면 시작도 못 함)
```

빠진 파일만 다시 받는다:

```python
from huggingface_hub import hf_hub_download
hf_hub_download("<org>/<이름>", repo_type="dataset",
                filename="videos/observation.images.cam0/chunk-000/file-000.mp4")
```

⚠ `.incomplete` 가 남아 있어도 스캐너는 **에피소드 수·프레임 수를 정상으로 보고한다** —
그건 `meta/` 에서 읽기 때문이다. 목록이 멀쩡하다고 영상이 다 온 것은 아니다.

### 2-3. 그래도 코드 3·4 가 뜨면

이제 배너가 브라우저의 코드와 메시지를 그대로 옮기고, 콘솔에 URL 까지 찍는다:

```
[episodes] video error cam0 /api/datasets/…/videos/cam0/0/0 MediaError {code: 4, …}
```

그 URL 을 `curl` 로 직접 쳐 본다 — **200 이 아니면 코덱 문제가 아니다.**

## 3. "디코딩 캐시가 없습니다"

받은(또는 새로 만든) 데이터셋에는 프레임 캐시가 없다. **정상이다** — 동영상 모드는
캐시 없이 되고, 캐시는 프레임 단위 편집·폴백용이다.

버튼을 누르면 백그라운드 작업이 시작되고 토스트가 한 번 뜬다. ⚠ **진행률은 없다.**
13000 프레임 × 2캠이면 수 분 걸리고 그동안 화면은 조용하다 — "아무 반응이 없다" 로
읽히기 쉽다. 정말 도는지는 이렇게 본다:

```bash
docker exec piper-web-backend ps aux | grep decode_cache
du -sh /srv/piper-data/hf/lerobot/<org>/<이름>/images
```

### ⚠⚠ API 를 **빈 본문으로** 부르면 원본 해상도 PNG 가 쌓인다

화면은 `{format:'jpeg', max_dim:320}` 을 보내지만, 서버 기본값은 **PNG·원본 해상도**다
(LeRobot 공식 캐시 형식). `curl -d '{}'` 로 부르면 그 기본값이 걸린다 — 실측으로 19MB
데이터셋 하나가 **1분 만에 2.2GB** 를 썼고, 멈추지 않았으면 디스크를 채웠을 것이다
(그 기계는 이미 93% 였다).

```bash
# 손으로 부를 때는 화면과 같은 값을 명시한다
curl -X POST "http://<호스트>/api/datasets/$D/decode-cache" \
  -H 'Content-Type: application/json' -d '{"format":"jpeg","max_dim":320}'

# 지우기 (파일 수를 돌려준다)
curl -X POST "http://<호스트>/api/datasets/$D/decode-cache/delete"
```

## 4. 받은 데이터셋으로 학습이 안 될 때

증상은 `FileNotFoundError: …/huggingface/lerobot/<org>/<이름>/meta/info.json` 이고, 그
뒤에 엉뚱한 예외가 겹쳐 진짜 사유를 가린다:

```
RevisionNotFoundError: Your dataset must be tagged with a codebase version
TypeError: HfHubHTTPError.__init__() missing 1 required keyword-only argument: 'response'
```

⚠ **`TypeError` 를 쫓지 마라.** lerobot 이 예외를 만들다 난 2차 오류다. 진짜 사유는 첫
줄 — `meta/info.json` 을 **엉뚱한 자리에서** 찾았다는 것이다.

### 자리가 둘이다

| 누가 | 어디 |
|---|---|
| lerobot 이 보는 곳 | `HF_LEROBOT_HOME/<repo_id>` = `~/.cache/huggingface/lerobot/<org>/<이름>` |
| 우리가 받는 곳 | HF 허브 캐시 = `~/.cache/huggingface/hub/datasets--<org>--<이름>/snapshots/<hash>` |

못 찾으면 lerobot 은 허브에 **코드베이스 태그**(`v3.0`)를 물으러 간다. `main` 브랜치만
있고 태그가 없는 저장소에서는 거기서 죽는다 — 파일은 멀쩡히 받아 놨는데도.

### 처방 — `--dataset.root`

`root` 에 `meta/info.json` 이 있으면 lerobot 은 **허브를 아예 안 탄다**(실측).
로컬 학습은 이제 게이트웨이가 알아서 붙인다:

```
--dataset.repo_id=wego-mink/sim_two_box_3_120
--dataset.root=/home/…/hub/datasets--wego-mink--sim_two_box_3_120/snapshots/5cb955…
```

⚠ **원격(임대 GPU) 학습에는 안 붙인다** — 그 경로가 거기 없고, 이미지가 제 손으로
받는다. 대신 원격에서는 같은 이유로 **태그 없는 저장소가 막힐 수 있다.** 그때는 저장소에
`v3.0` 태그를 달거나, 데이터셋을 기계로 직접 올린다.

손으로 확인:

```bash
curl -s -X POST localhost:8000/api/training/preview -H 'Content-Type: application/json' \
  -d '{"dataset_repo_id":"<org>/<이름>","policy_type":"act"}' \
  | python3 -c "import sys,json;print(' '.join(json.load(sys.stdin)['args']))" | tr ' ' '\n' | grep dataset
```

## 5. 관련 문서

- [episode-editor.md](../feature/episode-editor.md) — 뷰어 설계, 동영상/프레임 두 모드
- [install-troubleshooting.md](install-troubleshooting.md) · [camera-troubleshooting.md](camera-troubleshooting.md)
