#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
mkdir -p backups
[ -f data/jury.db ] && cp data/jury.db "backups/jury-$(date +%Y%m%d-%H%M%S).db" && echo "✓ Бэкап базы сохранён в backups/"
docker compose up -d --build
sleep 3
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8010/vote || true)
if [ "$code" = "200" ]; then echo "✓ Обновлено, сервис отвечает (200)"; else echo "⚠ Сервис ответил: $code — смотрите: docker logs --tail 30 jury-vote"; fi
