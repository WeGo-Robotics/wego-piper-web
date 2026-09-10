# 버전 정보 · 업데이트 버튼

> 요청(2026-09-09): "서비스 및 외부 소프트웨어 버전 정보, 업데이트 버튼."

## 0. 요지

[설정 → 서비스] 맨 위에 **버전 카드** 하나: 지금 도는 piper-web 버전, 새 버전이
있으면 배지와 [업데이트], 아래에 외부 소프트웨어 표(컨테이너·호스트·데몬).
업데이트는 **호스트의 `piper-unitd` 가 일시 유닛으로 `piper-install.sh vX` 를
띄운다** — 게이트웨이는 그 절차의 마지막에 자기 자신이 갈아치워지므로 직접 돌릴
수 없다. 화면은 끊길 것을 알고 끊긴다.

## 1. 지금 있는 것 (실측 2026-09-09)

| 사실 | 뜻 |
|---|---|
| 앱은 자기 버전을 모른다 — `frontend/package.json` `0.0.0`, `backend/pyproject.toml` `0.1.0` | 버전 정본은 코드 밖에 있다 |
| 이미지 안 `/opt/piper-host/manifest.txt` 에 `version="v0.4.5" prev= built_at= images= wheels= daemons= registry=` | **배포 기계의 정본.** 게이트웨이가 읽을 수 있다(같은 이미지) |
| 호스트에 `~/piper-web-deploy/<버전>/` 가 버전마다 남고 `current` 가 적용본 | 이전 버전으로 되돌릴 재료가 이미 있다 |
| 소스 기계는 `git describe --tags` 뿐 | 정본이 다르다 — 리졸버 하나로 감싼다 |
| 컨테이너: lerobot 0.5.0 · torch 2.11.0+cu130 · CUDA 13.0 · pyrealsense2 2.58 · transformers 5.3 · piper_sdk 0.6.1 · opencv 4.12 | `importlib.metadata` 로 즉시 |
| 호스트: NVIDIA 580.173.02 · docker 29.1.3 · redis 7.0.15 · python 3.13 · ollama 0.32.13(`:11434/api/version`) | 컨테이너에서는 **안 보인다** — 호스트 쪽이 말해야 한다 |
| 데몬 venv: mujoco · feetech-servo-sdk · pyrealsense2 · piper_* wheel | 데몬만 안다 |
| 데몬 자기 보고(`self_report` → `mark_alive` info): pid·started·code_mtime | **버전을 실을 자리가 이미 있다** |
| `piper-install.sh vX` = pull → `docker create/cp` → `apply.sh`(전제 확인 → sudo 필요하면 명령 찍고 **exit 1** → wheel·유닛 → `compose up -d`) | 마지막 단계가 게이트웨이 컨테이너를 재생성한다 |
| `apply.sh` 4절이 estopd·robotd·camerad·rsd 를 재시작하고, `--optional` 은 켜 둔 것만 재시작 — **unitd 는 빠져 있었다**(1단계에서 넣음) | 업데이트 뒤 옛 코드로 남는 데몬이 있으면 서비스 패널의 "코드가 더 새것"과 [버전] 카드의 wheel 대조가 잡는다 |
| 레지스트리 `piper-build:5000` — HTTP, 인증 없음, `/v2/<이미지>/tags/list` | 새 버전 유무를 여기서 안다 |
| `.dockerignore` 가 `*.md` 를 뺀다 | CHANGELOG 가 이미지에 없다 — 후보 버전의 "무엇이 달라졌나"를 보여 주려면 실어야 한다 |

## 2. 버전 — 정본 하나, 리졸버 하나

```
running_version():
    PIPER_VERSION 환경변수            # 이미지에 ENV/LABEL 로 박는다 (release.sh 가 --build-arg)
    → /opt/piper-host/manifest.txt   # 배포 기계
    → git describe --tags --dirty    # 소스 기계
    → "unknown"
```

`GET /api/system/version` →
```json
{"version": "v0.4.5", "built_at": "...", "prev": "v0.4.4", "source": "manifest|git|env",
 "installed_at": "...",                       // ~/piper-web-deploy/current 의 시각 (호스트 보고)
 "external": {
   "container": {"lerobot": "0.5.0", "torch": "2.11.0+cu130", "cuda": "13.0", ...},
   "host":      {"driver": "580.173.02", "docker": "29.1.3", "redis": "7.0.15", "ollama": "0.32.13"},
   "daemons":   {"robotd": {"piper_robot": "0.1.0", "piper_sdk": "0.6.1"},
                 "simd": {"mujoco": "3.12.0"}, "so101d": {"feetech-servo-sdk": "..."}, ...}
 }}
```

