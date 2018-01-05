# Copyright (c) 2017 Red Hat, Inc. All rights reserved. This copyrighted material
# is made available to anyone wishing to use, modify, copy, or
# redistribute it subject to the terms and conditions of the GNU General
# Public License v.2 or later.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

import datetime
import dateutil.parser
import email
import json
import logging
import requests
import re
import xmlrpclib
import sktm

SKIP_PATTERNS = [
        "\[[^\]]*iproute.*?\]",
        "\[[^\]]*pktgen.*?\]",
        "\[[^\]]*ethtool.*?\]",
        "\[[^\]]*git.*?\]",
        "\[[^\]]*pull.*?\]",
        "pull.?request"
]

class skt_patchwork2(object):
    def __init__(self, baseurl, projectname, since, apikey = None):
        self.baseurl = baseurl
        self.since = since
        self.nsince = None
        self.apikey = apikey
        self.apiurls = self.get_apiurls()
        self.skp = re.compile("%s"  % "|".join(SKIP_PATTERNS),
                              re.IGNORECASE)
        self.project = None

        if projectname != None:
            self.project = self.get_project(projectname)

    @property
    def projectid(self):
        return self.project.get("id")

    @property
    def newsince(self):
        return self.nsince.isoformat()

    def patchurl(self, patch):
        return "%s/patch/%d" % (self.baseurl, patch.get("id"))

    def get_project(self, pname):
        r = requests.get("%s/%s" % (self.apiurls.get("projects"), pname))
        if r.status_code != 200:
            raise Exception("Can't get project data: %s %d" % (pname,
                            r.status_code))
        return r.json()

    def get_apiurls(self):
        r = requests.get("%s/api/1.0" % self.baseurl)
        if r.status_code != 200:
            raise Exception("Can't get apiurls: %d" % r.status_code)

        return r.json()

    def get_patch_emails(self, pid):
        emails = set()
        used_addr = list()

        r = requests.get("%s/%s" % (self.apiurls.get("patches"), pid))

        if r.status_code != 200:
            raise Exception("Failed to get data for patch %s (%d)" % (pid,
                            r.status_code))

        pdata = r.json()
        headers = pdata.get("headers")

        for header in ["From", "To", "Cc"]:
            if headers.get(header) == None:
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
        patchsets = list()

        logging.debug("get_series_from_url %s", url)
        r = requests.get(url)

        if r.status_code != 200:
            raise Exception("Can't get series from url %s (%d)" % (url,
                            r.status_code))

        sdata = r.json()
        if type(sdata) is not list:
            sdata = [sdata]

        for series in sdata:
            plist = list()
            emails = set()
            logging.info("series: [%d] %s", series.get("id"),
                         series.get("name"))
            for patch in series.get("patches"):
                logging.info("patch: [%d] %s", patch.get("id"),
                             patch.get("name"))
                plist.append(self.patchurl(patch))
                emails = emails.union(self.get_patch_emails(patch.get("id")))
            logging.info("---")

            if len(plist) > 0:
                patchsets.append((plist, emails))

        link = r.headers.get("Link")
        if link != None:
            m = re.match("<(.*)>; rel=\"next\"", link)
            if m:
                nurl = m.group(1)
                patchsets += self.get_series_from_url(nurl)

        return patchsets

    def get_patchsets_from_events(self, url):
        patchsets = list()

        logging.debug("get_patchsets_from_events: %s", url)
        r = requests.get(url)

        if r.status_code != 200:
            raise Exception("Can't get series from url %s (%d)" % (url,
                            r.status_code))

        edata = r.json()
        if type(edata) is not list:
            sdata = [edata]

        for event in edata:
            series = event.get("payload", {}).get("series")
            if series == None:
                continue

            edate = dateutil.parser.parse(event.get("date"))
            if self.nsince == None or self.nsince < edate:
                self.nsince = edate

            patchsets += self.get_series_from_url(series.get("url"))

        link = r.headers.get("Link")
        if link != None:
            m = re.match("<(.*)>; rel=\"next\"", link)
            if m:
                nurl = m.group(1)
                patchsets += self.get_patchsets_from_events(nurl)

        return patchsets

    def _set_patch_check(self, patch, payload):
        r = requests.post(patch.get("checks"),
                          headers = { "Authorization" : "Token %s" % self.apikey,
                                      "Content-Type"  : "application/json" },
                          data = json.dumps(payload))

        if r.status_code not in [200, 201]:
            logging.warning("Failed to post patch check: %d" % r.status_code)

    def set_patch_check(self, pid, jurl, result):
        if self.apikey == None:
            logging.debug("No patchwork api key provided, not setting checks")
            return

        payload = { 'patch' : pid,
                    'state' : None,
                    'target_url' : jurl,
                    'context' : 'skt',
                    'description' : 'skt boot test' }
        if result == sktm.tresult.SUCCESS:
            payload['state'] = 'success'
        elif result == sktm.tresult.BASELINE_FAILURE:
            payload['state'] = 'warning'
            payload['description'] = 'Baseline failure found while testing this patch'
        else:
            payload['state'] = 'fail'
            payload['description'] = str(result)

        self._set_patch_check(self.get_patch_by_id(pid), payload)

    def get_patch_by_id(self, pid):
        r = requests.get("%s/%d" % (self.apiurls.get("patches"), pid))

        if r.status_code != 200:
            raise Exception("Can't get patch by id %d (%d)" % (pid,
                            r.status_code))

        return r.json()

    def get_new_patchsets(self):
        nsince = dateutil.parser.parse(self.since) + \
                  datetime.timedelta(seconds=1)

        logging.debug("get_new_patchsets since %s", nsince.isoformat())
        patchsets = self.get_patchsets_from_events(
                         "%s?project=%d&category=series-completed&since=%s" %
                         (self.apiurls.get("events"),
                          self.projectid,
                          nsince.isoformat()))
        return patchsets

    def get_patchsets(self, patchlist):
        patchsets = list()
        seen = set()

        logging.debug("get_patchsets: %s", patchlist)
        for pid in patchlist:
            patch = self.get_patch_by_id(pid)
            if patch == None:
                continue

            for series in patch.get("series"):
                sid = series.get("id")
                if sid not in seen:
                    patchsets += self.get_series_from_url("%s/%d" %
                                                  (self.apiurls.get("series"),
                                                  sid))
                    seen.add(sid)

        return patchsets

