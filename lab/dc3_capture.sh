#!/usr/bin/env bash
# DC3 VXLAN/EVPN capture: control plane and data plane between two lab hosts, with the matching show outputs.
# Runs on the lab host while the full lab is up: sudo bash lab/dc3_capture.sh <out dir>.
# docs/VXLAN-EVPN-CAPTURE.md explains what the files show.
#
# Hosts: DC3-HOST-PROD (10.24.10.10, VRF PROD, behind DC3-L11 Ethernet1) and DC3-HOST-ISP-A (198.18.1.10, behind the ISP side).
# Path PROD -> ISP-A: L11 --VXLAN, L3 VNI of PROD--> service leaf -> Internal FW -> service leaf --VXLAN, L3 VNI of TRANSIT-->
# border leaf -> Internet FW -> ISP router -> carrier -> host. The three test hosts all sit on L11, so traffic between them
# never leaves the leaf; this path crosses the fabric twice.
#
# Captures (pcap, tcpdump inside each container's network namespace via nsenter, so cEOS needs no tools):
#   control-plane-L11-uplinks.pcap   TCP 179 on L11's four spine uplinks while the PROD host's port is bounced (type-2 withdraw
#                                    and re-advertise) and the host ARPs again
#   data-plane-L11-host-port.pcap    ICMP on L11 Ethernet1 (the frame before encapsulation)
#   data-plane-L11-uplinks.pcap      UDP 4789 on L11's uplinks (VXLAN, L3 VNI of PROD)
#   data-plane-SL1-uplinks.pcap      UDP 4789 on SL1's uplinks (both VXLAN legs through the service leaf)
#   data-plane-BL1-uplinks.pcap      UDP 4789 on BL1's uplinks (VXLAN, L3 VNI of TRANSIT)
# Decoded copies (*.txt, tshark -V) and show outputs (show-*.txt) next to them; summary.txt lists what was seen.
set -uo pipefail
OUT=${1:?usage: dc3_capture.sh <output dir> [decode]}
cd "$(dirname "$0")/.." || exit 1
mkdir -p "$OUT"
P=clab-dc3-
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT/summary.txt"; }
pid() { docker inspect -f '{{.State.Pid}}' "$P$1"; }
eos() { docker exec "$P$1" Cli -p 15 -c "$2"; }
uplinks() {  # Linux names of a switch's spine uplinks, from its rendered config (EthernetN/1 described "DC3-S..." -> ethN)
  awk '/^interface Ethernet[0-9]+\/1$/{i=$2} /^   description DC3-S[0-9]/{if(i){sub("Ethernet","",i); sub("/1","",i); print "eth" i}} /^!/{i=""}' "lab/configs/$1.cfg"
}
cap() {  # cap <node> <file> <filter> <iface...>: one tcpdump per interface in the node's namespace, merged later
  local node=$1 file=$2 filter=$3; shift 3
  # tcpdump writes to stdout and the shell writes the file: Ubuntu's AppArmor profile for tcpdump refuses files in a home
  # folder (every pcap was empty); its messages go to tcpdump-<file>-<iface>.log
  for i in "$@"; do
    nsenter -t "$(pid "$node")" -n timeout "${CAP_S:-90}" tcpdump -i "$i" -s 0 -U -w - "$filter" \
      > "$OUT/.$file.$i.pcap" 2> "$OUT/tcpdump-$file-$i.log" &
  done
}
# tshark refuses to open files as root when its dumpcap is not setuid ("You don't have permission to read the file"),
# so it reads each pcap from stdin: ts <pcap> <tshark args...>
ts() { local f=$1; shift; tshark -r - "$@" < "$f"; }
merge() { mergecap -w "$OUT/$1.pcap" "$OUT"/."$1".*.pcap 2>/dev/null && rm -f "$OUT"/."$1".*.pcap; }

decode() {  # tshark decodes and the summary; "dc3_capture.sh <dir> decode" runs only this, on pcaps already there
  log "decode (tshark)"
  ts "$OUT/control-plane-L11-uplinks.pcap" -Y 'bgp.type == 2' -V -o tcp.desegment_tcp_streams:TRUE > "$OUT/control-plane-L11-uplinks.txt" 2>&1
  ts "$OUT/control-plane-L11-uplinks.pcap" -Y 'bgp.type == 2' -T fields -E header=y -E separator=' | ' \
    -e frame.time_relative -e ip.src -e ip.dst -e bgp.mp_reach_nlri_ipv4_prefix -e bgp.evpn.nlri.rt -e bgp.evpn.nlri.mac_addr \
    -e bgp.evpn.nlri.ipv4_addr -e bgp.evpn.nlri.prefix_ip_v4 -e bgp.mp_unreach_nlri_afi -e bgp.update.path_attribute.extended_communities \
    > "$OUT/control-plane-L11-updates.tsv" 2>&1
  for f in data-plane-L11-host-port data-plane-L11-uplinks data-plane-SL1-uplinks data-plane-BL1-uplinks; do
    ts "$OUT/$f.pcap" -c 4 -V > "$OUT/$f.txt" 2>&1
    ts "$OUT/$f.pcap" -T fields -E header=y -E separator=' | ' -e frame.time_relative -e ip.src -e ip.dst -e vxlan.vni \
      -e eth.src -e eth.dst -e icmp.type > "$OUT/$f.tsv" 2>&1
  done

  log "summary"
  {
    echo "control plane: $(ts "$OUT/control-plane-L11-uplinks.pcap" -Y 'bgp.type == 2' 2>/dev/null | wc -l) BGP UPDATE frames;" \
         "EVPN route types seen: $(ts "$OUT/control-plane-L11-uplinks.pcap" -Y bgp.evpn.nlri.rt -T fields -e bgp.evpn.nlri.rt 2>/dev/null | tr ',' '\n' | sort | uniq -c | xargs)"
    for f in data-plane-L11-host-port data-plane-L11-uplinks data-plane-SL1-uplinks data-plane-BL1-uplinks; do
      echo "$f: $(ts "$OUT/$f.pcap" 2>/dev/null | wc -l) frames; VNIs: $(ts "$OUT/$f.pcap" -T fields -e vxlan.vni 2>/dev/null | sort | uniq -c | xargs)"
    done
  } | tee -a "$OUT/summary.txt"
}

