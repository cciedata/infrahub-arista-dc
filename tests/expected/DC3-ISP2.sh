#!/bin/sh
# DC3-ISP2: DC3 ISP router stand-in (FRR). Rendered by Infrahub, do not edit.
# Management 10.250.32.37/24 (containerlab mgmt-ipv4 of this node)
# Not modelled: the management VRF (container eth0; its default route is removed so 0.0.0.0/0 comes
# only from BGP) and VRRP (the edge VLAN VIP is not in the lab).
set -eu
sysctl -qw net.ipv4.ip_forward=1
ip route del default dev eth0 2>/dev/null || true
# --- loopback0
ip addr replace 10.27.15.18/32 dev lo
# --- BE21 (LACP; members that exist in the lab) and its sub-interface BE21.3200
ip link add BE21 type bond mode 802.3ad 2>/dev/null || true
if [ -e /sys/class/net/Gi0-0-0-0 ]; then ip link set Gi0-0-0-0 down; ip link set Gi0-0-0-0 mtu 9214; ip link set Gi0-0-0-0 master BE21; ip link set Gi0-0-0-0 up; fi
if [ -e /sys/class/net/Gi0-0-0-1 ]; then ip link set Gi0-0-0-1 down; ip link set Gi0-0-0-1 mtu 9214; ip link set Gi0-0-0-1 master BE21; ip link set Gi0-0-0-1 up; fi
ip link set BE21 mtu 9214 up
ip link add link BE21 name BE21.3200 type vlan id 3200 2>/dev/null || true
ip addr replace 10.27.14.3/24 dev BE21.3200
ip link set BE21.3200 mtu 9214 up
# --- cross-link Gi0-0-0-2 and carrier link Gi0-0-0-3
if [ -e /sys/class/net/Gi0-0-0-2 ]; then ip link set Gi0-0-0-2 mtu 9214; ip addr replace 10.27.15.1/31 dev Gi0-0-0-2; ip link set Gi0-0-0-2 up; fi
if [ -e /sys/class/net/Gi0-0-0-3 ]; then ip link set Gi0-0-0-3 mtu 9214; ip addr replace 192.0.2.22/30 dev Gi0-0-0-3; ip link set Gi0-0-0-3 up; fi
# --- routing daemons
pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-ISP2
service integrated-vtysh-config
!
ip route 198.18.3.0/24 blackhole
ip route 198.18.3.0/25 10.27.14.10
ip route 198.18.3.160/27 10.27.14.10
ip route 198.18.3.144/28 10.27.14.11
ip route 198.18.3.128/28 10.27.14.12
!
ip prefix-list PL-OWN-PUBLIC seq 10 permit 198.18.3.0/24
ip prefix-list PL-DEFAULT seq 10 permit 0.0.0.0/0
!
bgp as-path access-list AS-LOCAL seq 10 permit ^$
!
route-map RM-CARRIER-IN permit 10
route-map RM-CARRIER-OUT permit 10
 match ip address prefix-list PL-OWN-PUBLIC
 match as-path AS-LOCAL
route-map RM-INETFW-IN permit 10
 match ip address prefix-list PL-OWN-PUBLIC
route-map RM-INETFW-OUT deny 10
route-map RM-COND-DEFAULT permit 10
 match ip address prefix-list PL-DEFAULT
!
bfd
 profile ISP-BFD
  transmit-interval 999
  receive-interval 999
  detect-multiplier 3
  no echo-mode
 exit
exit
!
router bgp 65050
 bgp router-id 10.27.15.18
 bgp graceful-restart
 neighbor 192.0.2.21 remote-as 64497
 neighbor 192.0.2.21 description ISP-B
 neighbor 192.0.2.21 bfd profile ISP-BFD
 neighbor 10.27.15.0 remote-as 65050
 neighbor 10.27.15.0 description DC3-ISP1
 neighbor 10.27.15.0 bfd profile ISP-BFD
 neighbor 10.27.14.10 remote-as 65340
 neighbor 10.27.14.10 description DC3-INETFW1
 neighbor 10.27.14.10 bfd profile ISP-BFD
 address-family ipv4 unicast
  network 198.18.3.0/24
  neighbor 192.0.2.21 route-map RM-CARRIER-IN in
  neighbor 192.0.2.21 maximum-prefix 1200000 90 warning-only
  neighbor 192.0.2.21 route-map RM-CARRIER-OUT out
  neighbor 10.27.15.0 next-hop-self
  neighbor 10.27.14.10 route-map RM-INETFW-IN in
  neighbor 10.27.14.10 default-originate route-map RM-COND-DEFAULT
  neighbor 10.27.14.10 route-map RM-INETFW-OUT out
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-ISP2 stand-in ready"