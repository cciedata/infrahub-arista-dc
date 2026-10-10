# DC3 design

DC3 is a standalone datacenter on Arista EOS, without border gateways and DCI, and it has no link to
other sites. The data lives in Infrahub, a generator builds the
devices, Jinja2 transforms render the configs, and a containerlab lab (cEOS-lab plus FRR stand-ins) tests them.

This page describes the design as built.

Diagrams: `docs/diagrams/DC3-diagrams.pdf` (regenerate with `python3 scripts/dc3_diagrams.py`).

## 1. Scope

| Item | Value |
|---|---|
| Switches | 30: 2 spines, 20 server leaves, border-leaf pair, service-leaf pair, edge pair, DMZ pair |
| Server leaves | MLAG pair L1-L2, EVPN ESI pairs L3-L10, single leaves L11-L20 |
| Outside the fabric | FRR stand-ins, one per role: Internal FW, Internet FW, SD-WAN FW (+2 branches), VPN-partner FW (+1 partner), 2 ISP routers (+2 carriers), 2 cloud routers (+AWS, Azure) |
| Not in DC3 | BGW, DCI, links to other sites, OOB devices |
| Software | EOS 4.36.2F (cEOS-lab 4.36.2F in the lab), FRR 10.6.0 for every stand-in |
| Data and code | Infrahub 1.11.2, infrahub-sdk 1.23.2; standalone build on an empty Infrahub, branch main; an Infrahub that also holds other sites uses a branch such as `dc3` |

## 2. Hardware and roles

| Role | Devices | Model | Routing |
|---|---|---|---|
| Spine | DC3-S1, DC3-S2 | DCS-7060DX5-64S | eBGP underlay and EVPN overlay to every leaf |
| Border leaf (MLAG) | DC3-BL1, DC3-BL2 | DCS-7280DR3-24 | VTEP; Internet FW, SD-WAN, VPN, cloud routers |
| Service leaf (MLAG) | DC3-SL1, DC3-SL2 | DCS-7280DR3-24 | VTEP; Internal FW (all tenant VRFs and TRANSIT) |
| Server leaf | DC3-L1..DC3-L20 | DCS-7050SX3-48YC8C | VTEP; anycast gateway |
| DMZ pair | DC3-DMZ1, DC3-DMZ2 | DCS-7050SX3-48YC8C | L2 only, MLAG, no BGP |
| Edge pair | DC3-EDGE1, DC3-EDGE2 | DCS-7050DX4-32S | L2 only, MLAG, no BGP (ISP routers, Internet FW outside) |

Models and port facts come from the Arista datasheets; orderable SKU suffixes are open.
Interface names are the hardware names (`EthernetN/1`, `Management1`); the lab maps containerlab `ethN` to them with one
cEOS mapping file per model, tested in the smoke lab.

## 3. Numbering

### Addresses

| Block | Use |
|---|---|
| 10.24.0.0/16 | tenants (VRF blocks 10.24.0.0/18, 10.24.64.0/18, 10.24.144.0/20, 10.24.160.0/21; segment /24s) |
| 10.27.0.0/16 | infrastructure |
| 10.27.0.0/24 | pool DC3-LOOPBACK0 (/32) |
| 10.27.1.0/24 | pool DC3-LOOPBACK1-VTEP (/32; own VTEP address, on Loopback2 of MLAG VTEP members) |
| 10.27.2.0/25 | pool DC3-ANYCAST-VTEP (/32; Loopback1 of both MLAG members) |
| 10.27.3.0/24 | pool DC3-VPC-BACKUP (/31, Vlan3900) |
| 10.27.4.0/22 | pool DC3-FABRIC-P2P (/31 spine-leaf links) |
| 10.27.17.0/24 | pool DC3-VPC-KEEPALIVE (/31) |
| 10.27.13-16.0/24 | firewall attachments, edge VLAN, ISP routers, TRANSIT |
| 10.250.32.0/20 | OOB management; 10.250.32.0/24 = pool DC3-MGMT0, gateway .1 |
| 198.18.3.0/24 | public range (lab only, benchmark range) |
| 192.0.2.16/30, .20/30 | carrier links ISP-A, ISP-B |

