#!/usr/bin/env bash
# Полная матрица «после»: 1 экземпляр, 3 экземпляра, прод-схема 2 web + 3 worker.
#   OUT=/tmp/lt PY=python bash loadtest/matrix.sh
# Для «до» тот же run_all.py с --project <папка старого коммита>.
set -u
PY=${PY:-python}
OUT=${OUT:-loadtest/results}
mkdir -p "$OUT"
export PYTHONIOENCODING=utf-8
"$PY" loadtest/run_all.py --label "1 экземпляр ROLE=all" --instances 1 --scenarios a,b,c \
  --out "$OUT/after-1.json" > "$OUT/after-1.out" 2>&1
"$PY" loadtest/run_all.py --label "3 экземпляра ROLE=all" --instances 3 --scenarios a,b,c --answer-spread 5 \
  --out "$OUT/after-3.json" > "$OUT/after-3.out" 2>&1
"$PY" loadtest/run_all.py --label "2 web + 3 worker" --instances 2 --workers 3 --scenarios a,b,c --answer-spread 5 \
  --out "$OUT/after-2w3.json" > "$OUT/after-2w3.out" 2>&1
grep -h '^{' "$OUT"/after-*.out | cut -c1-400
