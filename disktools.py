# SPDX-License-Identifier: GPL-2.0-only

import constants
import errno
import re, subprocess, types, os, time
import ppexpect
from pprint import pprint
from copy import copy, deepcopy
import util
from xcp import logger

class Segment:
    """Segments are areas, e.g. disk partitions or LVM segments, defined by start address and size"""
    def __init__(self, start, size):
        self.start = start
        self.size = size

    def end(self):
        return self.start + self.size

    def __repr__(self):
        repr = { 'end' : self.end() }
        repr.update(self.__dict__)
        return str(repr)

class MoveChunk:
    """MoveChunks represent a move.  They contain source and destination addresses"""
    def __init__(self, src, dest, size):
        self.src = src
        self.dest = dest
        self.size = size

    def __repr__(self):
        return str(self.__dict__)

class FreePool:
    """FreePool manages the allotment of segments a pool of free segments, and
    divides segments as necessary to fill the requested size exactly"""
    def __init__(self, freeSegments, usedThreshold=0):
        self.freeSegments = freeSegments
        # Instead of altering the free segment list as free space is consumed by takeSegments,
        # this class maintains a usedThreshold address.  Addresses lower than the threshold
        # have already been used, and those at or above it are still available
        self.usedThreshold = usedThreshold

    def freeSpace(self):
        sizeLeft = 0
        for seg in self.freeSegments:
            freeSize = min(seg.size, seg.end() - self.usedThreshold)
            if freeSize > 0:
                sizeLeft += freeSize

        return sizeLeft

    def takeSegments(self, size):
        """Returns a LIST of segments that fill the requested size, and effectively removes
        those segments from the free pool by increasing usedThreshold"""
        initialFreeSpace = self.freeSpace()
        segsToTake = []
        sizeLeft = size
        for seg in self.freeSegments:
            availableStart = max(seg.start, self.usedThreshold)
            sizeToTake = min(seg.end() - availableStart, sizeLeft)
            if sizeToTake > 0:
                takenSegment = Segment(availableStart, sizeToTake)
                segsToTake.append(takenSegment)
                self.usedThreshold = takenSegment.end()
                sizeLeft -= takenSegment.size
            assert sizeLeft >= 0 # Underflow implies a logic error

        if sizeLeft > 0:
            raise Exception("Disk allocation failed - out of space")

        assert size == sum([seg.size for seg in segsToTake]) # Check we've allocated the size required
        assert size == initialFreeSpace - self.freeSpace() # Check that free space has shrunk by the right amount

        return segsToTake

    def __repr__(self):
        return str(self.__dict__)

