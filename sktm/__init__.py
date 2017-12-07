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

import enum
import logging
import os
import re
import time
import sktm.db
import sktm.jenkins
import sktm.patchwork

class tresult(enum.IntEnum):
    SUCCESS = 0
    MERGE_FAILURE = 1
    BUILD_FAILURE = 2
    PUBLISH_FAILURE = 3
    TEST_FAILURE = 4
    BASELINE_FAILURE = 5

class jtype(enum.IntEnum):
    BASELINE = 0
    PATCHWORK = 1

class watcher(object):
    def __init__(self, jenkinsurl, jenkinslogin, jenkinspassword,
                 jenkinsjobname, dbpath, makeopts = None):
        self.db = sktm.db.skt_db(os.path.expanduser(dbpath))
        self.jk = sktm.jenkins.skt_jenkins(jenkinsurl, jenkinslogin,
                                           jenkinspassword)
        self.jobname = jenkinsjobname
        self.makeopts = makeopts
        self.pj = list()
        self.pw = list()

    def set_baseline(self, repo, ref = "master", cfgurl = None):
        self.baserepo = repo
        self.baseref = ref
        self.cfgurl = cfgurl

    def cleanup(self):
        for (pjt, bid) in self.pj:
            logging.warning("Quiting before job completion: %d/%d", bid, pjt)

    def add_pw(self, baseurl, pname, lpatch = None):
        pw = sktm.patchwork.skt_patchwork(baseurl, pname,
                                          int(lpatch) if lpatch else None)

        if lpatch == None:
            lcpatch = self.db.get_last_checked_patch(baseurl, pw.projectid)
            lppatch = self.db.get_last_pending_patch(baseurl, pw.projectid)
            lpatch = max(lcpatch, lppatch)
            if lpatch == None:
                raise Exception("%s project: %s was never tested before, please provide initial patch id" %
                                (baseurl, pid))
            pw.lastpatch = lpatch
        self.pw.append(pw)

    def check_baseline(self):
        self.pj.append((sktm.jtype.BASELINE,
                        self.jk.build(self.jobname,
                                      baserepo = self.baserepo,
                                      ref = self.baseref,
                                      baseconfig = self.cfgurl,
                                      makeopts = self.makeopts)))

    def check_patchwork(self):
        stablecommit = self.db.get_stable(self.baserepo)
        if stablecommit == None:
            raise Exception("No known stable baseline for repo %s" %
                    self.baserepo)

        logging.info("stable commit for %s is %s", self.baserepo, stablecommit)
        for cpw in self.pw:
            patchsets = cpw.get_new_patchsets()
            patchsets += cpw.get_patchsets(
                    self.db.get_expired_pending_patches(cpw.baseurl,
                                                        cpw.projectid, 43200))
            for (patchset, emails) in patchsets:
                pids = list()
                for purl in patchset:
                    match = re.match("(.*)/patch/(\d+)$", purl)
                    if match:
                        pids.append(int(match.group(2)))

                self.db.set_patchset_pending(cpw.baseurl, cpw.projectid,
                                             pids)
                self.pj.append((sktm.jtype.PATCHWORK,
                                self.jk.build(self.jobname,
                                              baserepo = self.baserepo,
                                              ref = stablecommit,
                                              baseconfig = self.cfgurl,
                                              patchwork = patchset,
                                              emails = emails,
                                              makeopts = self.makeopts)))
                logging.info("submitted patchset: %s", patchset)
                logging.debug("emails: %s", emails)

    def check_pending(self):
        for (pjt, bid) in self.pj:
            if self.jk.is_build_complete(self.jobname, bid):
                logging.info("job completed: %d/%d", bid, pjt)
                self.pj.remove((pjt, bid))
                if pjt == sktm.jtype.BASELINE:
                    self.db.update_baseline(self.baserepo,
                            self.jk.get_base_hash(self.jobname, bid),
                            self.jk.get_base_commitdate(self.jobname, bid),
                            self.jk.get_result(self.jobname, bid),
                            bid)
                elif pjt == sktm.jtype.PATCHWORK:
                    patches = list()
                    bres = self.jk.get_result(self.jobname, bid)
                    logging.info("result=%s", bres)
                    basehash = self.jk.get_base_hash(self.jobname, bid)
                    logging.info("basehash=%s", basehash)
                    if bres == sktm.tresult.BASELINE_FAILURE:
                        self.db.update_baseline(self.baserepo,
                                basehash,
                                self.jk.get_base_commitdate(self.jobname, bid),
                                sktm.tresult.TEST_FAILURE,
                                bid)
                        continue

                    patchset = self.jk.get_patchwork(self.jobname, bid)
                    for purl in patchset:
                        match = re.match("(.*)/patch/(\d+)$", purl)
                        if match:
                            baseurl = match.group(1)
                            pid = int(match.group(2))
                            pw = sktm.patchwork.skt_patchwork(baseurl, None,
                                                              pid)
                            patch = pw.get_patch_by_id(pid)
                            if patch == None:
                                continue
                            logging.info("patch=%s", patch)
                            patches.append((pid, patch.get("name"), purl,
                                            baseurl, patch.get("project_id"),
                                            patch.get("date").replace(" ", "T")))
                        else:
                            raise Exception("Malfomed patch url: %s" % purl)

                    self.db.commit_patchtest(self.baserepo, basehash, patches,
                                             bres, bid)
                else:
                    raise Exception("Unknown job type: %d" % pjt)

    def wait_for_pending(self):
        self.check_pending()
        while len(self.pj) > 0:
            logging.debug("waiting for jobs to complete. %d remaining",
                          len(self.pj))
            time.sleep(60)
            self.check_pending()
        logging.info("no more pending jobs")
