#!/bin/bash
# DC3 ZTP laptop test: run-ztp.sh prep | start <device> | status [n] | stop
# Runs one cEOS-lab container with no startup-config on a lab bridge in WSL (no sudo: docker group, as the smoke test).
#   prep            pull the Kea image; check the cEOS image, the mapping file and ~/.dc3/secrets.env
#   start <device>  $DOCKER network dc3-ztp (bridge br-dc3-ztp, 10.250.32.0/24, host address .1 = Management1 gateway),
#                   Kea DHCP (pool .200-.239, option 67 http://10.250.32.1:8080/bootstrap), the ZTP server, and one cEOS
#                   container whose serial is the device's lab serial (ceos-config SERIALNUMBER) and whose ports use the
#                   device model's mapping file; no startup-config, so EOS ZTP runs (first cEOS run)
#   status [n]      last n server events, Kea log, show zerotouch, the switch's ZTP log
#   stop            remove the container, Kea, the server and the network
# Needs: Infrahub reachable at DC3_INFRAHUB_ADDRESS (default http://localhost:8000; Infrahub keeps running), ~/.dc3/secrets.env
# (DC3_ZTP_CALLBACK_TOKEN from ztp/setup_account.py, DC3_AUTOMATION_HASH/PASSWORD from scripts/dc3_make_secret.sh), infrahubctl
# in PATH (or INFRAHUBCTL), the device's serial recorded with ztp/set_serial.py. Cannot run while the smoke or full lab is up
# (its management network uses the same /24).
set -u
Z=$(cd "$(dirname "$0")" && pwd); REPO=$(dirname "$Z")
W=${DC3_ZTP_WORK:-$HOME/.dc3-ztp}; NET=dc3-ztp; BR=br-dc3-ztp; SRV_IP=10.250.32.1; SRV_PORT=8080; CEOS_IP=${CEOS_IP:-10.250.32.199}
CEOS_IMAGE=${CEOS_IMAGE:-ceos:4.36.2F}; KEA=docker.cloudsmith.io/isc/docker/kea-dhcp4:2.6.3; NODE=${ZTP_NODE:-dc3-ztp-ceos}
DOCKER=${DOCKER:-docker}   # docker command (default docker)
export DC3_INFRAHUB_ADDRESS=${DC3_INFRAHUB_ADDRESS:-http://localhost:8000}
PY=${PYTHON:-python3}
model_of() {  # model of a device from its name (plans/dc3-values.md)
  case "$1" in DC3-S*) echo DCS-7060DX5-64S ;; DC3-BL*|DC3-SL*) echo DCS-7280DR3-24 ;; DC3-EDGE*) echo DCS-7050DX4-32S ;;
               DC3-L*|DC3-DMZ*) echo DCS-7050SX3-48YC8C ;; *) echo "" ;; esac; }
