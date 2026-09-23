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
| **`No module named 'cv2'`** | **실기에서 실제로 난 원인(§2).** 데몬 venv 에 OpenCV 가 없다 — 열거는 cv2 없이 되고 **여는 것만** `cv2.VideoCapture` 라, 딱 등록에서만 죽는다 | 아래 **§2-1** |
| `Cannot open /dev/videoN` | 노드는 있는데 열리지 않는다 | 다른 프로세스가 쥐고 있나: `sudo fuser -v /dev/video*`. 브라우저 탭·화상회의 앱이 흔하다 |
| `Device or resource busy` | 누가 이미 열었다 | 위와 같다. camerad 자신이 이중으로 잡았으면 `systemctl --user restart piper-camerad` |
| `No space left on device` (열기·`STREAMON`) | **디스크가 아니라 USB 대역폭**이다. 같은 컨트롤러에 카메라를 여러 대 물리면 UVC 대역 예약이 모자란다 | 다른 USB 컨트롤러(다른 쪽 포트)로 옮긴다 · 해상도/FPS 를 낮춘다 · MJPG 로(`fourcc`) |
| `Permission denied` | `video` 그룹 | [설치 §3](install-troubleshooting.md#3-웹은-뜨는데-카메라팔이-안-보인다) |
| 30초쯤 뒤 아무 말 없이 실패 | RPC 타임아웃 — camerad 가 장치 열기에서 굳었다. UVC 컨트롤 질의가 커널 `D` 상태로 멈추는 전례가 있다(D405) | `ps -o stat= -p $(pgrep -f camerad)` 가 `D` 면 SIGKILL 도 안 먹는다. 그 포트를 리바인딩: `echo 0 \| sudo tee /sys/bus/usb/devices/<포트>/authorized; sleep 2; echo 1 \| sudo tee …` (포트는 `lsusb -t` 로 **매번** 확인 — 바뀐다) |

### 현장에서 한 번에 받아둘 것

⚠ **가장 빠른 한 방은 사유를 직접 꺼내는 것이다.** 화면이 사유를 안 주는 버전이라면
게이트웨이 컨테이너 안에서 RPC 를 그대로 부른다 — 실기에서 이걸로 3분 만에 잡았다:

```bash
docker exec piper-web-backend python -c "
from app.services.v4l2_client import v4l2_hub
print('scan   :', v4l2_hub.scan())
print('connect:', v4l2_hub.connect('/dev/video0', 0, 0, 0, {}))"
```

`scan` 은 되는데 `connect` 가 `(False, …)` 면 그 문자열이 답이다.


```bash
systemctl --user status piper-camerad
journalctl --user -u piper-camerad -b -n 100     # ⚠ -b = 그 부팅. 재부팅 뒤면 이게 핵심이다
v4l2-ctl --list-devices                          # 없으면: sudo apt install v4l-utils
v4l2-ctl -d /dev/video0 --list-formats-ext | head -30
sudo fuser -v /dev/video*                        # 누가 쥐고 있나
df -h /dev/shm                                   # 세그먼트 자리
dmesg | grep -i -E "usb|uvc|video" | tail -30
lsusb -t                                         # 어느 컨트롤러에 몇 대가 물렸나
```

## 2. 사례 — .120 (2026-09-22 보고 → 2026-09-23 **해결**)

| | |
|---|---|
| 상황 | .120(MSI Thin 15 노트북)을 외부로 들고 나가 재부팅. 인터넷 없음 |
| 증상 | USB 카메라도 내장 카메라도 **스캔은 되는데 등록이 안 됨** |
| 화면 | "등록 실패: 연결되지 않은 카메라입니다" — 결과를 사유처럼 말하고 있었다 |
| **원인** | 데몬 venv 에 **`cv2` 가 없었다** |

### 어떻게 찾았나

게이트웨이 컨테이너 안에서 RPC 를 직접 불러 **버려지던 사유**를 꺼냈다:

```
scan      : [{'id': '/dev/video0', 'name': 'HD Webcam: HD Webcam', ...}]   ← 열거는 된다
connect   : (False, "No module named 'cv2'")                               ← 여는 것이 안 된다
```

⚠ camerad 저널에는 **등록 시도 흔적이 아예 없었다.** 연결이 데몬에 닿기 전에 끝났기
때문이다 — 저널만 보면 "데몬은 멀쩡한데 왜 안 되지" 로 막힌다.

### 왜 cv2 가 없었나 — 세 겹이다

```
venv  python : 3.13.12   ← ~/miniconda3
OS    python : 3.10.12
OS 의 cv2    : ~/.local/lib/python3.10/site-packages/cv2   (4.13.0)
venv sys.path: …/miniconda3/lib/python3.13/site-packages   ← 3.10 것은 안 보인다
```

1. **`--system-site-packages` 의 "시스템" 은 OS 가 아니다.** venv 를 만든 *그 파이썬*의
   site-packages 다. conda 가 PATH 에 있으면 venv 는 miniconda 로 만들어지고, OS 에 깔린
   cv2 는 파이썬 버전이 달라 영영 안 보인다. numpy 는 miniconda 에도 있어서 통과했다 —
   **한 짝만 맞은 것이 더 고약했다.**
2. **`apply.sh` 의 numpy·opencv 검사가 venv 를 *만들 때만* 돌았다.** 그 venv 는 이미
   있었으므로 검사가 영영 안 돌았다.
3. **못 깔아도 `warn` 이었다.** 그래서 설치는 "다 됐다" 고 말했고, 사람은 몇 주 뒤
   카메라 앞에서야 알았다.

### ⚠ 정정 — 네트워크와 **무관하지 않았다**

이 문서는 처음에 "카메라 경로에는 네트워크 코드가 없으니 오프라인은 우연" 이라고
적었다. **런타임에 대해서는 맞지만 설치에 대해서는 틀렸다.** opencv 는 PyPI 에서
받아야 하고, 밖에서 망 없이 설치·재설치가 돌면 그 한 줄이 조용히 실패한다. 증상이
카메라에서 나타났을 뿐 원인은 설치 시점의 오프라인이었다.

### 2-1. 처방

망이 있으면:

```bash
~/.venvs/piper-daemons/bin/pip install numpy opencv-python-headless
systemctl --user restart piper-camerad piper-rsd
```

**망이 없으면** 다른 기계에서 wheel 을 받아 옮긴다. ⚠ `--python-version` 은 **데몬 venv
의** 파이썬이다(OS 것이 아니다 — 위가 그 함정이다):

```bash
# 받는 기계에서 — 버전은 `~/.venvs/piper-daemons/bin/python -V` 로 확인
pip download --no-deps --only-binary=:all: \
    --python-version 313 --implementation cp --abi cp313 \
    --platform manylinux2014_x86_64 \
    opencv-python-headless 'numpy>=2,<2.3' -d whl

# 대상 기계에서
~/.venvs/piper-daemons/bin/pip install --no-index --find-links whl \
    opencv-python-headless numpy
systemctl --user restart piper-camerad piper-rsd
```

⚠ **numpy 2 를 같이 넣는다.** 요즘 opencv wheel 은 `numpy>=2` 를 요구하는데 miniconda
쪽 numpy 는 1.26 일 수 있다. 둘을 같이 넣으면 venv 의 것이 이긴다.

### 확인

```
등록 응답: connected=true  ready=true  has_preview=true  streaming=true
프리뷰    : 28384 바이트 · JPEG 640x480
```

### 고쳐진 것 (v0.5.7 뒤 — 아직 릴리스 전)

| 자리 | 전 | 후 |
|---|---|---|
| `apply.sh` numpy·opencv 검사 | venv **만들 때만** | **매번** — `--check` 에서도 본다 |
| 못 깔았을 때 | `warn` (설치는 "다 됐다") | **`bad`** — 무엇이 안 되는지 적는다 |
| 등록 실패 사유 | 세 겹으로 덮어씀 | camerad 가 한 말을 화면까지 |

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
