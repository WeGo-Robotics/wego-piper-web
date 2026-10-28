# 플러그인 — 사람이 쓴 파이썬 스크립트를 웹에서 켜고 끈다

> 상태: **☑ 0~10단계 구현 (2026-10-27).** 8단계(멀티 인스턴스)는 7단계 뒤, 9·10단계(업데이트 설치·팔 상태 읽기)는
> 8단계 뒤 요청으로 더해졌다. 기획 확정 2026-10-16.
> 9단계 ☑: 업데이트 설치(§3-2) — `install`·`install/zip` 의 `replace`, 폴더만 바꾸고 `.runtime/` 보존, 의존은 바뀐 것만
> 무효, 목록 화면의 409 → "업데이트할까?" 논블로킹 확인. 10단계 ☑: 팔 상태 읽기(§7-5) — 매니페스트 `arms`, SDK
> `self.arm()`(shm 상태 구독·발행자 재시작 추종), 범위 `robot.read`(말단 자세), 상세의 팔 선택기.
> 1단계: `services/plugins.py`·`core/plugin_scopes.py`·`routers/plugins.py`(`/api/plugins/*`), authgate 의
> Bearer 분기, `Activity.PLUGIN`·`PLUGIN_GPU`, 번들 `plugins/hello`.
> 2단계: SDK [`plugin/piper_plugin`](../plugin/piper_plugin/)(`Plugin`·`MissionPlugin`·`Decision`·`robot`·`camera`·
> `detections`), `piper-plugin new|check|run` + 템플릿 셋, 번들 `detect-and-start`·`zone-guard`,
> [plugins/README.md](../plugins/README.md), 검출 페이로드·JPEG 인코딩이 SDK 로 합쳐짐,
> `GET /api/models/inference/policy`, 명령 큐(`commands` → `on_command`).
> 3단계 ☑: 번들 `plugins/recycling`(`MissionPlugin`), `services/orchestrator.py`·라우터·WS 타입·`Activity.ORCHESTRATOR`·
> VisionPage 루프 패널 삭제, 판단 스키마는 `services/judge_schema.py` 로, `/api/vision/judge` 가 `skills` 를 받음,
> 저널 `GET /api/plugins/{name}/runs`, ext API `missions` 가 플러그인 위에서 그대로 돈다(§12-2).
> 3b단계 ☑: 번들 `plugins/detector`, 로더가 `piper_plugin.detector` 로, 버스 메타가 발행자별
> (`piper:vision_meta:<publisher>`), `/api/vision/start·stop·status` 는 플러그인 위의 호환층(`publishers` 추가),
> `daemons/detectord.py`·`detector_loader.py` 삭제.
> 4단계 ☑: `/plugins` 목록(카드·zip/폴더 설치·GPU 경고) + `/plugins/:name` 상세(제목 옆 탭 설정·출력·로그,
> 시작·정지·재시작·명령 버튼·"UI 보기" 새 창), `PluginConfigForm`(SpecFields + text/select/model/dataset/camera),
> WS `plugin_log`·`plugin_state`, `POST /api/plugins/install/zip`, 상태바 활동 `plugin`·`plugin_gpu`.
> 5단계 ☑: `routers/plugin_ui.py` — `ui: ui/` 정적 서빙(SPA 폴백·경로 조작 거절), `ui: server` 유닉스 소켓
> 프록시(HTTP·WebSocket, `X-Forwarded-Prefix`·`X-Piper-Plugin`, 꺼져 있으면 안내 페이지), `ui: /경로` 는 리다이렉트.
> SDK `serve_ui(app)`·`PIPER_PLUGIN_UI_SOCKET`, 템플릿 `dashboard`(정적 + E-stop 버튼), 번들 `ui-demo`(FastAPI·WS),
> nginx·Vite `^/plugins/[^/]+/ui(/|$)` 블록. 실기: `hello`·`detector`(검출·프리뷰·발행자 메타) 를 새 게이트웨이에서 확인.
> 6단계 ☑: `python: venv | venv-isolated` 의존 설치(`POST /api/plugins/{name}/deps` — 별도 유닛, `.deps-ok` 표지,
> 설치 전 시작은 409), `resources.memory`·`cpu` → 유닛 속성 `MemoryMax`·`CPUQuota`(자식 폴백은 `limits_applied: false`),
> ext API `GET /api/ext/v1/plugins`, `POST /plugins/{name}/start|stop`(저장값 위에 본문).
> 7단계 ☑: 플러그인 간 입출력(§7-3) — 매니페스트 `inputs`(검출·토픽 슬롯)·`outputs.topics`, 버스 토픽
> `piper:plugin:topic:<이름>`(최신값·seq·TTL), SDK `publish`·`latest`·`input`·`input_detections`, 상세의 입력 선택기
> (살아 있는 소스, `GET /api/plugins/topics`)·토픽 출력, `inputs_live` 경고(막지 않는다), 번들 `size-grader`.
> 8단계 ☑: 멀티 인스턴스(§7-4) — 인스턴스 id `<폴더>:<라벨>`, 매니페스트 `multi_instance: true`,
> `POST /api/plugins/{name}/instances`, 목록 카드의 인스턴스 줄, 상세의 인스턴스 삭제. 소유자·토큰·런타임·버스 키·유닛이
> 전부 인스턴스 id 를 쓰고 기본 인스턴스 id = 폴더 이름이라 옛 호출자는 그대로 돈다. 요청: "카메라 스트림을 받아 개체 검출도 하고 REST API 로
> 추론도 시작할 수 있는 파이썬 스크립트를 플러그인 형태로 만들어 웹 UI 에서 실행."
> 이 문서는 그 요청을 **범용 플러그인 계층**으로 넓힌 설계다. 열린 질문 다섯은 §10 에
> 결정으로 닫혔고, 플러그인 자체 화면(대시보드·입력·설정 페이지)의 **서빙 포트는 게이트웨이와 같은
> 포트**(프록시, §8-2) 다. **에피소드 오케스트레이터는 제거하고 플러그인으로 대체한다**
> (2026-10-20 결정, §12). **검출기(yolod)도 플러그인으로 이관한다** (2026-10-22 결정, §13) —
> 검출기를 여러 개 동시에 돌리는 길이 그것이다. **플러그인 UI 는 "UI 보기" 버튼 → 새 창**(iframe 폐기,
> §8-2), **`yolo` 라는 단어는 프로젝트 전체에서 뺀다**(§14), 남은 결정 일곱은 **권고대로**(§15) —
> 전부 2026-10-22 결정. 열린 질문 없음.

## 0. 요지

플러그인은 **폴더 하나** — `plugin.yaml`(이름·설정 항목·권한) + `main.py`(스크립트) +
`requirements.txt`(선택). 게이트웨이가 폴더를 읽어 화면에 카드로 띄우고, 사람이 설정을
채워 **시작**하면 별도 프로세스로 돈다. 스크립트는 이 저장소가 이미 가진 세 계약을
그대로 쓴다:

| 스크립트가 하는 일 | 이미 있는 길 | 플러그인 SDK 가 감싸는 방식 |
|---|---|---|
| 카메라 프레임 읽기 | `/dev/shm` 세그먼트 (`piper_shm.Subscriber`, BGR, 다중 소비자) | `plugin.camera("top").read_new()` |
| 검출 결과·프리뷰 내보내기 | 버스 `piper:vision:<cam>` + `put_preview` (yolod 패턴) | `plugin.publish_detections(...)`, `plugin.publish_preview(...)` |
| 추론 시작·정지, 파라미터, 녹화 | 게이트웨이 REST `/api/*` | `plugin.api.post("/api/models/inference/start", ...)` — 토큰은 SDK 가 챈다 |
| 상태·숫자를 화면에 보이기 | 버스 최신값(TTL) | `plugin.state(fps=12.3, phase="searching")` |
| **자기 화면**(대시보드·입력·설정) | 게이트웨이가 `/plugins/<name>/ui/` 로 **서빙·프록시**, "UI 보기" 버튼이 **새 창**으로 연다 | `ui: ui/`(정적) 또는 `ui: server`(소켓에 HTTP 서버) (§8-2) |

**새 데몬도, 새 전송 계층도, 새 포트도 없다.** 플러그인은 yolod 와 같은 자리(필요할 때
켜는 유닛)에서 돌고, 화면은 매니페스트를 읽어 **범용 위젯**으로 그리거나, 플러그인이 내는
자기 화면을 **새 창**으로 연다(조종 창과 같은 방식).

## 1. 왜 — 그리고 왜 범용으로

요청의 직접 동기는 하나다: "물체가 보이면 추론을 시작하는 스크립트". 그걸 그대로 만들면
`/api/vision` 에 엔드포인트 하나가 늘고, 다음 스크립트(사람이 작업 영역에 들어오면 멈춰라,
마커 자세를 내보내라, 검출을 MQTT 로 보내라)마다 또 하나가 는다. 이 저장소는 그 길을
이미 세 번 거절했다 — 정책 UI 는 YAML 스펙으로([policy-ui-spec](policy-ui-spec.md)),
정책 종류는 `lerobot_policy_*` 규약으로([act-s](act-s.md)), 에피소드 루프는 "스텝은
Python, 시퀀스는 데이터"로([episode-orchestrator](episode-orchestrator.md) — 이제 플러그인에
**흡수된다**, §12). 플러그인은 **그 셋과 같은 원칙을 "사용자 스크립트"에 적용한 것**이다.

사과 분류기처럼 YOLO + 등급 분류기 + VLA + 자기 화면이 한 덩어리인 **애플리케이션**이
이 계층의 진짜 고객이다 — 플러그인은 스크립트가 아니라 **애플리케이션 컨트롤러**다.
인식·판단·화면은 플러그인 프로세스 안에, 팔은 게이트웨이 REST(추론 시작·`task` 변경)를
통해서만 움직인다.

범용성의 기준은 "**터미널에서 돌리던 스크립트가 거의 그대로 플러그인이 된다**"다.
현장에서 사람이 먼저 쓰는 것은 `python my_script.py` 고, 그게 잘 되면 화면에 넣고
싶어진다. 그 전환 비용이 매니페스트 20줄이면 쓰고, 프론트 수정이면 안 쓴다.

## 2. 지금 이런 상태다 — 재료는 다 있다

