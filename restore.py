# SPDX-License-Identifier: GPL-2.0-only

import backend
import product
from disktools import *
import diskutil
import util
import os
import os.path
import constants
import re
import tempfile
import shutil
import xcp.bootloader as bootloader
from xcp import logger

def restoreFromBackup(backup, progress=lambda x: ()):
    """ Restore files from backup_partition to the root partition on disk.
    Call progress with a value between 0 and 100.  Re-install bootloader.  Fails if
    backup is not same version as the CD in use."""

    label = None
    bootlabel = None
    disk = backup.root_disk
    tool = PartitionTool(disk)
    dsk = diskutil.probeDisk(disk, raid_members = diskutil.RAID_MEMBERS_PROBE_MEMBER)
    create_sr_part = dsk.storage[0] is not None
    primary_partnum, backup_partnum, _, boot_partnum, logs_partnum, swap_partnum = backend.partitionTargetDisk(disk, None, constants.PRESERVE_IF_UTILITY, create_sr_part)

    backup_fs = util.TempMount(backup.partition, 'backup-', options=['ro'])
    inventory = util.readKeyValueFile(os.path.join(backup_fs.mount_point, constants.INVENTORY_FILE), strip_quotes=True)
    backup_partition_layout = inventory['PARTITION_LAYOUT'].split(',')
    backup_fs.unmount()

    logger.log("BACKUP DISK PARTITION LAYOUT: %s" % backup_partition_layout)

    backup_partition = backup.partition

    assert backup_partition.startswith('/dev/')
    assert disk.startswith('/dev/')

    restore_partition = partitionDevice(disk, primary_partnum)
    logger.log("Restoring to partition %s." % restore_partition)

    boot_part = tool.getPartition(boot_partnum)
    boot_device = partitionDevice(disk, boot_partnum) if boot_part else None

    # determine current location of bootloader
    current_location = 'unknown'
    try:
        root_fs = util.TempMount(restore_partition, 'root-', options=['ro'], boot_device=boot_device)
        try:
            boot_config = bootloader.Bootloader.loadExisting(root_fs.mount_point)
            current_location = boot_config.location
            logger.log("Bootloader currently in %s" % current_location)
        finally:
            root_fs.unmount()
    except:
        pass

    # mount the backup fs
    backup_fs = util.TempMount(backup_partition, 'restore-backup-', options=['ro'])
    try:
        # extract the bootloader config
        boot_config = bootloader.Bootloader.loadExisting(backup_fs.mount_point)
        if boot_config.src_fmt == 'grub':
            raise RuntimeError("Backup uses grub bootloader which is no longer supported - " + \
                "to restore please use a version of the installer that matches the backup partition")

        # format the restore partition(s):
        restore_fs_type = diskutil.fs_type_from_device(backup_partition)
        try:
            util.mkfs(restore_fs_type, restore_partition)
        except Exception as e:
            logger.critical("Failed to create root filesystem", exc_info=1)
            raise RuntimeError("Failed to create root filesystem: %s" % e)

        # format the logs partition if the fs_type is changing
        logs_partition = partitionDevice(disk, logs_partnum)
        if restore_fs_type != diskutil.fs_type_from_device(logs_partition):
            try:
                util.mkfs(restore_fs_type, logs_partition)
            except OSError as e:
                raise RuntimeError("Failed to format logs filesystem (%s): %s" % (restore_fs_type, e))

        try:
            util.mkfs('vfat', boot_device)
        except Exception as e:
            raise RuntimeError("Failed to create boot filesystem: %s" % e)

        # mount restore partition:
        dest_fs = util.TempMount(restore_partition, 'restore-dest-')
        try:
            mounts = {'root': dest_fs.mount_point,
                        'boot': os.path.join(dest_fs.mount_point, 'boot'),
                        'esp': os.path.join(dest_fs.mount_point, 'boot', 'efi')}
            os.makedirs(mounts['esp'])
            util.mount(boot_device, mounts['esp'])

            # copy files from the backup partition to the restore partition:
            objs = [x for x in os.listdir(backup_fs.mount_point) if x not in ['lost+found', '.xen-backup-partition', '.xen-gpt.bin']]
            for i in range(len(objs)):
                obj = objs[i]
                logger.log("Restoring subtree %s..." % obj)
                progress((i * 100) / len(objs))

                # Use 'cp' here because Python's copying tools are useless and
                # get stuck in an infinite loop when copying e.g. /dev/null.
                if util.runCmd2(['cp', '-a', os.path.join(backup_fs.mount_point, obj),
                                 dest_fs.mount_point]) != 0:
                    raise RuntimeError("Failed to restore %s directory" % obj)

            logger.log("Data restoration complete.  About to re-install bootloader.")

            location = boot_config.location
            m = re.search(r'root=LABEL=(\S+)', boot_config.menu[boot_config.default].kernel_args)
            if m:
                label = m.group(1)
            if location == constants.BOOT_LOCATION_PARTITION and current_location == constants.BOOT_LOCATION_MBR:
                # if bootloader in the MBR it's probably not safe to restore with it
                # on the partition
                logger.log("Bootloader is currently installed to MBR, restoring to MBR instead of partition")
                location = constants.BOOT_LOCATION_MBR

            with open(os.path.join(backup_fs.mount_point, 'etc', 'fstab'), 'r') as fstab:
                for line in fstab:
                    m = re.match(r'LABEL=(\S+)\s+/boot/efi\s', line)
                    if m:
                        bootlabel = m.group(1)

            if diskutil.is_raid(disk):
                backend.waitForSWRAIDSync(disk)

                # umount and re-mount partitions to flush the cache
                # umount all partitions first before re-mounting
                util.umount(mounts['esp'])
                dest_fs.unmount()

                # Restart the SW RAID device
                physical_disks = diskutil.getSWRAIDDevices(disk)
                if len(physical_disks) == 0:
                    raise RuntimeError("Failed to identify physical devices of SWRAID device: %s" % disk)

                assembleCommand = ["mdadm", "--assemble", "--run", disk, physical_disks[0]]
                if len(physical_disks) == 2:
                    assembleCommand.append(physical_disks[1])

                diskutil.stopSWRAID(disk)
                if util.runCmd2(assembleCommand) != 0:
                    raise RuntimeError("Failed to re-assemble SWRAID device: %s" % disk)

                dest_fs = util.TempMount(restore_partition, 'restore-dest-')
                os.makedirs(mounts['esp'])
                util.mount(boot_device, mounts['esp'])
            else:
                physical_disks = [disk]

            # prepare extra mounts for installing bootloader:
            util.bindMount("/dev", "%s/dev" % dest_fs.mount_point)
            util.bindMount("/sys", "%s/sys" % dest_fs.mount_point)
            util.bindMount("/proc", "%s/proc" % dest_fs.mount_point)

            branding = util.readKeyValueFile(os.path.join(backup_fs.mount_point, constants.INVENTORY_FILE))
            branding['product-brand'] = branding['PRODUCT_BRAND']
            backend.writeBootEntries(True, mounts, physical_disks, boot_partnum, constants.INSTALL_TYPE_RESTORE, branding)

            # restore bootloader configuration
            dst_file = boot_config.src_file.replace(backup_fs.mount_point, dest_fs.mount_point, 1)
            util.assertDir(os.path.dirname(dst_file))
            boot_config.commit(dst_file)
        finally:
            util.umount("%s/proc" % dest_fs.mount_point)
            util.umount("%s/sys" % dest_fs.mount_point)
            util.umount("%s/dev" % dest_fs.mount_point)
            util.umount(mounts['esp'])
            dest_fs.unmount()
    finally:
        backup_fs.unmount()

    if not label:
        raise RuntimeError("Failed to find label required for root filesystem.")
    if not bootlabel:
        raise RuntimeError("Failed to find label required for boot filesystem.")

    if util.runCmd2(['e2label', restore_partition, label]) != 0:
        raise RuntimeError("Failed to label root partition")

    if bootlabel:
        if util.runCmd2(['fatlabel', boot_device, bootlabel]) != 0:
            raise RuntimeError("Failed to label boot partition")

    if 'LOG' in backup_partition_layout: # From 7.x (new layout) to 7.x (new layout)
        tool.commitActivePartitiontoDisk(boot_partnum)
        rdm_label = label.split("-")[1]
        logs_part = partitionDevice(disk, logs_partnum)
        swap_part = partitionDevice(disk, swap_partnum)
        if util.runCmd2(['e2label', logs_part, constants.logsfs_label%rdm_label]) != 0:
            raise RuntimeError("Failed to label logs partition")
        if util.runCmd2(['swaplabel', '-L', constants.swap_label%rdm_label, swap_part]) != 0:
            raise RuntimeError("Failed to label swap partition")
