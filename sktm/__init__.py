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

import enum
import logging
import os
import re
import time
import sktm.db
import sktm.jenkins
import sktm.patchwork


class tresult(enum.IntEnum):
    """Test result"""
    SUCCESS = 0
    MERGE_FAILURE = 1
    BUILD_FAILURE = 2
    PUBLISH_FAILURE = 3
    TEST_FAILURE = 4
    BASELINE_FAILURE = 5


class jtype(enum.IntEnum):
    """Job type"""
    BASELINE = 0
    PATCHWORK = 1


# TODO This is no longer just a watcher. Rename/refactor/describe accordingly.
class watcher(object):
    def __init__(self, jenkinsurl, jenkinslogin, jenkinspassword,
                 jenkinsjobname, dbpath, makeopts=None):
        """
        Initialize a "watcher".

        Args:
            jenkinsurl:         Jenkins instance URL.
            jenkinslogin:       Jenkins user name.
            jenkinspassword:    Jenkins user password.
            jenkinsjobname:     Name of the Jenkins job to trigger and watch.
            dbpath:             Path to the job status database file.
            makeopts:           Extra arguments to pass to "make" when
                                building.
        """
        # FIXME Clarify/fix member variable names
        # Database instance
        self.db = sktm.db.skt_db(os.path.expanduser(dbpath))
        # Jenkins interface instance
        self.jk = sktm.jenkins.skt_jenkins(jenkinsurl, jenkinslogin,
                                           jenkinspassword)
        # Jenkins project name
        self.jobname = jenkinsjobname
        # Extra arguments to pass to "make"
        self.makeopts = makeopts
        # List of pending Jenkins builds, each one represented by a 3-tuple
        # containing:
        # * Build type (jtype)
        # * Build number
        # * Patchwork interface to get details of the tested patch from
        self.pj = list()
        # List of Patchwork interfaces
        self.pw = list()
        # True if REST-based Patchwork interfaces should be created,
        # False if XML RPC-based Patchwork interfaces should be created
        self.restapi = False

    def set_baseline(self, repo, ref="master", cfgurl=None):
        """
        Set baseline parameters.

        Args:
            repo:   Git repository URL.
            ref:    Git reference to test.
            cfgurl: Kernel configuration URL.
        """
        self.baserepo = repo
        self.baseref = ref
        self.cfgurl = cfgurl

    # FIXME The argument should not have a default
    # FIXME This function should likely not exist
    def set_restapi(self, restapi=False):
        """
        Set the type of the next added Patchwork interface.

        Args:
            restapi:    True if the next added interface will be REST-based,
                        false, if it will be XML RPC-based.
        """
        self.restapi = restapi

    def cleanup(self):
        for (pjt, bid, cpw) in self.pj:
            logging.warning("Quiting before job completion: %d/%d", bid, pjt)

    # FIXME Pass patchwork type via arguments, or pass a whole interface
    def add_pw(self, baseurl, pname, lpatch=None, apikey=None):
        """
        Add a Patchwork interface with specified parameters.
        Add an XML RPC-based interface, if self.restapi is false,
        add a REST-based interface, if self.restapi is true.

        Args:
            baseurl:        Patchwork base URL.
            pname:          Patchwork project name.
            lpatch:         Last processed patch. Patch ID, if adding an XML
                            RPC-based interface. Patch timestamp, if adding a
                            REST-based interface. Can be omitted to
                            retrieve one from the database.
            apikey:         Patchwork REST API authentication token.
        """
        if self.restapi:
            pw = sktm.patchwork.skt_patchwork2(baseurl, pname, lpatch, apikey)

            # FIXME Figure out the last patch first, then create the interface
            if lpatch is None:
                lcdate = self.db.get_last_checked_patch_date(baseurl,
                                                             pw.projectid)
                lpdate = self.db.get_last_pending_patch_date(baseurl,
                                                             pw.projectid)
                since = max(lcdate, lpdate)
                if since is None:
                    raise Exception("%s project: %s was never tested before, "
                                    "please provide initial patch id" %
                                    (baseurl, pname))
                pw.since = since
        else:
            pw = sktm.patchwork.skt_patchwork(baseurl, pname,
                                              int(lpatch) if lpatch else None)

            # FIXME Figure out the last patch first, then create the interface
            if lpatch is None:
                lcpatch = self.db.get_last_checked_patch(baseurl, pw.projectid)
                lppatch = self.db.get_last_pending_patch(baseurl, pw.projectid)
                lpatch = max(lcpatch, lppatch)
                if lpatch is None:
                    raise Exception("%s project: %s was never tested before, "
                                    "please provide initial patch id" %
                                    (baseurl, pname))
                pw.lastpatch = lpatch
        self.pw.append(pw)

    # FIXME Fix the name, this function doesn't check anything by itself
    def check_baseline(self):
        """Submit a build for baseline"""
        self.pj.append((sktm.jtype.BASELINE,
                        self.jk.build(self.jobname,
                                      baserepo=self.baserepo,
                                      ref=self.baseref,
                                      baseconfig=self.cfgurl,
                                      makeopts=self.makeopts),
                        None))

    def check_patchwork(self):
        """
        Submit and register Jenkins builds for patchsets which appeared in
        Patchwork instances after their last processed patches, and for
        patchsets which are comprised of patches added to the "pending" list
        in the database, more than 12 hours ago.
        """
        stablecommit = self.db.get_stable(self.baserepo)
        if stablecommit is None:
            raise Exception("No known stable baseline for repo %s" %
                            self.baserepo)

        logging.info("stable commit for %s is %s", self.baserepo, stablecommit)
        # For every Patchwork interface
        for cpw in self.pw:
            # Get patchset summaries for all patches the Patchwork interface
            # hasn't seen yet
            patchsets = cpw.get_new_patchsets()
            # Add patchset summaries for all patches staying pending for
            # longer than 12 hours
            patchsets += cpw.get_patchsets(
                    self.db.get_expired_pending_patches(cpw.baseurl,
                                                        cpw.projectid, 43200))
            # For each patchset summary
            for patchset in patchsets:
                # Create an empty list of patch ID and patch date tuples
                pids = list()
                # For each Patchwork patch URL in the patchset
                for purl in patchset.patch_url_list:
                    # If patch ID can be extracted from the URL
                    match = re.match(r"(.*)/patch/(\d+)$", purl)
                    if match:
                        # TODO Shouldn't we be getting this from Patchwork in
                        # the first place, when calling get_patchsets()?
                        pid = int(match.group(2))
                        patch = cpw.get_patch_by_id(pid)
                        pids.append((pid, patch.get("date").replace(" ", "T")))

                # (Re-)add the patchset's patches to the "pending" list
                self.db.set_patchset_pending(cpw.baseurl, cpw.projectid,
                                             pids)
                # Submit and remember a Jenkins build for the patchset
                self.pj.append((sktm.jtype.PATCHWORK,
                                self.jk.build(
                                    self.jobname,
                                    baserepo=self.baserepo,
                                    ref=stablecommit,
                                    baseconfig=self.cfgurl,
                                    message_id=patchset.message_id,
                                    emails=patchset.email_addr_set,
                                    patchwork=patchset.patch_url_list,
                                    makeopts=self.makeopts),
                                cpw))
                logging.info("submitted message ID: %s", patchset.message_id)
                logging.info("submitted emails: %s", patchset.email_addr_set)
                logging.info("submitted patchset: %s", patchset.patch_url_list)

    def check_pending(self):
        for (pjt, bid, cpw) in self.pj:
            if self.jk.is_build_complete(self.jobname, bid):
                logging.info("job completed: jjid=%d; type=%d", bid, pjt)
                self.pj.remove((pjt, bid, cpw))
                if pjt == sktm.jtype.BASELINE:
                    self.db.update_baseline(
                        self.baserepo,
                        self.jk.get_base_hash(self.jobname, bid),
                        self.jk.get_base_commitdate(self.jobname, bid),
                        self.jk.get_result(self.jobname, bid),
                        bid
                    )
                elif pjt == sktm.jtype.PATCHWORK:
                    patches = list()
                    slist = list()
                    series = None
                    bres = self.jk.get_result(self.jobname, bid)
                    rurl = self.jk.get_result_url(self.jobname, bid)
                    logging.info("result=%s", bres)
                    logging.info("url=%s", rurl)
                    basehash = self.jk.get_base_hash(self.jobname, bid)
                    logging.info("basehash=%s", basehash)
                    if bres == sktm.tresult.BASELINE_FAILURE:
                        self.db.update_baseline(
                            self.baserepo,
                            basehash,
                            self.jk.get_base_commitdate(self.jobname, bid),
                            sktm.tresult.TEST_FAILURE,
                            bid
                        )

                    patchset = self.jk.get_patchwork(self.jobname, bid)
                    for purl in patchset:
                        match = re.match(r"(.*)/patch/(\d+)$", purl)
                        if match:
                            baseurl = match.group(1)
                            pid = int(match.group(2))
                            patch = cpw.get_patch_by_id(pid)
                            if patch is None:
                                continue
                            logging.info("patch: [%d] %s", pid,
                                         patch.get("name"))
                            if self.restapi:
                                projid = int(patch.get("project").get("id"))
                                for series in patch.get("series"):
                                    slist.append(series.get("id"))
                            else:
                                projid = int(patch.get("project_id"))
                            patches.append((pid, patch.get("name"), purl,
                                            baseurl, projid,
                                            patch.get("date").replace(" ",
                                                                      "T")))
                            cpw.set_patch_check(pid, rurl, bres)
                        else:
                            raise Exception("Malfomed patch url: %s" % purl)

                    try:
                        series = max(set(slist), key=slist.count)
                    except ValueError:
                        pass

                    if bres != sktm.tresult.BASELINE_FAILURE:
                        self.db.commit_patchtest(self.baserepo, basehash,
                                                 patches, bres, bid, series)
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
