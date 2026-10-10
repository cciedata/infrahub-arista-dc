# How the DC3 VXLAN fabric works: control plane and data plane, with captured packets

This page explains how DC3 moves a packet between two hosts across the EVPN/VXLAN fabric. It covers the control plane (BGP
EVPN tells every VTEP where each host and prefix lives) and the data plane (VXLAN carries the packet between VTEPs), using
packets captured in the full lab. `lab/dc3_capture.sh` makes the captures (on the lab host, while the full lab is up:
`sudo bash lab/dc3_capture.sh <out dir>`).

## The two hosts and the path

| Item | Value | Source |
|---|---|---|
| Host A | `DC3-HOST-PROD`, 10.24.10.10/24, gateway 10.24.10.1, VRF PROD, VLAN 1010, on DC3-L11 Ethernet1 | lab/dc3.clab.yml, tests/expected/DC3-L11.cfg |
| Host B | `DC3-HOST-ISP-A`, 198.18.1.10/24, behind carrier ISP-A (outside the fabric) | lab/dc3.clab.yml |
| Leaf of host A | DC3-L11, single leaf, VTEP 10.27.1.11 (Loopback1), router ID 10.27.0.13 (Loopback0), ASN 65382 | tests/expected/DC3-L11.cfg, plans/dc3-values.md |
| Service leaf pair | DC3-SL1/SL2, anycast VTEP 10.27.2.3 (Loopback1 on both), own addresses on Loopback2 (SL1 10.27.1.23) | tests/expected/DC3-SL1.cfg |
| Border leaf pair | DC3-BL1/BL2, anycast VTEP 10.27.2.2 (Loopback1 on both), BL1 own 10.27.1.21 | tests/expected/DC3-BL1.cfg |
| Spines | DC3-S1/S2, ASN 65300, underlay and EVPN route servers; they are not VTEPs | plans/dc3-values.md |
| VNIs | VLAN 1010 = L2 VNI 11010; L3 VNI PROD 50001, TRANSIT 50100 | tests/expected/DC3-L11.cfg, DC3-SL1.cfg |

Why these two: the three test hosts all sit on L11, so traffic between them is switched or routed inside L11 and never
becomes VXLAN. Host A to host B is the path the failure tests also ping. It crosses the fabric twice:

```
DC3-HOST-PROD --VLAN 1010--> L11 (gateway 10.24.10.1, VRF PROD)
   L11 ==VXLAN, L3 VNI 50001 (PROD)==> SL pair (anycast VTEP 10.27.2.3)
      SL --VRF PROD--> Internal FW stand-in --VRF TRANSIT--> SL
   SL ==VXLAN, L3 VNI 50100 (TRANSIT)==> BL pair (anycast VTEP 10.27.2.2)
      BL --> Internet FW stand-in --> ISP router --> carrier ISP-A --> DC3-HOST-ISP-A
```
Tenant VRFs leave the fabric only through the Internal FW, and only TRANSIT reaches the Internet FW, so the packet
changes VRF at the firewall.

## Control plane: how L11 learns where things are

DC3 runs **eBGP for the underlay and eBGP EVPN for the overlay**. Every leaf peers with both spines over its point-to-point
/31s for IPv4 (loopbacks only), and from Loopback0 to both spines' Loopback0 for EVPN (multihop, TTL 3).
The spines pass EVPN routes between leaves without being VTEPs themselves.

EVPN route types that matter here:

| Type | Name | What it says | In DC3 |
|---|---|---|---|
| 2 | MAC/IP advertisement | "MAC m (and IP i) lives behind VTEP v, in L2 VNI x (and L3 VNI y)" | L11 advertises host A's MAC and 10.24.10.10 when it learns them (ARP / first frame) |
| 3 | Inclusive multicast (IMET) | "VTEP v wants BUM traffic for L2 VNI x" (ingress replication flood list) | every VTEP with VLAN 1010 |
| 5 | IP prefix | "prefix p in VRF V is reachable via VTEP v, L3 VNI y" | the SL pair advertises the default route it gets from the Internal FW into PROD; BL advertises TRANSIT prefixes |

Attributes on each route (tests/expected/DC3-L11.cfg):
- **RD** `<Loopback0>:<VNI>`, for example `10.27.0.13:11010` for VLAN 1010 on L11. It keeps routes from different
  leaves distinct.
