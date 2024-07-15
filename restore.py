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
    disk_device = backup.root_disk
    tool = PartitionTool(disk_device)
    disk = diskutil.probeDisk(disk_device)
    create_sr_part = disk.storage[0] is not None
    boot_partnum = tool.partitionNumber(disk.boot[1]) if disk.boot[0] else -1

    backup_fs = util.TempMount(backup.partition, 'backup-', options=['ro'])
    inventory = util.readKeyValueFile(os.path.join(backup_fs.mount_point, constants.INVENTORY_FILE), strip_quotes=True)
    backup_partition_layout = inventory['PARTITION_LAYOUT'].split(',')
    backup_fs.unmount()

    logger.log("BACKUP DISK PARTITION LAYOUT: %s" % backup_partition_layout)

    backup_partition = backup.partition

    assert backup_partition.startswith('/dev/')
    assert disk_device.startswith('/dev/')

    restore_partition = disk.root[1]
    if not isinstance(restore_partition, str):
        raise RuntimeError("failed to identify root partition to restore to, disk=%s" % disk)
    logger.log("Restoring to partition %s." % restore_partition)

    boot_part = tool.getPartition(boot_partnum)
    boot_device = disk.boot[1] if boot_part else None
    efi_boot = boot_part and boot_part['id'] == GPTPartitionTool.ID_EFI_BOOT

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

    # should we restore old schema and move to MBR?
    restore_partitions = None
    if 'BOOT' not in backup_partition_layout and 'LOG' not in backup_partition_layout:
        new_restore_partition = restore_partition
        efi_boot = False

        # remove unneeded partitions
        if disk.swap[0]:
            tool.deletePartition(tool.partitionNumber(disk.swap[1])) # swap
        tool.deletePartitionIfPresent(boot_partnum) # boot

        # use logs as root
        root_partnum = tool.partitionNumber(disk.root[1])
        backup_partnum = tool.partitionNumber(backup_partition)
        if disk.logs[0]:
            logs_partnum = tool.partitionNumber(disk.logs[1])
            if (tool.partitionStart(logs_partnum) < tool.partitionStart(root_partnum) and
                    tool.partitionSize(logs_partnum) >= constants.root_gpt_size_old):
                restore_partition = disk.logs[1]
                tool.deletePartition(root_partnum)
                tool.renamePartition(logs_partnum, root_partnum)
            else:
                tool.deletePartition(tool.partitionNumber(disk.logs[1])) # logs

        # rewrite with MBR format, we need to manually convert IDs from GPT to MBR
        # Potentially partitions are not created with the same original alignment, but this is not an issue
        # with modern hardware.
        assert len(tool.partitions) <= 4
        new_tool = PartitionTool(tool.device, constants.PARTITION_DOS)
        for number in list(new_tool.partitions):
            new_tool.deletePartition(number)
        for number in tool.partitions:
            if number == backup_partnum:
                continue
            assert number >= 1 and number <= 4
            part = tool.getPartition(number)
            # convert id from GPT to MBR
            id = new_tool.ID_LINUX
            if part['id'] == tool.ID_LINUX_SWAP:
                id = new_tool.ID_LINUX_SWAP
            elif part['id'] == tool.ID_LINUX_LVM:
                id = new_tool.ID_LINUX_LVM
            elif part['id'] == tool.ID_LINUX:
                rv, out = util.runCmd2(['blkid', '-s', 'TYPE', '-o', 'value', partitionDevice(tool.device, number)], with_stdout=True)
                if rv == 0 and 'vfat' in out:
                    id = 0x1c if part.get('hidden', False) else 0x0c
            # if root make it active
            active = (number == root_partnum)
            new_tool.createPartition(id, tool.partitionSize(number), number, startBytes=tool.partitionStart(number), active=active)
        # create new backup parition
        new_backup_partnum = root_partnum + 1
        new_tool.createPartition(new_tool.ID_LINUX, constants.backup_size_old * 2**20, new_backup_partnum, order=new_backup_partnum)

        def restore_partitions():
            """Restore old partition schema.
            Returns new PartitionTool object and new restore partition.
            """

            new_tool.writePartitionTable()
            # Clear GPT header, sfdisk doesn't do it properly.
            # We do this way to avoid intermediate states without partitions.
            if tool.partTableType == constants.PARTITION_GPT and new_tool.partTableType == constants.PARTITION_DOS:
                clear_gpt_headers(tool)
            util.mkfs(constants.rootfs_type, partitionDevice(new_tool.device, new_backup_partnum))
            return (new_tool, new_restore_partition)

    # mount the backup fs
    backup_fs = util.TempMount(backup_partition, 'restore-backup-', options=['ro'])
    try:
        # extract the bootloader config
        boot_config = bootloader.Bootloader.loadExisting(backup_fs.mount_point)
        if boot_config.src_fmt == 'grub':
            raise RuntimeError("Backup uses grub bootloader which is no longer supported - " + \
                "to restore please use a version of the installer that matches the backup partition")

        # format the restore partition(s):
        try:
            util.mkfs(constants.rootfs_type, restore_partition)
        except Exception as e:
            logger.critical("Failed to create root filesystem", exc_info=1)
            raise RuntimeError("Failed to create root filesystem: %s" % e)

        if efi_boot:
            try:
                util.mkfs('vfat', boot_device)
            except Exception as e:
                raise RuntimeError("Failed to create boot filesystem: %s" % e)

        # mount restore partition:
        dest_fs = util.TempMount(restore_partition, 'restore-dest-')
        efi_mounted = False
        try:
            mounts = {'root': dest_fs.mount_point, 'boot': os.path.join(dest_fs.mount_point, 'boot')}
            if efi_boot:
                mounts['esp'] = os.path.join(dest_fs.mount_point, 'boot', 'efi')
                os.makedirs(mounts['esp'])
                util.mount(boot_device, mounts['esp'])
                efi_mounted = True

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

            # restore bootloader configuration
            dst_file = boot_config.src_file.replace(backup_fs.mount_point, dest_fs.mount_point, 1)
            util.assertDir(os.path.dirname(dst_file))
            boot_config.commit(dst_file)

            # repartition if needed
            backup_fs.unmount()
            if restore_partitions:
                dest_fs.unmount()
                (tool, restore_partition) = restore_partitions()
                dest_fs = util.TempMount(restore_partition, 'restore-dest-')

            # prepare extra mounts for installing bootloader:
            util.bindMount("/dev", "%s/dev" % dest_fs.mount_point)
            util.bindMount("/sys", "%s/sys" % dest_fs.mount_point)
            util.bindMount("/proc", "%s/proc" % dest_fs.mount_point)

            # restore boot loader
            if boot_config.src_fmt == 'grub2':
                if efi_boot:
                    branding = dict(inventory)
                    branding['product-brand'] = branding['PRODUCT_BRAND']
                    backend.setEfiBootEntry(mounts, disk_device, boot_partnum, constants.INSTALL_TYPE_RESTORE, branding)
                else:
                    if location == constants.BOOT_LOCATION_MBR:
                        backend.installGrub2(mounts, disk_device, False)
                    else:
                        backend.installGrub2(mounts, restore_partition, True)
            else:
                backend.installExtLinux(mounts, disk_device, probePartitioningScheme(disk_device), location)
        finally:
            util.umount("%s/proc" % dest_fs.mount_point)
            util.umount("%s/sys" % dest_fs.mount_point)
            util.umount("%s/dev" % dest_fs.mount_point)
            if efi_mounted:
                util.umount(mounts['esp'])
            dest_fs.unmount()
    finally:
        backup_fs.unmount()

    if not label:
        raise RuntimeError("Failed to find label required for root filesystem.")
    if efi_boot and not bootlabel:
        raise RuntimeError("Failed to find label required for boot filesystem.")

    if util.runCmd2(['e2label', restore_partition, label]) != 0:
        raise RuntimeError("Failed to label root partition")

    if bootlabel:
        if util.runCmd2(['fatlabel', boot_device, bootlabel]) != 0:
            raise RuntimeError("Failed to label boot partition")

    if 'LOG' in backup_partition_layout: # From 7.x (new layout) to 7.x (new layout)
        if boot_partnum >= 0:
            tool.commitActivePartitiontoDisk(boot_partnum)
        rdm_label = label.split("-")[1]
        logs_part = disk.logs[1]
        swap_part = disk.swap[1]
        if util.runCmd2(['e2label', logs_part, constants.logsfs_label%rdm_label]) != 0:
            raise RuntimeError("Failed to label logs partition")
        if util.runCmd2(['swaplabel', '-L', constants.swap_label%rdm_label, swap_part]) != 0:
            raise RuntimeError("Failed to label swap partition")

def clear_gpt_headers(tool):
    """This functions clears GPT header and footer.
    It should be called in case MBR tools doesn't do it properly.
    """

    assert tool.partTableType == constants.PARTITION_GPT
    f = open(tool.device, 'r+b')

    # header
    f.seek(tool.sectorSize)
    f.write(b'\x00' * tool.sectorSize)

    # footer
    f.seek((tool.sectorExtent - 1) * tool.sectorSize)
    f.write(b'\x00' * tool.sectorSize)

    f.close()
