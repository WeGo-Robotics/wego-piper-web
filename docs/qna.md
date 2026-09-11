# 자주 묻는 것 (QnA)

설치·운영에서 **실제로 나온 질문**과 답. 답이 스크립트·문서와 어긋나면 안 되므로
`backend/tests/test_install_docs.py` 가 핵심 문구를 잠근다. 새 질문은 아래에 덧붙인다 —
질문은 **나온 말 그대로** 적는다. 그래야 다음 사람이 같은 말로 찾는다.

## 설치하고 나서 브라우저로 어디에 접속하나?

**`http://<이 기계의 IP>/`** — 같은 기계라면 `http://localhost/`. 포트는 80 이고, `apply.sh` 가
**마지막 줄**에 실제 주소를 찍어 준다(포트를 옮겼으면 그 포트로). IP 는 `hostname -I` 로 본다.

## 80 이 아닌 다른 포트를 쓰고 싶으면?

```bash
PIPER_WEB_PORT=8081 ./piper-install.sh     # 한 번만
./piper-install.sh                          # 이후 업데이트 — 8081 그대로
```

한 번 주면 `apply.sh` 가 `~/piper-web-deploy/current/.env` 에 적어 두어 업데이트에도 유지된다.
바뀌는 것은 **바깥** 포트뿐이다 — 안쪽 :80 은 컨테이너 안 nginx 가 듣는 포트다.

예전 방식(`docker-compose.override.yml` 에 `ports: !override`)도 그대로 동작하고, 있으면
그쪽이 이긴다 — `!override` 는 포트 목록을 통째로 치환하기 때문이다. 그 태그를 빼먹으면
80 과 새 포트를 **둘 다** 잡으려다 조용히 실패한다. 그래서 환경변수 쪽을 권한다.

## 설치하고 나서 포트를 바꾸고 싶으면?

```bash
cd ~/piper-web-deploy/current
sed -i '/^PIPER_WEB_PORT=/d' .env; echo 'PIPER_WEB_PORT=9000' >> .env
docker compose up -d
```

compose 가 설정 차이를 보고 **frontend 컨테이너만** 다시 만든다(몇 초). 백엔드·데몬은
그대로고, 다음 업데이트에도 `.env` 값이 남는다. 새 주소는 `http://<IP>:9000/`.

⚠ **녹화·추론 중엔 하지 않는다.** 브라우저의 E-stop heartbeat 가 frontend(nginx)를 거쳐
가는데, 컨테이너를 다시 만드는 몇 초가 2초 타임아웃을 넘겨 estopd 가 그 활동을 죽인다.

`PIPER_WEB_PORT=9000 ./piper-install.sh <지금 깔린 버전>` 으로도 되지만 무겁다 — 버전을 안
붙이면 최신으로 올라가는 부작용이 있고, `apply.sh` 는 데몬 다섯을 전부 재시작한다.
포트 하나 바꾸는 일에는 과하다.

## GPU 없는 기계에 설치하면?

된다 — 학습·추론만 못 돌고 수집·시뮬레이터·조종 창은 된다. `apply.sh` 가 nvidia 가 없으면
GPU 예약 없는 compose 조합을 배포 디렉토리 `.env` 의 `COMPOSE_FILE` 에 적는다
(`docker-compose.yml:docker-compose.nogpu.yml`, override 파일이 있으면 그 뒤에). GPU 가 있는
기계에선 그 키를 지워 예전 그대로다. (v0.4.13 이하 번들은 경고만 하고 넘겨서 4절
`docker compose up` 이 `could not select device driver "nvidia"` 로 죽었다 — NUC 에서 실제로.)

⚠ 조각이 `!reset` 을 써서 **compose 2.24 이상**이어야 한다 — 낮으면 스크립트가 멈추고
올리라고 한다(Ubuntu 아카이브는 `docker-compose-v2`, docker.com 저장소면 `docker-compose-plugin`).

⚠ GPU 없는 기계에서 **나중에** `docker-compose.override.yml` 을 만들면 `.env` 의
`COMPOSE_FILE` 끝에 `:docker-compose.override.yml` 을 붙이거나 `apply.sh` 를 다시 돌린다 —
`COMPOSE_FILE` 을 쓰는 동안은 compose 의 기본 탐색(override 자동 포함)이 꺼져 있다.

## 지우고 싶으면?

`./piper-uninstall.sh` — 설치 스크립트와 같은 자리에서 받는다(README 의 "제거"). 유닛·
컨테이너·이미지·venv·`~/piper-web-deploy` 를 되돌리고 **데이터는 남긴다**(`/srv/piper-data`,
`~/.cache/huggingface/lerobot`, `~/.config/piper-web`). 지우려면 `--purge-data` 를 명시한다.
`--dry-run` 이 무엇을 지울지 먼저 보여 준다. `--keep-images` 는 이미지를 둔다(다시 깔 때
3.5GB 를 안 받게).

sudo 는 직접 안 쓴다 — udev 규칙처럼 root 로 놓은 것은 명령을 찍어 준다. 이 호스트의
`docker-compose.override.yml` 은 `~/override.keep.yml` 로 빼 두므로 다시 깔면 apply.sh 가
되돌린다. `PIPER_WEB_PORT` 를 썼다면 값을 찍어 주니 다시 깔 때 앞에 붙인다.