- **Route target** `65300:<VNI>`, for example `65300:11010` for VLAN 1010 and `65300:50001` for VRF PROD. It decides
  which VLANs and VRFs import the route.
- **Next hop** = the advertising VTEP's Loopback1. The MLAG pairs advertise their anycast address (10.27.2.3 for the SL
  pair), so one VTEP address stands for both members.
- **Extended communities**: the route targets, the tunnel encapsulation (VXLAN), and on type-2 and type-5 routes the
  **router MAC** of the advertising VTEP. That MAC becomes the inner destination MAC in routed (symmetric IRB) traffic.

The gateway is distributed: every leaf answers for 10.24.10.1 with the same anycast MAC `0200.0000.00aa`, so a host
keeps its gateway wherever it is attached.

## Data plane: what the packet looks like

DC3 uses **symmetric IRB**. Traffic between subnets or VRFs is routed on the ingress leaf, sent over the **L3 VNI of the
VRF**, and routed again on the egress VTEP. Bridged traffic inside one VLAN would use the L2 VNI (11010 for VLAN 1010).

One ping from host A to host B, on the first leg:
1. Host A sends ICMP to 198.18.1.10 with destination MAC `0200.0000.00aa` (its gateway) on VLAN 1010.
2. L11 routes in VRF PROD. The best route for 198.18.1.10 is the default route, a type-5 route from the SL pair (next hop
   10.27.2.3, L3 VNI 50001, router MAC of the SL pair).
3. L11 encapsulates the packet:

```
outer Ethernet  L11 uplink MAC -> spine interface MAC
outer IPv4      10.27.1.11 (L11 VTEP) -> 10.27.2.3 (SL anycast VTEP), UDP dst 4789
VXLAN header    VNI 50001 (L3 VNI of PROD)
inner Ethernet  L11 router MAC -> SL router MAC (from the type-5 route)
inner IPv4      10.24.10.10 -> 198.18.1.10, ICMP echo request
```
4. The spine routes the outer packet like any IP packet and never looks inside (ECMP over both spines).
5. The SL pair decapsulates. VNI 50001 maps to VRF PROD, which sends the packet to the Internal FW. The FW returns it in
   VRF TRANSIT, and the SL encapsulates again: outer 10.27.2.3 -> 10.27.2.2, VNI 50100.
6. The BL pair decapsulates into TRANSIT and routes to the Internet FW, then on to the ISP side.

The underlay MTU is 9214, so the 50 bytes of VXLAN overhead never fragment a 1500-byte host packet.

## What the capture shows

Run: the full lab, one capture run of about 2 minutes. The decodes below are from
`tcpdump -r <file> -nn -e -vv` (those pcaps). tcpdump prints EVPN NLRI only as hex ("no AFI 25 / SAFI 70 decoder"), so
the NLRI fields below are read from that hex byte by byte (RFC 7432 section 7.2 layout); Wireshark decodes them directly.

### Control plane: host A's port goes down and comes back

`control-plane-L11-uplinks.pcap`: 40 frames on L11's four uplinks in 75 s, 6 BGP UPDATEs and 16 KEEPALIVEs. The
underlay sessions (10.27.4.x /31s) carry only keepalives. All UPDATEs are on the EVPN sessions from L11 Loopback0
10.27.0.13 to the spines' Loopback0 10.27.0.1 and 10.27.0.2 (TTL 3).

| Time (UTC) | Event | What L11 sent to each spine |
|---|---|---|
| 04:34:22.71 | L11 Ethernet1 shutdown (logged 04:34:22) | UPDATE with only MP_UNREACH_NLRI (AFI 25 / SAFI 70): withdraw of two type-2 routes, RD `10.27.0.13:11010`, MAC `aa:c1:ab:09:17:8d`, one with IP 10.24.10.10 and one MAC-only. Sent to both spines in the same millisecond |
| 04:34:22-04:34:42 | port down | `show bgp evpn route-type mac-ip 10.24.10.10` on L11: empty table (show-L11-during-mac-ip-host.txt) |
| 04:34:43.50 | no shutdown, then host A ARPs for its gateway | two UPDATEs (MP_REACH_NLRI): the same two routes again, next hop 10.27.1.11 (L11 VTEP) |

