# Copyright (c) 2005-2006 XenSource, Inc. All use and distribution of this
# copyrighted material is governed by and subject to terms and conditions
# as licensed by XenSource, Inc. All other rights reserved.
# Xen, XenSource and XenEnterprise are either registered trademarks or
# trademarks of XenSource Inc. in the United States and/or other countries.

###
# XEN CLEAN INSTALLER
# Packaging functions
#
# written by Andrew Peace

import os
import os.path
import glob
import errno
import md5
import hashlib
import tempfile
import urlparse
import urllib
import urllib2
import ftplib
import subprocess
import re
import gzip
import shutil
from xml.dom.minidom import parse

import diskutil
import hardware
import version
import util
from util import dev_null, elide
from xcp.version import *
from xcp import logger
import cpiofile
from constants import *
import xml.dom.minidom
import ConfigParser

# get text from a node:
def getText(nodelist):
    rc = ""
    for node in nodelist:
        if node.nodeType == node.TEXT_NODE:
            rc = rc + node.data
    return rc.encode().strip()

class NoRepository(Exception):
    pass

class RepoFormatError(Exception):
    pass

class UnknownPackageType(Exception):
    pass

class UnrecoverableRepoError(Exception):
    pass

class RepoSecurityConfigError(Exception):
    pass

class Repository(object):
    """ Represents a repository containing packages and associated meta data. """
    def __init__(self, accessor):
        self._accessor = accessor
        self._product_version = None

    def accessor(self):
        return self._accessor

    def check(self, progress=lambda x: ()):
        """ Return a list of problematic packages. """
        def pkg_progress(start, end):
            def progress_fn(x):
                progress(start + ((x * (end - start)) / 100))
            return progress_fn

        self._accessor.start()

        try:
            problems = []
            total_size = sum((p.size for p in self._packages))
            total_progress = 0
            for p in self._packages:
                start = (total_progress * 100) / total_size
                end = ((total_progress + p.size) * 100) / total_size
                if not p.check(False, pkg_progress(start, end)):
                    problems.append(p)
                total_progress += p.size
        finally:
            self._accessor.finish()
        return problems

    def __iter__(self):
        return self._packages.__iter__()

def _generateYumConf(cachedir):
    return """[main]
cachedir=/%s
keepcache=0
debuglevel=2
logfile=/var/log/yum.log
exactarch=1
obsoletes=1
plugins=0
installonlypkgs=
distroverpkg=xenserver-release
reposdir=/tmp/repos
history_record=false
""" % cachedir

