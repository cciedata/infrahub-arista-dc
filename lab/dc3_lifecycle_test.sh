#!/usr/bin/env bash
# DC3 node lifecycle test: stop, start and restart single devices in a running lab, the way the containerlab VS Code
# extension's node actions "Stop" / "Start" / "Restart" and the lab actions "Stop nodes" / "Start nodes" do, and check that
# links, configs and routing come back (docs/LAB-GUI.md).
#
#   bash lab/dc3_lifecycle_test.sh                 laptop: the smoke subset (S1, S2, L1, L2, L11, PROD host), Infrahub
#                                                  stopped during the test and started again in every case
#   LIFECYCLE_KEEP=1 bash lab/dc3_lifecycle_test.sh  same, but the lab stays up afterwards (open it in the VS Code extension)
#   LIFECYCLE_LAB=up bash lab/dc3_lifecycle_test.sh   a lab that is already deployed (for example the full lab,
#                                                  TOPO=lab/dc3.clab.yml): nothing deployed, destroyed or stopped
#
# Tests (each prints PASS / FAIL with what it saw; results in lab/results/lifecycle-<UTC>/):
#   A  containerlab stop --node DC3-L11, then start: the switch is down while stopped (spines lose its sessions), and after
#      start its startup-config, its links and its BGP sessions are back, and the PROD host reaches its gateway again
#   B  containerlab stop --node DC3-HOST-PROD, then start: does the host keep its address and route? Its config comes from
#      containerlab `exec` at deploy time; if start does not run exec again, the test says so and re-applies it (exec lines
#      of the topology) so the lab is usable
#   C  containerlab restart --node DC3-L1 (MLAG member): MLAG active again on L1 and L2, BGP back
#   D  containerlab stop / start of the whole lab (no --node): every switch back with all its BGP sessions
#   E  read only: does L11's Management1 answer on TCP 22 (SSH) and 443 (eAPI) from the lab host (the SSH node action)
# Needs: containerlab >= 0.75 (node subsets for stop/start/restart: containerlab.dev/rn/0.75), docker and containerlab
# without sudo, cEOS image ceos:4.36.2F and FRR image, renders in lab/configs.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB="$REPO/lab"
TOPO="${TOPO:-$LAB/dc3-smoke.clab.yml}"
MODE="${LIFECYCLE_LAB:-deploy}"
KEEP="${LIFECYCLE_KEEP:-0}"       # 1: leave the lab running after the tests (and Infrahub stopped) to work on it in the GUI
PREFIX="clab-dc3-"
INFRAHUB_DIR="${INFRAHUB_DIR:-$REPO}"                  # the repo's own Infrahub stack (docs/BUILD.md step 3)
INFRAHUB_PROJECT="${INFRAHUB_PROJECT:-$(awk '/^name:/{print $2; exit}' "$INFRAHUB_DIR/docker-compose.yml" 2>/dev/null)}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-900}"
CONVERGE_TIMEOUT="${CONVERGE_TIMEOUT:-300}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RES="$LAB/results/lifecycle-$TS"
mkdir -p "$RES"
exec > >(tee -a "$RES/lifecycle.log") 2>&1