| 층 | 있는 것 | 어디 |
|---|---|---|
| 프레임 | 세그먼트 구독, 발행자 재시작 추종(`orphaned`), 이름 조회 | [shm/piper_shm](../shm/piper_shm/), [shm_snapshot.py](../backend/app/services/shm_snapshot.py) `segment_reader` |
| 검출 계약 | `put_detections`/`get_detections`, 페이로드 모양(`objects[]`, `text`), 어노테이트 프리뷰 | [yolod.py](../daemons/yolod.py) `_payload`, [contract.py](../bus/piper_bus/contract.py) |
| 프로세스 소유 | `make_process("piper-<unit>")` — 유닛이면 journald 로그, 못 쓰면 자식 프로세스로 폴백하고 **말한다** | [systemd_process.py](../backend/app/services/systemd_process.py) |
| 자식 환경 | `injected_env` 가 버스 주소·logfix 를 모든 자식에게 | [process_manager.py](../backend/app/services/process_manager.py) |
| 로그 | stdout/journald → WS | 학습·yolod 가 쓰는 길 그대로 |
| 설정 폼 | 필드 스펙(`key/kind/min/max/default`) → 범용 렌더 | [SpecFields.tsx](../frontend/src/components/SpecFields.tsx) |
| 배타·E-stop | `Activity` 표 + `require_idle`, `ESTOP_TARGETS`, estopd 는 버스의 **모든** 활동 PID 를 죽인다 | [exclusivity.py](../backend/app/services/exclusivity.py), [estopd.py](../daemons/estopd.py) |
| 인증 | 세션 쿠키(`/api/*`) · Bearer(`/api/ext/v1/*`) · 열린 `/api/estop/*` | [authgate.py](../backend/app/core/authgate.py) |
| 프록시 경계 | nginx 가 `/api/`·`/ws`·`/health`·`/docs` 만 백엔드로, Vite 도 같은 목록 | [nginx.conf](../frontend/nginx.conf), [vite.config.ts](../frontend/vite.config.ts) |
| 페이지 등록 | `pages.ts` 한 줄, 그룹 `통합` 존재 | [pages.ts](../frontend/src/config/pages.ts) |

없는 것은 **넷**뿐이다: 플러그인 폴더를 읽는 레지스트리, 스크립트가 REST 를 부를 때 쓰는
자격, 매니페스트를 카드로 그리는 화면, 플러그인 화면으로 가는 프록시.

## 3. 확정한 결정

1. **플러그인 = 폴더.** `plugin.yaml` + `main.py` (+ `requirements.txt`, `README.md`, `ui/`).
   위치는 `PIPER_PLUGINS_DIR`(기본 `<데이터 루트>/plugins/`, 컨테이너는 `/data/plugins`) —
   데이터 볼륨이라 이미지 교체에도 남는다.
2. **설치 출처는 zip 업로드와 로컬 폴더 경로, 둘뿐이다.** git URL 은 두지 않는다 — 권한
   화면이 있어도 받아온 코드는 그대로 돈다. 저장소의 예제 플러그인은 설치 시 데이터 루트로
   복사된다. **같은 이름을 다시 깔면 409** 이고, `replace` 로 다시 보내면 **업데이트**다(9단계) —
   폴더만 바뀌고 저장 설정·실행 기록·인스턴스·venv(전부 폴더 밖 `.runtime/<id>`)는 남는다. 지우고 다시
   깔면 그게 통째로 사라졌다. 코딩 에이전트가 "고치고 다시 깔기" 를 가장 자주 돌기 때문이다(§7-2).
   의존은 바뀐 것만 무효로 한다 — `python:` 이 바뀌면 venv 삭제, `requirements.txt` 만 바뀌면 `.deps-ok`
   만 삭제(둘 다 시작이 409 로 [의존 설치] 를 다시 요구). 도는 인스턴스가 하나라도 있으면 409.
3. **별도 프로세스.** yolod 와 같이 `make_process("piper-plugin-<name>")`. 플러그인이
   죽어도 게이트웨이는 산다. 동시 실행 가능(플러그인마다 소유자 하나).
4. **SDK 는 선택.** `piper_plugin` 패키지(bus·shm 와 같은 설치 패키지)가 편의를 주지만,
   **매니페스트의 `entry` 가 그냥 스크립트여도 돈다** — 환경변수(`PIPER_API_BASE`,
   `PIPER_API_TOKEN`, `PIPER_REDIS_URL`, `PIPER_PLUGIN_CONFIG`, `PIPER_PLUGIN_UI_SOCKET`)만
   읽으면 `requests` 로도 된다.
5. **REST 자격은 실행마다 발급하는 토큰.** 게이트웨이가 시작 시 무작위 토큰을 만들어 환경으로
   넘기고, 매니페스트의 `permissions` 범위 안에서만 `/api/*` 를 통과시킨다. **`"*"` (전부
   허용)을 쓸 수 있다** — 그 경우 카드가 "모든 API 를 부를 수 있습니다"를 배지로 보인다.
   프로세스가 끝나면 토큰도 죽는다. (§5)
6. **플러그인 화면은 같은 포트, 새 창.** `ui: ui/` 는 정적 폴더를 게이트웨이가 서빙하고,
   `ui: server` 는 게이트웨이가 준 유닉스 소켓에 플러그인이 HTTP 서버를 띄우면 프록시한다.
   둘 다 `/plugins/<name>/ui/` 이고, 카드·상세의 **"UI 보기" 버튼이 새 창**(`window.open`
   popup — 조종 창과 같다)으로 연다. iframe 과 사이드바 직항은 하지 않는다. 별도 포트는
   **열지 않는다**. (§8-2 — 근거는 heartbeat 다)
7. **플러그인은 E-stop 대상이다.** 시작 시 PID 를 버스 활동 `plugin:<name>` 으로 올린다.
   heartbeat 가 끊기거나 E-stop 이 눌리면 estopd 가 추론·녹화와 **같이** 죽인다. 브라우저를
   닫으면 플러그인도 선다 — 녹화와 같은 계약이다. 무인 운용을 원하는 플러그인은
   `heartbeat: true` 를 선언하고 SDK 가 박동 스레드를 돌린다([external-api](external-api.md) §1 의
   데드맨과 같다).
8. **GPU 경합은 경고만.** `resources.gpu: true` 플러그인끼리, 또는 yolod 와의 공존은 막지
   않는다. 카드가 "GPU 를 쓰는 다른 것이 돌고 있다"를 보이고 판단은 사람이 한다. 학습
   (TRAINING)과는 배타다.
9. **게이트웨이가 재시작되면 플러그인은 수동으로 다시 시작한다.** 재부착하지 않는다. 기동 시
   남아 있는 `piper-plugin-*` 유닛은 **정지시킨다** — 토큰이 죽은 채 도는 고아를 두지 않는다.
   카드는 "게이트웨이가 재시작되어 정지됨"을 보인다.
10. **오케스트레이터는 제거하고 플러그인으로 대체한다.** 분리수거 루프는 번들 플러그인
    `recycling` 이 된다. 미션 수준 루프를 쓰는 길이 둘(YAML 스텝 + 플러그인)이면 복잡한
    앱은 결국 파이썬으로 가므로, 한 길만 남긴다. 이관 내용은 §12.
11. **검출기(yolod)를 플러그인으로 이관한다.** 번들 플러그인 `detector`. 지금 yolod 는 단일
    유닛(프로세스 하나 = 모델 하나)이라 검출기 둘을 동시에 못 돌린다. 플러그인이면 한 프로세스에
    모델 여러 개(CUDA 컨텍스트 공유)도, 프로세스 여러 개도 된다. 버스 계약 한 곳(`yolo:meta`)이
    발행자별로 바뀐다. 이관 내용은 §13.
12. **첫 플러그인은 요청 그 자체.** `detect-and-start` 예제 — 지정 클래스가 N 프레임 연속
    보이면 체크포인트로 추론 시작, 사라지면 정지. 두 번째는 `zone-guard`(ROI 에 사람 → 추론
    일시정지/E-stop). 세 번째 `ui-demo` 는 Streamlit 대시보드 한 장 — 프록시 경로의 실측용.

## 4. 매니페스트

```yaml
# plugins/detect-and-start/plugin.yaml
name: detect-and-start                 # 영숫자·_·- , 유닛 이름(piper-plugin-<name>)이 된다
version: 0.1.0
title: "물체가 보이면 추론 시작"          # 화면 카드 제목 (원문 한국어, i18n 은 §8)
description: "지정 클래스가 연속으로 보이면 체크포인트로 추론을 켜고, 사라지면 끈다"
entry: main.py                         # python <entry> — SDK 사용 여부는 자유
python: system                         # system | venv  (§6)

cameras:                               # 화면이 스트림 선택기(CameraStreamPicker)로 채운다
  - { key: watch, label: "감시 카메라", required: true }

arms:                                  # (선택) 상태를 **읽을** 팔 — 값은 CAN 인터페이스. 움직이지 않는다 (§7-5)
  - { key: follower, label: "팔로워" }

config:                                # SpecFields 가 그린다 — 정책 YAML 과 같은 어휘
  - { key: target_class, kind: text,   default: "pet_bottle", label: "대상 클래스" }
  - { key: min_frames,   kind: number, default: 5, min: 1, max: 60, label: "연속 프레임" }
  - { key: conf,         kind: number, default: 0.5, min: 0.05, max: 1, step: 0.05 }
  - { key: checkpoint,   kind: model,  label: "체크포인트" }            # 모델 선택기
  - { key: auto_stop,    kind: bool,   default: true, label: "사라지면 정지" }

permissions:                           # REST 범위 — 없는 것은 403. ["*"] 이면 전부 (§5)
  - inference.start
  - inference.stop
  - vision.read

resources:
  gpu: true                            # 학습(TRAINING)과 배타. 다른 GPU 사용자와는 경고만
  heartbeat: false                     # true 면 SDK 가 데드맨 박동을 돈다 (§3-7)

ui: false                              # ui/ (정적 폴더) | server (소켓에 HTTP 서버) — §8-2. 있으면 "UI 보기" 버튼이 생긴다

outputs:                               # 화면 위젯 힌트 — 없어도 버스에 있는 것은 다 보인다
  previews: ["det_watch"]              # bus put_preview 이름
  detections: ["watch"]                # bus put_detections 이름
  state: [{ key: phase, label: "단계" }, { key: det_fps, label: "검출 fps", unit: "fps" }]
```

- `kind` 는 정책 스펙의 `number`·`bool` 에 `text`·`select`(options)·`model`·`dataset`·
  `camera` 를 더한다. 뒤의 셋은 화면이 **기존 선택기**로 채운다 — 플러그인 폼이 모델
  목록을 따로 들고 있을 이유가 없다.
- `cameras` 와 `config` 를 가른 이유: 카메라는 **값이 아니라 자원**이다. 연결 안 된 스트림은
  비활성으로 보이고(선택기 규칙, [stream-based-cameras](stream-based-cameras.md) §5-3),
  시작 시 세그먼트 존재를 검사해 없으면 409 와 사유를 돌려준다.
- 매니페스트가 못 표현하는 건 **안 표현한다.** 레이아웃·색·컴포넌트는 화면이 정한다
  (SpecFields 주석의 선과 같다). 그 이상이 필요하면 `ui: true` 로 자기 화면을 가진다.

