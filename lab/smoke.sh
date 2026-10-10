#!/usr/bin/env bash
# DC3 laptop smoke test: Infrahub stopped, the 6-node smoke subset deployed with its startup-configs, checked,
# captured, destroyed; Infrahub started again in every case (trap).
#
# Needs, before it is run (the script checks and stops early, with Infrahub untouched, when one is missing):
#   - lab/configs/<node>.cfg rendered for DC3-S1 DC3-S2 DC3-L1 DC3-L2 DC3-L11
#   - docker usable without sudo; containerlab usable without sudo (no sudo in this script)
#   - images ceos:4.36.2F and quay.io/frrouting/frr:10.6.0 present locally; python3 with PyYAML (topology script)
#
# Run from anywhere:  bash lab/smoke.sh                     (stops / starts the repo's own Infrahub stack, docs/BUILD.md step 3)
# Another Infrahub (a different folder or compose project):
#   INFRAHUB_DIR=<folder with its docker-compose.yml> INFRAHUB_PROJECT=<compose project> bash lab/smoke.sh
# Results: lab/results/smoke-<UTC time>/ (summary.txt, postcheck.json/.txt, syntax.json/.txt, memory.txt, per node
# show outputs, smoke.log).
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB="$REPO/lab"
TOPO="$LAB/dc3-smoke.clab.yml"
PREFIX="clab-dc3-"
EOS_NODES="DC3-S1 DC3-S2 DC3-L1 DC3-L2 DC3-L11"
ALL_NODES="DC3-S1,DC3-S2,DC3-L1,DC3-L2,DC3-L11,DC3-HOST-PROD"
INFRAHUB_DIR="${INFRAHUB_DIR:-$REPO}"                  # the repo's own Infrahub stack (docs/BUILD.md step 3)
INFRAHUB_PROJECT="${INFRAHUB_PROJECT:-$(awk '/^name:/{print $2; exit}' "$INFRAHUB_DIR/docker-compose.yml" 2>/dev/null)}"
CEOS_IMAGE="ceos:4.36.2F"
FRR_IMAGE="quay.io/frrouting/frr:10.6.0"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-900}"       # s, per run (all EOS nodes)
POSTCHECK_WAIT="${POSTCHECK_WAIT:-300}"   # s, dc3_postcheck.py --wait
POSTCHECK_SETTLE="${POSTCHECK_SETTLE:-90}" # s before the first post-check attempt (BGP/BFD came up ~80 s after the CLI answered)
CAPTURE_CMDS=("show version" "show running-config" "show mlag" "show mlag detail" "show bgp evpn summary"
              "show ip bgp summary" "show bfd peers" "show interfaces status" "show vxlan vtep" "show logging")
# also captured as JSON (<node>/<words>.json) so the post-check field names can be checked against them
CAPTURE_JSON=("show mlag detail" "show ip bgp summary" "show bgp evpn summary" "show bfd peers")

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RES="$LAB/results/smoke-$TS"
mkdir -p "$RES"
exec > >(tee -a "$RES/smoke.log") 2>&1

step() { echo "[$(date -u +%H:%M:%S)] $*"; }
S_INFRAHUB_STOP="not run"; S_DEPLOY="not run"; S_DESTROY="not run"; S_INFRAHUB_START="not run"
S_POSTCHECK="not run"; S_SYNTAX="not run"; BOOT_LINES=""; DEPLOYED=0; INFRAHUB_STOPPED=0

