# syntax=docker/dockerfile:1
#
# 프론트엔드 이미지: Vite 정적 빌드 → nginx 리버스 프록시.
# 빌드 컨텍스트는 레포 루트(docker-compose 의 context: .).
# 상대경로(/api,/ws,/health)만 쓰므로 빌드타임 env 불필요, same-origin 프록시로 동작.

# ── 1) 빌드 스테이지 ──
FROM node:22-bookworm-slim AS build
WORKDIR /app
# 락파일 먼저 복사해 의존성 레이어 캐시
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build   # → /app/dist

# ── 2) 서빙 스테이지 ──
FROM nginx:alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