## 5. REST 자격 — 실행 토큰과 권한 범위

플러그인은 사용자 코드다. 세션 쿠키를 쥐어 주면 `/api/*` 전체(비밀번호 변경·유닛 종료
포함)가 열린다. 외부 API 토큰(`PIPER_API_TOKEN`)은 미션 수준이라 추론 시작이 없다
([external-api](external-api.md) §4 — 의도적). 그래서 **세 번째 자격**을 둔다.

| 항목 | 설계 |
|---|---|
| 발급 | `start` 때 `secrets.token_urlsafe(32)`. 메모리 딕셔너리 `{token: (plugin, scopes, pid)}`. 정지·크래시·게이트웨이 재시작으로 폐기 — 파일에 적지 않는다(§3-9, 재부착이 없으므로 필요 없다) |
| 전달 | `env_extra={"PIPER_API_TOKEN": ..., "PIPER_API_BASE": "http://127.0.0.1:8000"}` — `injected_env` 경로 |
| 검사 | authgate 가 `Authorization: Bearer` 를 보고 플러그인 토큰이면 **범위 표**로 경로를 대조. 통과 시 `scope["piper_plugin"]` 에 이름을 적어 라우터가 감사 로그에 남길 수 있게 |
| 범위 → 경로 | `inference.start` → `POST /api/models/inference/start`, `inference.stop`, `inference.read`(상태·정책 종류), `params.write` → `POST /api/params`(`task` 변경이 여기), `recording.*`, `vision.read` → `GET /api/vision/*`, `vision.judge` → `POST /api/vision/judge`(LLM 판단 — 오케스트레이터 이관의 필수 범위), `eval.write` → `POST /api/eval/log`, `robot.read` → `GET /api/robots/end-pose/<iface>`(말단 자세 — 움직이는 경로는 없다, §7-5), `activity.read` → `GET /api/activity`, `estop` → 어차피 열려 있다. 표는 코드 한 곳(`plugin_scopes.py`), 테스트가 라우터 목록과 대조 |
| `"*"` | 모든 `/api/*` 를 통과시킨다. **`/api/auth/*` 만은 예외** — 비밀번호를 바꾸거나 로그인을 끄는 것은 사람의 행위다. 카드에 배지로 보인다 |
| 설치 때 | 매니페스트의 `permissions` 를 카드에 **보여 주고** 켠다. "이 플러그인은 추론을 시작할 수 있습니다" — 모바일 앱 권한 화면과 같은 자리 |

ext API 를 재사용하지 않는 이유: 그 계약은 외부 소비자를 모르는 채 오래 살아야 해서
좁게 잠갔다. 플러그인은 **같은 기계 안의 사용자 코드**라 요구가 다르고, 섞으면 외부
계약이 넓어진다.

**플러그인 화면(§8-2)에서 브라우저가 부르는 `/api/*` 는 이 토큰이 아니다.** 같은 출처라
사용자의 세션 쿠키가 그대로 간다 — 화면은 사람의 권한으로, 프로세스는 토큰의 권한으로.
둘을 섞지 않는다.

## 6. 실행 환경 — 어디서, 무엇으로 도나

- **프로세스 소유자**: `make_process("piper-plugin-<name>")`. 소스 설치 기계는 일시
  유닛(journald), 컨테이너는 자식 프로세스 폴백 — yolod 가 지금 그 둘을 다 겪고 있다.
  플러그인은 세그먼트(`ipc: host`)와 버스(소켓)만 쓰므로 **컨테이너 안에서 돈다.** 장치를
  열지 않는다는 저장소 원칙이 플러그인에도 그대로 성립한다.
- **파이썬**: `python: system` 은 게이트웨이와 같은 인터프리터(torch·transformers·opencv 가
  이미 있다). `python: venv` 는 `<plugin>/.venv` 를 `--system-site-packages` 로 만들고
  `requirements.txt` 를 깐다 — 무거운 것은 상속, 플러그인 고유 의존만 추가. `python: venv-isolated` 는
  시스템 패키지를 **상속하지 않는다** — 다른 torch 버전·TensorRT·특정 CUDA 빌드처럼
  게이트웨이의 것과 충돌하는 의존이 있을 때. 토치를 다시 까는 수 GB 와 시간을 먹으므로
  카드가 설치 전에 그 사실을 말한다. 설치는 화면
  버튼 + 로그 스트림(학습 설치와 같은 UX). PyPI 가 안 닿는 호스트는 실패를 보여 주고
  `system` 으로 돌 수 있으면 돈다.
- **자원 제한**: 유닛이면 `systemd-run -p MemoryMax= -p CPUQuota=` 로 매니페스트의
  `resources.memory`·`cpu` 를 건다. 폴백(자식)에서는 못 걸고, 그 사실을 카드가 말한다.
- **배타**: 활동이 **둘**이다 — `Activity.PLUGIN`(모든 플러그인. 아무것도 막지 않고 아무것도 못 막는다,
  UPLOAD 와 같다. E-stop 대상)과 `Activity.PLUGIN_GPU`(**지금 GPU 를 쓰는** 플러그인이 하나라도 돌고
  있다는 상태. TRAINING 과 상호 배타). `resources.gpu: true` 는 "쓸 수 있다"이고, 실제로 쓰는지는 시작
  시점의 설정(`resources.gpu_field`, 기본 `device`)이 `cpu` 가 아닌지로 판정한다 — 검출기를 CPU 로
  돌리면 학습 중에도 켤 수 있다. 둘로 나눈 이유: 배타 표는 활동 단위인데 GPU 사용은
  플러그인마다 다르다 — 구현(1단계)에서 확정.
  GPU 플러그인 둘, 또는 yolod 와의 동시 실행은 **경고만**(§3-8). `require_idle` 은
  **플러그인 자체**가 아니라 플러그인이 부른 REST 가 건다 — 추론 시작이 막히면 플러그인이
  409 를 받고, 그걸 어떻게 할지는 스크립트 몫이다.
- **E-stop**: 시작 직후 `bus.set_activity_pid("plugin:<name>", pid)`. estopd 는 활동 키를
  가리지 않고 버스의 PID 를 전부 죽이므로 데몬 수정이 없다. `ESTOP_TARGETS` 에
  `Activity.PLUGIN` 을 넣고 `STOPPERS` 에 "도는 플러그인 전부 kill" 을 등록해 게이트웨이
  쪽 수동 E-stop 도 같은 결과가 된다. 죽은 뒤 카드는 `last_estop` 의 사유를 보인다.
- **수명**: 게이트웨이 재시작 → 플러그인 **수동 재시작**(§3-9). 기동 시 `systemctl --user
  list-units 'piper-plugin-*'` 로 남은 유닛을 멈추고 활동 PID 를 지운다. 폴백(자식)은
  게이트웨이와 함께 죽으니 할 일이 없다. 두 경우 모두 화면 결과가 같다 — "정지됨".

## 7. SDK — `piper_plugin`

```python
# plugins/detect-and-start/main.py
from piper_plugin import Plugin

class DetectAndStart(Plugin):
    def setup(self):
        self.model = self.detector(self.config["model"])   # SDK 로더 — RT-DETR(Apache), 커스텀 .pt 도
        self.cam = self.camera("watch")             # 매니페스트 cameras.key
        self.seen = 0
        self.running = False

    def loop(self):                                   # 반환하면 다시 불린다. 정지는 예외/플래그
        frame, seq, ts = self.cam.read_new(timeout_s=1.0) or (None, None, None)
        if frame is None:
            return
        r = self.model.predict(frame, conf=self.config["conf"])    # detector_loader 의 어댑터 모양
        labels = [r.names[int(c)] for c in r.boxes.cls]
        self.publish_detections("watch", frame.shape[:2], r)      # 검출 페이로드 계약 그대로
        self.publish_preview("det_watch", r.plot())
        self.seen = self.seen + 1 if self.config["target_class"] in labels else 0
        self.state(phase="running" if self.running else "watching", seen=self.seen)

        if not self.running and self.seen >= self.config["min_frames"]:
            resp = self.api.post("/api/models/inference/start", json={
                "checkpoint_path": self.config["checkpoint"],
                "camera_mapping": self.camera_mapping(),            # 매니페스트 cameras → {key: stream}
            })
            self.running = resp.status_code == 200
            if resp.status_code == 409:
                self.log("추론 시작 거부: %s", resp.json()["detail"])  # 배타 — 사람이 볼 로그
        elif self.running and self.config["auto_stop"] and self.seen == 0:
            self.api.post("/api/models/inference/stop"); self.running = False

    def teardown(self):
        if self.running:
            self.api.post("/api/models/inference/stop")

if __name__ == "__main__":
    DetectAndStart.main()        # 환경변수·설정 JSON 파싱, SIGTERM → teardown, 상태 TTL 갱신
```

SDK 가 감추는 것: 환경변수 읽기, 설정 JSON 파싱과 기본값 채움, `Subscriber` 재개방
(`segment_reader` 의 orphaned 처리 그대로), 검출 모델 로더(`self.detector(name)` —
[detector_loader.py](../daemons/detector_loader.py) 가 SDK 로 들어온다. 기본은 RT-DETR 이고
**번들 플러그인은 ultralytics 를 쓰지 않는다**(AGPL — 그 파일 머리의 사유). 사용자 플러그인이
무엇을 import 하는지는 사용자 몫이다), 검출 페이로드 조립(`yolod._payload` 를 **함수로 빼서**
공유 — 두 벌이 되면 `text` 가 어긋난다), `put_preview` 의 JPEG 인코딩
(`encode_bgr` 한 함수 — 채널 뒤집힘 사고의 재발 방지), 상태 KV 의 TTL 발행, `api`
클라이언트(베이스 URL·토큰·타임아웃), `heartbeat: true` 일 때 박동 스레드, SIGTERM 에서
`teardown` 보장, 처리 중 예외의 로그 + 재시도 간격, `ui: server` 일 때 `self.ui_socket`
경로와 `serve_ui(app)` 도우미(ASGI 앱을 소켓에 띄운다 — uvicorn 한 줄).

### 7-1. 제어 루프는 세 층으로 준다

| 층 | 무엇을 쓰나 | 누가 |
|---|---|---|
| 1. `Plugin.loop()` | 반복 호출되는 함수 하나. 안에서 뭘 하든 자유 | "물체 보이면 추론 시작" 같은 스크립트, 번들 `detector` |
| 2. `MissionPlugin` | 관찰 → 판단 → 실행 → 완료 → 기록 → 리셋 뼈대가 들어 있고 사용자는 **훅만 채운다** | 사과 분류기·분리수거 같은 에피소드 루프, 번들 `recycling` |
| 3. 원시 연산 `self.robot.*` 등 | 추론 시작·정지, `task` 변경, 일시정지, 리셋, 정책 종류, 판단 LLM, 검출 읽기, 저널 | 위 두 층의 재료. 직접 써도 된다 |

