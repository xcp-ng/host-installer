# SPDX-License-Identifier: GPL-2.0-only

import os

import diskutil
import util
import netutil
from netinterface import *
import constants
import version
import re
import stat
import repository
from disktools import *
import hardware
import xcp
import xcp.bootloader as bootloader
from xcp.version import *
from xcp import logger
import xml.dom.minidom
import simplejson as json
import glob

class SettingsNotAvailable(Exception):
    pass

THIS_PLATFORM_VERSION = Version.from_string(version.PLATFORM_VERSION)
XENSERVER_7_0_0 = Version([2, 1, 0]) # Platform version
XENSERVER_MIN_VERSION = XENSERVER_7_0_0

def is_rootfs_uefi(mount_point):
    try:
        with open(os.path.join(mount_point, 'etc', 'fstab'), 'r') as fstab:
            for line in fstab:
                m = re.search(r'^\s*[^#]+\s/boot/efi\s', line)
                if m:
                    return True
    except FileNotFoundError:
        pass

    return False

class ExistingInstallation:
    def __init__(self, primary_disk, boot_device, state_device):
        self.primary_disk = primary_disk
        self.boot_device = boot_device
        self.state_device = state_device
        self.state_prefix = ''
        self.settings = None
        self.root_fs = None
        self._boot_fs = None
        self.boot_fs_mount = None
        self.detailed_version = ''

    def __str__(self):
        return "%s %s" % (
            self.visual_brand, self.visual_version)

    def mount_state(self):
        """ Mount main state partition on self.state_fs. """
        self.state_fs = util.TempMount(self.state_device, 'state-', )

    def unmount_state(self):
        self.state_fs.unmount()
        self.state_fs = None

    def join_state_path(self, *path):
        """ Construct an absolute path to a file in the main state partition. """
        return os.path.join(self.state_fs.mount_point, self.state_prefix, *path)

    def getInventoryValue(self, k):
        return self.inventory[k]

    def isUpgradeable(self):
        self.mount_state()
        result = True
        try:
            # CA-38459: handle missing firstboot directory e.g. Rio
            if os.path.exists(self.join_state_path('etc/firstboot.d/state')):
                firstboot_files = [ f for f in os.listdir(self.join_state_path('etc/firstboot.d')) \
                                    if f[0].isdigit() and os.stat(self.join_state_path('etc/firstboot.d', f))[stat.ST_MODE] & stat.S_IXUSR ]
                missing_state_files = [x for x in firstboot_files if not os.path.exists(self.join_state_path('etc/firstboot.d/state', x))]

                result = (len(missing_state_files) == 0)
                if not result:
                    logger.log('Upgradeability test failed:')
                    logger.log('  Firstboot:     '+', '.join(firstboot_files))
                    logger.log('  Missing state: '+', '.join(missing_state_files))
            else:
                for path in constants.INIT_SERVICE_FILES:
                    if not os.path.exists(self.join_state_path(path)):
                        result = False
                        logger.log('Cannot upgrade %s, expected file missing: %s (installation never booted?)' % (self.primary_disk, path))
        except Exception:
            result = False
        finally:
            self.unmount_state()
        return result

    def settingsAvailable(self):
        try:
            self.readSettings()
        except SettingsNotAvailable as text:
            logger.log("Settings unavailable: %s" % text)
            return False
        except Exception as e:
            logger.log("Settings unavailable: unhandled exception")
            logger.logException(e)
            return False
        else:
            return True

    def _readSettings(self):
        """ Read settings from the installation, returns a results dictionary. """

        results = { 'host-config': {} }

        self.mount_state()
        try:

            # timezone:
            tz = None
            clock_file = self.join_state_path('etc/localtime')
            if os.path.islink(clock_file):
                tzfile = os.path.realpath(clock_file)
                if '/usr/share/zoneinfo/' in tzfile:
                    _, tz = tzfile.split('/usr/share/zoneinfo/', 1)
            if not tz:
                # No timezone found:
                # Supply a default and for interactive installs prompt the user.
                logger.log('No timezone configuration found.')
                results['request-timezone'] = True
                tz = "Europe/London"
            results['timezone'] = tz

            # hostname.  We will assume one was set anyway and thus write
            # it back into the new filesystem.  If one wasn't set then this
            # will be localhost.localdomain, in which case the old behaviour
            # will persist anyway:
            fd = open(self.join_state_path('etc/sysconfig/network'), 'r')
            lines = fd.readlines()
            fd.close()
            for line in lines:
                if line.startswith('HOSTNAME='):
                    results['manual-hostname'] = (True, line[9:].strip())

            if os.path.exists(self.join_state_path('etc/hostname')):
                fd = open(self.join_state_path('etc/hostname'), 'r')
                line = fd.readline()
                results['manual-hostname'] = (True, line.strip())
                fd.close()

            if 'manual-hostname' not in results:
                results['manual-hostname'] = (False, None)

            # nameservers:
            domain = None
            if not os.path.exists(self.join_state_path('etc/resolv.conf')):
                results['manual-nameservers'] = (False, None)
            else:
                ns = []
                fd = open(self.join_state_path('etc/resolv.conf'), 'r')
                lines = fd.readlines()
                fd.close()
                for line in lines:
                    if line.startswith("nameserver "):
                        ns.append(line[11:].strip())
                    elif line.startswith("domain "):
                        domain = line[8:].strip()
                    elif line.startswith("search "):
                        domain = line.split()[1]
                results['manual-nameservers'] = (True, ns)

            # ntp servers:
            if os.path.exists(self.join_state_path('etc/chrony.conf')):
                fd = open(self.join_state_path('etc/chrony.conf'), 'r')
            else:
                fd = open(self.join_state_path('etc/ntp.conf'), 'r')
            lines = fd.readlines()
            fd.close()
            ntps = []
            for line in lines:
                if line.startswith("server "):
                    s = line[7:].split()[0].strip()
                    if any(s.endswith(d) for d in constants.DEFAULT_NTP_DOMAINS):
                        continue
                    ntps.append(s)
            results['ntp-servers'] = ntps
            # ntp-config-method should be set as follows:
            # 'dhcp' if dhcp was in use, regardless of server configuration
            # 'manual' if we had existing NTP servers defined (other than default servers)
            # 'default' if no NTP servers are defined
            if self._check_dhcp_ntp_status():
                results['ntp-config-method'] = 'dhcp'
            elif ntps:
                results['ntp-config-method'] = 'manual'
            else:
                results['ntp-config-method'] = 'default'

            # keyboard:
            keyboard_dict = {}
            keyboard_file = self.join_state_path('etc/sysconfig/keyboard')
            if os.path.exists(keyboard_file):
                keyboard_dict = util.readKeyValueFile(keyboard_file)
            keyboard_file = self.join_state_path('etc/vconsole.conf')
            if os.path.exists(keyboard_file):
                keyboard_dict.update(util.readKeyValueFile(keyboard_file))
            if 'KEYMAP' in keyboard_dict:
                results['keymap'] = keyboard_dict['KEYMAP']
            elif 'KEYTABLE' in keyboard_dict:
                results['keymap'] = keyboard_dict['KEYTABLE']
            # Do not error here if no keymap configuration is found.
            # This enables upgrade to still carry state on hosts without
            # keymap configured:
            # A default keymap is assigned in the backend of this installer.
            if 'keymap' not in results:
                logger.log('No existing keymap configuration found.')

            # root password:
            fd = open(self.join_state_path('etc/passwd'), 'r')
            root_pwd = None
            for line in fd:
                pwent = line.split(':')
                if pwent[0] == 'root':
                    root_pwd = pwent[1]
                    break
            fd.close()
            if len(root_pwd) == 1:
                root_pwd = None
                try:
                    fd = open(self.join_state_path('etc/shadow'), 'r')
                    for line in fd:
                        pwent = line.split(':')
                        if pwent[0] == 'root':
                            root_pwd = pwent[1]
                            break
                    fd.close()
                except:
                    pass

            if not root_pwd:
                raise SettingsNotAvailable("no root password found")
            results['root-password'] = ('pwdhash', root_pwd)

            # read network configuration.  We only care to find out what the
            # management interface is, and what its configuration was.
            # The dev -> MAC mapping for other devices will be preserved in the
            # database which is available in time for everything except the
            # management interface.
            mgmt_iface = self.getInventoryValue('MANAGEMENT_INTERFACE')

            if not mgmt_iface:
                logger.log('No existing management interface found.')
                raise SettingsNotAvailable("Could not find network configuration")
            elif os.path.exists(self.join_state_path(constants.NETWORK_DB)):
                logger.log('Checking %s for management interface configuration' % constants.NETWORKD_DB)

                def fetchIfaceInfoFromNetworkdbAsDict(bridge, iface=None):
                    args = ['chroot', self.state_fs.mount_point, '/'+constants.NETWORKD_DB, '-bridge', bridge]
                    if iface:
                        args.extend(['-iface', iface])
                    rv, out = util.runCmd2(args, with_stdout=True)
                    d = {}
                    for line in (x.strip() for x in out.split('\n') if len(x.strip())):
                        for key_value in line.split(" "):
                            var = key_value.split('=', 1)
                            d[var[0]] = var[1]
                    return d

                d = fetchIfaceInfoFromNetworkdbAsDict(mgmt_iface, mgmt_iface)
                # For mgmt on tagged vlan, networkdb output has no value for
                # 'interfaces' but instead has 'parent' specified. We need
                # to fetch 'interfaces' of parent and use for mgmt bridge.
                if not d.get('interfaces') and 'parent' in d:
                    p = fetchIfaceInfoFromNetworkdbAsDict(d['parent'])
                    d['interfaces'] = p['interfaces']

                results['net-admin-bridge'] = mgmt_iface
                results['net-admin-interface'] = d.get('interfaces').split(',')[0]

                if_hwaddr = netutil.getHWAddr(results['net-admin-interface'])

                vlan = int(d['vlan']) if 'vlan' in d else None
                proto = d.get('mode')
                if proto == 'static':
                    ip = d.get('ipaddr')
                    netmask = d.get('netmask')
                    gateway = d.get('gateway')
                    dns = d.get('dns', '').split(',')
                    if ip and netmask:
                        results['net-admin-configuration'] = NetInterface(NetInterface.Static, if_hwaddr, ip, netmask, gateway, dns, vlan=vlan)
                elif proto == 'dhcp':
                    results['net-admin-configuration'] = NetInterface(NetInterface.DHCP, if_hwaddr, vlan=vlan)
                else:
                    results['net-admin-configuration'] = NetInterface(None, if_hwaddr, vlan=vlan)

                protov6 = d.get('modev6')
                if protov6 == 'static':
                    ipv6 = d.get('ipaddrv6')
                    gatewayv6 = d.get('gatewayv6')
                    if ipv6:
                        results['net-admin-configuration'].addIPv6(NetInterface.Static, ipv6, gatewayv6)
                elif protov6 == 'dhcp':
                    results['net-admin-configuration'].addIPv6(NetInterface.DHCP)
                elif protov6 == 'autoconf':
                    results['net-admin-configuration'].addIPv6(NetInterface.Autoconf)
            else:
                logger.log("Failed to find " + self.join_state_path(constants.NETWORK_DB))
                raise SettingsNotAvailable("Could not find network configuration")

            repo_list = []
            if os.path.exists(self.join_state_path(constants.INSTALLED_REPOS_DIR)):
                try:
                    for repo_id in os.listdir(self.join_state_path(constants.INSTALLED_REPOS_DIR)):
                        try:
                            repo = repository.LegacyRepository(repository.FilesystemAccessor(self.join_state_path(constants.INSTALLED_REPOS_DIR, repo_id)))
                            if repo.hidden() != "true":
                                repo_list.append((repo.identifier(), repo.name(), (repo_id != constants.MAIN_REPOSITORY_NAME)))
                        except repository.RepoFormatError:
                            # probably pre-XML format
                            repo = open(self.join_state_path(constants.INSTALLED_REPOS_DIR, repo_id, repository.LegacyRepository.REPOSITORY_FILENAME))
                            repo_id = repo.readline().strip()
                            repo_name = repo.readline().strip()
                            repo.close()
                            repo_list.append((repo_id, repo_name, (repo_id != constants.MAIN_REPOSITORY_NAME)))
                except Exception as e:
                    logger.log('Scan for driver disks failed:')
                    logger.logException(e)

            results['repo-list'] = repo_list

            results['ha-armed'] = False
            try:
                db_path = "var/lib/xcp/local.db"
                if not os.path.exists(self.join_state_path(db_path)):
                    db_path = "var/xapi/local.db"
                db = open(self.join_state_path(db_path), 'r')
                if db.readline().find('<row key="ha.armed" value="true"') != -1:
                    results['ha-armed'] = True
                db.close()
            except:
                pass

            try:
                network_conf = open(self.join_state_path("etc/xensource/network.conf"), 'r')
                network_backend = network_conf.readline().strip()
                network_conf.close()

                if network_backend == constants.NETWORK_BACKEND_BRIDGE:
                    results['network-backend'] = constants.NETWORK_BACKEND_BRIDGE
                elif network_backend in [constants.NETWORK_BACKEND_VSWITCH, constants.NETWORK_BACKEND_VSWITCH_ALT]:
                    results['network-backend'] = constants.NETWORK_BACKEND_VSWITCH
                else:
                    raise SettingsNotAvailable("unknown network backend %s" % network_backend)
            except:
                pass

            results['master'] = None
            try:
                pt = open(self.join_state_path("etc/xensource/ptoken"), 'r')
                results['pool-token'] = pt.readline().strip()
                pt.close()
                pc = open(self.join_state_path("etc/xensource/pool.conf"), 'r')
                line = pc.readline().strip()
                if line.startswith('slave:'):
                    results['master'] = line[6:]
                pc.close()
            except:
                pass

        finally:
            self.unmount_state()

        # read bootloader config to extract various settings
        try:
            # Boot device
            self.mount_boot()
            boot_config = bootloader.Bootloader.loadExisting(self.boot_fs_mount)

            # Serial console
            try:
                xen_args = boot_config.menu['xe-serial'].getHypervisorArgs()
                com = [i for i in xen_args if re.match('com[0-9]+=.*', i)]
                results['serial-console'] = hardware.SerialPort.from_string(com[0])
            except Exception:
                logger.log("Could not parse serial settings")

                if boot_config.serial:
                    results['serial-console'] = hardware.SerialPort(boot_config.serial['port'],
                                                                    baud=str(boot_config.serial['baud']))
            results['bootloader-location'] = boot_config.location
            if boot_config.default != 'upgrade':
                results['boot-serial'] = (boot_config.default == 'xe-serial')

            # Subset of hypervisor arguments
            xen_args = boot_config.menu[boot_config.default].getHypervisorArgs()

            #   - cpuid_mask
            results['host-config']['xen-cpuid-masks'] = [x for x in xen_args if x.startswith('cpuid_mask')]

            #   - dom0_mem
            dom0_mem_arg = [x for x in xen_args if x.startswith('dom0_mem')]
            (dom0_mem, dom0_mem_min, dom0_mem_max) = xcp.dom0.parse_mem(dom0_mem_arg[0])
            if dom0_mem:
                results['host-config']['dom0-mem'] = dom0_mem // (1024*1024)

            #   - sched-gran
            sched_gran = next((x for x in xen_args if x.startswith('sched-gran=')), None)
            if sched_gran:
                results['host-config']['sched-gran'] = sched_gran

            # Subset of dom0 kernel arguments
            kernel_args = boot_config.menu[boot_config.default].getKernelArgs()

            #   - xen-pciback.hide
            pciback = next((x for x in kernel_args if x.startswith('xen-pciback.hide=')), None)
            if pciback:
                results['host-config']['xen-pciback.hide'] = pciback
        except Exception as e:
            logger.log('Exception whilst parsing existing bootloader config:')
            logger.logException(e)
        self.unmount_boot()

        return results

    def _check_dhcp_ntp_status(self):
        """Validate if DHCP was in use and had provided any NTP servers"""
        if os.path.exists(self.join_state_path('etc/dhcp/dhclient.d/chrony.sh')) and \
           not (os.stat(self.join_state_path('etc/dhcp/dhclient.d/chrony.sh')).st_mode & stat.S_IXUSR):
            # chrony.sh not executable indicates not using DHCP for NTP
            return False

        for f in glob.glob(self.join_state_path('var/lib/dhclient/chrony.servers.*')):
            if os.path.getsize(f) > 0:
                return True

        return False

    def mount_boot(self, ro=True):
        opts = None
        if ro:
            opts = ['ro']
        self._boot_fs = util.TempMount(self.boot_device, 'boot', opts, 'ext3')
        self.boot_fs_mount = self._boot_fs.mount_point

    def unmount_boot(self):
        if self.boot_fs:
            self._boot_fs.unmount()
            self._boot_fs = None
            self.boot_fs_mount = None

    def readSettings(self):
        if not self.settings:
            self.settings = self._readSettings()
        return self.settings


