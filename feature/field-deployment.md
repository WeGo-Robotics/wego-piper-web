# 현장 배포·운영 검토 — 대회와 일반 사용자

> 요청(2026-09-23): 아래 일곱 항목을 검토하고 기획·조사 자료와 의견을 달아 문서로.
>
> - Vast.ai 한 계정에 복수 인스턴스를 빌릴 때 각각 구분법 — 대회에서 주최측이 한 계정에
>   크레딧을 충전하고 API 키를 뿌려 모든 참가자가 그 계정에서 빌려 쓰게 하는 법
> - 다양한 환경 지원 여부 — 윈도우에서 되나? 맥에서 되나?
> - 부팅 시 문제가 생기면 알기 어렵다 — 브라우저에서 아예 접속이 안 된다. 원인을 안내하는
>   페이지는 떠 줘야 한다
> - 로컬 서버에서 받아서 설치 — 대회 장소의 외부 대역폭 한계. USB 나 로컬 서버로 받아 설치
> - 바탕화면 단축아이콘 — 일반인이 쓰기 쉽게
> - Colab 연동 검토 — vast.ai 처럼 API 로 되나?
> - 가상 환경에서 설치 테스트 후 릴리스 — CI/CD 로 새 우분투 가상환경에 설치·동작 확인 뒤 릴리스

**이 문서가 가정한 것.** 대회 = 일반인 참가자, 로봇 호스트는 우리가 준비한 Ubuntu 기계(NUC 급,
GPU 는 없을 수 있음), 참가자 노트북은 Windows·Mac, 현장 외부망은 느리거나 없다. 전제가 다르면
§9 에서 다시 정한다. "지금" 이라고 적은 것은 전부 2026-09-23 의 코드·문서에서 읽은 것이고,
"조사" 는 같은 날 외부 문서에서 확인한 것이다(출처 §10).

## 0. 요지

| # | 항목 | 지금 | 판정 | 권고 | 규모 |
|---|---|---|---|---|---|
| 1 | Vast 한 계정·여러 참가자 | 구분 단서는 라벨 `piper-<job>` 뿐. 같은 계정을 쓰는 게이트웨이끼리 서로의 기계가 보이고, 서로를 고아로 칠하고, [파기] 도 열려 있다 | 키 하나 복사는 **비권장** | 참가자별 계정 + 주최측 **크레딧 이체**(`vastai transfer credit`, 스크립트로). 우리 쪽은 **소유자 라벨** 한 층 | 소 (1~2일) |
| 2 | Windows · Mac | 조작하는 PC 는 브라우저라 **지금도 된다.** 로봇 호스트는 Linux 전용(CAN·V4L2·systemd·`/dev/shm`) | 호스트 이식은 **안 한다** | README 에 "조작 PC 는 브라우저면 된다" 한 줄 + qna 항목 | 극소 |
| 3 | 부팅 실패 안내 | 게이트웨이가 죽어도 화면 껍데기는 뜬다(정적) — 페이지마다 제각각 오류. 프론트 컨테이너가 안 뜨면 연결 거부 | 필요 | 앱 안의 **게이트웨이 관문 + 상태 화면**, unitd 가 쓰는 `boot.json`, 바탕화면 [진단] 도구 | 중 (2일) |
| 4 | 로컬 서버·USB 설치 | `release.sh --offline` tar 와 LAN 레지스트리가 **이미 있다.** 그러나 venv 의존(PyPI)·apt·ollama 는 망이 필요 — 망이 없으면 **팔·카메라가 안 뜬다** | 필요, 대회 전제 | **현장 키트**(이미지+wheel+pip 캐시+deb) + 손잡이 `PIPER_MIRROR` 하나 + 서빙 스크립트 | 중 (3일) |
| 5 | 바탕화면 아이콘 | 없음. 탭 제목이 `frontend` | ☑ 09-23 구현(§5-4) | `.desktop` 둘([Piper Studio]·[진단])을 `apply.sh` 가 만든다 | 극소 (반나절) |
| 6 | Colab 연동 | — | **API 가 없다** · ☑ README 안내(09-23) | Colab 은 노트북 템플릿 문서로 끝. 다음 프로바이더는 **HF Jobs**(LeRobot 공식 `--job.target`, 조직 과금 — 대회에 맞는다) | 문서 반나절 / HF Jobs 러너 2~3일 |
| 7 | CI 설치 검증 후 릴리스 | CI 없음. 릴리스는 `release.sh` + 사람 확인 + .120 실기 | 필요 | GitHub Actions — 정적 검사 + **새 우분투 런너에 `piper-install.sh` 그대로** 스모크, `release.sh` 가 초록을 확인한 뒤 push | 중 (3일) |

우선순위(대회 기준)는 §8, 먼저 정할 것은 §9.

---

## 1. Vast.ai — 한 계정에 여러 인스턴스, 대회용 공유 계정

### 1-1. 지금 코드가 하는 일

| 사실 | 자리 |
|---|---|
| API 키는 **게이트웨이당 하나** — `config_dir/cloud/vast_api_key`(0600) 또는 `.env` 의 `VAST_API_KEY` | [apikey.py](../backend/app/services/cloud/apikey.py) |
| 인스턴스 구분 단서는 **라벨뿐** — 한 묶음은 `piper-<job_id>`, 사람이 빌려 둔 기계는 `piper-box-<n>` | [procure.py](../backend/app/services/cloud/procure.py) · [cloud.py](../backend/app/routers/cloud.py) |
| 인스턴스 탭은 계정의 인스턴스를 **전부** 보여 주고 [파기] 도 전부에 붙는다 — 소유 검사가 없다 | `GET`/`DELETE /api/cloud/instances` |
| 고아 = `piper-` 접두인데 **이 게이트웨이의** 레지스트리가 모르는 것 → 빨간 배너. 자동 파기는 안 한다 — "다른 기계의 게이트웨이가 돌리는 학습일 수 있다" 가 설계 결정 | [lifecycle.py `is_orphan`](../backend/app/services/cloud/lifecycle.py) · [vast-training.md §10 결정 5](vast-training.md) |
| 예산 가드(시간·달러 상한)는 **자기 job** 에만 건다 | `Budget` |