log "tools"
command -v tshark >/dev/null || { echo "wireshark-common wireshark-common/install-setuid boolean false" | debconf-set-selections
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q tcpdump tshark >/dev/null 2>&1; }
command -v tcpdump >/dev/null && command -v tshark >/dev/null && command -v mergecap >/dev/null || { log "STOP: tcpdump/tshark missing"; exit 1; }
if [ "${2:-}" = decode ]; then decode; chmod -R a+rX "$OUT"; log "done (decode only): $OUT"; exit 0; fi
L11_UP=$(uplinks DC3-L11 | xargs); SL1_UP=$(uplinks DC3-SL1 | xargs); BL1_UP=$(uplinks DC3-BL1 | xargs)
log "uplinks L11: $L11_UP | SL1: $SL1_UP | BL1: $BL1_UP"

log "show outputs before"
for c in "show bgp evpn summary" "show bgp evpn route-type mac-ip" "show bgp evpn route-type ip-prefix ipv4" "show vxlan vtep" \
         "show vxlan vni" "show interfaces Vxlan1" "show mac address-table vlan 1010" "show vxlan address-table" "show ip route vrf PROD" \
         "show ip arp vrf PROD"; do
  f=$(echo "$c" | tr ' ' '-'); eos DC3-L11 "$c" > "$OUT/show-L11-before-$f.txt" 2>&1
done
eos DC3-SL1 "show bgp evpn route-type ip-prefix ipv4" > "$OUT/show-SL1-before-evpn-type5.txt" 2>&1
eos DC3-BL1 "show ip route vrf TRANSIT" > "$OUT/show-BL1-before-route-TRANSIT.txt" 2>&1

log "control plane: capture TCP 179 on L11 uplinks; bounce L11 Ethernet1 (PROD host port), then the host ARPs and pings again"
CAP_S=75 cap DC3-L11 control-plane-L11-uplinks "tcp port 179" $L11_UP
sleep 5
eos DC3-L11 $'configure\ninterface Ethernet1\nshutdown\nend' >/dev/null; log "  L11 Ethernet1 shutdown (expect: type-2 MAC/IP of 10.24.10.10 withdrawn)"
sleep 20
eos DC3-L11 "show bgp evpn route-type mac-ip 10.24.10.10" > "$OUT/show-L11-during-mac-ip-host.txt" 2>&1
eos DC3-L11 $'configure\ninterface Ethernet1\nno shutdown\nend' >/dev/null; log "  L11 Ethernet1 no shutdown"
sleep 10
docker exec "${P}DC3-HOST-PROD" sh -c 'ip neigh flush dev eth1; ping -c 3 -W 2 10.24.10.1' > "$OUT/host-PROD-ping-gateway.txt" 2>&1
log "  host PROD pinged its gateway (expect: type-2 MAC/IP re-advertised)"
wait; merge control-plane-L11-uplinks

log "data plane: capture ICMP on L11 Ethernet1 and UDP 4789 on L11, SL1 and BL1 uplinks; PROD host pings 198.18.1.10 (DC3-HOST-ISP-A)"
CAP_S=40 cap DC3-L11 data-plane-L11-host-port "icmp" eth1
CAP_S=40 cap DC3-L11 data-plane-L11-uplinks "udp port 4789" $L11_UP
CAP_S=40 cap DC3-SL1 data-plane-SL1-uplinks "udp port 4789" $SL1_UP
CAP_S=40 cap DC3-BL1 data-plane-BL1-uplinks "udp port 4789" $BL1_UP
sleep 5
docker exec "${P}DC3-HOST-PROD" ping -c 10 -i 0.5 -W 2 198.18.1.10 > "$OUT/host-PROD-ping-ISP-A.txt" 2>&1
docker exec "${P}DC3-HOST-PROD" traceroute -n -q 1 -w 2 198.18.1.10 > "$OUT/host-PROD-traceroute-ISP-A.txt" 2>&1
log "  ping: $(tail -2 "$OUT/host-PROD-ping-ISP-A.txt" | head -1)"
wait
for f in data-plane-L11-host-port data-plane-L11-uplinks data-plane-SL1-uplinks data-plane-BL1-uplinks; do merge $f; done

log "show outputs after"
for c in "show bgp evpn route-type mac-ip 10.24.10.10 detail" "show bgp evpn route-type ip-prefix ipv4 detail" "show vxlan address-table" \
         "show ip route vrf PROD 0.0.0.0/0 detail" "show ip arp vrf PROD"; do
  f=$(echo "$c" | tr ' ' '-' | tr '/' '_'); eos DC3-L11 "$c" > "$OUT/show-L11-after-$f.txt" 2>&1
done

decode
chmod -R a+rX "$OUT"
log "done: $OUT"
