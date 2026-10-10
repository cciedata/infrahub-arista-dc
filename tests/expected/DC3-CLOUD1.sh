#!/bin/sh
# DC3-CLOUD1: DC3 cloud router stand-in (FRR). Rendered by Infrahub, do not edit.
# Management 10.250.32.38/24 (containerlab mgmt-ipv4 of this node)
# Not modelled: the management VRF (container eth0; its default route is removed so 0.0.0.0/0 comes only from BGP).
set -eu
sysctl -qw net.ipv4.ip_forward=1
ip route del default dev eth0 2>/dev/null || true
# --- loopback0
ip addr replace 10.27.13.1/32 dev lo
# --- BE31 toward the border leaves (LACP) and the TRANSIT sub-interface BE31.3003
ip link add BE31 type bond mode 802.3ad 2>/dev/null || true
if [ -e /sys/class/net/Gi0-0-0-0 ]; then ip link set Gi0-0-0-0 down; ip link set Gi0-0-0-0 mtu 9214; ip link set Gi0-0-0-0 master BE31; ip link set Gi0-0-0-0 up; fi
if [ -e /sys/class/net/Gi0-0-0-1 ]; then ip link set Gi0-0-0-1 down; ip link set Gi0-0-0-1 mtu 9214; ip link set Gi0-0-0-1 master BE31; ip link set Gi0-0-0-1 up; fi
ip link set BE31 mtu 9214 up
ip link add link BE31 name BE31.3003 type vlan id 3003 2>/dev/null || true
ip addr replace 10.27.16.27/29 dev BE31.3003
ip link set BE31.3003 mtu 9214 up
# --- cross-link Gi0-0-0-2, cloud-exchange port Hu0-0-0-16 and its handoffs
if [ -e /sys/class/net/Gi0-0-0-2 ]; then ip link set Gi0-0-0-2 mtu 9214; ip addr replace 10.27.13.16/31 dev Gi0-0-0-2; ip link set Gi0-0-0-2 up; fi
ip link set Hu0-0-0-16 mtu 9214 up
ip link add link Hu0-0-0-16 name Hu0-0-0-16.120 type vlan id 120 2>/dev/null || true
ip addr replace 169.254.120.2/30 dev Hu0-0-0-16.120
ip link set Hu0-0-0-16.120 mtu 1500 up
ip link add link Hu0-0-0-16 name Hu0-0-0-16.220 type vlan id 220 2>/dev/null || true
ip addr replace 10.98.0.17/30 dev Hu0-0-0-16.220
ip link set Hu0-0-0-16.220 mtu 1500 up
# --- routing daemons
pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-CLOUD1
service integrated-vtysh-config
!
ip prefix-list PL-GUARDRAIL seq 10 permit 10.27.0.0/16 le 32
ip prefix-list PL-GUARDRAIL seq 20 permit 10.0.0.0/16 le 32
ip prefix-list PL-AWS seq 10 permit 172.16.0.0/13 le 24
ip prefix-list PL-AZURE seq 10 permit 172.30.0.0/15 le 24
ip prefix-list PL-TO-CLOUD seq 10 permit 0.0.0.0/0
ip prefix-list PL-TO-CLOUD seq 20 permit 10.24.0.0/16
ip prefix-list PL-TO-CLOUD seq 30 permit 10.24.160.0/21
ip prefix-list PL-FROM-FABRIC seq 10 permit 0.0.0.0/0
ip prefix-list PL-FROM-FABRIC seq 20 permit 10.24.0.0/16 le 24
!
route-map RM-AWS-IN deny 10
 match ip address prefix-list PL-GUARDRAIL
route-map RM-AWS-IN permit 20
 match ip address prefix-list PL-AWS
route-map RM-AZURE-IN deny 10
 match ip address prefix-list PL-GUARDRAIL
route-map RM-AZURE-IN permit 20
 match ip address prefix-list PL-AZURE
route-map RM-CLOUD-OUT permit 10
 match ip address prefix-list PL-TO-CLOUD
route-map RM-FABRIC-IN permit 10
 match ip address prefix-list PL-FROM-FABRIC
route-map RM-FABRIC-OUT permit 10
 match ip address prefix-list PL-AWS
route-map RM-FABRIC-OUT permit 20
 match ip address prefix-list PL-AZURE
!
bfd
 profile CLOUD-BFD
  transmit-interval 300
  receive-interval 300
  detect-multiplier 3
  no echo-mode
 exit
 profile FABRIC-BFD
  transmit-interval 999
  receive-interval 999
  detect-multiplier 3
  no echo-mode
 exit
exit
!
router bgp 65370
 bgp router-id 10.27.13.1
 neighbor 169.254.120.1 remote-as 65500
 neighbor 169.254.120.1 description AWS private VIF 1 to the Direct Connect Gateway, VLAN 120
 neighbor 169.254.120.1 bfd profile CLOUD-BFD
 neighbor 10.98.0.18 remote-as 12076
 neighbor 10.98.0.18 description Microsoft ExpressRoute primary private peering, VLAN 220
 neighbor 10.98.0.18 bfd profile CLOUD-BFD
 neighbor 10.27.13.17 remote-as 65370
 neighbor 10.27.13.17 description DC3-CLOUD2
 neighbor 10.27.13.17 bfd profile FABRIC-BFD
 neighbor 10.27.16.25 remote-as 65371
 neighbor 10.27.16.25 description DC3-BL1 (DC3-TRANSIT-CLOUD1)
 neighbor 10.27.16.25 bfd profile FABRIC-BFD
 neighbor 10.27.16.26 remote-as 65371
 neighbor 10.27.16.26 description DC3-BL2 (DC3-TRANSIT-CLOUD1)
 neighbor 10.27.16.26 bfd profile FABRIC-BFD
 address-family ipv4 unicast
  aggregate-address 10.24.0.0/16
  neighbor 169.254.120.1 route-map RM-AWS-IN in
  neighbor 169.254.120.1 maximum-prefix 100 90 warning-only
  neighbor 169.254.120.1 route-map RM-CLOUD-OUT out
  neighbor 10.98.0.18 route-map RM-AZURE-IN in
  neighbor 10.98.0.18 maximum-prefix 100 90 warning-only
  neighbor 10.98.0.18 route-map RM-CLOUD-OUT out
  neighbor 10.27.13.17 next-hop-self
  neighbor 10.27.16.25 route-map RM-FABRIC-IN in
  neighbor 10.27.16.25 route-map RM-FABRIC-OUT out
  neighbor 10.27.16.26 route-map RM-FABRIC-IN in
  neighbor 10.27.16.26 route-map RM-FABRIC-OUT out
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-CLOUD1 stand-in ready"