Source: `objects/dc3-03-ip-plan.yml`, `scripts/dc3_create_pools.py`. Pools are created by the script and
allocate by identifier, so reruns get the same addresses.

### ASNs

| Device(s) | ASN |
|---|---|
| Spines | 65300 |
| BL pair / SL pair / L1-L2 pair | 65371 / 65372 / 65373 |
| L3..L10 (ESI members) | 65374..65381 |
| L11..L20 | 65382..65391 (spare 65392-65399) |
| Internal FW / SD-WAN (local-as) / VPN / Internet FW | 65310 / 65320 / 65330 / 65340 |
| Cloud routers / ISP routers | 65370 / 65050 |
| Carriers ISP-A, ISP-B / AWS / Azure / SD-WAN overlay | 64496, 64497 / 65500 / 12076 / 65400 |

Rule: site block 65300 + role offset (fabric +00, Internal +10, SD-WAN +20, VPN +30, Internet +40, cloud +70); leaves
start at +71 so they do not clash with the cloud routers.

### Names

Devices `DC3-<role><n>`; MLAG domain-ids `DC3-MLAG-<m1>-<m2>` with schema domain numbers 301-305;
ESI pairs `DC3-ESI-L3-L4`..`DC3-ESI-L9-L10`; Infrahub object names prefixed `dc3_`; interface descriptions
`<peer device> <peer interface>` on every interface with a peer.

## 4. Underlay

- eBGP on the spine-leaf /31s, one ASN per leaf or pair, router-ID = Loopback0.
- Peer groups `IPV4-UNDERLAY-PEERS`, `EVPN-OVERLAY-PEERS`, `MLAG-IPV4-UNDERLAY-PEER`; `no bgp default
  ipv4-unicast`, address families activated explicitly; no BGP passwords anywhere.
- Loopbacks advertised by `redistribute connected route-map RM-CONN-2-BGP` with `PL-LOOPBACKS` (eq 32) over the
  three loopback pools, read from Infrahub.
- ECMP `maximum-paths 4 ecmp 4` on leaves and spines.
- BFD 999 ms x 3: single-hop per routed interface plus `neighbor <PG> bfd`; multihop under `router bfd` for the EVPN
  overlay; 36 sessions Up on cEOS in the smoke lab.
- MTU 9214 on routed links and SVIs: the EOS maximum.
- BGP graceful restart: helper only (`graceful-restart-helper`) on every EOS switch with BGP.

## 5. MLAG and ESI

| Item | Value |
|---|---|
| Pairs | L1-L2, BL1-BL2, SL1-SL2, EDGE1-EDGE2, DMZ1-DMZ2 |
| Peer-link | Port-Channel1000, LACP active members, trunk group MLAGPEER, no MTU line (L2) |
| Peer VLAN | 3900 `VPC-BACKUP-UNDERLAY`, `no autostate`, `no spanning-tree vlan-id 3900`, /31 from DC3-VPC-BACKUP |
| peer-address | other address of the own Vlan3900 /31 |
| Heartbeat | dedicated keepalive port in VRF VPC-KEEPALIVE, `heartbeat-interval 4000` (ms) |
| Dual-primary | detection delay 5 action errdisable all-interfaces; recovery delay mlag 360 non-mlag 300 |
| Port-channel ids | `mlag N` = port-channel number |
| VTEP | anycast address on Loopback1 of both members, own address on Loopback2; `vxlan virtual-router encapsulation mac-address mlag-system-id` |
| iBGP to the peer | MLAG-IPV4-UNDERLAY-PEER with next-hop-self |
| ESI leaves | link tracking group SPINE-UPLINKS, recovery delay 360 s, links minimum 1 (rendered with the ES ports, which are deferred) |
| Spanning tree | access and attach ports `spanning-tree portfast` + `bpduguard enable`; `spanning-tree mode mstp`, `spanning-tree mst 0 priority 8192` on the 28 leaves and pair members, none on the spines |