`MissionPlugin` 은 오케스트레이터 1단계가 하드코딩했던 루프를 상속 가능한 클래스로 바꾼 것이다.

| 훅 | 역할 | 기본 |
|---|---|---|
| `observe() -> ctx` | 한 회차의 입력을 모은다 (검출·프레임·센서) | 검출 신선도 검사만 |
| `decide(ctx) -> Decision` | `Decision.task("…")` · `Decision.skill("name")`(언어를 안 읽는 정책 — 체크포인트 교체) · `Decision.skip("사유")` | 없음 — **반드시** 구현 |
| `done(ctx) -> bool` | 완료 판정. 대기 중 주기적으로 불린다 | 항상 False → 타임아웃으로 끝난다 |
| `record(ctx, result) -> dict` | 저널에 더 남길 것 | `{}` |
| `reset()` · `before_episode()` · `on_estop()` · `on_command(name)` | 그 이름대로 | 비어 있음 |

뼈대가 대신 하는 것: 시작 전 검사(추론이 도는가, 정책이 `task` 를 읽는가 — 아니면 **시작 거부**,
검출 발행자가 사는가), `task` 전달과 대기, 회차 수·타임아웃·`dry_run`(공통 설정 항목으로 폼에
자동 추가), E-stop·추론 사망·정지 요청 감시, 판단 실패 시 회차 스킵, JSONL 저널,
상태 KV(`phase`·`episode`·`decision`) 발행.

```python
class AppleSorter(MissionPlugin):
    def setup(self):
        self.grader = load_grader(self.config["grader_weights"])
    def observe(self):
        det = self.detections("apple_top", fresh_s=1.0)
        return {"det": det, "grade": self.grader(self.camera("top").latest(), det)}
    def decide(self, ctx):
        if not ctx["det"].objects:
            return Decision.skip("사과 없음")
        return Decision.task(f"put the apple in the {self.config['bins'][ctx['grade']]} bin")
    def done(self, ctx):
        return self.detections("apple_top", fresh_s=1.0).count("apple") < ctx["det"].count("apple")
    def record(self, ctx, result):
        return {"grade": ctx["grade"]}
```

**명령(`commands`)**: 매니페스트 `commands: [pause, resume, skip]` 를 적으면 상세 페이지에 버튼이
생기고, 눌리면 버스 제어 큐로 들어와 `on_command(name)` 이 불린다. 외부 시스템은 ext API 로
같은 명령을 보낸다. 화면 없이 입력을 받는 범용 길이다.

**스캐폴드**: `python -m piper_plugin new apple-sorter --template mission|loop|detector` 가
매니페스트·`main.py`·테스트 틀을 만든다. `python -m piper_plugin check <폴더>` 가 매니페스트·권한
범위·카메라 키·금지 단어를 검사한다.

### 7-3. 플러그인 간 입출력 — 검출기 → 분류기 → 미션

플러그인을 **이어 붙이는** 길이 셋이다. 전부 버스의 "최신값 + TTL" 철학이고, 큐도 RPC 도 없다.

| 길 | 발행 | 소비 | 쓰는 때 |
|---|---|---|---|
| 검출 `piper:vision:<이름>` | `publish_detections(name, …)` | `input_detections("슬롯")` / `detections(name)` | 검출기의 출력 (화면도 같은 키를 그린다) |
| 토픽 `piper:plugin:topic:<이름>` | `publish("grades", data)` | `latest("슬롯")` → `Topic(data, seq, publisher, ts)` | 임의 구조의 결과 — 등급·자세·판정 |
| 상태 KV `piper:plugin:state:<이름>` | `state(**kv)` | (화면용) | 사람이 보는 숫자 |

**매니페스트가 배선을 선언한다.** `inputs: [{key, kind: detections|topic, source, required}]` 와
`outputs.topics: [...]`. 시작할 때 화면의 입력 선택기가 **지금 살아 있는** 검출 이름·토픽
(`GET /api/plugins/topics`, 발행자 표시) 중에서 고르고, 고른 것이 `PIPER_PLUGIN_INPUTS` 로 간다.
코드는 `self.input("슬롯")` 으로 풀어 쓰므로 소스가 바뀌어도 코드는 그대로다 — 카메라 슬롯과 같은
생각이다.

**막지 않고 말한다.** 입력 소스가 지금 죽어 있어도 시작은 된다(검출기를 뒤에 켤 수 있다). 상태의
`inputs_live` 가 슬롯마다 참·거짓을 주고 화면이 "그 출력을 내는 플러그인을 켜라" 고 쓴다.
필수 슬롯에 기본도 선택도 없을 때만 400 이다.

번들 `size-grader` 가 실례다: `detector` 의 검출을 입력으로 받아 bbox 크기로 대·중·소를 매기고
토픽 `grades` 로 올린다. 사과 분류기의 "등급" 칸을 가장 작게 흉내 낸 것이고, 미션 플러그인은
`self.latest("grades")` 로 그걸 읽어 `Decision` 을 낸다.

### 7-4. 멀티 인스턴스 — 같은 코드를 카메라마다 하나씩

검출기·영역 감시처럼 **같은 코드를 입력만 바꿔 여럿** 돌려야 하는 플러그인이 있다. 폴더 복사는
코드를 두 벌로 만들어 한쪽만 고쳐지는 길이라 거절하고, **인스턴스**를 둔다.

| 항목 | 설계 |
|---|---|
| id | `<폴더 이름>` 이 기본 인스턴스, `<폴더 이름>:<라벨>` 이 추가 인스턴스(라벨은 영숫자·`_`·`-`, 20자) |
| 켜는 법 | 매니페스트 `multi_instance: true`. 목록 카드에 인스턴스 줄이 생기고 "+ 인스턴스" 로 더한다(`POST /api/plugins/{name}/instances`). 미션(`mission: true`)은 동시 1개라 끄는 것이 맞다 |
| 무엇이 인스턴스별인가 | 소유자(유닛 `piper-plugin-detector:cam2`)·토큰·실행 기록·저장 설정·런타임 디렉토리(`.runtime/detector:cam2`)·버스 키(상태 KV·발행자 메타·토픽·E-stop 활동 `plugin:detector:cam2`) |
| 무엇이 공유인가 | 코드(폴더)·venv(의존은 코드에 딸린 것)·매니페스트 |
| SDK | `self.name` 이 인스턴스 id 다 — 버스 키가 자동으로 갈린다. 폴더 이름·라벨은 `self.env.base_name`·`self.env.instance` |
| 새 인스턴스의 출발점 | 기본 인스턴스의 저장 설정을 복사한다 — 카메라만 바꾸는 것이 보통이라서 |
| 호환 | 기본 인스턴스 id = 폴더 이름이라 `/api/vision/*` 호환층·외부 API·플러그인 상세 주소는 안 바뀐다 |

검출 이름(`piper:vision:<alias>`)과 프리뷰 이름은 사용자가 설정에 적는 값이라 인스턴스마다 다르게
적어야 한다 — 같으면 서로 덮어쓴다. 화면이 발행자(인스턴스 id)를 함께 보여 주므로 눈에는 띈다.

### 7-5. 팔 상태 — 읽기만 한다

카메라와 검출만으로는 "그리퍼가 닫혔다"·"팔이 작업 영역을 벗어났다"(데모 격차 G3 의 스냅샷 시점)를
모른다. `MissionPlugin.done()` 이 타임아웃에 기대던 이유다. 재료는 이미 있다 — robotd·simd 가 관절 상태를
`/dev/shm/piper.arm.<iface>.state` 로 늘 발행하고(다중 소비자), 말단 자세는 게이트웨이 REST 에 있다.

| 항목 | 설계 |
|---|---|
| 매니페스트 | `arms: [{key, label, required}]` — 카메라 슬롯과 같은 모양, 값은 CAN 인터페이스(`can0`, 시뮬 팔도 같다) |
| 시작 검사 | 필수 슬롯이 비면 400, 고른 팔의 **상태** 세그먼트가 없으면 409 — 카메라와 같은 규칙. 명령 세그먼트(`.action`)만 있는 것은 상태를 내는 팔이 아니다 |
| 선택기 | `GET /api/plugins/topics` 의 `arms` — 상태 세그먼트가 있는 팔 + 등록부의 역할·좌우. 리더도 상태를 내므로 나온다 |
| SDK | `self.arm(key).read()` / `.read_new()` → `ArmState(joints, gripper, err_code, ctrl_mode, seq, age_s)`. 값은 **정규화**(관절 -100..100, 그리퍼 0..100) — 데이터셋 `observation.state` 와 같은 눈금이고, 캘리브레이션 소유자(robotd)를 하나로 두려고 도(°)로 되돌리지 않는다 |
| 발행자 재시작 | 리더가 `orphaned`(파일이 지워졌다)를 보면 다시 연다 — 안 그러면 옛 mmap 이 **마지막 자세를 영원히** 낸다. `piper_shm.arm` 의 리더에 `orphaned` 를 더했다(라이터에만 있었다) |
| 말단 자세 | `self.arm(key).end_pose()` → mm·도 + 작업 공간 상자 안인가(`inside`). 범위 `robot.read`(`GET /api/robots/end-pose/<iface>`). robotd RPC 라 엔드포인트를 executor 로 옮겼다 — 플러그인이 폴링해도 이벤트 루프를 안 문다 |
| 움직이기 | **없다.** 범위 표에 명령 경로를 넣지 않고(`robot.read` 는 GET 하나), SDK 는 명령 세그먼트를 열지 않는다. 팔은 여전히 추론(REST)으로만 움직인다 |

### 7-2. 코딩 에이전트가 첫 사용자다

루프를 사람이 손으로 짜는 것보다 로컬 코딩 에이전트(Claude Code 등)에게 "이 `recycling` 을 사과
분류기로 바꿔 줘" 라고 시키는 쪽이 현실적인 사용법이다. 그래서 설계가 지켜야 할 것:

- 표면이 작고 선언적이다(훅 넷, 원시 연산 열 개 안쪽, YAML 하나).
- 참고 예제가 완성형이다(`recycling`·`detector`).
- 피드백 루프가 터미널에 있다 — `new` → `check` → `run --dry-run` 을 사람 없이 반복한다.
- **`plugins/README.md` 는 에이전트가 읽는 문서다**: 계약 요약, 하지 말 것(장치 직접 열기·
  `/api/auth` 호출·블로킹 대기·`yolo` 단어), 검증 명령 순서. 사람용 안내와 같은 파일.

이것이 [llm-integration](llm-integration.md) 4단계 "LLM 플래너(스펙을 생성하는 스텝)" 를
대체한다 — 런타임에 LLM 이 시나리오를 짜는 대신 **개발 시점에** 에이전트가 플러그인을 짜고
사람이 `dry_run` 으로 확인한다. 플래너 단계는 폐기.