class skt_patchwork(object):
    def __init__(self, baseurl, projectname, lastpatch):
        self.rpc = xmlrpclib.ServerProxy("%s/xmlrpc/" % baseurl)
        self.baseurl = baseurl
        self.projectid = self.get_projectid(projectname) if projectname else None
        self.lastpatch = lastpatch
        self.skp = re.compile("%s"  % "|".join(SKIP_PATTERNS),
                              re.IGNORECASE)
        self.series = dict()

    def patchurl(self, patch):
        return "%s/patch/%d" % (self.baseurl, patch.get("id"))

    def log_patch(self, patch):
        pid = patch.get("id")
        pname = patch.get("name")

        logging.info("%d: %s", pid, pname)

    def get_patch_by_id(self, pid):
        patch = self.rpc.patch_get(pid)

        if patch == None or patch == {}:
            logging.warning("Failed to get data for patch %d", pid)
            patch = None

        return patch

    def get_patch_emails(self, pid):
        emails = set()
        used_addr = list()

        mboxdata = self.rpc.patch_get_mbox(pid)
        mbox = email.message_from_string(mboxdata.encode('utf-8'))

        for header in ["From", "To", "Cc"]:
            if mbox[header] == None:
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
        # TODO: Implement this for xmlrpc
        pass

    def dump_patch(self, pid):
        patch = self.get_patch_by_id(pid)
        print "pinfo=%s\n" % patch
        print "emails=%s\n" % self.get_patch_emails(pid)

    def get_projectid(self, projectname):
        plist = self.rpc.project_list(projectname)
        for project in plist:
            if project.get("linkname") == projectname:
                pid = project.get("id")
                logging.debug("%s -> %d", projectname, pid)
                return pid

        raise Exception("Couldn't find project %s" % projectname)

    def parse_patch(self, patch):
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
                self.log_patch(patch)
                result = ([self.patchurl(patch)], emails)
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
        patchsets = list()

        logging.debug("get_new_patchsets: %d", self.lastpatch)
        for patch in self.rpc.patch_list({'project_id' : self.projectid,
                                          'id__gt': self.lastpatch}):
            pset = self.parse_patch(patch)
            if pset != None:
                patchsets.append(pset)
        return patchsets

    def get_patchsets(self, patchlist):
        patchsets = list()

        logging.debug("get_patchsets: %s", patchlist)
        for pid in patchlist:
            patch = self.get_patch_by_id(pid)
            if patch == None:
                continue

            pset = self.parse_patch(patch)
            if pset != None:
                patchsets.append(pset)
        return patchsets
