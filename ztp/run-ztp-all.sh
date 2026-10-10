#!/bin/bash
# DC3 ZTP, all 30 switches one after another on the laptop.
# Loop around ztp/run-ztp.sh: the network, Kea and the ZTP server start once; then for each switch a blank cEOS with that
# switch's serial runs ZTP until the server logs complete or failed for it (or PER_SWITCH_MAX s), and is removed before the next
# one (one cEOS at a time: Infrahub keeps running for the live render). Writes ztp-all-<UTC>.tsv with one row per switch.
#   run-ztp-all.sh [device ...]      default: the 30 switches in the order below
# Needs what run-ztp.sh needs, and a serial for every switch (ztp/set_serial.py).
set -u
Z=$(cd "$(dirname "$0")" && pwd); W=${DC3_ZTP_WORK:-$HOME/.dc3-ztp}; EV="$Z/ztp-events.jsonl"
PER_SWITCH_MAX=${PER_SWITCH_MAX:-600}
DEVS=("$@")
[ ${#DEVS[@]} -gt 0 ] || DEVS=(DC3-S1 DC3-S2 $(for n in $(seq 1 20); do echo DC3-L$n; done) DC3-BL1 DC3-BL2 DC3-SL1 DC3-SL2 DC3-EDGE1 DC3-EDGE2 DC3-DMZ1 DC3-DMZ2)
OUT="$W/ztp-all-$(date -u +%Y%m%dT%H%M%SZ).tsv"; mkdir -p "$W"
printf 'device\tserial\tstart_utc\tresult\tseconds\tdetail\n' > "$OUT"
i=0
for d in "${DEVS[@]}"; do
  i=$((i+1)); t0=$(date +%s); ts=$(date -u +%Y-%m-%dT%H:%M:%SZ); mac=$(printf '02:dc:30:00:00:%02x' $i)
  echo "=== $i/${#DEVS[@]} $d ($ts)"
  if [ $i = 1 ]; then CEOS_MAC=$mac bash "$Z/run-ztp.sh" start "$d" || { printf '%s\t-\t%s\tERROR\t0\tstart failed\n' "$d" "$ts" >> "$OUT"; break; }
  else SKIP_INFRA=1 CEOS_MAC=$mac bash "$Z/run-ztp.sh" start "$d" || { printf '%s\t-\t%s\tERROR\t0\tstart failed\n' "$d" "$ts" >> "$OUT"; continue; }; fi
  res=TIMEOUT; detail="no complete/failed within ${PER_SWITCH_MAX} s"
  while [ $(( $(date +%s) - t0 )) -lt "$PER_SWITCH_MAX" ]; do
    sleep 10
    line=$(python3 - "$EV" "$d" "$ts" <<'PY'
import json, sys
ev, dev, since = sys.argv[1:]
last = ""
for l in open(ev):
    r = json.loads(l)
    if r.get("device") == dev and r["t"] >= since and r.get("event") in ("state", "callback-failed") and r.get("state") in (None, "complete", "failed"):
        last = f'{r.get("state") or "failed"}\t{r.get("reason", r.get("after_s", ""))}'
print(last)
PY
)
    case "$line" in complete*|failed*) res=${line%%$'\t'*}; detail=${line#*$'\t'}; break ;; esac
  done
  sn=$(grep -o 'SERIALNUMBER=.*' "$W/flash-current/ceos-config" 2>/dev/null | cut -d= -f2)
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$d" "$sn" "$ts" "$res" "$(( $(date +%s) - t0 ))" "$detail" >> "$OUT"
  echo "    $res after $(( $(date +%s) - t0 )) s ($detail)"
  docker exec dc3-ztp-ceos Cli -p 15 -c "show hostname" 2>/dev/null | head -1 | sed 's/^/    /'
  docker rm -f dc3-ztp-ceos >/dev/null 2>&1
done
echo "=== result table: $OUT"; column -t -s$'\t' "$OUT"
awk -F'\t' 'NR>1{c[$4]++} END{for(k in c) printf "%s %d  ", k, c[k]; print ""}' "$OUT"
