# Нагрузочное тестирование «Перемены»

Всё локально: поддельный MAX API, PostgreSQL и Redis в Docker, экземпляры бота — обычные процессы.
Настоящий токен и прод не нужны и не трогаются. Новых зависимостей нет — хватает `requirements.txt`.

| Файл | Что делает |
|---|---|
| `fake_max.py` | поддельный MAX Bot API: отвечает на всё, что вызывает бот, считает вызовы, держит лимит запросов/с (сверх — 429), отдаёт `/_stats`, `/_sent` |
| `roster.py` | состав классов по формуле (учитель, 30 родителей, бот) — общий для засева, поддельного MAX и генератора |
| `seed.py` | засев базы: школы, классы, учителя, родители, открытые сборы (пакетные INSERT, схема — миграциями) |
| `run_all.py` | весь прогон: поднимает поддельный MAX и N экземпляров бота, гоняет сценарии, сверяет результат в базе, пишет JSON |
| `httpload.py` | лёгкий keep-alive HTTP-клиент генератора (httpx сам съедал бы процессор на тысячах запросов в секунду) |
| `profile_handlers.py` | профиль обработки событий в одном процессе: событий/с, мс процессора и SQL-запросов на событие |
| `bench_search.py` | микробенчмарк поиска по справочнику |
| `eval_semantic.py` | качество поиска по справочнику с эмбеддингами и без (54 вопроса, перебор порогов) |

| `vps_profile.py` | ступень «вечерний пик N классов» против поднятого стенда: вебхуки, «Мои сборы», сбор на параллель, сверка, метрики очереди с `/metrics` |
| `vps_stress.sh` | ступени на стенде профиля VPS: засев → нагрузка → `docker stats`, лог бота, OOM → `results/vps-<метка>-<N>.json` |
| `docker-compose.stress.yml` | стенд поверх `docker-compose.vps.yml`: поддельный MAX и генератор вне лимитов VPS |
| `results/` | JSON последних прогонов, по ним составлен `docs/НАГРУЗКА.md` |

## Подготовка

```bash
docker run -d --name pm-load-pg -e POSTGRES_USER=pm -e POSTGRES_PASSWORD=pm -e POSTGRES_DB=pm \
  -p 127.0.0.1:55433:5432 --memory=768m postgres:16-alpine -c max_connections=300
docker run -d --name pm-load-redis -p 127.0.0.1:56380:6379 --memory=128m redis:7-alpine
pip install -r requirements.txt
```

## Сценарии

```bash
# всё сразу, 1 экземпляр
python loadtest/run_all.py --instances 1 --out loadtest/results/after-1.json
# 3 одинаковых экземпляра (ROLE=all) — как docker compose up --scale bot=3
python loadtest/run_all.py --instances 3 --out loadtest/results/after-3.json
# прод-схема: 2 приёмника (ROLE=web) + 3 обработчика (ROLE=worker)
python loadtest/run_all.py --instances 2 --workers 3 --out loadtest/results/after-2w3.json
# старая версия для сравнения: --project <папка с выгрузкой старого коммита>
```

- **(а) сбор на параллель** — завуч через API запускает сбор в 10 классах (300 родителей), затем 300 родителей
  нажимают кнопку за 10 с (+5 % повторных доставок). Поддельный MAX пропускает не больше 30 запросов/с
  (`--max-limit`), у бота `MAX_RPS=25`. Сверка: каждому родителю ровно одно сообщение, 300 ответов в базе,
  каждый callback отвечен ровно раз, учителям — «Ответили все», ноль 429.
- **(б) пик вебхуков** — 1700 классов / 51 000 пользователей; `--rates 500,1000,2000` событий/с по `--duration` с:
  ~82 % сообщений в чатах (10 % из них — вопросы), 15 % нажатий кнопок сбора, 3 % повторных доставок
  в другой экземпляр. Сверка: каждый уникальный callback отвечен ровно раз, ответы в базе, каждый вопрос
  записан в «частые вопросы» ровно раз. Чтобы мерить само приложение, а не лимит MAX, здесь лимит снят
  (`--b-max-rps`).
- **(в) «Мои сборы»** — 200 родителей одновременно и непрерывно открывают `/api/my/polls`
  с настоящей подписью данных запуска MAX.

Метрики: RPS, p50/p95/p99 задержки ответа, коды ответов, время полной рассылки и обработки, пиковый RPS к MAX,
число 429, ошибки и таймауты пула в логах ботов (логи — рядом с JSON, в `*-logs/`).

## Стенд «один VPS 2 vCPU / 4 ГБ»

Бот, PostgreSQL, Redis и балансировщик — в лимитах `docker-compose.vps.yml`, поддельный MAX (30 запросов/с)
и генератор — отдельными контейнерами вне лимитов. Отдельный проект compose, порт только на 127.0.0.1:

```bash
P="docker compose -p peremena-stress -f docker-compose.yml -f docker-compose.vps.yml -f loadtest/docker-compose.stress.yml"
export PEREMENA_TAG=stress BOT_PORT=18090
$P up -d --build
DURATION=120 loadtest/vps_stress.sh final 100 300 1000 1200 1500 3000      # с лимитом MAX, как в жизни
FAKE_LIMIT=0 STRESS_MAX_RPS=5000 loadtest/vps_stress.sh nolimit 1500        # потолок без лимита MAX
BOT_CPUS=1.2 DB_CPUS=0.5 REDIS_CPUS=0.15 LB_CPUS=0.15 loadtest/vps_stress.sh real 1000   # другое деление CPU
$P down -v
```

Методика и результаты — `docs/НАГРУЗКА.md`, раздел «Один VPS 2 vCPU / 4 ГБ».

## Уборка

```bash
docker rm -f pm-load-pg pm-load-redis
```
