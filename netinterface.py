# SPDX-License-Identifier: GPL-2.0-only

import util
import netutil

def getText(nodelist):
    rc = ""
    for node in nodelist:
        if node.nodeType == node.TEXT_NODE:
            rc = rc + node.data
    return rc.strip().encode()
def getTextOrNone(nodelist):
    rc = ""
    for node in nodelist:
        if node.nodeType == node.TEXT_NODE:
            rc = rc + node.data
    return rc == "" and None or rc.strip().encode()

class NetInterface(object):
    """ Represents the configuration of a network interface. """

    Static = 1
    DHCP = 2
    Autoconf = 3

    def __init__(self, mode, hwaddr, ipaddr=None, netmask=None, gateway=None,
                 dns=None, domain=None, vlan=None):
        assert mode is None or mode == self.Static or mode == self.DHCP
        if ipaddr == '':
            ipaddr = None
        if netmask == '':
            netmask = None
        if gateway == '':
            gateway = None
        if dns == '':
            dns = None
        elif isinstance(dns, str):
            dns = [ dns ]
        if mode == self.Static:
            assert ipaddr
            assert netmask

        self.mode = mode
        self.hwaddr = hwaddr
        if mode == self.Static:
            self.ipaddr = ipaddr
            self.netmask = netmask
            self.gateway = gateway
            self.dns = dns
            self.domain = domain
        else:
            self.ipaddr = None
            self.netmask = None
            self.gateway = None
            self.dns = None
            self.domain = None
        self.vlan = vlan

        # Initialise IPv6 to None.
        self.modev6 = None
        self.ipv6addr = None
        self.ipv6_gateway = None

    def __repr__(self):
        hw = "hwaddr = '%s' " % self.hwaddr

        if self.mode == self.Static:
            ipv4 = "Static;" + \
                "ipaddr='%s';netmask='%s';gateway='%s';dns='%s';domain='%s'>" % \
                (self.ipaddr, self.netmask, self.gateway, self.dns, self.domain)
        elif self.mode == self.DHCP:
            ipv4 = "DHCP"
        else:
            ipv4 = "None"

        if self.modev6 == self.Static:
            ipv6 = "Static;" + \
                "ipaddr='%s';gateway='%s'>" % \
                (self.ipv6addr, self.ipv6_gateway)
        elif self.modev6 == self.DHCP:
            ipv6 = "DHCP"
        elif self.modev6 == self.Autoconf:
            ipv6 = "autoconf"
        else:
            ipv6 = "None"
        vlan = ("vlan = '%d' " % self.vlan) if self.vlan else ""

        return "<NetInterface: %s%s ipv4:%s ipv6:%s>" % (hw, vlan, ipv4, ipv6)

    def get(self, name, default=None):
        retval = default
        if hasattr(self, name):
            attr = getattr(self, name)
            if attr is not None:
                retval = attr
        return retval

    def getInterfaceName(self, iface):
        return ("%s.%d" % (iface, self.vlan)) if self.vlan else iface

    def addIPv6(self, modev6, ipv6addr=None, ipv6gw=None):
        assert modev6 is None or modev6 == self.Static or modev6 == self.DHCP or modev6 == self.Autoconf
        if ipv6addr == '':
            ipv6addr = None
        if ipv6gw == '':
            ipv6gw = None
        if modev6 == self.Static:
            assert ipv6addr

        self.modev6 = modev6
        if modev6 == self.Static:
            self.ipv6addr = ipv6addr
            self.ipv6_gateway = ipv6gw
        else:
            self.ipv6addr = None
            self.ipv6_gateway = None

    def valid(self):
        if (self.mode == self.Static) and ((self.ipaddr is None) or (self.netmask is None)):
            return False
        if (self.modev6 == self.Static) and (self.ipv6addr is None):
            return False
        return self.mode or self.modev6

    def isStatic4(self):
        """ Returns true if an IPv4 static interface configuration is represented. """
        return self.mode == self.Static

    def isStatic6(self):
        """ Returns true if an IPv6 static interface configuration is represented. """
        return self.modev6 == self.Static

    def isDynamic(self):
        """ Returns true if a dynamic interface configuration is represented. """
        return self.mode == self.DHCP or self.modev6 == self.DHCP or self.modev6 == self.Autoconf

    def isVlan(self):
        return self.vlan is not None

    def getBroadcast(self):
        bcast = None
        rc, output = util.runCmd2(['/bin/ipcalc', '-b', self.ipaddr, self.netmask],
                                  with_stdout=True)
        if rc == 0:
            bcast = output[10:].strip()
        return bcast

    def writeRHStyleInterface(self, iface):
        """ Write a RedHat-style configuration entry for this interface to
        file object f using interface name iface. """

        assert self.modev6 or self.mode
        iface_vlan = self.getInterfaceName(iface)

        f = open('/etc/sysconfig/network-scripts/ifcfg-%s' % iface_vlan, 'w')
        f.write("DEVICE=%s\n" % iface_vlan)
        f.write("ONBOOT=yes\n")
        if self.mode == self.DHCP:
            f.write("BOOTPROTO=dhcp\n")
            f.write("PERSISTENT_DHCLIENT=1\n")
        elif self.mode == self.Static:
            # CA-11825: broadcast needs to be determined for non-standard networks
            bcast = self.getBroadcast()
            f.write("BOOTPROTO=none\n")
            f.write("IPADDR=%s\n" % self.ipaddr)
            if bcast is not None:
                f.write("BROADCAST=%s\n" % bcast)
            f.write("NETMASK=%s\n" % self.netmask)
            if self.gateway:
                f.write("GATEWAY=%s\n" % self.gateway)

        if self.modev6:
            with open('/etc/sysconfig/network', 'w') as net_conf:
                net_conf.write("NETWORKING_IPV6=yes\n")
            f.write("IPV6INIT=yes\n")
            f.write("IPV6_DEFROUTE=yes\n")
            f.write("IPV6_DEFAULTDEV=%s\n" % iface_vlan)

        if self.modev6 == self.DHCP:
            f.write("DHCPV6C=yes\n")
            f.write("PERSISTENT_DHCLIENT_IPV6=yes\n")
            f.write("IPV6_FORCE_ACCEPT_RA=yes\n")
            f.write("IPV6_AUTOCONF=no\n")
        elif self.modev6 == self.Static:
            f.write("IPV6ADDR=%s\n" % self.ipv6addr)
            if self.ipv6_gateway:
                f.write("IPV6_DEFAULTGW=%s\n" % (self.ipv6_gateway))
            f.write("IPV6_AUTOCONF=no\n")
        elif self.modev6 == self.Autoconf:
            f.write("IPV6_AUTOCONF=yes\n")

        if self.vlan:
            f.write("VLAN=yes\n")
        f.close()


    def waitUntilUp(self, iface):
        iface_name = self.getInterfaceName(iface)
        if self.isStatic4() and self.gateway and util.runCmd2(
            ['/usr/sbin/arping', '-f', '-w', '120', '-I', iface_name, self.gateway]
        ):
            return False

        if self.isStatic6() and self.ipv6_gateway and util.runCmd2(
            ['/usr/sbin/ndisc6', '-1', '-w', '120', self.ipv6_gateway, iface_name]
        ):
            return False

        return True

    @staticmethod
    def getModeStr(mode):
        if mode == NetInterface.Static:
            return 'static'
        if mode == NetInterface.DHCP:
            return 'dhcp'
        if mode == NetInterface.Autoconf:
            return 'autoconf'
        return 'none'

class NetInterfaceV6(NetInterface):
    def __init__(self, mode, hwaddr, ipaddr=None, netmask=None, gateway=None, dns=None, domain=None, vlan=None):
        super(NetInterfaceV6, self).__init__(None, hwaddr, None, None, None, None, None, vlan)

        ipv6addr = None
        if mode == self.Static:
            assert ipaddr
            assert netmask

            ipv6addr = ipaddr + "/" + netmask
            if dns == '':
                dns = None
            elif isinstance(dns, str):
                dns = [ dns ]
            self.dns = dns
            self.domain = domain

        self.addIPv6(mode, ipv6addr=ipv6addr, ipv6gw=gateway)
