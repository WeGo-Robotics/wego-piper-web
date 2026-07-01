# vendor/

WeGo 내부 패키지 스냅샷. 공개 PyPI에 없어서 Docker 빌드 컨텍스트에 포함시킨다.
`backend/Dockerfile`이 여기서 오프라인으로 `pip install` 한다.

| 패키지 | 출처 | 비고 |
|--------|------|------|
| `lerobot_robot_piper` | `git@github.com:WeGo-Robotics/lerobot_robot_piper` (호스트 `~/lerobot_robot_piper`) | Piper 로봇 LeRobot 플러그인. `.git`/`build`/egg-info 제외한 소스 스냅샷 |
| `wego_piper` | 설치된 wheel(`wego_piper 0.0.2`)에서 재구성 | 순수 파이썬 서보 통신 모듈. `pyproject.toml`은 컨테이너 빌드용으로 추가 |

## 갱신 방법
소스가 바뀌면 스냅샷을 다시 떠야 한다:

```bash
# lerobot_robot_piper
rsync -a --exclude '.git' --exclude 'build' --exclude '*.egg-info' \
  --exclude '__pycache__' ~/lerobot_robot_piper/ vendor/lerobot_robot_piper/

# wego_piper (설치된 site-packages에서)
cp "$(python -c 'import wego_piper,os;print(os.path.dirname(wego_piper.__file__))')"/*.py \
   vendor/wego_piper/wego_piper/
```
