# Copyright (c) 2017 Red Hat, Inc. All rights reserved. This copyrighted
# material is made available to anyone wishing to use, modify, copy, or
# redistribute it subject to the terms and conditions of the GNU General
# Public License v.2 or later.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

import datetime
import dateutil.parser
import email
import email.header
import enum
import json
import logging
import requests
import re
import urllib
import xmlrpclib
import sktm

# TODO Move common code to a common parent class

SKIP_PATTERNS = [
        "\[[^\]]*iproute.*?\]",
        "\[[^\]]*pktgen.*?\]",
        "\[[^\]]*ethtool.*?\]",
        "\[[^\]]*git.*?\]",
        "\[[^\]]*pull.*?\]",
        "pull.?request"
]


def stringify(v):
    """Convert any value to a str object

    xmlrpc is not consistent: sometimes the same field
    is returned a str, sometimes as unicode. We need to
    handle both cases properly.
    """
    if type(v) is str:
        return v
    elif type(v) is unicode:
        return v.encode('utf-8')
    else:
        return str(v)


# Internal RH PatchWork adds a magic API version with each call
# this class just magically adds/removes it
class RpcWrapper:
    def __init__(self, real_rpc):
        self.rpc = real_rpc
        # patchwork api coded to
        self.version = 1010

    def _wrap_call(self, rpc, name):
        # Wrap a RPC call, adding the expected version number as argument
        fn = getattr(rpc, name)

        def wrapper(*args, **kwargs):
            return fn(self.version, *args, **kwargs)
        return wrapper

    def _return_check(self, r):
        # Returns just the real return value, without the version info.
        v = self.version
        if r[0] != v:
            raise RpcProtocolMismatch('Patchwork API mismatch (%i, '
                                      'expected %i)' % (r[0], v))
        return r[1]

    def _return_unwrapper(self, fn):
        def unwrap(*args, **kwargs):
            return self._return_check(fn(*args, **kwargs))
        return unwrap

    def __getattr__(self, name):
        # Add the RPC version checking call/return wrappers
        return self._return_unwrapper(self._wrap_call(self.rpc, name))


class pwresult(enum.IntEnum):
    """Patchwork state codes"""
    PENDING = 0
    SUCCESS = 1
    WARNING = 2
    FAILURE = 3


