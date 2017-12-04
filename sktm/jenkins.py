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

import jenkinsapi
import json
import logging
import time
import sktm

class skt_jenkins(object):
    def __init__(self, url, username = None, password = None):
        self.server = jenkinsapi.jenkins.Jenkins(url, username, password)

    def _wait_and_get_build(self, jobname, buildid):
        job = self.server.get_job(jobname)
        build = job.get_build(buildid)
        build.block_until_complete(delay=60)

        # call get_build again to ensure we have the results
        build = job.get_build(buildid)

        return build

    def get_cfg_data(self, jobname, buildid, stepname, cfgkey):
        build = self._wait_and_get_build(jobname, buildid)

        if not build.has_resultset():
            raise Exception("No results for build %d (%s)" % (buildid,
                            build.get_status()))

        for (key, val) in build.get_resultset().iteritems():
            if key == stepname:
                logging.debug("stdout=%s", val.stdout)
                cfg = json.loads(val.stdout)
                return cfg.get(cfgkey)

    def get_base_commitdate(self, jobname, buildid):
        return self.get_cfg_data(jobname, buildid, "skt.cmd_merge",
                                 "commitdate")

    def get_base_hash(self, jobname, buildid):
        return self.get_cfg_data(jobname, buildid, "skt.cmd_merge",
                                 "basehead")

    def get_patchwork(self, jobname, buildid):
        return self.get_cfg_data(jobname, buildid, "skt.cmd_merge",
                                 "pw")

    def get_baseretcode(self, jobname, buildid):
        return self.get_cfg_data(jobname, buildid, "skt.cmd_run",
                                 "baseretcode")

    def get_result(self, jobname, buildid):
        build = self._wait_and_get_build(jobname, buildid)

        bstatus = build.get_status()
        logging.info("build_status=%s", bstatus)

        if bstatus == "SUCCESS":
            return sktm.tresult.SUCCESS

        if not build.has_resultset():
            raise Exception("No results for build %d (%s)" % (buildid,
                            build.get_status()))

        if bstatus == "UNSTABLE" and \
                build.get_resultset()["skt.cmd_run"].status == "PASSED":
            if self.get_baseretcode(jobname, buildid) != 0:
                logging.warning("baseline failure found during patch testing")
                return sktm.tresult.BASELINE_FAILURE

        for (key, val) in build.get_resultset().iteritems():
            if not key.startswith("skt."):
                logging.debug("skipping key=%s; value=%s", key, val.status)
                continue
            logging.debug("key=%s; value=%s", key, val.status)
            if val.status == "FAILED" or val.status == "REGRESSION":
                if key == "skt.cmd_merge":
                    return sktm.tresult.MERGE_FAILURE
                elif key == "skt.cmd_build":
                    return sktm.tresult.BUILD_FAILURE
                elif key == "skt.cmd_run":
                    return sktm.tresult.TEST_FAILURE

        logging.warning("Unknown status. marking as test failure")
        return sktm.tresult.TEST_FAILURE

    def build(self, jobname, baserepo = None, ref = None, baseconfig = None,
              patchwork = [], emails = set(), makeopts = None):
        params = dict()
        if baserepo != None:
            params["baserepo"] = baserepo

        if ref != None:
            params["ref"] = ref

        if baseconfig != None:
            params["baseconfig"] = baseconfig

        if makeopts != None:
            params["makeopts"] = makeopts

        if len(patchwork) > 0:
            params["patchwork"] = " ".join(patchwork)

        if len(emails) > 0:
            params["emails"] = ",".join(emails)

        logging.debug(params)
        job = self.server.get_job(jobname)
        expected_id = self.server.get_job(jobname).get_next_build_number()
        self.server.build_job(jobname, params)
        build = self.find_build(jobname, params, expected_id)
        logging.info("submitted build: %s", build)
        return build.get_number()

    def is_build_complete(self, jobname, buildid):
        job = self.server.get_job(jobname)
        build = job.get_build(buildid)

        return not build.is_running()

    def _params_eq(self, build, params):
        result = True
        if build == None or build.get_actions() == None or \
                build.get_actions().get("parameters") == None:
            return False

        for param in build.get_actions().get("parameters"):
            if param.get("name") in params:
                if param.get("value") != params.get(param.get("name")):
                    result = False
                    break

        return result

    def find_build(self, jobname, params, eid = None):
        job = self.server.get_job(jobname)

        try:
            lbuild = job.get_last_build()
        except jenkinsapi.custom_exceptions.NoBuildData:
            lbuild = None

        while lbuild == None:
            time.sleep(1)
            try:
                lbuild = job.get_last_build()
            except jenkinsapi.custom_exceptions.NoBuildData:
                lbuild = None

        if eid != None:
            while lbuild.get_number() < eid:
                time.sleep(1)
                lbuild = job.get_last_build()
        if self._params_eq(lbuild, params):
            return lbuild

        # slowpath
        for bid in job.get_build_ids():
            build = job.get_build(bid)
            if self._params_eq(build, params):
                return build
        return None
