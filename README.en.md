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
| 🏛 **Education authority (region / district)** | `/region`: a school joins a district or region by code; methodologists join with a staff code or are appointed by a platform admin (`ADMIN_MAX_IDS`). A poll launched from above for the whole region, selected schools or grades is split into school-level and class-level polls; the homeroom teacher sees where it came from and may add a note, but cannot change the text. Delivery uses the regular poll dispatch within MAX rate limits. A bottom-up summary (“82 % answered, school No. 5 — 40 %”) with CSV export shows numbers only — no parents' names. |
| 📚 **Template library** | `/library`: shared poll templates and FAQ cards, searchable by topic, type and grade, sorted by popularity, with a “taken N times” counter. One tap to take a template. Publishing is anonymous and checked for phone numbers, e-mails and children's names; reports and moderation by a platform admin; 20 starter templates from the team. Also in the mini app and `GET /api/v1/library`. |

| 🤝 **Meeting sign-up** | `/meet`: the teacher sets time windows for parent consultations (in person or by phone); parents pick a free slot with a button, can cancel or reschedule, and get a reminder 2 hours before. Double booking is prevented by the database. Meetings show up in the teacher's schedule, morning digest and the iCal feed. |
| 📈 **Pilot metrics** | Median time to answer a parent (bot or teacher, 7 and 30 days, p90, share answered by the bot, unanswered counted separately) and weekly-cohort teacher retention (D7, D30). Teachers see their answer time in `/stats`; the deputy head sees the school and platform admins (`ADMIN_MAX_IDS`) see everything via `/metrics` and `/api/admin/metrics`. Until a real pilot, numbers exist only on clearly labelled demo history. |
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
| 📒 **School e-journal** | `/journal`: upload a CSV/XLSX export → homework on the board, schedule in bot answers, class list checked against the chat; absences and poll results back as CSV. Details below. |
| 🗓 **“This week”** | `/week`: deadlines, homework by day, announcements and absence notes in one card. |

Tokens are generated with `secrets.token_urlsafe` and stored as SHA-256 hashes; keys can be revoked and calendar
links rotated instantly. Calendar feeds contain no other people's names.

### School e-journals

Russian e-journals (“Сетевой город”, ЭлЖур, ЭЖД/МЭШ, regional “Электронная школа”, ФГИС «Моя школа») have no open
API for third-party bots, but almost all of them export to Excel or CSV. So the main path is a file
(`app/integrations/journals/`):

- **Import.** The teacher sends the exported table to the bot with the caption “журнал” (or `/journal` → “Upload”),
  or uploads it in the mini app. The bot first shows what it found (“27 students, 34 assignments, schedule for
  5 days”) and writes only after the teacher confirms. Homework goes to the class board silently (no chat
  message) with subject and date; re-importing the same file creates no duplicates, changed assignments are
  updated, hand-written entries are left alone. The schedule powers answers to “what lessons are tomorrow?” and
  “what to bring on Wednesday?”. The class list is matched against chat members, so the teacher sees who is not
  in the chat yet. Only “Surname Name” of a student and “Surname I.” of parents are kept; patronymics, birth dates,
  phones and e-mails are dropped while parsing.
- **Format detection** by column headers (several known variants per column, title rows above the header are
  skipped, every XLSX sheet is parsed, a weekday grid schedule is supported) plus a downloadable template
  (`/api/journals/template.xlsx`). The header variants were collected from typical exports and have **not been
  checked against real files** from every journal.
- **Safe parsing:** no `eval`, 3 MB / 5,000 rows limit, openpyxl in `read_only` + `data_only` mode (formulas are not
  evaluated), zip size checked up front, defusedxml for XML.
- **Export back:** absence notes and poll results as CSV (UTF-8 with BOM, `;` separator, formula-like cells are
  prefixed with an apostrophe) — from the bot, the mini app (signed links valid for 24 h) and the open API
  (`GET /api/v1/export/{absences|polls}.csv`, `GET /api/v1/schedule`).
- **Dnevnik.ru connector** over its OAuth REST API (`DNEVNIK_ENABLED=true`, per-teacher token stored encrypted
  or a school token in `DNEVNIK_TOKEN`). Endpoints are taken from public descriptions; **not tested against the
  live API** — tests run on a mock server. Off by default.

Only the class teacher of a class can import or export; parents and other teachers are refused.

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

### Semantic search (optional)

The FAQ core is word-based search: Russian stemming, trigrams and a dictionary of school slang. It is predictable
but misses paraphrases: "when do I pick my kid up from after-school care?" and a card "Until what time is the
extended-day group open?" share no words, so the question goes to the teacher. For that case there is an option:
[cointegrated/rubert-tiny2](https://huggingface.co/cointegrated/rubert-tiny2) embeddings (312 numbers per
phrase) stored in pgvector. **It is off by default** (`EMBEDDINGS=off`), and then the bot behaves exactly as
without it.

What stays the same when it is on:

- the bot answers only with the text the teacher saved. Nothing is generated; the model only helps find the card;
- semantics can raise a card in the ranking but never lower it: whatever was found by words is still found;
- a confident answer "by meaning" needs a high cosine and a clear gap to the second card. With two similar cards
  ("PE uniform" vs "field trip clothes") the bot offers "did you mean…" instead of guessing; when unsure, the
  question goes to the teacher as before;