_yumRepositoryId = 1
class YumRepository(Repository):
    """ Represents a Yum repository containing packages and associated meta data. """
    REPOMD_FILENAME = "repodata/repomd.xml"
    _cachedir = "var/cache/yum/installer"
    _targets = None

    def __init__(self, accessor):
        super(YumRepository, self).__init__(accessor)
        global _yumRepositoryId
        self._identifier = "repo%d" % _yumRepositoryId
        _yumRepositoryId += 1
        self._gpg_key = ""

    @property
    def _yum_conf(self):
        return _generateYumConf(self._cachedir)

    def _repo_config(self):
        return None

    def _parse_repodata(self, accessor):
        # Read packages from xml
        repomdfp = accessor.openAddress(self.REPOMD_FILENAME)
        repomd_xml = parse(repomdfp)
        xml_datas = repomd_xml.getElementsByTagName("data")
        for data_node in xml_datas:
            data = data_node.getAttribute("type")
            if data == "primary":
                primary_location = data_node.getElementsByTagName("location")
                primary_location = primary_location[0].getAttribute("href")
        repomdfp.close()

        primaryfp = accessor.openAddress(primary_location)
        # Open compressed xml using cpiofile._Stream which is an adapter between CpioFile and a stream-like object.
        # Useful when specifying the URL for HTTP or FTP repository - A simple GzipFile object will not work in this situation.
        primary_xml = cpiofile._Stream("", "r", "gz", primaryfp, 20*512)
        primary_dom = parse(primary_xml)
        package_names = primary_dom.getElementsByTagName("location")
        package_sizes = primary_dom.getElementsByTagName("size")
        package_checksums = primary_dom.getElementsByTagName("checksum")
        primary_xml.close()
        primaryfp.close()

        # Filter using only sha256 checksum
        sha256_checksums = filter(lambda x: x.getAttribute("type") == "sha256", package_checksums)

        # After the filter, the list of checksums will have the same size
        # of the list of names
        self._packages = []
        for name_node, size_node, checksum_node in zip(package_names, package_sizes, sha256_checksums):
            name = name_node.getAttribute("href")
            size = size_node.getAttribute("package")
            checksum = checksum_node.childNodes[0]
            pkg = RPMPackage(self, name, size, checksum.data)
            pkg.type = 'rpm'
            self._packages.append(pkg)

    def _setGpgKey(self, gpg_key):
        self._gpg_key = gpg_key

    def __repr__(self):
        return "%s@yum" % self._identifier

    @classmethod
    def isRepo(cls, accessor):
        """ Return whether there is a repository accessible using accessor."""
        return False not in [ accessor.access(f) for f in [cls.REPOMD_FILENAME] ]

    def identifier(self):
        return self._identifier

    def name(self):
        return self._identifier

    def __eq__(self, other):
        return self.identifier() == other.identifier()

    def __hash__(self):
        return hash(self.identifier())

    def record_install(self, answers, installed_repos):
        installed_repos[str(self)] = self
        return installed_repos

    def _installPackages(self, progress_callback, mounts):
        assert self._targets is not None
        url = self._accessor.url()
        logger.log("URL: " + str(url))
        gpgcheck = bool(self._gpg_key)
        with open('/root/yum.conf', 'w') as yum_conf:
            yum_conf.write(self._yum_conf)
            yum_conf.write("""
[install]
name=install
baseurl=%s
gpgcheck=%d
gpgkey=%s
repo_gpgcheck=%d
""" % (url.getPlainURL(), gpgcheck, self._gpg_key, gpgcheck))
            username = url.getUsername()
            if username is not None:
                yum_conf.write("username=%s\n" % (url.getUsername(),))
            password = url.getPassword()
            if password is not None:
                yum_conf.write("password=%s\n" % (url.getPassword(),))
            repo_config = self._repo_config()
            if repo_config is not None:
                yum_conf.write(repo_config)

        self.disableInitrdCreation(mounts['root'])
        installFromYum(self._targets, mounts, progress_callback, self._cachedir)
        self.enableInitrdCreation()

    def installPackages(self, progress_callback, mounts):
        self._accessor.start()
        try:
            self._installPackages(progress_callback, mounts)
        finally:
            self._accessor.finish()

    def disableInitrdCreation(self, root):
        pass

    def enableInitrdCreation(self):
        pass

    def getBranding(self, mounts, branding):
        return branding

class YumRepositoryWithInfo(YumRepository):
    """Represents a Yum repository which has an information file present."""
    INFO_FILENAME = None

    @classmethod
    def isRepo(cls, accessor):
        """ Return whether there is a repository accessible using accessor."""
        assert cls.INFO_FILENAME is not None
        return False not in [ accessor.access(f) for f in [cls.INFO_FILENAME, cls.REPOMD_FILENAME] ]