class ExistingRetailInstallation(ExistingInstallation):
    def __init__(self, primary_disk, boot_device, root_device, state_device, storage):
        self.variant = 'Retail'
        ExistingInstallation.__init__(self, primary_disk, boot_device, state_device)
        self.root_device = root_device
        self.storage = storage
        self._boot_fs_mounted = False
        self.readInventory()

    def __repr__(self):
        return "<ExistingRetailInstallation: %s (%s) on %s>" % (str(self), self.detailed_version, self.root_device)

    def mount_root(self, ro=True, boot_device=None):
        opts = None
        if ro:
            opts = ['ro']
        self.root_fs = util.TempMount(self.root_device, 'root', opts, 'ext3', boot_device=boot_device)

    def unmount_root(self):
        if self.root_fs:
            self.root_fs.unmount()
            self.root_fs = None

    # Because EFI boot stores the bootloader configuration on the ESP, mount
    # it at its usual location if necessary so that the configuration is found.
    def mount_boot(self, ro=True):
        self.mount_root(ro=ro, boot_device=self.boot_device)
        self.boot_fs_mount = self.root_fs.mount_point

    def unmount_boot(self):
        self.unmount_root()
        self.boot_fs_mount = None

    def readInventory(self):
        self.mount_root()
        try:
            self.inventory = util.readKeyValueFile(os.path.join(self.root_fs.mount_point,
                                                                constants.INVENTORY_FILE),
                                                   strip_quotes=True)
            self.build = self.inventory.get('BUILD_NUMBER', None)
            build_suffix = ('-' + self.build) if self.build is not None else ''
            self.version = Version.from_string(self.inventory['PLATFORM_VERSION'] +
                                               build_suffix)
            if 'PRODUCT_NAME' in self.inventory:
                self.name = self.inventory['PRODUCT_NAME']
                self.brand = self.inventory['PRODUCT_BRAND']
            else:
                self.name = self.inventory['PLATFORM_NAME']
                self.brand = self.inventory['PLATFORM_NAME']

            if 'OEM_BRAND' in self.inventory:
                self.oem_brand = self.inventory['OEM_BRAND']
                self.visual_brand = self.oem_brand
            else:
                self.visual_brand = self.brand
            if 'OEM_VERSION' in self.inventory:
                self.oem_version = self.inventory['OEM_VERSION']
                self.visual_version = self.inventory['OEM_VERSION'] + build_suffix
            else:
                self.visual_version = self.inventory['PRODUCT_VERSION_TEXT']
                self.detailed_version = self.inventory['PRODUCT_VERSION'] + build_suffix
        finally:
            self.unmount_root()

