#!/usr/bin/env bash
# Ступени нагрузки на стенде «один VPS 2 vCPU / 4 ГБ» (docker-compose.vps.yml + loadtest/docker-compose.stress.yml).
#
#   loadtest/vps_stress.sh <метка> <классов> [<классов> …]
#   DURATION=120 FAKE_LIMIT=30 STRESS_MAX_RPS=25 WEB_CONCURRENCY=2 loadtest/vps_stress.sh base 100 300 1000 3000
#
# На каждую ступень: бот остановлен → база засеяна ровно на N классов (с открытым сбором в каждом) → Redis очищен →
# бот запущен → нагрузка (loadtest/vps_profile.py) → docker stats каждые ~2 с, лог бота, перезапуски и OOM →
# loadtest/results/vps-<метка>-<N>.json. Запускать из корня проекта.
set -euo pipefail

LABEL=${1:?метка прогона}
shift
DURATION=${DURATION:-120}
PROJECT=peremena-stress
P=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.vps.yml -f loadtest/docker-compose.stress.yml)
RAW=loadtest/results/raw/$LABEL
mkdir -p "$RAW"
chmod 777 loadtest/results "$RAW" 2>/dev/null || true
DB_URL=postgresql+asyncpg://peremena:peremena@db:5432/peremena

wait_bot() {
  for _ in $(seq 1 120); do
    if docker exec "$PROJECT-bot-1" python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" 2>/dev/null; then
      sleep 7  # балансировщик перечитывает адреса bot раз в 5 с
      return 0
    fi
    sleep 1
  done
  echo "бот не поднялся" >&2
  return 1
}

for N in "$@"; do
  echo "=== $LABEL: $N классов, $DURATION с ==="
  "${P[@]}" stop bot >/dev/null
  "${P[@]}" run --rm -T loadgen loadtest/seed.py --db "$DB_URL" --project /srv --classes "$N" --polls --reset
  "${P[@]}" exec -T redis redis-cli FLUSHALL >/dev/null
  "${P[@]}" up -d bot fakemax >/dev/null
  wait_bot
  STATS="$RAW/stats-$N.csv"
  : > "$STATS"
  (
    while true; do
      now=$(date +%s.%N)
      docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' 2>/dev/null \
        | grep "^$PROJECT-" | sed "s/^/$now,/" >> "$STATS" || true
    done
  ) &
  SAMPLER=$!
  T0=$(date +%s)
  "${P[@]}" run --rm -T loadgen loadtest/vps_profile.py step --classes "$N" --duration "$DURATION" \
    --out "loadtest/results/raw/$LABEL/step-$N.json" || echo "ступень $N завершилась с ошибкой" >&2
  T1=$(date +%s)
  kill "$SAMPLER" 2>/dev/null || true
  wait "$SAMPLER" 2>/dev/null || true
  docker logs --since "$T0" "$PROJECT-bot-1" > "$RAW/bot-$N.log" 2>&1 || true
  INSPECT=$(for c in bot db redis lb; do
    docker inspect -f "{\"name\":\"$c\",\"restarts\":{{.RestartCount}},\"oom_killed\":{{.State.OOMKilled}},\"status\":\"{{.State.Status}}\"}" "$PROJECT-$c-1"
  done | paste -sd, - | sed 's/^/[/; s/$/]/')
  CONFIG=$(docker exec "$PROJECT-bot-1" python -c     "import json,os; print(json.dumps({k: os.environ.get(k) for k in ('WEB_CONCURRENCY','HANDLER_CONCURRENCY','DB_POOL_SIZE','DB_MAX_OVERFLOW','MAX_RPS','ACCESS_LOG','EVENT_STREAM_MAXLEN')}))")
  LIMITS=$(for c in bot db redis lb; do
    docker inspect -f "\"$c\": {\"cpus\": {{.HostConfig.NanoCpus}}, \"memory\": {{.HostConfig.Memory}}}" "$PROJECT-$c-1"
  done | paste -sd, -)
  CONFIG=$(echo "$CONFIG" | sed "s/}\$/, \"FAKE_LIMIT\": \"${FAKE_LIMIT:-30}\", \"DURATION\": \"$DURATION\", \"limits\": {$LIMITS}}/")
  "${P[@]}" run --rm -T loadgen loadtest/vps_profile.py merge --step "loadtest/results/raw/$LABEL/step-$N.json" \
    --stats "$STATS" --logs "$RAW/bot-$N.log" --t0 "$T0" --t1 "$T1" --inspect "$INSPECT" --config "$CONFIG" \
    --label "$LABEL" --out "loadtest/results/vps-$LABEL-$N.json"
done
