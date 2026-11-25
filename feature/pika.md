# PIKA — Sense·Gripper 를 PIPER STUDIO 에 붙인다 (기획)

상태: **◐ 2단계 — 장비 없이 시뮬 팔까지 ☑ (2026-11-25).** 1단계(웹에서 추가 → 확장 화면에 자세 실시간 → 재시작 뒤
다시 붙기, 2026-11-24)에 이어 PoseLeader·자세 조종 패널·손잡이 조명·진동을 가짜 Sense → `sim_follower1` 로 끝까지
돌렸다(§5-실측). 남은 것: 실기(Sense·기지국·동글 — 장비 아직 없음), 확장 화면의 캘리브레이션, OneEuro 실기 비교 (§10).

> 요청(2026-11-24): "PIKA 를 PIPER STUDIO 에 적용할 방법 기획. PIKA 는 ROS 도 필요하고 해서
> 도커로 구성하는 게 좋을 것 같은데. 확장성이나 편리성 고려해서."
>
> 결정(같은 날): ① 컨테이너 안은 **ROS 2 Humble** (pika_sdk 직접은 0단계 실측으로만 바꾼다)
> ② Gripper 의 그리퍼 자리는 **robotd** 가 잇는다 ③ **손 수집도 이 기획에** 넣는다
> ④ **웹에서 추가**한다. PIKA 는 **별도 저장소 `pika-docker`** 로 시작하고, 이 저장소에 submodule 은 두지 않는다.

