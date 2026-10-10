#!/bin/sh
# DC3-INETFW1: DC3 Internet FW stand-in (FRR). Rendered by Infrahub, do not edit.
# Management 10.250.32.32/24 (containerlab mgmt-ipv4 of this node)
# Lab-only: source NAT of 10.24.144.0/20, 10.24.64.0/18, 10.24.160.0/21, 10.24.0.0/18 to 198.18.3.0/25 on OUTSIDE-3200.
set -eu
sysctl -qw net.ipv4.ip_forward=1
sysctl -qw net.ipv4.tcp_l3mdev_accept=1
# --- VRF (FortiOS VRF 1 -> vrf1, table 1001)
ip link add vrf1 type vrf table 1001 2>/dev/null || true
ip link set vrf1 up
# --- aggregates (LACP; members that exist in the lab)
ip link add agg-dmz type bond mode 802.3ad 2>/dev/null || true
if [ -e /sys/class/net/port15 ]; then ip link set port15 down; ip link set port15 mtu 9214; ip link set port15 master agg-dmz; ip link set port15 up; fi
if [ -e /sys/class/net/port16 ]; then ip link set port16 down; ip link set port16 mtu 9214; ip link set port16 master agg-dmz; ip link set port16 up; fi
ip link set agg-dmz mtu 9214 up
ip link add agg-inside type bond mode 802.3ad 2>/dev/null || true
if [ -e /sys/class/net/port19 ]; then ip link set port19 down; ip link set port19 mtu 9214; ip link set port19 master agg-inside; ip link set port19 up; fi
if [ -e /sys/class/net/port20 ]; then ip link set port20 down; ip link set port20 mtu 9214; ip link set port20 master agg-inside; ip link set port20 up; fi
ip link set agg-inside mtu 9214 up
ip link add agg-outside type bond mode 802.3ad 2>/dev/null || true
if [ -e /sys/class/net/port21 ]; then ip link set port21 down; ip link set port21 mtu 9214; ip link set port21 master agg-outside; ip link set port21 up; fi
if [ -e /sys/class/net/port22 ]; then ip link set port22 down; ip link set port22 mtu 9214; ip link set port22 master agg-outside; ip link set port22 up; fi
ip link set agg-outside mtu 9214 up
# --- VLAN sub-interfaces: inside (TRANSIT), outside (edge VLAN), DMZ (Infrahub data)
ip link add link agg-dmz name DMZ-1400 type vlan id 1400 2>/dev/null || true
ip link set DMZ-1400 master vrf1
ip addr replace 10.24.128.1/24 dev DMZ-1400
ip link set DMZ-1400 mtu 9214 up
ip link add link agg-dmz name DMZ-1401 type vlan id 1401 2>/dev/null || true
ip link set DMZ-1401 master vrf1
ip addr replace 10.24.129.1/24 dev DMZ-1401
ip link set DMZ-1401 mtu 9214 up
ip link add link agg-dmz name DMZ-1402 type vlan id 1402 2>/dev/null || true
ip link set DMZ-1402 master vrf1
ip addr replace 10.24.130.1/24 dev DMZ-1402
ip link set DMZ-1402 mtu 9214 up
ip link add link agg-dmz name DMZ-1403 type vlan id 1403 2>/dev/null || true
ip link set DMZ-1403 master vrf1
ip addr replace 10.24.131.1/24 dev DMZ-1403
ip link set DMZ-1403 mtu 9214 up
ip link add link agg-dmz name DMZ-1404 type vlan id 1404 2>/dev/null || true
ip link set DMZ-1404 master vrf1
ip addr replace 10.24.132.1/24 dev DMZ-1404
ip link set DMZ-1404 mtu 9214 up
ip link add link agg-dmz name DMZ-1405 type vlan id 1405 2>/dev/null || true
ip link set DMZ-1405 master vrf1
ip addr replace 10.24.133.1/24 dev DMZ-1405
ip link set DMZ-1405 mtu 9214 up
ip link add link agg-dmz name DMZ-1406 type vlan id 1406 2>/dev/null || true
ip link set DMZ-1406 master vrf1
ip addr replace 10.24.134.1/24 dev DMZ-1406
ip link set DMZ-1406 mtu 9214 up
ip link add link agg-dmz name DMZ-1407 type vlan id 1407 2>/dev/null || true
ip link set DMZ-1407 master vrf1
ip addr replace 10.24.135.1/24 dev DMZ-1407
ip link set DMZ-1407 mtu 9214 up
ip link add link agg-dmz name DMZ-1408 type vlan id 1408 2>/dev/null || true
ip link set DMZ-1408 master vrf1
ip addr replace 10.24.136.1/24 dev DMZ-1408
ip link set DMZ-1408 mtu 9214 up
ip link add link agg-dmz name DMZ-1409 type vlan id 1409 2>/dev/null || true
ip link set DMZ-1409 master vrf1
ip addr replace 10.24.137.1/24 dev DMZ-1409
ip link set DMZ-1409 mtu 9214 up
ip link add link agg-dmz name DMZ-1410 type vlan id 1410 2>/dev/null || true
ip link set DMZ-1410 master vrf1
ip addr replace 10.24.138.1/24 dev DMZ-1410
ip link set DMZ-1410 mtu 9214 up
ip link add link agg-dmz name DMZ-1411 type vlan id 1411 2>/dev/null || true
ip link set DMZ-1411 master vrf1
ip addr replace 10.24.139.1/24 dev DMZ-1411
ip link set DMZ-1411 mtu 9214 up
ip link add link agg-dmz name DMZ-1412 type vlan id 1412 2>/dev/null || true
ip link set DMZ-1412 master vrf1
ip addr replace 10.24.140.1/24 dev DMZ-1412
ip link set DMZ-1412 mtu 9214 up
ip link add link agg-dmz name DMZ-1413 type vlan id 1413 2>/dev/null || true
ip link set DMZ-1413 master vrf1
ip addr replace 10.24.141.1/24 dev DMZ-1413
ip link set DMZ-1413 mtu 9214 up
ip link add link agg-dmz name DMZ-1414 type vlan id 1414 2>/dev/null || true
ip link set DMZ-1414 master vrf1
ip addr replace 10.24.142.1/24 dev DMZ-1414
ip link set DMZ-1414 mtu 9214 up
ip link add link agg-dmz name DMZ-1415 type vlan id 1415 2>/dev/null || true
ip link set DMZ-1415 master vrf1
ip addr replace 10.24.143.1/24 dev DMZ-1415
ip link set DMZ-1415 mtu 9214 up
ip link add link agg-outside name OUTSIDE-3200 type vlan id 3200 2>/dev/null || true
ip link set OUTSIDE-3200 master vrf1
ip addr replace 10.27.14.10/24 dev OUTSIDE-3200
ip link set OUTSIDE-3200 mtu 9214 up
ip link add link agg-inside name TRANSIT-3001 type vlan id 3001 2>/dev/null || true
ip link set TRANSIT-3001 master vrf1
ip addr replace 10.27.16.11/29 dev TRANSIT-3001
ip link set TRANSIT-3001 mtu 9214 up
# --- lab-only source NAT toward the ISP routers
command -v iptables >/dev/null || apk add --no-cache iptables >/dev/null
iptables -t nat -C POSTROUTING -s 10.24.144.0/20 -o OUTSIDE-3200 -j SNAT --to-source 198.18.3.1-198.18.3.126 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.24.144.0/20 -o OUTSIDE-3200 -j SNAT --to-source 198.18.3.1-198.18.3.126
iptables -t nat -C POSTROUTING -s 10.24.64.0/18 -o OUTSIDE-3200 -j SNAT --to-source 198.18.3.1-198.18.3.126 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.24.64.0/18 -o OUTSIDE-3200 -j SNAT --to-source 198.18.3.1-198.18.3.126
iptables -t nat -C POSTROUTING -s 10.24.160.0/21 -o OUTSIDE-3200 -j SNAT --to-source 198.18.3.1-198.18.3.126 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.24.160.0/21 -o OUTSIDE-3200 -j SNAT --to-source 198.18.3.1-198.18.3.126
iptables -t nat -C POSTROUTING -s 10.24.0.0/18 -o OUTSIDE-3200 -j SNAT --to-source 198.18.3.1-198.18.3.126 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.24.0.0/18 -o OUTSIDE-3200 -j SNAT --to-source 198.18.3.1-198.18.3.126
# --- routing daemons
pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-INETFW1
service integrated-vtysh-config
!
vrf vrf1
 ip route 198.18.3.0/24 blackhole