즉 같은 계정을 게이트웨이 둘이 써도 돌긴 한다. 다만 ① 서로의 기계가 다 보이고 ② 서로를
**고아로 칠하며**(10분마다 빨간 배너) ③ 남의 기계에도 [파기] 가 열려 있고 ④ 비용이 계정
하나로 뭉쳐 누가 얼마 썼는지 모른다. "복수 인스턴스 구분" 의 답은 **라벨에 소유자를 넣는
것**(§1-4)이고, "한 계정을 뿌리는 것" 의 답은 **뿌리지 않는 것**(§1-3)이다.

### 1-2. Vast 쪽에 있는 수단 (조사)

| 수단 | 내용 | 대회에 쓸 때 |
|---|---|---|
| **크레딧 이체** | `vastai transfer credit <이메일 또는 id> <금액>` = `PUT /api/v0/commands/transfer_credit`. 최소 0.01, 속도 제한 있음, API 키로 호출된다 | 주최측 계정 → 참가자 계정으로 정해진 액수를 보낸다. **참가자의 상한이 곧 이체액**이다 |
| **Teams** | 팀 잔액·결제가 개인과 분리. 역할 Owner / Manager / Member(인스턴스 보기·만들기·조작, 결제·팀 관리 불가) + 커스텀 역할(permission group 조합). 초대는 이메일, 받는 쪽도 Vast 계정이 있어야 한다 | 잔액 하나를 여럿이 쓴다. **인스턴스는 팀 자원이라 서로 보인다.** 멤버별 지출 상한은 문서에 없다 |
| **제한 API 키** | 키마다 permission JSON. `constraints` 로 파라미터 값을 고정(`eq`·`lte`·`gte`) — 예: 특정 인스턴스 id 에만 로그 조회. CLI `vastai create api-key` | "내가 만든 인스턴스만" 은 표현 못 한다(값 고정뿐). 파기를 통째로 막는 키는 만들 수 있지만 그러면 자기 것도 못 끈다 |
| **라벨** | 자유 문자열, `show instances` 에 실려 온다. 서버 필터는 없고 클라이언트가 거른다 | 소유자 표시에 쓴다(§1-4) |

⚠ 이체받은 크레딧만으로 **결제수단 등록 없이** 빌릴 수 있는지는 문서에 없다 — 대회 전에
계정 하나로 실측한다. 역방향 이체(남은 크레딧 회수)도 같은 명령이 되는지 함께 본다.

### 1-3. 대회 방식 셋 — 비교

| | A. 참가자별 계정 + 크레딧 이체 | B. Team 하나, 멤버 초대 | C. 계정 하나·키 하나를 복사 (요청된 방식) |
|---|---|---|---|
| 격리 | **완전** — 남의 기계가 안 보인다 | 없음 — 팀 인스턴스 전부 보임 | 없음 |
| 참가자 상한 | **이체액**이 상한 | 팀 잔액 전체(멤버별 상한 없음) | 계정 잔액 전체 |
| 사고 범위 | 자기 계정 안 | 남의 학습을 끌 수 있다 | 남의 학습을 끌 수 있고, **키 하나 유출 = 크레딧 전부** |
| 비용 귀속 | 계정별로 자동 | 팀 청구서 하나 | 없음 — 라벨로 추정뿐 |
| 준비 손 | 참가자가 가입 → 이메일 목록 → **스크립트가 이체** | 팀 생성 → 초대 N번 → 각자 키 발급 | 키 복사 |
| 우리 코드 | 없어도 된다(§1-4 는 덤) | §1-4 **필수** | §1-4 필수 + 경고 문구 |

**권고: A.** 이체가 API 라 "참가자 30명에게 $15 씩" 은 스크립트 한 줄이고(반복 작업은
스크립트가 한다 — [체크리스트 R9](../deploy/RELEASE-CHECKLIST.md)), 회수도 같은 명령이다.
B 는 A 가 막힐 때(참가자가 계정을 못 만드는 사정). C 는 하지 않기를 권한다 — 굳이 하려면
§1-4 를 먼저 깔고, 참가자 안내에 "이 키로는 남의 기계도 끌 수 있다" 를 명시한다.

### 1-4. 우리 쪽 할 일 — 어느 방식이든 값어치가 있다

1. **소유자 라벨.** 게이트웨이가 처음 뜰 때 8자 id 를 만들어 `config_dir` 에 둔다(재설치를
   넘어 남는 자리 — SSH 키·API 키와 같은 곳). 라벨을 `piper-<owner>-<job_id>` ·
   `piper-box-<owner>-<n>` 으로. `is_orphan` 은 "내 owner 인데 레지스트리가 모름" 만 고아로
   부르고, 남의 owner 는 **"다른 게이트웨이의 기계"** 로 회색 표시. 옛 라벨(`piper-<job>`,
   owner 없음)은 지금과 같은 취급 — 배포된 호스트가 이미 만든 기계를 못 알아보면 안 된다.
2. **파기 2단.** 남의 owner 는 [파기] 를 한 번 더 묻는다(id 타이핑). 지금 창은 체크포인트
   개수까지 물어보는 좋은 창이라 그 위에 한 줄 얹으면 된다.
3. **인스턴스 탭 필터** "이 기계 것만" 을 기본으로 켠다 — 제목 옆 토글.
4. **운영 스크립트** `tools/vast-credits.sh emails.txt 15` — 이메일마다 `transfer credit`,
   끝에 잔액을 표로. 회수 방향도 같은 스크립트.
5. 설정 → 클라우드 도우미([vast-training.md §9](vast-training.md)) 에 "대회 참가자는
   주최측이 안내한 계정으로 로그인해 키를 만든다" 분기.

