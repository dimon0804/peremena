"""Ступень нагрузки «вечерний пик N классов» против уже поднятого стенда (docker-compose.vps.yml).

    python loadtest/vps_profile.py step --classes 100 --duration 120 --out results/raw/step-100.json
    python loadtest/vps_profile.py merge --step results/raw/step-100.json --stats raw/stats.csv --logs raw/bot.log …

Модель вечера 20:00–22:00 на класс (30 родителей, учитель) — расчёт, а не замер; см. docs/НАГРУЗКА.md:
  * сообщения в чате класса — --msg-rate событий/с на класс (10 % из них — вопросы, попадают в «частые вопросы»);
  * нажатия кнопок открытого сбора — --cb-rate событий/с на класс (каждый родитель жмёт один раз);
  * «Мои сборы» в мини-приложении — --app-rate запросов/с на класс (подпись данных запуска MAX настоящая);
  * в начале ступени завуч запускает сбор на параллель — --campaign-classes классов (через API школы),
    родители этих классов отвечают на него в течение ступени;
  * --dup-share событий MAX доставляет повторно (не получил 200 вовремя).
Сверка после ступени: каждое уникальное нажатие отвечено ровно раз и записано в базу, каждый вопрос записан
ровно раз, каждому родителю параллели — ровно одно сообщение сбора. Метрики очереди — с /metrics самого бота.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import roster  # noqa: E402
from httpload import Pool, get_json, percentiles, post_json  # noqa: E402
from run_all import QUESTIONS, CHATTER, WH, callback, chat_message, init_data  # noqa: E402


async def pg(db: str):
    import asyncpg

    return await asyncpg.connect(db.replace("postgresql+asyncpg://", "postgresql://"))


async def scrape(host: str, port: int) -> dict:
    """Нужные строки /metrics: очередь и счётчики обработки по всем процессам."""
    from httpload import Conn

    c = Conn(host, port)
    try:
        status, body = await c.request("GET", "/metrics", timeout=10)
    finally:
        await c.close()
    out: dict = {"processed": 0.0, "failed": 0.0, "dead_total": 0.0, "backpressure": 0.0, "max_429": 0.0,
                 "max_requests": 0.0}
    if status != 200:
        out["status"] = status
        return out
    for line in body.decode().splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, val = line.rpartition(" ")
        v = float(val)
        base = name.split("{", 1)[0]
        if base in ("peremena_queue_length", "peremena_queue_pending", "peremena_queue_lag", "peremena_queue_dead"):
            out[base.replace("peremena_queue_", "q_")] = v
        elif base == "peremena_events_processed_total":
            out["processed"] += v
        elif base == "peremena_events_failed_total":
            out["failed"] += v
        elif base == "peremena_events_dead_total":
            out["dead_total"] += v
        elif base == "peremena_webhook_backpressure_total":
            out["backpressure"] += v
        elif base == "peremena_max_requests_total":
            out["max_requests"] += v
            if 'result="429"' in name:
                out["max_429"] += v
    return out


async def step(a) -> dict:
    host, port = a.target.split(":")
    port = int(port)
    fh, fp = a.fake.split(":")
    fp = int(fp)
    n = a.classes
    conn = await pg(a.db)
    try:
        polls = {r["chat_id"]: r["id"] for r in await conn.fetch("select id, chat_id from polls where not closed")}
        base_q = await conn.fetchval("select count(*) from open_questions")
        base_ans = await conn.fetchval("select count(*) from poll_targets where answer is not null")
    finally:
        await conn.close()
    await post_json(fh, fp, "/_reset")
    rnd = random.Random(n)
    m0 = await scrape(host, port)

    # --- сбор на параллель: завуч первой школы, первые campaign_classes классов
    camp_classes = list(range(min(a.campaign_classes, n, roster.CLASSES_PER_SCHOOL)))
    camp_result: dict = {}

    async def campaign():
        body = {"chat_ids": [roster.chat_id(i) for i in camp_classes], "template": "consent",
                "text": "Экскурсия в музей в субботу\nНужно согласие до пятницы", "deadline": "week"}
        t = time.time()
        st, resp = await post_json(host, port, f"/api/school/campaigns?dev_user={roster.admin_id(0)}", body,
                                   timeout=600)
        camp_result.update({"status": st, "api_s": round(time.time() - t, 2)})

    # --- расписание событий вебхука
    dur = a.duration
    total_msg = int(a.msg_rate * n * dur)
    total_cb = int(a.cb_rate * n * dur)
    fresh = [(i, j) for i in range(n) for j in range(roster.PARENTS) if i not in camp_classes]
    rnd.shuffle(fresh)
    if total_cb > len(fresh):
        raise SystemExit(f"нажатий {total_cb} больше, чем родителей без ответа ({len(fresh)}) — уменьшите длительность")
    sched: list[tuple[float, bytes, str]] = []  # (момент, тело, вид)
    uniq_cb, uniq_q, cb_sent = 0, 0, {}
    for k in range(total_cb):
        i, j = fresh[k]
        uid = roster.parent_id(i, j)
        cid = f"cb-{n}-{k}"
        at = rnd.uniform(0, dur)
        sched.append((at, json.dumps(callback(uid, f"pa:{polls[roster.chat_id(i)]}:{rnd.randint(0, 1)}", cid),
                                     ensure_ascii=False).encode(), "cb:" + cid))
        uniq_cb += 1
    for k in range(total_msg):
        i, j = rnd.randrange(n), rnd.randrange(roster.PARENTS)
        is_q = rnd.random() < a.q_share
        text = f"{rnd.choice(QUESTIONS)} ({n}-{k})" if is_q else rnd.choice(CHATTER)
        uniq_q += is_q
        sched.append((rnd.uniform(0, dur), json.dumps(chat_message(roster.parent_id(i, j), roster.chat_id(i), text,
                                                                   f"mid-{n}-{k}"), ensure_ascii=False).encode(), "msg"))
    dups = rnd.sample(sched, int(len(sched) * a.dup_share)) if sched else []
    sched += [(min(dur, at + rnd.uniform(0.2, 3)), body, kind + ":dup") for at, body, kind in dups]
    # мини-приложение
    n_app = int(a.app_rate * n * dur)
    app_users = [roster.parent_id(rnd.randrange(n), rnd.randrange(roster.PARENTS)) for _ in range(n_app)]
    app_sched = sorted(rnd.uniform(0, dur) for _ in range(n_app))
    sched.sort(key=lambda x: x[0])

    pool = Pool([(host, port)], a.connections)
    app_pool = Pool([(host, port)], a.app_connections)
    lat_wh, lat_app, lag, codes, app_codes = [], [], [], {}, {}
    samples: list[dict] = []
    t0 = time.time() + 1.0
    sending = True

    async def fire(at, body, kind):
        d = t0 + at - time.time()
        if d > 0:
            await asyncio.sleep(d)
        lag.append(max(0.0, time.time() - (t0 + at)))
        if kind.startswith("cb:") and not kind.endswith(":dup"):
            cb_sent[kind[3:]] = time.time()
        st, _, dt = await pool.request("POST", "/webhook", body, WH)
        lat_wh.append(dt)
        codes[st] = codes.get(st, 0) + 1

    async def open_app(at, uid):
        d = t0 + at - time.time()
        if d > 0:
            await asyncio.sleep(d)
        st, _, dt = await app_pool.request("GET", "/api/my/polls", b"", {"X-Max-Init-Data": init_data(uid)})
        lat_app.append(dt)
        app_codes[st] = app_codes.get(st, 0) + 1

    async def sampler():
        while sending:
            try:
                m = await scrape(host, port)
                m["t"] = round(time.time() - t0, 1)
                samples.append(m)
            except Exception as e:
                samples.append({"t": round(time.time() - t0, 1), "error": str(e)})
            await asyncio.sleep(a.sample_every)

    print(f"[{n}] ступень: {len(sched)} вебхуков ({uniq_cb} нажатий, {uniq_q} вопросов, {len(dups)} повторов), "
          f"{n_app} открытий «Мои сборы», сбор на {len(camp_classes)} классов, {dur:.0f} с", flush=True)
    samp = asyncio.create_task(sampler())
    camp = asyncio.create_task(campaign()) if camp_classes else None
    await asyncio.gather(*(fire(*x) for x in sched), *(open_app(at, u) for at, u in zip(app_sched, app_users)))
    t_sent = time.time()
    send_s = t_sent - t0
    if camp is not None:
        await camp
    # ответы родителей параллели на сбор завуча: по одному нажатию на каждого получателя.
    # Часть рассылки может доводить минутная задача планировщика уже после ответа API — ждём до 60 с.
    camp_parents = {roster.parent_id(i, j) for i in camp_classes for j in range(roster.PARENTS)}
    camp_msgs: dict[int, list[str]] = {}
    t_wait_camp = time.time()
    while True:
        camp_msgs = {}
        for m in await get_json(fh, fp, "/_sent"):
            if m["user_id"] in camp_parents:
                for x in m["payloads"]:
                    if x.startswith("pa:"):
                        camp_msgs.setdefault(m["user_id"], []).append(x)
                        break
        if len(camp_msgs) >= len(camp_parents) or time.time() - t_wait_camp > 60:
            break
        await asyncio.sleep(2)
    camp_result["messages_wait_s"] = round(time.time() - t_wait_camp, 1)
    camp_cb = []
    for uid, payloads in camp_msgs.items():
        pid = payloads[0].split(":")[1]
        camp_cb.append(json.dumps(callback(uid, f"pa:{pid}:0", f"cbc-{n}-{uid}"), ensure_ascii=False).encode())
    tc = time.time()
    for k, body in enumerate(camp_cb):  # за 10 с, как в жизни после рассылки
        d = tc + 10 * k / max(1, len(camp_cb)) - time.time()
        if d > 0:
            await asyncio.sleep(d)
        cid = f"cbc-{n}-{json.loads(body)['callback']['user']['user_id']}"
        cb_sent[cid] = time.time()
        st, _, dt = await pool.request("POST", "/webhook", body, WH)
        lat_wh.append(dt)
        codes[st] = codes.get(st, 0) + 1
    want_cb = uniq_cb + len(camp_cb)
    await pool.close()
    await app_pool.close()

    async def done():
        st = await get_json(fh, fp, "/_stats")
        c = await pg(a.db)
        try:
            q = await c.fetchval("select count(*) from open_questions") - base_q
            ans = await c.fetchval("select count(*) from poll_targets where answer is not null") - base_ans
        finally:
            await c.close()
        return st["answers_unique"] >= want_cb and q >= uniq_q and ans >= want_cb, {"st": st, "q": q, "ans": ans}
    t_wait = time.time()
    ok, val = False, {}
    while True:
        ok, val = await done()
        if ok or time.time() - t_wait > a.drain_timeout:
            break
        await asyncio.sleep(1)
    drain_s = time.time() - t_sent
    sending = False
    await samp
    m1 = await scrape(host, port)
    st = val["st"]
    times = await get_json(fh, fp, "/_answer_times")
    answer_lat = [times[c] - t for c, t in cb_sent.items() if c in times]
    peak_lag = max((s.get("q_lag", 0) + s.get("q_pending", 0) for s in samples if "error" not in s), default=None)
    end_send = [s for s in samples if "error" not in s and s["t"] <= send_s]
    processed = m1["processed"] - m0["processed"]
    return {
        "classes": n, "users": n * (roster.PARENTS + 1), "duration_s": dur,
        "model": {"msg_rate_per_class": a.msg_rate, "cb_rate_per_class": a.cb_rate, "app_rate_per_class": a.app_rate,
                  "q_share": a.q_share, "dup_share": a.dup_share, "campaign_classes": len(camp_classes)},
        "webhooks": len(sched) + len(camp_cb), "webhook_rate": round(len(sched) / send_s, 1),
        "send_s": round(send_s, 1), "webhook_codes": codes, "webhook_latency_ms": percentiles(lat_wh),
        "send_lag_ms": percentiles(lag),
        "miniapp_requests": len(lat_app), "miniapp_rate": round(len(lat_app) / dur, 1), "miniapp_codes": app_codes,
        "miniapp_latency_ms": percentiles(lat_app),
        "campaign": {**camp_result, "parents": len(camp_parents), "parents_messaged": len(camp_msgs),
                     "parents_with_duplicate_message": sum(1 for v in camp_msgs.values() if len(v) > 1)},
        "all_processed": ok, "drain_after_send_s": round(drain_s, 1),
        "events_processed": processed, "processed_per_s": round(processed / (send_s + drain_s), 1) if processed else 0,
        "queue_peak_backlog": peak_lag,
        "queue_backlog_at_end_of_send": (end_send[-1].get("q_lag", 0) + end_send[-1].get("q_pending", 0))
        if end_send else None,
        "callbacks_unique": want_cb, "max_answers_unique": st["answers_unique"], "max_answers_dup": st["answers_dup"],
        "db_answers": val["ans"], "questions_unique": uniq_q, "db_open_questions": val["q"],
        "press_to_answer_ms": percentiles(answer_lat),
        "max_calls": sum(st["calls"].values()), "max_peak_rps": st["peak_rps"], "max_429_seen_by_fake": st["too_many"],
        "metrics_delta": {k: round(m1.get(k, 0) - m0.get(k, 0), 1) for k in
                          ("processed", "failed", "dead_total", "backpressure", "max_429", "max_requests")},
        "queue_samples": samples,
    }


def merge(a) -> None:
    """Склеивает результат ступени с docker stats за время ступени, ошибками в логах и перезапусками."""
    res = json.loads(Path(a.step).read_text(encoding="utf-8"))
    t0, t1 = a.t0, a.t1
    per: dict[str, dict] = {}
    with open(a.stats, encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 4:
                continue
            ts, name, cpu, mem = float(row[0]), row[1], row[2], row[3]
            if not (t0 <= ts <= t1):
                continue
            p = per.setdefault(name, {"cpu": [], "mem": [], "limit": ""})
            try:
                p["cpu"].append(float(cpu.rstrip("%")))
            except ValueError:
                continue
            used, _, limit = mem.partition("/")
            p["mem"].append(_mib(used))
            p["limit"] = limit.strip()
    stats = {}
    for name, p in sorted(per.items()):
        short = name.replace("peremena-stress-", "").rsplit("-", 1)[0]
        cpu = sorted(p["cpu"])
        stats[short] = {"cpu_avg_pct": round(sum(cpu) / len(cpu), 1) if cpu else None,
                        "cpu_p95_pct": cpu[int(0.95 * (len(cpu) - 1))] if cpu else None,
                        "cpu_max_pct": cpu[-1] if cpu else None,
                        "mem_max_mib": round(max(p["mem"]), 1) if p["mem"] else None, "mem_limit": p["limit"],
                        "samples": len(cpu)}
    res["docker_stats"] = stats
    text = Path(a.logs).read_text(encoding="utf-8", errors="replace") if a.logs else ""
    res["bot_log"] = {"errors": text.count(" ERROR "), "tracebacks": text.count("Traceback"),
                      "pool_timeouts": text.count("QueuePool limit"), "warnings": text.count(" WARNING "),
                      "backpressure_warnings": text.count("EVENT_BACKLOG_WARN"),
                      "queue_unavailable": text.count("Очередь событий недоступна")}
    res["containers"] = json.loads(a.inspect) if a.inspect else None
    res["config"] = json.loads(a.config) if a.config else None
    res["label"] = a.label
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("classes", "webhook_rate", "webhook_latency_ms", "all_processed",
                                           "drain_after_send_s", "queue_peak_backlog", "processed_per_s")},
                     ensure_ascii=False))
    print(json.dumps(stats, ensure_ascii=False))


def _mib(s: str) -> float:
    s = s.strip()
    for unit, k in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("GB", 1000 ** 3 / 1024 ** 2),
                    ("MB", 1000 ** 2 / 1024 ** 2), ("kB", 1000 / 1024 ** 2), ("B", 1 / 1024 ** 2)):
        if s.endswith(unit):
            try:
                return float(s[:-len(unit)]) * k
            except ValueError:
                return 0.0
    return 0.0


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("step")
    s.add_argument("--classes", type=int, required=True)
    s.add_argument("--duration", type=float, default=120)
    s.add_argument("--target", default="lb:8080")
    s.add_argument("--fake", default="fakemax:59100")
    s.add_argument("--db", default="postgresql://peremena:peremena@db:5432/peremena")
    s.add_argument("--msg-rate", type=float, default=0.06)
    s.add_argument("--cb-rate", type=float, default=0.02)
    s.add_argument("--app-rate", type=float, default=0.02)
    s.add_argument("--q-share", type=float, default=0.10)
    s.add_argument("--dup-share", type=float, default=0.03)
    s.add_argument("--campaign-classes", type=int, default=5)
    s.add_argument("--connections", type=int, default=256)
    s.add_argument("--app-connections", type=int, default=64)
    s.add_argument("--sample-every", type=float, default=2.0)
    s.add_argument("--drain-timeout", type=float, default=600)
    s.add_argument("--out", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--step", required=True)
    m.add_argument("--stats", required=True)
    m.add_argument("--logs", default="")
    m.add_argument("--t0", type=float, required=True)
    m.add_argument("--t1", type=float, required=True)
    m.add_argument("--inspect", default="")
    m.add_argument("--config", default="")
    m.add_argument("--label", default="")
    m.add_argument("--out", required=True)
    a = p.parse_args()
    if a.cmd == "step":
        res = asyncio.run(step(a))
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        brief = {k: v for k, v in res.items() if k != "queue_samples"}
        print(json.dumps(brief, ensure_ascii=False), flush=True)
    else:
        merge(a)


if __name__ == "__main__":
    main()
