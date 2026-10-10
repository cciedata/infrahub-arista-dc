#!/bin/sh
# DC3-VPNFW1: DC3 VPN-partner FW stand-in (FRR). Rendered by Infrahub, do not edit.
# Management 10.250.32.35/24 (containerlab mgmt-ipv4 of this node)
# Partners: PARTNER01 (peer 198.18.100.1, 192.168.100.0/24 -> 10.24.160.0/28).
set -eu
sysctl -qw net.ipv4.ip_forward=1
sysctl -qw net.ipv4.tcp_l3mdev_accept=1
# --- VRF 16 (PARTNER); the WAN leg stays in the default table (FortiOS VRF 0)
ip link add vrf16 type vrf table 1016 2>/dev/null || true
ip link set vrf16 up
# --- aggregates (LACP; members that exist in the lab)
ip link add agg-inside type bond mode 802.3ad 2>/dev/null || true
if [ -e /sys/class/net/port37 ]; then ip link set port37 down; ip link set port37 mtu 9214; ip link set port37 master agg-inside; ip link set port37 up; fi
if [ -e /sys/class/net/port38 ]; then ip link set port38 down; ip link set port38 mtu 9214; ip link set port38 master agg-inside; ip link set port38 up; fi
ip link set agg-inside mtu 9214 up
ip link add agg-outside type bond mode 802.3ad 2>/dev/null || true
if [ -e /sys/class/net/port39 ]; then ip link set port39 down; ip link set port39 mtu 9214; ip link set port39 master agg-outside; ip link set port39 up; fi
if [ -e /sys/class/net/port40 ]; then ip link set port40 down; ip link set port40 mtu 9214; ip link set port40 master agg-outside; ip link set port40 up; fi
ip link set agg-outside mtu 9214 up
# --- VLAN sub-interfaces: inside (PARTNER, vrf16), outside (edge VLAN, default table)
ip link add link agg-outside name OUTSIDE-3200 type vlan id 3200 2>/dev/null || true
ip addr replace 10.27.14.12/24 dev OUTSIDE-3200
ip link set OUTSIDE-3200 mtu 9214 up
ip link add link agg-inside name PARTNER-3300 type vlan id 3300 2>/dev/null || true
ip link set PARTNER-3300 master vrf16
ip addr replace 10.27.9.3/29 dev PARTNER-3300
ip link set PARTNER-3300 mtu 9214 up
# --- LO-WAN (public WAN endpoint)
ip link add LO-WAN type dummy 2>/dev/null || true
ip addr replace 198.18.3.129/32 dev LO-WAN
ip link set LO-WAN up
# --- partner tunnels (lab: direct links) and source NAT of each partner to its translated /28
command -v iptables >/dev/null || apk add --no-cache iptables >/dev/null
if [ -e /sys/class/net/PARTNER01 ]; then ip link set PARTNER01 master vrf16; ip addr replace 198.18.100.0/31 dev PARTNER01; ip link set PARTNER01 up; fi
iptables -t nat -C POSTROUTING -s 192.168.100.0/24 -o PARTNER-3300 -j SNAT --to-source 10.24.160.0-10.24.160.15 2>/dev/null || iptables -t nat -A POSTROUTING -s 192.168.100.0/24 -o PARTNER-3300 -j SNAT --to-source 10.24.160.0-10.24.160.15
# --- routing daemons
pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-VPNFW1
service integrated-vtysh-config
!
ip route 0.0.0.0/0 10.27.14.1
vrf vrf16
 ip route 192.168.100.0/24 198.18.100.1
 ip route 10.24.160.0/28 blackhole
exit-vrf
!
ip prefix-list PL-DEFAULT seq 10 permit 0.0.0.0/0
ip prefix-list PL-PARTNER-NAT seq 10 permit 10.24.160.0/28
!
route-map RM-PARTNER-FROM-LEAF permit 10
 match ip address prefix-list PL-DEFAULT
route-map RM-PARTNER-TO-LEAF permit 10
 match ip address prefix-list PL-PARTNER-NAT
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
router bgp 65330 vrf vrf16
 bgp router-id 10.27.9.3
 bgp graceful-restart
 neighbor 10.27.9.1 remote-as 65371
 neighbor 10.27.9.1 description DC3-BL1
 neighbor 10.27.9.1 update-source PARTNER-3300
 neighbor 10.27.9.1 bfd profile FW-BFD
 neighbor 10.27.9.2 remote-as 65371
 neighbor 10.27.9.2 description DC3-BL2
 neighbor 10.27.9.2 update-source PARTNER-3300
 neighbor 10.27.9.2 bfd profile FW-BFD
 address-family ipv4 unicast
  network 10.24.160.0/28
  neighbor 10.27.9.1 route-map RM-PARTNER-FROM-LEAF in
  neighbor 10.27.9.1 route-map RM-PARTNER-TO-LEAF out
  neighbor 10.27.9.2 route-map RM-PARTNER-FROM-LEAF in
  neighbor 10.27.9.2 route-map RM-PARTNER-TO-LEAF out
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-VPNFW1 stand-in ready"