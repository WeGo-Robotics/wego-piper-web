# 카메라 트러블슈팅 — 어느 단계에서 끊겼나

설치 직후 문제(그룹 권한·데몬 기동)는 [설치 트러블슈팅 §3](install-troubleshooting.md#3-웹은-뜨는데-카메라팔이-안-보인다)
에 있다. 이 문서는 **깔려서 돌던 기계에서 카메라가 안 될 때** 본다.

## 0. 단계를 알면 범위가 반으로 준다

```
[스캔]  화면 → GET /api/cameras/scan → v4l2_hub.scan()  → camerad → 열거만
[등록]  화면 → POST /api/cameras/register
                  └ register_camera() → cam.connect()   → camerad → 장치 열기 + 스트림 시작 + shm 발행
[프리뷰] 화면 <img src=…/preview>                        ← shm 세그먼트를 읽는다
```

⚠ **스캔도 camerad 를 거친다.** 그러니 **스캔이 된다 = camerad 는 살아 있다** 이고,
"데몬이 안 떴다" 는 후보에서 빠진다. 스캔만 되고 등록이 안 되면 문제는 **장치를 열고
스트림을 시작하는 구간**이다 — 열거는 되는데 `STREAMON` 이 안 되는 상태.

| 어디까지 됐나 | 남은 후보 |
|---|---|
| 스캔 0개 | 그룹 권한(`video`) · camerad 죽음 · USB 컨트롤러([설치 §3](install-troubleshooting.md#3-웹은-뜨는데-카메라팔이-안-보인다)) |
| 스캔 O, 등록 X | **이 문서 §1** — 장치 열기·스트림 시작 |
| 등록 O, 프리뷰 X | shm 세그먼트 · `streaming=false`(프레임이 안 온다) |

## 1. 스캔은 되는데 등록이 안 된다

**먼저 화면이 말하는 사유를 읽는다.** 등록 실패는 camerad 가 말한 사유를 그대로 보여
준다(`등록 실패: Cannot open /dev/video0` 처럼).

⚠ **v0.5.7 까지는 안 그랬다**(고침은 그 뒤, 아직 릴리스 전). 사유를 **세 번 덮어썼다** — `register_camera()` 가 `ok, _ = cam.connect()`
로 버리고, 라우터가 "연결되지 않은 카메라입니다" 라는 *결과*로 바꾸고, 화면이 다시
"등록 실패" 한 줄로 만들었다. 사유 없는 실패는 고칠 수 없는 실패다. **.120 이 지금
그 버전이므로**, 돌아올 때까지는 저널에서 직접 읽어야 한다:

```bash
journalctl --user -u piper-camerad -b -n 100
```

| 사유에 이런 말이 | 뜻 | 처방 |
|---|---|---|
| `Cannot open /dev/videoN` | 노드는 있는데 열리지 않는다 | 다른 프로세스가 쥐고 있나: `sudo fuser -v /dev/video*`. 브라우저 탭·화상회의 앱이 흔하다 |
| `Device or resource busy` | 누가 이미 열었다 | 위와 같다. camerad 자신이 이중으로 잡았으면 `systemctl --user restart piper-camerad` |
| `No space left on device` (열기·`STREAMON`) | **디스크가 아니라 USB 대역폭**이다. 같은 컨트롤러에 카메라를 여러 대 물리면 UVC 대역 예약이 모자란다 | 다른 USB 컨트롤러(다른 쪽 포트)로 옮긴다 · 해상도/FPS 를 낮춘다 · MJPG 로(`fourcc`) |
| `Permission denied` | `video` 그룹 | [설치 §3](install-troubleshooting.md#3-웹은-뜨는데-카메라팔이-안-보인다) |
| 30초쯤 뒤 아무 말 없이 실패 | RPC 타임아웃 — camerad 가 장치 열기에서 굳었다. UVC 컨트롤 질의가 커널 `D` 상태로 멈추는 전례가 있다(D405) | `ps -o stat= -p $(pgrep -f camerad)` 가 `D` 면 SIGKILL 도 안 먹는다. 그 포트를 리바인딩: `echo 0 \| sudo tee /sys/bus/usb/devices/<포트>/authorized; sleep 2; echo 1 \| sudo tee …` (포트는 `lsusb -t` 로 **매번** 확인 — 바뀐다) |

### 현장에서 한 번에 받아둘 것

```bash
systemctl --user status piper-camerad
journalctl --user -u piper-camerad -b -n 100     # ⚠ -b = 그 부팅. 재부팅 뒤면 이게 핵심이다
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext | head -30
sudo fuser -v /dev/video*                        # 누가 쥐고 있나
df -h /dev/shm                                   # 세그먼트 자리
dmesg | grep -i -E "usb|uvc|video" | tail -30
lsusb -t                                         # 어느 컨트롤러에 몇 대가 물렸나
```

## 2. 사례 — .120 을 밖에 들고 나갔을 때 (2026-09-22, **미해결**)

사용자 보고. **원인은 아직 모른다** — 기계가 돌아오면 §1 의 명령으로 좁힌다.

| | |
|---|---|
| 상황 | .120 을 외부로 들고 나가 재부팅. 인터넷·LAN 없음 |
| 증상 | USB 카메라가 **스캔은 되는데 등록이 안 됨**. 노트북 내장 카메라도 같음 |
| 화면이 준 단서 | "등록 실패" 한 줄뿐 (그래서 §1 의 사유 표시를 고쳤다) |

### 좁혀진 것

- **camerad 는 살아 있었다.** 스캔이 camerad 를 거치므로(§0), 데몬 기동 실패는 아니다.
- **장치 하나의 문제가 아니다.** USB 카메라와 내장 카메라가 **둘 다** 그랬다면 그 부팅의
  환경 문제다 — 케이블·개별 장치 불량 쪽은 약하다.
- **권한도 아닐 가능성이 높다.** `video` 그룹이 빠졌으면 스캔이 0개가 된다(설치 §3).

### ⚠ 네트워크 때문은 아니다

오프라인이 눈에 띄는 조건이라 의심할 만하지만, **카메라 경로에는 네트워크 코드가 없다**
(`daemons/` 전수 확인: `urlopen`/`requests`/`httpx` 를 쓰는 데몬은 `unitd` 뿐이고, 그것도
`127.0.0.1:11434`(Ollama) 한 줄이다). 부팅도 막히지 않는다 — 유닛 10개 어디에도
`After=network-online.target` 이 없다.

인터넷이 필요한 것은 전부 사람이 누를 때다: 저장소 다운로드, 클라우드 GPU, 새 버전 확인.
수집·추론·텔레옵·로컬 학습은 오프라인으로 된다.

⚠ 다만 **시계**는 다르다. 네트워크가 없으면 NTP 동기도 없어서, RTC 가 없거나 방전된
보드는 시각이 틀어진 채 뜬다. 카메라와는 무관하지만 에피소드·학습 폴더 이름이 엉뚱한
날짜로 찍히고, 클라우드 회수의 "이번 회차 가중치인가" 판정이 벽시계를 쓴다.

### 남은 후보 (좁히지 못함)

1. `STREAMON` 실패 — USB 대역폭. 밖에서 허브·포트 구성이 달라졌을 수 있다
2. 다른 프로세스가 장치를 쥠
3. `/dev/shm` 부족 — 세그먼트 할당 실패
4. 장치 열기가 `D` 상태로 멈춰 30초 RPC 타임아웃

## 3. 등록은 됐는데 영상이 없다

| 증상 | 원인 | 처방 |
|---|---|---|
| 화면은 "연결됨" 인데 프리뷰가 정지 | 세그먼트에 **마지막 프레임이 남아** 있어 `has_preview` 는 참이지만 스트림은 죽었다. 그래서 뽑힌 카메라가 정상처럼 보였다 | 화면의 `streaming` 표시를 본다 — "지금 프레임이 오나" 만 말한다. 거짓이면 [연결 해제] → [연결] |
| D405 에서 color 가 0fps | **color 만 켜면 안 나온다.** depth 를 같이 켜야 color 가 나온다 | `use_depth=True` (데이터셋·정책에는 color 만 들어간다) |
| 재시작 뒤 화면이 빈다(시뮬 카메라) | simd 가 재시작하면 세그먼트가 사라지는데 게이트웨이가 `connected=True` 로 캐시한 채면 창이 연결을 건너뛴다 | 스캔을 다시 — simd 의 사실로 맞춘다 |

## 4. 관련 문서

- [설치 트러블슈팅](install-troubleshooting.md) — 그룹 권한·데몬 기동·USB 컨트롤러 사망
- [카메라 프로파일](../feature/camera-profiles.md) — 노출·화이트밸런스가 연결 시 적용되는 경로
- [조명 감시](../feature/lighting-watch.md) — 카메라별 경보 on/off