write_summary() {
  {
    echo "DC3 smoke test $TS"
    echo "nodes: $ALL_NODES"
    echo "infrahub stop: $S_INFRAHUB_STOP"
    echo "deploy: $S_DEPLOY"
    printf "boot:\n%b" "$BOOT_LINES"
    echo "postcheck: $S_POSTCHECK"
    [ -f "$RES/postcheck.txt" ] && grep -E '^Total ' "$RES/postcheck.txt" | sed 's/^/  /'
    echo "syntax: $S_SYNTAX"
    [ -f "$RES/syntax.txt" ] && grep -vE '^( {4}|---)' "$RES/syntax.txt" | sed 's/^/  /'
    echo "memory (docker stats --no-stream):"
    [ -f "$RES/memory.txt" ] && sed 's/^/  /' "$RES/memory.txt"
    echo "destroy: $S_DESTROY"
    echo "infrahub start: $S_INFRAHUB_START"
    echo "finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$RES/summary.txt"
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [ "$DEPLOYED" = 1 ]; then
    step "destroy the smoke lab"
    if containerlab destroy -t "$TOPO" --cleanup; then S_DESTROY="ok"; else S_DESTROY="FAILED rc $?"; fi
  fi
  step "start Infrahub again (always)"
  if (cd "$INFRAHUB_DIR" && docker compose -p "$INFRAHUB_PROJECT" start); then S_INFRAHUB_START="ok"
  else S_INFRAHUB_START="FAILED rc $? (run by hand: cd $INFRAHUB_DIR && docker compose -p $INFRAHUB_PROJECT start)"; fi
  write_summary
  step "summary: $RES/summary.txt"
  cat "$RES/summary.txt"
  exit "$rc"
}

# ---------------------------------------------------------------- pre-flight (Infrahub untouched until it passes)
step "pre-flight"
fail=0
for n in $EOS_NODES; do
  [ -f "$LAB/configs/$n.cfg" ] || { echo "  missing lab/configs/$n.cfg (render it first)"; fail=1; }
done
command -v containerlab >/dev/null || { echo "  containerlab not found"; fail=1; }
docker info >/dev/null 2>&1 || { echo "  docker not usable without sudo for $(id -un)"; fail=1; }
for img in "$CEOS_IMAGE" "$FRR_IMAGE"; do
  docker image inspect "$img" >/dev/null 2>&1 || { echo "  image $img not present (docker images)"; fail=1; }
done
python3 -c 'import yaml' 2>/dev/null || { echo "  python3 PyYAML missing (needed by scripts/dc3_lab_topology.py)"; fail=1; }
# containerlab needs root unless the user is in the clab_admins group (containerlab >= 0.62 non-root mode)
if [ "$(id -u)" -ne 0 ] && ! id -nG | tr ' ' '\n' | grep -qx clab_admins && [ "${SMOKE_CLAB_NONROOT_OK:-0}" != 1 ]; then
  echo "  containerlab is not usable without sudo for $(id -un) (not in group clab_admins). Run by hand once:"
  echo "    sudo groupadd -f clab_admins && sudo usermod -aG clab_admins $(id -un)   # then log in again"
  echo "  or deploy by hand: sudo containerlab deploy -t $TOPO --reconfigure"
  echo "  (set SMOKE_CLAB_NONROOT_OK=1 if containerlab already runs without sudo another way)"
  fail=1
fi
[ -d "$INFRAHUB_DIR" ] || { echo "  $INFRAHUB_DIR not found (set INFRAHUB_DIR)"; fail=1; }
if [ "$fail" = 1 ]; then
  step "pre-flight failed; nothing stopped or deployed"
  S_INFRAHUB_STOP="not run (pre-flight failed)"; S_INFRAHUB_START="not needed"
  write_summary
  exit 3
fi

trap cleanup EXIT INT TERM

# ---------------------------------------------------------------- stop Infrahub (memory)
step "stop Infrahub ($INFRAHUB_PROJECT)"
if (cd "$INFRAHUB_DIR" && docker compose -p "$INFRAHUB_PROJECT" stop); then S_INFRAHUB_STOP="ok"; INFRAHUB_STOPPED=1
else S_INFRAHUB_STOP="FAILED rc $?"; exit 4; fi
free -m > "$RES/free-before.txt" 2>&1 || true

# ---------------------------------------------------------------- topology and deploy
step "write the smoke topology"
python3 "$REPO/scripts/dc3_lab_topology.py" --subset smoke || exit 5
step "fill delivery-time secrets into lab/configs (missing secret = line dropped, warning)"
python3 "$LAB/dc3_secrets.py" fill "$LAB/configs" 2>&1 | tee "$RES/secrets-fill.txt" | grep -c WARNING | sed 's/^/  warnings: /'
step "deploy $TOPO"
DEPLOYED=1
containerlab deploy -t "$TOPO" --reconfigure 2>&1 | tee "$RES/deploy.txt"; rc=${PIPESTATUS[0]}
if [ "$rc" = 0 ]; then S_DEPLOY="ok"
# containerlab's post-deploy step can time out on slow cEOS boots ("channel timeout sending input to device") while the
# switches still come up; then go on: the EOS wait and the post-check below show whether they did
elif grep -q 'post-deploy: errTimeoutError: channel timeout' "$RES/deploy.txt" \
     && ! grep ' ERRO ' "$RES/deploy.txt" | grep -vq 'post-deploy: errTimeoutError: channel timeout'; then
  S_DEPLOY="ok (containerlab post-deploy timeouts only; see boot and postcheck)"
else S_DEPLOY="FAILED rc $rc"; exit 6; fi

# ---------------------------------------------------------------- wait for EOS
step "wait for EOS (Cli show version, max ${BOOT_TIMEOUT} s)"
t0=$(date +%s)
for n in $EOS_NODES; do
  while :; do
    if docker exec "$PREFIX$n" Cli -p 15 -c "show version" >/dev/null 2>&1; then
      BOOT_LINES+="  $n: Cli answers after $(( $(date +%s) - t0 )) s\n"; break
    fi
    if [ $(( $(date +%s) - t0 )) -ge "$BOOT_TIMEOUT" ]; then
      BOOT_LINES+="  $n: no answer within ${BOOT_TIMEOUT} s\n"; break
    fi
    sleep 10
  done
done
printf "%b" "$BOOT_LINES"

step "memory after boot"
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' | grep "^$PREFIX" | sort > "$RES/memory.txt"
free -m > "$RES/free-after-boot.txt" 2>&1 || true
cat "$RES/memory.txt"

# ---------------------------------------------------------------- post-check (startup-configs only, nothing loaded)
step "post-check (settle ${POSTCHECK_SETTLE} s, re-run while something fails for ${POSTCHECK_WAIT} s; raw output in postcheck-raw/)"
python3 "$LAB/dc3_postcheck.py" --topology "$TOPO" --nodes "$ALL_NODES" --settle "$POSTCHECK_SETTLE" \
  --wait "$POSTCHECK_WAIT" --out "$RES/postcheck.json" > "$RES/postcheck.txt" 2>&1
pc=$?
S_POSTCHECK=$([ $pc = 0 ] && echo "PASS (no FAIL / ERROR)" || echo "rc $pc (FAIL or ERROR rows, see postcheck.txt)")
tail -3 "$RES/postcheck.txt"

# ---------------------------------------------------------------- captures
step "captures"
for n in $EOS_NODES; do
  mkdir -p "$RES/$n"
  for c in "${CAPTURE_CMDS[@]}"; do
    f="$RES/$n/$(echo "$c" | tr ' ' '_').txt"
    docker exec "$PREFIX$n" Cli -p 15 -c "$c" > "$f" 2>&1 || echo "(rc $?)" >> "$f"
  done
  for c in "${CAPTURE_JSON[@]}"; do
    f="$RES/$n/$(echo "$c" | tr ' ' '_').json"
    docker exec "$PREFIX$n" Cli -p 15 -c "$c | json" > "$f" 2> "$f.err" || echo "(rc $?)" >> "$f.err"
    [ -s "$f.err" ] || rm -f "$f.err"
  done
done
mkdir -p "$RES/DC3-HOST-PROD"
docker exec "${PREFIX}DC3-HOST-PROD" sh -c 'ip addr; ip route' > "$RES/DC3-HOST-PROD/ip.txt" 2>&1 || true

# ---------------------------------------------------------------- syntax items
step "syntax items (running-config and logging)"
python3 "$LAB/dc3_smoke_syntax.py" --results "$RES" --nodes "$(echo $EOS_NODES | tr ' ' ',')"
sx=$?
S_SYNTAX=$([ $sx = 0 ] && echo "no FAIL" || echo "rc $sx (see syntax.txt)")

step "memory after the checks"
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' | grep "^$PREFIX" | sort > "$RES/memory-after-checks.txt"
[ "$pc" = 0 ] || exit 7     # post-check FAIL or ERROR rows (a node that never answers is an ERROR row)
exit 0