규모: 1~3 이 하루, 4 가 반나절. 테스트: 라벨 파서(옛/새), 남의 owner 가 고아로 안 잡히는 것, 필터.

---

## 2. Windows · Mac

### 2-1. 무엇이 어디서 도나

| 층 | OS 의존 | Windows | Mac |
|---|---|---|---|
| **브라우저** — 화면·조종 창·E-stop heartbeat·업데이트 버튼 | 없음 | ◎ 지금 된다 | ◎ 지금 된다 |
| 게이트웨이·프론트 컨테이너 | `ipc: host`(호스트 `/dev/shm` 공유), `/run/redis` 유닉스 소켓 마운트 ([docker-compose.yml](../docker-compose.yml)) | ✗ Docker Desktop 은 VM — 호스트의 shm·소켓을 못 나눈다 | ✗ 같다 |
| 호스트 데몬 estopd·robotd·camerad·rsd·unitd | systemd 사용자 유닛·linger, `/dev/shm`, socketcan(`ip link`, gs_usb), V4L2 `/dev/video*`, pyrealsense2, udev, sudoers | ✗ | ✗ CAN·V4L2 자체가 없다 |
| 로컬 학습 | CUDA 컨테이너 | WSL2 에서만 | ✗ |

**답: 조작하는 PC 는 어느 OS 든 된다 — 지금도.** 로봇을 꽂는 기계는 Ubuntu 다. 대회 그림은
"참가자 노트북(아무 OS) → 브라우저 → 우리가 준 Ubuntu 기계" 이고, 그건 지금 구조 그대로다.

### 2-2. 굳이 호스트를 옮기려면 — 검토한 길

| 길 | 판정 |
|---|---|
| WSL2 | systemd 는 켜진다. USB 는 usbipd 로 넘길 수 있으나 **기본 커널에 gs_usb·UVC 가 없다** — 커널을 직접 빌드해야 하고 RealSense 는 더 어렵다. "지원 대상 아님" 으로 적는다 |
| Mac | CAN·V4L2 가 없다. 시뮬 전용(simd + 컨테이너)이라도 shm 공유·EGL 렌더를 다시 설계해야 한다 |
| "시뮬 전용 데스크톱 앱(Windows/Mac)" | 별개의 제품이다. 대회가 실물 로봇이면 쓸 데가 없다 — **하지 않기를 권한다** |

할 일(극소): README 요구사항 표에 "조작 PC: 최신 Chrome·Edge·Safari, OS 무관" 한 줄,
[qna.md](../docs/qna.md) 에 "윈도우에서 되나?" 항목. ⚠ Safari 는 MJPEG 프리뷰·WebSocket·
조종 창 키 입력을 **한 번 실측**한다 — 지금까지 본 브라우저는 리눅스·윈도우의 Chrome 계열뿐이다.

---

## 3. 부팅 실패를 알 수 있게 — 원인 안내 화면

### 3-1. 지금

- 게이트웨이가 죽어 있거나 뜨는 중이면 nginx 는 정적 SPA 를 **그대로 준다**([nginx.conf](../frontend/nginx.conf) 는 `/api`·`/ws`·`/health`·`/docs` 만 프록시). 그래서 **화면은 뜨는데** 페이지마다
  "응답 없음 (30초)"·"502 Bad Gateway" 를 제각각 띄운다. 어디에도 "게이트웨이가 안 떠 있다,
  이유는 이것" 이 없다. [AuthGate](../frontend/src/components/AuthGate.tsx) 는 상태를 못 물으면
  **일부러** 통과시킨다(로그인 막다른 길을 피하려고) — 옳은 결정이고, 그 뒤를 받는 화면이 없는 것이 문제다.
- `/docs`·`/health` 를 직접 열면 nginx 기본 502 흰 페이지. [`/health`](../backend/app/routers/health.py) 는 `{"status":"ok"}` 뿐이다.
- 프론트 컨테이너·docker 가 못 뜨면(포트 점유·docker 안 켜짐) 브라우저는 "연결 거부" — 화면이 있을 자리가 없다.
- **웹이 뜬 뒤**는 잘 돼 있다: [설정 → 서비스] 패널, 로그 페이지, 버전 카드, `apply.sh --check`.
- 실제로 겪은 부팅 실패([install-troubleshooting.md](../docs/install-troubleshooting.md)): redis 소켓
  없음 → backend 못 붙음 / GPU 없는 기계에 nvidia 예약 → backend 안 뜸 / linger 꺼짐 → 데몬
  전멸(웹은 뜬다) / 80 포트 점유 → frontend 못 뜸 / 업데이트 적용 중(정상인데 몇 분 끊긴다).

### 3-2. 실패는 세 층이고, 말할 수 있는 자리가 다르다

| 층 | 증상 | 누가 말할 수 있나 |
|---|---|---|
| ① 게이트웨이 죽음·기동 중·업데이트 중 (**가장 흔함**) | 껍데기만 뜨고 전부 오류 | 프론트는 살아 있다 → **앱 안의 게이트웨이 관문** (§3-3) |
| ② 프론트 컨테이너·docker 자체 | 연결 거부 | 브라우저로는 불가 → **바탕화면 [진단]** (§5-3) |
| ③ 데몬 죽음 | 웹은 뜨는데 팔·카메라 없음 | 이미 서비스 패널·장치 경보가 말한다. 첫 화면 배너 한 줄이면 된다 |

### 3-3. 설계 — 게이트웨이 관문 (①)

```
브라우저 ── GET / ──▶ nginx ──▶ 정적 SPA (백엔드와 무관하게 뜬다)
   SPA 의 GatewayGate: /health 를 5초마다 → 연속 실패면 전체 화면 "게이트웨이 없음"
                       └ 그 화면이 /status/boot.json 을 읽는다 ◀── ${PIPER_DATA_ROOT}/status/ (nginx 정적, ro)
                                                                        ▲ unitd 가 10초마다 쓴다
```

