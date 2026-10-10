# DC3 values

DC3 is a standalone Arista EVPN/VXLAN datacenter. Fabric rules: eBGP underlay and overlay, ASN rule, RTs site ASN:VNI, MTU 9214, MLAG with VLAN 3900, keepalive ports (..), port map, MLAG reload-delay 360, manual ESI, no DF command, link tracking on ESI leaves, hardware models (..).

## Switches: 30
| Role | Devices | Model |
|---|---|---|
| Spine | DC3-S1, DC3-S2 | DCS-7060DX5-64S |
| Border leaf (MLAG) | DC3-BL1, DC3-BL2 | DCS-7280DR3-24 |
| Service leaf (MLAG) | DC3-SL1, DC3-SL2 | DCS-7280DR3-24 |
| Server leaf | DC3-L1..DC3-L20: MLAG pair L1-L2, ESI pairs L3-L10, single L11-L20 | DCS-7050SX3-48YC8C |
| DMZ (MLAG, no BGP) | DC3-DMZ1, DC3-DMZ2 | DCS-7050SX3-48YC8C |
| Edge (MLAG, L2, no BGP) | DC3-EDGE1, DC3-EDGE2 | DCS-7050DX4-32S |
No BGW, no DCI.

## ASNs (order by role)
| Device(s) | ASN |
|---|---|
| Spines S1, S2 | 65300 |
| BL1-BL2 (MLAG) | 65371 |
| SL1-SL2 (MLAG) | 65372 |
| L1-L2 (MLAG) | 65373 |
| L3 .. L10 (ESI members) | 65374 .. 65381 |
| L11 .. L20 (single) | 65382 .. 65391 |
Spare 65392-65399. RTs 65300:<VNI>.

## Outside the fabric: FRR stand-ins, one per role
| Stand-in | ASN |
|---|---|
| Internal FW | 65310 |
| SD-WAN FW (hub, overlay 65400, local-as 65320 to the fabric) + 2 branch stand-ins | 65320 / 65400 |
| VPN-partner FW + 1 partner stand-in | 65330 |
| Internet FW | 65340 |
| 2 ISP routers | 65050 (carriers ISP-A 64496, ISP-B 64497) |
| 2 cloud routers + AWS / Azure stand-ins | 65370 (AWS 65500, Azure 12076) |
Offset rule: site block + role offset: fabric +00, Internal +10, SD-WAN +20, VPN +30, Internet +40, cloud routers +70.

## Addressing
- Tenants 10.24.0.0/16, infrastructure 10.27.0.0/16 (e.g. TRANSIT 10.27.16.0/24, VPC-BACKUP 10.27.3.0/24). Pools in objects/dc3-03-ip-plan.yml.
- OOB management 10.250.32.0/20.
- Public (lab-only) 198.18.3.0/24.

## Tenants
VRFs PROD, DEV, CITRIX, PARTNER, REPLICATION, TRANSIT with their VNIs and segments (objects/dc3-06-fabric.yml, objects/dc3-07-segments.yml).

## BFD
999 ms x 3 on every BFD session.

## Code location
EOS templates, queries and the DC3 generator live in this repository, registered in Infrahub as a read-only repository.

## Template values
- MLAG heartbeat-interval: 4 s, written heartbeat-interval 4000 (ms)
- MLAG dual-primary detection delay and action: dual-primary detection delay 5 action errdisable all-interfaces
- MLAG dual-primary recovery delay: mlag 360, non-mlag 300 (non-MLAG must be less than MLAG)
- Link tracking group name (ESI leaves): link tracking group SPINE-UPLINKS; upstream = spine uplinks, downstream = ES port-channels
- Link tracking recovery delay: recovery delay 360 s (range 0-3600; matches reload-delay 360)
- Link tracking links minimum: links minimum 1: ES ports go down only when every spine uplink is down
- cEOS interface names: map containerlab ethN to the hardware names with cEOS interface mapping (cEOS >= 4.28.0F), so lab configs equal hardware configs

## Build and template values
- How DC3 devices are created in Infrahub: generator dc3_site in this repository
- MLAG domain-id per pair: DC3-MLAG-L1-L2, DC3-MLAG-BL1-BL2, DC3-MLAG-SL1-SL2, DC3-MLAG-DMZ1-DMZ2, DC3-MLAG-EDGE1-EDGE2
- MLAG peer-link port-channel number: Port-Channel1000
- Infrahub repository type: read-only repository (never writes to Git)
- Name prefix for the DC3 Infrahub objects: dc3_ (names stay unique in the instance)
- Laptop memory for the cEOS smoke test: stop Infrahub during the smoke test, start it after (WSL ~7 GB, ~0 free with Infrahub running)
- Loopback numbering: Loopback0 router-ID/EVPN, Loopback1 VTEP
- BGP router-ID source: Loopback0 address
- BGP peer group names: IPV4-UNDERLAY-PEERS, EVPN-OVERLAY-PEERS, MLAG-IPV4-UNDERLAY-PEER
- ECMP on leaves: maximum-paths 4 ecmp 4
- Loopback advertisement: redistribute connected route-map RM-CONN-2-BGP + prefix-list PL-LOOPBACKS (eq 32)
- no bgp default ipv4-unicast: yes, activate address families explicitly
- send-community on EVPN overlay: send-community (all)
- ebgp-multihop TTL: 3
- Single-hop BFD placement: per routed interface bfd interval 999 min-rx 999 multiplier 3 + neighbor <PG> bfd
- Multihop BFD on EVPN overlay: router bfd multihop interval 999 min-rx 999 multiplier 3 + neighbor EVPN-OVERLAY-PEERS bfd; cEOS support checked in the smoke test
- BGP neighbor passwords: none
- vxlan udp-port: vxlan udp-port 4789
- vxlan virtual-router encapsulation mac-address mlag-system-id: yes on MLAG VTEP pairs
- RD format: <Loopback0 IP>:<VNI>
- Route-target lines: VLAN route-target both 65300:<VNI>; VRF import/export evpn 65300:<VNI>
- Anycast gateway MAC: 0200.0000.00aa
- VRF BGP router-id and connected routes: router-id Loopback0, redistribute connected without route-map
- SVI MTU: 9214
- Vlan3900 no autostate: yes
- LACP mode on the MLAG peer-link: active
- mlag <id> numbering: = port-channel number
- Management VRF and interface: VRF MGMT, Management1 via interface mapping
- service routing protocols model multi-agent: write it explicitly
- cEOS interface mapping file: one file per model
- Interface description format: <peer device> <peer interface>
