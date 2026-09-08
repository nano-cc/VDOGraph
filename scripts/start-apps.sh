#!/bin/bash
# 启动全部应用服务（infra 由 scripts/dev-up.sh 负责）
set -a
source /mnt/Data/projs/Java/DOVideo-AI/.env
set +a

cd /mnt/Data/projs/Java/DOVideo-AI/ai-service
nohup python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 >> logs/ai-service.log 2>&1 &

cd /mnt/Data/projs/Java/DOVideo-AI/server
nohup ./mvnw spring-boot:run > /tmp/java-server.log 2>&1 &

cd /mnt/Data/projs/Java/DOVideo-AI/client
nohup npm run dev > /tmp/vite-dev.log 2>&1 &

echo "python/java/frontend launched"