**개발 루프**: `python -m piper_plugin run plugins/detect-and-start --config '{"conf":0.4}'
--camera watch=HD_Webcam_1` — 게이트웨이 없이 터미널에서 같은 코드가 돈다(토큰은 비어
있고 `api` 호출은 거절된다고 말한다. `ui:` 가 있으면 `127.0.0.1:8765` 에 띄워 바로 본다).
게이트웨이가 켠 것과 **같은 진입점**이라 "터미널에선 됐는데 화면에선 안 된다"가 구조로
줄어든다.

SDK 는 `wrapper/` 처럼 lerobot 을 import 하지 않는다. 의존은 `piper_bus`·`piper_shm`·
`numpy`·`opencv`·`httpx` 뿐이다.

## 8. 화면

### 8-1. 플러그인 페이지 — 목록 하나 + 플러그인별 상세

**플러그인 페이지 하나가 허브고, 플러그인마다 상세 라우트가 있다.** 목록 카드는 켜고 끄는
것까지만 하고, 설정·출력·로그는 상세에서 탭으로 본다. 사용자가 손대는 자리는 셋이다 —
켜고 끄기는 카드 또는 상세 머리, 설정은 상세의 설정 탭, 플러그인 자기 화면은 **"UI 보기"
버튼이 여는 새 창**(§8-2). **설정 → 서비스에는 넣지 않는다** — 그곳은 데몬
유닛의 자리고, 플러그인은 활동이다.

`pages.ts` 에 두 줄: `{ path: '/plugins', label: '플러그인', group: '통합', nav: true }`,
`{ path: '/plugins/:name', nav: false }`.

#### 목록 `/plugins` — 카드 그리드

| 영역 | 내용 | 재료 |
|---|---|---|
| 상단 한 줄 | 설치 — zip 업로드 / 폴더 경로 입력 / 예제 복사. 매니페스트 검증 실패는 줄 번호와 함께 | 신규 1곳 |
| 카드 머리 | 제목·버전·상태 점(idle/running/crashed/estopped)·권한 배지(`*` 는 눈에 띄게)·GPU 경고 | `/api/plugins` |
| 카드 몸 | `outputs.state` 의 앞 한두 값(`phase`, fps) — 목록만 훑어도 돌아가는지 보이게 | 버스 상태 KV |
| 카드 버튼 | **시작/정지** · **열기**(상세로) · **UI 보기**(`ui:` 가 있는 플러그인만, 새 창) | |

설정 폼을 카드에 펼치지 않는다 — 카드가 길어지면 목록이 목록 노릇을 못 한다.

#### 상세 `/plugins/<name>` — 제목 옆 탭

제목 줄에 상태 점과 시작·정지·재시작·**UI 보기** 버튼, 그 **바로 옆**에 탭을 둔다(아래 줄로
내리거나 오른쪽 끝으로 밀지 않는다 — 이 저장소의 탭 규칙).

| 탭 | 내용 | 비고 |
|---|---|---|
| 설정 | `cameras` → `CameraStreamPicker`, `config` → `SpecFields`(+text/select/model 확장), 의존 설치(venv) 버튼 | 도는 중에는 **읽기 전용** + "적용하려면 재시작" 안내. 값은 서버가 플러그인별로 저장해(`<plugin>/.runtime/config.json`) 다음 시작에 재사용 |
| 출력 | 프리뷰 `<img>`(기존 `/api/vision/preview/{name}` 경로), 검출 표(`objects[]`), 상태 KV 전체 | 전부 버스 최신값 — yolo-demo 카메라 카드와 같은 꼴 |
| 로그 | WS 로그 뷰어 — 프로세스 상태 메시지와 같은 채널 | `LogViewer` |

기본 탭은 **출력**이다. 자기 화면이 본체인 플러그인은 "UI 보기" 가 새 창을 열므로 탭에 없다.

#### 어디서든 보이는 것

- 상태바의 "도는 활동"에 `Activity.PLUGIN` 이 들어간다 — 플러그인이 돌면 어느 페이지에서든
  이름이 보이고, 클릭하면 상세로 간다. 기존 `useActivity` 가 해주므로 추가 작업이 없다.
  라벨은 `LABELS[Activity.PLUGIN]` + 플러그인 제목.
- 크래시·E-stop 정지는 SystemMessages 로 뜬다(사건), 상태바는 상태 — 짝이다
  ([layout-redesign](layout-redesign.md) §1).

문구 규칙: 매니페스트의 `title`·`label`·`description` 은 **원문 한국어**로 쓰고 화면이
`t()` 로 넘긴다 — 사전에 없으면 원문 그대로 나온다([i18n](i18n.md)). 플러그인이 `en`
을 싣고 싶으면 `i18n/en.json` 을 폴더에 두고 페이지가 그걸 합친다(4단계 이후).

⚠ 정지·삭제 확인은 **논블로킹 모달**이다. `window.confirm` 은 heartbeat 를 끊는다.

### 8-2. 플러그인 자기 화면 — 같은 포트로 서빙, "UI 보기" 버튼이 새 창으로 연다

플러그인은 대시보드·입력 폼·설정 페이지 같은 **자기 화면을 통째로** 가질 수 있다
(정적 HTML/JS, Streamlit·Gradio·FastAPI+HTML, 무엇이든). 두 질문이 있었다 — 어느 포트로
내는가, 어디에 띄우는가.

#### 포트 — 게이트웨이와 같은 포트

| 길 | 판정 |
|---|---|
| **별도 포트** — 플러그인이 `0.0.0.0:<포트>` 에 자기 서버를 열고 사용자가 `http://robot:<포트>` 로 간다 | ❌ 로그인 관문 밖. 컨테이너는 8000 하나만 노출해 플러그인마다 compose·방화벽·포트 할당을 건드린다. 화면이 `/api/*` 를 부르면 CORS·쿠키 문제. 메인 탭을 그 주소로 **이동**하면 heartbeat 가 끊겨 추론·녹화가 SIGKILL |
| **같은 포트, 게이트웨이가 서빙·프록시** — 플러그인은 정적 폴더를 두거나 로컬 소켓에만 바인딩, 게이트웨이가 `/plugins/<name>/ui/` 로 낸다 | ✅ 세션 쿠키 검사가 공짜로 걸린다. 밖에서 닿는 포트는 여전히 하나. nginx·Vite 에 블록 하나씩 |

**플러그인이 포트를 직접 고르지 않는다.** 사용자에게 보이는 주소는 언제나 `/plugins/<name>/ui/`
다. 포트 충돌, 방화벽 추가, 재시작 뒤 주소 변경이 구조로 없다.

#### 띄우는 자리 — 새 창

| 길 | 판정 |
|---|---|
| **앱 껍데기 안 iframe** (처음 안) | ◐ E-stop·상태바가 같은 화면에 남는 장점이 있지만, 화면이 좁아지고 base path·WS 프록시·꺼짐 안내 등 **가장 복잡한 부분**이 전부 여기서 나온다. 플러그인 UI 가 우리 테마·컴포넌트를 못 쓰는 건 똑같다 |
| **새 창** (`window.open` popup) | ✅ 조종 창(`/teleop`)이 이미 이 방식이다 — `window.open(url, 'piper-plugin-<name>', 'popup,width=1280,height=820')`. 메인 창이 남아 heartbeat·E-stop 이 산다. 플러그인 UI 는 창 전체를 쓴다. iframe 전용 배선이 전부 사라진다 |
| **메인 탭 이동** | ❌ heartbeat 가 끊긴다 |

**새 창으로 간다.** 결정의 근거는 둘이다 — heartbeat 는 메인 창에 살고, 저장소에 같은
패턴이 이미 있다.

#### 설계

| 항목 | 설계 |
|---|---|
| 매니페스트 | `ui: /detector` — **저장소의 standalone 페이지 경로**(번들용. 기존 컴포넌트·테마·i18n 을 그대로 쓴다). `ui: ui/` — 폴더의 정적 파일을 게이트웨이가 `/plugins/<name>/ui/` 에 서빙(`index.html` 폴백 포함, SPA 가능). `ui: server` — 게이트웨이가 `PIPER_PLUGIN_UI_SOCKET=<데이터 루트>/plugins/.runtime/<name>.sock` 을 주고, 플러그인이 거기에 HTTP 서버를 띄우면 HTTP·WebSocket 을 프록시한다. 정적이 기본이고 서버는 Streamlit·Gradio 처럼 서버가 곧 UI 인 것들을 위해 둔다 |
| 포트만 되는 프레임워크 | `127.0.0.1:0` 에 바인딩하고 실제 포트를 버스 상태 KV `ui_port` 로 보고한다. 소켓이 없으면 그 포트로 프록시한다. **루프백만** |
| 인증 | authgate 가 `/plugins/` 접두사를 `/api/` 와 같게 다룬다 — 세션 쿠키 없으면 401. 프록시는 `X-Piper-Plugin: <name>` 과 `X-Forwarded-Prefix: /plugins/<name>/ui` 를 붙인다 |
| 버튼 | 카드와 상세 머리의 **"UI 보기"**. `ui:` 가 없는 플러그인에는 없다. 정적 UI 는 플러그인이 꺼져 있어도 열린다(데이터가 없다고 자기가 말하면 된다). `server` 는 꺼져 있으면 버튼이 비활성 + "시작하면 볼 수 있다" |
| 화면 → 게이트웨이 API | 브라우저가 같은 출처로 `/api/*` 를 부른다 — **세션 쿠키**, 사람의 권한. 토큰은 쓰지 않는다(§5) |
| 새 창에는 E-stop 이 없다 | 메인 창이 담당한다. `new --template` 의 정적 UI 틀에는 `POST /api/estop/trigger`(열린 경로) 를 부르는 E-stop 버튼이 **기본으로 들어 있다.** 임의 UI 에 버튼이 있는지 기계가 검사할 수는 없으므로 그 이상은 권고다 |
| nginx · Vite | `location /plugins/ { proxy_pass ...; Upgrade 헤더 }` 한 블록, `vite.config.ts` 프록시에 `/plugins` 한 줄(`ws: true`). 둘 다 `/ws` 블록과 같은 꼴 |

번들 `detector` 가 첫 실례다 — 지금 `/yolo-demo` 페이지를 `/detector` 로 개명해 **standalone**
(Layout 밖, 조종 창과 같은 플래그)으로 돌리고, 매니페스트가 `ui: /detector` 로 가리킨다.
플러그인 폴더에 정적 파일을 따로 빌드하지 않는다 — 번들은 저장소 페이지, 서드파티는 `ui/`·`server`.

