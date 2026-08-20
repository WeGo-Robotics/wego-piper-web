# YOLO 커스텀 학습 — 캡처→라벨→학습→가중치 루프

> **기획 단계.** 아직 코드 없음. 이 문서가 승인되면 단계별로 구현한다.

데모 페이지(90a8eb4~)가 커스텀 가중치의 **소비** 쪽을 완성했다: 업로드하면
드롭다운에 뜨고, 시작하면 돈다. 이 문서는 **생산** 쪽이다 — 이 화면에서
이미지를 모으고, 라벨을 달고, 학습을 돌려서, 결과 best.pt 가 같은 드롭다운에
자동으로 나타나는 루프.

분리수거 데모의 실제 필요에서 나왔다: COCO 80 클래스에는 우리 현장의 물체
(특정 재활용품, 우리 통, 우리 조명)가 없거나 약하다. 현장 카메라로 찍은
수백 장 + yolo11n 파인튜닝이면 충분한 자리다.

## 원칙 — 전부 기존 계약 위에 얹는다

1. **캡처는 기존 읽기 경로의 재사용이다.** 소스는 둘:
   - **라이브**: `/api/vision/segments/{name}/snapshot` 이 이미 세그먼트→JPEG
     를 한다. 캡처 = 같은 읽기를 디스크에 저장. 카메라를 새로 열지 않는다.
   - **LeRobot 데이터셋 영상**: 녹화해 둔 수천 프레임이 곧 현장(물체·통·
     조명) 이미지 은행이다. 경로는 둘 다 기존 것 —
     일괄 가져오기는 디코딩 캐시(파일 복사, 아래), **동영상 재생 중 낱장
     캡처는 브라우저가 `<video>` 현재 프레임을 canvas 로 떠서 업로드**한다.
     서버에 프레임 추출(ffmpeg seek) 경로를 새로 만들지 않는다 — 뷰어
     기본 모드(동영상)에서 캐시 생성 없이 바로 줍는 게 목적이고,
     보이는 그대로가 곧 원하는 프레임이다.
2. **학습은 유닛이다.** `piper-yolotrain` — yolod·정책서버와 같은
   `make_process` 소유. 게이트웨이가 재시작해도 학습은 산다. 로그는
   journald 스트리밍(systemd_process 에 이미 있음)으로 WS 에 흐른다.
3. **결과는 기존 카탈로그로 들어간다.** 학습 완료 시 best.pt 를
   `yolo_models_dir` 에 `<데이터셋>-<날짜>.pt` 로 복사하면 끝 —
   `/api/vision/models` 가 이미 그 디렉토리를 스캔한다. 새 소비 경로 없음.
4. **GPU 배타.** ultralytics 학습은 VLA 학습·추론과 GPU 를 다툰다.
   기존 exclusivity(모드 배타 관리) 에 `yolo_train` 활동으로 등록한다.
   yolod(추론)와의 공존은 nano 기준 문제없지만 대형 모델은 경고를 띄운다.

## 데이터 모델 — ultralytics 형식을 그대로 정본으로

```
backend/data/yolo_datasets/<name>/
  classes.json          # ["pet_bottle", "can", ...] — 인덱스가 곧 클래스 id
  images/<uuid>.jpg     # 캡처 원본 (카메라 해상도 그대로)
  labels/<uuid>.txt     # YOLO 형식: class cx cy w h (정규화) — 없으면 미라벨
  sources.jsonl         # {file, type: live|episode|upload, cam?, dataset?,
                        #  episode?, frame? | t?}  — 동영상 캡처는 프레임
                        #  번호 대신 재생 시각(t)을 적는다
```

- `sources.jsonl` 은 출처 기록 (append 전용). YOLO 형식에는 출처 칸이 없고,
  **val 분할을 에피소드 단위로 묶는 데** 필요하다 (아래 리스크) + 갤러리
  배지("ep 3 · top")용. 학습 입력에는 안 들어간다.

- **변환 계층을 두지 않는다.** 자체 스키마→YOLO 변환을 만들면 그게 새
  중복원이 된다. 학습 시 생성하는 것은 `data.yaml` + train/val 파일 리스트
  (9:1, 시드 고정)뿐이다.
- **val 분할은 출처 그룹 단위다.** 같은 에피소드에서 나온 프레임들은 거의
  같은 장면이라, 프레임 단위 랜덤 분할이면 train 과 val 에 사실상 같은
  그림이 들어가 mAP 가 부풀려진다 — 데모에서는 좋아 보였는데 현장에서
  약한 모델이 나온다. 그룹 키 = `sources.jsonl` 의 (type, dataset, episode)
  또는 라이브 캡처의 (cam, 날짜).
- 클래스 목록은 데이터셋 생성 시 정하고 **추가만 허용** (삭제·순서 변경은
  기존 라벨 파일의 id 를 전부 깨뜨린다 — 에피소드 sidecar 어긋남과 같은
  버그 클래스).
- 외부에서 라벨링한 YOLO 데이터셋 zip 가져오기를 탈출구로 둔다 —
  자체 라벨러가 부족하면 Roboflow/labelImg 로 나갔다 돌아올 수 있다.

## 단계

### 1단계 — 캡처

- `POST /api/yolo/datasets` `{name, classes[]}` / `GET /api/yolo/datasets`
  (이미지 수·라벨 수 포함) / `DELETE`
- `POST /api/yolo/datasets/{name}/capture` `{cam}` — 스냅샷을 저장하고
  이미지 id 반환. 간격 캡처(N초마다, 프론트 타이머)로 물체를 재배치하며
  다양성을 모은다.