MLAG L1-L2 active, connected, peer-link up, config consistent (smoke lab).

## 6. Overlay

- EVPN over eBGP multihop (TTL 3) from Loopback0 to both spines; spines peer with all 24 VTEP leaves;
  `send-community` on the overlay.
- VXLAN on `Vxlan1`, source Loopback1, `vxlan udp-port 4789`.
- RD `<Loopback0>:<VNI>`; RTs `65300:<VNI>` (`route-target both` for VLANs, import/export evpn for VRFs).
- Anycast gateway MAC 0200.0000.00aa, gateway .1 of each segment.
- 12 overlay sessions Established, remote VTEPs learnt, host pings its gateway over VXLAN (smoke lab).

## 7. Tenants and routing policy

| VRF | L3 VNI / VLAN |
|---|---|
| PROD | 50001 / 2501 |
| DEV | 50002 / 2502 |
| CITRIX | 50022 / 2522 |
| PARTNER | 50031 / 2531 |
| REPLICATION | 50041 / 2541 (no switch carries it yet) |
| TRANSIT | 50100 / 2599 |

Source: `objects/dc3-06-fabric.yml` ; 86 entries in `objects/dc3-07-segments.yml`.

Traffic paths:
- Tenant VRFs leave the fabric only through the Internal FW on the service leaves. Each tenant VRF sends its DC3 summary
  and accepts only 0/0; only DC3 summaries are used.
- In TRANSIT, the service leaves send the Internal FW only 0/0. They accept the tenant summaries. The Internal FW stand-in
  rewrites the AS path toward the leaves (`set as-path exclude all`).
- The border leaves carry TRANSIT to the Internet FW (accept 0/0 only, send the tenant summaries), the SD-WAN hub, the VPN-partner
  FW and the cloud routers. The ISP routers send 0/0 to the Internet FW only while a carrier default exists.
- Tenant traffic to the clouds and branches goes leaf → Internal FW → service leaf → border leaf → cloud router or SD-WAN
  FW, never through the Internet FW. Failure test 16 and 6 post-check routes prove this path.
- Firewall attachments: SVI `.1`/`.2` on the pair members, stand-in `.3` on a VLAN sub-interface. The attach ports are one MLAG
  port-channel per stand-in and pair, numbers Po11/13/15/21/22/31/32.
- Partner: /31 with the VPN FW. Source NAT goes to the partner's DC3 /28.

## 8. Management

`Management1` in VRF MGMT, with a default route to .1 of the management /24 and `no ip routing vrf MGMT`.
`service routing protocols model multi-agent`. eAPI runs `management api http-commands` over HTTPS in VRF MGMT
. Login: local user `dc3-automation` (privilege 15, role network-admin) with local AAA; the
secret is a placeholder in every render and is filled at delivery time from `~/.dc3/secrets.env` (`lab/dc3_secrets.py`,
`scripts/dc3_make_secret.sh`). The lab checks still use `docker exec`. NTP, DNS, logging,
SNMP, clock and banner are left out on purpose.

## 8a. Zero-touch provisioning

All 30 switches can provision themselves from Infrahub (`ztp/`):
- Kea DHCP gives an address from 10.250.32.200-.239 in the DC3 OOB /24. Option 67 points to `http://10.250.32.1:8080/bootstrap`
.
- The EOS bootstrap script sends the switch serial to the ZTP server. The server finds the device by `serial_number` on
  its Infrahub branch (`DC3_ZTP_BRANCH`, default main), renders its config live with `infrahubctl render`, fills the `dc3-automation` secret and serves it.
  The script writes the startup-config and calls back, and EOS reboots into it.
