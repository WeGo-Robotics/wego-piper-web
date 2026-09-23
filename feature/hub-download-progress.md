# 허브 다운로드 진행 상황 — 기획

> 상태: 구현됨 · 2026-09-23

「허브에서 다운로드 누르면 **다운로드 중만 뜨고** 진행 상황을 알 수 없네」

## 1. 지금 무엇이 깨져 있나 (전부 실측)

네 군데가 **각각** 깨져 있다. 하나만 고치면 화면은 그대로다.

| # | 자리 | 무엇이 |
|---|---|---|
| 1 | [`hub_client._download`](../backend/app/services/hub_client.py) | `{"status":"downloading","progress":0}` 을 넣고 **다시는 안 건드린다.** 진행률이라는 데이터가 **애초에 없다** |
| 2 | [`HubBrowser.tsx:57`](../frontend/src/components/HubBrowser.tsx#L57) | `setDownloading(...add(repoId))` 뿐 — **지우는 곳이 없다.** `/download/status` 를 아예 안 본다. 그래서 "다운로드 중..." 이 영원히 남고 버튼이 계속 잠긴다 |
| 3 | [`TrainingPage.tsx:307`](../frontend/src/pages/TrainingPage.tsx#L307) | `status !== 'running' && status !== 'started'` 로 끝을 판정하는데 서버가 주는 값은 `downloading`·`completed`·`error` 다 → **첫 폴링(2초)에서 바로 빠져나온다**. 기다리는 척만 한다 |
| 4 | `_download` 완료 판정 | `snapshot_download` 가 돌아오면 `completed`. **파일이 다 왔는지 안 본다** |

⚠ 4번은 이미 사고를 냈다(2026-09-23): `wego-mink/sim_two_box_3_120` 의 `cam0` mp4 가 안
받아졌는데(`.incomplete` 0바이트) 상태는 완료였고, 목록·에피소드 수는 정상이라 아무도
몰랐다. 증상은 **몇 주 뒤 영상이 안 나오는 것**으로 나타났다.

곁가지: `start_download(local_dir=...)` 은 인자를 받아 **쓰지 않는다**(주석이 "HF 기본
캐시" 라고 적어 뒀다). 라우터가 `settings.datasets_dir` 을 계산해 넘기지만 버려진다.

## 2. 숫자를 어디서 얻나 — 실측이 설계를 바꿨다

`snapshot_download(tqdm_class=...)` 로 가로채 봤다(huggingface_hub 1.22.0, 7파일):

```
tqdm 인스턴스: 2
  total=7  n=7   desc=Fetching 7 files            ← 파일 개수는 정확하다
  total=0  n=0   desc=Downloading (incomplete total...)   ← 바이트는 안 온다
```

**그래서 바이트를 tqdm 에서 얻으려 하면 안 된다.** 둘을 다른 데서 가져온다:

| 값 | 출처 | 왜 |
|---|---|---|
| `files_done / files_total` | `tqdm_class` 의 "Fetching N files" 카운터 | 실측으로 정확 |
| `bytes_total` | `repo_info(files_metadata=True)` 의 `size` 합 | 받기 **전에** 알 수 있다 |
| `bytes_done` | 그 repo 캐시 `blobs/` 의 파일 크기 합(`.incomplete` 포함) | 라이브러리 내부에 안 기댄다 |
| `speed` · `eta` | `bytes_done` 표본 두 개의 차 | 위에서 파생 |

⚠ **캐시 히트를 진행률로 세면 안 된다.** 이미 있는 파일은 다시 안 받으므로 바이트가 안
늘고, 그걸 "멈췄다" 로 보이면 안 된다 — `files_done` 이 같이 오르므로 둘을 같이 보여준다.

⚠ `bytes_done` 은 **줄어들 수 있다**(`.incomplete` → 최종 블롭으로 바뀌는 순간). 퍼센트는
단조 증가로 클램프한다. 안 그러면 막대가 뒤로 간다.

## 3. 완료는 **대조로** 판정한다 — 이게 이 기획의 핵심

`snapshot_download` 가 예외 없이 돌아온 것과 파일이 다 있는 것은 다르다(§1-4).

```
끝난 뒤 → Hub 파일 목록(repo_info) 과 로컬 스냅샷을 대조
         ├ 다 있다      → completed
         └ 빠진 게 있다 → incomplete + 무엇이 빠졌는지 목록
```

⚠ **끊긴 링크도 없는 것으로 친다.** HF 캐시는 스냅샷이 `blobs/` 로의 심볼릭이라, 링크만
있고 실체가 없을 수 있다 — `Path.exists()` 는 링크를 따라가므로 그대로 쓰면 된다.

`incomplete` 는 실패가 아니라 **다시 받으면 되는 상태**다. 화면은 [다시 받기] 를 준다
(`snapshot_download` 는 있는 것을 건너뛰므로 빠진 것만 받는다).

## 4. 상태 모양

```json
{
  "status": "idle|downloading|verifying|completed|incomplete|error",
  "repo_id": "wego-mink/sim_two_box_3_120", "repo_type": "dataset",
  "files_done": 7, "files_total": 9,
  "bytes_done": 12345678, "bytes_total": 19629153,
  "percent": 62.9,
  "speed_bps": 22097768, "eta_s": 4,
  "started_at": 1758600000.0,
  "path": "/home/…/snapshots/5cb955…",
  "missing": ["videos/observation.images.cam0/chunk-000/file-000.mp4"],
  "error": null
}
```

⚠ **`progress` 라는 이름을 버린다.** 지금 그 키는 늘 0 이라, 남겨 두면 옛 화면이 계속
0% 를 그린다. 이름을 바꾸면 안 고친 곳이 조용히 틀리는 대신 눈에 띈다.

## 5. 화면

| 자리 | 지금 | 후 |
|---|---|---|
| `HubBrowser` 버튼 | `다운로드 중...`(영구) | 막대 + `4/9 · 62% · 21MB/s · 4초` → 끝나면 `받음 ✓`, 불완전이면 빨간 줄 + [다시 받기] |
| `TrainingPage` 베이스 모델 | 2초 뒤 멋대로 완료 | 같은 상태를 보고 진짜 끝날 때까지 |

⚠ 폴링은 **한 곳에서** 한다 — `useHubDownload(repoId)` 훅 하나를 두 화면이 쓴다. 지금
두 곳이 각자 다른 규칙으로 끝을 판정하다 3번이 났다.

⚠ 폴링 간격은 1초, `api.ts` 의 단일비행이 이미 겹침을 막는다. 다운로드가 없으면
폴링하지 않는다(브라우저 요청 적체 전례).

## 6. 안 하는 것 · 정해야 하는 것

| | |
|---|---|
| **취소** | `snapshot_download` 에는 중단 수단이 없다. 진짜 취소는 **프로세스로 빼야** 가능하다(`decode_cache` 가 그 모양이다). 이번 범위 밖 — 다만 상태에 `pid` 자리를 비워 둔다 |
| **재시작 생존** | `_download_status` 는 메모리다. 게이트웨이가 재시작하면 돌던 다운로드의 상태가 사라진다(다운로드 자체는 스레드라 같이 죽는다). 파일로 둘지는 ☐ 결정 필요 |
| **동시 다운로드** | 같은 repo 를 두 번 누르면 스레드가 둘 뜬다. `status == downloading` 이면 거절하는 편이 맞다 |
| **`local_dir`** | 쓰지 않는 인자를 **지운다.** 남겨 두면 다음 사람이 "여기로 받는구나" 로 읽는다 |

## 7. 단계

1. **백엔드** — 총량 조회 · 샘플러 · `tqdm_class` · 완료 대조 · 상태 모양(§4).
   화면을 안 고쳐도 `GET /hub/download/status` 가 **진짜를 말하게** 된다
2. **훅** — `useHubDownload` 하나
3. **두 화면** — 막대와 결과 표시, 3번 버그 제거
4. **테스트**
   - 완료인데 파일이 빠졌으면 `incomplete` 이고 `missing` 에 이름이 있다 (§1-4 재발 방지)
   - 캐시 히트만 있어도 `files_done` 이 오른다
   - `bytes_done` 이 줄어도 퍼센트는 안 줄어든다
   - `status` 값 집합이 프론트 판정과 같다 (3번 재발 방지)

## 8. 곁가지 — 이 기획으로 같이 닫히는 것

받다 만 데이터셋을 **사람이 알아챌 방법이 지금은 없다.** §3 의 대조를 `GET /hub/download/status`
뿐 아니라 **데이터셋 상세**에서도 부를 수 있게 하면, 이미 받아 둔 것들도 한 번 훑어
`missing` 을 말해 줄 수 있다 — [dataset-viewer-troubleshooting.md](../docs/dataset-viewer-troubleshooting.md) §2-2
가 손으로 하라고 적어 둔 그 일이다.

## 9. 구현 결과 (2026-09-23)

### 만든 것

| 자리 | |
|---|---|
| `services/hub_download.py` (새로) | 상태·샘플러·완료 대조. **디스크만 본다** — tqdm 문구에 안 기댄다 |
| `hub_client.start_download` | `hub_download.start` 에 위임. 죽은 인자 `local_dir` 삭제 |
| `POST /hub/download` | 고정 문구 대신 **상태를 그대로** 돌려준다(폴링 응답과 같은 모양) |
| `GET /hub/verify` (새로) | 이미 받아 둔 것의 빠진 파일 — §8 의 그 일 |
| `hooks/useHubDownload.ts` (새로) | 폴링하는 **유일한 자리**. 받는 중일 때만 돈다 |
| `HubBrowser` | 막대 + `n/N · %·속도·ETA`, `받음 ✓`, `incomplete` 면 [다시 받기] |
| `TrainingPage` | 끝 판정을 서버 값으로. 받다 만 것은 경고로 말한다 |

### 실측 — 진짜 다운로드 (`wego-mink/sim_two_box_2_120`, 19MB)

```
downloading  0/10 파일    0.0%          0/19119146
downloading  5/10 파일    0.1%      22309/19119146   ETA 856
downloading  8/10 파일    5.4%    1028886/19119146   0.3 MB/s  ETA 61
completed   10/10 파일  100.0%   19119146/19119146
```

캐시 히트(이미 다 받은 repo)도 확인했다 — **바이트는 안 늘고 파일 수만 10/10 으로
올라간다.** §2 가 걱정한 "멈춘 것처럼 보이는" 경우가 그대로 나왔고, 파일 수가 같이
보이므로 오해가 안 생긴다.

⚠ 완료 시 `bytes_done` 을 디스크 실측으로 채운다. 안 그러면 캐시 히트에서
`0/18897020 · 100%` 가 되어 **버그로 읽힌다**(처음 구현이 그랬다).

### 테스트가 잡아준 것

`test_pressing_twice_does_not_start_two_downloads` 가 `_set(repo_id, **_blank(repo_id, …))`
의 **중복 인자 TypeError** 를 잡았다 — 두 번 누르는 경로에서만 터지는 것이라 손으로는
안 밟았을 자리다.

### 아직 안 한 것 (§6 그대로)

- **취소** — `snapshot_download` 에 중단 수단이 없다. 프로세스로 빼야 진짜 취소가 된다
- **재시작 생존** — 상태가 메모리다. 게이트웨이가 재시작하면 돌던 다운로드가 같이 죽고
  상태도 사라진다
- **`GET /hub/verify` 를 화면에 붙이는 것** — 엔드포인트는 있고 부르는 화면이 아직 없다.
  데이터셋 상세에 [파일 확인] 을 두면 받아 둔 것들을 한 번에 훑을 수 있다