- **데모 페이지 훅**: 실행 중 카메라 카드에 "이 장면 캡처" 버튼 —
  오검출을 본 그 순간이 하드 케이스를 모을 최적 타이밍이다. 저장은
  어노테이트 프리뷰가 아니라 **원본 프레임** (스냅샷 경로 재사용).
- **에피소드 가져오기**: `POST /api/yolo/datasets/{name}/import-episode`
  `{dataset_id, episode, cam, stride | indices[]}` — 디코딩 캐시
  (`<ds>/images/<key>/episode-XXXXXX/frame-XXXXXX.jpg`)를 서버가 직접 읽어
  **파일 복사**로 끝낸다. 재디코딩도, 자기 HTTP 호출도 없다. 캐시가 없으면
  404 + "decode-cache 먼저 생성" — 에피소드 뷰어와 같은 UX·같은 생성 버튼.
  `sources.jsonl` 에 출처를 적는다.
  - UI 는 캡처 탭의 두 번째 모드: 데이터셋→에피소드→카메라 선택(에피소드
    뷰어의 픽커 재사용), 프레임 스크럽으로 낱장 캡처 + "N프레임 간격으로
    일괄 가져오기".
  - **stride 기본 30** (30fps 기준 1초에 1장). 연속 프레임은 거의 같은
    그림이라 다 넣으면 용량만 먹고 학습에는 중복이다.
  - **범용 이미지 업로드**: `POST /api/yolo/datasets/{name}/images` —
    raw JPEG 바디 + 출처 메타 쿼리 (가중치 업로드와 같은 방식, multipart
    없음). 이 엔드포인트 하나가 세 입구를 받는다: ① 뷰어 동영상 모드의
    캡처(canvas.toBlob), ② 갤러리에 외부 이미지 드래그&드롭(폰 사진 등,
    type: upload), ③ 향후 어떤 화면이든 "이 장면 줍기" 훅.
  - 에피소드 뷰어 훅: **동영상 모드에서든 프레임(캐시) 모드에서든**
    "YOLO 데이터셋으로 캡처" 버튼 — 동영상 모드는 canvas 캡처(출처에
    재생 시각 t 기록), 프레임 모드는 캐시 파일 복사(프레임 번호 기록).
    캐시 생성 없이도 줍기가 된다는 게 요점이다.
- 갤러리: 새 페이지 `/yolo-train` (학습 그룹, 라벨·학습 탭과 한 페이지).
  썸네일 그리드(출처 배지: live cam / ep N) + 삭제.

### 2단계 — 라벨링 (공수의 대부분)

- 캔버스 bbox 편집기: 드래그로 박스, 클래스 선택(숫자키), 삭제,
  이전/다음 이미지 (방향키). `GET/PUT .../labels/{img}` 는 박스 배열 JSON
  ↔ YOLO txt 를 서버가 왕복 변환.
- **사전 라벨**: `POST .../prelabel/{img}` `{model, conf}` — 선택한 모델
  (표준 또는 이전 커스텀)로 1장 추론해 박스 초안을 채운다. 사람은 수정만.
  ⚠ 게이트웨이에서 torch 를 import 하지 않는다 — yolod 와 같은 이유로
  `--once` 모드 subprocess 또는 경량 RPC. 첫 구현은 subprocess `--once`
  (이미 있음)에 `--image <path>` 입력을 추가하는 쪽이 싸다.
- 진행 표시: 전체/라벨됨/박스 0개(배경 샘플로 유효) 카운트.

### 3단계 — 학습 유닛

- `POST /api/vision/train` `{dataset, base_model, epochs, imgsz}` →
  data.yaml + 리스트 생성 → `piper-yolotrain` 유닛으로
  `yolo detect train data=... model=<base> epochs=... imgsz=...` 실행.
  base_model 은 표준 카탈로그 또는 **이전 커스텀 가중치** (반복 파인튜닝).
- `GET /api/vision/train/status` — 유닛 상태 + `results.csv` 꼬리 파싱
  (epoch, box_loss, mAP50). 화면은 데모 페이지 스타일의 단순 폴링.
- 완료 시 수확: `runs/.../weights/best.pt` → `yolo_models_dir/<dataset>-<MMDD>.pt`
  + 학습 파라미터·mAP 를 곁 JSON 으로 (카탈로그 label 에 노출).
  수확은 **게이트웨이 훅이 아니라 유닛 스크립트의 마지막 스텝** —
  에피소드 편집 wrapper 와 같은 이유(유닛은 게이트웨이 재시작을 넘어 산다).
- 검증: 학습 표준 출력의 최종 per-class mAP 표를 상태 응답에 포함.

### 4단계 — 루프 마감 (선택)

- 데모 페이지에서 새 가중치로 바로 재시작하는 단축 버튼.
- 커스텀 가중치의 곁 JSON(mAP, 클래스, 학습일)을 모델 드롭다운 설명에.
- 하드 케이스 루프: 데모 중 캡처 → 라벨 탭에 "미라벨 N장" 배지.

## 리스크·미결

- **라벨러 공수**: 2단계가 전체의 절반 이상. zip 가져오기(반나절)를 먼저
  깔면 라벨러 없이도 1→3단계가 성립한다 — 구현 순서를 1, 3, 2 로 뒤집는
  선택지 있음.
- **환경**: ultralytics 는 `settings.grpc_python` 환경에 이미 있다 (yolod
  가 쓴다). 학습 유닛도 같은 인터프리터를 쓴다 — 새 의존성 없음.
- **디스크**: 이미지 수백 장은 수십 MB, runs/ 는 수백 MB 까지 —
  기존 디스크 경고 임계치가 커버. runs/ 는 수확 후 정리.
- **VRAM**: nano/small 학습(imgsz 640, batch 16)은 ~4-6GB. 정책서버(1GB)
  와 공존 가능하지만 VLA 학습과는 배타 필수.
