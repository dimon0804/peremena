"""Нагрузочный прогон «Перемены» целиком: поддельный MAX, засев базы, N экземпляров бота, сценарии, сверка.

    python loadtest/run_all.py --instances 3 --scenarios a,b,c --rates 500,1000,2000 --out loadtest/results/after-3.json

Сценарии:
  a — сбор на параллель: 10 классов × 30 родителей, рассылка через API завуча и 300 нажатий кнопок;
      MAX ограничен 30 запросами в секунду (как настоящий), бот — своим лимитом MAX_RPS=25.
  b — пик вебхуков: сообщения в чатах классов и нажатия кнопок от ~50 000 пользователей, RATE событий/с,
      ~3 % повторных доставок (как делает MAX без 200) — в разные экземпляры.
  c — мини-приложение: 200 родителей одновременно открывают «Мои сборы» (подпись данных запуска MAX).

После каждого сценария — сверка в базе и в поддельном MAX: всё обработано ровно один раз, дублей и потерь нет.
Нужны PostgreSQL и Redis (см. loadtest/README.md); новых зависимостей нет — только пакеты бота.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import roster  # noqa: E402
from httpload import Pool, get_json, percentiles, post_json  # noqa: E402

TOKEN = "load-test-token"
WEBHOOK_SECRET = hashlib.sha256(("wh:" + TOKEN).encode()).hexdigest()[:40]
FAKE_PORT = 59100
BOT_PORT0 = 58100
QUESTIONS = ["Во сколько завтра родительское собрание?", "Когда сдавать деньги на экскурсию?",
             "Где взять расписание звонков на четверг?", "Какую форму надеть на физкультуру?",
             "Когда будет олимпиада по математике?", "Сколько стоит обед в столовой?"]
CHATTER = ["Спасибо!", "Хорошо", "Ок", "Принято, спасибо", "👍", "Доброе утро всем", "Поняла", "Сделаем"]


# ---------------------------------------------------------------- процессы
class Stack:
    def __init__(self, a, logdir: Path) -> None:
        self.a = a
        self.logdir = logdir
        self.procs: list[subprocess.Popen] = []
        self.bots: list[tuple[str, int]] = []
        self.workers: list[tuple[str, int]] = []

    def _spawn(self, args, env, log_name, cwd):
        log = open(self.logdir / log_name, "w", encoding="utf-8")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        p = subprocess.Popen(args, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        self.procs.append(p)
        return p

    def start_fake(self, limit: float) -> None:
        self._spawn([sys.executable, str(HERE / "fake_max.py"), "--port", str(FAKE_PORT), "--limit", str(limit),
                     "--latency", str(self.a.max_latency)], dict(os.environ), "fake_max.log", str(HERE))

    def bot_env(self, max_rps: float) -> dict:
        env = dict(os.environ)
        env.update({
            "MAX_BOT_TOKEN": TOKEN, "MAX_API_URL": f"http://127.0.0.1:{FAKE_PORT}", "UPDATES_MODE": "webhook",
            "WEBHOOK_URL": "", "PUBLIC_URL": "", "DATABASE_URL": self.a.db, "REDIS_URL": self.a.redis,
            "DEV_AUTH": "true", "BOT_USERNAME": "peremena_load_bot", "LOG_LEVEL": "WARNING",
            "MAX_RPS": str(max_rps), "DB_POOL_SIZE": str(self.a.pool_size), "DB_MAX_OVERFLOW": str(self.a.max_overflow),
            "HANDLER_CONCURRENCY": str(self.a.handler_concurrency), "DEMO_ENABLED": "false",
            "PYTHONIOENCODING": "utf-8", "SECRET_KEY": "load-secret", "EVENT_QUEUE": self.a.event_queue,
            "DB_PRE_PING": "false",
        })
        return env

    def start_bots(self, n: int, max_rps: float, tag: str) -> None:
        """n экземпляров принимают HTTP; при --workers M они ROLE=web, и ещё M процессов ROLE=worker
        (только очередь событий и планировщик) — прод-схема docker-compose.scale.yml."""
        env = self.bot_env(max_rps)
        self.bots, self.workers = [], []
        roles = [("web" if self.a.workers else "all")] * n + ["worker"] * self.a.workers
        for i, role in enumerate(roles):
            port = BOT_PORT0 + i
            self._spawn([sys.executable, "-m", "app", "run", "--port", str(port), "--host", "127.0.0.1"],
                        {**env, "ROLE": role}, f"bot{i}-{tag}.log", self.a.project)
            (self.workers if role == "worker" else self.bots).append(("127.0.0.1", port))

    async def wait_ready(self, timeout: float = 90) -> None:
        deadline = time.time() + timeout
        targets = [("127.0.0.1", FAKE_PORT, "/_stats")] + [(h, p, "/health") for h, p in self.bots + self.workers]
        for h, p, path in targets:
            while True:
                try:
                    if await get_json(h, p, path) is not None:
                        break
                except Exception:
                    pass
                if time.time() > deadline:
                    raise RuntimeError(f"{h}:{p} не поднялся — смотрите логи в {self.logdir}")
                await asyncio.sleep(0.5)

    async def leaders(self) -> dict:
        """Сколько экземпляров считают себя лидером (должен быть ровно один) — по /health в режиме разработки."""
        out = {"leaders": 0, "queue": None}
        for h, p in self.bots + self.workers:
            try:
                st = await get_json(h, p, "/health")
            except Exception:
                continue
            out["leaders"] += bool(st and st.get("leader"))
            out["queue"] = (st or {}).get("queue")
        return out

    def stop(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    if os.name == "nt":
                        p.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        p.terminate()
                except Exception:
                    pass
        for p in self.procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = []


def seed(a, classes: int, polls: bool) -> str:
    args = [sys.executable, str(HERE / "seed.py"), "--db", a.db, "--project", a.project, "--classes", str(classes),
            "--reset"] + (["--polls"] if polls else [])
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode:
        raise RuntimeError(out.stdout + out.stderr)
    return out.stdout.strip()


async def flush_redis(url: str) -> None:
    if not url:
        return
    import redis.asyncio as redis

    r = redis.from_url(url)
    await r.flushdb()
    await r.aclose()


async def pg(a):
    import asyncpg

    return await asyncpg.connect(a.db.replace("postgresql+asyncpg://", "postgresql://"))


def log_errors(logdir: Path, tag: str) -> dict:
    out = {}
    for f in sorted(logdir.glob(f"bot*-{tag}.log")):
        text = f.read_text(encoding="utf-8", errors="replace")
        out[f.stem] = {"errors": text.count(" ERROR "), "tracebacks": text.count("Traceback"),
                       "pool_timeouts": text.count("QueuePool limit"), "queue_errors": text.count("Очередь событий недоступна")}
    return out


# ---------------------------------------------------------------- данные запуска мини-приложения
def init_data(uid: int) -> str:
    pairs = {"auth_date": str(int(time.time())), "query_id": f"q{uid}",
             "user": json.dumps({"id": uid, "first_name": "Родитель", "last_name": roster.parent_by_id(uid)["last_name"]},
                                ensure_ascii=False)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def callback(uid: int, payload: str, cid: str, mid: str = "m") -> dict:
    return {"update_type": "message_callback", "timestamp": int(time.time() * 1000),
            "callback": {"timestamp": int(time.time() * 1000), "callback_id": cid, "payload": payload,
                         "user": roster.parent_by_id(uid)},
            "message": {"recipient": {"chat_type": "dialog", "chat_id": uid}, "body": {"mid": mid, "seq": 1},
                        "timestamp": 1}}


def chat_message(uid: int, chat: int, text: str, mid: str) -> dict:
    return {"update_type": "message_created", "timestamp": int(time.time() * 1000),
            "message": {"sender": roster.parent_by_id(uid),
                        "recipient": {"chat_type": "chat", "chat_id": chat},
                        "timestamp": int(time.time() * 1000), "body": {"mid": mid, "seq": 1, "text": text}}}


WH = {"Content-Type": "application/json", "X-Max-Bot-Api-Secret": WEBHOOK_SECRET}


async def wait_for(check, timeout: float, every: float = 0.5):
    t = time.time()
    while True:
        ok, val = await check()
        if ok or time.time() - t > timeout:
            return ok, val, time.time() - t
        await asyncio.sleep(every)


# ---------------------------------------------------------------- сценарий (а): сбор на параллель
async def scenario_a(a, stack: Stack, tag: str) -> dict:
    print(f"[{tag}] (а) засев: {seed(a, roster.CLASSES_PER_SCHOOL, False)}", flush=True)
    await flush_redis(a.redis)
    stack.start_fake(limit=a.max_limit)
    stack.start_bots(a.instances, a.max_rps, tag)
    await stack.wait_ready()
    await asyncio.sleep(1)
    leaders = await stack.leaders()
    await post_json("127.0.0.1", FAKE_PORT, "/_reset")
    body = {"chat_ids": [roster.chat_id(i) for i in range(roster.CLASSES_PER_SCHOOL)], "template": "consent",
            "text": "Экскурсия в музей в субботу\nНужно согласие до пятницы", "deadline": "week"}
    h, p = stack.bots[0]
    t0 = time.time()
    status, resp = await post_json(h, p, f"/api/school/campaigns?dev_user={roster.admin_id(0)}", body, timeout=600)
    t_dispatch = time.time() - t0
    assert status == 200, (status, resp)
    sent = await get_json("127.0.0.1", FAKE_PORT, "/_sent")
    parents = {roster.parent_id(i, j) for i in range(roster.CLASSES_PER_SCHOOL) for j in range(roster.PARENTS)}
    poll_msgs: dict[int, list[str]] = {}
    for m in sent:
        if m["user_id"] in parents and any(x.startswith("pa:") for x in m["payloads"]):
            poll_msgs.setdefault(m["user_id"], []).append(next(x for x in m["payloads"] if x.startswith("pa:")))
    group_posts = sum(1 for m in sent if m["chat_id"] and any(x.startswith("pa:") for x in m["payloads"]))
    # 300 родителей нажимают «Согласен» в течение ANSWER_SPREAD секунд; ~5 % — двойное нажатие / повтор доставки
    pool = Pool(stack.bots, 64)
    events = []
    for uid, payloads in poll_msgs.items():
        pid = payloads[0].split(":")[1]
        events.append(callback(uid, f"pa:{pid}:0", f"cb-a-{uid}"))
    random.shuffle(events)
    dups = random.sample(events, max(1, len(events) // 20))
    lat, codes = [], {}
    t1 = time.time()

    async def fire(ev, at):
        await asyncio.sleep(max(0, at - time.time()))
        st, _, dt = await pool.request("POST", "/webhook", json.dumps(ev).encode(), WH)
        lat.append(dt)
        codes[st] = codes.get(st, 0) + 1
    spread = a.answer_spread
    jobs = [fire(ev, t1 + spread * k / len(events)) for k, ev in enumerate(events)]
    jobs += [fire(ev, t1 + spread * random.random() + 0.2) for ev in dups]
    await asyncio.gather(*jobs)

    async def answered():
        st = await get_json("127.0.0.1", FAKE_PORT, "/_stats")
        return st["answers_unique"] >= len(events), st
    ok, st, _ = await wait_for(answered, 180)
    t_answers = time.time() - t1
    await pool.close()
    conn = await pg(a)
    try:
        db_answered = await conn.fetchval(
            "select count(*) from poll_targets t join polls p on p.id = t.poll_id "
            "where p.campaign_id is not null and t.answer is not null")
        db_targets = await conn.fetchval(
            "select count(*) from poll_targets t join polls p on p.id = t.poll_id where p.campaign_id is not null")
        db_polls = await conn.fetchval("select count(*) from polls where campaign_id is not null")
    finally:
        await conn.close()
    sent_all = await get_json("127.0.0.1", FAKE_PORT, "/_sent")
    all_done = sum(1 for m in sent_all if m["user_id"] and m["text"].startswith("🎉 Ответили все"))
    stack.stop()
    return {
        "dispatch_s": round(t_dispatch, 2), "answers_s": round(t_answers, 2),
        "total_s": round(t_dispatch + t_answers, 2), "all_processed": ok,
        "parents_messaged": len(poll_msgs), "parents_expected": len(parents),
        "parents_with_duplicate_poll_message": sum(1 for v in poll_msgs.values() if len(v) > 1),
        "group_posts": group_posts, "db_polls": db_polls, "db_targets": db_targets, "db_answered": db_answered,
        "callbacks_sent": len(events) + len(dups), "callbacks_unique": len(events),
        "max_answers_unique": st["answers_unique"], "max_answers_dup": st["answers_dup"],
        "teacher_all_answered_msgs": all_done, "max_429": st["too_many"], "max_peak_rps": st["peak_rps"],
        "max_calls": st["calls"], "webhook_codes": codes, "webhook_latency_ms": percentiles(lat),
        "leaders": leaders["leaders"], "logs": log_errors(stack.logdir, tag),
    }


# ---------------------------------------------------------------- сценарий (б): пик вебхуков
async def scenario_b(a, stack: Stack, tag: str, rate: float) -> dict:
    conn = await pg(a)
    try:
        polls = {r["chat_id"]: r["id"] for r in await conn.fetch("select id, chat_id from polls where not closed")}
        base_q = await conn.fetchval("select count(*) from open_questions")
        base_ans = await conn.fetchval("select count(*) from poll_targets where answer is not null")
    finally:
        await conn.close()
    await post_json("127.0.0.1", FAKE_PORT, "/_reset")
    n_classes = a.classes_b
    total = int(rate * a.duration)
    rnd = random.Random(int(rate))
    # родители, ещё не отвечавшие на сбор (кнопку жмут один раз; повторы — это повторная доставка)
    fresh = [(i, j) for i in range(n_classes) for j in range(roster.PARENTS)]
    random.Random(42).shuffle(fresh)  # один порядок на все прогоны: used_parents не пересекаются
    fresh = iter(fresh[a.used_parents:])
    events, uniq_cb, uniq_q = [], 0, 0
    for k in range(total):
        x = rnd.random()
        if events and x < a.dup_share:
            events.append(rnd.choice(events))  # MAX доставил повторно
            continue
        if x < a.dup_share + a.cb_share:
            i, j = next(fresh)
            a.used_parents += 1
            uid = roster.parent_id(i, j)
            events.append(callback(uid, f"pa:{polls[roster.chat_id(i)]}:{rnd.randint(0, 1)}", f"cb-{tag}-{rate}-{k}"))
            uniq_cb += 1
        else:
            i, j = rnd.randrange(n_classes), rnd.randrange(roster.PARENTS)
            is_q = rnd.random() < a.q_share
            text = f"{rnd.choice(QUESTIONS)} ({k})" if is_q else rnd.choice(CHATTER)
            uniq_q += is_q
            events.append(chat_message(roster.parent_id(i, j), roster.chat_id(i), text, f"mid-{tag}-{rate}-{k}"))
    bodies = [json.dumps(ev, ensure_ascii=False).encode() for ev in events]
    pool = Pool(stack.bots, a.connections)
    lat, lag, codes = [], [], {}
    t0 = time.time() + 0.5

    async def fire(k):
        at = t0 + k / rate
        d = at - time.time()
        if d > 0:
            await asyncio.sleep(d)
        lag.append(max(0.0, time.time() - at))
        st, _, dt = await pool.request("POST", "/webhook", bodies[k], WH)
        lat.append(dt)
        codes[st] = codes.get(st, 0) + 1
    print(f"[{tag}] (б) {rate:.0f} событий/с × {a.duration} с: {total} событий "
          f"(кнопок {uniq_cb}, вопросов {uniq_q}, повторов {total - len(set(map(id, events)))})", flush=True)
    await asyncio.gather(*(fire(k) for k in range(total)))
    t_sent = time.time()
    send_s = t_sent - t0
    await pool.close()

    async def done():
        st = await get_json("127.0.0.1", FAKE_PORT, "/_stats")
        c = await pg(a)
        try:
            q = await c.fetchval("select count(*) from open_questions") - base_q
            ans = await c.fetchval("select count(*) from poll_targets where answer is not null") - base_ans
        finally:
            await c.close()
        return st["answers_unique"] >= uniq_cb and q >= uniq_q, {"st": st, "q": q, "ans": ans}
    ok, val, drain_s = await wait_for(done, a.drain_timeout, every=1.0)
    st = val["st"]
    return {
        "rate_target": rate, "events": total, "sent_s": round(send_s, 2),
        "rate_achieved": round(total / send_s, 1), "webhook_codes": codes,
        "webhook_latency_ms": percentiles(lat), "send_lag_ms": percentiles(lag),
        "all_processed": ok, "drain_after_send_s": round(drain_s, 1),
        "processed_rate": round(total / (send_s + drain_s), 1),
        "callbacks_unique": uniq_cb, "max_answers_unique": st["answers_unique"], "max_answers_dup": st["answers_dup"],
        "db_answers": val["ans"], "questions_unique": uniq_q, "db_open_questions": val["q"],
        "max_429": st["too_many"], "max_peak_rps": st["peak_rps"],
    }


# ---------------------------------------------------------------- сценарий (в): «Мои сборы»
async def scenario_c(a, stack: Stack, tag: str) -> dict:
    users = [roster.parent_id(i, roster.PARENTS - 1) for i in range(a.c_users)]  # последний родитель класса
    headers = [{"X-Max-Init-Data": init_data(u)} for u in users]
    pool = Pool(stack.bots, a.c_users)
    lat, codes, polls_seen = [], {}, []
    stop_at = time.time() + a.c_duration

    async def vu(k):
        while time.time() < stop_at:
            st, body, dt = await pool.request("GET", "/api/my/polls", b"", headers[k])
            lat.append(dt)
            codes[st] = codes.get(st, 0) + 1
            if st == 200 and len(polls_seen) < 1000:
                polls_seen.append(len(json.loads(body)))
    t = time.time()
    await asyncio.gather(*(vu(k) for k in range(len(users))))
    el = time.time() - t
    await pool.close()
    return {"users": len(users), "duration_s": round(el, 1), "requests": len(lat), "rps": round(len(lat) / el, 1),
            "codes": codes, "latency_ms": percentiles(lat),
            "polls_per_response": sorted(set(polls_seen))}


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project", default=str(HERE.parent))
    p.add_argument("--label", default="")
    p.add_argument("--instances", type=int, default=1, help="экземпляров, принимающих HTTP")
    p.add_argument("--workers", type=int, default=0, help="доп. процессов ROLE=worker (тогда HTTP-экземпляры — ROLE=web)")
    p.add_argument("--scenarios", default="a,b,c")
    p.add_argument("--db", default="postgresql+asyncpg://pm:pm@127.0.0.1:55433/pm")
    p.add_argument("--redis", default="redis://127.0.0.1:56380/0")
    p.add_argument("--max-limit", type=float, default=30, help="лимит поддельного MAX в (а), запросов/с")
    p.add_argument("--max-rps", type=float, default=25, help="MAX_RPS бота в (а)")
    p.add_argument("--max-latency", type=float, default=0.01)
    p.add_argument("--answer-spread", type=float, default=10)
    p.add_argument("--rates", default="500,1000,2000")
    p.add_argument("--duration", type=float, default=10)
    p.add_argument("--classes-b", type=int, default=1700)
    p.add_argument("--cb-share", type=float, default=0.15)
    p.add_argument("--q-share", type=float, default=0.10)
    p.add_argument("--dup-share", type=float, default=0.03)
    p.add_argument("--connections", type=int, default=256)
    p.add_argument("--drain-timeout", type=float, default=300)
    p.add_argument("--b-max-rps", type=float, default=5000, help="MAX_RPS бота в (б)/(в): меряем само приложение")
    p.add_argument("--c-users", type=int, default=200)
    p.add_argument("--c-duration", type=float, default=20)
    p.add_argument("--pool-size", type=int, default=10)
    p.add_argument("--max-overflow", type=int, default=20)
    p.add_argument("--handler-concurrency", type=int, default=64)
    p.add_argument("--event-queue", default="auto", help="EVENT_QUEUE ботов: auto | redis | local")
    p.add_argument("--out", default="")
    a = p.parse_args()
    a.used_parents = 0
    a.project = str(Path(a.project).resolve())
    logdir = Path(a.out).with_suffix("") if a.out else HERE / "results" / "logs"
    logdir = logdir.parent / (logdir.name + "-logs")
    logdir.mkdir(parents=True, exist_ok=True)
    scen = a.scenarios.split(",")
    res: dict = {"label": a.label, "instances": a.instances, "workers": a.workers, "project": a.project, "started": time.ctime()}
    stack = Stack(a, logdir)
    try:
        if "a" in scen:
            res["a"] = await scenario_a(a, stack, f"a{a.instances}")
            print(json.dumps(res["a"], ensure_ascii=False), flush=True)
        if "b" in scen or "c" in scen:
            print(f"[b] засев: {seed(a, a.classes_b, True)}", flush=True)
            await flush_redis(a.redis)
            stack.start_fake(limit=0)
            stack.start_bots(a.instances, a.b_max_rps, f"b{a.instances}")
            await stack.wait_ready()
            await asyncio.sleep(1)
            res["b_leaders"] = await stack.leaders()
            if "b" in scen:
                res["b"] = []
                for r in [float(x) for x in a.rates.split(",") if x]:
                    one = await scenario_b(a, stack, f"b{a.instances}", r)
                    print(json.dumps(one, ensure_ascii=False), flush=True)
                    res["b"].append(one)
            if "c" in scen:
                res["c"] = await scenario_c(a, stack, f"c{a.instances}")
                print(json.dumps(res["c"], ensure_ascii=False), flush=True)
            res["b_logs"] = log_errors(logdir, f"b{a.instances}")
            stack.stop()
    finally:
        stack.stop()
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("готово", a.out)


if __name__ == "__main__":
    asyncio.run(main())
