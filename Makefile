USE_BRANDING := yes
IMPORT_BRANDING := yes
# makefile for the host installer components in build system
include $(B_BASE)/common.mk
include $(B_BASE)/rpmbuild.mk

SM_SOURCE_REPO = $(call git_loc,sm)

# For debugging
.PHONY: %var
%var:
	@echo "$* = $($*)"

REPO_NAME := host-installer
SPEC_FILE := $(REPO_NAME).spec
RPM_BUILD_COOKIE := $(MY_OBJ_DIR)/.rpm_build_cookie
REPO_STAMP := $(call git_req,$(REPO_NAME))

$(eval $(shell $(call git_cset_number,$(REPO_NAME)))) # Defines CSET_NUMBER for us
HOST_INSTALLER_VERSION := xs$(PLATFORM_VERSION).$(CSET_NUMBER)
HOST_INSTALLER_RELEASE := 1
HOST_INSTALLER_DIR := /opt/xensource/installer


.PHONY: build
build: $(RPM_BUILD_COOKIE) $(MY_OUTPUT_DIR)/host-installer.inc
	@ :


SOURCES := $(RPM_SOURCESDIR)/host-installer-$(HOST_INSTALLER_VERSION).tar.bz2
SOURCES += $(RPM_SPECSDIR)/$(SPEC_FILE)
SOURCES += $(RPM_SOURCESDIR)/multipath.conf

HOST_INSTALLER_TAR_EXCLUDE := \
	--delete '$(REPO_NAME)-$(HOST_INSTALLER_VERSION)/mk' \
	--delete '$(REPO_NAME)-$(HOST_INSTALLER_VERSION)/tests' \
	--delete '$(REPO_NAME)-$(HOST_INSTALLER_VERSION)/oem' \
	--delete '$(REPO_NAME)-$(HOST_INSTALLER_VERSION)/sample-version.py'

$(RPM_SOURCESDIR)/host-installer-$(HOST_INSTALLER_VERSION).tar.bz2: $(RPM_SOURCESDIRSTAMP)
	{ set -e; set -o pipefail; \
	cd $(call git_loc,$(REPO_NAME)); \
	git archive --prefix=$(REPO_NAME)-$(HOST_INSTALLER_VERSION)/ --format=tar HEAD | \
		tar -f - $(HOST_INSTALLER_TAR_EXCLUDE) | \
		bzip2 -4 > $@.tmp; \
	mv -f $@.tmp $@; \
	}

$(RPM_SOURCESDIR)/multipath.conf: $(SM_SOURCE_REPO)/multipath/multipath.conf
# Generate a multipath configuration from sm's copy, removing the blacklist
# and blacklist_exception sections.
	sed 's/\(^[[:space:]]*find_multipaths[[:space:]]*\)yes/\1no/' < $< > $@

$(RPM_SPECSDIR)/$(SPEC_FILE): $(SPEC_FILE).in $(RPM_SPECSDIRSTAMP)
	{ set -e; set -o pipefail; \
	sed -e s/@HOST_INSTALLER_VERSION@/$(HOST_INSTALLER_VERSION)/g \
	    -e s/@HOST_INSTALLER_RELEASE@/$(HOST_INSTALLER_RELEASE)/g \
	    -e s!@HOST_INSTALLER_DIR@!$(HOST_INSTALLER_DIR)!g \
	< $< > $@.tmp; \
	mv -f $@.tmp $@; \
	}

$(RPM_BUILD_COOKIE): $(RPM_DIRECTORIES) $(SOURCES)
	$(RPMBUILD) -ba $(RPM_SPECSDIR)/$(SPEC_FILE)
	touch $@

.PHONY: $(MY_OUTPUT_DIR)/host-installer.inc
$(MY_OUTPUT_DIR)/host-installer.inc: $(MY_OUTPUT_DIRSTAMP)
	{ set -e; set -o pipefail; \
	{ echo HOST_INSTALLER_PKG_NAME := host-installer; \
	  echo HOST_INSTALLER_PKG_VERSION := $(HOST_INSTALLER_VERSION)-$(HOST_INSTALLER_RELEASE); \
	  echo HOST_INSTALLER_PKG_FILE := RPMS/$(DOMAIN0_ARCH)/host-installer-\$$\(HOST_INSTALLER_PKG_VERSION\).$(DOMAIN0_ARCH).rpm; \
	  echo HOST_INSTALLER_STARTUP_PKG_FILE := RPMS/$(DOMAIN0_ARCH)/host-installer-startup-\$$\(HOST_INSTALLER_PKG_VERSION\).$(DOMAIN0_ARCH).rpm; \
	} > $@.tmp; \
	mv -f $@.tmp $@; \
	}

.PHONY: clean
clean:
	rm -f $(RPM_BUILD_COOKIE)
	rm -f $(SOURCES)
	rm -f $(SOURCES:%=%.tmp)
	rm -f $(MY_OBJ_DIR)/version.inc $(MY_OUTPUT_DIR)/host-installer.inc