- The server checks the switch with eAPI `show hostname` on its Management1 address within 30 min. It records
  `ztp_state` pending/served/complete/failed with `ztp_updated` on the device's branch as `dc3-ztp-callback`.
- Laptop test: one cEOS-lab container with no startup-config (`ztp/run-ztp.sh`): DC3-L11 went from blank to `complete`
  in 66 s after the callback, with an eAPI login as dc3-automation.
- `ztp_state` and `ztp_updated` come from `schemas-dc3/ztp.yml`.

## 9. Data model and pipeline

```
objects/dc3-*.yml ──► Infrahub (branch main)    ──► generator dc3_site ──► devices, interfaces, pairs, addresses
                         ▲  pools (scripts/dc3_create_pools.py)                    │
                         │                                                         ▼
                 queries/dc3_*.gql ◄──────────── transforms templates/dc3_*.j2 ──► 30 EOS + 13 FRR renders
                                                                                   │
                                          lab/ (containerlab topology, post-check, failure tests) ◄┘
```

- Objects are loaded in this order: dc3-00 groups, dc3-01 bootstrap, dc3-02 site, dc3-03 IP plan, dc3-04 hardware,
  dc3-05 ASNs, dc3-06 VRFs, dc3-07 segments, dc3-08 stand-ins, then the pools, the generator and dc3-10 lab host ports.
- Generator `dc3_site` has a plan-only mode that prints a table and a write mode. The reconciler defaults to report
. Infrahub never starts the generator by itself; scripts/bootstrap.sh runs it. Guards stop a run that would move an address held by another
  device.
- Idempotency: a full rerun kept all 1094 generator-owned ids.
- Renders: 30 EOS + 13 FRR, 0 failures after the review fixes. `tests/expected` holds
  the baselines; `lab/dc3_render_check.py` re-checks cross-device consistency.
- Unit tests: offline suite in `tests/` (135 tests). The standalone build was rebuilt from scratch on a fresh Linux host
  with an empty Infrahub (docs/BUILD.md).

## 10. Lab and verification

| Item | Status |
|---|---|
| Smoke subset (S1, S2, L1, L2, L11, PROD host; cEOS 4.36.2F) | PASS 86, FAIL 0; with the values of PASS 85, FAIL 0, ERROR 1 read error |
| Memory per cEOS node | 1.29-1.40 GiB |
| Full lab (30 cEOS + 22 FRR) | pre-check 1525/1525 PASS, post-check 0 new failures, ZTP 30/30 complete; memory about 45 GB for the 30 cEOS plus the FRR nodes, a 64 GB Linux host |
| Failure tests 1-16 | written (`lab/dc3_failtests.py`); outage limits: 1 (phase 1), 2, 3, 4, 6, 14 = 5 s; 5, 8 = 2 s; 7, 12, 13 = 0.5 s; 9, 10, 11, 15, 16 measured only; full lab: 15 of 16 PASS, test 6 open |
| Syntax check | every rendered line accepted; 4 display-only differences (hidden `no shutdown`, hidden `protocol https`, `portfast` shown as `portfast edge`, MAC with colons), normalised by the check since |

The smoke test stops Infrahub while it runs and starts it again afterwards, because the laptop has about 7 GB in WSL
. Destroying the lab and restarting Infrahub run in a trap, so they always happen.

## 11. Known gaps

1. are built, re-rendered and run on cEOS in the smoke subset (no config error, PASS 85, FAIL 0, one read ERROR). That run had no secret set, so the username line was dropped there; the eAPI login as dc3-automation was shown by the ZTP test (section 8a).
2. Server Ethernet segments, ESI values and their link tracking are deferred.
3. No cabling objects. Peer lookup depends on the description format.
4. `tests/expected` is a render of the current templates and objects; `tests/expected-delta.diff` is empty.
5. One smoke deploy failed with a containerlab deploy timeout, and the identical retry passed (cause not found).
