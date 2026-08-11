# 카메라 설정 프로파일 + 실행 전 자동 재적용

카메라 조정값(해상도/FPS + 노출·화이트밸런스 등 컨트롤)을 **이름 붙은 프로파일**로 저장하고,
녹화·추론 subprocess가 카메라를 열기 **직전에 한 번 더 강제 적용**한다.

---

## 왜 자꾸 초기화되는가 — 원인은 하나가 아니다

"초기화된다"는 증상 뒤에 서로 다른 원인이 7개 있다. 프로파일 파일 하나 만든다고 전부
해결되지 않으므로 먼저 분리한다.

### (1) 컨트롤 값은 애초에 저장되지 않는다

[camera_manager.py:584-601](../backend/app/services/camera_manager.py#L584-L601)의 `save_session`이
저장하는 건 `width/height/fps/color_mode/rotation/fourcc` 뿐이다. 사용자가 실제로 오래
만지는 값 — 밝기, 노출, 화이트밸런스, 게인, 포커스 — 은 **디바이스에만 있는 휘발성 상태**고
디스크에 흔적이 없다. [cameras.py:142-152](../backend/app/routers/cameras.py#L142-L152)의
`/control`도 디바이스에 쓰기만 하고 어디에도 기록하지 않는다.

→ 서버 재시작, USB 재열거, 하드웨어 리셋 중 **아무거나** 한 번이면 전부 날아간다.

### (2) 세션 키가 `/dev/videoN`이다

[camera_manager.py:621-624](../backend/app/services/camera_manager.py#L621-L624)에서 세션 복원은
`cam_id`(=`/dev/video2`)로 매칭한다. USB가 재열거되면 video 번호가 바뀌므로
`Session camera ... not found in scan, skipping`으로 조용히 버려진다. USB 컨트롤러가 죽어
리바인딩한 뒤(이 저장소에서 실제로 겪은 시나리오) 설정이 통째로 사라지는 경로가 이것이다.

정작 안정 식별자는 이미 수집하고 있다 — OpenCV는 `usb_port`
([camera_manager.py:419-430](../backend/app/services/camera_manager.py#L419-L430)),
RealSense는 `serial`. 쓰지 않고 있을 뿐이다.

### (3) 디바이스 재열거·리셋이 컨트롤을 드라이버 기본값으로 되돌린다

`/cameras/reset-device`([cameras.py:155-171](../backend/app/routers/cameras.py#L155-L171))는
펌웨어 파워사이클이다. 복구 수단으로는 맞지만 컨트롤은 전부 default로 돌아간다. 프론트는
4초 뒤 재스캔만 하고([CamerasPage.tsx:137-143](../frontend/src/pages/CamerasPage.tsx#L137-L143))
값을 되돌려 놓는 단계가 없다.

### (4) 해상도/FPS는 설정해도 디바이스에 전달되지 않고, 오히려 덮어써진다

`update_config`([camera_manager.py:75-78](../backend/app/services/camera_manager.py#L75-L78))는
파이썬 필드만 바꾼다. 그리고 `_open_cap`
([camera_manager.py:91-93](../backend/app/services/camera_manager.py#L91-L93))이
연결/probe 때마다 **캡처에서 읽어온 값으로 `self.width/height/fps`를 덮어쓴다**.
`cap.set(CAP_PROP_*)` 호출은 아예 없다.

→ UI에서 1280×720을 입력해도 다음 probe 한 번이면 640×480으로 되돌아간다. 이건 저장의
문제가 아니라 **적용이 없는** 문제다.

### (5) 실행 경로가 설정을 안 본다 — 해상도가 하드코딩되어 있다

| 경로 | 코드 | 문제 |
|---|---|---|
| 추론(gRPC) | [models.py:97-116](../backend/app/routers/models.py#L97-L116) | `width: 640, height: 480, fps: 30` **문자열 하드코딩** |
| 추론(로컬 wrapper) | [models.py:119-146](../backend/app/routers/models.py#L119-L146) | width/height/fps를 **아예 안 넣음** (LeRobot 기본값에 맡김) |
| 녹화 | [RecordingPage.tsx:104-125](../frontend/src/pages/RecordingPage.tsx#L104-L125) | 프론트가 별도 `camWidth/camHeight/fps` 상태로 조립 |

카메라 페이지에서 뭘 하든 실행 인자에는 반영되지 않는다. 녹화만 프론트 로컬 상태를 쓰는데,
그건 카메라 페이지의 설정과 다른 값이다.

### (6) 녹화/추론 직전에 카메라를 놓는데, 다시 맞춰주는 단계가 없다

[recording.py:85-95](../backend/app/routers/recording.py#L85-L95)와
[models.py:56-71](../backend/app/routers/models.py#L56-L71)은 subprocess 기동 전에
OpenCV·RealSense를 강제 해제한다(USB 대역폭 경합 방지 — 필요한 동작이다). 문제는 그 다음
LeRobot이 **자기 파이프라인으로 디바이스를 새로 여는데**, 그 사이에 값을 다시 밀어 넣는
지점이 없다는 것. RealSense는 파이프라인 재시작에서 옵션이 되돌아가는 경우가 있어
특히 티가 난다.

### (7) 자동 모드 종속성 — 순서를 안 지키면 "적용했는데 안 먹은" 것처럼 보인다

`auto_exposure`가 자동이면 `exposure_time_absolute`는 커널이 `inactive` 플래그를 세우고
쓰기를 무시한다. UI는 이걸 표시만 한다
([CamerasPage.tsx:274-277](../frontend/src/pages/CamerasPage.tsx#L274-L277)).
값을 복원할 때 **스위치를 먼저 수동으로 돌리지 않으면** 종속 값이 조용히 버려진다.
`white_balance_automatic` / `white_balance_temperature`, `focus_automatic_continuous` /
`focus_absolute`도 같은 관계다.

---

## 설계 — 3층으로 나눈다

```
저장층   camera_profiles.py : 프로파일 파일 (config_dir/camera_profiles/*.json)
             ↑ 저장 / 목록 / 활성 프로파일
적용층   CameraProfileApplier : 안정 키 매칭 → 순서 있는 적용 → read-back 검증
             ↑ apply(cam_ids, timeout_budget) -> ApplyReport
트리거층 실행 훅 : 부팅 복원 / 스캔·연결 후 / 하드웨어 리셋 후 / **녹화·추론 시작 직전**
```

핵심은 적용층을 **함수 하나로 모으고**, 실행 경로 세 곳이 전부 그 함수만 부르게 하는 것이다.
지금처럼 경로마다 카메라 설정 조립 로직이 흩어져 있으면 또 어긋난다.

### 안정 식별자 (`profile_key`)

| 타입 | 키 | 근거 |
|---|---|---|
| RealSense | `rs:<serial>:<stream>` | serial은 재열거·리셋에도 불변 |
| OpenCV(USB) | `usb:<usb_port>` (예: `usb:4-3:1.0`) | 물리 포트 고정. 동일 모델 2대 구분 가능 |
| OpenCV(포트 미상) | `name:<sysfs name>` | 폴백 |

`/dev/videoN`은 키가 아니라 **매칭 결과**다. 프로파일에는 `last_dev`로 참고만 기록하고,
매칭은 `usb_port → name → last_dev` 순으로 시도한다. 같은 모델을 다른 포트에 꽂았을 때
잘못 적용되는 걸 막으려면 `name` 폴백은 후보가 1개일 때만 허용한다.

### 프로파일 스키마

`~/.config/piper-web/camera_profiles/<name>.json`
(경로는 [config.py:50](../backend/app/core/config.py#L50)의 `config_dir`. 로봇 프리셋
[robot_manager.py:24](../backend/app/services/robot_manager.py#L24)와 같은 규칙)

```json
{
  "name": "주간-형광등",
  "updated_at": "2026-08-11T14:02:11+09:00",
  "cameras": [
    {
      "key": "usb:4-3:1.0",
      "match": { "cam_type": "opencv", "usb_port": "4-3:1.0",
                 "name": "HD Webcam", "last_dev": "/dev/video2" },
      "stream": { "width": 640, "height": 480, "fps": 30,
                  "fourcc": "MJPG", "color_mode": "rgb", "rotation": 0 },
      "controls": {
        "auto_exposure": 1,
        "exposure_time_absolute": 312,
        "white_balance_automatic": 0,
        "white_balance_temperature": 4600,
        "brightness": 0, "contrast": 32, "saturation": 60
      }
    },
    {
      "key": "rs:207522073562:color",
      "match": { "cam_type": "realsense", "serial": "207522073562", "stream": "color" },
      "stream": { "width": 640, "height": 480, "fps": 30 },
      "controls": { "enable_auto_exposure": 0, "exposure": 8500, "gain": 64 }
    }
  ]
}
```

저장 규칙:
- **값만 저장한다.** `min/max/step/default`는 디바이스가 진실이므로 저장하지 않는다
  (펌웨어가 바뀌면 범위도 바뀐다). 적용 시 현재 범위로 클램프한다 —
  RealSense는 이미 그렇게 한다([realsense_manager.py:581](../backend/app/services/realsense_manager.py#L581)).
- `readonly` 컨트롤은 저장 대상에서 제외.
- default와 같은 값은 생략(파일이 짧아지고 펌웨어 차이에 강해진다). 단 `auto_*` 스위치는
  default와 같아도 **항상 명시 저장** — 종속 값 적용 순서를 결정하는 축이기 때문이다.

### 적용 순서 — 조용한 실패를 막는 유일한 방법

원인 (7) 때문에 컨트롤은 dict 순회로 밀어 넣으면 안 된다. 3그룹으로 나눠 순서를 강제한다.

```
1단계  스위치를 수동으로 : auto_exposure(→수동), white_balance_automatic=0,
                          focus_automatic_continuous=0, enable_auto_exposure=0(RS)
2단계  종속 값          : exposure_time_absolute, white_balance_temperature,
                          focus_absolute, gain, exposure(RS)
3단계  독립 값          : brightness, contrast, saturation, sharpness, hue, gamma
4단계  스위치 복귀      : 프로파일이 auto를 원하는 항목만 마지막에 auto로
```

> v4l2 `auto_exposure`는 값이 반직관적이다 — **1 = Manual Mode, 3 = Aperture Priority(자동)**.
> "1이니까 자동" 으로 읽고 코드를 짜면 정확히 거꾸로 동작한다. 그룹 분류는 이름이 아니라
> `type==3`(menu)/`type==2`(bool) + 알려진 이름 테이블로 판정한다.

적용 후 **read-back 검증**: 다시 읽어 값이 다르면 1회 재시도, 그래도 다르면
`locked`(inactive/readonly 때문) / `failed`(진짜 실패)로 분류해 리포트에 남긴다.
`locked`는 실패로 세지 않는다 — 자동 모드가 켜진 정상 상태일 수 있다.

### 스트림 설정(해상도/FPS)을 실제로 반영

원인 (4)(5)를 같이 고쳐야 프로파일이 의미를 갖는다.

1. `_open_cap`에서 프로파일 값이 있으면 `cap.set(CAP_PROP_FRAME_WIDTH/HEIGHT/FPS/FOURCC)`를
   **먼저** 호출하고, 그 다음 읽어온 실제 값은 `actual_*`에 따로 담는다. `self.width` 등을
   덮어쓰지 않는다 — 요청값과 실제값을 구분해야 UI가 "요청 1280, 실제 640(미지원)"을 보여줄 수 있다.
2. `_build_cameras_json` / `_build_cameras_draccus`가 하드코딩 대신 프로파일의 `stream`을 읽는다.
3. 녹화 프론트의 `camWidth/camHeight/fps`는 **프로파일 값으로 초기화**하고, 사용자가 바꾸면
   "프로파일과 다름" 배지를 띄운다. 값의 출처를 한 곳으로 모으는 게 목적이다.

---

## 트리거 — 언제 적용하는가

| # | 시점 | 코드 지점 | 대상 |
|---|---|---|---|
| 1 | 서버 부팅 | [main.py:39](../backend/app/main.py#L39) `restore_session` 직후 | 등록된 전부 |
| 2 | 스캔/연결 후 | [cameras.py:37-82](../backend/app/routers/cameras.py#L37-L82) | 새로 매칭된 카메라 |
| 3 | 하드웨어 리셋 후 | [cameras.py:155-171](../backend/app/routers/cameras.py#L155-L171) → 재열거 대기 후 | 해당 카메라 |
| 4 | **녹화 시작 직전** | [recording.py:95](../backend/app/routers/recording.py#L95) 해제 직후, [L115](../backend/app/routers/recording.py#L115) `build_record_args` 전 | 매핑된 카메라만 |
| 5 | **추론 시작 직전** | [models.py:312](../backend/app/routers/models.py#L312), [L333](../backend/app/routers/models.py#L333) `_release_all_cameras()` 직후 | 매핑된 카메라만 |
| 6 | (옵션) 실행 후 재확인 | 프로세스 `RUNNING` 전이 + N초 | **OpenCV 전용** |

4·5가 사용자가 요청한 "실행 전 한 번 더"다. 둘 다 같은 함수를 부른다:

```python
# app/services/camera_profiles.py
report = camera_profiles.apply_before_run(
    camera_ids=[...],        # 실행에 실제로 쓰는 카메라만
    budget_s=2.0,            # 총 예산. 초과분은 스킵하고 경고
)
logger.info("camera profile applied: %s", report.summary())
# 실패해도 절대 예외를 올리지 않는다 (best-effort)
```

### 왜 "해제 직후, subprocess 기동 전"인가

- **v4l2 컨트롤**은 스트리밍 여부와 무관하게 `open(O_RDWR)` + ioctl로 설정할 수 있고
  ([camera_manager.py:407-416](../backend/app/services/camera_manager.py#L407-L416)),
  값은 디바이스에 남는다. 그래서 우리가 닫은 뒤 LeRobot이 열어도 그대로 유지된다.
- **RealSense**는 반대다. 파이프라인이 떠 있으면 옵션 접근이 `op_lock`·디바이스 경합을 만들고,
  최악의 경우 D405가 커널 D-state로 물린다. `release_all()` 이후 subprocess 기동 이전의
  **좁은 창**이 유일하게 안전한 구간이다.

### (6) 실행 후 재확인은 OpenCV 한정 — RealSense는 절대 금지

일부 UVC 웹캠은 스트림 시작 시 자동 노출을 다시 켠다. 이 경우 pre-start 적용만으로는 부족해서,
프로세스가 뜨고 프레임이 안정된 뒤 v4l2 컨트롤만 다시 밀어 넣는 단계가 필요하다.
다른 프로세스가 스트리밍 중이어도 `VIDIOC_S_EXT_CTRLS`는 안전하다(`S_FMT`/`STREAMON`이 아니다).

**RealSense는 여기서 제외한다.** 실행 중 RealSense UVC 컨트롤 질의는 D405를 D-state로 물리게
하는 대표 원인이고, 그래서 지금도 `_guard_device_access`가 실행 중 컨트롤 접근을 전부 막고 있다
([cameras.py:18-34](../backend/app/routers/cameras.py#L18-L34), [L198-L205](../backend/app/routers/cameras.py#L198-L205)).
이 가드는 유지하고, (6)만 내부 경로에서 `cam_type == "opencv"` 조건으로 우회한다.
기본값은 **off**, 프로파일별 `apply.post_start_recheck` 플래그로 켠다.

---

## 안전 가드 — 시작을 막지 않는다

| 가드 | 규칙 |
|---|---|
| 예외 전파 금지 | 프로파일 적용 실패로 녹화/추론이 시작 안 되는 일은 없어야 한다. 전부 best-effort + 경고 |
| 시간 예산 | 카메라당 timeout(v4l2 1.5s / RS 3s는 `_run_guarded`가 이미 강제), 총 2초. 초과 시 남은 카메라 스킵 |
| executor 오프로드 | 모든 디바이스 I/O는 스레드풀에서. 이벤트 루프를 절대 블로킹하지 않는다 (D405 hang으로 서버 전체가 멈춘 전례) |
| E-stop 무간섭 | 적용은 시작 요청 핸들러 안에서 끝난다. 프론트에 모달/`confirm`을 띄우지 않는다 — heartbeat가 막히면 2초 타임아웃으로 추론이 강제 종료된다. 결과는 로그/토스트로만 |
| 가드 우회 최소화 | `_guard_device_access`는 그대로. 내부 apply는 프로세스 기동 *전*이라 애초에 충돌하지 않고, 예외는 (6)뿐 |

---

## API / WS

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/cameras/profiles` | 목록 + 활성 프로파일 이름 |
| POST | `/api/cameras/profiles/save` | `{name, camera_ids?}` — 현재 디바이스 값을 읽어 저장 |
| POST | `/api/cameras/profiles/apply` | `{name?, camera_ids?}` — 수동 적용, `ApplyReport` 반환 |
| POST | `/api/cameras/profiles/active` | `{name}` — 자동 적용에 쓸 프로파일 지정 |
| DELETE | `/api/cameras/profiles/{name}` | 삭제 |
| GET | `/api/cameras/profiles/report` | 마지막 적용 결과 (실행 페이지 배지용) |

WS 이벤트 `{"type": "camera_profile", "data": ApplyReport}` 추가
([ws.py](../backend/app/routers/ws.py)의 기존 브로드캐스트 패턴과 동일).

```jsonc
// ApplyReport
{ "profile": "주간-형광등", "at": "...", "trigger": "record-start",
  "cameras": [
    { "key": "usb:4-3:1.0", "dev": "/dev/video2", "matched": true,
      "applied": 7, "locked": 1, "failed": 0, "skipped_reason": null,
      "details": [{ "name": "exposure_time_absolute", "want": 312, "got": 312, "ok": true }] }
  ] }
```

---

## UI

- **카메라 페이지**: 컨트롤 패널 하단에 `프로파일로 저장 / 적용 / 활성 지정`.
  목록 UI는 로봇 프리셋([RobotsPage.tsx:465-486](../frontend/src/pages/RobotsPage.tsx#L465-L486))
  패턴을 그대로 재사용한다. 프로파일은 조명 조건별로 여러 개 두는 게 실사용에 맞다(주간/야간/형광등).
- **녹화·추론 페이지**: `적용될 프로파일: 주간-형광등` 배지 + 시작 후 마지막 적용 결과
  (`7 적용 / 1 잠김 / 0 실패`). 실패가 있을 때만 색을 바꾼다.
- 프로파일에 없는 카메라를 매핑하면 "프로파일 없음(현재 디바이스 값 사용)" 회색 표시.

---

## 작업 분해

1. `backend/app/services/camera_profiles.py` 신규 — 스토어(CRUD) + `profile_key` 해석 +
   `CameraProfileApplier`(그룹 순서 적용, read-back 검증, 예산 관리) + `apply_before_run`
2. `camera_manager.py` — `profile_key` 프로퍼티, `_open_cap`의 `cap.set` + `actual_*` 분리,
   `restore_session`을 프로파일 경유로 전환(세션은 "등록 여부"만 들고 값은 프로파일이 갖는다)
3. `routers/cameras.py` — 프로파일 엔드포인트 6개
4. 트리거 배선 — `main.py`(부팅), `cameras.py`(스캔/리셋), `recording.py`(L95 이후),
   `models.py`(L312·L333 이후). policy server/gRPC 경로도 같은 함수
5. `models.py` `_build_cameras_json` / `_build_cameras_draccus` — 하드코딩 제거, 프로파일 주입
6. 프론트 — `CamerasPage` 프로파일 UI, `RecordingPage`/`InferencePage` 배지,
   `api.ts` 타입, WS `camera_profile` 핸들링
7. 마이그레이션 — 기존 `camera_session.json`의 `config`를 첫 실행 시 `default` 프로파일로 승격

---

## 검증

```bash
# 실제 디바이스 값 확인 (프로파일이 먹었는지 판정하는 기준)
v4l2-ctl -d /dev/video2 --list-ctrls

cd frontend && npm run build     # 타입 검증은 반드시 build (tsc --noEmit은 no-op)
```

시나리오:
1. 노출/WB 조정 → 서버 재시작 → 값 유지되는가
2. USB 리바인딩(`/dev/video` 번호 변경) → `usb_port` 매칭으로 값 유지되는가
3. 녹화 시작 → 실행 중 `v4l2-ctl --list-ctrls`로 프로파일 값이 살아있는가
4. 자동 노출이 켜진 상태로 저장 → 복원 시 종속 값이 `locked`로 리포트되고 실패로 세지 않는가
5. D405 연결 상태에서 녹화 시작 → 적용이 예산(2초) 안에 끝나고, **적용이 실패해도 녹화는 시작**되는가
6. 하드웨어 리셋 → 재스캔 → 자동 재적용되는가

---

## 먼저 정해야 할 것

| 항목 | 선택지 | 권장 |
|---|---|---|
| 프로파일 단위 | 카메라별 파일 / 세트 1파일 | **세트 1파일** — 조명 조건 단위로 통째 스위칭하는 게 실사용 패턴 |
| 자동 적용 기본값 | on / off | **on**, 실패는 경고만 |
| 세션 vs 프로파일 | 통합 / 분리 | **분리** — 세션=등록 상태·활성 프로파일 이름, 프로파일=설정값 |
| post-start 재확인 | 기본 on / off | **기본 off**, 프로파일 플래그로 opt-in (OpenCV 전용) |
| 해상도 오버라이드 | 프론트 허용 / 프로파일 고정 | **허용하되 표시** — 프로파일과 다르면 배지 |

---

## 상태

설계안. 구현 착수 전.
