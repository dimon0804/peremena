# Peremena — a homeroom teacher's assistant bot for MAX

[![docker](https://github.com/dimon0804/peremena/actions/workflows/docker.yml/badge.svg)](https://github.com/dimon0804/peremena/actions/workflows/docker.yml)
[![image](https://img.shields.io/badge/ghcr.io-dimon0804%2Fperemena-2496ED?logo=docker&logoColor=white)](https://github.com/dimon0804/peremena/pkgs/container/peremena)

[Русский](README.md) · **English**

> Let the teacher have a break, too. («Перемена» means “school break” in Russian.)

A bot for the MAX messenger (the Sferum school profile) that collects parental consents and notes without
manual counting, answers parents' routine questions on the teacher's behalf, and keeps the teacher's materials
at hand. By our estimate this gives a homeroom teacher back about **3 h 15 min a week** — roughly
15 working days per school year.

| | |
|---|---|
| **Bot in MAX** | https://max.ru/ideya_fiks_35_bot |
| **Website and no-sign-up demo** | https://peremena.clv-digital.tech |
| **Docker image** | `docker pull ghcr.io/dimon0804/peremena:latest` |

Built for the «Идея фикс» hackathon (“Teacher's Arsenal” track) by team СИЭЛВИ-ДИДЖИТАЛ, RKSI college, Rostov-on-Don.

## Features

| Module | What it does |
|---|---|
| 📋 **Roll call** | `/poll`: pick a template (consent, medical note, acknowledgement, event sign-up, choice, money collection), write the text and a deadline. The bot takes the chat member list and sends each parent buttons in a private message; answers are tied to a person. It reminds silent parents 24 h and 2 h before the deadline, closes the poll and reports “25 of 27 answered, missing: …”, with CSV export. |
| 📖 **Class FAQ** | The bot answers only with what the teacher saved — it never makes things up. Parents ask in a private chat or right in the class chat by mentioning the bot. When the teacher replies to a question in the chat, the bot offers “Save this answer”. Unanswered questions go to the teacher; when three different parents ask the same thing, the bot suggests answering once for everyone. |
| 🗂 **Shelf** | Files forwarded with a caption and `#tags` are stored and searchable; any file can be sent to any class in one tap. |
| 🌙 **Quiet hours** | Outside the working window the bot keeps answering what it knows and brings everything new in one morning digest. Messages marked “срочно” (urgent) go through immediately. |
| 📚 **Class board** | Homework and announcements; “What's the algebra homework for tomorrow?” is answered by subject dictionary and date, with deferred morning publishing. |
| 🏛 **School** | Classes join a school by code: a shared FAQ base and school-wide polls with a per-class breakdown for the deputy head. |
| 🤒 **Absences**, 💰 **money collection**, ⏱ **usefulness counter**, 📱 **mini app** | Absence notes in one tap, money collections with paid/pending tracking, weekly stats, and a mini app / web dashboard with the same features. |

The core has no generative AI: buttons, member lists, a database and deterministic search (Snowball stemming + trigrams).

## Integrations

Class data does not stay locked in the messenger. Each integration is a separate module in `app/integrations/`
on top of the same services the bot uses, and all of them work in the demo sandbox too.

| Integration | What it does |
|---|---|
| 📆 **Calendar (iCal)** | A personal secret link `…/cal/<token>.ics` for Yandex, Google, Apple or Outlook calendars: poll deadlines with live “answered 18/24” counters and homework for teachers; polls awaiting an answer (with a 3-hour alert) and homework for parents. `/calendar` in the bot. |
| 🔌 **Open REST API v1** | API keys (`Authorization: Bearer prm_…`) for school portals, e-journals, 1C, spreadsheets: classes, polls with per-person answers, absences, homework; write-enabled keys can launch polls. Schema: `GET /api/v1/openapi.json`. 120 requests/min per key. |
| 📡 **Outgoing webhooks** | `poll.created`, `poll.answered`, `poll.closed`, `absence.created` delivered as signed JSON (`X-Peremena-Signature: sha256=HMAC(secret, timestamp + "." + body)`), with a delivery log and a “Test” button. HTTPS to public addresses only (SSRF protection), no redirects, 3 attempts. |
| 🖨 **Printable sheet and QR consent forms** | A signature sheet for the teacher's folder and tear-off consent forms with pre-filled names and a QR code that opens the poll in MAX. |
| 🗓 **“This week”** | `/week`: deadlines, homework by day, announcements and absence notes in one card. |

Tokens are generated with `secrets.token_urlsafe` and stored as SHA-256 hashes; keys can be revoked and calendar
links rotated instantly. Calendar feeds contain no other people's names.

## Quick start

### Ready-made image

The image is built and verified by GitHub Actions on every commit to `main`
([`.github/workflows/docker.yml`](.github/workflows/docker.yml)): tests on SQLite and PostgreSQL → build → smoke test of `/health` → publish.

```bash
docker pull ghcr.io/dimon0804/peremena:latest
docker run -d --name peremena -p 8080:8080 -e MAX_BOT_TOKEN=<token> \
  -v peremena-data:/srv/data ghcr.io/dimon0804/peremena:latest
curl http://localhost:8080/health
```

Each [release](https://github.com/dimon0804/peremena/releases) also carries the image as an archive:
`gunzip -c peremena-image-v1.1.0.tar.gz | docker load`.

### Build from source

```bash
cp .env.example .env              # set MAX_BOT_TOKEN
docker build -t peremena .
docker run -d --name peremena -p 8080:8080 -e MAX_BOT_TOKEN=<token> -v peremena-data:/srv/data peremena
```

Without `WEBHOOK_URL` the bot uses long polling — no HTTPS or public IP needed.

### Full stack: PostgreSQL + Redis

```bash
docker compose up -d --build
docker compose logs -f bot
```

The schema is created and migrated by Alembic on start. The service port listens on `127.0.0.1` only; put an HTTPS
proxy in front (`docker compose --profile https up -d` starts Caddy with Let's Encrypt). MAX delivers webhooks and opens
mini apps over HTTPS only.

### Scaling

Built for a whole region: thousands of schools, hundreds of thousands of parents, evening peaks.
Several instances are safe: the scheduler and long polling run in exactly one leader (Redis `SET NX PX`
lock, or a PostgreSQL advisory lock without Redis); webhooks go to a Redis Streams queue processed by all
`all`/`worker` processes; the MAX API rate limit (`MAX_RPS`, 25/s) is shared through Redis.

```bash
docker compose up -d --scale bot=3                                    # identical instances behind lb
docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --scale bot=3 --scale worker=4
```

Variables: `ROLE` (all/web/worker), `WEB_CONCURRENCY`, `EVENT_QUEUE`, `HANDLER_CONCURRENCY`, `MAX_RPS`,
`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`. Load test results: [docs/НАГРУЗКА.md](docs/НАГРУЗКА.md), scripts: [loadtest/](loadtest/).

### Command line

```bash
python -m app run                    # run the service (bot + API + mini app)
python -m app check                  # verify the token (GET /me)
python -m app subscribe              # subscribe the webhook to WEBHOOK_URL
python -m app commands               # update the bot command menu
python -m app copy-data <db url>     # copy data from another database (SQLite → PostgreSQL)
docker run --rm -e MAX_BOT_TOKEN=<token> ghcr.io/dimon0804/peremena check
```

Locally without Docker (Python 3.12): `pip install -r requirements.txt && python -m app run --port 8080`.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                                   # offline, against a fake MAX API
TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:55432/test python -m pytest -q
```

## Data and security

- No chat archive. Stored: what the teacher explicitly saved, who answered a poll, and short-lived working data
  that expires automatically (unanswered questions — 30 days, digest items — 7 days, absence notes — 14 days).
  Parents can erase their data with `/forget`.
- Mini app sign-in is verified by the MAX launch-data signature (HMAC-SHA256); webhooks by a secret header;
  website sign-in is bound to the browser that started it; rate limits on codes and PINs.
- Demo sandboxes are isolated from real schools and chats and cannot send anything to MAX.
- Security headers (CSP, nosniff), request size limits, non-root container without extra capabilities.

The full documentation — environment variables, bot commands, architecture, migrations — is in the [Russian README](README.md).

## Team

Daria Karachevtseva (captain) · Dmitry Doviryak · Nikita Zhadan · Alyona Golovina · Arina Shuklina