class MainYumRepository(YumRepositoryWithInfo):
    """Represents a Yum repository containing the main XenServer installation."""

    INFO_FILENAME = ".treeinfo"
    _targets = ['xcp-ng-deps']

    def __init__(self, accessor):
        super(MainYumRepository, self).__init__(accessor)
        self._identifier = MAIN_REPOSITORY_NAME
        self.keyfiles = []

        def get_name_version(config_parser, section, name_key, vesion_key):
            name, version = None, None
            if config_parser.has_section(section):
                name = config_parser.get(section, name_key)
                ver_str = config_parser.get(section, vesion_key)
                version = Version.from_string(ver_str)
            return name, version

        accessor.start()
        try:
            treeinfo = ConfigParser.SafeConfigParser()
            treeinfofp = accessor.openAddress(self.INFO_FILENAME)
            try:
                treeinfo.readfp(treeinfofp)
            except Exception as e:
                raise RepoFormatError("Failed to read %s: %s" % (self.INFO_FILENAME, str(e)))
            finally:
                treeinfofp.close()

            self._platform_data = {}
            self._product_data = {}
            platform_name, platform_version = get_name_version(
                treeinfo, 'system-v1', 'platform_name', 'platform_version')
            product_brand, product_version = get_name_version(
                treeinfo, 'system-v1', 'product_name', 'product_version')
            if platform_name is None:
                platform_name, platform_version = get_name_version(
                    treeinfo, 'platform', 'name', 'version')
            if product_brand is None:
                product_brand, product_version = get_name_version(
                    treeinfo, 'branding', 'name', 'version')
            if platform_name:
                self._platform_data = {
                    'name': platform_name,
                    'version': platform_version
                }
            if product_brand:
                self._product_data = {
                    'brand': product_brand,
                    'version': product_version
                }

            if treeinfo.has_section('build'):
                self._build_number = treeinfo.get('build', 'number')
            else:
                self._build_number = None
            if treeinfo.has_section('keys'):
                for _, keyfile in treeinfo.items('keys'):
                    self.keyfiles.append(keyfile)
        except Exception as e:
            accessor.finish()
            logger.logException(e)
            raise RepoFormatError("Failed to read %s: %s" % (self.INFO_FILENAME, str(e)))

        self._parse_repodata(accessor)
        accessor.finish()

    def _repo_config(self):
        if len(self.keyfiles) > 0:
            # Only deal with a single key for the repo
            keyfile = self.keyfiles[0]
            infh = None
            outfh = None
            try:
                infh = self._accessor.openAddress(keyfile)
                key_path = os.path.join('/root', os.path.basename(keyfile))
                outfh = open(key_path, "w")
                outfh.write(infh.read())
                return """
gpgcheck=1
repo_gpgcheck=1
gpgkey=file://%s
""" % (key_path)
            finally:
                if infh:
                    infh.close()
                if outfh:
                    outfh.close()
        return None

    def name(self):
        return self._product_data.get('brand', self._identifier)

    def disableInitrdCreation(self, root):
        # Speed up the install by disabling initrd creation.
        # It is created after the yum install phase.
        confdir = os.path.join(root, 'etc', 'dracut.conf.d')
        self._conffile = os.path.join(confdir, 'xs_disable.conf')

        # makedirs throws an exception if the directory exists.
        # It's the case if the repository is reconfigured by the user
        # in the installation process. Remove it is a good thing to retrieve a
        # proper state.
        shutil.rmtree(confdir, True)

        os.makedirs(confdir, 0775)
        with open(self._conffile, 'w') as f:
            print >> f, 'echo Skipping initrd creation during host installation'
            print >> f, 'exit 0'

    def enableInitrdCreation(self):
        os.unlink(self._conffile)

    def getBranding(self, mounts, branding):
        if self._platform_data:
            branding.update({'platform-name': self._platform_data['name'],
                             'platform-version': self._platform_data['version'].ver_as_string() })
        if self._product_data:
            branding.update({'product-brand': self._product_data['brand'],
                             'product-version': self._product_data['version'].ver_as_string() })

        if self._build_number:
            branding['product-build'] = self._build_number
        return branding


    def setGpgCheck(self, status = True):
        self._setGpgKey(('file://' + MAIN_REPOSITORY_GPG_KEY_FILE) if status else '')

class UpdateYumRepository(YumRepositoryWithInfo):
    """Represents a Yum repository containing packages and associated meta data for an update."""

    INFO_FILENAME = "update.xml"

    def __init__(self, accessor):
        super(UpdateYumRepository, self).__init__(accessor)

        accessor.start()
        try:
            updatefp = accessor.openAddress(self.INFO_FILENAME)
            try:
                dom = xml.dom.minidom.parseString(updatefp.read())
            except Exception as e:
                logger.logException(e)
                raise RepoFormatError("Failed to read %s: %s" % (self.INFO_FILENAME, str(e)))
            finally:
                updatefp.close()

            assert dom.documentElement.tagName == 'update'
            self._controlpkg = dom.documentElement.getAttribute('control')
            self._identifier = dom.documentElement.getAttribute('name-label')
            self._targets = [self._controlpkg, 'update-' + self._identifier]
        except Exception as e:
            accessor.finish()
            logger.logException(e)
            raise RepoFormatError("Failed to read %s: %s" % (self.INFO_FILENAME, str(e)))

        self._parse_repodata(accessor)
        accessor.finish()

    def name(self):
        return self._identifier