exit-vrf
!
ip prefix-list PL-DEFAULT seq 10 permit 0.0.0.0/0
ip prefix-list PL-TRANSIT-SUMMARY seq 10 permit 10.24.144.0/20
ip prefix-list PL-TRANSIT-SUMMARY seq 20 permit 10.24.64.0/18
ip prefix-list PL-TRANSIT-SUMMARY seq 30 permit 10.24.160.0/21
ip prefix-list PL-TRANSIT-SUMMARY seq 40 permit 10.24.0.0/18
ip prefix-list PL-PUBLIC seq 10 permit 198.18.3.0/24
!
route-map RM-TRANSIT-FROM-LEAF permit 10
 match ip address prefix-list PL-TRANSIT-SUMMARY
route-map RM-TRANSIT-TO-LEAF permit 10
 match ip address prefix-list PL-DEFAULT
route-map RM-FROM-ISP permit 10
 match ip address prefix-list PL-DEFAULT
route-map RM-TO-ISP permit 10
 match ip address prefix-list PL-PUBLIC
!
bfd
 profile FW-BFD
  transmit-interval 999
  receive-interval 999
  detect-multiplier 3
  no echo-mode
 exit
exit
!
router bgp 65340 vrf vrf1
 bgp router-id 10.27.16.11
 bgp graceful-restart
 neighbor 10.27.16.9 remote-as 65371
 neighbor 10.27.16.9 description DC3-BL1
 neighbor 10.27.16.9 update-source TRANSIT-3001
 neighbor 10.27.16.9 bfd profile FW-BFD
 neighbor 10.27.16.10 remote-as 65371
 neighbor 10.27.16.10 description DC3-BL2
 neighbor 10.27.16.10 update-source TRANSIT-3001
 neighbor 10.27.16.10 bfd profile FW-BFD
 neighbor 10.27.14.2 remote-as 65050
 neighbor 10.27.14.2 description DC3-ISP1
 neighbor 10.27.14.2 update-source OUTSIDE-3200
 neighbor 10.27.14.2 bfd profile FW-BFD
 neighbor 10.27.14.3 remote-as 65050
 neighbor 10.27.14.3 description DC3-ISP2
 neighbor 10.27.14.3 update-source OUTSIDE-3200
 neighbor 10.27.14.3 bfd profile FW-BFD
 address-family ipv4 unicast
  network 198.18.3.0/24
  neighbor 10.27.16.9 route-map RM-TRANSIT-FROM-LEAF in
  neighbor 10.27.16.9 route-map RM-TRANSIT-TO-LEAF out
  neighbor 10.27.16.10 route-map RM-TRANSIT-FROM-LEAF in
  neighbor 10.27.16.10 route-map RM-TRANSIT-TO-LEAF out
  neighbor 10.27.14.2 route-map RM-FROM-ISP in
  neighbor 10.27.14.2 route-map RM-TO-ISP out
  neighbor 10.27.14.3 route-map RM-FROM-ISP in
  neighbor 10.27.14.3 route-map RM-TO-ISP out
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-INETFW1 stand-in ready"