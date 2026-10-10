#!/bin/sh
# DC3-INTFW1: DC3 Internal FW stand-in (FRR). Rendered by Infrahub, do not edit.
# Management 10.250.32.33/24 (containerlab mgmt-ipv4 of this node)
# Routes toward the leaves carry only the FW AS.
set -eu
sysctl -qw net.ipv4.ip_forward=1
sysctl -qw net.ipv4.tcp_l3mdev_accept=1
# --- VRFs (FortiOS VRF id -> vrf<id>, table 1000 + id)
ip link add vrf1 type vrf table 1001 2>/dev/null || true
ip link set vrf1 up
ip link add vrf15 type vrf table 1015 2>/dev/null || true
ip link set vrf15 up
ip link add vrf11 type vrf table 1011 2>/dev/null || true
ip link set vrf11 up
ip link add vrf16 type vrf table 1016 2>/dev/null || true
ip link set vrf16 up
ip link add vrf10 type vrf table 1010 2>/dev/null || true
ip link set vrf10 up
# --- aggregates (LACP; members that exist in the lab)
ip link add agg-inside type bond mode 802.3ad 2>/dev/null || true
if [ -e /sys/class/net/port19 ]; then ip link set port19 down; ip link set port19 mtu 9214; ip link set port19 master agg-inside; ip link set port19 up; fi
if [ -e /sys/class/net/port20 ]; then ip link set port20 down; ip link set port20 mtu 9214; ip link set port20 master agg-inside; ip link set port20 up; fi
ip link set agg-inside mtu 9214 up
# --- VLAN sub-interfaces, one per attachment (address = the stand-in's .3 from Infrahub)
ip link add link agg-inside name TRANSIT-3000 type vlan id 3000 2>/dev/null || true
ip link set TRANSIT-3000 master vrf1
ip addr replace 10.27.16.3/29 dev TRANSIT-3000
ip link set TRANSIT-3000 mtu 9214 up
ip link add link agg-inside name PROD-3100 type vlan id 3100 2>/dev/null || true
ip link set PROD-3100 master vrf10
ip addr replace 10.27.8.3/29 dev PROD-3100
ip link set PROD-3100 mtu 9214 up
ip link add link agg-inside name DEV-3101 type vlan id 3101 2>/dev/null || true
ip link set DEV-3101 master vrf11
ip addr replace 10.27.8.11/29 dev DEV-3101
ip link set DEV-3101 mtu 9214 up
ip link add link agg-inside name CITRIX-3105 type vlan id 3105 2>/dev/null || true
ip link set CITRIX-3105 master vrf15
ip addr replace 10.27.8.43/29 dev CITRIX-3105
ip link set CITRIX-3105 mtu 9214 up
ip link add link agg-inside name PARTNER-3106 type vlan id 3106 2>/dev/null || true
ip link set PARTNER-3106 master vrf16
ip addr replace 10.27.8.51/29 dev PARTNER-3106
ip link set PARTNER-3106 mtu 9214 up
# --- routing daemons (watchfrr starts zebra and staticd; bgpd and bfdd are started here)
pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-INTFW1
service integrated-vtysh-config
!
ip prefix-list PL-DEFAULT seq 10 permit 0.0.0.0/0
ip prefix-list PL-CITRIX-SUMMARY seq 10 permit 10.24.144.0/20
ip prefix-list PL-DEV-SUMMARY seq 10 permit 10.24.64.0/18
ip prefix-list PL-PARTNER-SUMMARY seq 10 permit 10.24.160.0/21
ip prefix-list PL-PROD-SUMMARY seq 10 permit 10.24.0.0/18
ip prefix-list PL-TRANSIT-SUMMARY seq 10 permit 10.24.144.0/20
ip prefix-list PL-TRANSIT-SUMMARY seq 20 permit 10.24.64.0/18
ip prefix-list PL-TRANSIT-SUMMARY seq 30 permit 10.24.160.0/21
ip prefix-list PL-TRANSIT-SUMMARY seq 40 permit 10.24.0.0/18
!
route-map RM-CITRIX-FROM-LEAF permit 10
 match ip address prefix-list PL-CITRIX-SUMMARY