class skt_patchwork2(object):
    """
    A Patchwork REST interface
    """
    def __init__(self, baseurl, projectname, since, apikey=None):
        """
        Initialize a Patchwork REST interface.

        Args:
            baseurl:        Patchwork base URL.
            projectname:    Patchwork project name, or None.
            since:          Last processed patch timestamp in a format
                            accepted by dateutil.parser.parse. Patches with
                            this or earlier timestamp will be ignored.
            apikey:         Patchwork API authentication token.
        """
        # Base Patchwork URL
        self.baseurl = baseurl
        # Last processed patch timestamp in a dateutil.parser.parse format
        self.since = since
        # TODO Describe
        self.nsince = None
        # Patchwork API authentication token.
        self.apikey = apikey
        # JSON representation of API URLs retrieved from the Patchwork server
        self.apiurls = self.get_apiurls()
        # A regular expression matching names of the patches to skip
        self.skp = re.compile("%s" % "|".join(SKIP_PATTERNS), re.IGNORECASE)
        # JSON representation of the specified project (if any), retrieved
        # from the Patchwork server
        self.project = None

        if projectname is not None:
            self.project = self.get_project(projectname)

    # TODO Convert this to a simple function
    @property
    def projectid(self):
        return int(self.project.get("id"))

    # TODO Convert this to a simple function
    @property
    def newsince(self):
        return self.nsince.isoformat() if self.nsince else None

    # FIXME Rename to use a verb
    def patchurl(self, patch):
        """
        Retrieve a patch URL from a JSON patch object.

        Args:
            patch:  The JSON patch object to get URL from.

        Returns:
            URL of the patch.
        """
        return "%s/patch/%d" % (self.baseurl, patch.get("id"))

    def get_project(self, pname):
        """
        Retrieve JSON representation of the specified project from the
        Patchwork server.

        Args:
            pname:  The name of the project to retrieve.

        Returns:
            The JSON representation of the specified project.
        """
        r = requests.get("%s/%s" % (self.apiurls.get("projects"), pname))
        if r.status_code != 200:
            raise Exception("Can't get project data: %s %d" % (pname,
                            r.status_code))
        return r.json()

    def get_apiurls(self):
        """
        Retrieve JSON representation of the list of API URLs supported by the
        Patchwork server.

        Returns:
            The JSON representation of the API URLs.
        """
        r = requests.get("%s/api/1.0" % self.baseurl)
        if r.status_code != 200:
            raise Exception("Can't get apiurls: %d" % r.status_code)

        return r.json()

    def get_patch_emails(self, pid):
        """
        Get a set of e-mail addresses involved with the patch with specified
        ID.

        Args:
            pid:    ID of the patch to get e-mails for.

        Returns:
            A set of e-mail addresses.
        """
        emails = set()
        used_addr = list()

        r = requests.get("%s/%s" % (self.apiurls.get("patches"), pid))

        if r.status_code != 200:
            raise Exception("Failed to get data for patch %s (%d)" % (pid,
                            r.status_code))

        pdata = r.json()
        headers = pdata.get("headers")

        for header in ["From", "To", "Cc"]:
            if headers.get(header) is None:
                continue
            for faddr in [x.strip() for x in headers.get(header).split(",")]:
                logging.debug("patch=%d; header=%s; email=%s", pid, header,
                              faddr)
                maddr = re.search("\<([^\>]+)\>", faddr)
                if maddr:
                    addr = maddr.group(1)
                    if addr not in used_addr:
                        emails.add(faddr)
                        used_addr.append(addr)
                else:
                    emails.add(faddr)

        return emails

    def get_series_from_url(self, url):
        """
        Retrieve a list of info tuples of applicable (non-skipped) patchsets
        for the specified patch series URL.

        Args:
            url:    The patch series URL to retrieve patchset info tuples for.

        Returns:
            A list of patchset info tuples, each containing a list of URLs of
            patches comprising the patchset, and a set of e-mail addresses
            involved with the patchset.
        """
        patchsets = list()

        logging.debug("get_series_from_url %s", url)
        r = requests.get(url)

        if r.status_code != 200:
            raise Exception("Can't get series from url %s (%d)" % (url,
                            r.status_code))

        sdata = r.json()
        # TODO Why is this necessary?
        if type(sdata) is not list:
            sdata = [sdata]

        for series in sdata:
            plist = list()
            emails = set()

            if not series.get("received_all"):
                logging.info("skipping incomplete series: [%d] %s",
                             series.get("id"), series.get("name"))
                continue

            if self.skp.search(series.get("name")):
                logging.info("skipping series %d: %s", series.get("id"),
                             series.get("name"))
                continue

            logging.info("series: [%d] %s", series.get("id"),
                         series.get("name"))
            # For each patch in series
            for patch in series.get("patches"):
                logging.info("patch: [%d] %s", patch.get("id"),
                             patch.get("name"))
                plist.append(self.patchurl(patch))
                emails = emails.union(self.get_patch_emails(patch.get("id")))
            logging.info("---")

            if len(plist) > 0:
                patchsets.append((plist, emails))

        link = r.headers.get("Link")
        if link is not None:
            m = re.match("<(.*)>; rel=\"next\"", link)
            if m:
                nurl = m.group(1)
                # TODO Limit recursion
                patchsets += self.get_series_from_url(nurl)

        return patchsets

    def get_patchsets_from_events(self, url):
        """
        Retrieve a list of info tuples of applicable (non-skipped) patchsets
        for the specified event list URL.

        Args:
            url:    The event list URL to retrieve patchset info tuples for.

        Returns:
            A list of patchset info tuples, each containing a list of URLs of
            patches comprising the patchset, and a set of e-mail addresses
            involved with the patchset.
        """
        patchsets = list()

        logging.debug("get_patchsets_from_events: %s", url)
        r = requests.get(url)

        if r.status_code != 200:
            raise Exception("Can't get events from url %s (%d)" % (url,
                            r.status_code))

        edata = r.json()
        # TODO Why is this necessary?
        if type(edata) is not list:
            sdata = [edata]

        # For each event
        for event in edata:
            # TODO Are these the only possible events?
            series = event.get("payload", {}).get("series")
            if series is None:
                continue

            edate = dateutil.parser.parse(event.get("date"))
            if self.nsince is None or self.nsince < edate:
                self.nsince = edate

            patchsets += self.get_series_from_url(series.get("url"))

        link = r.headers.get("Link")
        if link is not None:
            m = re.match("<(.*)>; rel=\"next\"", link)
            if m:
                nurl = m.group(1)
                # TODO Limit recursion
                patchsets += self.get_patchsets_from_events(nurl)

        return patchsets

    def _set_patch_check(self, patch, payload):
        """
        Add a patch "check" payload for the specified JSON representation of a
        patch.

        Args:
            patch:      JSON representation of a patch to add the check for.
            payload:    The "check" payload dictionary to be converted to JSON.
        """
        r = requests.post(patch.get("checks"),
                          headers={"Authorization": "Token %s" % self.apikey,
                                   "Content-Type": "application/json"},
                          data=json.dumps(payload))

        if r.status_code not in [200, 201]:
            logging.warning("Failed to post patch check: %d" % r.status_code)

    def set_patch_check(self, pid, jurl, result):
        """
        Add a patch "check" for the specified patch, with the specified
        Jenkins build URL and result (sktm.tresult).

        Args:
            pid:    The ID of the patch to add the "check" for.
            jurl:   Jenkins build URL for the "check" to reference.
            result: Test result (sktm.tresult) to feature in the "check"
                    state.
        """
        if self.apikey is None:
            logging.debug("No patchwork api key provided, not setting checks")
            return

        payload = {'patch': pid,
                   'state': None,
                   'target_url': jurl,
                   'context': 'skt',
                   'description': 'skt boot test'}
        if result == sktm.tresult.SUCCESS:
            payload['state'] = int(pwresult.SUCCESS)
        elif result == sktm.tresult.BASELINE_FAILURE:
            payload['state'] = int(pwresult.WARNING)
            payload['description'] = 'Baseline failure found while testing '
            'this patch'
        else:
            payload['state'] = int(pwresult.FAILURE)
            payload['description'] = str(result)

        self._set_patch_check(self.get_patch_by_id(pid), payload)

    def get_patch_by_id(self, pid):
        """
        Retrieve a patch object by patch ID.

        Args:
            pid:    ID of the patch to retrieve.

        Returns:
            Parsed JSON object as described in
            https://patchwork-freedesktop.readthedocs.io/en/latest/rest.html#patches
        """
        r = requests.get("%s/%d" % (self.apiurls.get("patches"), pid))

        if r.status_code != 200:
            raise Exception("Can't get patch by id %d (%d)" % (pid,
                            r.status_code))

        return r.json()

    def get_patchsets_by_patch(self, url, db=None, seen=set()):
        """
        Retrieve a list of info tuples of applicable (non-skipped) patchsets,
        which contain the patch or patches available at the specified URL, and
        which weren't already tested.

        Args:
            url:    The URL pointing to a patch or a patch list to retrieve
                    the list of patch series from.
            db:     The optional database interface to retrieve "tested"
                    status for the patch series from.
            seen:   A set of IDs of patch series which should be ignored, and
                    which should have patch series IDs added once they're
                    processed.

        Returns:
            A list of patchset info tuples, each containing a list of URLs of
            patches comprising the patchset, and a set of e-mail addresses
            involved with the patchset.
        """
        patchsets = list()

        logging.debug("get_patchsets_by_patch %s", url)
        r = requests.get(url)

        if r.status_code != 200:
            raise Exception("Can't get series from url %s (%d)" % (url,
                            r.status_code))

        pdata = r.json()
        # TODO Why is this necessary?
        if type(pdata) is not list:
            pdata = [pdata]

        # For each patch
        for patch in pdata:
            # For each patch series the patch belongs to
            for series in patch.get("series"):
                sid = series.get("id")
                if (sid in seen):
                    continue
                elif (db is not None and
                      db.get_series_result(sid) is not None):
                    logging.info("skipping already tested series: [%d] %s",
                                 sid, series.get("name"))
                    continue
                else:
                    patchsets += self.get_series_from_url("%s/%d" % (
                        self.apiurls.get("series"),
                        sid
                    ))
                    seen.add(sid)

        link = r.headers.get("Link")
        if link is not None:
            m = re.match("<(.*)>; rel=\"next\"", link)
            if m:
                nurl = m.group(1)
                # TODO Limit recursion
                patchsets += self.get_patchsets_by_patch(nurl, db, seen)

        return patchsets

    # FIXME The "db" argument is unused
    def get_new_patchsets(self, db=None):
        """
        Retrieve a list of info tuples for applicable (non-skipped) patchsets
        which haven't been processed yet.

        Returns:
            A list of patchset info tuples, each containing a list of URLs of
            patches comprising the patchset, and a set of e-mail addresses
            involved with the patchset.
        """
        # TODO Figure out if adding a second is right here, since the API doc
        # at https://patchwork-freedesktop.readthedocs.io/en/latest/rest.html
        # says regarding "since" query parameter:
        #
        #   Retrieve only events newer than a specific time. Format is the
        #   same as event_time in response, an ISO 8601 date. That means that
        #   the event_time from the last seen event can be used in the next
        #   query with a since parameter to only retrieve events that haven't
        #   been seen yet.
        nsince = dateutil.parser.parse(
            self.since
        ) + datetime.timedelta(seconds=1)

        logging.debug("get_new_patchsets since %s", nsince.isoformat())
        patchsets = self.get_patchsets_by_patch("%s?project=%d&since=%s" %
                                                (self.apiurls.get("patches"),
                                                 self.projectid,
                                                 urllib.quote(
                                                     nsince.isoformat()
                                                 )))
        return patchsets

    # TODO This shouldn't really skip patches to retrieve, should it?
    def get_patchsets(self, patchlist):
        """
        Retrieve a list of info tuples of applicable (non-skipped) patchsets
        for a list of specified patch IDs.

        Args:
            patchlist:  List of patch IDs to retrieve info tuples for,
                        or skip over.

        Returns:
            A list of patchset info tuples, each containing a list of URLs of
            patches comprising the patchset, and a set of e-mail addresses
            involved with the patchset.
        """
        patchsets = list()
        seen = set()

        logging.debug("get_patchsets: %s", patchlist)
        # For each patch ID
        for pid in patchlist:
            patch = self.get_patch_by_id(pid)
            if patch is None:
                continue

            # For each series the patch belongs to
            for series in patch.get("series"):
                sid = series.get("id")
                if sid not in seen:
                    patchsets += self.get_series_from_url("%s/%d" % (
                        self.apiurls.get("series"),
                        sid
                    ))
                    seen.add(sid)

        return patchsets


