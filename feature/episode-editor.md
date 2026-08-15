# 에피소드 에디터/뷰어 — 재생·신호·페이즈를 한 화면에서 보고 고친다

> [01-phase-annotation](01-phase-annotation.md) §4~5(편집 UI, 4~5단계 미착수)의 **집을 여기로 넓힌다.**
> 그 문서의 UI 는 페이즈 전용이었는데, 실제로 필요한 도구는 더 넓다 —
> REF.md §5.3 이 의도한 *"에피소드 리플레이 → 불량 체크 → 삭제"* 가 지금은 불가능하다.
> [DatasetsPage](../frontend/src/pages/DatasetsPage.tsx) 는 **에피소드 번호만 보고** 지운다.
>
> 전제가 되는 결정(이미 실행됨): **분류기는 이 화면과 별개로 단독 실행된다**
> (`python -m piper_phase`, [01 문서](01-phase-annotation.md) 머리말 셋째 항목).
> 이 화면은 사이드카를 **읽고 쓰는 쪽**이지 분류기의 실행 전제가 아니다.
> CLI 로 만든 사이드카든 API 로 만든 것이든 같은 파일이라 똑같이 보인다.

---

## 0. 지금 있는 것 (전수)

| 층 | 있는 것 | 없는 것 |
|---|---|---|
| UI | [DatasetsPage](../frontend/src/pages/DatasetsPage.tsx): 목록·정렬, 에피소드 테이블, 선택 삭제([:115](../frontend/src/pages/DatasetsPage.tsx#L115))·task 일괄 수정([:123](../frontend/src/pages/DatasetsPage.tsx#L123)), 디코딩 캐시 생성/삭제 버튼 | **에피소드 내용을 볼 방법 전부.** 재생·신호·페이즈 표시 없음 |
| 백엔드 | [/edit](../backend/app/routers/datasets.py#L114)(delete_episodes·split·merge, `edit_pm` 유닛) · [/update-task](../backend/app/routers/datasets.py#L131) · [/decode-cache](../backend/app/routers/datasets.py#L237)(PNG, `upload_pm`) · [/api/phase/*](../backend/app/routers/phase.py)(분석·라벨 조회/저장·신호·상태, 검증 포함) | **프레임 서빙 엔드포인트** (디버그 런용 [debug_logs](../backend/app/routers/debug_logs.py)만 있음) |
| 계약·부품 | 사이드카 2종([labeler.py](../phase/piper_phase/labeler.py)) · [PlotlyChart](../frontend/src/components/PlotlyChart.tsx) markerX 재생헤드 · [DebugLogsPage](../frontend/src/pages/DebugLogsPage.tsx#L230) 스크러버+`<img>` 패턴 · [phase_pm](../backend/app/services/dataset_jobs.py#L30) 유닛(자리만 있고 미사용) | — |

## 1. 역할 분담 (결정)

**DatasetsPage = 저장소 관리** (목록·용량·업로드·캐시·split/merge 같은 데이터셋 단위 작업).
**EpisodesPage(신규 `/episodes`) = 에피소드 안을 보는/고치는 도구** — 재생 + 신호 그래프 +
페이즈 트랙 + 구간 편집 + 에피소드 수명주기(삭제·task 수정).

- 01 §4.1 의 `/phase-editor` 는 **만들지 않고 이 페이지가 대체한다.** 페이즈 트랙은
  이 화면의 트랙 하나이지 화면의 정체성이 아니다. 라벨 없는 데이터셋도 열린다
  (페이즈 트랙만 "분석 안 됨" — [분석] 버튼 노출).
- DatasetsPage 의 에피소드 테이블(삭제·task)은 단기 유지하되, 뷰어 안정화 후 **이관**한다.
  같은 기능이 두 화면에 계속 살면 한쪽만 고치는 사고가 난다 (§5 순서 4).
- [pages.ts](../frontend/src/config/pages.ts) '수집' 그룹에 한 줄 추가. Plotly 포함이므로
  DebugLogsPage 처럼 **lazy import** (01 §4.1 그대로).

## 2. 레이아웃 — 01 §4.2~4.3 을 그대로 쓴다

ASCII 목업·그래프 3종·단축키·드래그 편집 전부 [01 §4](01-phase-annotation.md#4-편집-ui) 가 정본이다.
여기서는 **추가분만** 적는다:

- 에피소드 리스트: `flag_outliers` 의 ⚠(사유 포함)를 배지로, **⚠ 먼저 정렬** (01 §4.2 의 핵심 가치).
  미분석 데이터셋이면 배지 대신 "분석 안 됨" 상태 하나.
- 리스트에서 선택 → [삭제]·[task 수정] — 재생으로 **확인한 뒤** 지운다. REF §5.3 의 흐름 완성.
- 헤더에 사이클 분포 요약(`3사이클×50`) — CLI `--json` 과 같은 `summary()` 출력이다.

## 3. 재생 경로 — **이중 모드** (구현하며 결정 변경)

**뷰어 기본 = 동영상 직접 재생, 프레임 캐시 = 폴백 + 편집(3단계)용.**
01 §4.3 은 이미지 시퀀스 단독이었는데, 실측해 보니 동영상 쪽 조건이 전부 좋았다:
비디오가 h264/yuv420p(브라우저 네이티브 재생), 에피소드 경계가 메타에 이미 있고
(`videos/{key}/from·to_timestamp` — 상세 응답의 episodes 레코드에 포함),
Starlette `FileResponse` 가 Range 를 206 으로 처리한다(실측). 프레임 동기는
`requestVideoFrameCallback` 의 mediaTime 으로 잡는다. **캐시 생성 없이 즉시 열리는**
체감 차이가 크다.

프레임 캐시가 남는 이유 둘: 코덱 의존(yuv444p 데이터셋은 `<video>` 불가)과
3단계 편집의 프레임 단위 정밀 조작(파일명 = 프레임 번호가 정본).
비디오 서빙은 에피소드가 아니라 **파일 단위**(`/videos/{cam}/{chunk}/{file}`) —
같은 chunk 의 에피소드들이 브라우저 캐시를 공유한다.

| # | 작업 | 비고 |
|---|---|---|
| 3a | `GET /api/datasets/{id}/episodes/{ep}/frames/{cam}/{idx}` | `FileResponse`, 캐시 없으면 404 + UI 에 [캐시 생성] 버튼 (01 §7). ⚠ catch-all(`/{dataset_id:path}`) **위에** 등록 — upload-status 가 겪은 사고 |
| 3b | decode-cache 에 JPEG(q=85)+긴변 320px 옵션 | 31k 프레임×2캠: PNG 수 GB → 수백 MB (01 §4.3 주의). 뷰어는 축소본이면 충분하다 |
| 3c | ⚠ **decode-cache 의 단일 chunk 가정 수정** | 현재 스크립트가 `chunk-000/file-000` 하나만 연다([datasets.py:266](../backend/app/routers/datasets.py#L266)). 멀티 chunk 데이터셋이면 **에피소드 절반이 조용히 빠진다** — [labeler.load_frames](../phase/piper_phase/labeler.py#L64) 가 같은 함정을 이미 문서화했다 |

프론트 재생은 [DebugLogsPage:230-238](../frontend/src/pages/DebugLogsPage.tsx#L230) 의
`<input type="range">`+`<img>` 패턴 + ±N 프레임 프리페치, `requestAnimationFrame` 재생.

## 4. 데이터 흐름 — API 는 대부분 이미 있다

| 동작 | 경로 | 상태 |
|---|---|---|
| 에피소드 목록·메타 | `GET /api/datasets/{id}` | 있음 |
| 라벨/신호/상태 읽기 | `GET /api/phase/{id}/labels` · `/signals/{ep}` · `/status` | 있음 |
| 구간 편집 저장 | `PUT /api/phase/{id}/labels/{ep}` | 있음 (빈틈·겹침 검증 포함) |
| 파라미터 미리보기 | `POST /api/phase/{id}/analyze` `{episodes:[ep], save:false}` | 있음 (병합 저장이라 안전) |
| 프레임 이미지 | §3a | **신규** |
| 에피소드 삭제 | `POST /api/datasets/{id}/edit` delete_episodes | 있음 — 아래 ⚠ |

**✓ 삭제 ↔ 사이드카 어긋남 — 해소됨.** 실제 실체는 문서 예상보다 나빴다:
lerobot 의 in-place delete 는 원본을 `<이름>_old` 로 옮기고 meta 를 **새로 쓴다** —
사이드카는 밀리는 게 아니라 **백업에 버려진다** (손으로 복사하면 그때 밀린다).
편집 경로가 이제 [wrapper/edit_dataset.py](../wrapper/edit_dataset.py) 를 지난다:
편집 성공 후 **같은 프로세스에서** `_old` 의 사이드카(페이즈 라벨·신호·piper_cameras)를
번호 재매핑해 새 meta 로 가져온다 ([piper_phase.sidecar](../phase/piper_phase/sidecar.py)).
게이트웨이 훅이 아닌 래퍼인 이유: 편집은 유닛이라 게이트웨이 재시작에도 도는데,
훅이면 재시작 순간 동기화가 소리 없이 빠진다. split/merge 는 안 가져간다 —
산출물이 여러 개라 대응이 자명하지 않아 로그로 알리고 재분석을 안내한다.
`_old` 백업은 lerobot 표준 동작이라 그대로 둔다 (데이터셋 목록에 보이면 그게 백업이다).

**덤으로 잡힌 것**: 기존 편집 인자 `--repo-id` 는 lerobot 0.5 CLI 가
`unrecognized arguments` 로 거부한다 (`--repo_id` 밑줄이 맞다) — 즉 화면의
에피소드 삭제는 **애초에 CLI 에서 죽고 있었다.** 래퍼로 실제 실행해 보고서야 드러났다.

**분석·굽기는 [phase_pm](../backend/app/services/dataset_jobs.py#L30) 으로.** 지금 analyze 는
게이트웨이 in-process(`asyncio.to_thread`)다 — 짧아서 버텼지만, 전체 재분석은 수 분이고
굽기(01 §5)는 더 길다. 유닛이면 게이트웨이 재시작에도 살고 journald 로 로그를 이어 읽는다.
`phase_pm` 이 §5.3 의도대로 자리만 잡아놓고 미사용인 상태를 이 김에 해소한다.

## 5. 구현 순서 — 각 단계가 그 자체로 쓸모 있게

| # | 단계 | 그 시점의 가치 | 검증 |
|---|---|---|---|
| 1 | ☑ §3 재생 경로 (비디오 Range + 프레임 서빙 + JPEG 옵션 + 멀티 chunk 수정) | API 만으로 프레임 확인 가능 | 실데이터셋 206/JPEG 실측 |
| 2 | ☑ EpisodesPage 뷰어 (이중 모드 재생 + 그래프 + 페이즈 트랙 + ⚠ 정렬) + §3.5 수동 분석(파라미터·미리보기) | "리플레이 → 불량 체크 → 삭제" 완성 | `npm run build` + 실데이터셋 열람 |
| 3 | ☑ 편집 인터랙션 (드래그/분할 S/병합 M/0~6 지정/Ctrl+Z) + PUT 저장(검토됨 ✔) | 01 의 4~5단계 완료 | 50개 실제 검토 (01 §8) 는 사용 중 진행 |
| 4 | ☑ 삭제·task 이관 + **사이드카 재매핑** — 뷰어 리스트에서 선택→[삭제]/[task 변경] | 기능 단일 소유 + 기존 버그 해소 | 실데이터 사본으로 delete 실행 — 11→9, 옛 #2→새 #1 라벨·신호 일치 |
| 5 | 굽기 UI (01 §5, `phase_pm`) | 학습 투입 가능 | 01 §9 라운드트립 |

검증 공통: 프론트는 반드시 `cd frontend && npm run build` (`npx tsc --noEmit` 은 no-op).

## 6. 결정이 필요한 것

1. **원격 데이터셋** — 학습이 두 번째 머신으로 갔다. 뷰어는 일단 게이트웨이 로컬
   데이터셋만 연다 (원격은 Hub 왕복이 이미 있는 경로).
2. **decode-cache 자동 생성** — 뷰어 진입 시 캐시가 없으면 자동으로 만들지, 버튼만 둘지.
   초안: 버튼만 (수 분짜리 디스크 작업을 열람이 암묵 트리거하면 놀란다).
3. **DatasetsPage 테이블 이관 시점** — 2단계 직후 vs 4단계. 초안: 4단계 (뷰어가 검증되기 전에
   기존 동선을 없애지 않는다).