- **컨테이너 것**은 게이트웨이가 `importlib.metadata` 로 직접 잰다.
- **호스트 것**은 unitd 가 `list` 에 실어 보낸다(docker·redis·driver·ollama). unitd 는
  이미 호스트에서 돌고 systemctl 을 쥔 데몬이다 — 호스트 사실을 말할 자리로 맞다.
- **데몬 것**은 각 데몬의 자기 보고에 `versions` 필드를 더한다 — `self_report` 가
  자기 패키지(`piper_*`)와 바깥 의존(`mujoco`·`scservo_sdk`·`pyrealsense2`)을
  `importlib.metadata` 로 한 번 재서 싣는다. 컨테이너 게이트웨이가 호스트 venv 를
  들여다볼 길이 없으므로 이 길이 유일하다.
- ⚠ **`nvidia-smi` 는 D-state 로 멈출 수 있다**(resources.py 의 교훈). 드라이버
  버전은 자원 샘플러가 이미 4초마다 뜬 것(`trends.latest_gpus()`)을 재사용한다 —
  같은 위험한 호출을 또 하지 않는다.
- `piper_*` wheel 버전은 전부 `0.1.0` 이라 뜻이 없다. **릴리스 태그가 곧 버전**이므로
  release.sh 가 wheel 을 굽기 전에 `pyproject` 의 version 을 태그로 바꿔 굽는다
  (저장소는 안 바꾼다 — 굽는 사본만). 그래야 호스트 venv 에 어느 릴리스의 wheel 이
  깔렸는지 데몬 보고로 안다.

## 3. 새 버전 확인

- `GET /api/system/update/check` → 레지스트리 `tags/list` 를 읽어 semver 로 정렬,
  지금 버전보다 큰 것이 있으면 `{"latest": "v0.4.6", "available": true}`.
  주소는 매니페스트의 `registry=` (배포 기계) — 소스 기계는 `git ls-remote --tags origin`.
- **자동으로 안 받는다.** 확인은 폴링(10분)하고 배지만 띄운다 — 390MB 를 사람이
  모르게 받지 않는다.
- 후보의 "무엇이 달라졌나": stage-hostside 가 `CHANGELOG.md` 를 `/opt/piper-host/` 에
  싣는다. **받기(pull) 뒤**에야 읽을 수 있으므로 절차를 셋으로 나눈다 —
  [확인] → [받기] → [적용]. 받기는 안전하다(실행하지 않는다, `piper-install.sh` 2절과
  같은 이유). 받은 뒤 화면이 그 버전의 절을 보여 주고, 그때 [적용]을 누른다.

## 4. 업데이트 실행 — unitd 의 일시 유닛

| 길 | 판정 |
|---|---|
| 게이트웨이가 `piper-install.sh` 를 subprocess 로 | ❌ 마지막 `compose up -d` 가 **자기 컨테이너를 죽인다** — 절차가 중간에 끊긴다 |
| 게이트웨이가 `systemd-run` | ❌ 컨테이너엔 systemd 가 없다 (services.md §1 과 같은 벽) |
| **unitd 가 `systemd-run --user --unit piper-update-<버전> <스크립트>`** | ✅ 소유자가 systemd. 게이트웨이가 죽어도 산다. 로그는 journald |

- `unitd.update(version, stage)` — `stage ∈ pull | apply`. `pull` 은 `piper-install.sh`
  의 0~2절(받기·꺼내기)만, `apply` 는 `~/piper-web-deploy/<버전>/apply.sh`.
  둘 다 일시 유닛 하나로 돌고 `unitd.update_status()` 가 유닛 상태(active/failed/
  inactive) + 저널 꼬리 + **전제 미비 줄**(`sudo ...` 로 시작하는 줄)을 돌려준다.
- 소스 기계: `deploy/update-source.sh <태그>` = `git fetch` → `git checkout <태그>` →
  `deploy/install.sh` → 바뀐 유닛 재시작. 같은 unitd 경로, 스크립트만 다르다.
- **적용 전 게이트**: `require_idle` 전부 — 녹화·추론·학습·텔레옵 중이면 거절.
  학습은 별도 유닛이라 살아남지만 컨테이너 재생성으로 `/data` 마운트가 잠깐 흔들릴
  이유는 없다 — 그래도 막는다. 판단은 사람이 다시 누르는 것으로.