- in the class group chat the bot never answers by meaning — an almost verbatim question is still required;
- only cards of the asker's classes and their school's shared base are searched;
- if the model or the database fails, the bot silently falls back to word search.

```bash
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml exec bot python -m app embeddings reindex
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml exec bot python -m app embeddings status
```

The override builds the image with `--build-arg WITH_EMBEDDINGS=1`, sets `EMBEDDINGS=on` and switches the
database to `pgvector/pgvector:pg16` (if the volume already holds a database from `postgres:16-alpine`, run
`REINDEX DATABASE` afterwards — see the file's comments). At build time the model is downloaded from Hugging Face
at a pinned commit and converted to ONNX (`scripts/export_rubert_onnx.py`, which also checks the ONNX output
against the original model). torch is used only in that build stage; the image gets onnxruntime, tokenizers,
numpy (`requirements-ml.txt`) and the ~120 MB model. The default image (`WITH_EMBEDDINGS=0`) is unchanged.
`EMBEDDINGS_BACKEND=st` uses sentence-transformers instead (pulls in torch).

How it works (`app/services/semantic.py`): a card's vector is computed in the background, in batches, right after
it is saved or edited; the leader catches up on anything missed every 5 minutes (`python -m app embeddings
reindex [--all]` does it by hand). With pgvector the vector lives in a `vector(312)` column with an HNSW
(cosine) index and the database computes the cosine; without the extension (plain PostgreSQL or SQLite) it is
stored as bytes and the bot computes the cosine over the class's cards. Migration `0010` adds the vector column
only if the extension is available. The final score is the maximum of the word score and the cosine score on
the same scale, with the same FAQ thresholds; the cosine score always stays below the group-chat threshold.

Settings: `EMBEDDINGS` (`off`), `EMBEDDINGS_BACKEND` (`auto`/`onnx`/`st`), `EMBEDDINGS_MODEL_DIR`
(`/srv/models/rubert-tiny2`), `EMBEDDINGS_THREADS` (`1`), `EMBEDDINGS_ANSWER_COS` (`0.80`),
`EMBEDDINGS_SUGGEST_COS` (`0.65`), `EMBEDDINGS_MARGIN` (`0.05`).

**Quality has not been measured on the real model yet.** The thresholds are cautious starting values.
`loadtest/eval_semantic.py` has 15 FAQ cards and 54 hand-written parent questions (46 answerable, many of them
paraphrased; 8 not covered by the FAQ); it compares off vs on and sweeps the thresholds. Without embeddings the bot
gives a correct confident answer to 6 of the 46 and no wrong confident answers; numbers with the model will be added
after a server run (the script needs only the model, not the database; `loadtest` is not in the image, so it is
mounted from the host):

```bash
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml run --rm --no-deps \
  --entrypoint python -v "$PWD/loadtest:/srv/loadtest:ro" bot \
  loadtest/eval_semantic.py --sweep --verbose > loadtest/results/semantic_eval.txt
```

Single 2 vCPU / 4 GB VPS: `docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build` —
CPU and memory limits for the bot, PostgreSQL, Redis and the balancer plus tuned settings. Measured on a stand with
these limits: an evening peak of 100 classes takes a quarter of a core and 345 MiB; the ceiling is about
1000 classes without queue growth.

An event whose processing fails `EVENT_MAX_ATTEMPTS` times (5) goes to the dead-letter stream `pm:events:dead`
with the reason instead of looping forever: `python -m app events` shows the queue, `--replay-dead` puts the events
back. `/metrics` exposes Prometheus metrics for the whole installation (events received/processed/failed/dead,
handling time, queue length and lag, MAX API requests and 429s, DB pool) — open to private addresses, from outside
only with `Authorization: Bearer <METRICS_TOKEN>`. `/health/ready` checks the database, Redis, queue consumers
and the queue backlog.

### Command line

```bash
python -m app run                    # run the service (bot + API + mini app)
python -m app check                  # verify the token (GET /me)
python -m app subscribe              # subscribe the webhook to WEBHOOK_URL
python -m app commands               # update the bot command menu
python -m app copy-data <db url>     # copy data from another database (SQLite → PostgreSQL)
python -m app embeddings reindex     # compute FAQ card vectors (EMBEDDINGS=on; --all recomputes everything)
python -m app embeddings status      # how many cards have vectors, is pgvector available

python -m app events [--replay-dead] # event queue status, replay the dead-letter stream
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
  that expires automatically (unanswered questions — 30 days, digest items — 7 days, absence notes — 14 days,
  answer-time records without any text — 180 days, teacher active days — 400 days, meeting sign-ups — 30 days
  after the last slot).
  Parents can erase their data with `/forget`.
- Mini app sign-in is verified by the MAX launch-data signature (HMAC-SHA256); webhooks by a secret header;
  website sign-in is bound to the browser that started it; rate limits on codes and PINs.
- Demo sandboxes are isolated from real schools and chats and cannot send anything to MAX.
- A regional methodologist sees aggregates only, never parents' names or answers.
- Security headers (CSP, nosniff), request size limits, non-root container without extra capabilities.

The full documentation — environment variables, bot commands, architecture, migrations — is in the [Russian README](README.md).

## Team

Daria Karachevtseva (captain) · Dmitry Doviryak · Nikita Zhadan · Alyona Golovina · Arina Shuklina
