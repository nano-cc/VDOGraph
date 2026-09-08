#!/bin/bash
# 只重启 Java 后端
set -a
source /mnt/Data/projs/Java/DOVideo-AI/.env
set +a
cd /mnt/Data/projs/Java/DOVideo-AI/server
nohup ./mvnw spring-boot:run > /tmp/java-server.log 2>&1 &
echo "java launched"
