#!/usr/bin/env bash
# DC3 standalone build: from an EMPTY Infrahub 1.11.2 (this repo's docker-compose.yml) to the 43 rendered files.
# Every write goes to branch main of the standalone instance, and only after scripts/dc3_guard.py has confirmed that the
# instance holds no site other than DC3 and no device outside DC3 (so it never writes into an instance that holds other sites).
#
# Needs: the Infrahub stack of this repo up (docs/BUILD.md step 3), .env filled in, the uv environment (step 2).
# Run from the repo root, inside the uv environment:
#   uv run bash scripts/bootstrap.sh              all steps
#   uv run bash scripts/bootstrap.sh --from 8     resume at step 8 (steps 0 and 3 always run)
#
# Step numbers = docs/BUILD.md. Each step prints PASS or FAIL; the script stops at the first FAIL.
#    0 settings and tools          .env values, infrahubctl.toml, infrahub-sdk 1.23.2
#    3 empty Infrahub              /api/config answers; Infrahub 1.11.2; guard: empty or DC3-only instance
#    4 schema                      schemas/ (check, load, wait); guard with the schema kinds
#    5 objects                     objects/dc3-00 .. dc3-08 in load order
#    6 pools                       scripts/dc3_create_pools.py --standalone --branch main
#    7 generator dry run           DC3_DRY_RUN=1: plan printed, every referenced object found, nothing written
#    8 write + rerun, same ids     DC3_RECONCILE=report: nothing to delete or move; id snapshot, rerun, id snapshot, compare
#    9 host ports                  objects/dc3-10-lab-host-ports.yml (needs DC3-L11 from step 8)
#   10 render                      43 files into lab/configs; lab/dc3_render_check.py; scripts/dc3_render_diff.py
# Logs and snapshots: lab/results/bootstrap-<UTC time>/ (git-ignored). No sudo anywhere.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