- **sudo 는 자동화하지 않는다.** apply.sh 의 설계 그대로: 전제가 빠지면 명령을
  찍고 멈춘다. 화면이 그 줄들을 **복사 가능한 블록**으로 보여 주고 "실행한 뒤
  [적용]을 다시 누르세요". 재로그인이 필요한 것(그룹)은 그렇게 말한다.
- **적용 뒤 데몬 재시작**: apply.sh 가 돌고 있는 유닛을 건너뛰므로, 적용이 끝나면
  unitd 가 **wheel 이나 데몬 소스가 바뀐 유닛만** 재시작한다(매니페스트의 `wheels=`·
  `daemons=` 로 안다). estopd 포함 — 이 순간은 활동이 없다(게이트가 보장). 순서:
  estopd 마지막.
- **끊김**: [적용]을 누르면 화면이 "업데이트 중 — 몇 초 뒤 연결이 끊깁니다" 를 띄우고,
  `/health` 를 2초마다 두드려 돌아오면 새로고침한다. 돌아온 화면의 버전 카드가
  새 버전과 **적용 로그**(unitd 저널)를 보여 준다 — 무슨 일이 있었는지 사람이 본다.
- **되돌리기**: `~/piper-web-deploy/` 에 남은 이전 버전 디렉토리로 `apply.sh` 를 다시 —
  같은 unitd 경로, `version=<이전>`. 이미지는 로컬에 남아 있어 pull 이 없다.
  데이터(`/srv/piper-data`·데이터셋·설정)는 어느 방향이든 안 건드린다(README 규칙).

## 5. 화면

```
┌ 버전 ────────────────────────────────────────────────────────┐
│ piper-web v0.4.5   2026-09-09 19:20 설치 · 직전 v0.4.4        │
│ ● 새 버전 v0.4.6 있음   [받기]  ─(받은 뒤)→  [적용]  [무엇이 달라졌나] │
│ 업데이트 기록: v0.4.4 → v0.4.5 (09-09 19:20, 2분 10초, 성공)   │
├ 외부 소프트웨어 ────────────────────────────────────────────┤
│ 컨테이너  lerobot 0.5.0 · torch 2.11.0 (CUDA 13.0) · transformers 5.3 · piper_sdk 0.6.1 │
│ 호스트    NVIDIA 580.173.02 · docker 29.1.3 · redis 7.0.15 · ollama 0.32.13             │
│ 데몬      robotd piper_robot v0.4.5 · simd mujoco 3.12.0 · so101d feetech 1.x · rsd pyrealsense2 2.58 │
└──────────────────────────────────────────────────────────────┘
```

- 데몬 줄은 **자기 보고에서 온 것만** 그린다. 죽은 데몬은 "—". 지어내지 않는다
  (`get_safety` 규칙과 같다: 모르는 것은 모른다고).
- 컨테이너 버전과 데몬 wheel 버전이 **다르면** 노란 줄: "게이트웨이 v0.4.6, robotd
  wheel v0.4.5 — 데몬 재시작이 안 됐다". 지금 서비스 패널의 "코드가 더 새것"과 짝이다.
- 적용 중 전제 미비는 빨간 블록 + 복사 버튼.

## 6. 순서

1. ✅ **버전 정보** (2026-09-09) — `app/services/version.py` 리졸버(ENV → 매니페스트 →
   git), `GET /api/system/version`, 이미지에 `PIPER_VERSION`(compose `build.args` 로 —
   release.sh 의 빌드 명령 원문은 테스트가 지키므로 환경으로 넘긴다), 데몬
   `self_report` 에 `versions`(소스 목록 → `piper-*`, 계약 `DAEMON_DISTS` → 바깥 것),
   unitd `host_info`(docker·redis·python·**드라이버는 `/proc/driver/nvidia/version`**·
   ollama·배포 디렉토리), release 가 wheel 사본의 version 을 태그로 도장, apply.sh 가
   `current/VERSION` 기록 + **unitd 재시작 누락** 보완. 실측(소스 기계): 게이트웨이
   `v0.4.5-1-g7a6c7c0-dirty`(git), 컨테이너 lerobot 0.5.0·torch 2.10·piper_sdk 0.6.1,
   호스트 드라이버 580.173.02·docker 29.1.3·redis 7.0.15·ollama 0.32.13, simd 자기 보고
   mujoco 3.12.0. 데몬 버전은 **재시작한 데몬부터** 온다(옛 self_report 엔 없다).
   잡은 것: `/proc` 의 "Kernel Module for x86_64  580.173.02" 에서 첫 정규식이 "for" 를
   잡았다 → 점 든 숫자열로.
