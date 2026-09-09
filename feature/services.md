# 서비스 켜기/끄기 · 부팅 시 시작

> 요청(2026-09-09): "서비스 on/off, 시작 시 같이 실행을 선택할 수 있게 하고 싶다.
> 그럼 simd·so101d 도 다 포함시켜서 배포하면 되겠지."

## 0. 요지

선택 데몬(simd·so101d·ollama)은 **모든 기계에 깔되 처음엔 꺼져 있고**, 사람이
웹 [설정 → 서비스] 에서 켜고 "부팅 시 시작"을 고른다. 켜고 끄는 손은 호스트의
작은 데몬 `piper-unitd` 다 — 컨테이너 게이트웨이는 `systemctl` 이 없다.

## 1. 왜 unitd 인가 — 통로가 하나여야 한다

| 길 | 판정 |
|---|---|
| 데몬에게 `restart` RPC (지금까지) | ❌ "끄기"가 안 된다 — 스스로 죽어도 `Restart=always` 가 되살린다. enable/disable 은 길이 없다 |
| 컨테이너에 호스트 systemd 소켓 마운트 | ❌ compose 가 특권·`/dev` 를 **일부러** 뺐다. uid·배포판마다 소켓 경로가 다르다 |
| **호스트 데몬 `piper-unitd`** (버스 RPC `list`/`control`) | ✅ 소스로 도는 기계도 같은 길. 허용 목록은 계약의 `UNIT_CATALOG` 뿐 |

unitd 가 없는 기계(설치 전)에서만 게이트웨이가 로컬 `systemctl` 로 폴백한다 —
그것도 없으면 버튼이 잠기고 화면이 이유를 말한다.

## 2. 무엇을 만지고 무엇을 안 만지나

- **카탈로그는 한 곳** — `piper_bus.contract.UNIT_CATALOG` (이름·설명·core/optional).
  게이트웨이(목록·게이트)와 unitd(허용 목록)가 같은 표를 읽는다. 유닛 파일을 새로
  만들면 여기에도 넣어야 한다 — 테스트가 `deploy/systemd/*.service` 와 대조한다.
- **estopd 는 읽기 전용.** 안전장치에 원격 종료 경로를 다는 것은 별개의 결정이다
  (`UNIT_READONLY`). 화면은 상태만 보인다.
- **unitd 는 자기 자신을 못 끈다** — 끄면 켜기/끄기가 같이 죽는다.
- **끄기·재시작은 활동 중에 막는다** (게이트웨이 `require_idle`). robotd 를 끄면
  팔 발행이 멈추고 rsd 를 끄면 카메라가 끊겨 에피소드가 깨진다. 켜기와 "부팅 시
  시작"은 지금 도는 것에 영향이 없어 막지 않는다.
- 핵심 데몬을 끌 때는 한 번 묻는다 (논블로킹 모달 — `window.confirm` 금지).
- 게이트웨이·프론트 유닛은 이 화면의 켜기/끄기 대상이 아니다 — 그건 화면 자체다.

## 3. 설치 — "깔되 켜지 않는다"

`deploy/install-daemons.sh estopd robotd camerad rsd unitd --optional simd so101d`

- `--optional` 뒤는 유닛 파일을 깔고 `daemon-reload` 만 한다. **처음이면 꺼진 채.**
- 재배포 때 **사용자의 선택을 지킨다**: 이미 `enabled` 면 재시작만, 아니면 그대로 둔다.
  기본 목록에 넣어 무조건 켜면 시뮬 데몬이 실기 로봇 호스트에서도 돈다.
- unitd 는 기본 목록이다 — 모든 기계.
- 소스 설치(`install.sh`)와 배포(`apply.sh`) 둘 다 이 형태로 부른다.

## 4. 배포에 실리는 것

| 층 | 전 | 후 |
|---|---|---|
| 데몬 wheel (`stage-hostside.sh`) | bus shm robot cam rs | + **so101 sim** (순수 파이썬, 200KB 안쪽) |
| 릴리스 변경 판정 (`release.sh`) | — | `so101/*`·`sim/*` → wheel |
| 호스트 venv 바깥 의존 (`apply.sh`) | redis·pyrealsense2 (venv 생성 때만) | + `mujoco`·`feetech-servo-sdk` **매 적용**, 실패해도 진행(선택) |
| 시스템 전제 | — | `libEGL` 경고만 (시뮬 카메라 렌더) |
| 유닛 | estopd robotd camerad rsd | + unitd, `--optional simd so101d` |

mujoco 는 플랫폼 wheel(~20MB)이라 번들에 못 싣는다 — apply.sh 가 PyPI 에서 깐다.
PyPI 가 안 닿는 호스트는 경고를 남기고 핵심 데몬 설치는 그대로 끝난다.

## 5. 검증

- unitd 로 simd 를 stop → `/api/system/services` 가 `active: false`, start → 되살아남,
  disable/enable → `enabled` 가 따라온다. estopd 에 stop 은 400.
- 활동 중(조그·추론) stop/restart 는 409.
- `install-daemons.sh --optional` 을 두 번 돌려도 켜 둔 선택이 안 바뀐다.

## 열린 질문

- ollama 유닛은 `Condition` 으로 스스로 건너뛴다 — 카탈로그에는 있으나 unitd 가
  깔지는 않는다(설치 스크립트가 따로). "설치 안 됨"으로 보이는 게 맞다.
- estopd 재시작만은 허용할지 — 지금은 읽기 전용. 코드가 낡았을 때 재시작할 길이
  SSH 뿐이다.