- 프론트에 `GatewayGate`(AuthGate 옆, 라우터 **밖**): `/health` 가 3회 연속 실패하면 상태
  화면을 **오버레이**로 띄우고, 다시 답하면 걷는다 — 페이지 상태를 잃지 않게. 화면은
  `/status/boot.json` 을 읽어 표를 그린다.
- `boot.json` 은 **unitd** 가 쓴다 — 호스트에서 이미 `systemctl` 을 쥔 유일한 데몬
  ([services.md](services.md)). 내용: 유닛별 active/failed, `docker compose ps` 요약, redis 소켓
  존재, `piper-update` 일시 유닛이 도는지(→ "업데이트 적용 중, 곧 돌아옵니다"), 마지막
  `apply.sh --check` 의 ✗ 줄, `written_at`. 프론트 compose 에
  `${PIPER_DATA_ROOT}/status:/usr/share/nginx/html/status:ro` 한 줄.
- 처방 문구는 **새로 짓지 않는다.** `apply.sh --check` 가 이미 찍는 문장(`redis 소켓 없음 — …`)을
  그대로 실어 나른다. 트러블슈팅 표와 같은 문장이어야 하고 `test_install_docs.py` 가 잠근다.
- ⚠ unitd 까지 죽었으면 파일이 낡는다 → `written_at` 이 60초 넘게 옛것이면 "호스트 데몬(unitd)도
  응답이 없습니다 — 바탕화면 [Piper Studio 진단] 을 여세요" 를 띄운다. 낡은 표를 새것처럼 보이지 않는다.
- ⚠ 인증([gateway-auth.md](gateway-auth.md))을 켠 게이트웨이에서도 이 파일은 인증 밖이다.
  그래서 담는 것은 유닛 이름·상태·처방 문장뿐 — 저널 본문·경로·비밀은 넣지 않는다.
- ⚠ 업데이트 중에는 VersionCard 가 이미 "돌아오면 새로고침" 을 한다. 관문이 다른 말을 하면 안
  되므로 "업데이트 적용 중" 판정은 `boot.json` 한 곳에서 오고 문장도 거기서 만든다.
- E-stop 과는 충돌이 없다 — heartbeat 는 게이트웨이가 없으면 어차피 못 가고, 추론 중 게이트웨이가
  죽으면 estopd 가 이미 처리한다.

### 3-4. ②·③

- ② 는 브라우저 밖의 일이다. §5-3 의 [진단] 아이콘이 `apply.sh --check` + `docker compose ps`
  + `systemctl --user list-units 'piper-*'` + 유닛별 저널 끝 20줄을 HTML 하나로 만들어 브라우저에
  `file://` 로 연다. 원인이 "docker 가 안 켜짐" 이면 이게 유일하게 말해 줄 수 있는 자리다.
- ③ 은 대시보드 첫 화면에 "핵심 데몬 n개가 죽어 있습니다 → [서비스]" 배너 한 줄(있는 판정을
  재사용하고 문구도 백엔드가 만든다 — `DeviceAlerts` 와 같은 규칙).

규모: 관문 + 상태 화면 하루, unitd 의 `boot.json` 반나절, 진단 도구 반나절(§5 와 공유). 검증은
재현이 쉽다 — `docker compose stop backend` 뒤 `/` 가 원인을 말하나. §7 의 CI 에 잡 하나로 넣는다.

---

## 4. 로컬 서버·USB 설치 — 현장 대역폭

### 4-1. 이미 있는 것

| 길 | 명령 | 상태 |
|---|---|---|
| USB tar | 빌드 머신 `./deploy/release.sh vX --offline` → `dist/piper-web-vX.tar.gz`(gzip 3.46GB: 이미지 둘 + wheel + 데몬 소스 + `apply.sh`) → 호스트 `tar xzf … && ./vX/apply.sh` | 문서화됨([트러블슈팅 §2](../docs/install-troubleshooting.md)) |
| LAN 레지스트리 | 빌드 머신 `PIPER_REGISTRY_BIND=0.0.0.0 ./deploy/registry.sh` → 호스트 `PIPER_IMAGE=piper-build:5000/piper-web-backend ./piper-install.sh vX` (+ `insecure-registries`) | **현장망 배포용으로 쓰고 있다.** 둘째 기계부터는 우리 코드 390MB 만 받는다 ([registry.sh](../deploy/registry.sh)) |

### 4-2. 그런데 "망 없이" 는 아직 완결이 아니다 — `apply.sh` 가 망을 찾는 자리

| 단계 | 어디서 받나 | 없으면 |
|---|---|---|
| 0절 apt: `docker.io` `docker-compose-v2` `python3-venv` `redis-server` `libegl1` (`nvidia-driver-580`·`nvidia-container-toolkit` 은 NVIDIA 저장소) | Ubuntu 아카이브 | 멈추고 명령을 찍는다 — 망이 없으면 그 명령이 안 된다 |
| 1절 이미지 | 레지스트리 또는 `images.tar.gz` | OK — 두 길 다 있다 |
| 2절 데몬 wheel 7개 | 번들 | OK |
| 2절 venv 바깥 의존, **필수**: `redis` `pyrealsense2` `numpy` `opencv-python-headless` `piper_sdk==0.6.1` `python-can==4.6.1` | **PyPI** | **경고만 하고 넘어간다** → 카메라·팔이 통째로 안 된다. 증상은 나중에 "camerad 가 안 뜬다"·"piper_sdk not installed" 로 |
| 2절 선택: `mujoco` `feetech-servo-sdk` | PyPI | simd·so101d 를 못 켠다 |
| ollama 바이너리 + `qwen2.5:7b`(≈4.7GB) | ollama.com — 지금도 손 설치([체크리스트 5절](../deploy/RELEASE-CHECKLIST.md)) | 판단 LLM 없음(선택) |
| HF 데이터셋·모델, YOLO 가중치 | 필요할 때 내려받음 | 해당 기능만 |

