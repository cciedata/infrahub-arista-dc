#!/bin/bash
# DC3 ZTP, all 30 switches IN PARALLEL. Needs a host with memory for 30 cEOS at once (as the full
# lab); a smaller host uses run-ztp-all.sh (one at a time).
# The network, Kea and the ZTP server start once (run-ztp.sh start with the first switch); every other switch gets its own blank
# cEOS dc3-ztp-<device> with Docker address 10.250.32.150+n (outside the switch Management1 addresses .2-.39, the DHCP pool
# .200-.239 and the lab-only .240-.253) and lab MAC 02:dc:30:00:01:NN, started STAGGER s apart so the live renders do not all
# hit Infrahub at once. Waits until every switch is complete or failed (or MAX_S), writes ztp-par-<UTC>.tsv, then removes the
# switches (run-ztp.sh stop removes Kea, the server and the network).
#   run-ztp-par.sh [device ...]          env: DOCKER (docker command, default docker), STAGGER (10), MAX_S (1500)
set -u
Z=$(cd "$(dirname "$0")" && pwd); W=${DC3_ZTP_WORK:-$HOME/.dc3-ztp}; EV="$Z/ztp-events.jsonl"; DOCKER=${DOCKER:-docker}
STAGGER=${STAGGER:-10}; MAX_S=${MAX_S:-1500}
DEVS=("$@")
[ ${#DEVS[@]} -gt 0 ] || DEVS=(DC3-S1 DC3-S2 $(for n in $(seq 1 20); do echo DC3-L$n; done) DC3-BL1 DC3-BL2 DC3-SL1 DC3-SL2 DC3-EDGE1 DC3-EDGE2 DC3-DMZ1 DC3-DMZ2)
mkdir -p "$W"; OUT="$W/ztp-par-$(date -u +%Y%m%dT%H%M%SZ).tsv"; T0=$(date +%s); SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
i=0
for d in "${DEVS[@]}"; do
  i=$((i+1)); export ZTP_NODE="dc3-ztp-$d" CEOS_IP="10.250.32.$((149+i))" CEOS_MAC=$(printf '02:dc:30:00:01:%02x' $i)
  if [ $i = 1 ]; then bash "$Z/run-ztp.sh" start "$d" || { echo "STOP: infra/first switch failed"; exit 1; }
  else SKIP_INFRA=1 bash "$Z/run-ztp.sh" start "$d" | tail -1; fi
  sleep "$STAGGER"
done
echo "=== all ${#DEVS[@]} started after $(( $(date +%s) - T0 )) s; waiting (max ${MAX_S} s)"
while :; do
  sleep 20
  st=$(python3 - "$EV" "$SINCE" "${DEVS[@]}" <<'PY'
import json, sys
ev, since, devs = sys.argv[1], sys.argv[2], sys.argv[3:]
res = {}
for l in open(ev):
    r = json.loads(l)
    d = r.get("device")
    if d in devs and r["t"] >= since and r.get("event") in ("state", "callback-failed"):
        s = r.get("state") or "failed"
        if s in ("complete", "failed"):
            res[d] = (s, r["t"], str(r.get("reason", r.get("after_s", ""))))
done = len(res)
print(f"{done} {sum(1 for v in res.values() if v[0] == 'complete')}")
with open(sys.argv[1] + ".par", "w") as f:
    for d in devs:
        s, t, x = res.get(d, ("PENDING", "", ""))
        f.write(f"{d}\t{s}\t{t}\t{x}\n")
PY
)
  set -- $st; echo "  $(( $(date +%s) - T0 )) s: finished $1 of ${#DEVS[@]}, complete $2"
  [ "$1" -ge ${#DEVS[@]} ] && break
  [ $(( $(date +%s) - T0 )) -ge "$MAX_S" ] && { echo "  MAX_S reached"; break; }
done
printf 'device\tresult\tfinished_utc\tdetail\n' > "$OUT"; cat "$EV.par" >> "$OUT"
echo "=== result table: $OUT"; column -t -s$'\t' "$OUT"
awk -F'\t' 'NR>1{c[$2]++} END{for(k in c) printf "%s %d  ", k, c[k]; print ""}' "$OUT"
for d in "${DEVS[@]}"; do $DOCKER rm -f "dc3-ztp-$d" >/dev/null 2>&1; done