class DriverUpdateYumRepository(UpdateYumRepository):
    """Represents a Yum repository containing packages and associated meta data for a driver disk."""

    INFO_FILENAME = "update.xml"
    _cachedir = 'run/yuminstaller'
    _yum_conf = """[main]
cachedir=/%s
keepcache=0
debuglevel=2
logfile=/var/log/yum.log
exactarch=1
obsoletes=1
gpgcheck=0
plugins=0
group_command=compat
installonlypkgs=
distroverpkg=xenserver-release
reposdir=/tmp/repos
diskspacecheck=0
history_record=false
""" % _cachedir

    def __init__(self, accessor):
        super(DriverUpdateYumRepository, self).__init__(accessor)
        self._targets = ['@drivers']

    @classmethod
    def isRepo(cls, accessor):
        if UpdateYumRepository.isRepo(accessor):
            url = accessor.url()
            with open('/root/yum.conf', 'w') as yum_conf:
                yum_conf.write(cls._yum_conf)
                yum_conf.write("""
[driverrepo]
name=driverrepo
baseurl=%s
""" % url.getPlainURL())
                username = url.getUsername()
                if username is not None:
                    yum_conf.write("username=%s\n" % (url.getUsername(),))
                password = url.getPassword()
                if password is not None:
                    yum_conf.write("password=%s\n" % (url.getPassword(),))

            # Check that the drivers group exists in the repo.
            rv, out = util.runCmd2(['yum', '-c', '/root/yum.conf',
                                    'group', 'summary', 'drivers'], with_stdout=True)
            if rv == 0 and 'Groups: 1\n' in out.strip():
                return True

        return False

class RPMPackage(object):
    def __init__(self, repository, name, size, sha256sum):
        self.repository = repository
        self.name = name
        self.size = long(size)
        self.sha256sum = sha256sum

    def check(self, fast=False, progress=lambda x : ()):
        """ Check a package against it's known checksum, or if fast is
        specified, just check that the package exists. """
        if fast:
            return self.repository.accessor().access(self.name)
        else:
            try:
                logger.log("Validating package %s" % self.name)
                namefp = self.repository.accessor().openAddress(self.name)
                m = hashlib.sha256()
                data = ''
                total_read = 0
                while True:
                    data = namefp.read(10485760)
                    total_read += len(data)
                    if data == '':
                        break
                    else:
                        m.update(data)
                    progress(total_read / (self.size / 100))
                namefp.close()
                calculated = m.hexdigest()
                valid = (self.sha256sum == calculated)
                return valid
            except Exception as e:
                return False

class Accessor:
    def pathjoin(base, name):
        return os.path.join(base, name)
    pathjoin = staticmethod(pathjoin)

    def access(self, name):
        """ Return boolean determining where 'name' is an accessible object
        in the target. """
        try:
            f = self.openAddress(name)
            f.close()
        except:
            return False
        else:
            return True

    def canEject(self):
        return False

    def start(self):
        pass

    def finish(self):
        pass

    def findRepository(self):
        classes = [MainYumRepository, UpdateYumRepository, YumRepository]
        for cls in classes:
            if cls.isRepo(self):
                return cls(self)

    def findDriverRepository(self):
        if DriverUpdateYumRepository.isRepo(self):
            return DriverUpdateYumRepository(self)

class FilesystemAccessor(Accessor):
    def __init__(self, location):
        self.location = location

    def start(self):
        pass

    def finish(self):
        pass

    def openAddress(self, addr):
        return open(os.path.join(self.location, addr), 'r')

    def url(self):
        return util.URL("file://%s" % self.location)