case "${1:-}" in
prep)
  $DOCKER image inspect "$CEOS_IMAGE" >/dev/null 2>&1 && echo "cEOS image: $CEOS_IMAGE" || { echo "STOP: no image $CEOS_IMAGE"; exit 1; }
  $DOCKER pull -q $KEA | tail -1
  ls "$REPO"/lab/intf-mapping/*.json | sed 's/^/mapping: /'
  command -v "${INFRAHUBCTL:-infrahubctl}" >/dev/null && echo "infrahubctl: $(command -v "${INFRAHUBCTL:-infrahubctl}")" \
    || echo "infrahubctl: MISSING (PATH=\"$REPO/.venv/bin:\$PATH\" bash ztp/run-ztp.sh ...)"
  for k in DC3_ZTP_CALLBACK_TOKEN DC3_AUTOMATION_HASH DC3_AUTOMATION_PASSWORD; do
    grep -q "^$k=" "${DC3_SECRETS:-$HOME/.dc3/secrets.env}" 2>/dev/null && echo "secret $k: set" || echo "secret $k: MISSING"; done
  ;;
start)
  DEV=${2:?usage: run-ztp.sh start <device>}; M=$(model_of "$DEV"); [ -n "$M" ] || { echo "STOP: $DEV is not a DC3 switch"; exit 1; }
  if ip -4 route show | grep -v "$BR" | grep -q '^10\.250\.32\.0/24 '; then
    echo "STOP: 10.250.32.0/24 is already in use on this host (smoke or full lab up?)"; exit 1; fi
  mkdir -p "$W"
  SN=$(cd "$REPO" && $PY - "$DEV" <<'PY'
import json, os, sys, urllib.request
sys.path.insert(0, "lab"); import dc3_secrets
q = '{ DcimDevice(name__value: "%s") { edges { node { serial_number { value } } } } }' % sys.argv[1]
req = urllib.request.Request(os.environ["DC3_INFRAHUB_ADDRESS"] + "/graphql/" + os.environ.get("DC3_ZTP_BRANCH", "main"),
    data=json.dumps({"query": q}).encode(), headers={"Content-Type": "application/json",
    "X-INFRAHUB-KEY": dc3_secrets.load().get("DC3_ZTP_CALLBACK_TOKEN", "")})
e = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]["DcimDevice"]["edges"]
print(e[0]["node"]["serial_number"]["value"] or "" if e else "")
PY
)
  [ -n "$SN" ] || { echo "STOP: $DEV has no serial_number (ztp/set_serial.py $DEV <serial>)"; exit 1; }
  echo "device $DEV model $M serial $SN"
  if [ "${SKIP_INFRA:-0}" != 1 ]; then   # run-ztp-all.sh starts the network, Kea and the server once, then only the switches
  $DOCKER network inspect $NET >/dev/null 2>&1 || $DOCKER network create $NET --subnet 10.250.32.0/24 --gateway $SRV_IP \
    -o com.docker.network.bridge.name=$BR >/dev/null && echo "network $NET ($BR, $SRV_IP/24)"
  $DOCKER rm -f dc3-ztp-kea >/dev/null 2>&1
  $DOCKER run -d --name dc3-ztp-kea --network host --cap-add NET_ADMIN --cap-add NET_RAW \
    -v "$Z/kea-dhcp4.json:/etc/kea/kea-dhcp4.conf:ro" $KEA >/dev/null && echo "kea started"
  curl -s -o /dev/null -w "Infrahub at $DC3_INFRAHUB_ADDRESS: http %{http_code}\n" "$DC3_INFRAHUB_ADDRESS/api/config"
  pkill -f "ztp_server.py $SRV_IP" ; nohup $PY "$Z/ztp_server.py" $SRV_IP $SRV_PORT > "$W/server.log" 2>&1 &
  sleep 2; curl -s "http://$SRV_IP:$SRV_PORT/bootstrap" | head -2
  fi
  $DOCKER rm -f $NODE >/dev/null 2>&1
  ENVS="INTFTYPE=eth ETBA=1 CEOS=1 EOS_PLATFORM=ceoslab container=docker MGMT_INTF=eth0"
  # own empty flash folder on the host (as containerlab does): no startup-config and no zerotouch-config, so EOS ZTP is not
  # switched off by a file from the image ("ZeroTouch Mode: Disabled" without this)
  # a new folder per run: files EOS writes there belong to root and cannot be removed without sudo
  F="$W/flash/$DEV-$(date -u +%Y%m%dT%H%M%S)"; mkdir -p "$F"; ln -sfn "$F" "$W/flash-current"
  printf 'SERIALNUMBER=%s\nSYSTEMMACADDR=%s\n' "$SN" "${CEOS_MAC:-02:dc:30:00:00:01}" > "$F/ceos-config"
  cp "$REPO/lab/intf-mapping/$M.json" "$F/EosIntfMapping.json"
  $DOCKER create --name $NODE --hostname $NODE --privileged --network $NET --ip $CEOS_IP -v "$F:/mnt/flash" \
    $(for e in $ENVS; do printf -- '-e %s ' "$e"; done) "$CEOS_IMAGE" /sbin/init \
    $(for e in $ENVS; do printf 'systemd.setenv=%s ' "$e"; done) >/dev/null
  $DOCKER start $NODE >/dev/null && echo "cEOS $NODE started $(date -u +%T) (no startup-config: ZTP)"
  ;;
status)
  echo "== server events"; tail -n "${2:-15}" "$Z/ztp-events.jsonl" 2>/dev/null
  echo "== kea"; $DOCKER logs --tail 5 dc3-ztp-kea 2>&1 | cut -c1-200
  echo "== show zerotouch"; $DOCKER exec $NODE Cli -p 15 -c "show zerotouch" 2>&1 | tail -5
  echo "== switch ZTP log"; tail -5 "$W/flash-current/dc3_ztp.log" 2>/dev/null
  echo "== flash"; ls -la "$W/flash-current/" 2>/dev/null; cat "$W/flash-current/zerotouch-config" 2>/dev/null
  echo "== hostname"; $DOCKER exec $NODE Cli -p 15 -c "show hostname" 2>&1 | tail -2
  ;;
stop)
  $DOCKER rm -f $NODE dc3-ztp-kea >/dev/null 2>&1; pkill -f "ztp_server.py $SRV_IP"; $DOCKER network rm $NET >/dev/null 2>&1
  echo stopped ;;
*) sed -n '2,15p' "$0"; exit 1 ;;
esac