별도 포트가 맞는 경우는 하나다 — 이 웹 UI 를 안 쓰는 다른 기계·팀이 플러그인 화면만
봐야 할 때. 그 요구가 생기면 매니페스트에서 명시적으로 켠 플러그인만 포트를 열고, 로그인
관문 밖이라는 사실을 카드가 경고한다. **지금은 만들지 않는다.**

## 9. 단계

| 단계 | 내용 | 끝났다는 기준 |
|---|---|---|
| 0 | **`yolo` 제거(§14)** — 파일·모듈·경로·설정 키·데이터 디렉토리·문서·화면 문구 일괄 개명 + 기동 시 데이터 디렉토리 마이그레이션 + 옛 URL 리다이렉트 + 금지 테스트를 **트리 전체**로 확장 | `grep -ri yolo` 가 CHANGELOG 와 .git 밖에서 비어 있고, 배포 기계에서 기존 검출 데이터셋·가중치가 새 이름 아래 그대로 보인다 |
| 1 | 레지스트리(`services/plugins.py`: 폴더 스캔·매니페스트 검증·소유자 맵·기동 시 고아 유닛 정리) + `/api/plugins` CRUD·start/stop/status + 실행 토큰(`*` 포함) + authgate Bearer 범위 검사 + `Activity.PLUGIN`(ESTOP_TARGETS·STOPPERS 포함) | `curl` 로 예제 플러그인을 켜고, 그 안에서 `inference.start` 는 통과·`auth` 는 403. 브라우저를 닫으면 플러그인이 estopd 에 죽고 사유가 남는다 |
| 2 | `piper_plugin` SDK(camera·publish·state·api·main) + `python -m piper_plugin run` + 예제 둘(`detect-and-start`, `zone-guard`) + yolod 의 `_payload`·`encode_bgr` 를 공유 함수로 | 터미널 실행과 게이트웨이 실행이 같은 결과. 예제 1 이 실기에서 물체 → 추론 시작 → 사라짐 → 정지 왕복 |
| 3 | **오케스트레이터 이관(§12)**: 번들 플러그인 `recycling`(검출→`/api/vision/judge`→`task`→대기→`/api/eval/log`→리셋, 저널은 SDK `journal()`) + `heartbeat: true` 데드맨 + ext API `missions` 를 플러그인 위에 다시 배선 + `services/orchestrator.py`·라우터·WS 타입·VisionPage 패널·`Activity.ORCHESTRATOR` 삭제 | 드라이런 3회차가 플러그인으로 완주(1단계 때와 같은 기준). ext API 로 미션 제출→조회→취소가 그대로 된다. `grep -ri orchestrator backend frontend` 가 비어 있다 |
| 3b | **검출기 이관(§13)**: 번들 플러그인 `detector`(모델 여러 개 × 카메라 여러 대, 한 프로세스) + `detector_loader` 를 SDK 로 + 버스 메타 발행자별 키 + 페이로드 `publisher` + `/api/vision/start·stop·status` 를 플러그인 위로 다시 배선 + yolo-demo·vision 페이지가 발행자별로 그린다 + `daemons/yolod.py` 삭제 | 검출기 둘(일반 + 커스텀 가중치)이 같은 카메라에 동시에 돌고 화면이 둘을 구분해 보인다. yolo-demo·yolo-train 의 캡처·사전 라벨이 그대로 된다. ext API `vision/*` 가 그대로 된다 |
| 4 | 플러그인 목록(카드·설치) + 상세(설정·출력·로그 탭) + 상태바 활동 표시 + 영어 번역 | 새로고침 뒤에도 카드 상태가 맞다. 게이트웨이 재시작 뒤 "정지됨" 과 수동 재시작. 상세의 설정은 도는 중 잠긴다 |
| 5 | **자기 화면**: `ui: ui/` 정적 서빙 + `ui: server` 소켓 프록시(HTTP·WS) + authgate 접두사 + nginx·Vite 블록 + "UI 보기" 새 창 버튼 + 번들 `detector` 의 UI(§15 결정 뒤) + 예제 `ui-demo`(Streamlit) | detector UI 가 새 창에서 돌고, 그 동안 메인 창의 E-stop heartbeat 가 끊기지 않는다(추론 켜 둔 채 10분). 정적 UI 가 플러그인이 꺼진 상태에서도 열린다 |
| 6 | venv·venv-isolated 의존 설치 + 자원 제한 + ext API `POST /api/ext/v1/plugins/{name}/start·stop`(미션이 아닌 플러그인을 외부가 켠다) | 외부에서 플러그인만 켜고 미션 없이 운용 가능 |
| 7 (후보) | 플러그인 간 입출력(버스 키 구독 — 검출기 → 분류기 → 미션처럼 플러그인을 이어 붙이기) | 범용성의 실측 — 저장소 자신의 기능이 플러그인 규약 위에서 돈다 |
| 9 | 업데이트 설치(§3-2) — `replace`, `.runtime/` 보존, 의존 무효화는 바뀐 것만 | 고친 폴더·zip 을 다시 깔아도 저장 설정·실행 기록·인스턴스가 그대로다 |
| 10 | 팔 상태 읽기(§7-5) — `arms` 슬롯, `self.arm()`, `robot.read` | 플러그인이 그리퍼 열림·말단 자세로 회차 완료를 판정한다. 움직이는 길은 여전히 추론뿐 |

0~5 가 요청의 범위다(개명, 오케스트레이터·검출기 이관, 자기 화면 포함). 0 이 맨 앞인 이유: 새로
만드는 파일이 처음부터 바른 이름으로 태어나야 한다 — 뒤에 하면 플러그인 코드까지 두 번 고친다. 6 은 외부 연동·자원 제한,
7 은 확장이라 뒤에 둔다. 3·3b 를 2 바로 뒤에 두는 이유: 저장소 자신의 루프와 검출기가
플러그인으로 돌아야 SDK 가 진짜 필요한 것을 갖췄는지 드러난다 — 예제 둘보다 좋은 시험이다.
이 둘이 끝나면 저장소에 "플러그인이 아닌 필요할 때 켜는 유닛"은 정책 서버와 YOLO 학습만 남는다.

## 10. 결정 기록 (2026-10-16)

| # | 질문 | 결정 |
|---|---|---|
| 1 | 설치 출처 | **zip·로컬 폴더만.** git URL 없음 |
| 2 | 범위 `*` | **둔다.** `/api/auth/*` 만 예외, 카드에 배지 |
| 3 | 플러그인도 estopd 대상인가 | **같이 죽는다.** 활동 `plugin:<name>` 으로 PID 등록 |
| 4 | GPU 플러그인 동시 실행 | **경고만.** 판단은 사람이 |
| 5 | 게이트웨이 재시작 뒤 | **수동 재시작.** 재부착 없음, 고아 유닛은 기동 시 정지 |
| — | 사과 분류기 같은 복합 앱에 맞는가 (10-17) | **맞다 — 플러그인 = 애플리케이션 컨트롤러.** 그래서 오케스트레이터와 겹치는 것이 문제였다 |
| — | 오케스트레이터와의 관계 (10-20) | **오케스트레이터 제거, 플러그인으로 대체.** 분리수거 루프 = 번들 플러그인 `recycling`. §12 |
| — | 검출기 동시 여러 개 (10-22) | **yolod 를 플러그인 `detector` 로 이관.** 한 프로세스에 모델 여러 개 또는 프로세스 여러 개. `yolo:meta` 는 발행자별 키로 |
| — | 자기 화면의 서빙 포트 | **같은 포트.** 정적은 게이트웨이가 서빙, 서버는 로컬 소켓 프록시. 별도 포트는 로그인 관문 밖이라 거절 — 표는 §8-2 |
| — | 자기 화면을 띄우는 자리 (10-22) | **"UI 보기" 버튼 → 새 창.** iframe·사이드바 직항 폐기. 조종 창과 같은 패턴, 메인 창이 heartbeat 를 쥔다 |
| — | `yolo` 라는 단어 (10-22) | **프로젝트 전체에서 뺀다.** 화면 문구만 막던 테스트를 트리 전체로. CHANGELOG·git 이력만 예외. §14 |

## 11. 손대는 곳

| 파일 | 일 |
|---|---|
| `backend/app/services/plugins.py` (신규) | 스캔·검증·소유자·토큰·고아 정리·활동 PID |
| `backend/app/core/plugin_scopes.py` (신규) | 범위 → (메서드, 경로 접두사) 표, `*` 규칙 |
| `backend/app/core/authgate.py` | Bearer 플러그인 토큰 분기 · `/plugins/` 접두사를 쿠키 보호 대상에 |
| `backend/app/routers/plugins.py` (신규) | `/api/plugins/*` |
| `backend/app/services/exclusivity.py` | `Activity.PLUGIN` + LABELS + ESTOP_TARGETS + STOPPERS |
| `backend/app/core/config.py` | `plugins_dir` |
| `plugin/` (신규 설치 패키지 `piper_plugin`) | SDK(`Plugin`·`MissionPlugin`·`Decision`·`detector`·`detections`) + `__main__`(`new`·`check`·`run`) + 템플릿(`mission`·`loop`·`detector`, 정적 UI 틀에 E-stop 버튼) + `serve_ui` |
| `plugins/README.md` (신규) | 에이전트·사람이 함께 읽는 계약 요약·금지 사항·검증 순서 (§7-2) |
| `plugins/detect-and-start/`, `plugins/zone-guard/`, `plugins/ui-demo/` (신규) | 예제 — 저장소에 두고 설치 시 데이터 루트로 복사 |
| `plugins/recycling/` (신규 번들) | 분리수거 루프 — 오케스트레이터 1단계의 플러그인판 (§12) |
| ~~`backend/app/services/orchestrator.py`~~, ~~`routers/orchestrator.py`~~, ~~`tests/test_orchestrator*.py`~~ | **삭제** (§12) |
| `backend/app/routers/external.py` | `missions` 를 플러그인 레지스트리 위에 다시 배선, `status.orchestrator` → `status.plugins` |
| `backend/app/routers/ws.py`, `core/ws_messages.py`, `frontend/src/types/ws.ts` | `orchestrator` 이벤트 타입 삭제 |
| `frontend/src/pages/VisionPage.tsx` | 루프 시작/정지·회차 이력 패널 삭제(플러그인 상세로 간다) |
| `backend/app/routers/models.py` | `GET /api/models/inference/policy` — 정책 종류(`takes_language`)를 플러그인이 묻는 길 |
| `plugins/detector/` (신규 번들) | yolod 의 플러그인판 — 모델 목록 × 카메라 목록, 한 프로세스 (§13) |
| ~~`daemons/yolod.py`~~ | **삭제** (3b). `_payload`·`detections_text`·`_model_meta` 는 SDK 로 |
| `daemons/detector_loader.py` → `plugin/piper_plugin/detector.py` | 이동. `yolo_prelabel.py` 가 SDK 에서 import |
| `bus/piper_bus/contract.py`, `client.py` | `YOLO_META` → `VISION_META_PREFIX:<publisher>`, `put/get_vision_meta(publisher)`, `vision_meta_names()` |
| `backend/app/routers/vision.py` | `_yolod_pm` 삭제. `start·stop·status` 는 플러그인 `detector` 위의 얇은 호환층(한 릴리스), `models·segments·snapshot·judge·detections·preview` 는 그대로 |
| `frontend/src/pages/YoloDemoPage.tsx`, `VisionPage.tsx` | 상태·메타를 **발행자별**로 그린다. 시작/정지는 플러그인 API 로 |
| `frontend/src/pages/PluginsPage.tsx`(목록), `PluginDetailPage.tsx`(상세·탭·"UI 보기" `window.open`), `config/pages.ts`(두 줄), `SpecFields.tsx`(kind 확장) | 화면 |
| `backend/app/routers/plugin_ui.py` | 정적 서빙(`ui/`, index 폴백) + 소켓 프록시(HTTP·WS) 둘 다 여기 |
| §14 의 개명 표 전부 | 0단계 |
| `frontend/nginx.conf`, `frontend/vite.config.ts` | `/plugins/` 프록시 블록(WS 포함) |
| `deploy/stage-hostside.sh`, `backend/Dockerfile` | `piper_plugin` wheel/설치 |
| `backend/tests/test_plugins.py`, `test_plugin_scopes.py`, `test_authgate.py` | 매니페스트 검증·범위 표가 라우터와 맞는지·`/plugins/` 가 잠기는지 |

