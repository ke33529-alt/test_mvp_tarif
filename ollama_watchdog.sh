#!/bin/sh
# =============================================================================
# ollama_watchdog.sh — сторожевой скрипт для зависаний Ollama runner'а
# =============================================================================
# ПОЧЕМУ ЭТОГО НЕДОСТАТОЧНО через healthcheck на /api/tags:
# /api/tags отвечает от Ollama-демона мгновенно ДАЖЕ КОГДА конкретный
# runner модели завис (демон жив, runner — нет). Известный баг Ollama:
# runner застревает на inference-запросе, ест 100% CPU или простаивает
# без GPU-нагрузки, и не отвечает на новые запросы, пока контейнер не
# перезапустят. Обычный healthcheck это не ловит — see github.com/ollama/ollama
# issues #9382, #7766, #8200 (зависания разных версий, без единого фикса).
#
# ЧТО ДЕЛАЕТ ЭТОТ СКРИПТ:
# Раз в CHECK_INTERVAL секунд дёргает /api/generate с коротким промптом
# и жёстким таймаутом. Если запрос не укладывается в TIMEOUT секунд —
# считает это зависанием и рестартует контейнер ollama через Docker socket.
# Логирует каждую проверку для последующей диагностики через docker logs.
# =============================================================================

set -u

OLLAMA_URL="${OLLAMA_URL:-http://ollama:11434}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
CHECK_MODEL="${CHECK_MODEL:-qwen3.5:9b}"
CHECK_INTERVAL="${CHECK_INTERVAL:-60}"       # сек между проверками
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-90}"     # сек на один health-запрос
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"        # подряд неудач до рестарта
STARTUP_GRACE="${STARTUP_GRACE:-120}"        # сек после старта скрипта, когда
                                              # неудачи не считаются — холодная
                                              # загрузка модели не должна
                                              # триггерить ложный рестарт

fail_count=0
watchdog_started_at=$(date +%s)

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] $1"
}

log "Запущен. URL=$OLLAMA_URL модель=$CHECK_MODEL интервал=${CHECK_INTERVAL}с таймаут=${REQUEST_TIMEOUT}с порог=$FAIL_THRESHOLD grace=${STARTUP_GRACE}с"

while true; do
    sleep "$CHECK_INTERVAL"

    now_ts=$(date +%s)
    in_grace_period=0
    if [ $((now_ts - watchdog_started_at)) -lt "$STARTUP_GRACE" ]; then
        in_grace_period=1
    fi

    start_ts=$(date +%s)
    http_code=$(curl -s -o /tmp/watchdog_resp.json -w "%{http_code}" \
        -m "$REQUEST_TIMEOUT" \
        -X POST "$OLLAMA_URL/api/generate" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$CHECK_MODEL\",\"prompt\":\"ping\",\"stream\":false,\"options\":{\"num_predict\":5}}" \
        2>/tmp/watchdog_err.log)
    curl_exit=$?
    elapsed=$(( $(date +%s) - start_ts ))

    if [ "$curl_exit" -ne 0 ] || [ "$http_code" != "200" ]; then
        if [ "$in_grace_period" -eq 1 ]; then
            log "НЕУДАЧА в период прогрева ($((now_ts - watchdog_started_at))с из ${STARTUP_GRACE}с) — " \
                "не считается (curl_exit=$curl_exit http_code=$http_code elapsed=${elapsed}с)"
        else
            fail_count=$((fail_count + 1))
            log "НЕУДАЧА #$fail_count (curl_exit=$curl_exit http_code=$http_code elapsed=${elapsed}с): $(cat /tmp/watchdog_err.log 2>/dev/null | head -c 200)"
        fi
    else
        if [ "$fail_count" -gt 0 ]; then
            log "Восстановилось после $fail_count неудач(и) — сбрасываю счётчик"
        fi
        fail_count=0
        log "OK (elapsed=${elapsed}с)"
    fi

    if [ "$fail_count" -ge "$FAIL_THRESHOLD" ]; then
        log "Порог достигнут ($fail_count/$FAIL_THRESHOLD) — рестартую контейнер $OLLAMA_CONTAINER"
        curl -s -m 30 -X POST \
            --unix-socket /var/run/docker.sock \
            "http://localhost/containers/$OLLAMA_CONTAINER/restart?t=10" \
            -o /tmp/watchdog_restart.json
        restart_exit=$?
        if [ "$restart_exit" -eq 0 ]; then
            log "Рестарт запрошен успешно. Ответ Docker API: $(cat /tmp/watchdog_restart.json 2>/dev/null | head -c 200)"
        else
            log "ОШИБКА рестарта (curl_exit=$restart_exit) — проверьте монтирование /var/run/docker.sock"
        fi
        fail_count=0
        watchdog_started_at=$(date +%s)
        log "Пауза 90с на прогрев модели после рестарта перед следующей проверкой"
        sleep 90
    fi
done