class MountingAccessor(FilesystemAccessor):
    def __init__(self, mount_types, mount_source, mount_options=['ro']):
        (
            self.mount_types,
            self.mount_source,
            self.mount_options
        ) = (mount_types, mount_source, mount_options)
        self.start_count = 0
        self.location = None

    def start(self):
        if self.start_count == 0:
            self.location = tempfile.mkdtemp(prefix="media-", dir="/tmp")
            # try each filesystem in turn:
            success = False
            for fs in self.mount_types:
                try:
                    util.mount(self.mount_source, self.location,
                               options=self.mount_options,
                               fstype=fs)
                except util.MountFailureException as e:
                    continue
                else:
                    success = True
                    break
            if not success:
                os.rmdir(self.location)
                raise util.MountFailureException
        self.start_count += 1

    def finish(self):
        if self.start_count == 0:
            return
        self.start_count -= 1
        if self.start_count == 0:
            util.umount(self.location)
            os.rmdir(self.location)
            self.location = None

    def __del__(self):
        while self.start_count > 0:
            self.finish()

class DeviceAccessor(MountingAccessor):
    def __init__(self, device, fs=['iso9660', 'vfat', 'ext3']):
        """ Return a MountingAccessor for a device 'device', which should
        be a fully qualified path to a device node. """
        MountingAccessor.__init__(self, fs, device)
        self.device = device

    def __repr__(self):
        return "<DeviceAccessor: %s>" % self.device

    def canEject(self):
        return diskutil.removable(self.device)

    def eject(self):
        if self.canEject():
            self.finish()
            util.runCmd2(['eject', self.device])

class NFSAccessor(MountingAccessor):
    def __init__(self, nfspath):
        MountingAccessor.__init__(self, ['nfs'], nfspath, ['ro', 'tcp'])

class URLFileWrapper:
    "This wrapper emulate seek (forwards) for URL streams"
    SEEK_SET = 0 # SEEK_CUR and SEEK_END not supported

    def __init__(self, delegate):
        self.delegate = delegate
        self.pos = 0

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def read(self, *params):
        ret_val = self.delegate.read(*params)
        self.pos += len(ret_val)
        return ret_val

    def seek(self, offset, whence=0):
        consume = 0
        if whence == self.SEEK_SET:
            if offset >= self.pos:
                consume = offset - self.pos
            else:
                raise Exception('Backward seek not supported')
        else:
            raise Exception('Only SEEK_SET supported')

        if consume > 0:
            step = 100000
            while consume > step:
                if len(self.read(step)) != step: # Discard data
                    raise IOError('Seek beyond end of file')
                consume -= step
            if len(self.read(consume)) != consume: # Discard data
                raise IOError('Seek beyond end of file')

class URLAccessor(Accessor):
    def __init__(self, url):
        self._url = url

        if self._url.getScheme() not in ['http', 'https', 'ftp', 'file']:
            raise Exception('Unsupported URL scheme')

        if self._url.getScheme() in ['http', 'https']:
            username = self._url.getUsername()
            if username is not None:
                logger.log("Using basic HTTP authentication")
                hostname = self._url.getHostname()
                password = self._url.getPassword()
                self.passman = urllib2.HTTPPasswordMgrWithDefaultRealm()
                self.passman.add_password(None, hostname, username, password)
                self.authhandler = urllib2.HTTPBasicAuthHandler(self.passman)
                self.opener = urllib2.build_opener(self.authhandler)
                urllib2.install_opener(self.opener)

        logger.log("Initializing URLRepositoryAccessor with base address %s" % str(self._url))

    def _url_concat(url1, end):
        url1 = url1.rstrip('/')
        end = end.lstrip('/')
        return url1 + '/' + urllib.quote(end)
    _url_concat = staticmethod(_url_concat)

    def _url_decode(url):
        start = 0
        i = 0
        while i != -1:
            i = url.find('%', start)
            if (i != -1):
                hex = url[i+1:i+3]
                if re.match('[0-9A-F]{2}', hex, re.I):
                    url = url.replace(url[i:i+3], chr(int(hex, 16)), 1)
                start = i+1
        return url
    _url_decode = staticmethod(_url_decode)

    def start(self):
        pass

    def finish(self):
        pass

    def access(self, path):
        if not self._url.getScheme == 'ftp':
            return Accessor.access(self, path)

        url = self._url_concat(self._url.getPlainURL(), path)

        # if FTP, override by actually checking the file exists because urllib2 seems
        # to be not so good at this.
        try:
            (scheme, netloc, path, params, query) = urlparse.urlsplit(url)
            fname = os.path.basename(path)
            directory = self._url_decode(os.path.dirname(path[1:]))
            hostname = self._url.getHostname()
            username = self._url.getUsername()
            password = self._url.getPassword()

            # now open a connection to the server and verify that fname is in
            ftp = ftplib.FTP(hostname)
            ftp.login(username, password)
            ftp.cwd(directory)
            if ftp.size(fname) is not None:
                return True
            lst = ftp.nlst()
            return fname in lst
        except:
            # couldn't parse the server name out:
            return False

    def openAddress(self, address):
        if self._url.getScheme() in ['http', 'https']:
            ret_val = urllib2.urlopen(self._url_concat(self._url.getPlainURL(), address))
        else:
            ret_val = urllib2.urlopen(self._url_concat(self._url.getURL(), address))
        return URLFileWrapper(ret_val)

    def url(self):
        return self._url