BRANCH="main"                                   # standalone instance only; the guard protects every write
SITE="DC3"
INFRAHUB_VERSION="1.11.2"                       # version in docker-compose.yml
SDK_VERSION="1.23.2"                            # version in pyproject.toml
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-600}"          # s, step 3
SCHEMA_CHECK_TRIES="${SCHEMA_CHECK_TRIES:-6}"     # schema check tries on HTTP 500 (fresh instance, step 4)
SCHEMA_CHECK_GAP="${SCHEMA_CHECK_GAP:-30}"        # s between those tries
SCHEMA_WAIT="${SCHEMA_WAIT:-120}"               # s, infrahubctl schema load --wait
FROM=0
if [ "${1:-}" = "--from" ] && [[ "${2:-}" =~ ^[0-9]+$ ]]; then FROM="$2"
elif [ $# -gt 0 ]; then echo "usage: bash scripts/bootstrap.sh [--from STEP]"; exit 2; fi

# Load order (tests/test_dc3_standalone.py reads these two arrays and checks that every reference resolves to an object
# loaded earlier).
SCHEMA_FILES=(
  schemas/base-schemas/dcim.yml
  schemas/base-schemas/ipam.yml
  schemas/base-schemas/location.yml
  schemas/base-schemas/organization.yml
  schemas/locations.yml
  schemas/fabric.yml
  schemas-dc3/ztp.yml
)
OBJECTS_BEFORE_GENERATOR=(
  objects/dc3-00-groups.yml
  objects/dc3-01-bootstrap.yml
  objects/dc3-02-site.yml
  objects/dc3-03-ip-plan.yml
  objects/dc3-04-hardware.yml
  objects/dc3-05-asns.yml
  objects/dc3-06-fabric.yml
  objects/dc3-07-segments.yml
  objects/dc3-08-standins.yml
)
OBJECTS_AFTER_GENERATOR=(
  objects/dc3-10-lab-host-ports.yml
)
# The 43 renders: transform|variable|output file in lab/configs (branch main)
RENDERS=()
for d in DC3-S1 DC3-S2; do RENDERS+=("dc3_spine_config|device=$d|$d.cfg"); done
for n in $(seq 1 20); do RENDERS+=("dc3_leaf_config|device=DC3-L$n|DC3-L$n.cfg"); done
for d in DC3-BL1 DC3-BL2 DC3-SL1 DC3-SL2; do RENDERS+=("dc3_border_service_leaf_config|device=$d|$d.cfg"); done
for d in DC3-EDGE1 DC3-EDGE2 DC3-DMZ1 DC3-DMZ2; do RENDERS+=("dc3_l2_pair_config|device=$d|$d.cfg"); done
RENDERS+=(
  "dc3_frr_internal_fw|device=DC3-INTFW1|DC3-INTFW1.sh"
  "dc3_frr_internet_fw|device=DC3-INETFW1|DC3-INETFW1.sh"
  "dc3_frr_sdwan_fw|device=DC3-SDWANFW1|DC3-SDWANFW1.sh"
  "dc3_frr_branch|device=DC3-SDWANFW1|DC3-SDWANFW1.branch.sh"
  "dc3_frr_vpn_fw|device=DC3-VPNFW1|DC3-VPNFW1.sh"
  "dc3_frr_partner|device=DC3-VPNFW1|DC3-VPNFW1.partner.sh"
  "dc3_frr_isp_router|device=DC3-ISP1|DC3-ISP1.sh"
  "dc3_frr_isp_router|device=DC3-ISP2|DC3-ISP2.sh"
  "dc3_frr_carrier|device=DC3-ISP1|DC3-ISP1.carrier.sh"
  "dc3_frr_carrier|device=DC3-ISP2|DC3-ISP2.carrier.sh"
  "dc3_frr_cloud_router|device=DC3-CLOUD1|DC3-CLOUD1.sh"
  "dc3_frr_cloud_router|device=DC3-CLOUD2|DC3-CLOUD2.sh"
  "dc3_frr_cloud_provider|site=DC3|DC3.cloud-provider.sh"
)
EXPECTED_DEVICES=38                             # 30 Arista switches + 8 FRR stand-ins

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RES="$REPO/lab/results/bootstrap-$TS"
mkdir -p "$RES"
exec > >(tee -a "$RES/bootstrap.log") 2>&1

STEP=""
begin() { STEP="$1"; shift; echo; echo "== step $STEP: $* ($(date -u +%H:%M:%S) UTC)"; }
pass() { echo "PASS  step $STEP: $*"; }
fail() { echo "FAIL  step $STEP: $*"; echo "log: $RES/bootstrap.log (resume after the fix: uv run bash scripts/bootstrap.sh --from $STEP)"; exit 1; }
want() { [ "$FROM" -le "$1" ]; }
guard() {   # $1 = extra option (--schema)
  python3 scripts/dc3_guard.py --branch "$BRANCH" ${1:-} || fail "guard refused: this instance is not a DC3-only instance (nothing written by this step)"
}
run_logged() {   # $1 = log file, rest = command; prints the output, returns the command's exit code
  local log="$1"; shift
  "$@" > "$log" 2>&1
  local rc=$?
  cat "$log"
  return $rc
}

# ---------------------------------------------------------------- 0 settings and tools
begin 0 "settings and tools"
[ -f .env ] || fail ".env missing: cp .env.example .env and replace every CHANGE_ME (docs/BUILD.md step 3)"
declare -A ENV=()
while IFS= read -r line || [ -n "$line" ]; do
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]] || continue
  v="${BASH_REMATCH[2]}"; v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  ENV["${BASH_REMATCH[1]}"]="$v"
done < .env
for k in INFRAHUB_INITIAL_ADMIN_TOKEN INFRAHUB_INITIAL_AGENT_TOKEN INFRAHUB_SECURITY_SECRET_KEY INFRAHUB_INITIAL_ADMIN_PASSWORD \
         INFRAHUB_DB_PASSWORD INFRAHUB_BROKER_PASSWORD INFRAHUB_TASKMANAGER_DB_PASSWORD; do
  val="${ENV[$k]:-}"
  { [ -n "$val" ] && [ "$val" != "CHANGE_ME" ]; } || fail ".env: $k is empty or still CHANGE_ME"
done
PORT="${ENV[DC3_INFRAHUB_PORT]:-8000}"
ADDR="${ENV[DC3_INFRAHUB_ADDRESS]:-}"; [ -n "$ADDR" ] || ADDR="http://localhost:$PORT"
TOKEN="${ENV[DC3_INFRAHUB_API_TOKEN]:-}"; [ -n "$TOKEN" ] || TOKEN="${ENV[INFRAHUB_INITIAL_ADMIN_TOKEN]}"
export INFRAHUB_ADDRESS="$ADDR" INFRAHUB_API_TOKEN="$TOKEN"
if [ -f infrahubctl.toml ]; then
  toml_addr="$(python3 -c 'import tomllib; print(tomllib.load(open("infrahubctl.toml","rb")).get("server_address",""))' 2>/dev/null)"
  [ "$toml_addr" = "$ADDR" ] || fail "infrahubctl.toml server_address '$toml_addr' differs from $ADDR (fix or remove infrahubctl.toml)"
