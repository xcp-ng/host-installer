# SPDX-License-Identifier: GPL-2.0-only

import os
import re
import shutil

from OpenSSL import crypto

import diskutil
import product
from xcp.version import *
from xcp import logger
from disktools import *
from netinterface import *
import util
import constants
import version
import netutil

def upgradeAvailable(src):
    return __upgraders__.hasUpgrader(src.name, src.version, src.variant)

def getUpgrader(src):
    """ Returns an upgrader instance suitable for src. Propogates a KeyError
    exception if no suitable upgrader is available (caller should have checked
    first by calling upgradeAvailable). """
    return __upgraders__.getUpgrader(src.name, src.version, src.variant)(src)

class Upgrader(object):
    """ Base class for upgraders.  Superclasses should define an
    upgrades_product variable that is the product they upgrade, an
    upgrades_variants list of Retail install types that they upgrade, and an
    upgrades_versions that is a list of pairs of version extents they support
    upgrading."""

    requires_backup = False
    optional_backup = True
    repartition = False

    def __init__(self, source):
        """ source is the ExistingInstallation object we're to upgrade. """
        self.source = source

    def upgrades(cls, product, version, variant):
        return (cls.upgrades_product == product and
                variant in cls.upgrades_variants and
                True in [ _min <= version <= _max for (_min, _max) in cls.upgrades_versions ])

    upgrades = classmethod(upgrades)

    prepTargetStateChanges = []
    prepTargetArgs = []
    def prepareTarget(self, progress_callback):
        """ Modify partition layout prior to installation. """
        return

    doBackupStateChanges = []
    doBackupArgs = []
    def doBackup(self, progress_callback):
        """ Collect configuration etc from installation. """
        return

    prepStateChanges = []
    prepUpgradeArgs = []
    def prepareUpgrade(self, progress_callback):
        """ Collect any state needed from the installation, and return a
        tranformation on the answers dict. """
        return

    def buildRestoreList(self, src_base):
        """ Returns list of filenames which will be copied by
        completeUpgrade(). """
        return []

    completeUpgradeArgs = ['mounts', 'primary-disk', 'backup-partnum']
    def completeUpgrade(self, mounts, target_disk, backup_partnum):
        """ Write any data back into the new filesystem as needed to follow
        through the upgrade. """

        src_uid_map = {}
        dst_uid_map = {}
        src_gid_map = {}
        dst_gid_map = {}

        def init_id_maps(src_root, dst_root):
            """ Create mappings between (username and uid), and (group and
            gid) for the source and destination roots. """
            with open(os.path.join(src_root, 'etc/passwd'), 'r') as f:
                for line in f:
                    try:
                        pwnam, _, uid, _ = line.split(':', 3)
                        src_uid_map[int(uid)] = pwnam
                    except ValueError as e:
                        logger.error('Failed to parse: ' + line)
                        logger.logException(e)

            with open(os.path.join(src_root, 'etc/group'), 'r') as f:
                for line in f:
                    try:
                        pwnam, _, gid, _ = line.split(':', 3)
                        src_gid_map[int(gid)] = pwnam
                    except ValueError as e:
                        logger.error('Failed to parse: ' + line)
                        logger.logException(e)

            with open(os.path.join(dst_root, 'etc/passwd'), 'r') as f:
                for line in f:
                    try:
                        pwnam, _, uid, _ = line.split(':', 3)
                        dst_uid_map[pwnam] = int(uid)
                    except ValueError as e:
                        logger.error('Failed to parse: ' + line)
                        logger.logException(e)

            with open(os.path.join(dst_root, 'etc/group'), 'r') as f:
                for line in f:
                    try:
                        pwnam, _, gid, _ = line.split(':', 3)
                        dst_gid_map[pwnam] = int(gid)
                    except ValueError as e:
                        logger.error('Failed to parse: ' + line)
                        logger.logException(e)

        # Copy attributes from a path in a source root to another path in a
        # destination root. 
        # - The ownership is copied such that it is notaffected by changes 
        #   in the underlying uid/gid. Or it's set to the specified.
        # - The mode is set to the specified.
        def copy_attributes(src_root, src_path, dst_root, dst_path, attr=None):
            dst_f = '%s/%s' % (dst_root, dst_path)

            st = os.lstat('%s/%s' % (src_root, src_path))
            try:
                user = src_uid_map[st.st_uid]
                group = src_gid_map[st.st_gid]
                if attr is not None:
                    user = attr.get('user', user)
                    group = attr.get('group', group)
                new_uid = dst_uid_map[user]
                new_gid = dst_gid_map[group]
            except IndexError as e:
                logger.error('Failed to determine ownership')
                logger.logException(e)
                return
            if st.st_uid != new_uid or st.st_gid != new_gid:
                logger.log("Update uid=%d gid=%d" % (new_uid, new_gid))
                os.lchown(dst_f, new_uid, new_gid)

            if attr is not None and 'mode' in attr:
                mode = (st.st_mode & ~0o7777) | attr['mode']
                logger.log("Update mode=0o%o" % mode)
                os.chmod(dst_f, mode)

        def restore_file(src_base, f, d=None, attr=None):
            if not d: d = f
            src = os.path.join(src_base, f)
            dst = os.path.join(mounts['root'], d)
            if os.path.lexists(src):
                logger.log("Restoring /%s" % f)
                util.assertDir(os.path.dirname(dst))
                # copy file/folder and try to preserve all attributes
                if os.path.isdir(src):
                    util.runCmd2(['cp', '-a', src, os.path.dirname(dst)])
                else:
                    util.runCmd2(['cp', '-a', src, dst])

                # copy or set mode and ownership attributes
                abs_f = os.path.join('/', f)
                abs_d = os.path.join('/', d)
                copy_attributes(src_base, abs_f, mounts['root'], abs_d, attr)
                for dirpath, dirnames, filenames in os.walk(src):
                    for i in dirnames + filenames:
                        src_path = os.path.join(dirpath, i)[len(src_base):]
                        dst_path = os.path.join(abs_d, src_path[len(abs_f) + 1:])
                        copy_attributes(src_base, src_path, mounts['root'], dst_path, attr)
            else:
                logger.log("WARNING: /%s did not exist in the backup image." % f)

        def convert_interface_rename_data(src, dest):
            """
            Convert data of interface-rename to the data consumed by xcp-networkd.
            src - the mount point of backup partition which contains the interface-rename data.
            dest - the mount point of the root partition of installation.
            """
            src_file_path = os.path.join(src, constants.INTERFACE_RENAME_DYNAMIC_RULES)
            dest_file_path = os.path.join(
                dest,
                constants.FIRSTBOOT_DATA_DIR,
                'initial_network_device_rules.conf'
            )
            net_devs = []
            if os.path.exists(src_file_path):
                net_devs = netutil.net_devs_of_last_boot(src_file_path)
            else:
                logger.log(f"The interface-rename data file {src_file_path} does not exist.")

            if not net_devs:
                return

            # The sorting result of interface-rename is to rename the interfaces to eth<N>
            pattern = re.compile(r'^eth(\d+)$')
            try:
                with open(dest_file_path, mode='w', encoding='utf-8') as f:
                    for (name, mac) in net_devs:
                        match = pattern.search(name)
                        if match:
                            position = match.group(1)
                            line = f'{position}:mac="{mac}"\n'
                            logger.log(f"Converted interface-rename data: {line}")
                            f.write(line)
                        else:
                            logger.error(f'Network device name {name} is not like ethN.')
            except Exception as e:
                logger.error(f"Failed to convert interface-rename data: {e}")

        backup_volume = partitionDevice(target_disk, backup_partnum)
        tds = util.TempMount(backup_volume, 'upgrade-src-', options=['ro'])
        try:
            restore_list = self.buildRestoreList(tds.mount_point)
            init_id_maps(tds.mount_point, mounts['root'])

            logger.log("Restoring preserved files")
            for f in restore_list:
                if isinstance(f, str):
                    restore_file(tds.mount_point, f)
                elif isinstance(f, dict):
                    if 'src' in f:
                        assert 'dst' in f
                        restore_file(tds.mount_point, f['src'], f['dst'])
                    elif 'dir' in f:
                        pat = f.get('re', None)
                        src_dir = os.path.join(tds.mount_point, f['dir'])
                        if os.path.exists(src_dir):
                            if pat is not None:
                                # filter here (though should ideally let restore_file do it)
                                for ff in os.listdir(src_dir):
                                    fn = os.path.join(f['dir'], ff)
                                    if pat.match(fn):
                                        restore_file(tds.mount_point, fn, attr=f.get('attr'))
                            else:
                                restore_file(tds.mount_point, f['dir'])

            convert_interface_rename_data(tds.mount_point, mounts['root'])

        finally:
            tds.unmount()


