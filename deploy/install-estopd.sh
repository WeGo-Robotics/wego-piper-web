#!/usr/bin/env bash
# estopd 만 설치한다 — 이제 install-daemons.sh 가 넷을 다 다룬다.
# 예전 문서·메모가 이 경로를 가리키므로 껍데기로 남긴다.
exec "$(dirname "${BASH_SOURCE[0]}")/install-daemons.sh" estopd