덤으로 걸리는 것 둘. ⚠ LAN 레지스트리로 받아도 매니페스트의 `registry=` 는 **주 레지스트리
(GHCR)** 라 `apply.sh` 가 frontend 를 GHCR 에서 받으려 한다 — 지금은 `PIPER_REGISTRY=piper-build:5000`
을 **같이** 줘야 한다(환경변수 둘). ⚠ 오프라인 tar 의 wheel 은 버전 도장이 안 찍혀(체크리스트
v0.5.5 메모, `release.sh` 가 `stage-hostside.sh` 와 달리 도장을 안 찍는다) 버전 카드가 "데몬
wheel 이 다르다" 고 말한다.

### 4-3. 설계 — 현장 키트 하나, 손잡이 하나

- **키트** = `release.sh vX --kit` 의 산출물 `dist/piper-web-vX-kit/`:
  - `images.tar.gz`(있는 그대로) · `wheels/`(**도장 찍힌** 것 — `stage-hostside.sh` 와 같은 절차) ·
    `daemons.tar.gz` · `apply.sh` · `piper-install.sh`
  - `pip/` — 위 표의 venv 바깥 의존을 `pip download`(manylinux x86_64, `--only-binary`)로.
    호스트 파이썬은 22.04=3.10, 24.04=3.12 — **둘 다** 싣는다(수백 MB, opencv·mujoco 가 큰 쪽)
  - `apt/22.04/`·`apt/24.04/` — `apt-get download` + 의존 → `dpkg-scanpackages` 로 로컬 저장소.
    NVIDIA 드라이버·컨테이너 툴킷은 **넣지 않는다**(커널 의존·재부팅) — GPU 기계는 미리 준비된 것을
    전제로 하고 문서에 그렇게 적는다
  - `ollama/`(선택) — 바이너리 + 모델 디렉토리 tar
  - `README-현장.md` 한 장
- **손잡이 하나**: `PIPER_MIRROR=<디렉토리 | http://호스트:포트>`. `piper-install.sh`·`apply.sh` 가
  이것만 보고 ① 이미지 출처(`PIPER_IMAGE` 와 매니페스트 `registry=` 덮어쓰기 — 지금의 환경변수 둘을
  하나로) ② pip 는 `--find-links $MIRROR/pip --no-index` ③ apt 는 `sources.list.d/piper-kit.list`
  를 sudo 명령으로 찍는다. 사람이 셋을 기억하지 않는다 — R9 와 같은 정신.
- **현장 서버** = 노트북 하나: `deploy/kit-serve.sh <kit>` 가 `registry:2`(이미지를 tar 에서 밀어
  넣고) + `python3 -m http.server`(pip·apt·ollama)를 띄운다. Windows·Mac 노트북이면 Docker Desktop
  과 파이썬으로 같은 일이 된다. 참가자 기계는 `PIPER_MIRROR=http://<노트북>:8080 ./piper-install.sh`.
- **USB**: 같은 디렉토리를 복사, `PIPER_MIRROR=/media/usb/piper-web-vX-kit`. 스무 대에 tar 를 꽂는
  것과 LAN 서버 하나 중 현장 사정으로 고른다 — 첫 대는 어느 쪽이든 7GB 급이고, 둘째 대부터는
  레지스트리가 훨씬 가볍다.
- **검증**: §7 의 CI 에 "망을 끊고(`unshare -n` 안에서 미러만 허용) 키트로 설치" 잡. 손으로는 안
  잡힌다 — 개발 머신엔 전부 이미 깔려 있어 PyPI 가 안 닿는 걸 못 느낀다(v0.5.3 의 `piper_sdk` 가 그 사고다).

규모: 키트 빌드 1~2일, 손잡이 1일, 서빙 스크립트 반나절, 문서 반나절. **대회 전제라 우선순위 1.**

---

## 5. 바탕화면 바로가기

### 5-1. 지금

없다. 설치 마지막 줄이 주소를 찍어 줄 뿐이다. 덤으로 [index.html](../frontend/index.html) 의
`<title>` 이 **`frontend`** 라 탭·북마크·바로가기 이름이 전부 "frontend" 로 나온다 — 한 줄 고침("Piper Studio").

### 5-2. 방식

| 방식 | 내용 | 판정 |
|---|---|---|
| **`.desktop` 파일** | `~/Desktop/piper-studio.desktop` + `~/.local/share/applications/`(앱 메뉴·검색). `Exec=` 는 Chrome/Chromium 이 있으면 `--app=http://localhost:<포트>/`(주소창 없는 창), 없으면 `xdg-open`. `Icon=` 은 번들의 png(favicon.svg 에서 256px 하나 굽는다). GNOME(22.04+)은 바탕화면 파일에 "실행 허용" 이 필요 — `chmod +x` + `gio set … metadata::trusted true` 를 **`apply.sh` 가 한다**(sudo 불필요). `Name[ko]=` 로 한글 이름 | **채택.** 설치가 만들고 제거가 지운다. 포트는 `.env` 의 `PIPER_WEB_PORT` 를 그대로 |
| 브라우저 키오스크 | 같은 `.desktop` 의 `--kiosk` 변형 하나 — 대회 부스용 | 덤, 한 줄 |
| PWA "앱으로 설치" | manifest + 아이콘이면 Chrome 이 [설치] 를 준다. ⚠ **HTTP 의 LAN 주소에서는 안 뜬다**(secure context — `localhost` 만 예외). 서비스 워커는 넣지 않는다(캐시가 업데이트를 가린다) | 나중에. 로봇 호스트 화면에서만 의미 있다 |
| 참가자 노트북(Windows/Mac) | 우리 손이 안 닿는다. 첫 화면에 **"이 주소를 바탕화면에 끌어다 놓으세요" 안내 + QR** | 안내 한 줄 |