class ThirdGenUpgrader(Upgrader):
    """ Upgrader class for series 7+ Retail products. """
    upgrades_product = version.PRODUCT_NAME
    upgrades_versions = [ (product.XENSERVER_MIN_VERSION, product.THIS_PLATFORM_VERSION) ]
    upgrades_variants = [ 'Retail' ]
    requires_backup = True
    optional_backup = False

    def __init__(self, source):
        Upgrader.__init__(self, source)
        primary_fs = util.TempMount(self.source.root_device, 'primary-', options=['ro'])
        safe2upgrade_path = os.path.join(primary_fs.mount_point, constants.SAFE_2_UPGRADE)
        default_storage_conf_path = os.path.join(primary_fs.mount_point, "etc/firstboot.d/data/default-storage.conf")

        self.safe2upgrade = os.path.isfile(safe2upgrade_path)
        self.vgs_output = None

        self.storage_type = None
        if os.path.exists(default_storage_conf_path):
            input_data = util.readKeyValueFile(default_storage_conf_path)
            self.storage_type = input_data['TYPE']

        cert_path = os.path.join(primary_fs.mount_point, "etc/xensource/xapi-ssl.pem")
        with open(cert_path, "r") as cert_file:
            cert_text = cert_file.read()
        cert = crypto.load_certificate(crypto.FILETYPE_PEM, cert_text)
        self.key_type = cert.get_pubkey().type()
        self.key_size = cert.get_pubkey().bits()
        logger.info("ExistingInstallation %s: certificate key type %s size %s" %
                    (source, self.key_type, self.key_size))

        primary_fs.unmount()

    def testUpgradeForbidden(self, tool):
        if tool.partTableType == constants.PARTITION_DOS:
            raise RuntimeError("Upgrade from a DOS partition type is not supported.")
        if self.key_type != crypto.TYPE_RSA:
            raise RuntimeError("Current server certificate is not RSA, please regenerate it before upgrade.")
        if self.key_size < constants.RSA_MIN_KEY_SIZE:
            raise RuntimeError("Current server certificate is too small (%s bits), please regenerate before upgrade with at least %s bits." % (self.key_size, constants.MIN_KEY_SIZE))

    prepTargetStateChanges = []
    prepTargetArgs = ['primary-disk', 'boot-partnum', 'primary-partnum', 'logs-partnum', 'swap-partnum', 'storage-partnum']
    def prepareTarget(self, progress_callback, primary_disk, boot_partnum, primary_partnum, logs_partnum, swap_partnum, storage_partnum):
        """ Modify partition layout prior to installation. """

        tool = PartitionTool(primary_disk, constants.PARTITION_GPT)
        logs_partition = tool.getPartition(logs_partnum)

        # Create the new partition layout (5,2,1,4,6,3) after the backup
        # 1 - dom0 partition
        # 2 - backup partition
        # 3 - LVM partition
        # 4 - Boot partition
        # 5 - logs partition
        # 6 - swap partition

        self.testUpgradeForbidden(tool)

        if self.safe2upgrade and logs_partition is None:
            # Rename old dom0 and Boot (if any) partitions (10 and 11 are temporary number which let us create
            # dom0 and Boot partitions using the same numbers)
            tool.renamePartition(srcNumber=primary_partnum, destNumber=10, overwrite=False)
            boot_part = tool.getPartition(boot_partnum)
            if boot_part:
                tool.renamePartition(srcNumber=boot_partnum, destNumber=11, overwrite=False)
            # Create new bigger dom0 partition
            tool.createPartition(tool.ID_LINUX, sizeBytes=constants.root_size * 2**20, number=primary_partnum, label=constants.rootpart_label)
            # Create Boot partition
            tool.createPartition(tool.ID_EFI_BOOT, sizeBytes=constants.boot_size * 2**20, number=boot_partnum, label=constants.bootpart_label)
            # Create swap partition
            tool.createPartition(tool.ID_LINUX_SWAP, sizeBytes=constants.swap_size * 2**20, number=swap_partnum, label=constants.swappart_label)
            # Create storage LVM partition
            if storage_partnum > 0 and self.vgs_output:
                tool.createPartition(tool.ID_LINUX_LVM, number=storage_partnum, label=constants.storagepart_label)
            # Create logs partition using the old dom0 + Boot (if any) partitions
            tool.deletePartition(10)
            if boot_part:
                tool.deletePartition(11)
            tool.createPartition(tool.ID_LINUX, sizeBytes=constants.logs_size * 2**20, startBytes=1024*1024, number=logs_partnum, label=constants.logspart_label)

            tool.commit(log=True)

            if storage_partnum > 0 and self.vgs_output:
                storage_part = partitionDevice(primary_disk, storage_partnum)
                rc, out = util.runCmd2(['pvs', '-o', 'pv_name,vg_name', '--noheadings'], with_stdout=True)
                vgs_list = out.strip().splitlines()
                primary_dev = getMajMin(primary_disk)
                vgs_output_wrong = [i for i in vgs_list if diskutil.parentdev_from_devpath(i.strip().split()[0]) == primary_dev]
                if vgs_output_wrong:
                    vgs_output_wrong = vgs_output_wrong[0].strip()
                    if ' ' in vgs_output_wrong:
                        _, vgs_label = vgs_output_wrong.split(None, 1)
                        util.runCmd2(['vgremove', '-f', vgs_label])
                util.runCmd2(['vgcreate', self.vgs_output, storage_part])

                if self.storage_type == 'ext':
                    _, sr_uuid = self.vgs_output.split('-', 1)
                    util.runCmd2(['lvcreate', '-n', sr_uuid, '-l', '100%VG', self.vgs_output])
                    try:
                        util.mkfs('ext3', '/dev/' + self.vgs_output + '/' + sr_uuid, ['-F'])
                    except Exception as e:
                        raise RuntimeError("Backup: Failed to format filesystem on %s: %s" % (storage_part, e))
        else:
            # If the boot partition already, exists, no partition updates are
            # necessary.
            part = tool.getPartition(boot_partnum)
            if part:
                if logs_partition is not None:
                    return
            raise RuntimeError("Old partition layout is unsupported, run prepare_host_upgrade plugin and try again")

    doBackupArgs = ['primary-disk', 'backup-partnum', 'boot-partnum', 'storage-partnum', 'logs-partnum']
    doBackupStateChanges = []
    def doBackup(self, progress_callback, target_disk, backup_partnum, boot_partnum, storage_partnum, logs_partnum):

        tool = PartitionTool(target_disk)
        boot_part = tool.getPartition(boot_partnum)
        boot_device = partitionDevice(target_disk, boot_partnum) if boot_part else None
        logs_partition = tool.getPartition(logs_partnum)

        self.testUpgradeForbidden(tool)

        # Check if possible to create new partition layout, increasing the size, using plugin result
        if self.safe2upgrade and logs_partition is None: 
            if storage_partnum > 0:
                # Get current Volume Group
                rc, out = util.runCmd2(['pvs', '-o', 'pv_name,vg_name', '--noheadings'], with_stdout=True)
                vgs_list = out.strip().splitlines()
                target_dev = getMajMin(target_disk)
                self.vgs_output = [i for i in vgs_list if diskutil.parentdev_from_devpath(i.strip().split()[0]) == target_dev]
                if self.vgs_output:
                    self.vgs_output = self.vgs_output[0]
                    self.vgs_output = self.vgs_output.split()[1]
                    self.vgs_output = self.vgs_output.strip()
                    # Remove current Volume Group
                    util.runCmd2(['vgremove', '-f', self.vgs_output])
                    # Remove LVM Phisical Volume
                    storage_part = partitionDevice(target_disk, storage_partnum)
                    util.runCmd2(['pvremove', storage_part])
                # Delete LVM partition
                tool.deletePartition(storage_partnum)
            # Resize backup partition
            tool.resizePartition(number=backup_partnum, sizeBytes=constants.backup_size * 2**20)
            # Write partition table
            tool.commit(log=True)

        # format the backup partition:
        backupfs_type = diskutil.fs_type_from_device(self.source.root_device)
        backup_partition = partitionDevice(target_disk, backup_partnum)
        try:
            util.mkfs(backupfs_type, backup_partition)
        except Exception as e:
            raise RuntimeError("Backup: Failed to format filesystem on %s: %s" % (backup_partition, e))
        progress_callback(10)

        # copy the files across:
        primary_fs = util.TempMount(self.source.root_device, 'primary-', options=['ro'], boot_device=boot_device)
        try:
            backup_fs = util.TempMount(backup_partition, 'backup-')
            try:
                just_dirs = ['dev', 'proc', 'lost+found', 'sys']
                top_dirs = os.listdir(primary_fs.mount_point)
                val = 10
                for x in top_dirs:
                    if x in just_dirs:
                        path = os.path.join(backup_fs.mount_point, x)
                        if not os.path.exists(path):
                            os.mkdir(path, 0o755)
                    else:
                        cmd = ['cp', '-a'] + \
                              [ os.path.join(primary_fs.mount_point, x) ] + \
                              ['%s/' % backup_fs.mount_point]
                        if util.runCmd2(cmd) != 0:
                            raise RuntimeError("Backup of %s directory failed" % x)
                    val += 90 / len(top_dirs)
                    progress_callback(val)

                # save the GPT table
                rc, err = util.runCmd2(["sgdisk", "-b", os.path.join(backup_fs.mount_point, '.xen-gpt.bin'), target_disk], with_stderr=True)
                if rc != 0:
                    raise RuntimeError("Failed to save partition layout: %s" % err)
            finally:
                # replace rolling pool upgrade bootloader config
                def replace_config(config_file, destination):
                    src = os.path.join(backup_fs.mount_point, constants.ROLLING_POOL_DIR, config_file)
                    if os.path.exists(src):
                        util.runCmd2(['cp', '-f', src, os.path.join(backup_fs.mount_point, destination)])

                configMaps = [("efi-grub.cfg", "boot/efi/EFI/xenserver/grub.cfg"),
                                ("grub.cfg", "boot/grub"),
                                ("menu.lst", "boot/grub"),
                                ("extlinux.conf", "boot")]
                for config_file, destination in configMaps:
                    replace_config(config_file, destination)

                fh = open(os.path.join(backup_fs.mount_point, '.xen-backup-partition'), 'w')
                fh.close()
                backup_fs.unmount()
        finally:
            primary_fs.unmount()

    prepUpgradeArgs = []
    prepStateChanges = ['installation-uuid', 'control-domain-uuid']
    def prepareUpgrade(self, progress_callback):
        """ Try to preserve the installation and control-domain UUIDs from
        xensource-inventory."""
        try:
            installID = self.source.getInventoryValue("INSTALLATION_UUID")
            controlID = self.source.getInventoryValue("CONTROL_DOMAIN_UUID")
        except KeyError:
            raise RuntimeError("Required information (INSTALLATION_UUID, CONTROL_DOMAIN_UUID) was missing from your xensource-inventory file.  Aborting installation; please replace these keys and try again.")

        return installID, controlID

    def buildRestoreList(self, src_base):
        restore_list = []
        restore_list += ['etc/xensource/ptoken', 'etc/xensource/pool.conf',
                         'etc/xensource/xapi-ssl.pem', 'etc/xensource/xapi-pool-tls.pem']
        restore_list.append({'dir': 'etc/ssh', 're': re.compile(r'.*/ssh_host_.+_key$'),
                             'attr': {'mode': 0o0600, 'group': 'root'}})
        restore_list.append({'dir': 'etc/ssh', 're': re.compile(r'.*/ssh_host_.+_key\.pub$')})

        restore_list += [ 'etc/sysconfig/network']
        restore_list.append({'dir': 'etc/sysconfig/network-scripts', 're': re.compile(r'.*/ifcfg-[a-z0-9.]+')})

        restore_list += [constants.XAPI_DB, 'etc/xensource/license']
        restore_list += [constants.CLUSTERD_CONF]
        restore_list.append({'dir': constants.FIRSTBOOT_DATA_DIR, 're': re.compile(r'.*.conf')})

        restore_list += ['etc/xensource/syslog.conf']

        restore_list.append({'src': 'etc/xensource-inventory', 'dst': 'var/tmp/.previousInventory'})

        # CP-1508: preserve AD config
        restore_list += ['etc/resolv.conf', 'etc/krb5.conf', 'etc/krb5.keytab']
        restore_list.append({'dir': 'var/lib/samba'})

        # CP-35398: Integrate automatic upgrade tool from PBIS to winbind
        restore_list += ['var/lib/pbis/db/registry.db']

        # CA-47142: preserve v6 cache
        restore_list += ['var/lib/xcp/lpe-cache']

        # CP-2056: preserve RRDs etc
        restore_list += ['var/lib/xcp/blobs']

        restore_list.append('etc/sysconfig/mkinitrd.latches')

        # CA-67890: preserve root's ssh state
        restore_list += [{'dir': 'root/.ssh'}]

        # CA-82709: preserve networkd.db for Tampa upgrades
        restore_list.append(constants.NETWORK_DB)

        # CP-9653: preserve Oracle 5 blacklist
        restore_list += ['etc/pygrub/rules.d/oracle-5.6']

        # CA-150889: backup multipath config
        restore_list.append({'src': 'etc/multipath.conf', 'dst': 'etc/multipath.conf.bak'})

        restore_list += ['etc/locale.conf', 'etc/machine-id', 'etc/vconsole.conf']

        # CP-12750: Increase log size when dedicated partion is on the disk
        restore_list += ['etc/sysconfig/logrotate']

        # CA-195388: Preserve /etc/mdadm.conf across upgrades
        restore_list += ['etc/mdadm.conf']

        # CA-413099: Preserve /var/lib/xcp/verify-certificates for certificates checking enable/disable status
        restore_list += ['var/lib/xcp/verify-certificates']

        # CP-42523: NRPE service config
        restore_list += ['etc/nagios/nrpe.cfg', {'dir': 'etc/nrpe.d'}]
        nrpe_service = 'etc/systemd/system/multi-user.target.wants/nrpe.service'
        if os.path.islink(os.path.join(src_base, nrpe_service)):
            restore_list += [nrpe_service]

        # CP-44441: SNMP service config
        # From XS 8.4 SNMP feature is supported, and new file /etc/snmp/snmp.xs.conf is added
        # into dom0, the file can be treated as the feature flag. Host installer must not
        # restore old default config files prior to XS 8.4 because they are incompatible with
        # xapi snmp plugin.
        snmp_xs_conf = 'etc/snmp/snmp.xs.conf'
        if os.path.isfile(os.path.join(src_base, snmp_xs_conf)):
            restore_list += [snmp_xs_conf, 'etc/snmp/snmpd.xs.conf',
                             'etc/sysconfig/snmpd', 'var/lib/net-snmp/snmpd.conf']
        snmp_service = 'etc/systemd/system/multi-user.target.wants/snmpd.service'
        if os.path.islink(os.path.join(src_base, snmp_service)):
            restore_list += [snmp_service]

        # Preserve pool certificates across upgrades
        restore_list += ['etc/stunnel/xapi-pool-ca-bundle.pem', {'dir': 'etc/stunnel/certs-pool'}]
        restore_list += ['etc/stunnel/xapi-stunnel-ca-bundle.pem', {'dir': 'etc/stunnel/certs'}]
        restore_list += [{'dir': 'etc/trusted-certs'}]

        # XAPI firewall-port plugin
        restore_list += ['etc/sysconfig/iptables']

        return restore_list

    completeUpgradeArgs = ['mounts', 'installation-to-overwrite', 'primary-disk', 'backup-partnum', 'logs-partnum', 'net-admin-interface', 'net-admin-bridge', 'net-admin-configuration']
    def completeUpgrade(self, mounts, prev_install, target_disk, backup_partnum, logs_partnum, admin_iface, admin_bridge, admin_config):

        util.assertDir(os.path.join(mounts['root'], "var/lib/xcp"))
        util.assertDir(os.path.join(mounts['root'], "etc/xensource"))

        Upgrader.completeUpgrade(self, mounts, target_disk, backup_partnum)

        v = Version(prev_install.version.ver)
        f = open(os.path.join(mounts['root'], 'var/tmp/.previousVersion'), 'w')
        f.write("PLATFORM_VERSION='%s'\n" % v)
        f.close()

        state = open(os.path.join(mounts['root'], constants.FIRSTBOOT_DATA_DIR, 'host.conf'), 'w')
        print("UPGRADE=true", file=state)
        state.close()

        net_dict = util.readKeyValueFile(os.path.join(mounts['root'], 'etc/sysconfig/network'))
        if net_dict.get('NETWORKING_IPV6', 'no') == 'no':
            nfd = open(os.path.join(mounts['root'], 'etc/sysconfig/network'), 'a')
            nfd.write("NETWORKING_IPV6=no\n")
            nfd.close()
            netutil.disable_ipv6_module(mounts["root"])

        # handle the conversion of devices from aacraid to smartpqi
        primary_disk = self.source.getInventoryValue("PRIMARY_DISK")
        target_link = diskutil.idFromPartition(target_disk) or target_disk
        if primary_disk.startswith('/dev/disk/by-id/scsi-') and \
                target_link.startswith('/dev/disk/by-id/scsi-') and \
                primary_disk != target_link:
            for i in (os.path.join(constants.FIRSTBOOT_DATA_DIR, 'default-storage.conf'),
                      constants.XAPI_DB):
                util.runCmd2(['sed', '-i', '-e', "s#%s#%s#g" % (primary_disk, target_link),
                              os.path.join(mounts['root'], i)])

class InCloudSphereUpgrader(ThirdGenUpgrader):
    """Upgrader which supports upgrading from releases of InCloud Sphere with
    an incorrect product name. Workaround for CA-263669."""

    @classmethod
    def upgrades(cls, product, platform_version, variant):
        return (version.PRODUCT_NAME == 'incloudsphere' and
                product == 'xenenterprise' and
                platform_version in (Version([1, 9, 0]),  # ICS 4.0
                                     Version([2, 1, 0]))) # ICS 4.5

################################################################################

# Upgraders provided here, in preference order:
class UpgraderList(list):
    def getUpgrader(self, product, version, variant):
        for x in self:
            if x.upgrades(product, version, variant):
                return x
        raise KeyError("No upgrader found for %s" % version)

    def hasUpgrader(self, product, version, variant):
        for x in self:
            if x.upgrades(product, version, variant):
                return True
        return False

__upgraders__ = UpgraderList([ThirdGenUpgrader, InCloudSphereUpgrader])

def filter_for_upgradeable_products(installed_products):
    upgradeable_products = [p for p in installed_products if p.isUpgradeable() and upgradeAvailable(p)]
    return upgradeable_products
