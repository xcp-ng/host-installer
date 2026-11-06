# SPDX-License-Identifier: GPL-2.0-only

import os
import diskutil
import util
import re
import subprocess
import time
import errno
from xcp import logger
from xcp.net.biosdevname import all_devices_all_names
from socket import inet_ntoa
from struct import pack

class NIC:
    def __init__(self, nic_dict):
        self.name = nic_dict.get("Kernel name", "")
        self.hwaddr = nic_dict.get("Assigned MAC", "").lower()
        self.pci_string = nic_dict.get("Bus Info", "").lower()
        self.driver = "%s (%s)" % (nic_dict.get("Driver", ""),
                                   nic_dict.get("Driver version", ""))
        self.smbioslabel = nic_dict.get("SMBIOS Label", "")

    def __repr__(self):
        return "<NIC: %s (%s)>" % (self.name, self.hwaddr)

def scanConfiguration():
    """ Returns a dictionary of string -> NIC with a snapshot of the NIC
    configuration.

    Filter out any NICs that have been reserved by the iBFT for use
    with boot time iSCSI targets.  (iBFT = iSCSI Boot Firmware Tables.)
    This is because we cannot use NICs that are used to access iSCSI
    LUNs for other purposes e.g. XenServer Management.
    """
    conf = {}
    nics = []

    for nif in getNetifList():
        if nif not in diskutil.ibft_reserved_nics:
            nics.append(nif)

    for nic in all_devices_all_names().values():
        name = nic.get("Kernel name", "")
        if name in nics:
            conf[name] = NIC(nic)

    return conf

def getNetifList(include_vlan=False):
    allNetifs = os.listdir("/sys/class/net")

    def ethfilter(interface, include_vlan):
        return interface != "lo" and (interface.isalnum() or
                                    (include_vlan and "." in interface))

    relevant = [x for x in allNetifs if ethfilter(x, include_vlan)]
    # We just need to make sure vlan comes after its hosting interface
    # for systemd interface configuration
    relevant.sort()
    return relevant

def writeNetInterfaceFiles(configuration):
    for iface in configuration:
        configuration[iface].writeSystemdNetworkdConfig(iface)

def reloadNetwork(timeout=120):
    """ Use networkctl to reload the configuration """
    util.runCmd2(["networkctl", "reload"])
    # Command return immediately, wait until network is up
    ret = util.runCmd2(["/usr/lib/systemd/systemd-networkd-wait-online", "--ipv4", f"--timeout={timeout}"])
    if ret:
        LOG.error(f"Timeout {timeout} waiting for network online")
    return ret

def ipaddr(interface):
    rc, out = util.runCmd2(['ip', 'addr', 'show', interface], with_stdout=True)
    if rc != 0:
        return None
    inets = [x for x in out.split("\n") if 'inet ' in x]
    if len(inets) == 1:
        m = re.search(r'inet (\S+)/', inets[0])
        if m:
            return m.group(1)
    return None

def interfaceUp(interface):
# work out if an interface is up:
    rc, out = util.runCmd2(['ip', 'addr', 'show', interface], with_stdout=True)
    if rc != 0:
        return False
    inets = [x for x in out.split("\n") if x.startswith("    inet ")]
    return len(inets) == 1

# work out if a link is up:
def linkUp(interface):
    linkUp = None

    try:
        fh = open("/sys/class/net/%s/operstate" % interface)
        state = fh.readline().strip()
        linkUp = (state == 'up')
        fh.close()
    except IOError:
        pass
    return linkUp

def setAllLinksUp():
    subprocs = []

    for nif in getNetifList():
        if nif not in diskutil.ibft_reserved_nics:
            subprocs.append(subprocess.Popen(['ip', 'link', 'set', nif, 'up'], close_fds=True))

    while None in [x.poll() for x in subprocs]:
        time.sleep(1)

def networkingUp():
    rc, out = util.runCmd2(['ip', 'route'], with_stdout=True)
    if rc == 0 and len(out.split('\n')) > 2:
        return True
    return False

def __readOneLineFile__(filename):
    f = open(filename)
    value = f.readline().strip('\n')
    f.close()
    return value

def getHWAddr(iface):
    try:
        return __readOneLineFile__('/sys/class/net/%s/address' % iface)
    except IOError as e:
        if e.errno == errno.ENOENT:
            return None
        raise

def valid_hostname(x, emptyValid=False, fqdn=False):
    if emptyValid and x == '':
        return True
    if fqdn:
        return re.match('^[a-zA-Z0-9]([-a-zA-Z0-9]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([-a-zA-Z0-9]{0,61}[a-zA-Z0-9])?)*$', x) is not None
    else:
        return re.match('^[a-zA-Z0-9]([-a-zA-Z0-9]{0,61}[a-zA-Z0-9])?$', x) is not None

def valid_vlan(vlan):
    if not re.match('^\d+$', vlan):
        return False
    if int(vlan)<1 or int(vlan)>=4095:
        return False
    return True

