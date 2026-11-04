"""Микробенчмарк поиска по справочнику: сколько стоит один вопрос при большой общей базе школы.

    python loadtest/bench_search.py [путь к проекту] [число карточек]

Поиск — чистый Python в цикле событий: пока он считает, процесс не принимает другие события.
"""
import random
import sys
import time
from pathlib import Path

root = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)
n = int(sys.argv[2]) if len(sys.argv) > 2 else 500
sys.path.insert(0, root)

from app.services import search  # noqa: E402

random.seed(1)
W = ("экскурсия музей оплата форма физкультура собрание родителей справка медосмотр каникулы расписание "
     "звонков столовая питание кружок олимпиада учебник тетрадь сменка дежурство").split()
entries = [(" ".join(random.sample(W, 4)) + "?", ", ".join(random.sample(W, 3))) for _ in range(n)]
qs = ["когда будет " + " ".join(random.sample(W, 2)) + "?" for _ in range(100)]
search.rank(qs[0], entries, lambda e: e[1], lambda e: e[0])  # прогрев
t = time.perf_counter()
for q in qs:
    search.rank(q, entries, lambda e: e[1], lambda e: e[0])
dt = (time.perf_counter() - t) / len(qs)
print(f"rank по {n} карточкам: {dt * 1000:.2f} мс на вопрос ({1 / dt:.0f} вопросов/с на ядро)")
