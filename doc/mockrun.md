The `mockrun-installer` script allows to quickly check some of the TUI
changes locally, providing a big speedup of dev cycle as compared to
remastering an ISO image and testboot it.

The execution environment has to be as close a possible as the one
used as dom0 on the install ISO, which in turn is very close to a
XenServer or XCP-ng dom0 OS, currently based on CentOS 7.

The xcp-ng-build-env docker container is one possible choice for a
runtime environment.  It will require installation of a few extra
packages:

```
sudo yum install -y xcp-python-libs newt-python python2-simplejson python-six pyOpenSSL
```

## details of operation

This script fakes many calls to lower-level python libraries,
currently in a hardcoded fashion, so it possibly has to be hand-edited
to check other use cases.

It will attempt to run system commands, NEVER try to run it as root on
a real system.

No specific way to interrupt the TUI is provided, but causing it to
"abort and reboot" will cause it to exit.  As a last resort going to
the final dialog for confirmation of install (F12 is your friend) and
proceeeding will attempt execution of unstubbed commands
(e.g. `uuidgen`) and abort.  Or you can kill the process externally
and `reset` your tty afterwards.

`installer.log` is generated in current directory.