def repositoriesFromDefinition(media, address, drivers=False):
    if media == 'local':
        # this is a special case as we need to locate the media first
        return findRepositoriesOnMedia(drivers)
    else:
        accessors = { 'filesystem': FilesystemAccessor,
                      'url': URLAccessor,
                      'nfs': NFSAccessor }
        if media in accessors:
            accessor = accessors[media](address)
        else:
            raise RuntimeError("Unknown repository media %s" % media)

        accessor.start()
        if drivers:
            rv = accessor.findDriverRepository()
        else:
            rv = accessor.findRepository()
        accessor.finish()
        return [rv] if rv else []

def findRepositoriesOnMedia(drivers=False):
    """ Returns a list of repositories available on local media. """

    static_device_patterns = [ 'sd*', 'scd*', 'sr*', 'xvd*', 'nvme*n*', 'vd*' ]
    static_devices = []
    for pattern in static_device_patterns:
        static_devices.extend(map(os.path.basename, glob.glob('/sys/block/' + pattern)))

    removable_devices = diskutil.getRemovableDeviceList()
    removable_devices = filter(lambda x: not x.startswith('fd'),
                               removable_devices)

    parent_devices = []
    partitions = []
    for dev in removable_devices + static_devices:
        if os.path.exists("/dev/%s" % dev):
            if os.path.exists("/sys/block/%s" % dev):
                dev_partitions = diskutil.partitionsOnDisk(dev)
                if len(dev_partitions) > 0:
                    partitions.extend([x for x in dev_partitions if x not in partitions])
                else:
                    if dev not in parent_devices:
                        parent_devices.append(dev)
            else:
                if dev not in parent_devices:
                    parent_devices.append(dev)

    da = None
    repos = []
    try:
        for check in parent_devices + partitions:
            device_path = "/dev/%s" % check
            logger.log("Looking for repositories: %s" % device_path)
            if os.path.exists(device_path):
                da = DeviceAccessor(device_path)
                try:
                    da.start()
                except util.MountFailureException:
                    da = None
                    continue
                else:
                    if drivers:
                        repo = da.findDriverRepository()
                    else:
                        repo = da.findRepository()
                    if repo:
                        repos.append(repo)
                    da.finish()
                    da = None
    finally:
        if da:
            da.finish()

    return repos