step() { echo "[$(date -u +%H:%M:%S)] $*"; }
RESULTS=()
result() { RESULTS+=("$1 $2: $3"); echo ">>> $1 $2: $3"; }
eos() { docker exec "$PREFIX$1" Cli -p 15 -c "$2" 2>/dev/null; }
state() { docker inspect -f '{{.State.Status}}' "$PREFIX$1" 2>/dev/null || echo absent; }
clab() { containerlab "$@" -t "$TOPO"; }
# established sessions of a switch, all VRFs (JSON of show ip bgp summary vrf all + show bgp evpn summary)
estab() {
  python3 - "$1" <<'PY'
import json, subprocess, sys
n = sys.argv[1]; tot = 0
for cmd in ("show ip bgp summary vrf all | json", "show bgp evpn summary | json"):
    p = subprocess.run(["docker", "exec", "clab-dc3-" + n, "Cli", "-p", "15", "-c", cmd], capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
    except Exception:
        continue
    for v in d.get("vrfs", {}).values():
        tot += sum(1 for x in v.get("peers", {}).values() if x.get("peerState") == "Established")
print(tot)
PY
}
wait_cli() {   # wait_cli <node> <timeout s>
  local t0=$(date +%s)
  until eos "$1" "show version" >/dev/null; do
    [ $(( $(date +%s) - t0 )) -ge "$2" ] && return 1
    sleep 10
  done
  echo $(( $(date +%s) - t0 ))
}
wait_estab() {  # wait_estab <node> <count> <timeout s>
  local t0=$(date +%s) n
  while :; do
    n=$(estab "$1"); [ "${n:-0}" -ge "$2" ] && { echo "$n after $(( $(date +%s) - t0 )) s"; return 0; }
    [ $(( $(date +%s) - t0 )) -ge "$3" ] && { echo "$n (timeout $3 s)"; return 1; }
    sleep 10
  done
}
host_ok() { docker exec "${PREFIX}DC3-HOST-PROD" ping -c 2 -W 2 10.24.10.1 >/dev/null 2>&1; }

INFRAHUB_STOPPED=0; DEPLOYED=0
finish() {
  if [ "$MODE" = deploy ] && [ "$DEPLOYED" = 1 ] && [ "$KEEP" = 1 ]; then
    step "LIFECYCLE_KEEP=1: lab left running, Infrahub left stopped (docs/LAB-GUI.md)"
    echo "  when done: containerlab destroy -t $TOPO --cleanup ; cd $INFRAHUB_DIR && docker compose -p $INFRAHUB_PROJECT start"
    INFRAHUB_STOPPED=0
  elif [ "$MODE" = deploy ] && [ "$DEPLOYED" = 1 ]; then step "destroy the lab"; containerlab destroy -t "$TOPO" --cleanup >/dev/null 2>&1 && echo "  destroyed"; fi
  if [ "$INFRAHUB_STOPPED" = 1 ]; then step "start Infrahub again (always)"; (cd "$INFRAHUB_DIR" && docker compose -p "$INFRAHUB_PROJECT" start) >/dev/null 2>&1 && echo "  started" || echo "  FAILED: cd $INFRAHUB_DIR && docker compose -p $INFRAHUB_PROJECT start"; fi
  { echo "DC3 node lifecycle test $TS ($MODE, $(basename "$TOPO"))"; echo "containerlab: $CLAB_VER"; printf '%s\n' "${RESULTS[@]}"; } > "$RES/summary.txt"
  echo "===== summary"; cat "$RES/summary.txt"; echo "results: $RES"
}
trap finish EXIT

# ---------------------------------------------------------------- pre-flight
step "pre-flight"
CLAB_VER=$(containerlab version 2>/dev/null | awk '/version:/{print $2; exit}')
echo "  containerlab $CLAB_VER"
python3 - "$CLAB_VER" <<'PY' || { echo "STOP: containerlab >= 0.75 needed for node stop/start/restart (containerlab.dev/rn/0.75)"; exit 2; }
import sys
v = tuple(int(x) for x in (sys.argv[1] or "0.0").split(".")[:2])
sys.exit(0 if v >= (0, 75) else 1)
PY
docker info >/dev/null 2>&1 || { echo "STOP: docker not usable without sudo"; exit 2; }
if [ "$MODE" = deploy ]; then
  if docker ps --format '{{.Names}}' | grep -q '^clab-'; then echo "STOP: another containerlab lab is running"; exit 2; fi
  for d in S1 S2 L1 L2 L11; do [ -s "$LAB/configs/DC3-$d.cfg" ] || { echo "STOP: lab/configs/DC3-$d.cfg missing (render first)"; exit 2; }; done
  step "stop Infrahub ($INFRAHUB_PROJECT: memory)"
  if [ -n "$(cd "$INFRAHUB_DIR" 2>/dev/null && docker compose -p "$INFRAHUB_PROJECT" ps -q 2>/dev/null)" ]; then
    (cd "$INFRAHUB_DIR" && docker compose -p "$INFRAHUB_PROJECT" stop) >/dev/null 2>&1 && INFRAHUB_STOPPED=1
    echo "  stopped (started again at the end)"
  else
    echo "  Infrahub is not running: left as it is (not started at the end)"
  fi
  free -m | awk '/^Mem:/{print "  memory: " $7 " MB available"}'
  step "write the smoke topology, fill secrets, deploy"
  python3 "$REPO/scripts/dc3_lab_topology.py" --subset smoke >/dev/null || exit 3
  python3 "$LAB/dc3_secrets.py" fill "$LAB/configs" > "$RES/secrets-fill.txt" 2>&1
  DEPLOYED=1
  # containerlab post-deploy "channel timeout sending input to device" on a few cEOS: 2 of 4 laptop deploys
  # and one later run; the unchanged retry passed every time, so one retry here
  if ! containerlab deploy -t "$TOPO" --reconfigure > "$RES/deploy.txt" 2>&1; then
    echo "  deploy failed: $(grep -c 'post-deploy' "$RES/deploy.txt") post-deploy errors; destroy and retry once"
    containerlab destroy -t "$TOPO" --cleanup > "$RES/deploy-destroy.txt" 2>&1
    containerlab deploy -t "$TOPO" --reconfigure > "$RES/deploy-retry.txt" 2>&1 || { echo "STOP: deploy failed twice (deploy.txt, deploy-retry.txt)"; tail -5 "$RES/deploy-retry.txt"; exit 3; }
    echo "  retry ok"
  fi
fi
EOS=$(docker ps --format '{{.Names}}' | sed -n "s/^${PREFIX}//p" | grep -E '^DC3-(S|L|BL|SL)[0-9]+$' | sort -V | xargs)
echo "  switches: $EOS"
for n in $EOS; do wait_cli "$n" "$BOOT_TIMEOUT" >/dev/null || { echo "STOP: $n CLI not up"; exit 4; }; done
step "baseline: established sessions per switch (settle up to ${CONVERGE_TIMEOUT} s)"
sleep 60
declare -A BASE
for n in $EOS; do BASE[$n]=$(estab "$n"); echo "  $n ${BASE[$n]}"; done
host_ok && echo "  PROD host reaches 10.24.10.1" || echo "  PROD host does NOT reach 10.24.10.1 before the tests"

# ---------------------------------------------------------------- A: one switch
step "A: containerlab stop --node DC3-L11"
s0=${BASE[DC3-S1]}
clab stop --node DC3-L11 > "$RES/A-stop.txt" 2>&1; echo "  rc $? state $(state DC3-L11)"
sleep 20
s1=$(estab DC3-S1); h=$(host_ok && echo reaches || echo "does not reach")
echo "  S1 sessions $s0 -> $s1; host $h its gateway"
[ "$(state DC3-L11)" = exited ] && [ "$s1" -lt "$s0" ] && result A stop PASS "L11 stopped, S1 sessions $s0 -> $s1, host $h gateway" || result A stop FAIL "state $(state DC3-L11), S1 $s0 -> $s1"
step "A: containerlab start --node DC3-L11"
clab start --node DC3-L11 > "$RES/A-start.txt" 2>&1; echo "  rc $? state $(state DC3-L11)"
bt=$(wait_cli DC3-L11 "$BOOT_TIMEOUT") || bt="no CLI"
hn=$(eos DC3-L11 "show hostname" | awk '/Hostname:/{print $2}')
links=$(docker exec "${PREFIX}DC3-L11" sh -c 'ls /sys/class/net | grep -cE "^eth(1|49|50|51|52)$"')
conv=$(wait_estab DC3-L11 "${BASE[DC3-L11]}" "$CONVERGE_TIMEOUT"); ok=$?
for i in 1 2 3 4 5 6; do host_ok && break; sleep 10; done; h=$(host_ok && echo yes || echo no)
eos DC3-L11 "show ip bgp summary vrf all" > "$RES/A-L11-bgp-after.txt"
[ $ok = 0 ] && [ "$hn" = DC3-L11 ] && [ "$links" -ge 5 ] && [ "$h" = yes ] \
  && result A start PASS "CLI after ${bt} s, hostname $hn (config kept), 5 links present, sessions $conv, host gateway yes" \
  || result A start FAIL "CLI ${bt}, hostname '$hn', links $links of 5, sessions $conv, host gateway $h"

# ---------------------------------------------------------------- E: SSH / eAPI reachability (what the extension's "Connect (SSH)" needs)
step "E: TCP 22 (SSH) and 443 (eAPI) on DC3-L11 Management1 (VRF MGMT) from this machine"
MG=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${PREFIX}DC3-L11")
for port in 22 443; do
  if timeout 5 bash -c "</dev/tcp/$MG/$port" 2>/dev/null; then result E "tcp$port" PASS "$MG:$port answers"
  else result E "tcp$port" INFO "$MG:$port does not answer"; fi
done
eos DC3-L11 "show management ssh" > "$RES/E-L11-management-ssh.txt"

# ---------------------------------------------------------------- B: lab-only host (exec config)
if docker ps -a --format '{{.Names}}' | grep -qx "${PREFIX}DC3-HOST-PROD"; then
  step "B: containerlab stop / start --node DC3-HOST-PROD"
  clab stop --node DC3-HOST-PROD > "$RES/B-stop.txt" 2>&1; echo "  stop rc $? state $(state DC3-HOST-PROD)"
  clab start --node DC3-HOST-PROD > "$RES/B-start.txt" 2>&1; echo "  start rc $? state $(state DC3-HOST-PROD)"
  sleep 5
  addr=$(docker exec "${PREFIX}DC3-HOST-PROD" ip -4 -o addr show dev eth1 2>/dev/null | awk '{print $4}')
  rt=$(docker exec "${PREFIX}DC3-HOST-PROD" ip route show default 2>/dev/null)
  echo "  eth1 ${addr:-no address}; default: ${rt:-none}"
  if [ "$addr" = 10.24.10.10/24 ] && host_ok; then
    result B host PASS "address and route kept, gateway reachable"
  else
    result B host FAIL "after start: eth1 '${addr:-none}', route '${rt:-none}' (exec not run again on start?); re-applying the topology exec lines"
    docker exec "${PREFIX}DC3-HOST-PROD" sh -c 'ip addr add 10.24.10.10/24 dev eth1 2>/dev/null; ip link set eth1 up; ip route replace default via 10.24.10.1 dev eth1'
    for i in 1 2 3; do host_ok && break; sleep 5; done
    host_ok && result B reapply PASS "host usable again after re-applying exec" || result B reapply FAIL "host still not reaching its gateway"
  fi
fi

# ---------------------------------------------------------------- C: MLAG member restart
if [[ " $EOS " == *" DC3-L1 "* ]]; then
  step "C: containerlab restart --node DC3-L1"
  clab restart --node DC3-L1 > "$RES/C-restart.txt" 2>&1; echo "  rc $?"
  bt=$(wait_cli DC3-L1 "$BOOT_TIMEOUT") || bt="no CLI"
  conv=$(wait_estab DC3-L1 "${BASE[DC3-L1]}" "$CONVERGE_TIMEOUT"); ok=$?
  sleep 30
  m1=$(eos DC3-L1 "show mlag" | awk -F: '/^state/{gsub(/ /,"",$2); print $2}')
  m2=$(eos DC3-L2 "show mlag" | awk -F: '/^state/{gsub(/ /,"",$2); print $2}')
  eos DC3-L1 "show mlag" > "$RES/C-L1-mlag-after.txt"
  [ $ok = 0 ] && [ "$m1" = Active ] && [ "$m2" = Active ] \
    && result C restart PASS "CLI after ${bt} s, sessions $conv, MLAG L1 $m1 / L2 $m2" \
    || result C restart FAIL "CLI ${bt}, sessions $conv, MLAG L1 '$m1' / L2 '$m2'"
fi

# ---------------------------------------------------------------- D: whole lab
if [ "$MODE" = deploy ]; then
  step "D: containerlab stop / start of the whole lab"
  clab stop > "$RES/D-stop.txt" 2>&1; echo "  stop rc $?: $(docker ps --format '{{.Names}}' | grep -c "^$PREFIX") containers running"
  clab start > "$RES/D-start.txt" 2>&1; echo "  start rc $?"
  bad=""
  for n in $EOS; do wait_cli "$n" "$BOOT_TIMEOUT" >/dev/null || bad+=" $n(no CLI)"; done
  for n in $EOS; do wait_estab "$n" "${BASE[$n]}" "$CONVERGE_TIMEOUT" >/dev/null || bad+=" $n(sessions $(estab "$n")/${BASE[$n]})"; done
  host_ok || bad+=" host(no gateway: exec config, see B)"
  [ -z "$bad" ] && result D lab PASS "every switch back with all its sessions; host reaches its gateway" || result D lab FAIL "$bad"
fi
exit 0