fi
command -v infrahubctl >/dev/null || fail "infrahubctl not found: run inside the uv environment (uv sync; uv run bash scripts/bootstrap.sh)"
command -v curl >/dev/null || fail "curl not found"
sdk="$(python3 -c 'import importlib.metadata as m; print(m.version("infrahub-sdk"))' 2>/dev/null)"
[ "$sdk" = "$SDK_VERSION" ] || fail "infrahub-sdk '$sdk' found, $SDK_VERSION expected (uv sync)"
pass "address $ADDR, branch $BRANCH, infrahub-sdk $sdk, results $RES"

# ---------------------------------------------------------------- 3 empty Infrahub: health, version, guard (always run)
begin 3 "empty Infrahub: health, version, guard"
t0=$(date +%s)
until curl -sf -o /dev/null "$ADDR/api/config"; do
  [ $(( $(date +%s) - t0 )) -ge "$HEALTH_TIMEOUT" ] && fail "$ADDR/api/config does not answer after ${HEALTH_TIMEOUT} s (docker compose ps)"
  sleep 5
done
ver="$(python3 - <<'EOF'
import asyncio
from infrahub_sdk import InfrahubClient
async def v():
    r = await InfrahubClient().execute_graphql(query="query { InfrahubInfo { version } }")
    print(r["InfrahubInfo"]["version"])
asyncio.run(v())
EOF
)" || fail "version query failed (token? $ADDR)"
[ "$ver" = "$INFRAHUB_VERSION" ] || fail "Infrahub $ver answers at $ADDR, $INFRAHUB_VERSION expected"
guard
pass "$ADDR answers after $(( $(date +%s) - t0 )) s, Infrahub $ver, no site other than $SITE, no device outside $SITE"

# ---------------------------------------------------------------- 4 schema
if want 4; then
  begin 4 "schema (${#SCHEMA_FILES[@]} files)"
  guard
  # a fresh instance can answer HTTP 500 on the first schema check for a short time after it reports healthy (seen on a
  # rebuild: "Schema 'DcimDeviceType' on branch main has incorrect hash"; it passed 90 s later). The check writes
  # nothing, so it is retried: SCHEMA_CHECK_TRIES times, SCHEMA_CHECK_GAP s apart; any other error fails at once.
  try=1
  until run_logged "$RES/04-schema-check.log" infrahubctl schema check "${SCHEMA_FILES[@]}" --branch "$BRANCH"; do
    grep -q "500 Internal Server Error" "$RES/04-schema-check.log" && [ "$try" -lt "$SCHEMA_CHECK_TRIES" ] || fail "infrahubctl schema check"
    echo "schema check: HTTP 500 from a fresh instance, try $try of $SCHEMA_CHECK_TRIES; again in $SCHEMA_CHECK_GAP s"
    try=$((try + 1)); sleep "$SCHEMA_CHECK_GAP"
  done
  run_logged "$RES/04-schema-load.log" infrahubctl schema load "${SCHEMA_FILES[@]}" --branch "$BRANCH" --wait "$SCHEMA_WAIT" \
    || fail "infrahubctl schema load"
  guard --schema
  pass "schema loaded, every required kind present"
fi

# ---------------------------------------------------------------- 5 objects
if want 5; then
  begin 5 "objects (${#OBJECTS_BEFORE_GENERATOR[@]} files in load order)"
  guard --schema
  for f in "${OBJECTS_BEFORE_GENERATOR[@]}"; do
    echo "-- $f"
    run_logged "$RES/05-$(basename "$f" .yml).log" infrahubctl object load "$f" --branch "$BRANCH" || fail "infrahubctl object load $f"
  done
  pass "${#OBJECTS_BEFORE_GENERATOR[@]} object files loaded"
fi

# ---------------------------------------------------------------- 6 pools
if want 6; then
  begin 6 "pools"
  run_logged "$RES/06-pools.log" python3 scripts/dc3_create_pools.py --standalone --branch "$BRANCH" || fail "scripts/dc3_create_pools.py"
  n=$(grep -c '^CoreIP.*Pool ' "$RES/06-pools.log")
  [ "$n" = 7 ] || fail "$n pools reported, 7 expected"
  pass "7 pools (DC3-*)"
fi

