# 카메라 설정 프로파일 + 연결 시 자동 적용

카메라 조정값(해상도/FPS + 노출·화이트밸런스 등 컨트롤)을 **이름 붙은 프로파일**로 저장하고,
**장치를 여는 유일한 주체인 데몬(camerad/rsd)이 연결할 때** 적용한다.

> 원래 제목은 "실행 전 자동 재적용"이었다. 녹화·추론 subprocess가 카메라를 열기 직전에
> 한 번 더 밀어 넣는다는 뜻이었는데, 데몬 분리 후 **subprocess는 장치를 열지 않는다.**
> 아래 상태표 참조.

---

## ⚠ 이 문서는 데몬 분리(3b-5) 이전에 쓰였다

아래 7개 원인 중 **넷이 구조 개편으로 사라졌다.** 원인 분석은 그대로 두되(왜 그랬는지가
설계 근거로 남아야 한다) 각 절 머리에 지금 상태를 붙였다. 남은 것만 요약하면:

| 원인 | 상태 | 근거 |
|---|---|---|
| (1) 컨트롤 값이 저장되지 않는다 | ❗**그대로** | `save_session`은 여전히 스트림 필드만 |
| (2) 세션 키가 `/dev/videoN` | ◐ **절반** | RealSense는 `rs_<시리얼>_<스트림>`이라 이미 안정. v4l2만 남음 |
| (3) 리셋이 컨트롤을 되돌린다 | ❗**그대로** | (1)의 따름 결과 — 되돌릴 값이 없다 |
| (4) 해상도/FPS가 장치에 전달 안 됨 | ☑ **해결** | `prepare_cameras(w,h,fps)` → 데몬 `connect()` → `resolve()` |
| (5) 실행 경로가 설정을 안 본다 | ☑ **해결** | 하드코딩 제거. 요청값은 `info()["want"]`, 실제값은 별도로 온다 |
| (6) 해제 후 다시 맞추는 단계가 없다 | ☑ **소멸** | shm에선 **해제하지 않는다** — 데몬이 장치를 계속 쥔다 |
| (7) 자동 모드 순서 | ❗**그대로** | 순수 v4l2/RS 의미론이라 구조와 무관 |

그래서 **트리거 6개 배선도 같이 소멸했다.** 게이트웨이가 장치를 놓지 않으므로
"실행 직전에 다시 밀어 넣는" 창 자체가 없다. 적용 지점은 **데몬의 `connect()` 안 한 곳**이다.
아래 "트리거" 절은 이 이유로 통째로 폐기된 설계다.

---

## 왜 자꾸 초기화되는가 — 원인은 하나가 아니다

"초기화된다"는 증상 뒤에 서로 다른 원인이 7개 있다. 프로파일 파일 하나 만든다고 전부
해결되지 않으므로 먼저 분리한다.

### (1) 컨트롤 값은 애초에 저장되지 않는다 — ❗**남아 있다**