### 5-3. 둘째 아이콘 — [Piper Studio 진단]

§3 의 ② 층을 이게 맡는다. `deploy/piper-doctor.sh`: `apply.sh --check` + `docker compose ps` +
`systemctl --user list-units 'piper-*'` + 유닛별 저널 끝 20줄 + `hostname -I` →
`~/.cache/piper-web/doctor.html` → 브라우저로. 웹이 살아 있으면 그냥 웹을 연다. 첫 판은
`Terminal=true` 로 텍스트만 띄워도 된다.

규모: `apply.sh` 에 12줄 + png + title + `piper-uninstall.sh` 반영 — 반나절. 실측: GNOME
42(22.04)·46(24.04) 에서 "실행 허용" 이 안 뜨는지.

### 5-4. 구현 (2026-09-23)

| 자리 | 무엇 |
|---|---|
| [install-shortcuts.sh](../deploy/install-shortcuts.sh) | 앱 메뉴 항목 둘 + 바탕화면 사본(디렉토리가 있을 때만) + 아이콘. `--check`·`--remove`. 실행 비트와 `gio` 의 `metadata::trusted` 를 미리 준다 |
| [piper-studio.sh](../deploy/piper-studio.sh) | [Piper Studio]: 포트를 compose → `.env` → 80 순으로 찾고, 프론트가 응답하면 브라우저(Chrome 앱 창 → `xdg-open`), 아니면 진단 보고서 |
| [piper-doctor.sh](../deploy/piper-doctor.sh) | [Piper Studio 진단]: 적용본·웹·컨테이너·유닛·`apply.sh --check` 의 ✗·! 줄·저널 → 텍스트/HTML. §3 의 ② 층 |
| `apply.sh` 5절 · `stage-hostside.sh` · `release.sh` · `piper-uninstall.sh` 4b | 번들에 싣고, `current/` 에 깔고, 제거가 지운다 |
| 아이콘 | png 대신 **프론트의 favicon.svg 그대로** — GNOME 은 절대 경로 svg 를 받는다. 원본이 하나 |
| 테스트 | [test_desktop_shortcut.py](../backend/tests/test_desktop_shortcut.py) — 임시 HOME 에서 만들고·두 번 만들고·지우고, 진단이 아무것도 없는 기계에서도 0 으로 끝나는 것, 아이콘이 웹/보고서 중 맞는 쪽을 여는 것 |

⚠ 실기 확인이 남았다: GNOME 42·46 에서 아이콘이 잠금 없이 뜨는지, `piper-unitd` 가 띄운 업데이트
(세션 버스 유무)에서 `gio set` 이 먹는지.

---

## 6. Google Colab 연동

### 6-1. 조사 결과

| 사실 | 뜻 |
|---|---|
| 소비자 Colab(무료·Pro·Pro+)에는 런타임을 만들거나 노트북을 실행하는 **공개 API 가 없다** — FAQ 어디에도 없고, 있는 것은 Colab Enterprise(GCP·Vertex AI)의 API 뿐 | Vast 에 하는 "빌리기 → SSH → 학습 → 파기" 를 Colab 에는 할 길이 없다 |
| 무료 등급에서 **금지**: SSH 셸·원격 데스크톱, "노트북 UI 를 우회해 웹 UI 로 주로 상호작용", 분산 컴퓨팅 워커. 유료 플랜에서는 풀린다 | "Colab 을 SSH 박스로 만들어 우리 SSH 러너에 붙이기" 는 무료 등급에서 **약관 위반**이다 |
| 세션 최대 12시간(Pro+ 는 24시간 연속), 유휴 종료, GPU 배정 보장 없음 | 몇 시간짜리 학습을 맡기기엔 끊김이 기본값이다 |
| Colab Enterprise: runtime template · notebook execution API, `gcloud colab executions create` | 되긴 하나 GCP 프로젝트·과금이 전제 — "무료 Colab" 을 원한 이유가 사라진다 |
| LeRobot 공식 문서가 Colab 을 안내한다 — ACT 학습 노트북 | 노트북 안에서 `lerobot-train` 이 돈다는 뜻. 우리 HF 업로드·다운로드 경로와 맞는다 |

### 6-2. 할 수 있는 것

| | 방법 | 판정 |
|---|---|---|
| (a) 노트북 템플릿 | 우리 화면의 [HF 업로드](hf-account.md) → Colab 노트북(LeRobot 공식 것에 repo_id 만) → 끝나면 `push_to_hub` → 우리 [Hub 다운로드] 가 `models_dir` 에 안착 | **코드 0, 문서 한 장.** 있는 경로 전부 재사용 |
| (b) 풀 워커 노트북 | 노트북이 HF 레포를 작업 큐로 폴링 → 학습 → push. 게이트웨이는 원격 job 으로 표시 | 무료 등급의 "워커" 금지에 걸리고 세션 끊김에 약하다 — **비권장** |
| (c) Colab Enterprise 프로바이더 | Vast 와 같은 층에 넣을 수 있다 | 대상 사용자(개인·대회)와 안 맞는다 |

### 6-3. 같은 목적이면 더 나은 후보

| 후보 | API | 비용 | 한도 | 우리 코드 |
|---|---|---|---|---|
| **HF Jobs** | LeRobot 이 **공식 지원** — `lerobot-train … --job.target=a10g-small`. 로컬 데이터셋은 비공개 레포로 자동 업로드, 끝나면 모델 push, `--save_checkpoint_to_hub=true` 면 **중간 체크포인트가 Hub 로 스트리밍**(Vast 에서 못 하던 것 — [§12-25](vast-training.md)), `hf jobs logs`/`cancel`, 재개도 같은 명령 | t4-small $0.40/h · a10g-small $1.00/h · a100-large $2.50/h, **분 단위**, 양수 잔액이면 누구나(PRO 불필요). `--namespace <조직>` 으로 **조직 과금** | 기본 timeout 2일 | 러너 하나(`hf jobs` 래핑 — SSH 아님). **중** |
| Kaggle | `kaggle kernels push`(`enable_gpu`) · `status` · `output` | **무료** 30h/주(T4×2 / P100) | 세션 12h, 토요일 UTC 리셋 | 노트북 생성·push·회수 러너. 중 |
| RunPod · Lambda | Vast 와 같은 모양 | 시간당 | — | 프로바이더 한 장. 소~중 |