## 12. 오케스트레이터 → 플러그인 이관

[episode-orchestrator](episode-orchestrator.md) 1단계는 백엔드 안의 async 상태기계로 분리수거
루프를 하드코딩했고, 2단계로 스텝 레지스트리 + YAML 스펙을 계획했다. 플러그인이 들어오면
"미션 수준 루프를 만드는 길"이 둘이 된다. 사과 분류기 같은 복합 앱은 YAML 로 못 쓰고
결국 파이썬이 되므로 **한 길만 남긴다 — 플러그인.** 오케스트레이터가 지키던 원칙(스텝은
Python, 전 구간 E-stop 취소, 저널, 판단 실패는 회차 스킵)은 전부 플러그인 SDK 로 옮겨 간다.

### 12-1. 무엇이 어디로 가나

| 오케스트레이터의 것 | 플러그인에서 |
|---|---|
| `EpisodeOrchestrator` 상태기계 (`services/orchestrator.py`) | SDK `MissionPlugin`(§7-1) 뼈대 + 번들 `plugins/recycling/main.py` 의 훅 넷. 회차 = 뼈대의 한 바퀴 |
| `OrchestratorConfig` (회차 수·대기·규칙·프로바이더·dry_run) | `plugin.yaml` 의 `config` — SpecFields 가 그린다. 규칙 텍스트는 `kind: text` 멀티라인 |
| 스텝 `capture` — `bus.get_detections()` + 신선도 | SDK `self.detections(cam, fresh_s)` (yolod 가 돌고 있어야 한다 — 시작 전 `vision.read` 로 확인, 없으면 로그와 함께 대기) |
| 스텝 `judge` — `llm_client.judge()` 직접 호출 | **REST** `POST /api/vision/judge` (범위 `vision.judge`). 플러그인은 별도 프로세스라 HTTP 가 맞는 경계다 — "자기 HTTP 호출 금지" 는 게이트웨이 **안**의 규칙이었다. LLM 클라이언트·프로바이더 설정은 게이트웨이에 남는다 |
| 스텝 `set_task` — 버스 파라미터 | `POST /api/params {"task": ...}` (범위 `params.write`) |
| 스텝 `wait_done` — 타임아웃 (G3 대기) | SDK `self.wait(seconds, until=callable)` — 타임아웃이 기본, 플러그인이 카메라로 판정하면 `until` 에 넣는다. 센서 이벤트(로드셀·스캐너)는 여전히 없다 |
| 스텝 `eval` — `eval_log` | `POST /api/eval/log` (범위 `eval.write`) |
| 스텝 `reset` | `POST /api/params {"reset": ...}` 또는 정책 재시작 — 지금 하는 그대로 REST |
| `_require_actionable_policy` — ACT 에 task 를 줘도 조용히 헛도는 침묵 제거 | 신규 `GET /api/models/inference/policy` → `{type, takes_language}`. SDK `self.require_language_policy()` 가 아니면 **시작을 거부**한다 — 같은 침묵을 같은 자리에서 막는다 |
| E-stop·추론 사망·정지 요청 0.5초 폴링 | SDK 기본 동작 — `loop()` 사이마다 `bus.last_estop()`·활동 PID·SIGTERM 을 본다. 플러그인은 estopd 대상이기도 하다(§3-7) |
| 회차 JSONL 저널 (`log_dir/orchestrator/run_*.jsonl`) | SDK `self.journal(record)` → `<plugin>/.runtime/runs/<run_id>.jsonl`. `GET /api/plugins/{name}/runs`·`/runs/{id}` 가 읽는다 |
| WS `orchestrator` 이벤트 → 파이프라인 뷰 | 상태 KV(`phase`, `episode`, `decision`) + 저널. "YOLO 박스 → LLM 명령문 → VLA" 뷰는 `recycling` 의 `ui: true` 화면으로 — 번들 플러그인이 자기 화면의 첫 실례가 된다 |
| `Activity.ORCHESTRATOR` 배타 (녹화·학습·편집·조종과 상호 배타) | 삭제. 플러그인은 **추론을 통해서만** 팔에 닿고, 추론이 이미 그 배타를 진다. 플러그인 자체는 `Activity.PLUGIN`(기본 무배타) |
| `/vision` 페이지의 시작/정지·회차 이력 패널 | 삭제 — 플러그인 상세(`/plugins/recycling`)가 그 자리다 |
| 테스트 `test_orchestrator.py`·`test_orchestrator_policy_kind.py` | `plugins/recycling/tests/` 로 — 드라이런 3회차 완주와 정책 종류 거부가 그대로 기준 |

### 12-2. 외부 API 는 깨지 않는다

`/api/ext/v1/missions` 는 v1 계약이다([external-api](external-api.md) "버전을 못 박는다").
표면은 그대로 두고 **구현만 플러그인 레지스트리 위에 다시 배선한다.**

| v1 | 뒤에서 |
|---|---|
| `POST /missions {"type": "recycling", ...}` | `type` = 플러그인 이름. 나머지 본문 = 그 플러그인의 `config`. 플러그인 시작 + 실행 토큰 발급. 동시 1개 제한은 **미션 타입 플러그인**(매니페스트 `mission: true`)에만 — 그 외 플러그인은 몇 개든 |
| `GET /missions/{id}` | 도는 중이면 상태 KV, 끝났으면 저널 `runs/<id>.jsonl`. `id` = 저널 run id — 지금과 같다 |
| `POST /missions/{id}/cancel` | 플러그인 정지 |
| `GET /status` 의 `orchestrator` | `plugins: [{name, state, phase, episode}]`. `orchestrator` 키는 **한 릴리스 동안 `null` 로 남긴다** — 낡은 클라이언트가 키를 읽다 죽지 않게 |

미션 타입이 늘어나는 길도 바뀐다: "미션 타입 추가 = 코드" 에서 "미션 타입 추가 = 플러그인
설치" 로. 사과 분류기는 `POST /missions {"type": "apple-sorter"}` 가 된다.

### 12-3. 없어지는 것, 그대로인 것

- **없어진다**: `services/orchestrator.py`, `routers/orchestrator.py`, WS `orchestrator` 타입,
  `Activity.ORCHESTRATOR`, VisionPage 루프 패널, `log_dir/orchestrator/` 신규 기록
  (기존 저널 파일은 읽기 전용으로 남겨 두고 플러그인 `runs/` 로 옮기지 않는다).
- **그대로다**: `llm_client`(게이트웨이 안, REST 로 노출), 검출(yolod 는 §13 에서 따로 플러그인이 된다 — recycling 은 발행자 이름으로 검출을 읽을 뿐이라 영향 없음), `/api/eval/log`, 외부 API 표면,
  "판단은 에피소드 경계에서만·슬롯 JSON 만" 이라는 [llm-integration](llm-integration.md) 의
  원칙(플러그인이 지키는 쪽으로 자리만 옮긴다).
- **episode-orchestrator.md 는 폐기 표시**만 하고 지우지 않는다 — 원칙과 거절한 대안(Lua 등)의
  근거가 거기 있다.

## 13. 검출기(yolod) → 플러그인 이관

### 13-1. 왜

yolod 는 단일 유닛이다 — `make_process("piper-yolod")` 하나, 프로세스 하나에 모델 하나, 카메라는
여러 대. 다시 시작하면 전 것을 대체한다. **검출기를 동시에 여러 개 돌릴 수 없다.** 사과 분류기는
일반 검출기(사과 찾기)와 커스텀 가중치(등급·결점)를 같은 카메라에 동시에 쓰고, 분리수거는
그 옆에서 다른 모델로 돌 수 있어야 한다. 플러그인은 프로세스 소유자가 플러그인마다 하나라 그
제한이 구조로 없다. 그리고 yolod 가 이미 플러그인 모양(shm 읽기 → 버스 발행, 필요할 때 켜는
유닛)이라 이관 비용이 가장 낮은 후보이기도 하다.

### 13-2. 두 가지 "여러 개"

| 모양 | 어떻게 | 언제 |
|---|---|---|
| **한 프로세스에 모델 여러 개** | 번들 `detector` 의 설정이 **목록**이다: `detectors: [{name, model, cams, conf, fps, imgsz}, ...]`. `loop()` 가 차례로 돈다. CUDA 컨텍스트를 공유해 GPU 메모리가 가장 적다 | 한 앱이 쓰는 검출기 묶음 (사과 찾기 + 등급) |
| **프로세스 여러 개** | `detector` 의 **인스턴스**(`detector:cam2`, §7-4) 또는 자기 모델을 올리는 다른 플러그인 | 서로 독립이어야 할 때(하나가 죽어도 남은 것이 돈다). 프로세스마다 CUDA 컨텍스트 수백 MB 가 더 든다 — 경고만(§3-8) |

### 13-3. 바뀌는 계약 — 하나

| 지금 | 뒤 |
|---|---|
| `piper:vision:<name>` 검출 최신값, `name` = 시작 인자의 alias | **그대로.** 발행자가 `name` 을 정한다(`apple_top`, `person_top`). 같은 이름을 두 플러그인이 쓰면 시작 시 409 |
| 페이로드 `{ts, cam, frame_seq, size, objects[], text, speed_ms, det_seq}` | + `publisher`(플러그인 이름) + `model`(파일명). 화면이 "어느 검출기가 낸 것"을 구분해 그린다 |
| `piper:yolo:meta` — **프로세스당 하나**, 둘이 돌면 서로 덮어쓴다 | `piper:vision_meta:<publisher>` — 발행자별. `put_vision_meta(publisher, meta)` / `vision_meta_names()`. TTL 은 지금처럼 루프마다 갱신 |
| `put_preview("yolo_<alias>")` 어노테이트 프리뷰 | **그대로.** 이름 충돌은 검출 이름과 같은 규칙으로 막는다 |