2. ✅ **확인·받기** (2026-09-09) — `GET /api/system/update/check`(배포: 매니페스트의
   레지스트리 `tags/list`, 소스: `git ls-remote --tags`; `vX.Y.Z` 만 세고 10분 캐시,
   실패는 `error`), `POST /update/pull` → `unitd.update(ver, "pull")` =
   `REPO/deploy/piper-install.sh <ver> --pull-only`(새 옵션 — 받고 꺼내기만, apply 를
   부르지 않는다), 번들에 `piper-install.sh`·`update-source.sh`(daemons.tar.gz)·
   `CHANGELOG.md` 를 싣고 `GET /update/notes` 가 그 절을 낸다. 실측(이 기계, 로컬
   레지스트리): 받기 0.5초, 상태 진행 중→완료(ok, `sub=exited`)와 저널이 화면에 옴.
   두 번째 받기는 꺼낸 번들의 매니페스트가 `piper-build:5000` 을 가리켜 이 기계에선
   이름이 안 풀려 **실패로 정확히 보고**됐다(종료 코드 1 + 원인 로그) — 실패 경로 검증.
   잡은 것: `systemd-run --collect` 는 끝나는 순간 유닛을 지워 "끝났나"를 알 길이
   없다 → `--remain-after-exit` 로 남기고 SubState 로 판정, 저널은 시작 시각 이후만,
   ANSI 색 코드는 걷어낸다.
3. ✅ **적용·되돌리기** (2026-09-09, 코드·테스트) — `POST /update/apply` 는 활동
   중(`exclusivity.running()`)이면 409, unitd 가 받아 둔 `<WORK>/<ver>/apply.sh` 를
   같은 일시 유닛으로; 화면은 논블로킹 확인 뒤 "곧 끊깁니다"를 띄우고 `/health` 를
   두드리다 돌아오면 새로고침, 전제 미비(`sudo …` 줄)는 복사 블록. 되돌리기 =
   받아 둔 다른 버전 선택 → 같은 적용. apply.sh 가 unitd 도 재시작하고
   `current/VERSION` 을 기록. ⚠ **실기 적용은 아직 안 돌렸다** — 이 기계는 소스로
   도는데 배포 번들의 apply.sh 는 compose 로 같은 포트에 컨테이너를 띄우려 든다.
   첫 실전은 .120 에서 v0.4.5 → v0.4.6 이다(§6 검증 기준 그대로).
4. ✅ **소스 기계** (코드) — `deploy/update-source.sh <태그>`: 작업 트리가 더러우면
   거절, fetch → checkout(태그) 또는 ff-only pull → `install.sh` → 돌고 있는
   `piper-*` 재시작(estopd 마지막). 확인은 원격 태그로. 이 기계에서 실행은 안 했다 —
   작업 트리가 더러워 스스로 거절하는 것이 맞다.

## 7. 위험

| 위험 | 대응 |
|---|---|
| 레지스트리에 인증이 없다 — 같은 망의 누가 `v0.4.6` 을 밀어 넣으면 버튼이 그걸 깐다 | 후보의 **이미지 다이제스트**를 카드에 보여 주고 빌드 기계의 매니페스트와 대조한다. 망 밖이면 TLS 부터(registry.sh 머리말) |
| 적용 중 게이트웨이가 죽어 진행률을 못 보여 준다 | 그 사이는 못 본다고 말한다. 돌아온 뒤 저널로 사후 보고 |
| `apply.sh` 가 중간에 멈추면(전제) 반쯤 적용된다 | apply.sh 는 전제를 **먼저** 다 보고 멈춘다 — 그 뒤 단계는 순서대로, 두 번 돌려도 같다(설계 문서). 되돌리기도 같은 스크립트 |
| 업데이트 유닛이 로그아웃에 죽는다 | linger — 이미 전제(`apply.sh` 가 확인) |
| 데몬 wheel 이 전부 `0.1.0` 이라 어느 릴리스인지 모른다 | release.sh 가 굽는 사본의 version 을 태그로 |
| 긴 pull(첫 설치 7GB) | 받기 단계는 저널에 진행률이 남는다 — 화면이 꼬리를 보여 준다 |

## 열린 질문

- 학습이 도는 중에 업데이트를 허용할지 — 학습은 별도 유닛이라 이론상 살지만, 이번엔 막는다.
- ghcr 공개 이미지(README 의 기본 주소)로 가면 "새 버전 확인"이 GitHub API 가 된다 — 오프라인 현장은 레지스트리·USB 뿐.
- 프론트 `package.json` 버전을 태그와 맞출지 — 화면 우하단 버전 표기용. 리졸버가 있으면 필요 없다.