# ---------------------------------------------------------------- 7 generator dry run
if want 7; then
  begin 7 "generator dry run"
  guard --schema
  DC3_DRY_RUN=1 run_logged "$RES/07-dry-run.log" infrahubctl generator dc3_site --branch "$BRANCH" "site=$SITE" || fail "generator dry run"
  grep -q "DRY RUN: nothing written" "$RES/07-dry-run.log" || fail "no 'DRY RUN: nothing written' line (see the log)"
  grep -q "Traceback" "$RES/07-dry-run.log" && fail "Python traceback in the dry run"
  pass "plan printed, every referenced object found, nothing written"
fi

# ---------------------------------------------------------------- 8 generator write, rerun with identical ids
check_generator_log() {   # $1 = log
  grep -q "Traceback" "$1" && fail "Python traceback in the generator run"
  grep -q "reconcile (report) done" "$1" || fail "the generator did not finish (no 'reconcile (report) done')"
  grep -qE "^(WOULD DELETE|DELETED|MOVED) " "$1" && fail "the reconciler reports deletes or moves on a fresh build (see the log)"
  return 0
}
if want 8; then
  begin 8 "generator write, rerun with identical ids"
  guard --schema
  DC3_RECONCILE=report run_logged "$RES/08-write.log" infrahubctl generator dc3_site --branch "$BRANCH" "site=$SITE" || fail "generator write"
  check_generator_log "$RES/08-write.log"
  python3 scripts/dc3_id_snapshot.py --branch "$BRANCH" --out "$RES/ids-1.json" || fail "id snapshot 1"
  devs=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['devices']))" "$RES/ids-1.json")
  [ "$devs" = "$EXPECTED_DEVICES" ] || fail "$devs devices in $SITE, $EXPECTED_DEVICES expected"
  guard --schema
  DC3_RECONCILE=report run_logged "$RES/08-rerun.log" infrahubctl generator dc3_site --branch "$BRANCH" "site=$SITE" || fail "generator rerun"
  check_generator_log "$RES/08-rerun.log"
  python3 scripts/dc3_id_snapshot.py --branch "$BRANCH" --out "$RES/ids-2.json" || fail "id snapshot 2"
  python3 scripts/dc3_id_snapshot.py --compare "$RES/ids-1.json" "$RES/ids-2.json" || fail "the rerun changed ids (lines above)"
  pass "$devs devices written, nothing to delete or move; the rerun keeps every id (devices, interfaces, addresses, pairs, attachments, prefixes)"
fi

# ---------------------------------------------------------------- 9 host ports
if want 9; then
  begin 9 "host ports"
  guard --schema
  for f in "${OBJECTS_AFTER_GENERATOR[@]}"; do
    run_logged "$RES/09-$(basename "$f" .yml).log" infrahubctl object load "$f" --branch "$BRANCH" || fail "infrahubctl object load $f"
  done
  pass "DC3-L11 Ethernet1-3 (test hosts PROD, DEV, CITRIX)"
fi

# ---------------------------------------------------------------- 10 render
if want 10; then
  begin 10 "render ${#RENDERS[@]} files"
  mkdir -p lab/configs
  rm -f lab/configs/*.cfg lab/configs/*.sh
  for r in "${RENDERS[@]}"; do
    IFS='|' read -r transform var out <<< "$r"
    infrahubctl render "$transform" "$var" --branch "$BRANCH" --out "lab/configs/$out" > "$RES/10-render-$out.log" 2>&1 \
      || { cat "$RES/10-render-$out.log"; fail "infrahubctl render $transform $var"; }
    [ -s "lab/configs/$out" ] || fail "lab/configs/$out is empty"
  done
  n=$(ls lab/configs/*.cfg lab/configs/*.sh 2>/dev/null | wc -l)
  [ "$n" = 43 ] || fail "$n files in lab/configs, 43 expected"
  python3 lab/dc3_render_check.py --configs lab/configs --json "$RES/10-render-check.json" > "$RES/10-render-check.txt" 2>&1
  rc=$?
  grep -E "^(# dc3_render_check|counts:)" "$RES/10-render-check.txt"
  [ "$rc" = 0 ] || fail "lab/dc3_render_check.py exit $rc (blocker or major findings: $RES/10-render-check.txt)"
  python3 scripts/dc3_render_diff.py lab/configs | tee "$RES/10-render-diff.txt"
  [ "${PIPESTATUS[0]}" = 0 ] || fail "renders differ from tests/expected beyond tests/expected-delta.diff"
  pass "43 renders in lab/configs; render check without blocker / major; equal to tests/expected + recorded delta"
fi

echo
echo "DONE: standalone DC3 built on $ADDR branch $BRANCH. Renders in lab/configs. Log: $RES/bootstrap.log"