The re-advertised routes, field by field (hex in the pcap; `show bgp evpn route-type mac-ip 10.24.10.10 detail` agrees):

| Field | MAC/IP route | MAC-only route |
|---|---|---|
| Route type | 2 (MAC/IP advertisement), length 40 | 2, length 33 |
| RD | type 1, `10.27.0.13:11010` | same |
| ESI / Ethernet tag | 0 / 0 (single-homed host) | same |
| MAC | `aa:c1:ab:09:17:8d` (host A's eth1) | same |
| IP | 10.24.10.10 | none |
| Label 1 / label 2 | 11010 (L2 VNI) / 50001 (L3 VNI of PROD) | 11010 only |
| Route targets | `65300:11010`, `65300:50001` | `65300:11010` |
| Tunnel encapsulation | VXLAN | VXLAN |
| Router MAC (ext. community 0x0603) | `00:1c:73:6f:ff:35` (L11) | none |
| AS path | 65382 (L11); the spines add 65300 when they pass it on | same |

So the MAC-only route serves bridging in VLAN 1010 (L2 VNI 11010), and the MAC/IP route also serves routing in VRF PROD
(L3 VNI 50001), with L11's router MAC as the inner destination for routed packets. One host = two type-2 routes. The
spines are not VTEPs: they only pass the routes on.

Type-5 routes (from `show bgp evpn route-type ip-prefix ipv4 detail` on L11, after the run): L11 receives the default
route in two VRFs, `0.0.0.0/0` with RD `10.27.0.25:50001` (SL pair, RT `65300:50001`, next hop 10.27.2.3) and with RD
`10.27.0.23:50100` / `10.27.0.24:50100` (BL pair, RT `65300:50100`, next hop 10.27.2.2, router MAC `02:1c:73:5d:31:8a`).
L11 imports only RT `65300:50001` (it has no TRANSIT VRF), so in VRF PROD the default is
`via VTEP 10.27.2.3 VNI 50001 router-mac 02:1c:73:5f:aa:c6` (show-L11-after-show-ip-route-vrf-PROD-0.0.0.0_0-detail.txt).
The route-target filter is what keeps PROD from going straight to the border leaf, as requires.

Type 3 (IMET) was not sent during the capture: VLAN 1010 stayed up on L11 when Ethernet1 went down, so the flood list did not
change. `show vxlan vtep` before the capture lists 18 remote VTEPs in L11's flood list.

### Data plane: one ping, three VXLAN legs

Host A's ping to 198.18.1.10: 10 sent, 10 received, round trip 5.0/6.7/13.2 ms (host-PROD-ping-ISP-A.txt). Traceroute:
`* > * > 10.27.8.3 (Internal FW) > 10.27.16.1 (SL1, VRF TRANSIT) > * > 10.27.16.11 (Internet FW) > 10.27.14.3 > 10.27.14.2 >
192.0.2.17 > 198.18.1.10`. The `*` hops are L11, the SL pair in PROD, and the BL pair: they are EOS VTEPs in routed VRFs
and do not answer the traceroute probes in this lab (cause not checked).

1. **On the host port** (`data-plane-L11-host-port.pcap`):
   `aa:c1:ab:09:17:8d > 02:00:00:00:00:aa, 10.24.10.10 > 198.18.1.10: ICMP echo request`. The destination MAC is the
   anycast gateway MAC; no VXLAN yet.
2. **Leg 1, L11 to the SL pair** (`data-plane-L11-uplinks.pcap`):
   ```
   outer  aa:c1:ab:ff:46:0d > aa:c1:ab:4c:c4:37   (L11 uplink -> spine)
          10.27.1.11.64659 > 10.27.2.3.4789  UDP, TTL 64, DF
   VXLAN  flags [I] (0x08), vni 50001
   inner  00:1c:73:6f:ff:35 > 02:1c:73:5f:aa:c6   (L11 router MAC -> SL pair router MAC from the type-5 route)
          10.24.10.10 > 198.18.1.10  ICMP echo request, TTL 63 (L11 routed it once)
   ```
   Frame length 148 = 98 (inner frame) + 50 (outer Ethernet 14, IPv4 20, UDP 8, VXLAN 8). The reply comes back
   `10.27.2.3 > 10.27.1.11`, VNI 50001, inner source `00:1c:73:f2:be:3c`, destination L11's router MAC. On the way out L11
   addresses the SL pair's shared router MAC; the reply carries the answering member's own MAC as source (which member
   was not checked). The outer UDP source port varies per flow, so the spines can spread flows over ECMP.
3. **The SL pair** (`data-plane-SL1-uplinks.pcap`): the same packet arrives as `10.27.1.11 > 10.27.2.3 vni 50001` and
   leaves, after the Internal FW, as `10.27.2.3 > 10.27.2.2 vni 50100`. This is where the VRF changes from PROD to TRANSIT.
4. **Leg 2, the SL pair to the BL pair** (`data-plane-BL1-uplinks.pcap`): `10.27.2.3 > 10.27.2.2 vni 50100` in,
   `10.27.2.2 > 10.27.2.3 vni 50100` back. The BL pair then routes in TRANSIT to the Internet FW (10.27.16.11 in the traceroute).

Packet counts in 40 s:

| Capture | Outer flow | VNI | Frames |
|---|---|---|---|
| L11 uplinks | 10.27.1.11 > 10.27.2.3 / back | 50001 | 3137 / 3134 |
| SL1 uplinks | 10.27.1.11 > 10.27.2.3 / back | 50001 | 2550 / 3133 |
| SL1 uplinks | 10.27.2.3 > 10.27.2.2 / back | 50100 | 3131 / 3130 |
| BL1 uplinks | 10.27.2.3 > 10.27.2.2 / back | 50100 | 586 / 3132 |

Only VNIs 50001 and 50100 appear: the path is routed end to end, so L2 VNI 11010 is never used. SL1 and BL1 each see only
part of the traffic towards their pair: both members own the anycast VTEP, and the spines split flows between them by ECMP
(the other member's share was not captured).

Why so many frames for 10 pings: 17 other ping processes were still running on host A, left over from the failure tests
(ICMP ids on the host port; most to 198.18.1.10, some to 198.18.2.10). Stopping the local `docker exec` did not stop the
ping inside the container. `lab/dc3_failtests.py` now also kills it inside the container (`Pinger.stop`).

## Files

| File | What it holds |
|---|---|
| `control-plane-L11-uplinks.pcap` / `.txt` / `-updates.tsv` (tshark decodes; empty in this run, see Known issue) | BGP on L11's four spine uplinks while host A's port is shut for 20 s and restored, then host A ARPs again: type-2 withdraw and re-advertise of 10.24.10.10, the IMET and type-5 routes; full decode and a one-line-per-update table |
| `data-plane-L11-host-port.pcap` / `.txt` | the ICMP frames on L11 Ethernet1, before encapsulation |
| `data-plane-L11-uplinks.pcap` / `.txt` / `.tsv` | VXLAN on L11's uplinks: outer IPs, VNI, inner frame |
| `data-plane-SL1-uplinks.pcap` / … | both VXLAN legs through the service leaf (VNI 50001 in, VNI 50100 out) |
| `data-plane-BL1-uplinks.pcap` / … | VXLAN arriving at the border leaf in VNI 50100 |
| `show-*.txt` | `show bgp evpn ...`, `show vxlan ...`, MAC, ARP and route tables before, during and after |
| `host-PROD-*.txt` | the pings and the traceroute from host A |
| `summary.txt` | what the script did, and counts per file |

Open the `.pcap` files in Wireshark: the filters `bgp.type == 2` (UPDATEs) and `vxlan` show the interesting packets.
Without Wireshark: `tcpdump -r <file> -nn -e -vv`.

Known issue (this run): the tshark decodes (`*.txt`, `*.tsv`) and the counts in `summary.txt` came out empty because tshark,
run as root, refused to open the files. `lab/dc3_capture.sh` now feeds each pcap to tshark on stdin, and
`sudo bash lab/dc3_capture.sh <dir> decode` decodes pcaps that are already there without capturing again.

## Sources
- Arista EOS EVPN/VXLAN: https://www.arista.com/en/um-eos/eos-evpn-overview and https://www.arista.com/en/um-eos/eos-vxlan-configuration
- RFC 7432 (BGP MPLS-based EVPN, route types 2 and 3), RFC 8365 (EVPN with VXLAN), RFC 9135/9136 (integrated routing and bridging; IP prefix route type 5)
- DC3 configs: tests/expected/*.cfg (reviewed renders); design: docs/DESIGN.md