**HF Jobs 는 §1 의 문제도 푼다.** 주최측이 HF **조직** 하나에 크레딧을 넣고 참가자를 초대하면,
각자 자기 토큰으로 `--namespace 조직` 학습을 돌리고 조직 Jobs 페이지에 **누가 무엇을 돌렸는지**
남는다. 키를 뿌릴 일이 없다(Enterprise 면 그룹별 지출 상한까지).

### 6-4. 권고

Colab 은 (a) 로 끝낸다 — 문서 반나절. 다음 프로바이더는 **HF Jobs**: 학습 페이지가 이미 HF
토큰·네임스페이스·push 권한 검사를 갖고 있어([hf-account.md](hf-account.md)) 러너만 얹으면 된다.
⚠ 먼저 실측할 것 둘 — Jobs 의 기본 이미지가 쓰는 lerobot 버전이 우리 컨테이너(0.5.0)와 같은
데이터셋 포맷을 읽는지, 우리 `piper-train` 이미지(GHCR)를 잡 이미지로 지정할 수 있는지(`hf jobs run`
은 Docker Hub·Spaces 이미지를 받는다고 돼 있다).

(a) 는 2026-09-23 README 「Google Colab으로 학습하기」에 실었다. ⚠ 실측 전 — 노트북에서 `lerobot==0.5.0`
핀이 Colab 의 torch 와 맞는지, 그 버전이 화면으로 올린 데이터셋(태그 포함)을 읽는지 한 번 돌려 본다.

---

## 7. 가상 환경에서 설치 테스트 후 릴리스 — CI/CD

### 7-1. 지금

- CI 가 없다(`.github/` 없음). 릴리스 = [release.sh](../deploy/release.sh)(diff 로 레이어 판정 →
  빌드 → GHCR·사설 둘 다 push → 매니페스트 재확인) + 사람 확인(R8 "이미지 안을 열어 본다") + .120
  실기. 백엔드 테스트 2,286개와 프론트 빌드는 **개발 머신에서 사람이** 돌린다.
- 이력이 답을 이미 준다. 지난 릴리스의 사고 대부분이 "**개발 머신엔 있는데 배포판엔 없는 것**"
  이었다([체크리스트 R1~R5](../deploy/RELEASE-CHECKLIST.md)): `piper_sdk` 없음(v0.5.3), 파이썬 3.10
  거절(v0.4.16), `docker cp` 겹침(v0.4.16), 반쪽 venv(v0.5.7 뒤), GPU 없는 compose(v0.4.13), GHCR
  미푸시(v0.5.5). 전부 **새 우분투에 처음 깔아 보면 첫 실행에서 잡히는 종류**다 — 요청한 것이 정확히 그것이다.

### 7-2. 세 층

| 층 | 어디서 | 무엇을 | 잡는 것 |
|---|---|---|---|
| **L0 정적** | GitHub Actions, 커밋마다 (~5분) | `pytest`(backend) · `npm run build`(⚠ `tsc --noEmit` 은 no-op) · `ruff` · `test_release.py`·`test_install_docs.py` | 코드·문서 계약 |
| **L1 설치 스모크** | GitHub Actions `ubuntu-22.04` × `ubuntu-24.04` = **매번 새 VM**, docker·compose v2 있음. 태그 push·수동·야간 (~15분) | `piper-install.sh` 를 **그대로** 실행 → 찍힌 sudo 명령을 CI 래퍼가 대신 실행 → 다시 실행 → `/health` 200 · `/` 가 index.html · `/docs` 200 · `apply.sh --check` 0 · `systemctl --user list-units 'piper-*'` 전부 active(장치 없이 뜨는지). 2단계: simd 켜서 가상 팔 등록·짧은 수집 API 한 번 | **배포판에만 없는 것** 전부. GPU 가 없으니 nogpu compose 경로가 저절로 검증된다. 22.04 는 파이썬 3.10 을 지킨다 |
| **L2 맨 VM** | 런너 안 VM(`multipass`/LXD, cloud image) 또는 **빌드 머신의 self-hosted 러너 + libvirt** | "docker 조차 없는 우분투" 부터 — 0절의 "멈추고 사람" 흐름까지. self-hosted 면 사설 레지스트리·**GPU** 스모크도 닿는다 | 전제 검사 자체 |

⚠ L2 의 KVM: GitHub 호스트 런너에서 `/dev/kvm` 은 udev 규칙 한 줄(`KERNEL=="kvm", GROUP="kvm",
MODE="0666"`)로 여는 것이 android-emulator-runner 의 표준 절차이고 2025-10 에도 표준 런너에서 된다는
보고가 있으나, **GitHub 공식 문구는 larger runners 만** 말한다 — 첫 워크플로에서 실측하고 안 되면
self-hosted 로 간다. 실기(CAN·카메라·GPU)는 CI 가 흉내 내지 않는다 — .120 실기는 그대로 남고, CI 는
"새 우분투에 깔면 뜬다" 까지다.

### 7-3. L1 의 함정 (미리 아는 것)

- 런너 사용자에 systemd user manager 가 없다 → `sudo loginctl enable-linger runner`,
  `XDG_RUNTIME_DIR=/run/user/$(id -u)`, `DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus`.
- `apply.sh` 는 설계상 sudo 를 안 쓰고 **명령을 찍고 exit 1** 한다. CI 는 그 출력을 파싱해 실행하는
  래퍼(`deploy/ci/answer-prereqs.sh`)를 둔다 — 래퍼가 못 알아듣는 줄이 나오면 실패 = 문서와
  스크립트가 어긋났다는 신호라 그것도 검사다. `PIPER_YES` 같은 자동 sudo 를 스크립트에 넣지 않는다(설계를 지킨다).