class XenServerBackup:
    def __init__(self, part, mnt):
        self.partition = part
        self.inventory = util.readKeyValueFile(os.path.join(mnt, constants.INVENTORY_FILE), strip_quotes=True)
        self.build = self.inventory.get('BUILD_NUMBER', None)
        build_suffix = ('-' + self.build) if self.build is not None else ''
        self.detailed_version = ''
        self.version = Version.from_string(self.inventory['PLATFORM_VERSION'] +
                                           build_suffix)
        if 'PRODUCT_NAME' in self.inventory:
            self.name = self.inventory['PRODUCT_NAME']
            self.brand = self.inventory['PRODUCT_BRAND']
        else:
            self.name = self.inventory['PLATFORM_NAME']
            self.brand = self.inventory['PLATFORM_NAME']

        if 'OEM_BRAND' in self.inventory:
            self.oem_brand = self.inventory['OEM_BRAND']
            self.visual_brand = self.oem_brand
        else:
            self.visual_brand = self.brand
        if 'OEM_VERSION' in self.inventory:
            self.oem_version = self.inventory['OEM_VERSION']
            self.visual_version = self.inventory['OEM_VERSION'] + build_suffix
        else:
            self.visual_version = self.inventory['PRODUCT_VERSION_TEXT']
            self.detailed_version = self.inventory['PRODUCT_VERSION'] + build_suffix

        if self.inventory['PRIMARY_DISK'].startswith('/dev/md_'):
            # Handle restoring an installation using a /dev/md_* path
            self.root_disk = os.path.realpath(self.inventory['PRIMARY_DISK'].replace('md_', 'md/') + '_0')
        else:
            self.root_disk = diskutil.partitionFromId(self.inventory['PRIMARY_DISK'])
            self.root_disk = getMpathMasterOrDisk(self.root_disk)

    def __str__(self):
        return "%s %s" % (
            self.visual_brand, self.visual_version)

    def __repr__(self):
        return "<XenServerBackup: %s (%s) on %s>" % (str(self), self.detailed_version, self.partition)