def valid_ip_addr(addr):
    if not re.match('^\d+\.\d+\.\d+\.\d+$', addr):
        return False
    els = addr.split('.')
    if len(els) != 4:
        return False
    for el in els:
        if int(el) > 255:
            return False
    return True

def network(ipaddr, netmask):
    ip = list(map(int,ipaddr.split('.',3)))
    nm = list(map(int,netmask.split('.',3)))
    nw = [ip[i] & nm[i] for i in range(4)]
    return ".".join(map(str,nw))

def prefix2netmask(mask):
    bits = 0
    for i in range(32-mask, 32):
        bits |= (1 << i)
    return inet_ntoa(pack('>I', bits))

class NetDevices:
    def __init__(self):
        self.netdev = []
        details = {}

        pipe = subprocess.Popen(['biosdevname', '-d'], bufsize=1, stdout=subprocess.PIPE, universal_newlines=True)
        for line in pipe.stdout:
            l = line.strip('\n')
            if len(l) == 0:
                self.netdev.append(details)
                details = {}
            else:
                (k, v) = l.split(':', 1)
                details[k.strip().lower().replace(' ', '-')] = v.strip()
        pipe.wait()

    def as_xml(self):
        output = '<net-devices>\n'

        for d in self.netdev:
            output += ' <net-device'
            for k, v in d.items():
                output += ' %s="%s"' % (k, v)
            output += '/>\n'

        output += '</net-devices>\n'
        return output

### EA-1069

import xcp.logger as LOG
from xcp.pci import VALID_SBDFI
from xcp.net.mac import VALID_COLON_MAC

RX_MAC = VALID_COLON_MAC
RX_PCI = VALID_SBDFI

interface_rules = []

class Rule:
    #pylint: disable=too-few-public-methods
    """ Class for a interface rule"""
    def __init__(self, slot, method, interface):
        self.slot = int(slot)
        self.method = method
        self.interface = interface

    def __str__(self):
        return f'{self.slot}:{self.method}="{self.interface}"'

    def __eq__(self, other):
        # One postion can have only one interface
        return (self.slot == other.slot and self.method == other.method
               and self.interface == other.interface)

    def __lt__(self, other):
        return self.slot < other.slot


def parse_interface_slot(rule):
    """
    Parse target slot and interface from rule
    return:
        Success: (slot, interface)
        Fail: (None, None)
    """
    sep = ":"
    split = rule.split(sep, 1)
    if len(split) != 2:
        LOG.warning(f"Invalid device mapping {rule} - Ignoring")
        return None, None
    target, remaining = split
    target = target.lstrip("eth")
    if not target.isnumeric():
        LOG.warning(f"{target} slot is NOT a number, Ignoring")
        return None, None
    if remaining.startswith("s:") or remaining.startswith("d:"):
        remaining = remaining.split(sep, 1)[1]
    return target, remaining

def parse_rule(rule):
    """
    Takes list from the code which parses the installer commandline.
    Returns a tupe:
            (Target eth name, Static/Dynamic, Method of id, Val of id)
    or None if the parse was not successful
    """

    slot, val = parse_interface_slot(rule)
    if slot is None:
        return None

    if val[0] == '"' and val[-1] == '"':
        return Rule(slot, "label", val[1:-1])
    if ":" not in val:
        # Can't be a valid mac or pci, take it as label
        return Rule(slot, "label", val.lower())
    if RX_MAC.match(val) is not None:
        return Rule(slot, "mac", val.lower())
    if RX_PCI.match(val) is not None:
        return Rule(slot, "pci", val.lower())
    LOG.warning(f"'{val}' is not a valid interface format label|mac|pci, Ignoring")
    return None

def generate_interface_rules(remap_list):
    """
    Generate interface rules basing on remap_list
    """
    interface_rules.clear()
    for cmd in remap_list:
        rule = parse_rule(cmd)
        if rule is None:
            continue
        exists = [r for r in interface_rules if r.slot == rule.slot]
        if exists:
            LOG.warning(f"Position for {rule} already occupied by {exists}, Ignoring")
            continue
        interface_rules.append(rule)
    interface_rules.sort()

def save_inteface_rules(path):
    """ save interface_rules to path"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding="utf-8") as file:
        for rule in interface_rules:
            file.write(f"{rule}\n")

def disable_ipv6_module(root):
    # Disable IPv6 loading by default.
    # This however does not disable from loading for requiring modules
    # (like bridge)
    dv6fd = open("%s/etc/modprobe.d/disable-ipv6.conf" % root, "w")
    dv6fd.write("alias net-pf-10 off\n")
    dv6fd.close()


from xcp.net.ifrename.dynamic import DynamicRules

def net_devs_of_last_boot(file_path):
    dynamic_rules = DynamicRules(file_path)
    if not dynamic_rules.load_and_parse():
        LOG.warning(f"Failed to parse the interface-rename dynamic rules.")
        return []
    else:
        return [(d.tname, d.mac.as_string(sep=":")) for d in dynamic_rules.lastboot]