def installFromYum(targets, mounts, progress_callback, cachedir):
        # Use a temporary file to avoid deadlocking
        stderr = tempfile.TemporaryFile()

        yum_command = ['yum', '-c', '/root/yum.conf',
                       '--installroot', mounts['root'],
                       'install', '-y'] + targets
        logger.log("Running yum: %s" % ' '.join(yum_command))
        p = subprocess.Popen(yum_command, stdout=subprocess.PIPE, stderr=stderr)
        count = 0
        total = 0
        verify_count = 0
        while True:
            line = p.stdout.readline()
            if not line:
                break
            line = line.rstrip()
            logger.log("YUM: %s" % line)
            if line == 'Resolving Dependencies':
                progress_callback(1)
            elif line == 'Dependencies Resolved':
                progress_callback(3)
            elif line.startswith('-----------------------------------------'):
                progress_callback(7)
            elif line == 'Running transaction':
                progress_callback(10)
            elif line.endswith(' will be installed') or line.endswith(' will be updated'):
                total += 1
            elif line.startswith('  Installing : ') or line.startswith('  Updating : '):
                count += 1
                if total > 0:
                    progress_callback(10 + int((count * 80.0) / total))
            elif line.startswith('  Verifying  : '):
                verify_count += 1
                progress_callback(90 + int((verify_count * 10.0) / total))
        rv = p.wait()
        stderr.seek(0)
        stderr = stderr.read()
        if stderr:
            logger.log("YUM stderr: %s" % stderr.strip())

        shutil.rmtree(os.path.join(mounts['root'], cachedir))

        if rv:
            logger.log("Yum exited with %d" % rv)
            # See:
            # https://github.com/rpm-software-management/urlgrabber/blob/master/urlgrabber/grabber.py#L725
            # https://github.com/rpm-software-management/yum/blob/master/yum/yumRepo.py#L1709
            if (stderr.find('repomd.xml.asc: [Errno 14]', 0) >= 0 or
                stderr.find('repomd.xml: [Errno -1]', 0) >= 0):
                raise RepoSecurityConfigError(
"""The authenticity of the repository metadata could not be established.\n
Aborting installation.\n
If you are using your own modified (and trusted) repository over a trusted network, you may consider disabling authenticity verification."""
                )
            else:
                # See:
                # https://github.com/rpm-software-management/rpm/blob/fc51fc39cff7970b10ef4da30f75d1db8eaa8025/lib/package.c#L311
                # https://github.com/rpm-software-management/rpm/blob/b4c832caed0da0c4b0710cfe2510203a3940c2db/rpmio/rpmlog.c#L190
                # https://github.com/rpm-software-management/rpm/blob/362c4401979f896de1e69a3e18d33954953912cc/lib/rpmvs.c#L283
                # https://github.com/rpm-software-management/rpm/blob/362c4401979f896de1e69a3e18d33954953912cc/lib/rpmvs.c#L491
                res = re.match('^(?:warning|error): (?:/.*/)?([^/]+.rpm): (?:.*) (?:BAD|NOKEY|NOTTRUSTED|NOTFOUND|UNKNOWN)', stderr)
                if res:
                    raise RepoSecurityConfigError(
"""The authenticity of the %s package could not be established.\n
Aborting installation.\n
If you are using your own modified (and trusted) repository over a trusted network, you may consider disabling authenticity verification.""" % \
                        elide(res.groups()[0], 40)
                    )
            raise UnrecoverableRepoError("Error installing packages")

def installFromRepos(progress_callback, repos, mounts):
    """Install from a stacked set of repositories"""

    cachedir = "var/cache/yum/installer"
    for repo in repos:
        repo._accessor.start()

    try:
        # Build a yum config
        with open('/root/yum.conf', 'w') as yum_conf:
            yum_conf.write(_generateYumConf(cachedir))
            for repo in repos:
                url = repo._accessor.url()
                yum_conf.write("""
[%s]
name=%s
baseurl=%s
""" % (repo.identifier(), repo.identifier(), url.getPlainURL()))
                username = url.getUsername()
                if username is not None:
                    yum_conf.write("username=%s\n" % (url.getUsername(),))
                password = url.getPassword()
                if password is not None:
                    yum_conf.write("password=%s\n" % (url.getPassword(),))
                repo_config = repo._repo_config()
                if repo_config is not None:
                    yum_conf.write(repo_config)


        repos[0].disableInitrdCreation(mounts['root'])
        targets = []
        for repo in repos:
            if repo._targets:
                targets += repo._targets
        targets = list(set(targets))

        installFromYum(targets, mounts, progress_callback, cachedir)
        repos[0].enableInitrdCreation()
    finally:
        for repo in repos:
            repo._accessor.finish()