def findXenSourceBackups():
    """Scans the host and find partitions containing backups of XenSource
    products.  Returns a list of device node paths to partitions containing
    said backups. """
    partitions = diskutil.getQualifiedPartitionList()
    backups = []

    for p in partitions:
        b = None
        try:
            b = util.TempMount(p, 'backup-', ['ro'], 'ext3')
            if os.path.exists(os.path.join(b.mount_point, '.xen-backup-partition')):
                backup = XenServerBackup(p, b.mount_point)
                logger.log("Found a backup: %s" % (repr(backup),))
                # Don't restore a BIOS backup with a UEFI installer and conversely
                backup_is_uefi = is_rootfs_uefi(b.mount_point)
                if backup_is_uefi != constants.UEFI_INSTALLER:
                    logger.log("findXenSourceBackups: ignoring, installer mode (%s) does not match backup boot mode (%s)" %
                               ("uefi" if constants.UEFI_INSTALLER else "legacy", "uefi" if backup_is_uefi else "legacy" ))
                elif backup.version >= XENSERVER_MIN_VERSION and \
                        backup.version <= THIS_PLATFORM_VERSION:
                    backups.append(backup)
        except:
            pass
        if b:
            b.unmount()

    return backups

def findXenSourceProducts():
    """Scans the host and finds XenSource product installations.
    Returns list of ExistingInstallation objects.

    Currently requires supervisor privileges due to mounting
    filesystems."""

    installs = []

    for disk_device in diskutil.getQualifiedDiskList():
        disk = diskutil.probeDisk(disk_device)

        inst = None
        try:
            if disk.root[0] == diskutil.INSTALL_RETAIL:
                inst = ExistingRetailInstallation(disk_device, disk.boot[1], disk.root[1], disk.state[1], disk.storage)
        except Exception as e:
            logger.log("A problem occurred whilst scanning for existing installations:")
            logger.logException(e)
            logger.log("This is not fatal.  Continuing anyway.")

        if inst:
            logger.log("Found an installation: %s" % (repr(inst),))
            installs.append(inst)

    return installs

def readInventoryFile(filename):
    return util.readKeyValueFile(filename, strip_quotes=True)

def find_installed_products():
    try:
        installed_products = findXenSourceProducts()
    except Exception as e:
        logger.log("A problem occurred whilst scanning for existing installations:")
        logger.logException(e)
        logger.log("This is not fatal.  Continuing anyway.")
        installed_products = []
    return installed_products