class skt_patchwork(object):
    """
    A Patchwork XML RPC interface
    """
    def __init__(self, baseurl, projectname, lastpatch):
        """
        Initialize a Patchwork XML RPC interface.

        Args:
            baseurl:        Patchwork base URL.
            projectname:    Patchwork project name, or None.
            lastpatch:      Last processed patch ID.
                            Patches with this or lower ID will be ignored.
        """
        self.fields = None
        # XML RPC interface to Patchwork
        self.rpc = self.get_rpc(baseurl)
        # Base Patchwork URL
        self.baseurl = baseurl
        # ID of the project, if project name is supplied, otherwise None
        self.projectid = self.get_projectid(
            projectname
        ) if projectname else None
        # Last processed patch ID
        self.lastpatch = lastpatch
        # A regular expression matching names of the patches to skip
        self.skp = re.compile("%s" % "|".join(SKIP_PATTERNS), re.IGNORECASE)
        # TODO Describe
        self.series = dict()

    # TODO Convert this to a simple function
    @property
    def newsince(self):
        return None

    # FIXME Just move this into __init__
    def get_rpc(self, baseurl):
        """
        Create an XML RPC interface for a Patchwork base URL and initialize
        compatibility information.

        Args:
            baseurl:    Patchwork base URL to create the interface with.

        Returns:
            The XML RPC interface for the Patchwork
        """
        rpc = xmlrpclib.ServerProxy("%s/xmlrpc/" % baseurl)
        try:
            ver = rpc.pw_rpc_version()
            # check for normal patchwork1 xmlrpc version numbers
            if not (ver == [1, 3, 0] or ver == 1):
                raise Exception("Unknown xmlrpc version %s", ver)

        except xmlrpclib.Fault as err:
            if err.faultCode == 1 and \
               re.search("index out of range", err.faultString):
                # possible internal RH instance
                rpc = RpcWrapper(rpc)
                ver = rpc.pw_rpc_version()
                if ver < 1010:
                    raise Exception("Unsupported xmlrpc version %s", ver)

                # grab extra info for later parsing
                self.fields = ['id', 'name', 'submitter', 'msgid',
                               ['root_comment', ['headers']],
                               'date', 'project_id']
            else:
                raise Exception("Unknown xmlrpc fault: %s", err.faultString)

        return rpc

    # FIXME Use a verb in the name
    def patchurl(self, patch):
        """
        Format a URL for a patch object.

        Args:
            patch:  Patch object as returned by get_patch_by_id().

        Returns:
            Patch URL.
        """
        return "%s/patch/%d" % (self.baseurl, patch.get("id"))

    def log_patch(self, patch):
        """
        Log patch ID and name.

        Args:
            patch:  The patch object, as returned by XML RPC, to log ID and
                    name of.
        """
        pid = patch.get("id")
        pname = patch.get("name")

        logging.info("%d: %s", pid, pname)

    def update_patch_name(self, patch):
        """
        Set patch name in a patch XML RPC object from its e-mail Subject, for
        patches coming from internal Red Hat instance, where patch names are
        apparently unsuitable.

        Args:
            patch:  The patch XML RPC object to set the name in.

        Returns:
            The possibly modified patch XML RPC object.
        """
        if 'root_comment' in patch:
            # internal RH only: rewrite the original subject line
            e = email.message_from_string(patch['root_comment']['headers'])
            subject = e.get('Subject')
            if subject is not None:
                subject = subject.replace('\n\t', ' ').replace('\n', ' ')
            # TODO What happens when subject is None?
            patch['name'] = stringify(
                email.header.decode_header(subject)[0][0]
            )

        return patch

    def get_patch_by_id(self, pid):
        """
        Retrieve a patch object by patch ID.

        Args:
            pid:    ID of the patch to retrieve.

        Returns:
            The patch object as returned by XML RPC.
            TODO document at least the fields we care about, Patchwork is not
            likely to document the deprecated XML RPC interface for us.
        """
        if not self.fields:
            patch = self.rpc.patch_get(pid)
        else:
            # internal RH only: special hook to get original subject line
            patch = self.rpc.patch_get(pid, self.fields)

        if patch is None or patch == {}:
            logging.warning("Failed to get data for patch %d", pid)
            patch = None

        self.update_patch_name(patch)

        return patch

    def get_patch_list(self, filt):
        """
        Get a list of patch XML RPC objects, filtered according to the
        specified filter dictionary.

        Args:
            filt:   The filter dictionary structured according to the XML RPC
                    documentation.

        Returns:
            The list of patch XML RPC objects.
        """
        if not self.fields:
            patches = self.rpc.patch_list(filt)
            return patches

        # internal RH only: special hook to get original subject line
        patches = self.rpc.patch_list(filt, False, self.fields)

        # rewrite all subject lines back to original
        for patch in patches:
            self.update_patch_name(patch)

        return patches

    def get_patch_emails(self, pid):
        """
        Get a set of e-mail addresses involved with the patch with specified
        ID.

        Args:
            pid:    ID of the patch to get e-mails for.

        Returns:
            A set of e-mail addresses.
        """
        emails = set()
        used_addr = list()

        mboxdata = stringify(self.rpc.patch_get_mbox(pid))
        mbox = email.message_from_string(mboxdata)

        for header in ["From", "To", "Cc"]:
            if mbox[header] is None:
                continue
            for faddr in [x.strip() for x in mbox[header].split(",")]:
                logging.debug("patch=%d; header=%s; email=%s", pid, header,
                              faddr)
                maddr = re.search("\<([^\>]+)\>", faddr)
                if maddr:
                    addr = maddr.group(1)
                    if addr not in used_addr:
                        emails.add(faddr)
                        used_addr.append(addr)
                else:
                    emails.add(faddr)

        return emails

    def set_patch_check(self, pid, jurl, result):
        """
        Add a patch "check" for the specified patch, with the specified
        Jenkins build URL and result (sktm.tresult).

        Args:
            pid:    The ID of the patch to add the "check" for.
            jurl:   Jenkins build URL for the "check" to reference.
            result: Test result (sktm.tresult) to feature in the "check"
                    state.
        """
        # TODO: Implement this for xmlrpc
        pass

    def dump_patch(self, pid):
        """
        Output the specified patch information to stdout.

        Args:
            pid:    The ID of the patch to dump.
        """
        patch = self.get_patch_by_id(pid)
        print "pinfo=%s\n" % patch
        print "emails=%s\n" % self.get_patch_emails(pid)

    # TODO Move this to __init__ or make it a class method
    def get_projectid(self, projectname):
        """
        Retrieve ID of the project with the specified name.

        Args:
            projectname:    The name of the project to retrieve ID for.

        Returns:
            The project name.

        Raises:
            A string containing an error message, if the project with the
            specified name was not found.
        """
        plist = self.rpc.project_list(projectname)
        for project in plist:
            if project.get("linkname") == projectname:
                pid = int(project.get("id"))
                logging.debug("%s -> %d", projectname, pid)
                return pid

        raise Exception("Couldn't find project %s" % projectname)

    # FIXME This doesn't just parse a patch. Name/refactor accordingly.
    def parse_patch(self, patch):
        """
        Extract the list of patch URLs and the list of involved e-mail
        addresses from a patchset object, if it is not supposed to be skipped.
        TODO Describe the criteria.

        Args:
            patch   The patch object as returned by get_patch_by_id().

        Returns:
            None, if patch should be skipped, or a patchset info tuple,
            containing a list of URLs of patches comprising the patchset, and
            a set of e-mail addresses involved with the patchset.
        """
        pid = patch.get("id")
        pname = patch.get("name")
        result = None

        if self.skp.search(pname):
            logging.info("skipping patch %d: %s", pid, pname)
            if pid > self.lastpatch:
                self.lastpatch = pid
            return result

        emails = self.get_patch_emails(pid)

        smatch = re.search("\[.*?(\d+)/(\d+).*?\]", pname)
        if smatch:
            cpatch = int(smatch.group(1))
            mpatch = int(smatch.group(2))

            if cpatch < 1 or cpatch > mpatch:
                logging.info("skipping patch %d: %s", pid, pname)
                if pid > self.lastpatch:
                    self.lastpatch = pid
                return result

            mid = patch.get("msgid")

            mmatch = re.match("\<(\d+\W\d+)\W\d+.*@", mid)
            seriesid = None
            if mmatch:
                seriesid = mmatch.group(1)
            else:
                seriesid = "%s_%s" % (patch.get("submitter_id"), mpatch)

            if seriesid not in self.series:
                self.series[seriesid] = dict()

            if cpatch in self.series[seriesid]:
                return result

            self.series[seriesid][cpatch] = patch

            if len(self.series[seriesid].keys()) == mpatch:
                logging.info("---")
                logging.info("patchset: %s", seriesid)

                eml = set()
                patchset = list()
                for cpatch in sorted(self.series[seriesid].keys()):
                    patch = self.series[seriesid].get(cpatch)
                    self.log_patch(patch)
                    pid = patch.get("id")
                    eml = eml.union(self.get_patch_emails(pid))
                    patchset.append(self.patchurl(patch))

                logging.info("emails: %s", eml)
                logging.info("---")
                result = (patchset, eml)
        else:
            self.log_patch(patch)
            result = ([self.patchurl(patch)], emails)

        if pid > self.lastpatch:
            self.lastpatch = pid

        return result

    def get_new_patchsets(self):
        """
        Retrieve a list of info tuples for applicable (non-skipped) patchsets
        which haven't been processed yet.

        Returns:
            A list of patchset info tuples, each containing a list of URLs of
            patches comprising the patchset, and a set of e-mail addresses
            involved with the patchset.
        """
        patchsets = list()

        logging.debug("get_new_patchsets: %d", self.lastpatch)
        for patch in self.get_patch_list({'project_id': self.projectid,
                                          'id__gt': self.lastpatch}):
            pset = self.parse_patch(patch)
            if pset is not None:
                patchsets.append(pset)
        return patchsets

    # TODO This shouldn't really skip patches to retrieve, should it?
    def get_patchsets(self, patchlist):
        """
        Retrieve a list of info tuples of applicable (non-skipped) patchsets
        for a list of specified patch IDs.

        Args:
            patchlist:  List of patch IDs to retrieve info tuples for,
                        or skip over.

        Returns:
            A list of patchset info tuples, each containing a list of URLs of
            patches comprising the patchset, and a set of e-mail addresses
            involved with the patchset.
        """
        patchsets = list()

        logging.debug("get_patchsets: %s", patchlist)
        for pid in patchlist:
            patch = self.get_patch_by_id(pid)
            if patch is None:
                continue

            pset = self.parse_patch(patch)
            if pset is not None:
                patchsets.append(pset)
        return patchsets