근거: 사내 도구 [pika-setup-ui](https://github.com/WeGo-Robotics/pika-setup-ui)(README·화면),
AgileX [pika_ros `ros2` 브랜치](https://github.com/agilexrobotics/pika_ros/tree/ros2)(런치·스크립트·package.xml),
[pika_sdk](https://github.com/agilexrobotics/pika_sdk) API 문서, 사내 문서 둘 — `PIKA_설치운용가이드_20261116.html`(2026-11-16
실장비 확인판)·`Pika_PiPER_Teleop_OneEuro_적용정리.html`. 둘은 pika-docker `docs/reference/` 에 있다.

## 0. 요지

- **두 저장소.** `pika-docker` 가 PIKA 의 전부를 든다: 이미지(ROS 2 Humble·libsurvive·pika_ros)·브리지·PIKA
  전용 화면. **이 저장소는 PIKA 를 모른다** — 여기 생기는 것은 전부 범용이다: 장치 확장 추가, 자세 리더,
  외부 그리퍼, 말단 action space. PIKA 는 그 넷의 첫 사용자다.
- **잇는 것은 코드가 아니라 계약과 이미지다.** 이미지는 자기 매니페스트를 라벨로 들고 오고, 런타임에 버스
  RPC·shm·heartbeat 로만 만난다 ([so101d.md §0](so101d.md) 의 세 통로). ROS 는 컨테이너 밖으로 안 나온다.
- **웹에서 추가, sudo 없음.** 설정 → 서비스 → [확장 추가] 에 이미지 주소를 넣으면 unitd 가 받아서 사용자
  유닛으로 켠다. 데몬은 원래 `systemctl --user` 이고 설치 사용자는 docker 그룹이다. PIKA 의 udev 규칙은
  `uaccess`(로그인 사용자 권한) 뿐이라 root 로 도는 컨테이너에는 필요 없다.
- **카메라(어안·D405)는 컨테이너가 열지 않는다.** camerad·rsd 가 지금처럼 독점한다.
- 순서: **텔레옵 → 녹화 → Gripper 장착 → 손 수집.**

## 1. 무엇이 어디에

| | `pika-docker` (별도 저장소) | 이 저장소 (범용) |
|---|---|---|
| 장치 | 이미지 · 런치 · 브리지 (트래커·시리얼 → 계약) | — |
| 설치 | 매니페스트 (`extension.json` → 이미지 라벨) | **장치 확장 추가** (§2) |
| 화면 | 좌/우 배정 · 기지국 캘리브레이션 · 검수 (확장 자체 화면) | 확장 목록·상태·로그, 화면 프록시 |
| 텔레옵 | 자세를 낸다 | **PoseLeader** (§5) |
| Gripper | 모터를 쥔다 | **외부 그리퍼** — robotd 가 잇는다 (§6) |
| 손 수집 | 자세·벌림·중력을 낸다 | **`pose_shm` 로봇 + `ee_delta_v1`** (§7) |

### 1-1. submodule 없이 — 의존 방향은 한쪽뿐

- **pika-docker → 이 저장소**: 계약 패키지 `piper_shm`·`piper_bus` 만. 이미지 빌드 때 BuildKit **이름 붙은
  컨텍스트**로 받는다 — 개발은 옆 체크아웃(`--build-context piper=../wego-piper-web`), 릴리스는 git 태그
  (`…wego-piper-web.git#v0.5.x`). 복사본도 submodule 도 없다.
- **이 저장소 → pika-docker**: 없다. 이미지 주소와 매니페스트를 런타임에 알 뿐이다. 매니페스트의
  `contract` 가 모르는 버전이면 추가를 거절한다.
- **vcstool 은 pika-docker 안에서만** — AgileX 업스트림(`pika_ros@ros2`, `data_msgs@ros2`)을 커밋으로 고정하는
  데 쓴다(`pika.repos`). 이 저장소를 vcstool 로 끌어가지 않는다: 하위 디렉토리(`shm/`·`bus/`)만 가져올 수 없고,
  비공개 저장소를 빌드 안에서 SSH 로 통째 받아야 하고, 로컬 수정으로 개발할 길이 없다. 이름 붙은 컨텍스트는
  셋 다 된다.

## 2. 장치 확장 — 웹에서 추가

```
[설정 → 서비스 → 확장 추가]  이미지 주소 (추천 목록 + 직접 입력)
   → 게이트웨이: 허용 레지스트리인가 (ghcr.io/wego-robotics · 설정한 사설 레지스트리)
   → unitd (호스트, 사용자 유닛, docker 그룹):
        docker pull (진행률을 버스로 — 업데이트 화면과 같은 길)
        라벨 io.wego.piper.extension 읽기 → contract·장치 어휘 검사
        고정 템플릿으로 ~/.config/systemd/user/piper-ext-<name>.service 작성
        daemon-reload → 켜기 → 동적 카탈로그에 등록
   → 서비스 화면에 다른 데몬과 나란히. 끄기·부팅 시 시작·로그·재시작이 그대로 된다
```

- **매니페스트는 ExecStart 를 못 쓴다.** 유닛은 unitd 의 고정 템플릿이고, 매니페스트가 고르는 것은 장치
  어휘뿐이다: `usb-serial`(188)·`usb-raw`(189)·`hidraw` → `--device-cgroup-rule`. 늘 붙는 것: `ipc: host`,
  `/run/redis` 소켓, `network none`, 데이터 `/srv/piper-data/ext/<name>` → `/data`. `privileged`·`cap_add`·임의
  마운트는 어휘에 없다. 이미지를 고르는 사람이 곧 장치 권한을 주는 사람이라 허용 레지스트리가 1차 관문이다.
- **허용**: 허용 레지스트리(`ghcr.io/wego-robotics/` + `PIPER_EXT_REGISTRIES`)에서만 받는다. **이 기계에 이미 있는
  이미지**는 받지 않고 쓴다 — docker 에 손이 닿는 사람이 이미 올린 것이다(개발 빌드 `pika-docker:dev`). 어느 쪽이든
  라벨 매니페스트가 없으면 거절. 매니페스트 검사: 이름, `contract` 1, 장치 어휘, RPC 허브(핵심 데몬 이름·다른 확장과
  겹치면 거절), `provides` 모양, `ui` 는 `server` 뿐.
- **유닛**(`daemons/ext_unit.py` 의 고정 템플릿): **검사한 이미지 ID** 로 돈다 — 태그가 나중에 다른 이미지를 가리켜도
  검사 안 한 것이 안 돈다. 장치가 있으면 `-v /dev:/dev`(뒤에 꽂힌 장치도 보이게) + 어휘의 `--device-cgroup-rule` 만
  (열 수 있는 것은 그 major 뿐). hidraw major 는 `/proc/devices` 에서 읽고 unitd 가 뜰 때마다 유닛을 다시 쓴다.
  `ExecStop` 은 `docker stop` — 이미지가 `STOPSIGNAL SIGINT` 여야 깔끔히 내린다(pika-docker 실측: SIGTERM 은
  컨테이너 1번이 무시해 10초 뒤 SIGKILL, 자세 세그먼트가 남았다).
- **등록부**: `UNIT_CATALOG` 은 그대로 정적이다. 확장은 unitd 소유 `~/.config/piper-web/extensions.json` 에 있고
  (데이터 루트의 `config/` 는 게이트웨이 컨테이너가 root 로 만들어 사용자 unitd 가 못 쓴다 — 실측), 게이트웨이는
  RPC(`ext_list`)로만 본다. unitd 의 허용 목록 = 정적 + 등록된 확장(`ext-<name>`, 종류 `extension`). 테스트가
  `deploy/systemd/*.service` 와 대조하는 규칙은 정적 쪽에만 걸린다.
- **업데이트**: 같은 주소로 다시 추가 — 받기 → 검사 → 새 ID 로 유닛을 다시 쓰고 재시작한다.
  **제거**: 유닛 정지·삭제, **우리가 받은 이미지만** 삭제(이 기계에서 빌드한 것은 남긴다). 데이터(캘리브레이션·배정)는
  남긴다 — 다시 추가하면 그대로 쓴다. 지우려면 API `purge=true`(안의 root 파일은 그 이미지로 지운다). 제거는 활동 중에
  막는다(`require_idle`).
- **누가**: 플러그인 토큰은 `*` 로도 `/api/system/extensions` 에 못 닿는다(`NEVER_PREFIXES`) — 장치 권한을 주는 일은
  사람이 한다. 로그인이 꺼져 있으면 막지 않고 서비스 화면이 경고한다(결정 2026-11-24, §12-3).
- **확장 화면**: 플러그인 `ui: server` 의 유닉스 소켓 프록시([plugins.md §8](plugins.md))를 함수로 나눠 같이 쓴다 —
  소켓은 `<데이터 루트>/ext/<name>/run/ui.sock`(게이트웨이 컨테이너는 `PIPER_EXT_DIR=/data/ext`), 경로는
  `/ext/<name>/ui/`(nginx·vite 에 한 줄씩). 확장은 소켓 주인을 데이터 디렉토리 주인(호스트 사용자)으로 넘기고 0660 —
  소스로 도는 게이트웨이(사용자)도 붙는다. **새 창**으로 연다 — 메인 창이 남아 heartbeat·E-stop 이 산다(플러그인
  화면과 같다). 문장은 다국어 규칙을 따르고 사전(`i18n/en.json`)은 확장이 든다. 언어는 앱이 저장한 `piper.lang` 을
  같은 출처에서 읽고, 없으면 원문(한국어) — 앱과 같은 규칙이다.
- **이 저장소에 PIKA 라는 이름이 남는 자리**: 추천 목록의 이미지 주소 한 줄과 이 문서뿐이다.

## 3. 컨테이너 안 (요약 — 자세한 것은 pika-docker `docs/design.md`)

| 것 | 자리 | 근거 |
|---|---|---|
| `pika_locator` (libsurvive, 트래커 자세) | 안 | **소스가 없다** — `source/install.zip` 의 22.04 완성본뿐(glibc ≥2.34). 이것이 컨테이너의 진짜 이유다. `/pika_pose`(PoseStamped) **200~250Hz**, 정지 흔들림 **~1mm**(가이드 실측). 양팔은 `/pika_pose_l`·`_r`. 캘리브레이션 = `survive-cli --force-calibrate` |
| `serial_gripper_imu` (손잡이 벌림·제스처·조명·진동, Gripper 모터) | 안 | `sensor_tools` 의 C++ 노드(시리얼 460800, JSON). 벌림 `/gripper/data` ~125Hz. **손잡이 두 번 쥐기**: 시리얼 `Command` 가 바뀌면 `/teleop_trigger`·수집 서비스를 부른다(매뉴얼의 텔레옵 시작 동작). **조명**: 상태 토픽을 구독한다(텔레옵·수집 진행 초록·실패 노랑, `/localization_status.accurate=false` 빨강). **진동**: `/arm_control_status.over_limit`. IMU 는 Sense 에서 안 나온다(Ego 헬멧 전용) |
| 어안 (`usb_camera.py`)·D405 (`realsense2_camera`) | **밖** — camerad·rsd | 업스트림 `open_single_sensor` 런치는 넷을 다 띄우고 끄는 인자가 없다 → **우리 런치**가 앞의 둘만 띄운다 |
| IK·정합·필터 | 밖 — 게이트웨이 | 팔로워 상태·`ArmModel`·안전 가드가 거기 있다 |

- `network none` + `ROS_LOCALHOST_ONLY=1`: 같은 LAN 의 로봇들(.117·.120)이 도메인 0 으로 DDS 를 뿌리면 남의
  Sense 자세가 섞인다(사내 가이드도 같은 망의 ROS 장비를 경고한다). 버스는 유닉스 소켓, 데이터는 shm 이라 네트워크가 필요 없다.
- 이미지는 **처음 추가할 때만** 받는다 — PIKA 없는 로봇에 받지 않는다. 가이드 설치(약 8GB) 중 librealsense 빌드·
  ROS desktop·RViz 는 이미지에 안 든다(D405 는 rsd 것, 화면은 웹).

## 4. 계약 — 이 저장소에 생기는 것

| 이름 | 내용 |
|---|---|
| `piper.pose.<id>` (shm, `piper_shm.pose` ☑) | 80바이트 레코드, seqlock 슬롯 4: `wall_ns`(소스가 잰 시각), 위치 xyz(m, float64), 쿼터니언 xyzw, 벌림(mm — 없는 소스는 NaN → `None`), 플래그(`tracking_ok`·`encoder_ok`), 트리거 횟수 `trigger_seq`(두 번 쥐기 — 읽기 사이에 두 번 와도 안 놓친다. 소스가 재시작하면 0 부터 — 줄면 새 기준). 매직 `PIPO`. 세그먼트는 **첫 자세가 올 때** 생기고 소스가 내려가면 지운다 — 있는 것 자체가 "자세가 온다" 는 뜻 |
| `piper.grip.<id>.{state,action}` (shm, 새 레코드) | 외부 그리퍼: 벌림(mm)·힘·enabled·전압·오류, action 에 데드맨 (§6). 전압이 있어야 화면이 "24V 가 안 들어왔다"(가이드의 첫 번째 문제)를 말한다 |
| 버스 RPC 허브 (이름은 매니페스트) | 범용: `scan`·`info`·`attach`·`release`·`estop`. 매니페스트 `features` 에 `feedback` 이 있으면 `feedback(id, {engaged, blocked, buzz})` ☑ — PoseLeader 가 바뀔 때와 1초마다 보낸다(§5). 캘리브레이션 같은 고유 기능은 확장 화면이 컨테이너 안에서 직접 한다 |
| heartbeat | estopd·서비스 화면이 본다 |

- **소스의 몸체 축은 REP-103(x 앞·y 왼·z 위)** 이라고 본다 — PIKA Sense 가 그렇다. 다른 소스가 다르면 브리지가 돌려서 낸다.
  팔 끝 축은 팔 모델이 정한다(`pose_leader.AXES`).

기각: 팔 7f 레코드에 자세를 욱여넣기(xyz+rpy+그리퍼 = 7). 자릿수는 맞지만 정규화 관절 규약을 믿는 소비자
(녹화·조그·파킹·장치 화면)가 팔로 오인한다.

## 5. A·B — 텔레옵과 녹화: PoseLeader

[웹 리더](web-leader.md)가 "속도 → 적분 → IK → 가상 리더" 였다면 PoseLeader 는 "**절대 자세 → 정합 → IK →
가상 리더**" 다. 소스는 `piper.pose.*` 무엇이든 — PIKA 를 모른다.

```
T_목표 = T_팔로워정합 · R · (T_소스정합⁻¹ · T_소스) · R⁻¹   ← 릴레이 POSE 크로스 모델 식 (relay.py) + 축 정렬 R
       → 위치 배율(옵션) → ArmModel.ik(시드 = 직전 해)
       → lowest_z 바닥 · POSE_MAX_STEP_MM/DEG · POSE_MAX_JOINT_STEP_DEG
       → OneEuro (관절마다, rad — PR #18 원본)
       → piper.arm.<가상 리더>.state (PIPER 정규화 7f) → relay(관절 복제) → robotd filter_goal
```

- **정합(클러치)**: 물리는 순간 첫 목표 = 팔로워 현재 자세라 점프 0. 해제하고 옮겨 다시 물리면 작업 공간을
  이어 쓴다 ([so101d.md §5-공통](so101d.md)). 손: 텔레옵 패널 버튼 + 발판(키) + **손잡이 두 번 쥐기**(`trigger_seq` 가
  늘면 토글 — 매뉴얼의 시작 동작과 같다).
- **축 정렬 R**: Sense 는 x 앞·y 왼·z 위, PiPER 끝단은 z 앞·x 아래 — PikaAnyArm 은 y 축 90°(`handle_pose_pitch: 1.57`)로
  잇는다. 팔 모델별 상수로 `ArmModel` 옆에 둔다.
- **정합 때 자세를 맞춘다**: 몸체 좌표 상대 매핑이라 Sense 를 든 방향이 팔 끝 방향과 다르면 "앞으로" 가 팔에서는
  다른 쪽이 된다 — 매뉴얼이 가장 강조하는 주의다. 텔레옵 패널이 정합 전에 팔 끝 방향을 보여 준다. (대안인 트래커
  세계 축 매핑은 캘리브레이션마다 "앞" 을 다시 재야 해서 — 가이드 10단계 — v1 에서는 안 한다.)
- **녹화(B)**: 가상 리더는 PIPER 관절 그 자체라 `piper_leader_shm` 이 읽으면 끝 — 지금과 같은 관절 데이터셋.
- **그리퍼**: 소스 벌림 **mm → 팔로워 그리퍼 mm**. 정규화끼리 옮기지 않는다 (.117 행정 눈금 98↔68mm 사고).
- **추적 상실**: `tracking_ok=false` 이거나 0.5초 낡으면 **그 자리에 붙잡는다** — 가상 리더는 마지막 목표를 계속
  낸다(녹화가 멈춘 리더를 읽게). 처음엔 "보내지 않는다(릴레이 `STALE_S`)" 였는데, 그러면 PoseLeader 가 산 채로 리더가
  낡아 보여 녹화가 리더 끊김으로 읽는다. PoseLeader 자체가 죽으면 그때 릴레이 `STALE_S` 가 선다(두 겹).
  가림 뒤 **딴 자리에서 다시 잡히면**(한 틱에 100mm·45° 넘게 튀면) 따라가지 않고 **해제**한다 — 다시 정합.
- **진동·조명**: 막힐 때(도달 불가·바닥·걸음 상한·추적 상실) 확장에 `feedback` 을 보내 Sense 를 떨게 하고, 정합
  중이면 초록 불을 켠다 — 사람은 화면이 아니라 팔을 보고 있다. 업스트림 노드가 이미 하는 일이라(§3) 브리지가
  상태 토픽만 내면 된다. 확장이 `feedback` 을 선언했을 때만 보낸다.
- **OneEuro 는 여기 한 곳, IK 뒤 관절에** — 가상 리더 앞이라 팔과 녹화가 **같은 필터된 값**을 받는다(PR #18 원칙:
  "녹화 전, 로봇 직전"). pikad 는 원값을 낸다 — 손 수집(§7) 데이터에 텔레옵용 필터가 구워지면 안 된다.
  - **PR #18 원본**(NERO_DualArm_VR `common/one_euro.py`, 30줄)을 그대로 가져온다. 원본은 IK 가 낸 **관절값에 관절마다**
    건다 — 추적 떨림과 IK 떨림(NERO 실측 ~0.5°)을 한 번에 잡고, 쿼터니언·회전벡터 문제가 아예 없다. 사내 정리 문서의
    "IK 전 자세에 건다" 스케치(벡터 노름 컷오프, 첫 샘플 기준 회전벡터 — 180° 근처에서 끊긴다)보다 이쪽이다.
  - 기본값 = 원본 기본 `--smooth 2 2`: **`min_cutoff` 2Hz, `beta` 2 (rad/s 당)**. 녹화 161건 오프라인 측정으로 관절 속도
    거칠기 0.38→0.23, 지연 ~42ms (`1 1`: 0.19·75ms, `4 2`: 0.26·28ms). 관절 단위가 rad 라 값이 그대로 옮겨 온다.
    **로봇 실기 검증은 PR #18 도 아직 없다** — 2단계 끝나는 기준이 그것이다.
  - 그리퍼는 걸지 않는다(원본도 팔 관절만). 정합·해제 때 리셋한다(영점이 바뀌면 이전 값이 튀어 들어온다).
  - 튜닝: `beta=0` 에서 `min_cutoff` 를 내려 정지 떨림이 사라지는 점을 찾고, 느리면 `beta` 를 올린다. 파라미터는
    텔레옵 패널 + [프리셋](parameter-presets.md).
- **양팔**: 소스 둘 → PoseLeader 둘 → 기존 [양팔 조종](bimanual.md). **시뮬 먼저**: 팔로워 `sim_follower1` 로
  매핑·필터·배율을 맞춘 뒤 실팔.
- **주기·지연**: 소스는 200~250Hz 다. PikaAnyArm 은 30Hz 로 솎아 IK 하고, PR #18 은 30Hz 명령 사이를 50Hz 로
  선형 보간했다 — 같은 목표가 반복돼 생기는 멈칫임 때문이다. PoseLeader 는 더 빨리 돌 수 있다(IK 2.8ms 실측).
  릴레이(30Hz)까지 두 홉이라 최악 ~66ms 가 더해지므로, 재 보고 부족하면 릴레이 주기를 올리거나 녹화가 아닐 때
  팔로워 action 에 직접 쓴다.
- **끝나는 기준**: 사내 문서의 A/B/C(필터 끔·기본·약하게)를 같은 동작(천천히 → 빠르게 → 방향 전환 → 정지, 30초)
  으로 비교한다 — 속도 거칠기·지연·멈칫임. 떨림이 추적 자체에서 오면(햇빛·반사·가림) 필터가 아니라 환경을 고친다.

### 5-구현 (2026-11-25) — `app/services/pose_leader.py`, `routers/pose_leader.py`, `PoseTeleopPanel.tsx`

- **자리**: 웹 리더와 같다 — 가상 리더 `piper.arm.pose_<팔로워>.state`(30Hz) + 릴레이(관절 복제, `leader=pose_<팔로워>`).
  팔로워마다 세션 하나(양팔 = 둘), 한 소스로 두 팔을 끌지 않는다. 배타는 `TELEOP`(`allow_self`) — 같은 팔은 `teleop_session`.
  E-stop 은 `_stop_teleop` 이 세션까지 닫고, 릴레이가 다른 길로 끝나도 세션이 알아채고 같이 끝난다(가상 리더를 지운다).
- **시작은 해제 상태** — 첫 값 = 팔로워의 지금 자세라 붙는 순간 점프 0. 정합은 [정합]·**두 번 쥐기**(`trigger_seq` 가
  홀수만큼 늘면 토글, 줄면 새 기준) — §12 Q2 결정대로 텔레옵 중이니 정합이다. 정합 앵커는 **지금 내는 값**(필터 뒤)이고
  필터를 거기서 새로 시작한다 — 해제도 같다(필터가 남은 걸 끌고 가지 않는다).
- **축 정렬 R = y 축 −90°** (소스 x → 끝단 z, 소스 z → 끝단 −x). 시뮬에서 손잡이 앞 50mm → 끝단 앞 축 49.5mm(필터 지연 0.5),
  손잡이 20° 숙임 → 끝단 20°. 정합 전 맞춤 도우미: 끝단 앞 축·왼 축과 손잡이 x·y 축의 **올려본 각**(중력 기준)을 나란히 —
  15° 안이면 ✓. 방위(yaw)는 트래커와 로봇 사이를 모르니 비교하지 않는다.
- **따라잡기**: 한 틱 30mm·20°(릴레이 POSE 상한)까지만 목표가 움직인다 — 위치 직선, 자세 slerp. 손이 빠르면 팔이 늦게
  따라오지만 길은 곧다(관절 공간에서 잘리면 길이 휜다).
- **IK**: 시드 = 직전 해, 잔차 5mm·3° 안이면 해(웹 리더와 같다 — 한계 근처 `ok` 는 2mm 로도 거짓), 관절 25° 튐·바닥이면
  붙잡고 이유. **바닥은 더 내려가는 것만 막는다** — 바닥 설정을 나중에 켜서 이미 아래에 있어도 올라오는 길은 연다
  (웹 리더·릴레이 POSE 는 아래에 있으면 전부 막혀 못 빠져나온다).
- **그리퍼**: 정합 중에만, `encoder_ok` 일 때만 — 손잡이 mm × 1000 / 팔로워 `raw_max_um`(robotd `get_gripper_stroke`,
  시뮬·모르는 팔은 68000). 필터 안 건다. 해제한 그 틱에도 안 따라간다(실측에서 잡은 것: 77.8 → 29).
- **손잡이 조명·진동**: 확장 매니페스트가 `feedback` 을 선언하면(`unitd ext_list` 로 소스 → 확장을 찾는다) 바뀔 때 + 1초마다
  `feedback(id, {engaged, blocked, buzz})`. 진동은 막힌 순간 한 번, 계속 막히면 2초마다. 확장이 죽어 있으면 안 보낸다(RPC 큐에
  쌓였다가 다시 뜬 확장이 낡은 상태를 한꺼번에 먹는다). 브리지는 `/teleop_status`(초록·노랑)·`/arm_control_status`(진동 한 번)
  를 내고, 3초 끊기면 불을 끈다.
- **파라미터**: `min_cutoff`(0.1~10)·`beta`(0~10)·위치 배율(0.25~2) — 패널 슬라이더, 도는 세션에 바로. 배율을 바꾸면 지금
  자리에서 다시 정합한다(같은 손 자리가 다른 목표가 되므로).
- **화면**: 로봇 페이지 "로봇" 제목 옆 [자세 조종] — 자세 소스가 있을 때만 뜬다. 팔로워마다 줄 하나(소스 고르기·시작 /
  상태·기울기 맞춤·정합·해제·정지), 아래에 필터 슬라이더. 닫거나 떠나면 이 패널의 세션은 끝난다.
- **API**: `/api/leader/pose/{sources,status,start,engage,disengage,stop,params}`.

### 5-실측 (2026-11-25, 장비 없이 — 가짜 Sense → `sim_follower1`)

컨테이너 안 가짜 발행기(`/pika_pose` 200Hz·`/gripper/data`·`/pika_localization_status`)와 진짜 `/teleop_trigger` 서비스로
33초 시나리오. 위치 추적 노드는 그동안 내렸다(장비가 없어 `false` 를 내므로).

| 장면 | 결과 |
|---|---|
| 두 번 쥐기 → 정합 | 점프 0, 확장의 PIPER 상태 "정합" |
| 손잡이 앞 6cm | 끝단 앞 축으로 59.9mm |
| 원 세 바퀴(y·z, 반지름 4cm, 0.5Hz) | 옆 ±38mm · 위 79mm(지름 80) |
| 가림 2초(그동안 손 12cm 이동) | 그 자리에 붙잡음, 진동 1 |
| 딴 자리에서 다시 잡힘 | "119mm 튀어 해제" |
| 두 번 쥐기 → 재정합 → 12cm 되돌림 | 점프 0, 끝단 y −119.7mm |
| 못 가는 곳까지 밀기 | "도달 불가" 붙잡음, 진동 2초마다(2→4), 돌아오면 풀림 |
| 두 번 쥐기 → 해제 → 정지 | 세션·가상 리더 세그먼트 사라짐 |

**함께 잡은 1단계 버그**: 위치 추적 노드는 `/pika_localization_status` 를 **`std_msgs/Bool`** 로 낸다(`ros2 topic info -v`).
브리지와 업스트림 시리얼 노드는 `data_msgs/LocalizationStatus` 로 구독해 한 번도 못 받았다 — `tracking_ok` 가 늘 참이었고
손잡이 빨간 불도 안 켜졌다. 브리지가 Bool 로 받아 `/piper/localization_status`(LocalizationStatus)로 다시 내고, 런치가 시리얼
노드를 거기 잇는다(pika-docker). 한 노드가 두 타입으로 같은 토픽을 구독하면 DDS 가 거절해 브리지가 죽는다 — 하나만.

## 6. C — 외부 그리퍼: robotd 가 잇는다

녹화·추론·릴레이·조그는 전부 팔 7f 의 그리퍼 자리 하나만 본다. 그 자리를 채우는 쪽이 하나여야 소비자가
전원 무수정이다 — 그래서 robotd 다.

- 로봇 설정: `end_effector: {kind: external, grip: pika_gripper1, tcp_mm: [...]}`.
- **지령**: 팔 action 의 그리퍼 값 → robotd 가 CAN 대신 `piper.grip.<id>.action` 에 mm 로 쓴다(행정 눈금은 robotd 가
  이미 안다 — `gripper_store`). **상태**: `piper.grip.<id>.state` → 팔 state 의 그리퍼 자리.
- **안전**: action 에 robotd 의 데드맨을 같이 싣는다 — 경과하면 확장은 지금 벌림을 유지한다. E-stop 은 robotd 의
  estop 경로가 grip 에도 estop 을 쓴다. grip 상태가 낡으면 팔 state 에 플래그 → 녹화가 경고한다.
- **TCP**: 말단 오프셋(Pika Gripper 집게 중심)을 FK·IK 가 쓴다 — PoseLeader 와 §7 의 추론 복원이 같은 TCP 를 본다.
  PikaAnyArm 은 `piper_rand_params.yaml` 의 `gripper_xyzrpy`(joint6 기준, m·rad)로 둔다.

## 7. D — 손 수집 → 로봇 추론

로봇 없이 Sense 로 시연하고, Pika Gripper 를 단 PIPER 에서 추론한다. Sense 와 Gripper 가 **같은 카메라를 같은
자리에** 다는 것이 PIKA 의 설계라 관측이 맞는다.

- **수집**: LeRobot 쌍 `pose_shm`(로봇, 구동 없음: state = 벌림 mm + 중력 방향, 카메라는 shm) +
  `pose_shm_leader`(action = 절대 자세 + 벌림). Sense 하나가 리더이자 로봇이다. `vendor/lerobot_robot_pipershm` 옆에 둔다.
- **카메라 이름**: Sense·Gripper 가 같은 키(`wrist_fisheye`·`wrist_depth`)와 같은 [카메라 프로필](camera-profiles.md).
- **action space `ee_delta_v1`** — [ACT-delta](act-delta.md) F3 의 말단판: `ΔT[t] = T[t−1]⁻¹ · T[t]`(몸체 좌표),
  첫 프레임 항등. 벌림은 절대 mm 그대로.
  - **몸체 좌표 차분이라 좌표계 보정이 없다.** 트래커 세계 좌표(기지국 놓기마다 다르다)와 로봇 베이스 좌표를 잇는
    외부 보정이 필요 없다 — 차분은 둘 어디에도 안 묶인다. 상태에 절대 자세를 안 넣는 것도 같은 이유다. 벌림과
    중력 방향은 로봇에서도 같은 값을 계산할 수 있다 — Sense 쪽은 자세에서(트래커 세계 z 가 위다: 가이드 실측
    "위로 들면 Z 쪽"), 로봇 쪽은 FK 에서. Sense 에는 IMU 가 없다.
  - 변환기: `<이름>_eedelta` 사본 + `meta/piper_eedelta.json` — delta 변환기 옆, 같은 꼴.
- **추론**: wrapper 한 자리 — `T_지령 = FK(현재 q)·TCP` 에서 시작해 `T_지령 ← T_지령 · ΔT`, IK(시드 = 현재) →
  관절 → robotd `filter_goal`. 벌림 → 외부 그리퍼(§6). 스텝당 상한(mm·deg)은 delta 와 같은 클램프.
  [action_space](../backend/app/services/action_space.py) 판정 체인에 셋째 값으로 들어간다.
- **TCP 가 같아야 한다**: Sense 자세는 집게 중심(SDK 가 트래커 → 집게 중심으로 옮긴다), 로봇 TCP 도 Pika Gripper
  집게 중심.
- **한계**: 적분 드리프트(매 청크 시각 피드백이 바로잡는다 — ACT-delta 와 같은 한계), 사람 손이 팔 속도 한계를
  넘는 구간(수집 화면이 경고, 학습 전 걸러낸다), 팔 작업 공간 밖 시연(IK 실패 → 막고 말한다).

## 8. 편의 — 터미널 없이

| setup-ui | PIPER STUDIO |
|---|---|
| 설치 (12단계) | [확장 추가] 한 번. sudo 없음 — 02~11단계가 이미지 안에 있다 |
| 바인딩 (11단계 — sudo, udev 규칙 4개, 태그를 케이블로) | 확장 화면: 포트 경로로 Sense/Gripper 를 고른다(둘 안의 시리얼·어안은 고유 번호가 없어 포트로만 구분된다 — 가이드). 트래커는 locator 가 내는 `LHR-…` 이름으로, 둘이면 "왼쪽 Sense 를 흔드세요" 로 가린다. 볼륨에 저장하고 **udev 를 쓰지 않는다** — root 가 필요 없고 camerad 의 카메라 식별과 안 부딪힌다 |
| 장비 검수 | 확장 화면: 태그·동글·기지국·캘리브레이션, 자세 Hz·정지 흔들림 mm, 벌림 Hz, Gripper 전압·enable, USB 3.0 인가. 어안·D405 는 장치 화면(camerad·rsd) |
| 캘리브레이션 (9단계) | 확장 화면 버튼 하나. 가이드는 "실패해도 에러가 안 나오고 멈춰 있기만 한다" 며 사람이 출력을 읽게 한다 — **확장이 대신 읽는다**: 기지국 둘(`for 0`·`for 1`) 다 `acc err < 0.005` 면 통과, 시간 안에 안 나오면 실패 + 원인 목록(태그 배터리·각도·햇빛). 도는 locator 는 멈췄다 다시 띄운다(동글을 같이 쓴다) |
| 명령어 (터미널 A/B) | 확장 화면 버튼: 캘리브레이션 초기화(`config.json` 삭제)·진동 시험. 정합은 텔레옵 패널 |
| "태그를 껐다 켜면 다시 실행" (문제 해결) | 확장이 자세 끊김을 보고 locator 를 스스로 다시 띄운다 |
| 3D 보기 (RViz) | 텔레옵 패널의 자세·궤적 |
| 데이터 수집 (HDF5) | 녹화 화면 — LeRobot 데이터셋 (§7) |

## 9. 확장성

1. **장치 확장**: ROS 든 아니든 "이미지 + 매니페스트 + 계약" 이면 웹에서 붙는다. 다음 장치(다른 그리퍼, UR·두산
   ROS 2 드라이버 — setup-ui 가 이미 가리킨 방향)는 이 저장소를 안 고친다.
2. **자세 소스 `piper.pose.*`**: Vive 트래커 단독·VR 컨트롤러·스페이스마우스도 PoseLeader 하나로, 팔로워는
   `ArmModel` 레지스트리(PIPER·SO-101·시뮬) 무엇이든.
3. **`ee_delta_v1`**: 사람 손 데이터 → IK 가 있는 어느 팔이든.
4. ROS 이미지 틀(베이스·브리지 골격)은 **두 번째 ROS 장치가 올 때** pika-docker 에서 뽑는다 — 지금 일반화하지 않는다.

## 10. 단계

| 단계 | pika-docker | 이 저장소 | 끝나는 기준 |
|---|---|---|---|
| 0 | ☑ 사내 문서 대조(토픽·주기·흔들림·제스처·축·IMU 없음), ☑ PR #18 `one_euro.py` 원본, ☑ 이미지 빌드·장비 없는 기동(2026-11-24, pika-docker `0b7a781`): 런타임 의존 `ldd` 통과·`serial` 은 죽은 의존·`network none` 에서 ROS 2 통신·`/pika_localization_status` 는 locator 가 낸다·두 단계 이미지 1.21GB(받는 양 280MB). 남은 것: 컨테이너 안 hidraw·동글 핫플러그, Sense 둘, `/pika_pose` 기준점(장비), **install.zip 재배포 조건** | — | 이 문서의 "0단계" 가 전부 사실로 바뀐다 |
| 1 | ☑ 브리지(자세·벌림·추적·두 번 쥐기 → shm, heartbeat·RPC `scan`·`info`·`estop`·`restart`), ☑ 확장 화면(자세·궤적·값·장치·시리얼 배정), ☑ 시리얼은 고를 수 있을 때만(하나뿐·배정) + 꽂으면 다시 뜸. ☐ 캘리브레이션 화면·locator 재기동(장비) | ☑ `piper_shm.pose`, ☑ unitd 확장 추가·갱신·제거(허용 레지스트리·로컬 이미지·라벨 검사·고정 템플릿·이미지 ID), ☑ 게이트웨이 API·`/ext/<name>/ui` 프록시, ☑ 서비스 화면(추가·화면·제거·로그인 꺼짐 경고) | ☑(가짜 자세) 웹 API 로 `pika-docker:dev` 추가 → 화면 프록시로 자세 200Hz, 재시작 1.7초 만에 다시 붙음, 로그 화면에 저널. ☐ 실기 Sense |
| 2 | ☑ `feedback`(조명·진동), ☑ 추적 상태 Bool 수신(1단계 버그) | ☑ PoseLeader(정합·축 정렬·따라잡기·IK·OneEuro·두 번 쥐기) + 자세 조종 패널 | ☑ `sim_follower1` 단팔(가짜 Sense, §5-실측). ☐ 양팔(소스 둘 — 다중 Sense 런치), ☐ 실팔, ☐ OneEuro A/B/C 비교(실기) |
| 3 | — | (텔레옵 녹화는 코드 0줄 예상) | A 로 모은 데이터로 학습·추론 한 바퀴 |
| 4 | `piper.grip` · 모터 | 외부 그리퍼(robotd)·TCP | 녹화·추론·E-stop 이 Pika 그리퍼를 쥐고 논다 |
| 5 | — | `pose_shm` 로봇·`ee_delta_v1` 변환기·wrapper 복원 | Sense 손 데이터로 학습한 정책이 Gripper 단 PIPER 에서 돈다 |

## 11. 하지 말 것

- **이 저장소에 PIKA 코드를 두지 않는다** — submodule·vendor 복사 모두. 범용 기능만 여기에.
- 컨테이너가 카메라를 열지 않는다. 같은 호스트에서 setup-ui 의 센서 명령을 같이 돌리지 않는다 — camerad 와
  장치를 다툰다.
- 매니페스트가 ExecStart·마운트·권한을 정하게 두지 않는다. DDS 를 네트워크에 내지 않는다.
- **Mac 호스트는 안 된다** — Docker Desktop 은 USB 를 컨테이너에 못 넘긴다(Mac 은 `mac-port` 브랜치에서 따로).
- `install.zip`(업스트림이 미리 빌드한 바이너리)의 재배포 조건을 확인하기 전에는 이미지를 사내 밖 레지스트리에
  올리지 않는다.

## 12. 열린 질문

1. ☑ 저장소: [WeGo-Robotics/pika-docker](https://github.com/WeGo-Robotics/pika-docker) (비공개, 2026-11-24).
2. ☑ 두 번 쥐기 — **도는 활동이 정한다**(2026-11-25): 텔레옵 중이면 정합 토글(구현), 손 수집 중이면 에피소드 시작/끝(5단계).
   해제하려고 두 번 쥐는 동안 팔로워 그리퍼가 여닫히는 것은 **v1 은 그대로 둔다** — 잡아 두려면 제스처 판정 시간만큼
   그리퍼 지령을 늦춰야 해서 평소 반응이 느려진다. 실기에서 문제가 되면 다시 본다.
3. ☑ 확장 추가를 로그인이 꺼진 상태에서도 허용할지 — **허용 + 경고**(2026-11-24). 서비스 화면이 "이 네트워크의 누구나
   확장을 더할 수 있고 확장은 USB·시리얼 장치 권한을 갖는다" 고 말한다. 플러그인 토큰은 어느 쪽이든 못 한다.
