import os
import shutil
import tempfile
import unittest

import disktools


class TestMdDevicesBlocking(unittest.TestCase):
    """getMdDevicesBlocking() on a fake /proc/mdstat and a fake /sys/class/block,
    laid out as the kernel does: /sys/class/block/<dev> links to
    .../block/<disk>[/<partition>], partitions carry a `partition` file."""

    DISKS = {'nvme0n1': ['nvme0n1p1', 'nvme0n1p2'], 'nvme1n1': ['nvme1n1p1', 'nvme1n1p2'],
             'sda': [], 'sdb': []}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        devices = os.path.join(self.tmp, 'devices')
        classdir = os.path.join(self.tmp, 'class')
        os.makedirs(classdir)
        for disk, parts in self.DISKS.items():
            os.makedirs(os.path.join(devices, disk))
            os.symlink(os.path.join(devices, disk), os.path.join(classdir, disk))
            for part in parts:
                os.makedirs(os.path.join(devices, disk, part))
                open(os.path.join(devices, disk, part, 'partition'), 'w').close()
                os.symlink(os.path.join(devices, disk, part), os.path.join(classdir, part))
        self.mdstat = os.path.join(self.tmp, 'mdstat')
        self.saved = disktools.SYS_CLASS_BLOCK, disktools.PROC_MDSTAT
        disktools.SYS_CLASS_BLOCK, disktools.PROC_MDSTAT = classdir, self.mdstat

    def tearDown(self):
        disktools.SYS_CLASS_BLOCK, disktools.PROC_MDSTAT = self.saved
        shutil.rmtree(self.tmp)

    def blocking(self, mdstat, disks):
        with open(self.mdstat, 'w') as fh:
            fh.write(mdstat)
        return disktools.getMdDevicesBlocking(disks)

    def test_raid_on_partitions(self):
        # a Debian-style RAID1 over partitions, as left by a provider image
        mdstat = ("Personalities : [raid1]\n"
                  "md0 : active raid1 nvme1n1p2[1] nvme0n1p2[0]\n"
                  "      937035776 blocks super 1.2 [2/2] [UU]\n"
                  "\n"
                  "unused devices: <none>\n")
        self.assertEqual(self.blocking(mdstat, ['/dev/nvme0n1']), ['/dev/md0'])
        self.assertEqual(self.blocking(mdstat, ['/dev/sda']), [])

    def test_auto_read_only(self):
        mdstat = "md127 : active (auto-read-only) raid1 nvme0n1p2[0] nvme1n1p2[1]\n"
        self.assertEqual(self.blocking(mdstat, ['/dev/nvme1n1']), ['/dev/md127'])

    def test_inactive(self):
        mdstat = "md127 : inactive nvme0n1p2[0](S)\n      468517888 blocks super 1.2\n"
        self.assertEqual(self.blocking(mdstat, ['/dev/nvme0n1']), ['/dev/md127'])

    def test_whole_disks(self):
        # what getMdDevicesUsing() already handled
        mdstat = "md126 : active raid1 sdb[1] sda[0]\n"
        self.assertEqual(self.blocking(mdstat, ['/dev/sda', '/dev/sdb']), ['/dev/md126'])
        self.assertEqual(self.blocking(mdstat, ['/dev/nvme0n1']), [])

    def test_prefix_is_not_a_match(self):
        # sda must not match a member on sdaa, nor nvme0n1 one on nvme0n10
        mdstat = "md1 : active raid1 sdaa[0] nvme0n10p1[1]\n"
        self.assertEqual(self.blocking(mdstat, ['/dev/sda', '/dev/nvme0n1']), [])

    def test_no_mdstat(self):
        disktools.PROC_MDSTAT = os.path.join(self.tmp, 'missing')
        self.assertEqual(disktools.getMdDevicesBlocking(['/dev/nvme0n1']), [])


if __name__ == '__main__':
    unittest.main()
