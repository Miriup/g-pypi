#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Utility functions helping conversion of metadata from
a python package to ebuild.

"""

import urlparse
import socket
import logging
import httplib
import re
import os

from portage import pkgsplit
try:
    # portage >=2.2
    from portage import dep as portage_dep
    from portage import exception as portage_exception
except ImportError:
    # portage <=2.1
    from portage import portage_dep
    from portage import portage_exception

from gpypi2.portage_utils import PortageUtils
from gpypi2.exc import *


log = logging.getLogger(__name__)

class Enamer(object):
    """Ebuild namer

       Collection of methods for metadata conversion
       from Python distribution syntax to ebuild syntax

       Most of utilities are classmethods, for purpose
       of customization support.

    """

    @classmethod
    def get_filename(cls, uri):
        """
        Return file name minus extension from src_uri

        :param uri: URI to package with no variables substitution
        :type uri: string
        :returns: filename
        :rtype: string

        **Example:**

        >>> Enamer.get_filename('http://somesite.com/foobar-1.0.tar.gz')
        'foobar-1.0'

        """
        path = urlparse.urlparse(uri)[2]
        path = path.split('/')
        return cls.strip_ext(path[-1])

    @classmethod
    def strip_ext(cls, path):
        """Strip possible extensions from filename.

        Supported, valid extensions: zip, tgz, tar.gz, tar.bz2, tbz2

        :param path: Filesystem path to a file.
        :type path: string
        :returns: path minus extension
        :rtype: string

        **Example:**

        >>> Enamer.strip_ext('/home/user/filename.zip')
        '/home/user/filename'
        >>> Enamer.strip_ext('/home/user/filename.unknown')
        '/home/user/filename.unknown'

        """
        valid_extensions = [".zip", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2"]
        for ext in valid_extensions:
            if path.endswith(ext):
                return path[:-len(ext)]
        return path

    @classmethod
    def is_valid_uri(cls, uri):
        """
        Check if URI's addressing scheme is valid

        :param uri: URI to package with no variable substitution
        :type uri: string
        :returns: boolean

        **Example:**

        >>> Enamer.is_valid_uri('http://...')
        True
        >>> Enamer.is_valid_uri('foobar://...')
        False

        """
        return uri.startswith("http:") or uri.startswith("ftp:") or \
                uri.startswith("mirror:") or uri.startswith("svn:")

    @classmethod
    def _is_good_filename(cls, uri):
        """If filename is sane enough to deduce PN & PV, return pkgsplit results"""
        if cls.is_valid_uri(uri):
            psplit = cls.split_uri(uri)
            if psplit and psplit[0].islower():
                return psplit

    @classmethod
    def split_uri(cls, uri):
        """Try to split a URI into PN, PV and REV

        :param uri: SRC_URI
        :type uri: string
        :returns: PN, PV, REV
        :rtype: tuple of strings

        **Example:**

        >>> Enamer.split_uri('http://www.foobar.com/foobar-1.0.tar.gz')
        ('foobar', '1.0', 'r0')
        >>> Enamer.split_uri('http://www.foobar.com/foo-2.3_beta3-r5.tar.gz')
        ('foo', '2.3_beta3', 'r5')

        """
        p = cls.get_filename(uri)
        return pkgsplit(p)

    @classmethod
    def _get_components(cls, uri):
        """Split uri into pn and pv and new uri"""
        p = cls.get_filename(uri)
        psplit = cls.split_uri(uri)
        uri_out = uri.replace(p, "${P}")
        pn = psplit[0].lower()
        pv = psplit[1]
        return uri_out, pn, pv

    @classmethod
    def _guess_components(cls, my_p):
        """Try to break up raw MY_P into PN and PV"""
        pn, pv = "", ""

        # Ok, we just have one automagical test here.
        # We should look at versionator.eclass for inspiration
        # and then come up with several functions.
        my_p = my_p.replace("_", "-")

        psplit = pkgsplit(my_p)
        if psplit:
            pn = psplit[0]
            pv = psplit[1]
        log.debug("guess_components got: pn(%s), pv(%s)", pn, pv)
        return pn, pv

    @classmethod
    def parse_pv(cls, up_pn, up_pv, pn="", pv="", my_pn=None, my_pv=None):
        """
        :param up_pn: Upstream package name
        :param up_pv: Upstream package version
        :param pn: Converted Gentoo package name
        :param pv: Converted Gentoo package version
        :param my_pn: Bash substitutions for original package name
        :param my_pv: Bash substitutions for original package version
        :type up_pn: string
        :type up_pv: string
        :type pn: string
        :type pv: string
        :type my_pn: list
        :type my_pv: list
        :returns: (PN, PV, MY_PN, MY_PV)
        :rtype: tuple of (string, string, list, list)

        Can't determine PV from upstream's version.
        Do our best with some well-known versioning schemes:

        * 1.0a1 (1.0_alpha1)
        * 1.0-a1 (1.0_alpha1)
        * 1.0b1 (1.0_beta1)
        * 1.0-b1 (1.0_beta1)
        * 1.0-r1234 (1.0_pre1234)
        * 1.0dev-r1234 (1.0_pre1234)
        * 1.0.dev-r1234 (1.0_pre1234)
        * 1.0dev-20091118 (1.0_pre20091118)
        * (for more examples look at test_enamer.py)

        Regex match.groups():
            * pkgfoo-1.0.dev-r1234
            * group 1 pv major (1.0)
            * group 2 replace this with portage suffix (.dev-r)
            * group 3 suffix version (1234)

        The order of the regexes is significant. For instance if you have
        .dev-r123, dev-r123 and -r123 you should order your regex's in
        that order.

        The chronological portage release versions are:

        * _alpha
        * _beta
        * _pre
        * _rc
        * release
        * _p

        **Example:**

        >>> Enamer.parse_pv('foo.bar', '1.0b2')
        ('foo-bar', '1.0_beta2', ['${PN/-/.}'], ['${PV/_beta/b}'])

        .. note::
            The number of regex's could have been reduced, but we use four
            number of match.groups every time to simplify the code

        """
        bad_suffixes = re.compile(
            r'((?:[._-]*)(?:dev|devel|final|stable|snapshot)$)', re.I)
        revision_suffixes = re.compile(
            r'(.*?)([\._-]*(?:r|patch|p)[\._-]*)([0-9]*)$', re.I)
        suf_matches = {
                '_pre': [
                    r'(.*?)([\._-]*dev[\._-]*r?)([0-9]+)$',
                    r'(.*?)([\._-]*(?:pre|preview)[\._-]*)([0-9]*)$',
                ],
                '_alpha': [
                    r'(.*?)([\._-]*(?:alpha|test)[\._-]*)([0-9]*)$',
                    r'(.*?)([\._-]*a[\._-]*)([0-9]*)$',
                    r'(.*[^a-z])(a)([0-9]*)$',
                ],
                '_beta': [
                    r'(.*?)([\._-]*beta[\._-]*)([0-9]*)$',
                    r'(.*?)([\._-]*b)([0-9]*)$',
                    r'(.*[^a-z])(b)([0-9]*)$',
                ],
                '_rc': [
                    r'(.*?)([\._-]*rc[\._-]*)([0-9]*)$',
                    r'(.*?)([\._-]*c[\._-]*)([0-9]*)$',
                    r'(.*[^a-z])(c[\._-]*)([0-9]+)$',
                ],
        }
        rs_match = None
        my_pn = my_pn or []
        my_pv = my_pv or []
        additional_version = ""
        log.debug("parse_pv: up_pv(%s)", up_pv)

        rev_match = revision_suffixes.search(up_pv)
        if rev_match:
            pv = up_pv = rev_match.group(1)
            replace_me = rev_match.group(2)
            rev = rev_match.group(3)
            additional_version = '.' + rev
            my_pv.append("${PV: -%d}%s" % (len(additional_version), replace_me + rev))
            log.debug("parse_pv: new up_pv(%s), additional_version(%s), my_pv(%s)",
                up_pv, additional_version, my_pv)
            # TODO: if ALSO suf_matches succeeds, it's not implemented

        for this_suf in suf_matches.keys():
            if rs_match:
                break
            for regex in suf_matches[this_suf]:
                rsuffix_regex = re.compile(regex, re.I)
                rs_match = rsuffix_regex.match(up_pv)
                if rs_match:
                    log.debug("parse_pv: chosen regex: %s", regex)
                    portage_suffix = this_suf
                    break

        if rs_match:
            # e.g. 1.0.dev-r1234
            major_ver = rs_match.group(1) # 1.0
            replace_me = rs_match.group(2) #.dev-r
            rev = rs_match.group(3) #1234
            pv = major_ver + portage_suffix + rev
            my_pv.append("${PV/%s/%s}" % (portage_suffix, replace_me))
            log.debug("parse_pv: major_ver(%s) replace_me(%s), rev(%s)", major_ver, replace_me, rev)
        else:
            # Single suffixes with no numeric component are simply removed.
            match = bad_suffixes.search(up_pv)
            if match:
                suffix = match.groups()[0]
                my_pv.append("${PV}%s" % suffix)
                pv = up_pv[:-(len(suffix))]

        pv = pv + additional_version
        log.debug("parse_pv: pv(%s), my_pv(%s)", pv, my_pv)

        pn, my_pn = cls.parse_pn(up_pn, my_pn)
        return pn, pv, my_pn, my_pv

    @classmethod
    def parse_pn(cls, pn, my_pn=None):
        """Convert PN to MY_PN if needed

        :params pn: Gentoo package name
        :params my_pn: Bash substitutions to get original name
        :type pn: string
        :type my_pn: list
        :returns: (PN, MY_PN)
        :rtype: tuple of (string, list)

        **Example:**

        >>> Enamer.parse_pn('Test-Me')
        ('test-me', ['Test-Me'])
        >>> Enamer.parse_pn('test.me')
        ('test-me', ['${PN/-/.}'])

        """
        my_pn = my_pn or []
        if not pn.islower():
            # up_pn is lower but uri has upper-case
            log.debug('parse_pn: pn is not lowercase, converting to my_pn')
            if not my_pn:
                my_pn.append(pn)
            pn = pn.lower()

        if "." in pn:
            log.debug("parse_pn: dot found in pn")
            my_pn.append('${PN/-/.}')
            pn = pn.replace('.', '-')

        if " " in pn:
            log.debug("parse_pn: space found in pn")
            my_pn.append('${PN/-/ }')
            pn = pn.replace(' ', '-')

        #if not my_pn:
            #my_pn = "-".join(p.split("-")[:-1])
            #if (my_pn == pn) or (my_pn == "${PN}"):
                #my_pn = ""
            #log.debug("set my_on to %s", my_pn)

        log.debug("parse_pn: my_pn(%s) pn(%s)", my_pn, pn)
        return pn, my_pn

    @classmethod
    def sanitize_uri(cls, uri):
        """
        Return URI without any un-needed extension.

        :param uri: URI to pacakge with no variable substitution
        :type uri: string
        :returns: URL without fragment, parameters and query
        :rtype: string

        **Example:**

        >>> Enamer.sanitize_uri('http://downloads.sourceforge.net/pythonreports/PythonReports-0.3.1.tar.gz?modtime=1182702645&big_mirror=0')
        'http://downloads.sourceforge.net/pythonreports/PythonReports-0.3.1.tar.gz'

        """
        skinned_uri = urlparse.urlparse(uri)
        return urlparse.urlunparse(skinned_uri[:3] + ('',)*3)

    @classmethod
    def get_vars(cls, uri, up_pn, up_pv, pn="", pv="", my_pn=None, my_pv=None):
        """
        Determine P* and MY_* ebuild variables

        :param uri: HTTP URL to download link for a package
        :param up_pn: Upstream package name
        :param up_pv: Upstream package version
        :param pn: Converted package name
        :param pv: Converted package version
        :param my_pn: Bash substitution for upstream package name
        :param my_pv: Bash substitution for upstream package version
        :type uri: string
        :type up_pn: string
        :type up_pv: string
        :type pn: string
        :type pv: string
        :type my_pn: list
        :type my_pv: list
        :returns:
            * pn -- Ebuild package name
            * pv -- Ebuild package version
            * p -- Ebuild whole package name (name + version)
            * my_p -- Upstream whole package name (name + version)
            * my_pn -- Bash substitution for upstream package name
            * my_pv -- Bash substitution for upstream package version
            * my_p_raw -- my_p extracted from SRC_URI
            * src_uri -- Ebuild SRC_URI with MY_P variable
        :rtype: dict

        **Examples of what it can detect/convert** (see test_enamer.py for full capabilities)

        http://www.foo.com/pkgfoo-1.0.tbz2

        * PN="pkgfoo"
        * PV="1.0"
        * Ebuild name: pkgfoo-1.0.ebuild
        * SRC_URI="http://www.foo.com/${P}.tbz2"

        http://www.foo.com/PkgFoo-1.0.tbz2

        * PN="pkgfoo"
        * PV="1.0"
        * Ebuild name: pkgfoo-1.0.ebuild
        * MY_P="PkgFoo-${PV}"
        * SRC_URI="http://www.foo.com/${MY_P}.tbz2"

        http://www.foo.com/pkgfoo_1.0.tbz2

        * PN="pkgfoo"
        * PV="1.0"
        * Ebuild name: pkgfoo-1.0.ebuild
        * MY_P="${PN}_${PV}"
        * SRC_URI="http://www.foo.com/${MY_P}.tbz2"

        http://www.foo.com/PKGFOO_1.0.tbz2

        * PN="pkgfoo"
        * PV="1.0"
        * Ebuild name: pkgfoo-1.0.ebuild
        * MY_P="PKGFOO_${PV}"
        * SRC_URI="http://www.foo.com/${MY_P}.tbz2"

        http://www.foo.com/pkg-foo-1.0_beta1.tbz2

        * PN="pkg-foo"
        * PV="1.0_beta1"
        * Ebuild name: pkg-foo-1.0_beta1.ebuild
        * SRC_URI="http://www.foo.com/${P}.tbz2"

        **Example:**

        >>> d = Enamer.get_vars('http://www.foo.com/pkg.foo-1.0b1.tbz2', 'pkg.foo', '1.0b1')
        >>> assert d['pn'] == 'pkg-foo'
        >>> assert d['pv'] == '1.0_beta1'
        >>> assert d['p'] == 'pkg-foo-1.0_beta1'
        >>> assert d['my_pv'] == ['${PV/_beta/b}']
        >>> assert d['my_pn'] == ['${PN/-/.}']
        >>> assert d['my_p'] == '${MY_PN}-${MY_PV}'
        >>> assert d['my_p_raw'] == 'pkg.foo-1.0b1'
        >>> assert d['src_uri'] == 'http://www.foo.com/${MY_P}.tbz2'
        >>> assert len(d) == 8

        """
        log.debug("get_vars: %r" % locals())
        my_pn = my_pn or []
        my_pv = my_pv or []
        my_p = ""
        INVALID_VERSION = False
        uri = cls.sanitize_uri(uri)

        # Test for PV with -r1234 suffix
        # Portage uses -r suffixes for it's own ebuild revisions so
        # We have to convert it to _pre or _alpha etc.
        try:
            tail = up_pv.split("-")[-1][0]
        except:
            pass
        else:
            if tail == "r":
                INVALID_VERSION = True
                log.debug("We have a version with a -r### suffix")

        portage_atom = "=dev-python/%s-%s" % (up_pn, up_pv)
        if not portage_dep.isvalidatom(portage_atom):
            INVALID_VERSION = True
            log.debug("%s is not valid portage atom", portage_atom)

        if INVALID_VERSION:
            pn, pv, my_pn, my_pv = \
                cls.parse_pv(up_pn, up_pv, pn, pv, my_pn, my_pv)

        # No PN or PV given on command-line, try upstream's name/version
        if not pn and not pv:
            log.debug("pn and pv not given, trying upstream")
            # Try to determine pn and pv from uri
            parts = cls.split_uri(uri)
            if parts:
                # pylint: disable-msg=W0612
                # unused variable 'rev'
                # The 'rev' is never used because these are
                # new ebuilds being created.
                pn, pv, rev = parts
            else:
                pn = up_pn
                pv = up_pv

        # Try upstream's version if it could't be determined from uri or cli option
        elif pn and not pv:
            pv = up_pv
        elif not pn and pv:
            pn = up_pn.lower()

        pn, my_pn = cls.parse_pn(pn, my_pn)

        p = "%s-%s" % (pn, pv)
        log.debug("get_vars: p(%s)", p)

        # Make sure we have a valid P
        atom = "=dev-python/%s-%s" % (pn, pv)
        if not portage_dep.isvalidatom(atom):
            log.error(locals())
            raise GPyPiInvalidAtom("%s is not valid portage atom. "
            "We could not determine right pn and/or pv." % atom)

        # Check if we need to use MY_P based on src's uri
        src_uri, my_p_raw = cls.get_my_p(uri)
        if my_p_raw == p:
            my_pn = []
            my_p_raw = ''
            src_uri = src_uri.replace("${MY_P}", "${P}")
        elif my_pn or my_pv:
            src_uri, my_p_raw = cls.get_my_p(uri)
            log.debug("getting SRC_URI with ${MY_P}: %s %s %s", src_uri, my_p, my_p_raw)
        else:
            src_uri, my_p, my_pn, my_p_raw = cls._get_src_uri(uri, my_pn)
            log.debug("getting SRC_URI: %s %s %s", src_uri, my_p, my_p_raw)

        log.debug("before MY_P guessing: %r", locals())
        if my_pn or my_pv:
            my_p = "%s-%s" % ("${MY_PN}" if my_pn else "${PN}",
                "${MY_PV}" if my_pv else "${PV}")

        return {
            'pn': pn,
            'pv': pv,
            'p': p,
            'my_p': my_p,
            'my_pn': my_pn,
            'my_pv': my_pv,
            'my_p_raw': my_p_raw,
            'src_uri': src_uri,
        }

    @classmethod
    def _get_src_uri(cls, uri, my_pn):
        """
        """
        my_p = my_p_raw = ''
        if cls._is_good_filename(uri):
            src_uri, pn, pv = cls._get_components(uri)
        else:
            src_uri, my_p = cls.get_my_p(uri)
            pn, pv = cls._guess_components(my_p)
            if pn and pv:
                my_p_raw = my_p
                pn, my_pn = cls.parse_pn(pn)
                if my_pn and my_pn != pn:
                    for one_my_pn in my_pn:
                        my_p = my_p.replace(one_my_pn, "${MY_PN}")
                else:
                    my_p = my_p.replace(pn, "${PN}")
                my_p = my_p.replace(pv, "${PV}")

        log.debug("get_src_uri: src_uri(%s), my_p(%s), my_pn(%s), my_p_raw(%s)",
            src_uri, my_p, my_pn, my_p_raw)
        return src_uri, my_p, my_pn, my_p_raw

    @classmethod
    def get_my_p(cls, uri):
        """Return MY_P and new uri with MY_P in it

        :param uri: HTTP URL to a package
        :returns: (uri with ${MY_P}, ${MY_P})
        :rtype: tuple of strings

        **Example:**

        >>> Enamer.get_my_p('http://www.foobar.com/foobar-1.0.tar.gz')
        ('http://www.foobar.com/${MY_P}.tar.gz', 'foobar-1.0')

        """
        my_p = cls.get_filename(uri)
        log.debug('get_my_p out of uri: %s', my_p)
        return uri.replace(my_p, "${MY_P}"), my_p

    @classmethod
    def convert_license(cls, license):
        """
        Map defined classifier license to Portage license

        PyPi list of licences:
        http://pypi.python.org/pypi?:action=list_classifiers

        :param license: PyPi license classifier
        :type license: string
        :returns: Portage license or ""
        :rtype: string

        **Example:**

        >>> Enamer.convert_license("License :: OSI Approved :: BSD License")
        'BSD-2'
        >>> Enamer.convert_license("License :: OSI Approved :: foobar")
        ''

        """
        my_license = license.split(":: ")[-1]
        # TODO: renew list of licences
        known_licenses = {
            "Aladdin Free Public License (AFPL)": "Aladdin",
            "Academic Free License (AFL)": "AFL-3.0",
            "Apache Software License": "Apache-2.0",
            "Apple Public Source License": "Apple",
            "Artistic License": "Artistic-2",
            "BSD License": "BSD-2",
            "Common Public License": "CPL-1.0",
            "GNU Free Documentation License (FDL)": "FDL-3",
            "GNU General Public License (GPL)": "GPL-2",
            "GNU Library or Lesser General Public License (LGPL)": "LGPL-2.1",
            "IBM Public License": "IBM",
            "Intel Open Source License": "Intel",
            "MIT License": "MIT",
            "Mozilla Public License 1.0 (MPL)": "MPL",
            "Mozilla Public License 1.1 (MPL 1.1)": "MPL-1.1",
            "Nethack General Public License": "nethack",
            "Open Group Test Suite License": "OGTSL",
            "Python License (CNRI Python License)": "PYTHON",
            "Python Software Foundation License": "PSF-2.4",
            "Qt Public License (QPL)": "QPL",
            "Sleepycat License": "DB",
            "Sun Public License": "SPL",
            "University of Illinois/NCSA Open Source License": "ncsa-1.3",
            "W3C License": "WC3",
            "zlib/libpng License": "ZLIB",
            "Zope Public License": "ZPL",
            "Public Domain": "public-domain"
            }
        return known_licenses.get(my_license, "")

    @classmethod
    def is_valid_portage_license(cls, license):
        """
        Check if license string matches a valid one in ${PORTDIR}/licenses

        :param license: Portage license name
        :type license: string
        :returns: True if license is valid/exists
        :rtype: bool

        **Example:**

        >>> Enamer.is_valid_portage_license("KQEMU")
        True
        >>> Enamer.is_valid_portage_license("foobar")
        False

        """
        return os.path.exists(os.path.join(PortageUtils.get_portdir(), "licenses", license))

    @classmethod
    def format_depend(cls, dep_list):
        """
        Return a formatted string for ebuild DEPEND/RDEPEND

        :param dep_list: list of portage-ready dependency strings
        :returns: formatted DEPEND or RDEPEND string ready for ebuild

        * First dep has no tab, has linefeed
        * Middle deps have tab and linefeed
        * Last dep has tab, no linefeed

        **Example:**

        >>> print Enamer.format_depend(["dev-python/foo-1.0", #doctest: +NORMALIZE_WHITESPACE
        ... ">=dev-python/bar-0.2", "dev-python/zaba"])
        dev-python/foo-1.0
            >=dev-python/bar-0.2
            dev-python/zaba

        """

        if not len(dep_list):
            return ""

        return "\n\t".join(dep_list)


class SrcUriProvider(object):
    """Base class for SRC_URI providers

    :param uri: HTTP URI
    :type uri: string
    """

    def __init__(self, uri):
        self.uri = uri
        self.up = urlparse.urlparse(self.uri)

    def parse_uri(self):
        """Parse URI to mirror//provider format.

        Also determines a homepage string.

        Inheriting class should override this method.
        """
        raise NotImplemented

    def is_uri_online(self):
        """Issue HTTP HEAD request to confirm location of URI"""
        conn = httplib.HTTPConnection(self.up.netloc, timeout=3)
        try:
            conn.request('HEAD', self.up.path)
            resp = conn.getresponse()
        except (httplib.HTTPException, socket.error):
            return False
        return resp.status == 302


class SourceForgeSrcUri(SrcUriProvider):
    """"""

    def parse_uri(self):
        """ Change URI to mirror://sourceforge format. """

        host = self.up[1]
        upath = self.up[2]
        if upath.startswith("/sourceforge"):
            upath = upath[12:]
        if ("sourceforge" in host) or (host.endswith("sf.net")):
            uri_out = 'mirror://sourceforge%s' % upath
            homepage = "http://sourceforge.net/projects/%s/" % \
                       upath.split("/")[1]
        return uri_out, homepage


class PyPiSrcUri(SrcUriProvider):
    """"""