- 디스크: 런너 `/` 는 14GB 남짓 — 베이스 7GB 이미지는 docker 데이터 루트를 `/mnt` 로 옮기거나
  프리셋 정리 스텝을 앞에 둔다.
- 이미지 출처: 릴리스 검증이면 GHCR 의 그 태그를 받고, 커밋 검증이면 잡 안에서 `registry:2` 를
  띄워 `release.sh` 산출물을 밀어 넣고 `PIPER_MIRROR=localhost:5000` 으로(§4 의 손잡이가 여기서도 쓰인다).
- GHCR 패키지가 private 이면 `read:packages` 토큰을 secrets 에. ⚠ 지금 문서가 갈린다 —
  `piper-install.sh` 는 "공개 패키지", 체크리스트는 "private" — §9.

### 7-4. 릴리스 게이트

`release.sh` 가 push 전에 `gh run list --commit <sha>` 로 L0·L1 이 초록인지 보고, 아니면 거절한다
(`--force` 는 사람 결정 — 로그에 남긴다). 태그·굽기·push 는 **여전히 사람이 시작**한다. 다음
단계로 "태그 push 가 트리거 → CI 가 굽고 GHCR·사설 둘 다 밀고(R9) → L1 → 통과해야 `latest`" 로
옮기면 빌드 머신 의존이 사라진다 — 사설 레지스트리는 GHCR 에서 미러링(`skopeo copy`)하면 된다.

### 7-5. 단계와 규모

| 순서 | 일 | 규모 |
|---|---|---|
| 1 | L0 워크플로 | 반나절 |
| 2 | L1 워크플로 + 전제 응답 래퍼(첫 실행에서 런너 함정 몇 개 예상) | 2일 |
| 3 | `release.sh` 게이트 | 반나절 |
| 4 | §3 관문 잡(`compose stop backend` → `/` 가 원인을 말하나) · §4 키트 잡(망 끊고 설치) | 1일 |
| 5 | L2 (VM 또는 self-hosted) — 필요해지면 | 2일 |

⚠ 비용: 공개 저장소면 GitHub Actions 무료, private 이면 무료 플랜 월 2,000분 — L1 이 회당 15~20분이면
커밋마다는 무리라 태그·수동·야간으로 건다.

---

## 8. 우선순위 제안 — 대회 기준

| 순위 | 항목 | 이유 | 규모 |
|---|---|---|---|
| 1 | §4 현장 키트 + `PIPER_MIRROR` | 없으면 현장 설치가 **안 된다** — 지금 오프라인 tar 는 팔·카메라 의존을 못 깐다 | 3일 |
| 2 | §3 게이트웨이 관문 + §5 아이콘 둘 | 일반인이 만나는 첫 실패가 "전부 오류인 화면" 이면 거기서 끝난다. 둘이 한 묶음(진단 아이콘이 ② 층) | 2.5일 |
| 3 | §7 L0 + L1 + 게이트 | 1·2 를 릴리스에 태우는 안전망. 키트·관문 검증 잡도 여기 | 3일 |
| 4 | §1 소유자 라벨 + 크레딧 스크립트 | 대회 방식 A 면 스크립트만, B/C 면 라벨까지 | 1.5일 |
| 5 | §2 문서 한 줄 + Safari 실측 | 극소 | 0.5일 |
| 6 | §6 Colab 노트북 문서 → 다음 분기에 HF Jobs 러너 | 대회 뒤 | 0.5일 / 3일 |

합계 약 11일(HF Jobs 제외). 1~3 이 대회 전 필수.

## 9. 먼저 정해야 할 것

| 질문 | 갈리는 것 |
|---|---|
| 대회 Vast 방식 — 참가자가 각자 가입할 수 있나(결제수단 요구?) | A 냐 B 냐. A 면 §1-4 는 덤 |
| 현장 기계는 우리가 준비한 NUC(GPU 없음)인가, 참가자 기계에 새로 까나 | 키트에 NVIDIA 를 넣을지(넣지 않기를 권함) |
| 참가자 노트북 OS·브라우저 | Safari 실측 여부 |
| 저장소·GHCR 패키지 공개 여부 | CI 분·토큰. 문서가 지금 갈린다(§7-3) |
| 판단 LLM(ollama·4.7GB)을 현장에서 쓰나 | 키트 크기 |

## 10. 조사 출처

- Vast.ai — [Teams roles](https://docs.vast.ai/guides/teams/teams-roles) · [Teams quickstart](https://docs.vast.ai/guides/teams/teams-quickstart) · [API key permissions · constraints](https://docs.vast.ai/api-reference/permissions) · [transfer credit](https://docs.vast.ai/api-reference/accounts/transfer-credit)
- Google Colab — [FAQ](https://research.google.com/colaboratory/faq.html)(금지 행위·세션 한도) · [Colab Enterprise notebook execution](https://docs.cloud.google.com/colab/docs/schedule-notebook-run)
- Hugging Face — [LeRobot hardware guide: Hugging Face Jobs](https://huggingface.co/docs/lerobot/hardware_guide) · [LeRobot il_robots: Train using Hugging Face Jobs](https://huggingface.co/docs/lerobot/il_robots) · [Jobs pricing](https://huggingface.co/docs/hub/jobs-pricing)
- Kaggle — [Efficient GPU usage](https://www.kaggle.com/docs/efficient-gpu-usage); 주간 30h·세션 12h 는 Kaggle 포럼 공지 기준(2026 시점 유지)
- GitHub Actions KVM — [android-emulator-runner README](https://github.com/ReactiveCircus/android-emulator-runner)(udev 절차) · [community discussion #8305](https://github.com/orgs/community/discussions/8305)
