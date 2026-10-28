#!/bin/sh
# Full benchmark as published in bench/results/: this tool and the three rivals, 3 ecosystems x 2 modes,
# 10 trials each (override with TRIALS=n). Rivals must be cloned under $RIVALS_DIR (default bench/rivals/).
set -e
cd "$(dirname "$0")/.."
TRIALS="${TRIALS:-10}"
RES="${BENCH_RESULTS:-bench/results}"
mkdir -p "$RES"
echo "start $(date -Iseconds) load: $(uptime | sed 's/.*load average: //')" > "$RES/load.txt"
python3 bench/run.py --tools "$(python3 -c 'import sys; sys.path.insert(0, "bench"); import harness; print(harness.SELF)'),wasitme,pp,fof" \
  --trials "$TRIALS" --no-score
echo "end   $(date -Iseconds) load: $(uptime | sed 's/.*load average: //')" >> "$RES/load.txt"
python3 bench/score.py