class LVMTool:
    # Separation character - mustn't appear in anything we expect back from pvs/vgs/lvs
    SEP = '#'

    # Evacuate this many more extents than pvresize theoretically requires
    PVRESIZE_EXTENT_MARGIN = 0

    # Volume group prefixes
    VG_SWAP_PREFIX = 'VG_XenSwap'
    VG_CONFIG_PREFIX = 'VG_XenConfig'
    # Prefix used by the SR type 'lvm'
    VG_SR_PREFIX = 'VG_XenStorage'
    # Prefix for non-'lvm' local storage SR types
    VG_OTHER_SR_PREFIX = 'XSLocal'

    PVMOVE = ['pvmove']
    LVCHANGE = ['lvchange']
    LVREMOVE = ['lvremove']
    VGCHANGE = ['vgchange']
    VGREMOVE = ['vgremove']
    PVREMOVE = ['pvremove']
    PVRESIZE = ['pvresize']

    VGS_INFO = { # For one-per-VG records
        'command' : ['/sbin/lvm', 'vgs'],
        'arguments' : ['--noheadings', '--nosuffix', '--units', 'b', '--separator', SEP],
        'string_options' : ['vg_name'],
        'integer_options' : []
    }

    LVS_SEG_INFO = { # For one-per-LV-segment records
        'command' : ['/sbin/lvm', 'lvs'],
        'arguments' : ['--noheadings', '--nosuffix', '--units', 'b', '--separator', SEP, '--segments'],
        'string_options' : ['seg_pe_ranges'],
        'integer_options' : []
    }
    LVS_INFO = { # For one-per-LV records
        'command' : ['/sbin/lvm', 'lvs'],
        'arguments' : ['--noheadings', '--nosuffix', '--units', 'b', '--separator', SEP],
        'string_options' : ['lv_name', 'vg_name'],
        'integer_options' : []
    }
    PVS_INFO = { # For one-per-PV records
        'command' : ['/sbin/lvm', 'pvs'],
        'arguments' : ['--noheadings', '--nosuffix', '--units', 'b', '--separator', SEP],
        'string_options' : ['pv_name', 'vg_name'],
        'integer_options' : ['pe_start', 'pv_size', 'pv_free', 'pv_pe_count', 'dev_size']
    }

    def __init__(self):
        self.readAllInfo()
        self.pvsToDelete = []
        self.vgsToDelete = []
        self.lvsToDelete = []
        # moveLists are per device, so self.moveLists might be{ '/dev/sda3': [MoveChunk, MoveChunk, ...], '/dev/sdb3' : ... }
        self.moveLists = {}
        self.resizeList = []

    @classmethod
    def cmdWrap(cls, params):
        rv, out, err = util.runCmd2(params, True, True)
        if rv != 0:
            if isinstance(err, (types.ListType, types.TupleType)):
                raise Exception("\n".join(err)+"\nError="+str(rv))
            else:
                raise Exception(str(err)+"\nError="+str(rv))
        return out

    def readInfo(self, info):
        retVal = []
        allOptions = info['string_options'] + info['integer_options']
        cmd = info['command'] + info['arguments'] + ['--options', ','.join(allOptions)]
        out = self.cmdWrap(cmd)

        for line in out.strip().split('\n'):
            # skip blank lines
            if line == '':
                continue
            try:
                # Create a dict of the form 'option_name':value
                data = dict(zip(allOptions, line.lstrip().split(self.SEP)))
                if len(data) != len(allOptions):
                    raise Exception("Wrong number of options in reply")
                for name in info['integer_options']:
                    # Convert integer options to integer type
                    data[name] = int(data[name])
                retVal.append(data)
            except Exception as e:
                logger.log("Discarding corrupt LVM output line '"+str(line)+"'")
                logger.log("  Command was '"+str(cmd)+"'")
                logger.log("  Error was '"+str(e)+"'")

        return retVal

    def readAllInfo(self):
        self.vgs = self.readInfo(self.VGS_INFO)
        self.lvs = self.readInfo(self.LVS_INFO)
        self.lvSegs = self.readInfo(self.LVS_SEG_INFO)
        self.pvs = self.readInfo(self.PVS_INFO)
        # For DM nodes "pvs" incorrectly returns /dev/dm-n, which does not exist.
        # Replace occurrences of /dev/dm-n with the correct node under /dev/mapper/
        for pv in self.pvs:
            name = pv['pv_name']
            if name.startswith('/dev/dm-'):
                n = int(name[8:])
                pv['pv_name'] = getDeviceMapperNode(n)

    @classmethod
    def decodeSegmentRange(cls, segRange):
        # Handle only a single range, e.g. '/dev/sdb3:11001-16158'
        matches = re.match(r'([^:]+):([0-9]+)-([0-9]+)$', segRange)
        if not matches:
            raise Exception("Could not decode segment range from '"+segRange+"'")
        # End value is inclusive, so 0-0 is one segment long
        return {
            'device' : matches.group(1),
            'start' : int(matches.group(2)),
            'size' : int(matches.group(3)) - int(matches.group(2)) + 1 # +1 because end is inclusive
        }

    @classmethod
    def encodeSegmentRange(cls, device, start, size):
        endInclusive = start+size-1
        if start < 0 or endInclusive < start:
            raise Exception("Invalid segment to encode: "+str(device)+', start='+str(start)+', size='+str(size))
        retVal = device+':'+str(start)+'-'+str(endInclusive)
        return retVal

    def segmentList(self, device):
        # PV segments don't record whether the segment is free space or not, so iterate through
        # the LV segments for the device instead
        segments = []
        for lvSeg in self.lvSegs:
            segRange = self.decodeSegmentRange(lvSeg['seg_pe_ranges'])
            if segRange['device'] == device:
                segments.append(Segment(segRange['start'], segRange['size']))
        segments.sort(lambda x, y : cmp(x.start, y.start))
        return segments

    def freeSegmentList(self, device):
        pv = self.deviceToPV(device)
        usedSegs = self.segmentList(device)
        # Add a fake zero-sized end segment to the list, so the unallocated space at the end
        # of the volume is a gap between two segments and not a special case
        fakeEndSeg = Segment(pv['pv_pe_count'], 0)
        usedSegs.append(fakeEndSeg)
        freeSegs = []
        # Iterate over pairs of consecutive segments
        for seg, nextSeg in zip(usedSegs[:-1], usedSegs[1:]):
            # ... work out the gap between them ...
            gapSize = nextSeg.start - seg.end()
            if gapSize > 0:
                # ... and add that to the free segment list
                freeSegs.append(Segment(seg.end(), gapSize))

        return freeSegs

    def segmentsToMove(self, device, threshold):
        """Given a device, i.e. a partition containing an LVM volume, and a threshold in extents,
        returns the segments that would need to be moved so that all non-free segments are
        below that address.  Can add just part of a segment if the original straddles the threshold"""
        segsToMove = []
        for seg in self.segmentList(device):
            if seg.end() > threshold:
                start = max(seg.start, threshold)
                segsToMove.append(Segment(start, seg.end() - start))
        return segsToMove

    def makeSpaceAfterThreshold(self, device, thresholdExtent):
        """Queues up a set of MoveChunks that will free up space at the end of a PV so that
        a pvresize cammand can succeed, and these will lead to pvmove commands at
        commit time.  Doesn't queue up the pvresize command itself - resizeDevice will do that..
        Also safe to call if no pvmoves are necessary"""
        pv = self.deviceToPV(device)
        # Extents >= thresholdExtent must be freed.
        segsToMove = self.segmentsToMove(device, thresholdExtent)

        # Calculate the free pool if we haven't already.  If we have done it already, we've been
        # here before for this device, so use the existing FreePool object as it knows how much
        # free space is already used by reallocation
        if 'free-pool' not in pv:
            pv['free-pool'] =  FreePool(self.freeSegmentList(device))

        # Take a copy.  We'll only commit our modified copy back to pv['free-pool']  if our transaction succeeds
        freePool = deepcopy(pv['free-pool'])
        moveList = []

        for srcSeg in segsToMove:
            srcOffset = 0
            destSegs = freePool.takeSegments(srcSeg.size)
            # destSegs are a tailor-made set of segments to consume srcSeg exactly, and the loop
            # beow relies on that
            for destSeg in destSegs:
                # Divide up the source segments into the destination segments
                srcStart = srcSeg.start + srcOffset
                destStart = destSeg.start
                moveList.append(MoveChunk(srcStart, destStart, destSeg.size))
                srcOffset += destSeg.size
            assert srcOffset == srcSeg.size # Logic error if not

        # Add our moves to the current MoveChunk list for this device, creating the
        # dict element if necessary
        self.moveLists[device] = self.moveLists.get(device, []) + moveList
        pv['free-pool'] = freePool

    def deviceToPVOrNone(self, device):
        """ Returns the PV record for a given device (partition), or None if there is no PV
        for that device."""
        for pv in self.pvs:
            if pv['pv_name'] == device:
                return pv
        return None

    def deviceToPV(self, device):
        pv = self.deviceToPVOrNone(device)
        if pv is None:
            raise Exception("PV for device '"+str(device)+"' not found")
        return pv

    def vGContainingLV(self, lvol):
        for lv in self.lvs:
            if lv['lv_name'] == lvol:
                return lv['vg_name']
        raise Exception("VG for LV '"+lvol+"' not found")

    def deviceSize(self, device):
        pv = self.deviceToPV(device)
        return pv['pv_size'] # in bytes

    def deviceFreeSpace(self, device):
        pv = self.deviceToPV(device)
        return pv['pv_free'] # in bytes

    def resizeDevice(self, device, byteSize):
        """ Resizes the PV on a device, moving extents around if necessary
        """
        pv = self.deviceToPV(device)
        if byteSize > pv['dev_size']:
            raise Exception("Size requested for "+str(device)+" ("+str(byteSize)+
                ") is greater than device size ("+str(pv['dev_size'])+")")

        extentBytes = pv['pv_size'] // pv['pv_pe_count'] # Typically 4MiB
        # Calculate the threshold in extents beyond which segments must be moved elsewhere.
        # Round down, so enough space is freed for pvresize to complete, and allow
        # PVRESIZE_EXTENT_MARGIN for extents consumed by LVM metadata
        metadataExtents = (pv['pe_start'] + extentBytes - 1) // extentBytes # Round up

        thresholdExtent = byteSize // extentBytes - metadataExtents - self.PVRESIZE_EXTENT_MARGIN
        self.makeSpaceAfterThreshold(device, thresholdExtent)
        self.resizeList.append({'device' : device, 'bytesize' : byteSize})

    def testPartition(self, devicePrefix, vgPrefix):
        """Returns the first partition where the device name starts with devicePrefix and
        the volume group that it's in starts with vgPrefix"""
        retVal = None
        for pv in self.pvs:
            if pv['pv_name'].startswith(devicePrefix) and pv['vg_name'].startswith(vgPrefix):
                retVal = pv['pv_name']
                break
        return retVal

    def configPartition(self, devicePrefix):
        """Returns the PV name for a config partition on the specified WHOLE DEVICE, e.g. '/dev/sda',
        or None if none present"""
        return self.testPartition(devicePrefix, self.VG_CONFIG_PREFIX)

    def swapPartition(self, devicePrefix):
        return self.testPartition(devicePrefix, self.VG_SWAP_PREFIX)

    def srPartition(self, devicePrefix):
        retVal = self.testPartition(devicePrefix, self.VG_SR_PREFIX)
        if retVal is None:
            retVal = self.testPartition(devicePrefix, self.VG_OTHER_SR_PREFIX)
        return retVal

    def isPartitionConfig(self, device):
        """Returns True if there is a config partition on the specified PARTITION, e.g. '/dev/sda2',
        or False if none present"""
        pv = self.deviceToPVOrNone(device)
        return pv is not None and pv['vg_name'].startswith(self.VG_CONFIG_PREFIX)

    def isPartitionSwap(self, device):
        pv = self.deviceToPVOrNone(device)
        return pv is not None and pv['vg_name'].startswith(self.VG_SWAP_PREFIX)

    def isPartitionSR(self, device):
        pv = self.deviceToPVOrNone(device)
        return pv is not None and (pv['vg_name'].startswith(self.VG_SR_PREFIX) or \
                                   pv['vg_name'].startswith(self.VG_OTHER_SR_PREFIX))

    def deleteDevice(self, device):
        """Deletes PVs, VGs and LVs associated with a device (partition)"""
        pvsToDelete = []
        vgsToDelete = []
        lvsToDelete = []

        for pv in self.pvs:
            if pv['pv_name'] == device:
                pvsToDelete.append(pv['pv_name'])
                vgsToDelete.append(pv['vg_name'])

        for lv in self.lvs:
            if lv['vg_name'] in vgsToDelete:
                # lvremove requires a 'path': <VG name>/<LV name>
                lvsToDelete.append(lv['vg_name']+'/'+lv['lv_name'])

        self.pvsToDelete += pvsToDelete
        self.vgsToDelete += vgsToDelete
        self.lvsToDelete += lvsToDelete

    def activateVG(self, vg):
        self.cmdWrap(self.VGCHANGE + ['-ay', vg])

    def deactivateVG(self, vg):
        self.cmdWrap(self.VGCHANGE + ['-an', vg])

    def deactivateAll(self):
        """Makes sure that LVM has unmounted everything so that, e.g. sfdisk can succeed"""
        for vg in self.vgs:
            # Passing VG names to LVchange is intentional
            try:
                self.cmdWrap(self.LVCHANGE + ['-an', vg['vg_name']])
            except Exception as e:
                logger.logException(e)

    @classmethod
    def executeMoves(cls, progress_callback, device, moveList):
        # Call commit instead this method unless you have special requirements
        """Issues pvmove commands to move MoveChunks specified by the MoveList.  Doesn't
        handle overlapping source and destination segments in a single MoveChunk, but in
        a makeSpaceAtEnd scenario those aren't generated"""
        sizeStep = 16 # Moving 16 extents takes only slightly more time than moving 1
        totalExtents = sum(move.size for move in moveList)
        extentsSoFar = 0
        for move in moveList:
            offset = 0
            while offset < move.size:
                progress_callback((100 * extentsSoFar) / totalExtents)
                chunkSize = min(sizeStep, move.size - offset)
                srcRange = cls.encodeSegmentRange(device, move.src + offset, chunkSize)
                destRange = cls.encodeSegmentRange(device, move.dest + offset, chunkSize)
                cls.cmdWrap(cls.PVMOVE +
                    [
                    '--alloc',
                    'anywhere',
                    srcRange,
                    destRange
                ])
                offset += chunkSize
                extentsSoFar += chunkSize

    def commit(self, progress_callback=lambda _ : ()):
        """Commit the changes queued up by issuing LVM commands, delete our queues as they
        succeed, and then reread the new configuration from LVM"""
        progress_callback(0)
        # Abort pvmoves if any have been left partiially completed by e.g. a crash
        self.cmdWrap(self.PVMOVE + ['--abort'])
        self.deactivateAll()
        progress_callback(1)

        # Process delete lists
        for lv in self.lvsToDelete:
            self.cmdWrap(self.LVREMOVE + [lv])
        self.lvsToDelete = []
        progress_callback(2)
        for vg in self.vgsToDelete:
            self.cmdWrap(self.VGREMOVE + [vg])
        self.vgsToDelete = []
        progress_callback(3)
        for pv in self.pvsToDelete:
            self.cmdWrap(self.PVREMOVE + ['--force', '--yes', pv])
        self.pvsToDelete = []
        progress_callback(4)

        # Process move lists.  Most of the code here is for calculating smoothly
        # increasing progress values
        totalExtents = 0
        for moveList in self.moveLists.values():
            totalExtents += sum([ move.size for move in moveList ])
        extentsSoFar = 0

        for device, moveList in sorted(self.moveLists.items()):
            thisSize = sum([ move.size for move in moveList ])
            callback = lambda percent : (progress_callback( 5 + (98 - 5) * (extentsSoFar + thisSize * percent / 100) / totalExtents) )
            self.executeMoves(callback, device, moveList)
            extentsSoFar +=  thisSize
        self.moveLists = {}

        # Process resize list
        progress_callback(98)
        for resize in self.resizeList:
            self.cmdWrap(self.PVRESIZE + ['--setphysicalvolumesize', str(resize['bytesize']//1024)+'k', resize['device']])
        self.resizeList = []

        self.readAllInfo() # Reread the new LVM configuration
        progress_callback(99)
        self.deactivateAll() # Stop active LVs preventing changes to the partition structure
        progress_callback(100)

    def dump(self):
        pprint(self.__dict__)

def diskDevice(partitionDevice):
    matches = re.match(r'(.+)(p?|(-part))\d+$', partitionDevice)
    if matches:
        return matches.group(1)
    matches = re.match(r'(.+\D)\d+$', partitionDevice)
    if not matches:
        raise Exception("Could not determine disk device for device '"+partitionDevice+"'")
    return matches.group(1)

def determineMidfix(device):
    DISK_PREFIX = '/dev/'
    P_STYLE_DISKS = [ 'cciss', 'ida', 'rd', 'sg', 'i2o', 'amiraid', 'iseries', 'emd', 'carmel', 'mapper/', 'nvme', 'md', 'mmcblk' ]
    PART_STYLE_DISKS = [ 'disk/by-id' ]

    for key in P_STYLE_DISKS:
        if device.startswith(DISK_PREFIX + key):
            return '' if re.match(r'.+\D$', device) else 'p'
    for key in PART_STYLE_DISKS:
        if device.startswith(DISK_PREFIX + key):
            return '-part'
    return ''

def partitionDevice(device, deviceNum):
    return device + determineMidfix(device) + str(deviceNum)


def roundUp(n, mult):
    n += mult - 1
    return n - n % mult

class PartitionToolBase:
    """
    Base class for the DOS and GPT Partition Tool classes.
    Contains common code.
    """
    BLOCKDEV = '/sbin/blockdev'

    DEFAULT_SECTOR_SIZE = 512 # Used if sfdisk won't print its (hardcoded) value

    def __init__(self, device):
        self.sectorAlignment = 1
        self.device = device
        self.midfix = determineMidfix(device)
        self.readDiskDetails()
        self.partitions = self.partitionTable()
        self.origPartitions = deepcopy(self.partitions)

    def partitionNumber(self, partitionDevice):
        matches = re.match(self.device + self.midfix + r'(\d+)$', partitionDevice)
        if not matches:
            raise Exception("Could not determine partition number for device '"+partitionDevice+"'")
        return int(matches.group(1))

    # Private methods:
    @classmethod
    def cmdWrap(cls, params):
        rv, out, err = util.runCmd2(params, True, True)
        if rv != 0:
            raise Exception(err)
        return out

    def _partitionDevice(self, deviceNum):
        return self.device + self.midfix + str(deviceNum)

    def _partitionNumber(self, partitionDevice):
        # sfdisk is inconsistent in naming partitions of by-id devices
        matches = re.match(self.device + r'\D*(\d+)$', partitionDevice)
        if not matches:
            raise Exception("Could not determine partition number for device '"+partitionDevice+"'")
        return int(matches.group(1))

    def settleUdev(self):
        timeout = 30
        try:
            self.cmdWrap(util.udevsettleCmd() + ['--timeout=%d' % timeout ])
        except:
            logger.log('udevsettle with %d second timeout failed' % timeout)

    def waitForDeviceNodes(self):
        # Ensure new device nodes are available before we continue.
        # Wait a second to ensure that udev picks up the change event from
        # the kernel, then call settle to wait for all events complete.
        time.sleep(1)
        self.settleUdev()

    def writePartitionTable(self, dryrun=False, log=False):
        try:
            self.writeThisPartitionTable(self.partitions, dryrun, log)
        except Exception as e:
            try:
                # Revert to the original partition table
                self.writeThisPartitionTable(self.origPartitions, dryrun)
            except Exception as e2:
                raise Exception('The new partition table could not be written: '+str(e)+'\nReversion also failed: '+str(e2))
            raise Exception('The new partition table could not be written but was reverted successfully: '+str(e))
        else:
            self.waitForDeviceNodes()

    # Public methods from here onward:
    def getPartition(self, number, default=None):
        return deepcopy(self.partitions.get(number, default))

    def createPartition(self, id, sizeBytes=None, number=None, order=None, startBytes=None, active=False):
        if number is None:
            if len(self.partitions) == 0:
                newNumber = 1
            else:
                newNumber = 1+max(self.partitions.keys())
        else:
            newNumber = number
        if newNumber in self.partitions:
            raise Exception('Partition '+str(newNumber)+' already exists')

        partitions = [None] + [part for num, part in sorted(self.partitions.items(), key=lambda item: item[1]['start'])]

        if startBytes is None:
            if order:
                if order < 1:
                    raise Exception("Order cannot be less than 1")
                elif order == 1:
                    startSector = self.sectorFirstUsable
                else:
                    startSector =  partitions[order - 1]['start'] + partitions[order - 1]['size']
            else:
                startSector =  partitions[-1]['start'] + partitions[-1]['size']
            startSector = roundUp(startSector, self.sectorAlignment)
        else:
            if startBytes % self.sectorSize != 0:
                raise Exception("Partition start ("+str(startBytes)+") is not a multiple of the sector size "+str(self.sectorSize))
            startSector = startBytes // self.sectorSize

        if sizeBytes is None:
            if order:
                if order < 1:
                    raise Exception("Order cannot be less than 1")
                elif order > len(partitions):
                    raise Exception("Order too large")
                elif order == len(partitions):
                    sizeSectors = self.sectorLastUsable + 1 - startSector
                else:
                    sizeSectors =  partitions[order]['start'] - startSector
            else:
                sizeSectors = self.sectorLastUsable + 1 - startSector
        else:
            if sizeBytes % self.sectorSize != 0:
                raise Exception("Partition size ("+str(sizeBytes)+") is not a multiple of the sector size "+str(self.sectorSize))

            sizeSectors = sizeBytes // self.sectorSize

        if sizeSectors < 0:
            self.dump()
            raise Exception("Partition size in sectors ("+str(sizeSectors)+") is negative")

        self.partitions[newNumber] = {
            'start': startSector,
            'size': sizeSectors,
            'id': id,
            'active': active
        }

    def deletePartition(self, number):
        del self.partitions[number]

    def deletePartitionIfPresent(self, number):
        if number in self.partitions:
            self.deletePartition(number)

    def deletePartitions(self, numbers):
        for number in numbers:
            self.deletePartition(number)

    def renamePartition(self, srcNumber, destNumber, overwrite=False):
        if srcNumber not in self.partitions:
            raise Exception('Source partition '+str(srcNumber)+' does not exist')
        if srcNumber != destNumber:
            if not overwrite and destNumber in self.partitions:
                raise Exception('Destination partition '+str(destNumber)+' already exists')

            self.partitions[destNumber] = self.partitions[srcNumber]
            self.deletePartition(srcNumber)

    def partitionSize(self, number):
        if number not in self.partitions:
            raise Exception('Partition '+str(number)+' does not exist')
        return self.getPartition(number)['size'] * self.sectorSize

    def partitionStart(self, number):
        if number not in self.partitions:
            raise Exception('Partition '+str(number)+' does not exist')
        return self.getPartition(number)['start'] * self.sectorSize

    def partitionEnd(self, number):
        return self.partitionStart(number) + self.partitionSize(number)

    def partitionID(self, number):
        if number not in self.partitions:
            raise Exception('Partition '+str(number)+' does not exist')
        return self.getPartition(number)['id']

    def resizePartition(self, number, sizeBytes):
        if number not in self.partitions:
            raise Exception('Partition for resize '+str(number)+' does not exists')
        if sizeBytes % self.sectorSize != 0:
            raise Exception("Partition size ("+str(sizeBytes)+") is not a multiple of the sector size "+str(self.sectorSize))

        self.partitions[number]['size'] = sizeBytes // self.sectorSize

    def setActiveFlag(self, activeFlag, number):
        assert isinstance(activeFlag, types.BooleanType) # Assert that params are the right way around
        if not number in self.partitions:
            raise Exception('Partition '+str(number)+' does not exist')
        self.partitions[number]['active'] = activeFlag

    def inactivateDisk(self):
        for number, partition in self.partitions.items():
            if partition['active']:
                self.setActiveFlag(False, number)

    def items(self):
        # sorted() creates a new list, so you can delete partitions whilst iterating
        for number, partition in sorted(self.partitions.items()):
            yield number, partition

    def commit(self, dryrun=False, log=False):
        self.writePartitionTable(dryrun, log)
        if not dryrun:
            # Update the revert point so this tool can be used repeatedly
            self.origPartitions = deepcopy(self.partitions)

    def dump(self):
        output  = "Sector size         : "+str(self.sectorSize) + "\n"
        output += "Sector extent       : "+str(self.sectorExtent)+" sectors\n"
        output += "Sector last usable  : "+str(self.sectorLastUsable)+"\n"
        output += "Sector first usable : "+str(self.sectorFirstUsable)+"\n"
        output += "Partition size and start addresses in sectors:\n"
        for number, partition in sorted(self.origPartitions.items()):
            output += "Old partition "+str(number)+":"
            for k, v in sorted(partition.items()):
                output += ' '+k+'='+((k == 'id') and hex(v) or str(v))
            output += "\n"
        for number, partition in sorted(self.partitions.items()):
            output += "New partition "+str(number)+":"
            for k, v in sorted(partition.items()):
                output += ' '+k+'='+((k == 'id') and hex(v) or str(v))
            output += "\n"
        logger.log(output)


class DOSPartitionTool(PartitionToolBase):

    ID_LINUX_SWAP = 0x82
    ID_LINUX = 0x83
    ID_LINUX_LVM = 0x8e
    ID_DELL_UTILITY = 0xde
    ID_GPT_PROTECTIVE = 0xee
    ID_EFI_BOOT = 0xef

    # List of hidden Microsoft partition IDs
    __HIDDEN_MS_IDS = {
        0x11, 0x14, 0x16, 0x17, 0x1b, 0x1c, 0x1e, 0x27
    }

    SFDISK = '/sbin/sfdisk'
    partTableType = constants.PARTITION_DOS

    def __readDiskDetails(self):
        # Read basic geometry
        out = self.cmdWrap([self.SFDISK, '-Lg', self.device])
        matches = re.match(r'^[^:]*:\s*(\d+)\s+cylinders,\s*(\d+)\s+heads,\s*(\d+)\s+sectors', out)
        if not matches:
            raise Exception("Couldn't decode sfdisk output: "+out)
        cylinders = int(matches.group(1))
        heads = int(matches.group(2))
        sectors = int(matches.group(3))
        self.sectorExtent = cylinders * heads * sectors

        # DOS partition tables have 32bit sector addresses so we may need to truncate sectorExtent
        # Actually truncate a bit more because sfdisk has unfathomablely lower limit
        self.sectorExtent = min([self.sectorExtent, 0xffe00000]) # 2047G
        cylinders = int(self.sectorExtent//(heads * sectors))
        self.sectorExtent = cylinders * heads * sectors # Ignore partial cylinder at end

        self.sectorFirstUsable = sectors # Some SANs require bootable disks to start on sector boundary
        self.sectorLastUsable = self.sectorExtent - 1

        # Read sector size
        self.sectorSize = int(self.cmdWrap([self.BLOCKDEV, '--getss', self.device]))

    def __readDeviceMapperDiskDetails(self):
        # DM nodes don't have a geometry and this version of sfdisk will return nothing.
        # Later versions return the default geometry below.
        heads = 255
        sectors = 63
        self.sectorSize = int(self.cmdWrap([self.BLOCKDEV, '--getss', self.device]))
        out = self.cmdWrap([self.BLOCKDEV, '--getsize64', self.device])
        self.sectorExtent = int(out)//self.sectorSize
        # DOS partition tables have 32bit sector addresses so we may need to truncate sectorExtent
        # Actually truncate a bit more because sfdisk has unfathomablely lower limit
        self.sectorExtent = min([self.sectorExtent, 0xffe00000]) # 2047G
        cylinders = int(self.sectorExtent//(heads * sectors))
        self.sectorExtent = cylinders * heads * sectors # Ignore partial cylinder at end
        self.sectorFirstUsable = sectors # Some SANs require bootable disks to start on sector boundary
        self.sectorLastUsable = self.sectorExtent - 1

    def readDiskDetails(self):
        if isDeviceMapperNode(self.device):
            self.__readDeviceMapperDiskDetails()
        else:
            self.__readDiskDetails()

    def partitionTable(self):
        out = self.cmdWrap([self.SFDISK, '-Ld', self.device])
        state = 0
        partitions = {}
        for line in out.split("\n"):
            if line == '' or line[0] == '#':
                pass # Skip comments and blank lines
            elif state == 0:
                if line != 'unit: sectors':
                    raise Exception("Expecting 'unit: sectors' but got '"+line+"'")
                state += 1
            elif state == 1:
                matches = re.match(r'(.*?)\s*:\s*start=\s*(\d+),\s*size=\s*(\d+),\s*Id=\s*(\w+)\s*(,\s*bootable)?', line)
                if matches:
                    idt = int(matches.group(4), 16) # Base 16
                    active = (matches.group(5) is not None)
                else:
                    # extended BSD partition?
                    idt = 0
                    active = False
                    matches = re.match(r'(.*?)\s*:\s*start=\s*(\d+),\s*size=\s*(\d+)', line)
                    if not matches:
                        raise Exception("Could not decode partition line: '"+line+"'")

                size = int(matches.group(3))
                if size != 0: # Treat partitions of size 0 as not present
                    number = self._partitionNumber(matches.group(1))
                    hidden = idt in self.__HIDDEN_MS_IDS

                    partitions[number] = {
                        'start': int(matches.group(2)),
                        'size': size,
                        'id': idt,
                        'active': active,
                        'hidden': hidden
                        }
        return partitions

    def commitActivePartitiontoDisk(self, part_num):
        self.settleUdev()
        self.cmdWrap([self.SFDISK, '--no-reread', '-A%d' % part_num, self.device]) # BIOS bootable flag set for one and unset for others partition
        self.waitForDeviceNodes()

    def writeThisPartitionTable(self, table, dryrun=False, log=False):
        input = 'unit: sectors\n\n'

        # sfdisk doesn't allow us to skip partitions, so invent lines for empty slot
        for number in range(1, 1+max([1]+list(table.keys()))):
            partition = table.get(number, {
                'start': 0,
                'size': 0,
                'id': 0,
                'active': False
            })
            line = self._partitionDevice(number)+' :'
            line += ' start='+str(partition['start'])+','
            line += ' size='+str(partition['size'])+','
            line += ' Id=%x' % partition['id']
            if partition['active']:
                line += ', bootable'

            input += line+'\n'
        if log:
            logger.log('Input to sfdisk:\n'+input)

        if isDeviceMapperNode(self.device):
            # Destroy device mapper partitions before re-writing partition table on mpath device
            rv = destroyPartnodes(self.device)
            if rv:
                raise Exception('Failed to destroy partitions on ' + self.device)
            heads = 255
            sectors = 63
            cylinders = self.sectorExtent//(heads * sectors)
            cmd = [self.SFDISK, dryrun and '-Lnu' or '-Lu', '--no-reread', '-f', '-C%d' % cylinders, '-H%d' % heads, '-S%d' % sectors, self.device]
        else:
            cmd = [self.SFDISK, dryrun and '-LnuS' or '-LuS', '--no-reread', '-f', self.device]
        logger.log('sfdisk command: %s' % ' '.join(cmd))
        self.settleUdev()
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            universal_newlines=True,
            )
        output = process.communicate(input)
        if log:
            logger.log('Output from sfdisk:\n'+output[0])

        if isDeviceMapperNode(self.device):
            # Create partitions using device mapper
            rv = createPartnodes(self.device)
            if rv:
                raise Exception('Failed to create partitions on %s using kpartx ' % self.device)
            for number in table.keys():
                size = int(self.cmdWrap([self.BLOCKDEV, '--getsize64', '%sp%d' % (self.device, number)]))//self.sectorSize
                if size != table[number]['size']:
                    raise Exception('Failed to create partition %sp%d of size %d' % (self.device, number, table[number]['size']))

        else:
            if process.returncode != 0:
                raise Exception('Partition changes could not be applied: '+str(output[0]))

            # CA-35300: sfdisk doesn't return non-zero when the BLKRRPART ioctl fails
            if 'BLKRRPART: Device or resource busy' in output:
                raise Exception(constants.PARTITIONING_ERROR)

            # Verify the table
            # Ignore warnings about partitions apparently with ends beyond the end of the disk
            rc, err = util.runCmd2([self.SFDISK, '-LVquS', self.device], with_stderr=True)
            if rc == 1:
                lines = err.split('\n')
                if len([x for x in lines if x != '' and not x.endswith('extends past end of disk')]) != 0:
                    raise Exception(err)
            elif rc != 0:
                raise Exception(err)

    def utilityPartitions(self):
        # Return list of partition numbers for partitions we should preserve
        return [num for num in self.partitions.keys() if self.partitions[num]['id'] == self.ID_DELL_UTILITY]

    def convertToGPT(self):
        # Clear any GPT data left if there are no protective partition.
        # We ignore the error, in case there are data error is returned but data cleared.
        if all([part['id'] != self.ID_GPT_PROTECTIVE for part in self.partitions.values()]):
            util.runCmd2(['sgdisk', '--zap', self.device])

        GPTPartitionTool.gdiskCheck(['sgdisk', '--mbrtogpt', self.device])

        gpt_tool = GPTPartitionTool(self.device)

        # Copy hidden attribute on MS partitions
        hidden_args = []
        for num, part in self.partitions.items():
            if part['id'] in self.__HIDDEN_MS_IDS and gpt_tool.partitions[num]['id'] == gpt_tool.ID_LINUX:
                hidden_args.append('--attributes=%d:set:62' % num)
                gpt_tool.partitions[num]['hidden'] = True
        if hidden_args:
            GPTPartitionTool.gdiskCheck([gpt_tool.SGDISK] + hidden_args + [self.device])

        return gpt_tool


class GPTPartitionTool(PartitionToolBase):

    # These are partition type GUIDs
    ID_LINUX_SWAP   = "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F"
    # This GUID is "Microsoft basic data partition" but for historic
    # reasons we use for Linux partitions.
    ID_LINUX        = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
    ID_LINUX2       = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
    ID_LINUX_LVM    = "E6D6D379-F507-44C2-A23C-238F2A3DF928"
    ID_EFI_BOOT     = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
    ID_BIOS_BOOT    = "21686148-6449-6E6F-744E-656564454649"

    # Lookup used for creating partitions
    GUID_to_type_code = {
        ID_LINUX_SWAP:   '8200',
        ID_LINUX:        '0700',
        ID_LINUX2:       '0700',
        ID_LINUX_LVM:    '8e00',
        ID_EFI_BOOT:     'ef00',
        ID_BIOS_BOOT:    'ef02',
        }

    SGDISK = 'sgdisk'
    GDISK = 'gdisk'
    partTableType = constants.PARTITION_GPT

    def readDiskDetails(self):
        self.sectorSize        = int(self.cmdWrap([self.BLOCKDEV, '--getss', self.device]))
        self.sectorExtent      = int(self.cmdWrap([self.BLOCKDEV, '--getsize64', self.device])) // self.sectorSize
        # size depends on GPT entries (should be 128), their size (128 bytes) and sector size
        # first sector is MBR, second GPT header
        self.sectorFirstUsable = 2 - (-128*128 // self.sectorSize)
        self.sectorLastUsable  = self.sectorExtent - self.sectorFirstUsable
        self.sectorAlignment   = 2 ** 20 // self.sectorSize

    def partitionTable(self):
        cmd = [self.SGDISK, '--print', self.device]
        rv, out, _ = util.runCmd2(cmd, True, True)
        if rv != 0:
            logger.log('Invalid or corrupt partition table found on disk %s. Trying MBR partitions...' % self.device)
            partitions = self.__recoverDOSTable()
            self.waitForDeviceNodes()
            return partitions

        partitions = self.__parsePartitions(out)

        def getPartitionInfo(number):
            attrs = self.cmdWrap([self.SGDISK, '--attributes=%d:show' % number, self.device])
            infos = self.cmdWrap([self.SGDISK, '--info=%d' % number, self.device])
            return (attrs, infos)
        partitions = self.__getPartitionsInfo(partitions, getPartitionInfo)

        # sgdisk opens the device with O_WRONLY even when not changing anything
        # so settle udev to ensure device nodes are available for subsequent
        # commands.
        self.waitForDeviceNodes()
        return partitions

    def __parsePartitions(self, gdisk_output):
        matchWarning   = re.compile('Found invalid GPT and valid MBR; converting MBR to GPT format.')
        matchHeader    = re.compile('Number\s+Start \(sector\)\s+End \(sector\)\s+Size\s+Code\s+Name')
        matchPartition = re.compile('^\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+\s+\w+)\s+([0-9A-F]{4})(\s+(.*))?$') # num start end sz typecode name
        partitions = {}
        lines = gdisk_output.split('\n')
        gotHeader = False
        for line in lines:
            if not line.strip():
                continue
            if not gotHeader:
                if matchWarning.match(line):
                    logger.log("Warning: GPTPartitionTool found DOS partition table on device %s" % self.device)
                elif matchHeader.match(line):
                    gotHeader = True
            else:
                matches = matchPartition.match(line)
                if not matches:
                    raise Exception("Could not parse sgdisk output line: %s" % line)
                number  = int(matches.group(1))
                start   = int(matches.group(2))
                _end    = int(matches.group(3))
                size    = _end + 1 - start
                partlabel = matches.group(7) if matches.group(7) else ''
                partitions[number] = {
                    'start': int(matches.group(2)),
                    'size': size,
                    'partlabel': partlabel,
                    }
        return partitions

    def __getPartitionsInfo(self, partitions, getPartitionInfo):
        # For each partition determine the active state.
        # By active we mean "BIOS bootable"
        matchActive    = re.compile('.*\(legacy BIOS bootable\)')
        matchHidden    = re.compile('.*\(hidden\)')
        matchId        = re.compile('^Partition GUID code: ([0-9A-F\-]+) ')
        matchPartUUID  = re.compile('^Partition unique GUID: ([0-9A-F\-]+)$')
        for number in partitions:
            (attrs, infos) = getPartitionInfo(number)
            partitions[number]['active'] = matchActive.match(attrs) and True or False
            partitions[number]['hidden'] = matchHidden.match(attrs) and True or False
            for line in infos.split('\n'):
                m = matchId.match(line)
                if m:
                    partitions[number]['id'] = m.group(1)
                m = matchPartUUID.match(line)
                if m:
                    partitions[number]['partuuid'] = m.group(1)
            assert 'id' in partitions[number]
        return partitions

    def __recoverDOSTable(self):
        if probePartitioningScheme(self.device) != constants.PARTITION_DOS:
            return {}

        proc = ppexpect.Process([self.GDISK, self.device])
        proc.expect(re.compile(r'(?s).*Found valid MBR and corrupt GPT.*1 - MBR.* answer:'))
        proc.write('1\np\n')
        m = proc.expect(re.compile(r'(?ms).*^(Number.*)^Command.*for help'))
        partitions = self.__parsePartitions(m.group(1))

        proc.write('x\n') # expert mode
        proc.expect(re.compile('(?ms).*^Expert command'))

        partitionRE = re.compile(r'(?ms).* (Partition GUID.*)^Expert command.*^Attribute value is .*Set fields are(.*)^Expert command')
        def getPartitionInfo(number):
            # information and attribute commands
            proc.write('i\n%d\na\n%d\n\n' % (number, number))
            m = proc.expect(partitionRE)
            return (m.group(2), m.group(1))
        partitions = self.__getPartitionsInfo(partitions, getPartitionInfo)

        proc.close()
        return partitions

    def commitActivePartitiontoDisk(self, partnum):
        args = []
        for num, part in self.items():
            if num == partnum:
                args += ['--attributes=%d:set:2' % num] # BIOS bootable flag set
            else:
                args += ['--attributes=%d:clear:2' % num] # BIOS bootable flag clear

        if args:
            self.cmdWrap([self.SGDISK] + args + [self.device])

        self.waitForDeviceNodes()

    def writeThisPartitionTable(self, table, dryrun=False, log=False):
        for part in table.values():
            if part['id'] not in self.GUID_to_type_code:
                raise Exception("GPT partitions with part type GUID %s unsupported" % part['id'])

        if isDeviceMapperNode(self.device):
            # Destroy device mapper partitions before re-writing partition table on mpath device
            rv = destroyPartnodes(self.device)
            if rv:
                raise Exception('Failed to destroy GPT partitions on ' + self.device)

        # Bring us to a known state.
        try:
            # --clear is called to clear the backup GPT first, since --zap does not clear this and
            # will restore the backup GPT if the main GPT is damaged (which is not what we want).
            self.cmdWrap([self.SGDISK, '--clear', self.device])
        except:
            # Ignore error code which results from inconsistent initial state
            pass

        try:
            self.cmdWrap([self.SGDISK, '--zap', self.device])
        except:
            # Ignore error code which results from inconsistent initial state
            pass
        args = [self.SGDISK, '--mbrtogpt', '--clear', self.device]
        if isDeviceMapperNode(self.device):
            self.cmdWrap(args)
        else:
            self.gdiskCheck(args)

        has_esp = False
        for part in table.values():
            if part['id'] == self.ID_EFI_BOOT:
                has_esp = True
                break

        if not has_esp:
            # CA-54144: Some _stupid_ BIOSes refuse to boot disks that don't have a DOS partition table
            # with an active partition.  This is incorrect because it makes the assumption that the
            # bootloader uses a DOS partition table.  Instead the BIOSes _should_ just check for 0x55,0xaa
            # at location 0x1fe.
            # However, let's keep them happy by making the single partition in the protective MBR "active".
            self.settleUdev()
            self.cmdWrap(['sfdisk', '--no-reread', '-A1', self.device])

        # Ensure that we write out in on-disk order to prevent conflicts when
        # partition sizes get rounded.
        args = [self.SGDISK, '--set-alignment=1']
        items = sorted(table.items(), key=lambda item: item[1]['start'])
        for num,part in items:
            start  = part['start']
            end    = part['size'] + start - 1
            idt    = part['id']
            active = part['active']
            hidden = part.get('hidden', False)
            args += ['--new=%d:%d:%d' % (num,start,end)]
            args += ['--typecode=%d:%s' % (num,self.GUID_to_type_code[idt])]
            if active:
                args += ['--attributes=%d:set:2' % num] # BIOS bootable flag
            if hidden and idt == self.ID_LINUX:
                args += ['--attributes=%d:set:62' % num] # hidden flag
            if 'partlabel' in part and part['partlabel']:
                args += ['--change-name=%d:%s' % (num,part['partlabel'])]
            if 'partuuid' in part:
                args += ['--partition-guid=%d:%s' % (num,part['partuuid'])]
        args += [self.device]
        if isDeviceMapperNode(self.device):
            self.cmdWrap(args)
        else:
            self.gdiskCheck(args)

        if isDeviceMapperNode(self.device):
            # Create partitions using device mapper
            rv = createPartnodes(self.device)
            if rv:
                raise Exception('Failed to create partitions on %s using kpartx ' % self.device)

    def utilityPartitions(self):
        # Return list of partition numbers for partitions we should preserve
        return [num for num in self.partitions.keys() if
                self.partitions[num]['id'] == self.ID_EFI_BOOT and self.partitions[num]['partlabel'] == constants.UTILITY_PARTLABEL]

    @classmethod
    def gdiskCheck(cls, params):
        out = cls.cmdWrap(params)
        # This check is here to check partitions have been updated in the kernel.
        # Note that not all operations requires the kernel to be updated.
        # In this respect sgdisk differs from sfdisk (former gives warning latter an error)
        # so check it manually to detect this issue which potentially leads to corruptions.
        # Checking 2 strings allows to handle more possible versions of gdisk.
        if ('The kernel is still using the old partition table' in out
            or 'The new table will be used at the next reboot' in out):
            raise Exception(constants.PARTITIONING_ERROR)
        return out


def probePartitioningScheme(device):
    """Determine whether the MBR is a DOS MBR, or a GPT PMBR"""
    partitionType = constants.PARTITION_GPT   # default
    rv, out = util.runCmd2(['blkid', '-s', 'PTTYPE', '-o', 'value', device], with_stdout=True)
    out = out.strip()

    if out == 'dos':
        partitionType = constants.PARTITION_DOS

    logger.debug("probePartitioningScheme(%r) => %r", device, partitionType)
    return partitionType

def PartitionTool(device, partitionType=None):
    """
    By default PartitionTool() will return the tool appropriate to the partitioning
    system currently in use on device
    """
    if partitionType is None:
        partitionType = probePartitioningScheme(device)
    if partitionType == constants.PARTITION_DOS:
        return DOSPartitionTool(device)
    elif partitionType == constants.PARTITION_GPT:
        return GPTPartitionTool(device)

def destroyPartnodes(dev):
    # Destroy partition nodes for a device-mapper device
    dmnodes = [ '/dev/mapper/%s' % f for f in os.listdir('/dev/mapper') ]
    partitions = [dmnode for dmnode in dmnodes if re.match(dev + r'p?\d+$', dmnode)]
    for partition in partitions:
        # the obvious way to do this is to use "kpartx -d" but that's broken!
        rv = util.runCmd2(['dmsetup', 'remove', partition])
        if rv: return rv
    return 0

def destroyMpathPartnodes():
    mpnodes = getMpathNodes()
    for mpnode in mpnodes:
        rv = destroyPartnodes(mpnode)
        if rv: return rv
    return 0

def createPartnodes(dev):
    # Create partition nodes for a device-mapper device
    return util.runCmd2(['kpartx', '-a', dev])

def createMpathPartnodes():
    return util.runCmd2(['dmsetup', 'ls', '--target', 'multipath', '--exec', "kpartx -a"])

def getMpathNodes():
    nodes = []
    rv, out = util.runCmd2(['dmsetup', 'ls', '--target', 'multipath', '--exec', 'ls'], with_stdout=True)
    logger.log("multipath devs: %s" % out)
    lines = out.strip().split('\n')
    for line in lines:
        if line.startswith('/dev/'):
            nodes.append(line)
    return nodes

def getMajMin(dev):
    buf = os.stat(dev)
    major = os.major(buf.st_rdev)
    minor = os.minor(buf.st_rdev)
    return (major, minor)

cached_DM_maj = None
def getDeviceMapperMaj():
    global cached_DM_maj
    if not cached_DM_maj:
        try:
            line = [x for x in open('/proc/devices').readlines() if x.endswith('device-mapper\n')]
            cached_DM_maj = int(line[0].split()[0])
        except:
            pass
    return cached_DM_maj

def isDeviceMapperNode(dev):
    try:
        return getMajMin(dev)[0] == getDeviceMapperMaj()
    except OSError:
        return False

def getSysfsDir(dev):
    major, minor = getMajMin(dev)
    parts = open("/proc/partitions")
    partlines = [re.sub(" +", " ", x).strip() for x in parts.readlines()]
    parts.close()
    # parse it:
    disks = []
    for l in partlines:
        try:
           (_major, _minor, size, name) = l.split(" ")
           if (major, minor) == (int(_major), int(_minor)):
               name = name.replace('/','!')
               return '/sys/block/%s' % name
        except:
            pass
    raise RuntimeError("Couldn't find sysfs dir for device %s" % dev)

def hasDeviceMapperHolder(dev):
    sysfs = getSysfsDir(dev)
    if os.path.exists('%s/holders' % sysfs):
        for holder in os.listdir('%s/holders' % sysfs):
            if holder.startswith('dm-'):
                return True
    return False


def getDeviceMapperNode(n):
    "Return the /dev/mapper/node corresponding to /sys/block/dm-n"
    (major,minor) = map(int,open('/sys/block/dm-%s/dev' % str(n)).read().strip().split(':'))
    for i in os.listdir('/dev/mapper'):
        dmdev = '/dev/mapper/%s' % i
        if getMajMin(dmdev) == (major,minor):
            return dmdev
    return None


def getDeviceSlaves(disk):
    """ Return the list of slaves for an device or an empty list """
    slaves = []
    major, minor = getMajMin(disk)
    rv, out = util.runCmd2(['sh','-c','ls -d1 /sys/block/*/holders/*/dev'],with_stdout=True)
    lines = out.strip().split('\n')
    lines = [x for x in lines if x != '']
    for f in lines:
        _, _, _, dev, _, _, _ = f.split('/')
        __major, __minor = map(int, open(f).read().split(':'))
        if (__major, __minor) == (major, minor):
            dev = '/dev/' + dev.replace("!", "/")
            slaves.append(dev)
    return slaves

def getMpathMaster(dev):
    "Returns master device or None"
    try:
        d = getSysfsDir(dev)

        if dev.startswith('/dev/dm-'):
            holder = dev[5:]
        else:
            holders = os.listdir('%s/holders' % d)
            if len(holders) != 1 or (not holders[0].startswith('dm-')):
                logger.log('getMpathMaster: contents of %s/holders/ is %s' % (d,str(holders)))
                return None
            else:
                holder = holders[0]

        (major,minor) = map(int,open('/sys/block/%s/dev' % holder).read().strip().split(':'))
        for i in os.listdir('/dev/mapper'):
            dmdev = '/dev/mapper/%s' % i
            if getMajMin(dmdev) == (major,minor):
                logger.log('getMpathMaster: %s has master %s' % (dev,dmdev))
                return dmdev
        logger.log('getMpathMaster: could not find master %d:%d of %s in /dev/mapper/' % (major,minor,dev))

    except OSError:
        return None

def getMpathMasterOrDisk(disk):
    """Returns the multipath master or the original device if it is not part of
    a multipath setup."""

    master = getMpathMaster(disk)
    return master if master else disk

def getMdNodes():
    nodes = []
    try:
        fh = open('/proc/mdstat')
        for line in fh:
            line = line.rstrip()
            if not ' : ' in line:
                continue
            l = line.split(None, 3)
            if l[2] == 'active':
                nodes.append('/dev/'+l[0])
        fh.close()
    except IOError:
        pass
    return nodes


class DeviceMounter:
    class Mount:
        def __init__(self, dev, mountpoint, options=None, fstype=None):
            self.dev = dev
            self.mountpoint = mountpoint
            self.options = options
            self.fstype = fstype

        def __repr__(self):
            return "Mount(%s, %s, %s, %s)" % (self.dev, self.mountpoint, self.fstype, self.options)

        def mount(self):
            try:
                os.makedirs(self.mountpoint, mode=0o755)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise

            util.mount(self.dev, self.mountpoint, self.options, self.fstype)

        def umount(self):
            util.umount(self.mountpoint)

    mounts = []

    @classmethod
    def addMountPoints(cls, arg_mounts):
        mounts = []
        for arg in arg_mounts:
            m = arg.split(':')
            if len(m) < 2:
                continue
            dev = m[0]
            fstype = None
            options = None
            if len(m) > 4:
                raise Exception("Invalid syntax for --mount argument: %s" % arg)
            if len(m) == 4:
                fstype = m[1]
                options = m[2]
                mnt = m[3]
            elif len(m) >= 3:
                fstype = m[1]
                mnt = m[2]
            else:
                mnt = m[1]
            if dev.startswith("LABEL=") or dev.startswith("UUID="):
                rc, out = util.runCmd2(['blkid', '-t', dev, '-o', 'device'], with_stdout=True)
                if rc != 0:
                    # for compatibility ignore the device
                    continue
                dev = out.rstrip()
            elif dev.startswith("VG_"):
                rc = util.runCmd2(LVMTool.LVCHANGE + ['-a', 'y', dev])
                if rc != 0:
                    # for compatibility ignore the device
                    continue
                dev = dev.replace("-", "--")
                vg, lv, _ = (dev + "//").split("/", 2)
                dev = "/dev/mapper/%s-%s" % (vg, lv)
            mounts.append(cls.Mount(dev, mnt, options, fstype))
        cls.mounts = mounts

    def __init__(self):
        self.mounts = self.__class__.mounts

    def mount(self):
        for m in self.mounts:
            try:
                m.mount()
            except Exception as e:
                logger.logException(e)

    def __umount(self):
        for m in self.mounts:
            try:
                m.umount()
            except Exception as e:
                logger.logException(e)

    def __enter__(self):
        self.mount()

    def __exit__(self, type, value, traceback):
        self.__umount()