> 지금도 [camera_manager.py:282-300](../backend/app/services/camera_manager.py#L282-L300)의
> `save_session`은 `width/height/fps/color_mode/rotation/fourcc`만 저장하고,
> [cameras.py:166-177](../backend/app/routers/cameras.py#L166-L177)의 `/control`은
> 데몬에 쓰기만 한다. **이것이 이 기능의 유일한 핵심으로 남았다.**


[camera_manager.py:584-601](../backend/app/services/camera_manager.py#L584-L601)의 `save_session`이
저장하는 건 `width/height/fps/color_mode/rotation/fourcc` 뿐이다. 사용자가 실제로 오래
만지는 값 — 밝기, 노출, 화이트밸런스, 게인, 포커스 — 은 **디바이스에만 있는 휘발성 상태**고
디스크에 흔적이 없다. [cameras.py:142-152](../backend/app/routers/cameras.py#L142-L152)의
`/control`도 디바이스에 쓰기만 하고 어디에도 기록하지 않는다.

→ 서버 재시작, USB 재열거, 하드웨어 리셋 중 **아무거나** 한 번이면 전부 날아간다.

### (2) 세션 키가 `/dev/videoN`이다 — ◐ **RealSense는 해결, v4l2는 남음**

> RealSense cam_id가 `rs_<시리얼>_<스트림>`(예: `rs_250122070363_color`)로 바뀌면서
> 재열거·리셋에 불변이 됐다. 반면 v4l2는 여전히 `/dev/videoN`이고,
> [camera_manager.py:320-324](../backend/app/services/camera_manager.py#L320-L324)의 복원은
> 그 id로 매칭한다. `usb_port`는 [L54](../backend/app/services/camera_manager.py#L54)·[L185](../backend/app/services/camera_manager.py#L185)에서
> **수집은 하고 있으나 매칭에 쓰지 않는다.**

[camera_manager.py:302-334](../backend/app/services/camera_manager.py#L302-L334)에서 세션 복원은
`cam_id`(=`/dev/video2`)로 매칭한다. USB가 재열거되면 video 번호가 바뀌므로
`Session camera ... not found in scan, skipping`으로 조용히 버려진다. USB 컨트롤러가 죽어
리바인딩한 뒤(이 저장소에서 실제로 겪은 시나리오) 설정이 통째로 사라지는 경로가 이것이다.

정작 안정 식별자는 이미 수집하고 있다 — OpenCV는 `usb_port`
([camera_manager.py:419-430](../backend/app/services/camera_manager.py#L419-L430)),
RealSense는 `serial`. 쓰지 않고 있을 뿐이다.

### (3) 디바이스 재열거·리셋이 컨트롤을 드라이버 기본값으로 되돌린다 — ❗**남아 있다**

> (1)의 따름 결과다. 되돌릴 값이 저장돼 있지 않으니 복원 단계를 만들 수도 없다.
> (1)을 고치면 이건 "데몬이 재열거를 감지하면 다시 적용" 한 줄이 된다.

`/cameras/reset-device`([cameras.py:155-171](../backend/app/routers/cameras.py#L155-L171))는
펌웨어 파워사이클이다. 복구 수단으로는 맞지만 컨트롤은 전부 default로 돌아간다. 프론트는
4초 뒤 재스캔만 하고([CamerasPage.tsx:137-143](../frontend/src/pages/CamerasPage.tsx#L137-L143))
값을 되돌려 놓는 단계가 없다.

### (4) 해상도/FPS는 설정해도 디바이스에 전달되지 않고, 오히려 덮어써진다 — ☑ **해결됨**

> `prepare_cameras(mapping, purpose=…, width, height, fps)`
> ([camera_config.py:201](../backend/app/services/camera_config.py#L201))가 요청 프로파일을
> 데몬 `connect(cam_id, w, h, fps)`까지 넘기고, rsd의 `resolve()`가 장치가 낼 수 있는
> 조합으로 협상한다. `info()`는 **요청값(`want`)과 실제 열린 값을 따로** 돌려주므로
> 아래 "actual_* 분리"가 요구하던 것이 이미 그 형태다.
>
> 이 결함의 증상이 D405가 기본값(848×480@10)으로만 열려 녹화 루프가 10Hz에 묶이고
> 매 프레임 "루프가 느리다" 경고가 뜬 것이었다. 지금은 뜨지 않는다.

`update_config`([camera_manager.py:75-78](../backend/app/services/camera_manager.py#L75-L78))는
파이썬 필드만 바꾼다. 그리고 `_open_cap`
([camera_manager.py:91-93](../backend/app/services/camera_manager.py#L91-L93))이
연결/probe 때마다 **캡처에서 읽어온 값으로 `self.width/height/fps`를 덮어쓴다**.
`cap.set(CAP_PROP_*)` 호출은 아예 없다.

→ UI에서 1280×720을 입력해도 다음 probe 한 번이면 640×480으로 되돌아간다. 이건 저장의
문제가 아니라 **적용이 없는** 문제다.

### (5) 실행 경로가 설정을 안 본다 — 해상도가 하드코딩되어 있다 — ☑ **해결됨**

> `models.py`의 `640/480/30` 하드코딩은 사라졌다. 녹화는 `camera_width/height/fps`가
> 요청 바디부터 `prepare_cameras`까지 이어지고
> ([recording.py:105-113](../backend/app/routers/recording.py#L105-L113)),
> shm 경로에서는 **발행자(데몬)가 정한 값**이 곧 wrapper가 읽는 값이라
> 경로별로 어긋날 여지 자체가 없다. 아래 표는 개편 전 기록이다.

| 경로 | 코드 | 문제 |
|---|---|---|
| 추론(gRPC) | [models.py:97-116](../backend/app/routers/models.py#L97-L116) | `width: 640, height: 480, fps: 30` **문자열 하드코딩** |
| 추론(로컬 wrapper) | [models.py:119-146](../backend/app/routers/models.py#L119-L146) | width/height/fps를 **아예 안 넣음** (LeRobot 기본값에 맡김) |
| 녹화 | [RecordingPage.tsx:104-125](../frontend/src/pages/RecordingPage.tsx#L104-L125) | 프론트가 별도 `camWidth/camHeight/fps` 상태로 조립 |

카메라 페이지에서 뭘 하든 실행 인자에는 반영되지 않는다. 녹화만 프론트 로컬 상태를 쓰는데,
그건 카메라 페이지의 설정과 다른 값이다.

### (6) 녹화/추론 직전에 카메라를 놓는데, 다시 맞춰주는 단계가 없다 — ☑ **소멸**

> shm 전송에서는 **놓지 않는다.** `prepare_cameras`의 주석이 그 뒤집힘을 적어 두었다 —
> `direct`는 해제해야 하고 `shm`은 **계속 쥐어야** 한다. 데몬이 장치의 유일한 소유자이고
> 세그먼트가 곧 임대권이므로, "해제 → 재적용"이라는 창이 존재하지 않는다.
> 컨트롤을 적용할 곳은 데몬의 `connect()` 안이다.

아래는 개편 전 기록이다.

[recording.py:85-95](../backend/app/routers/recording.py#L85-L95)와
[models.py:56-71](../backend/app/routers/models.py#L56-L71)은 subprocess 기동 전에
OpenCV·RealSense를 강제 해제한다(USB 대역폭 경합 방지 — 필요한 동작이다). 문제는 그 다음
LeRobot이 **자기 파이프라인으로 디바이스를 새로 여는데**, 그 사이에 값을 다시 밀어 넣는
지점이 없다는 것. RealSense는 파이프라인 재시작에서 옵션이 되돌아가는 경우가 있어
특히 티가 난다.

### (7) 자동 모드 종속성 — 순서를 안 지키면 "적용했는데 안 먹은" 것처럼 보인다 — ❗**남아 있다**

> v4l2/librealsense의 의미론이라 프로세스 구조와 무관하다. 다만 적용 코드가 데몬
> 안으로 들어가므로, 순서 강제도 **게이트웨이가 아니라 camerad/rsd에서** 한다.


`auto_exposure`가 자동이면 `exposure_time_absolute`는 커널이 `inactive` 플래그를 세우고
쓰기를 무시한다. UI는 이걸 표시만 한다
([CamerasPage.tsx:274-277](../frontend/src/pages/CamerasPage.tsx#L274-L277)).
값을 복원할 때 **스위치를 먼저 수동으로 돌리지 않으면** 종속 값이 조용히 버려진다.
`white_balance_automatic` / `white_balance_temperature`, `focus_automatic_continuous` /
`focus_absolute`도 같은 관계다.

---

## 설계 — 트리거층이 없어져 2층이 된다

```
저장층   presets.py (domain="camera") : config_dir/presets/camera/<name>.json
             ↑ CRUD·검증·마이그레이션은 **이미 있다**. 도메인 스키마만 추가
적용층   데몬의 connect() 안 : 안정 키 매칭 → 순서 있는 적용 → read-back 검증
             ↑ camerad/rsd 가 장치를 여는 그 자리
```

저장층은 새로 만들지 않는다 — [parameter-presets](parameter-presets.md)의 공통 스토어가
로봇·학습·추론 프리셋을 이미 담고 있고, 그 문서의 **6단계가 정확히 이 흡수**다.

~~트리거층~~ — 게이트웨이가 장치를 놓지 않으므로 "실행 직전에 다시 밀어 넣는" 훅이
필요 없다. **장치가 열리는 순간은 데몬의 `connect()` 하나뿐이고**, 컨트롤 적용은
그 안에서 스트림 시작과 같은 트랜잭션으로 끝난다. 개편 전 설계가 훅 6개를 요구했던 건
소유자가 여럿이라 열림 시점도 여럿이었기 때문이다.

프로파일 **값**은 게이트웨이가 갖고(디스크·API·UI), **적용**은 데몬이 한다.
연결 RPC에 프로파일을 실어 보내는 방식이 (4)에서 해상도로 이미 검증됐다 —
`connect(cam_id, w, h, fps)`에 `controls`를 하나 더 붙이는 모양이다.

### 안정 식별자 (`profile_key`)

| 타입 | 키 | 근거 |
|---|---|---|
| RealSense | ~~`rs:<serial>:<stream>`~~ → **cam_id 그대로** | cam_id가 이미 `rs_<serial>_<stream>`이다. 별도 키를 만들 이유가 없다 |
| OpenCV(USB) | `usb:<usb_port>` (예: `usb:4-3:1.0`) | 물리 포트 고정. 동일 모델 2대 구분 가능 |
| OpenCV(포트 미상) | `name:<sysfs name>` | 폴백 |

즉 `profile_key`가 필요한 건 **v4l2 카메라뿐이다.**

`/dev/videoN`은 키가 아니라 **매칭 결과**다. 프로파일에는 `last_dev`로 참고만 기록하고,
매칭은 `usb_port → name → last_dev` 순으로 시도한다. 같은 모델을 다른 포트에 꽂았을 때
잘못 적용되는 걸 막으려면 `name` 폴백은 후보가 1개일 때만 허용한다.

### 프로파일 스키마

`config_dir/presets/camera/<name>.json` — 공통 `Preset` 봉투(`domain`/`name`/`scope`/
`version`/`updated_at`/`note`)에 아래가 `values`로 들어간다. `scope`는 **`device`**
(카메라는 이 기계에 물린 물건이라 공유하면 틀린다).

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

### ~~스트림 설정(해상도/FPS)을 실제로 반영~~ — ☑ **끝났다**

원인 (4)(5)가 이미 해결됐으므로 아래 3항목은 전부 불필요하다. 1은 데몬의 `resolve()` +
`info()`의 `want`/실제값 분리로, 2는 하드코딩 제거로, 3은 shm에서 발행자가 값을 정하는
구조로 각각 대체됐다. 프로파일이 스트림 필드를 갖는 것은 유지한다 — **연결 요청의
기본값**으로 쓰인다.

<details><summary>개편 전 계획 (보관)</summary>

1. `_open_cap`에서 프로파일 값이 있으면 `cap.set(CAP_PROP_FRAME_WIDTH/HEIGHT/FPS/FOURCC)`를
   **먼저** 호출하고, 그 다음 읽어온 실제 값은 `actual_*`에 따로 담는다. `self.width` 등을
   덮어쓰지 않는다 — 요청값과 실제값을 구분해야 UI가 "요청 1280, 실제 640(미지원)"을 보여줄 수 있다.
2. `_build_cameras_json` / `_build_cameras_draccus`가 하드코딩 대신 프로파일의 `stream`을 읽는다.
3. 녹화 프론트의 `camWidth/camHeight/fps`는 **프로파일 값으로 초기화**하고, 사용자가 바꾸면
   "프로파일과 다름" 배지를 띄운다. 값의 출처를 한 곳으로 모으는 게 목적이다.

</details>

---

## ~~트리거 — 언제 적용하는가~~ — ☒ **폐기**

**이 절 전체가 소멸했다.** 6개 트리거는 "장치를 여러 주체가 여닫는다"는 전제에서 나온 것인데,
데몬 분리로 여는 주체가 하나가 됐다. 적용 지점은 `camerad`/`rsd`의 `connect()` 안이며,
게이트웨이 쪽 훅은 **하나도 필요 없다**. 아래는 개편 전 기록이다.

<details><summary>개편 전 트리거 설계 (보관)</summary>

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

</details>

> **다만 "스트림 시작 시 자동 노출이 다시 켜지는 UVC 웹캠"이라는 실제 현상은 남는다.**
> 이제는 그것도 camerad 안의 문제다 — 스트림을 띄운 주체가 camerad이므로, 프레임이
> 안정된 뒤 컨트롤을 한 번 더 밀어 넣는 것도 camerad가 자기 안에서 한다.
> RealSense 금지 조항은 근거가 약해졌다(D-state가 나도 rsd만 멈추고 웹은 산다)
> 그러나 **복구가 여전히 USB 리바인딩이므로 금지는 유지한다.**

---

## 안전 가드 — 시작을 막지 않는다

| 가드 | 규칙 |
|---|---|
| 예외 전파 금지 | 프로파일 적용 실패로 연결이 실패하는 일은 없어야 한다. 전부 best-effort + 경고 |
| 시간 예산 | 카메라당 timeout, 총 2초. 초과 시 남은 컨트롤 스킵 |
| ~~executor 오프로드~~ | ☑ **소멸** — 디바이스 I/O가 데몬 프로세스 안에 있다. 게이트웨이 이벤트 루프에서 애초에 돌지 않는다 |
| ~~E-stop 무간섭~~ | ☑ **소멸** — 적용이 시작 요청 경로에 없다. 연결 시점에 끝난다 |
| 가드 우회 최소화 | `_guard_device_access`(현 `blocked_reason(CAMERA_ACCESS)`)는 그대로 — 실행 중 사용자가 컨트롤을 만지는 것을 막는 가드이고, 데몬 내부 적용과는 층이 다르다 |

---

## API / WS

CRUD는 **공통 프리셋 API를 그대로 쓴다** — `GET/POST/DELETE /api/presets/camera`.
카메라 고유 동작만 새로 만든다:

| 메서드 | 경로 | 설명 |
|---|---|---|
| ~~GET~~ | ~~`/api/cameras/profiles`~~ | → `GET /api/presets/camera` |
| POST | `/api/cameras/profiles/capture` | `{name, camera_ids?}` — **현재 디바이스 값을 읽어** 프리셋으로 저장. 값의 출처가 장치라 공통 CRUD로는 안 된다 |
| POST | `/api/cameras/profiles/apply` | `{name?, camera_ids?}` — 수동 적용, `ApplyReport` 반환 |
| POST | `/api/cameras/profiles/active` | `{name}` — 연결 시 자동 적용할 프로파일 지정 |
| ~~DELETE~~ | ~~`/api/cameras/profiles/{name}`~~ | → `DELETE /api/presets/camera/{name}` |
| GET | `/api/cameras/profiles/report` | 마지막 적용 결과 (카메라 페이지 배지용) |

여기에 더해 **데몬 RPC가 하나 필요하다**: `apply_controls(cam_id, controls)` —
`connect()`가 내부에서 부르는 것과 같은 함수를 밖에서도 부를 수 있게. 수동 적용과
"연결 시 자동 적용"이 같은 코드를 타야 한다.

WS 이벤트 `{"type": "camera_profile", "data": ApplyReport}` 추가
([ws.py](../backend/app/routers/ws.py)의 기존 브로드캐스트 패턴과 동일).

```jsonc
// ApplyReport — trigger 는 이제 "connect" | "manual" | "reset" 뿐이다
{ "profile": "주간-형광등", "at": "...", "trigger": "connect",
  "cameras": [
    { "key": "usb:4-3:1.0", "dev": "/dev/video2", "matched": true,
      "applied": 7, "locked": 1, "failed": 0, "skipped_reason": null,
      "details": [{ "name": "exposure_time_absolute", "want": 312, "got": 312, "ok": true }] }
  ] }
```

---

## UI

- **카메라 페이지**: 컨트롤 패널 하단에 [PresetBar](../frontend/src/components/PresetBar.tsx)를
  `domain="camera"`로 얹는다 — 학습·추론 페이지가 쓰는 그 컴포넌트다. 새로 그릴 것은
  `프로파일로 저장`(현재 장치 값 캡처)과 적용 결과 배지뿐이다.
  프로파일은 조명 조건별로 여러 개 두는 게 실사용에 맞다(주간/야간/형광등).
- ~~**녹화·추론 페이지** 배지~~ — 적용이 실행 경로에 없으므로 뺀다. 카메라 페이지에만 둔다.
- 프로파일에 없는 카메라를 매핑하면 "프로파일 없음(현재 디바이스 값 사용)" 회색 표시.

---

## 작업 분해 — 7개에서 4개로

1. `presets.py`에 `camera` 도메인 스키마 추가 + v4l2용 `profile_key` 해석.
   **스토어도 적용 로직도 새로 만들지 않는다** — CRUD는 있고, 적용은 데몬에 있다
2. `camerad`/`rsd` — `apply_controls(cam_id, controls)`: 그룹 순서 적용 + read-back 검증 +
   예산. `connect()`가 스트림을 연 직후 이걸 부른다
3. `routers/cameras.py` — 프로파일 엔드포인트 6개. `/connect` 핸들러가 활성 프로파일의
   `controls`를 실어 보낸다
4. 프론트 — `CamerasPage` 프로파일 UI, `api.ts` 타입, WS `camera_profile` 핸들링

<details><summary>개편 전 7개 항목 (보관)</summary>

2'. `camera_manager.py` — `_open_cap`의 `cap.set` + `actual_*` 분리 → **(4)에서 해결됨**
4'. 트리거 배선 6곳 → **소멸**
5'. `models.py` 하드코딩 제거 → **(5)에서 해결됨**
7'. `camera_session.json` → `default` 프로파일 승격 마이그레이션 → 세션에 컨트롤 값이
    애초에 없으므로 승격할 것이 없다. **불필요**

</details>

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
3. ~~녹화 시작 중 값이 살아있는가~~ → **데몬 재시작**(`systemctl --user restart camerad`) 후
   값이 살아있는가. 녹화는 이제 장치를 여닫지 않으므로 시험 대상이 아니다
4. 자동 노출이 켜진 상태로 저장 → 복원 시 종속 값이 `locked`로 리포트되고 실패로 세지 않는가
5. 적용이 예산(2초) 안에 끝나고, **적용이 실패해도 연결은 성공**하는가
6. 하드웨어 리셋 → 재연결 → 자동 재적용되는가

---

## 먼저 정해야 할 것

| 항목 | 선택지 | 권장 |
|---|---|---|
| 프로파일 단위 | 카메라별 파일 / 세트 1파일 | **세트 1파일** — 조명 조건 단위로 통째 스위칭하는 게 실사용 패턴 |
| 자동 적용 기본값 | on / off | **on**, 실패는 경고만 |
| 세션 vs 프로파일 | 통합 / 분리 | **분리** — 세션=등록 상태·활성 프로파일 이름, 프로파일=설정값 |
| post-start 재확인 | 기본 on / off | **기본 off**, 프로파일 플래그로 opt-in (OpenCV 전용) |
| 해상도 오버라이드 | 프론트 허용 / 프로파일 고정 | **허용하되 표시** — 프로파일과 다르면 배지 |
| ~~저장 위치~~ | 전용 `camera_profiles/*.json` / 공통 `presets` 스토어 | ☑ **결정됨 — 공통 스토어**. [parameter-presets](parameter-presets.md) 1~5단계가 이미 끝났고, 남은 6단계가 곧 이 작업이다 |

---

## 상태

☑ **구현 완료.** 데몬 분리 이후 남았던 셋을 전부 닫았다.

| | 어디 |
|---|---|
| (7) 자동 모드 순서 + read-back | [`cam/piper_cam/controls.py`](../cam/piper_cam/controls.py) — 순수 `plan()` + 범용 `apply_controls()`. **rsd 도 이 한 벌을 쓴다** |
| (1) 컨트롤 값 저장 | [`camera_profiles.py`](../backend/app/services/camera_profiles.py) — `presets` domain=`camera`, scope=`device` |
| (2) v4l2 안정 키 | `CameraInfo.profile_key` + `CameraManager.match_saved` — 세션 복원도 키로 매칭 |
| 적용 지점 | 두 허브의 `connect()` **안** — 게이트웨이 훅 0개 |

### 실기로 확인한 것 (D405, 2026-08-14)

1. 노출·WB 를 수동으로 맞추고 캡처 → 프리셋 파일에 값이 남는다
2. `controls/reset` 으로 전부 기본값으로 되돌린 뒤 수동 적용 → **적용 4 / 잠김 0 / 실패 0**,
   장치 값이 그대로 돌아왔다
3. 되돌린 뒤 **연결만 했는데** 값이 복원됐다 — 트리거 없이 `connect()` 하나로 끝난다

### 실기가 잡은 버그

**`depth_units` 가 `0` 으로 저장됐다.** RealSense 옵션에는 실수가 있고
(`depth_units` = 1e-4), 캡처가 `int()` 를 씌우고 있었다. 그 0 을 다시 밀어 넣었으면
**깊이 스케일이 0** 이 됐을 것이다. 두 가지를 고쳤다:

- 값 변환을 정수/실수 보존으로 (`_num`), 비교도 상대 오차로 (`_same`)
- **`depth_units` 는 아예 저장하지 않는다** — 픽셀값의 뜻을 정하는 **데이터셋 계약**이지
  조명 설정이 아니다. 깊이 인코딩은 rsd 가 소유하고 `meta/piper_cameras.json` 에 기록된다

### 아직 안 해본 것

**v4l2 웹캠에서의 확인.** 지금 이 기계에 물려 있는 게 RealSense 둘뿐이라
`auto_exposure` 가 menu(1=Manual, 3=자동)인 그 반직관적 경로는 단위 테스트로만 덮여 있다.
웹캠을 물리면 시나리오 1·2 를 그대로 돌려보면 된다.

## 후속 — 작업별 프로파일 (2026-09-02 구현)

수집·추론 화면에서 **그 작업이 쓸 프로파일을 따로 지정**할 수 있다. 시작 요청의
`camera_profile` 로 실려 가 `prepare_cameras`(연결) **뒤에** 1회 적용된다 —
학습 데이터와 같은 노출·색으로 추론해야 관측이 같은 분포다.

- **활성 프로파일은 안 바꾼다.** "이 작업은 이 기준"이지 기계의 기본 변경이 아니다.
  작업 중 재연결에는 활성이 다시 붙는데, 그 어긋남은
  [조명 감시](lighting-watch.md)가 밝기·색 급변으로 잡는다
- **없는 프로파일이면 시작을 거부한다** (400). 삭제한 프로파일 이름이 화면
  저장값에 남은 경우 — 기준 없이 찍히는 것보다 막는 게 낫다
- 코드: `camera_profiles.apply_for_task`, `routers/recording.py`·`routers/models.py`,
  프론트 `CameraProfilePicker` (tests/test_task_camera_profile.py)

## 후속 — 카메라 페이지 탭 분리 + 프로파일 편집기 (2026-09-02 구현)

카메라 페이지가 **장치 / 프로파일** 두 탭이 됐다. 장치 탭은 기존 그대로
(스캔·등록·설정 모달), 프로파일 탭(`CameraProfilesPanel`)이 기존 PresetBar 를
대체하며 **값 상세 편집**이 생겼다:

- 목록(활성 배지·메모·시각) · 캡처(장치값 → 저장+활성, 기존 동작 유지) ·
  지금 적용 · 활성 지정 · 삭제(활성이던 이름을 지우면 활성도 비운다 —
  댕글링 활성은 연결 때마다 경고를 반복한다)
- 항목(카메라)별로 스트림(W/H/fps/fourcc)과 컨트롤 이름·값을 고치고,
  컨트롤 추가/삭제, 항목 삭제. **match 는 안 고친다** — 재캡처의 몫이다
- ⚠ **적용·연결은 저장된 값 기준.** 편집 중(dirty)에는 화면이 그걸 말한다

## 검토 — 프로파일에 "반드시" 있어야 하는 것 (2026-09-02, **구현됨**)

프로파일의 존재 이유는 **재현**이다(노출·색이 다음 주에도 같게). 그 기준으로
코드를 검토하니, 지금 구조는 "장치 상태를 충실히 기록"하지만 **재현 가능한
상태인지는 안 따진다.** 계열별 필수 규칙:

| 계열 | 재현하려면 반드시 | 근거 (controls.py) |
|---|---|---|
| 노출 | AE 스위치 **수동값** + `exposure*`(+`gain`) | 자동이면 조명을 따라가 재현이 없다. 값이 없으면 아래 G2 |
| WB | AWB 스위치 **꺼짐** + `white_balance*` | 동일 |
| 초점 | (권장) AF 스위치 + `focus_absolute` | AF 헌팅 = 화질 변화 |
| match | `key` 또는 `last_dev` | `controls_for` 는 이름 폴백도 안 쓴다 — 없으면 어느 장치에도 안 붙는다 |
| stream | — | **아무도 안 읽는다** (아래 G4). 기록용이다 |

### 발견한 구멍

- **G1 — 자동 상태로 캡처해도 경고가 없다.** capture 는 `inactive` 를 안 보고,
  AE=자동인 채 캡처하면 프로파일이 "자동 모드"를 충실히 재현한다 — 조명이
  바뀌면 카메라가 따라가고, 그게 이 기능이 막으려던 바로 그 일이다.
- **G2 — default 와 같은 종속 값은 저장이 빠진다** (`capture` 의 규칙이 자동
  스위치에만 예외). 실측 시나리오: 야간 프로파일(노출 2000) 적용 → 주간
  프로파일(노출=default 라 미저장) 적용 → 스위치는 수동으로 돌아오지만
  **노출 2000 이 잔류한다.** 프로파일 전환이 완결되지 않는 재현성 구멍.
- **G3 — 검증이 어디에도 없다.** 편집기·캡처·작업 시작(camera_profile) 어느
  경로도 G1/G2 를 알려주지 않는다. 작업 시작의 `unmatched` (프로파일이 못
  덮는 카메라)도 에러가 아니라서 조용히 지나간다.
- **G4 — `stream` 은 죽은 데이터다.** 연결 해상도는 요청(`prepare_cameras`)이
  정하고 프로파일의 stream 을 읽는 코드가 없다 — 그런데 편집 UI 는 그걸
  고치게 해 준다(위 후속에서 만든 것 포함). 소비를 배선하거나 "기록용" 으로
  표시해야 한다.

### 구현 (2026-09-02, tests/test_profile_reproducibility.py)

1. ☑ capture: **자동 스위치의 종속 컨트롤은 default 와 같아도 저장**
   (`controls.DEPENDENT_CONTROLS`) → G2 폐쇄. 독립 값은 여전히 뺀다
2. ☑ `camera_profiles.validate(entries)` 순수 함수 + `POST /cameras/profiles/validate`
   (미저장 편집분도 받는다). 편집기가 400ms 디바운스로 "재현성 검토" 상자를
   그리고, 캡처 응답의 warnings 는 그 자리에서 시스템 메시지로 뜬다 → G1·G3
3. ☑ `apply_for_task` 가 `warnings` 를 싣는다 — unmatched(항목이 장치를 못 찾음)
   + **uncovered**(연결된 카메라를 프로파일이 안 덮음, 이름을 댄다). 시작
   응답(`camera_profile`)으로 나가 수집·추론 화면이 warn 으로 띄운다. 막지는
   않는다 — 일부 카메라만 다루는 프로파일도 정당하다. 문구는 백엔드가 만든다
4. ☑ stream: 편집 UI 라벨이 "스트림 (기록용)" — 연결에는 안 쓰인다고 말한다.
   소비 배선(프로파일 해상도로 연결)은 필요해지면 별도 결정
