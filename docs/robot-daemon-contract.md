# 로봇 데몬 계약 — 외부 로봇을 웹에 붙이는 표준 통로

새 로봇(다른 인터페이스의 팔)을 추가할 때 만드는 것은 **데몬 하나와 이 계약의
구현**이지, 게이트웨이·프론트 수술이 아니다. 첫 적용: so101d
([feature/so101d.md](../feature/so101d.md)). robotd 는 이 계약의 원형이되
Piper 전용 확장(마스터/슬레이브·0x150·진단)을 더 가진다.

## 세 채널

```
웹 ── REST/WS ── 게이트웨이 ─┬─ ① 제어면: 버스 RPC (rpc_call → 데몬)
                            ├─ ② 데이터면: /dev/shm 팔 세그먼트 (seqlock)
                            └─ ③ 버스: heartbeat (estopd·상태바 감시)
```

**웹은 데몬을 직접 모른다.** 게이트웨이의 허브 클라이언트
(`backend/app/services/so101_client.py` 꼴)가 유일한 통로다.

## ① 제어면 — RPC 동사 (이름·의미 고정)

| 동사 | 의미 |
|---|---|
| `scan()` | 장치 열거. **다시 보인 장치의 lost 를 지운다** (1b036f0 의 교훈) |
| `attach(id, name, …)` | 장치 열기 + 건강 확인 + shm 발행 시작. 실패 사유는 사람이 읽을 문구로 |
| `release(name)` | 발행 정지 + 세그먼트 정리 |
| `estop(name="")` | 즉시 무력화 (토크 차단 등). 빈 이름 = 전부 |
| `info(name="")` | 상태 + **capabilities 선언** (아래) |
| `lost()` | **데몬이 판정한** 사라진 장치 — 게이트웨이가 추론하지 않는다 |
| `restart` | 응답 후 자진 종료 — 유닛의 Restart=always 가 새 코드로 살린다 |

### capabilities — UI 는 이것으로 버튼을 그린다

```json
{ "model": "so101", "transport": "serial", "dof": 5, "gripper": true,
  "kinematics": "so101", "joint_names": ["..."],
  "features": { "master_slave": false, "hw_zero": false, "slip_reset": false,
                "parking": false, "rate_limit": true } }
```

프론트에 `if model == "piper"` 분기를 만들지 않는다 — 기능 유무는 데몬이
선언하고, 없는 기능의 버튼은 그리지 않는다.

## ② 데이터면 — 표준 팔 shm 세그먼트

- `piper.arm.<이름>.state` (데몬→소비자) / `.action` (소비자→데몬).
  레코드는 위치 인덱스 7-float (`piper_shm.arm`) — 관절이 6개 미만이면 빈
  자리를 0 으로 채우고, 자리↔이름 매핑 테이블을 자기 패키지 **한 곳**에 둔다.
- 정규화 규약: 관절 -100..100, 그리퍼 0..100.
- **deadman**: action 헤더의 `deadman_ms` 경과 = 그 자리에 정지 (토크 유지).
  토크 차단은 `estop` 의 몫이다 — 역할을 섞지 않는다.
- 세그먼트 수명: lost/release 시 반드시 지운다 — 남기면 소비자가 **멈춘
  자세**를 관측으로 받는다. 기동 시 **자기 접두사의 잔재만** 청소한다 — ⚠ robotd 가
  필터 없이 전부 지워 살아 있는 so101d 의 리더 세그먼트를 지운 사고(2026-09-09):
  발행자는 unlink 된 inode 에 계속 써 **조용히** 깨진다(published 는 오르는데 경로로는
  아무도 못 연다). 그래서 발행자의 의무 하나 더: 발행 루프가 주기적으로
  `StateWriter.orphaned` 를 보고 사라졌으면 **다시 만든다** (so101d·simd 구현).

## ③ 버스 — heartbeat

`mark_alive(<데몬이름>, info=self_report(...))` 주기 발행. `piper_bus.contract`
의 `DAEMON_SOURCES` 에 등록해야 상태바의 "낡은 코드" 감시가 붙는다.

## systemd

`deploy/systemd/piper-<이름>d.service` (Restart=always) +
`deploy/install-daemons.sh <이름>d` 로 설치. 로컬 패키지는
`deploy/install.sh` 의 PKGS 에 — 가드 테스트(`test_install.py`)가 잡는다.
유닛은 `piper_bus.contract.UNIT_CATALOG` 에도 넣는다 — 화면 [서비스] 가 그리는 표이자
unitd 의 허용 목록이다(`test_unitd.py` 가 유닛 파일과 대조). 있어도 없어도 되는
데몬은 `optional` 로 — 설치는 되지만 사람이 웹에서 켠다 (feature/services.md).

## 게이트웨이 쪽 의무

- 허브 클라이언트: `is_alive` 단축(죽은 데몬을 기다리지 않는다) + 실패해도
  게이트웨이를 죽이지 않는다.
- `device_watch` 에 `lost()` 합류 — 경보·화면 상태가 한 경로로 흐른다.
- 라우트는 `/api/robots/*` 아래로 — 프론트가 새 데몬을 알 필요가 없다.