route-map RM-CITRIX-TO-LEAF permit 10
 match ip address prefix-list PL-DEFAULT
 set as-path exclude all
route-map RM-LEAK-TRANSIT-TO-CITRIX permit 10
 match ip address prefix-list PL-DEFAULT
route-map RM-DEV-FROM-LEAF permit 10
 match ip address prefix-list PL-DEV-SUMMARY
route-map RM-DEV-TO-LEAF permit 10
 match ip address prefix-list PL-DEFAULT
 set as-path exclude all
route-map RM-LEAK-TRANSIT-TO-DEV permit 10
 match ip address prefix-list PL-DEFAULT
route-map RM-PARTNER-FROM-LEAF permit 10
 match ip address prefix-list PL-PARTNER-SUMMARY
route-map RM-PARTNER-TO-LEAF permit 10
 match ip address prefix-list PL-DEFAULT
 set as-path exclude all
route-map RM-LEAK-TRANSIT-TO-PARTNER permit 10
 match ip address prefix-list PL-DEFAULT
route-map RM-PROD-FROM-LEAF permit 10
 match ip address prefix-list PL-PROD-SUMMARY
route-map RM-PROD-TO-LEAF permit 10
 match ip address prefix-list PL-DEFAULT
 set as-path exclude all
route-map RM-LEAK-TRANSIT-TO-PROD permit 10
 match ip address prefix-list PL-DEFAULT
route-map RM-LEAK-TO-TRANSIT permit 10
 match ip address prefix-list PL-CITRIX-SUMMARY
route-map RM-LEAK-TO-TRANSIT permit 20
 match ip address prefix-list PL-DEV-SUMMARY
route-map RM-LEAK-TO-TRANSIT permit 30
 match ip address prefix-list PL-PARTNER-SUMMARY
route-map RM-LEAK-TO-TRANSIT permit 40
 match ip address prefix-list PL-PROD-SUMMARY
route-map RM-TRANSIT-FROM-LEAF permit 10
 match ip address prefix-list PL-DEFAULT
route-map RM-TRANSIT-TO-LEAF permit 10
 match ip address prefix-list PL-TRANSIT-SUMMARY
 set as-path exclude all
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
router bgp 65310 vrf vrf1
 bgp router-id 10.27.16.3
 bgp graceful-restart
 neighbor 10.27.16.1 remote-as 65372
 neighbor 10.27.16.1 description DC3-SL1
 neighbor 10.27.16.1 update-source TRANSIT-3000
 neighbor 10.27.16.1 bfd profile FW-BFD
 neighbor 10.27.16.2 remote-as 65372
 neighbor 10.27.16.2 description DC3-SL2
 neighbor 10.27.16.2 update-source TRANSIT-3000
 neighbor 10.27.16.2 bfd profile FW-BFD
 address-family ipv4 unicast
  neighbor 10.27.16.1 route-map RM-TRANSIT-FROM-LEAF in
  neighbor 10.27.16.1 route-map RM-TRANSIT-TO-LEAF out
  neighbor 10.27.16.2 route-map RM-TRANSIT-FROM-LEAF in
  neighbor 10.27.16.2 route-map RM-TRANSIT-TO-LEAF out
  import vrf vrf15
  import vrf vrf11
  import vrf vrf16
  import vrf vrf10
  import vrf route-map RM-LEAK-TO-TRANSIT
 exit-address-family
