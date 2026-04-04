#!/usr/bin/env bash
# 백엔드 + 프론트엔드 동시 실행
set -e

trap 'kill 0' EXIT

echo "Starting backend (FastAPI)..."
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 &

echo "Starting frontend (Vite)..."
cd ../frontend
npm run dev &

wait
