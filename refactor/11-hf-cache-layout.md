# 11. HF 캐시 레이아웃 해석 규칙 중복 (B급) — ☑ 완료

> [app/core/hf_layout.py](../backend/app/core/hf_layout.py) 로 통합.
> `info.json` 폴백을 **한 곳으로 모아** 목록과 상세가 같은 규칙을 보게 했다.
> 곁다리로 `/api/models` 가 이 머신에서 **500 을 내고 있던 것**도 고쳤다 —
> `model_paths.json` 에 남은 `/root/.cache/...` 하나 때문에 `.exists()` 가
> `PermissionError` 를 던져 모델 목록 전체가 죽었다.

## 문제

"HuggingFace 캐시 디렉토리를 어떻게 읽는가"라는 한 가지 사실이 두 파일에 쌍둥이로 존재하고,
그중 하나는 이미 어긋나 있다.

### (1) 헬퍼 쌍둥이 — 접두사만 다르고 본문이 같다

| model_scanner.py | dataset_scanner.py |
|---|---|
| [`_repo_id_from_dirname`:97-104](../backend/app/services/model_scanner.py#L97-L104) | [`_repo_id_from_dirname`:31-38](../backend/app/services/dataset_scanner.py#L31-L38) |
| [`_latest_snapshot`:107-115](../backend/app/services/model_scanner.py#L107-L115) | [`_latest_snapshot`:41-49](../backend/app/services/dataset_scanner.py#L41-L49) |

`_latest_snapshot`은 **주석을 빼면 완전히 동일**하다. `_repo_id_from_dirname`은
`"models--"` / `"datasets--"` 접두사 한 글자 차이뿐이다.

```python
# 양쪽 모두
snapshots_dir = X / "snapshots"
if not snapshots_dir.exists(): return None
candidates = [d for d in snapshots_dir.iterdir() if d.is_dir()]
if not candidates: return None
return max(candidates, key=lambda d: d.stat().st_mtime)
```

### (2) `info.json` 위치 규칙이 세 곳, 그중 하나만 다르다 — 드리프트

| 위치 | 규칙 |
|---|---|
| [dataset_scanner.py:65-67](../backend/app/services/dataset_scanner.py#L65-L67) (Hub 캐시 스캔) | `meta/info.json` → 없으면 `info.json` |
| [dataset_scanner.py:226-228](../backend/app/services/dataset_scanner.py#L226-L228) (상세 조회) | `meta/info.json` → 없으면 `info.json` |
| [dataset_scanner.py:95-97](../backend/app/services/dataset_scanner.py#L95-L97) (LeRobot 캐시 스캔) | `meta/info.json` **없으면 skip** (폴백 없음) |

`meta/` 없이 `info.json`이 평평하게 놓인 데이터셋이 LeRobot 캐시에 있으면
**목록에는 안 보이는데 상세 조회로는 열린다.** 사용자 입장에서는 "데이터셋이 사라졌다"로 보인다.

셋 중 어느 쪽이 정답인지부터 정해야 한다 — LeRobot v2는 `meta/info.json`을 쓰므로
평평한 폴백 자체가 구버전 잔재일 수 있다. 그렇다면 **폴백을 셋 다 없애는 것**이 답이다.

## 곁다리 — 삭제 경로도 같은 지식을 다시 안다

[dataset_scanner.py:257-259](../backend/app/services/dataset_scanner.py#L257-L259)가
*"Hub면 `snapshots/hash` → 상위 `datasets--org--name` 폴더를 지운다"* 는 레이아웃 지식을
또 한 번 직접 적고 있다. 스캔 규칙이 바뀌면 여기도 같이 틀어진다. **잘못된 폴더를 지울 수 있는
경로이므로** 레이아웃 지식이 흩어져 있는 게 특히 위험하다.

## 해결안

`backend/app/core/hf_layout.py`에 레이아웃 해석만 모은다:

```python
def repo_id_from_dirname(dirname: str, kind: Literal["models", "datasets"]) -> str | None: ...
def latest_snapshot(repo_dir: Path) -> Path | None: ...
def resolve_info_json(root: Path) -> Path | None: ...   # 폴백 규칙 단일 정의
def repo_root_for_delete(path: Path) -> Path: ...       # snapshots/hash → datasets--org--name
```

model_scanner / dataset_scanner는 이걸 부르기만 한다. 순수 경로 계산이라
의존성이 안 생기고 단위 테스트도 쉽다.

## 검증

- 데이터셋 목록 개수가 이전과 같은지 (Hub 캐시 / LeRobot 캐시 양쪽 다 있는 상태에서)
- 모델 목록 개수가 이전과 같은지
- 목록에 보이는 모든 데이터셋이 상세 조회로도 열리는지 **(드리프트 회귀 테스트)**
- 데이터셋 삭제가 올바른 폴더만 지우는지 — **실행 전 `--dry-run` 성격으로 경로를 먼저 로그로 확인**

## 상태

☑ 완료