exit
!
router bgp 65310 vrf vrf15
 bgp router-id 10.27.16.3
 bgp graceful-restart
 neighbor 10.27.8.41 remote-as 65372
 neighbor 10.27.8.41 description DC3-SL1
 neighbor 10.27.8.41 update-source CITRIX-3105
 neighbor 10.27.8.41 bfd profile FW-BFD
 neighbor 10.27.8.42 remote-as 65372
 neighbor 10.27.8.42 description DC3-SL2
 neighbor 10.27.8.42 update-source CITRIX-3105
 neighbor 10.27.8.42 bfd profile FW-BFD
 address-family ipv4 unicast
  neighbor 10.27.8.41 route-map RM-CITRIX-FROM-LEAF in
  neighbor 10.27.8.41 route-map RM-CITRIX-TO-LEAF out
  neighbor 10.27.8.42 route-map RM-CITRIX-FROM-LEAF in
  neighbor 10.27.8.42 route-map RM-CITRIX-TO-LEAF out
  import vrf vrf1
  import vrf route-map RM-LEAK-TRANSIT-TO-CITRIX
 exit-address-family
exit
!
router bgp 65310 vrf vrf11
 bgp router-id 10.27.16.3
 bgp graceful-restart
 neighbor 10.27.8.9 remote-as 65372
 neighbor 10.27.8.9 description DC3-SL1
 neighbor 10.27.8.9 update-source DEV-3101
 neighbor 10.27.8.9 bfd profile FW-BFD
 neighbor 10.27.8.10 remote-as 65372
 neighbor 10.27.8.10 description DC3-SL2
 neighbor 10.27.8.10 update-source DEV-3101
 neighbor 10.27.8.10 bfd profile FW-BFD
 address-family ipv4 unicast
  neighbor 10.27.8.9 route-map RM-DEV-FROM-LEAF in
  neighbor 10.27.8.9 route-map RM-DEV-TO-LEAF out
  neighbor 10.27.8.10 route-map RM-DEV-FROM-LEAF in
  neighbor 10.27.8.10 route-map RM-DEV-TO-LEAF out
  import vrf vrf1
  import vrf route-map RM-LEAK-TRANSIT-TO-DEV
 exit-address-family
exit
!
router bgp 65310 vrf vrf16
 bgp router-id 10.27.16.3
 bgp graceful-restart
 neighbor 10.27.8.49 remote-as 65372
 neighbor 10.27.8.49 description DC3-SL1
 neighbor 10.27.8.49 update-source PARTNER-3106
 neighbor 10.27.8.49 bfd profile FW-BFD
 neighbor 10.27.8.50 remote-as 65372
 neighbor 10.27.8.50 description DC3-SL2
 neighbor 10.27.8.50 update-source PARTNER-3106
 neighbor 10.27.8.50 bfd profile FW-BFD
 address-family ipv4 unicast
  neighbor 10.27.8.49 route-map RM-PARTNER-FROM-LEAF in
  neighbor 10.27.8.49 route-map RM-PARTNER-TO-LEAF out
  neighbor 10.27.8.50 route-map RM-PARTNER-FROM-LEAF in
  neighbor 10.27.8.50 route-map RM-PARTNER-TO-LEAF out
  import vrf vrf1
  import vrf route-map RM-LEAK-TRANSIT-TO-PARTNER
 exit-address-family
exit
!
router bgp 65310 vrf vrf10
 bgp router-id 10.27.16.3
 bgp graceful-restart
 neighbor 10.27.8.1 remote-as 65372
 neighbor 10.27.8.1 description DC3-SL1
 neighbor 10.27.8.1 update-source PROD-3100
 neighbor 10.27.8.1 bfd profile FW-BFD
 neighbor 10.27.8.2 remote-as 65372
 neighbor 10.27.8.2 description DC3-SL2
 neighbor 10.27.8.2 update-source PROD-3100
 neighbor 10.27.8.2 bfd profile FW-BFD
 address-family ipv4 unicast
  neighbor 10.27.8.1 route-map RM-PROD-FROM-LEAF in
  neighbor 10.27.8.1 route-map RM-PROD-TO-LEAF out
  neighbor 10.27.8.2 route-map RM-PROD-FROM-LEAF in
  neighbor 10.27.8.2 route-map RM-PROD-TO-LEAF out
  import vrf vrf1
  import vrf route-map RM-LEAK-TRANSIT-TO-PROD
 exit-address-family
exit
!
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-INTFW1 stand-in ready"