`detections_text()`(LLM 프롬프트 한 줄)와 `_payload()` 는 SDK `piper_plugin.detections` 로 옮겨
**생산자가 하나의 함수를 쓴다** — 지금 yolod 가 지키는 "같은 사실 두 곳 금지" 원칙 그대로다.

### 13-4. 무엇이 어디로 가나

| yolod 의 것 | 플러그인에서 |
|---|---|
| `daemons/yolod.py` 본체 | `plugins/detector/main.py` — `Plugin` 1층(§7). 설정 목록을 돌며 읽기 → 추론 → 발행 |
| `--cam alias=seg --model --fps --conf --imgsz` | `plugin.yaml` `config.detectors[]`. 화면은 목록 편집기(행 추가 = 검출기 추가), 각 행의 `cams` 는 스트림 선택기, `model` 은 `/api/vision/models` 카탈로그 선택기 |
| `detector_loader.py` (RT-DETR 어댑터, 세 데몬 공유) | `piper_plugin.detector` 로 이동. `yolo_prelabel.py`·`rtdetr_train.py` 는 SDK 에서 import. **라이선스 결정은 그대로** — 기본 RT-DETR(Apache), 번들은 ultralytics 를 쓰지 않는다 |
| `_model_meta` 자기소개 | SDK `self.publish_meta(publisher, meta)` — 발행자별 키 |
| `make_process("piper-yolod")` + `/api/vision/start·stop·status` | 플러그인 레지스트리. **세 엔드포인트는 한 릴리스 동안 호환층**으로 남긴다 — `start` 는 `detector` 플러그인을 그 인자로 시작, `status` 는 발행자 `detector` 의 메타를 돌려준다. yolo-demo·vision 페이지·ext API `vision/*`·yolo-train 의 "이 장면 캡처"가 전부 이걸 부른다 |
| 검출 학습(`piper-yolotrain` 유닛·라벨러·캡처) | **저장소 기능으로 남는다** — 플러그인이 아니다. 결과를 카탈로그에 떨구는 쪽이라 게이트웨이 소유가 맞다. §14 에서 이름만 개명 |
| `/api/vision/models`(카탈로그·커스텀 가중치 업로드·삭제) | **게이트웨이에 그대로.** yolo-train 이 best.pt 를 떨구는 자리이자 선택기의 원천이다 — 플러그인 소유로 넘기면 학습 쪽이 플러그인에 의존하게 된다 |
| `/api/vision/segments·snapshot·judge·detections·preview` | 그대로. `detections` 는 이미 모든 이름을 스캔한다 — 발행자가 늘어도 그대로 보인다 |
| YoloDemoPage·VisionPage 의 "yolod 상태" 한 칸 | **발행자별** 상태 — 메타 목록을 돌며 카드 하나씩. 시작/정지는 플러그인 상세와 같은 API |
| `Activity` | 지금도 yolod 는 활동이 아니다(배타 표에 없음). 플러그인이 되면 `Activity.PLUGIN` + `resources.gpu: true` — 학습과 배타가 **새로 생긴다.** 지금은 학습 중에도 yolod 를 켤 수 있었는데 막힌다. 올바른 방향이다(GPU 경합) |
| E-stop | 지금 yolod 는 estopd 대상이 아니다. 플러그인은 대상이다(§3-7) — **브라우저를 닫으면 검출기도 선다.** 무인 운용은 `heartbeat: true` |

### 13-5. 없어지는 것, 그대로인 것

- **없어진다**: `daemons/yolod.py`, `piper-yolod` 유닛 이름, `YOLO_META` 단일 키, `_yolod_pm`.
- **그대로다**: 검출 키·페이로드 뼈대·프리뷰 이름, `/api/vision/models`, yolo-train 전 구간,
  ext API `vision/start·stop`(호환층 경유), 라이선스 결정(RT-DETR 기본).
- **바뀌어 보이는 것**: 학습 중 검출기 시작이 409(GPU 배타), 브라우저를 닫으면 검출기가 선다.
  둘 다 화면이 사유를 말한다.

## 14. `yolo` 라는 단어를 프로젝트에서 뺀다

[test_no_model_branding.py](../backend/tests/test_no_model_branding.py) 는 **화면 문구**에서만
특정 검출 모델 이름을 막았다 — "구현은 갈아끼울 수 있어야 하고, 라이선스가 교체를 강제할 수도
있다"(그 파일 머리). 검출기는 이미 RT-DETR(Apache) 로 바뀌었고 학습도
[rtdetr_train.py](../daemons/rtdetr_train.py) 다. 이름만 남았다. **범위를 트리 전체로 넓힌다** —
파일·모듈·API 경로·설정 키·데이터 디렉토리·유닛 이름·버스 키·문서. 예외는 `CHANGELOG.md`
(이력은 다시 쓰지 않는다)와 `.git` 뿐이다.

대체어는 **`detector`** 하나다. 이미 `detector_loader.py`·"검출 학습"·"객체 검출" 그룹이
그 어휘를 쓴다. 둘을 섞지 않는다(`detect`·`det`·`vision` 은 기존 키가 쓰는 자리에서만).

### 14-1. 개명 표

| 지금 | 뒤 | 비고 |
|---|---|---|
| `daemons/yolod.py` · 유닛 `piper-yolod` | `daemons/detectord.py` · `piper-detectord` | 3b 에서 삭제되므로 **0단계에서는 이름만** |
| `daemons/yolo_prelabel.py`, `yolo_traind.py` | `detector_prelabel.py`, `detector_traind.py` | 유닛 `piper-yolotrain` → `piper-detector-train` |
| `services/yolo_train_manager.py`, `yolo_dataset.py` | `detector_train_manager.py`, `detector_dataset.py` | |
| `routers/yolo_train.py` · `/api/yolo/*` | `routers/detector_train.py` · `/api/detector/*` | UI 와 백엔드가 같은 이미지로 가므로 API 호환층은 없다 |
| `settings.yolo_models_dir` → `data/yolo_models` | `detector_models_dir` → `data/detector_models` | Dockerfile 데이터 경로 ENV 도 |
| `settings.yolo_datasets_dir` → `data/yolo_datasets` | `detector_datasets_dir` → `data/detector_datasets` | `.gitignore` 두 줄 |
| `Activity.YOLO_TRAIN` | `Activity.DETECTOR_TRAIN` | 라벨 "검출 학습" 은 이미 중립 |
| 버스 `YOLO_META`, `put/get_yolo_meta` | §13-3 의 `VISION_META_PREFIX:<publisher>`, `put/get_vision_meta` | 개명이 아니라 교체 |
| 프리뷰 키 `yolo_<cam>` | `det_<cam>` | 화면의 프리뷰 URL 이 바뀐다 |
| `/yolo-demo`, `/yolo-train` 라우트 | `/detector`, `/detector-train` | 옛 경로는 **한 릴리스 동안 리다이렉트**(북마크·`/yolo-demo?model=` 단축 링크) |
| `YoloDemoPage.tsx`, `YoloTrainPage.tsx`, `YoloLabeler.tsx` | `DetectorDemoPage.tsx`(§15 — 플러그인 UI 가 되면 삭제), `DetectorTrainPage.tsx`, `DetectorLabeler.tsx` | |
| 변수 `yoloTarget` 등 + 그걸 품은 i18n 키 두 개 | `detectorTarget` | 키가 바뀌므로 `tools/i18n.py sync` + `en` 채움 |
| `feature/yolo-training.md` | `feature/detector-training.md` | README 표·ROADMAP·refactor/daemon-inventory·데모 시나리오 md 의 링크 |
| 테스트 `test_yolod.py`, `test_yolo_train_api.py` | `test_detectord.py`, `test_detector_train_api.py` | |
| "YOLO 형식 라벨(`cls cx cy w h` 정규화)" 라는 **설명** | "txt 박스 라벨(`cls cx cy w h`, 정규화)" | **형식 자체는 그대로다** — 외부 라벨링 도구와의 호환이 그 형식에 있다. 이름만 바꾼다 |

### 14-2. 데이터 마이그레이션 — 기동 시 한 번

배포 기계의 `/data/yolo_models`·`/data/yolo_datasets` 에 커스텀 가중치와 캡처가 있다. 게이트웨이
기동 시 **옛 디렉토리가 있고 새 디렉토리가 없으면 `rename`** 한다(같은 파일시스템이라 원자적).
둘 다 있으면 건드리지 않고 경고를 남긴다(사람이 합친다). 소스 기계의 `backend/data/` 도 같은
코드가 처리한다. 사용자가 저장한 설정(JSON)에 옛 키가 있으면 읽을 때 새 키로 옮긴다.

### 14-3. 강제

`test_no_model_branding.py` 를 **트리 전체 검사**로 바꾼다: `.git`·`node_modules`·`dist`·
`__pycache__`·데이터 디렉토리·`CHANGELOG.md` 를 빼고 모든 텍스트 파일에서 `yolo`(대소문자 무시)를
찾아 하나라도 있으면 실패, 파일과 줄을 보고. 지금의 "한글 줄만" 휴리스틱은 없어진다 — 범위가
전체라 내부 이름도 걸려야 한다. 사용자 데이터(모델 파일 이름 `*.pt`)는 검사 대상이 아니다.

## 15. 남은 결정 — 전부 권고대로 (2026-10-22)

| # | 질문 | 결정 | 반영 |
|---|---|---|---|
| 1 | 번들 `detector` 의 UI | 저장소의 standalone 페이지 `/detector`, 매니페스트 `ui: /detector` | §8-2 |
| 2 | `MissionPlugin` + `commands` | 넣는다. `recycling` 이 첫 사용자 | §7-1, §12-1 |
| 3 | llm-integration 4단계 "LLM 플래너" | 폐기. 개발 시점의 코딩 에이전트가 대체 | §7-2, llm-integration.md |
| 4 | 에이전트가 읽는 `plugins/README.md` | 쓴다 | §7-2, §11 |
| 5 | 새 창 UI 의 E-stop | 템플릿 기본 포함 + 권고 | §8-2 |
| 6 | 검출 학습 | 저장소 기능으로 남긴다. 유닛 이름만 개명 | §13-4, §14-1 |
| 7 | 옛 URL·API 호환 | 한 릴리스. 라우트 리다이렉트 + `/api/vision/start·stop·status` 호환층, `/api/yolo/*` 는 없음 | §13-4, §14-1 |
