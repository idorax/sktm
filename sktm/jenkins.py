# Copyright (c) 2017-2018 Red Hat, Inc. All rights reserved. This copyrighted
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

import json
import logging
import time

import jenkinsapi

from sktm.misc import TestResult, join_with_slash


class JenkinsProject(object):
    """Jenkins project interface"""
    def __init__(self, name, url, username=None, password=None,
                 retry_cnt=None):
        """
        Initialize a Jenkins project interface.

        Args:
            name:        Name of the Jenkins project to operate on.
            url:         Jenkins instance URL.
            username:    Jenkins user name.
            password:    Jenkins user password.
            retry_cnt:   Counter to retry Jenkins in case of temporary network
                         failures.
        """
        if not name:
            raise ValueError('No Jenkins job name specified!')

        self.name = name

        # Initialize Jenkins server interface
        # TODO Add support for CSRF protection
        self.server = jenkinsapi.jenkins.Jenkins(url, username, password)

        self.retry_cnt = retry_cnt

    def __call_server_method(self, method, interval, *args):
        """
        Call method of self.server. Retry Jenkins self.retry_cnt times
        in case of temporary network failures.

        Args:
            method:     Method of self.server.
            interval:   Seconds to sleep before retrying.
            *args:      The args of method of self.server.

        Return:
            what the method of self.server return if succeed, else raise the
            last exception after retry self.retry_cnt times. Note it will
            directly raise AttributeError if the method is invalid.
        """
        func = getattr(self.server, method)

        for i in range(self.retry_cnt):
            try:
                return func(*args)
            except Exception as e:
                logging.warning("catch %s: %s" % (type(e), e))
                logging.info("now sleep %ds and try again" % interval)
                time.sleep(interval)

        logging.error("fail to %s after retry %d times" %
                      (method.replace('_', ' '), self.retry_cnt))
        raise e

    def __get_job(self, interval=60):
        return self.__call_server_method(self, "get_job", interval, self.name)

    def __build_job(self, params, interval=60):
        self.__call_server_method(self, "build_job", interval,
                                  self.name, params)

    def __base_server_url(self, interval=60):
        return self.__call_server_method(self, "base_server_url", interval)

    def __get_job_prop(self, job, method, interval, *args):
        """
        Get property of Jenkins job. Retry Jenkins self.retry_cnt times
        in case of temporary network failures.

        Args:
            job:        Jenkins job.
            method:     Method of job.
            interval:   Seconds to sleep before retrying.
            *args:      The args of method of job.

        Return:
            job property if succeed, else raise the last exception after retry
            self.retry_cnt times. Note it will directly raise AttributeError
            if the method is invalid.
        """
        func = getattr(job, method)

        for i in range(self.retry_cnt):
            try:
                return func(*args)
            except Exception as e:
                logging.warning("catch %s: %s" % (type(e), e))
                logging.info("now sleep %ds and try again" % interval)
                time.sleep(interval)

        logging.error("fail to %s after retry %d times" %
                      (method.replace('_', ' '), self.retry_cnt))
        raise e

    def __get_build(self, job, buildid, interval=60):
        return self.__get_job_prop(self, job, "get_build", interval, buildid)

    def __get_build_ids(self, job, interval=60):
        return self.__get_job_prop(self, job, "get_build_ids", interval)

    def __get_last_build(self, job, interval=60):
        return self.__get_job_prop(self, job, "get_last_build", interval)

    def __get_next_build_number(self, job, interval=60):
        return self.__get_job_prop(self, job, "get_next_build_number",
                                   interval)

    def _wait_and_get_build(self, buildid):
        job = self.__get_job()
        build = self.__get_build(job, buildid)
        build.block_until_complete(delay=60)

        # call self.__get_build() again to ensure we have the results
        build = self.__get_build(job, buildid)

        return build

    def __get_data_list(self, buildid, stepname, key):
        """
        Get a list of values of a build resultset key, for all steps matching
        the specified name, of the specified completed build. Wait for the
        build to complete, if it hasn't yet.

        Args:
            buildid:    Jenkins build ID.
            stepname:   A path matching test steps in the result, which
                        resultset should be accessed.
            key:        Name of the resultset key to retrieve value of.

        Returns:
            The list of key values.
        """
        value_list = []
        build = self._wait_and_get_build(buildid)

        if not build.has_resultset():
            raise Exception("No results for build %d (%s)" %
                            (buildid, build.get_status()))

        for (result_key, value) in build.get_resultset().iteritems():
            if result_key == stepname:
                value_list.append(value.__dict__[key])

        return value_list

    def __get_cfg_data_list(self, buildid, stepname,
                            cfgkey, default=None):
        """
        Get a list of values from a JSON-formatted output of a test result,
        for all steps matching the specified name, of the specified completed
        build. Wait for the build to complete, if it hasn't yet.

        Args:
            buildid:    Jenkins build ID.
            stepname:   A path matching test steps in the result, which output
                        should be parsed as JSON.
            cfgkey:     Name of the JSON key to retrieve value of.
            default:    The default value to use if a key is not found, for
                        each matching step. Optional, assumed None, if not
                        specified.

        Returns:
            The list of key values, with defaults for steps where they were
            not found.
        """
        value_list = []
        for stdout in self.__get_data_list(buildid, stepname, "stdout"):
            logging.debug("stdout=%s", stdout)
            cfg = json.loads(stdout)
            value_list.append(cfg.get(cfgkey, default))

        return value_list

    def __get_cfg_data_uniform(self, buildid, stepname,
                               cfgkey, default=None):
        """
        Get a uniform value from a JSON-formatted output of a test result,
        for all steps matching the specified name, of the specified completed
        build. Wait for the build to complete, if it hasn't yet. Throw an
        exception if the value is not uniform across all steps.

        Args:
            buildid:    Jenkins build ID.
            stepname:   A path matching test steps in the result, which output
                        should be parsed as JSON.
            cfgkey:     Name of the JSON key to retrieve value of.
            default:    The default value to use if a key is not found, for
                        each matching step. Optional, assumed None, if not
                        specified.

        Returns:
            The value uniform for the key across all steps.
        """
        def verify(x, y):
            if x != y:
                raise Exception("Non-uniform value of key %s: %s != %s",
                                cfgkey, x, y)
            return x

        return reduce(verify,
                      self.__get_cfg_data_list(buildid, stepname,
                                               cfgkey, default))

    def __get_cfg_data_max(self, buildid, stepname,
                           cfgkey, default=None):
        """
        Get the maximum value from a JSON-formatted output of a test result,
        for all steps matching the specified name, of the specified completed
        build. Wait for the build to complete, if it hasn't yet.

        Args:
            buildid:    Jenkins build ID.
            stepname:   A path matching test steps in the result, which output
                        should be parsed as JSON.
            cfgkey:     Name of the JSON key to retrieve value of.
            default:    The default value to use if a key is not found, for
                        each matching step. Optional, assumed None, if not
                        specified.

        Returns:
            The maximum value of the key across all steps.
        """
        return reduce(lambda x, y: (x if x > y else y),
                      self.__get_cfg_data_list(buildid, stepname,
                                               cfgkey, default))

    def get_base_commitdate(self, buildid):
        """
        Get base commit's committer date of the specified completed build.
        Wait for the build to complete, if it hasn't yet.

        Args:
            buildid:    Jenkins build ID.

        Return:
            The epoch timestamp string of the committer date.
        """
        return self.__get_cfg_data_uniform(buildid, "skt.cmd_merge",
                                           "commitdate")

    def get_base_hash(self, buildid):
        """
        Get base commit's hash of the specified completed build.
        Wait for the build to complete, if it hasn't yet.

        Args:
            buildid:    Jenkins build ID.

        Return:
            The base commit's hash string.
        """
        return self.__get_cfg_data_uniform(buildid, "skt.cmd_merge",
                                           "basehead")

    def get_patch_url_list(self, buildid):
        """
        Get the list of Patchwork patch URLs for the specified completed
        build. Wait for the build to complete, if it hasn't yet.

        Args:
            buildid:    Jenkins build ID.

        Return:
            The list of Patchwork patch URLs, in the order the patches should
            be applied in.
        """
        return self.__get_cfg_data_uniform(buildid, "skt.cmd_merge", "pw")

    def get_baseretcode(self, buildid):
        """
        Get the maximum (the worst) return code of a baseline test across all
        "run" steps for the specified completed build of the specified
        project. Wait for the build to complete, if it hasn't yet.
        """
        return self.__get_cfg_data_max(buildid, "skt.cmd_run",
                                       "baseretcode", 0)

    def get_result_url(self, buildid):
        """
        Get the URL of the web representation of the specified build of the
        specified Jenkins project.

        Args:
            jobname:    Jenkins project name.
            buildid:    Jenkins build ID.

        Result:
            The URL of the build result.
        """
        return join_with_slash(self.__base_server_url(), "job", str(buildid))

    def get_result(self, buildid):
        """
        Get result code (TestResult) for the specified build of the
        specified Jenkins project. Wait for the build to complete, if it
        hasn't yet.

        Args:
            buildid:    Jenkins build ID.

        Return:
            The build result code (TestResult).
        """
        build = self._wait_and_get_build(buildid)

        bstatus = build.get_status()
        logging.info("build_status=%s", bstatus)

        if bstatus == "SUCCESS":
            return TestResult.SUCCESS
        elif bstatus == "UNSTABLE":
            # Find earliest (worst) step failure
            step_failure_result_list = [
                ("skt.cmd_merge", TestResult.MERGE_FAILURE),
                ("skt.cmd_build", TestResult.BUILD_FAILURE),
                ("skt.cmd_run", TestResult.TEST_FAILURE),
            ]
            for (step, failure_result) in step_failure_result_list:
                if set(self.__get_data_list(buildid, step, "status")) & \
                        set(["FAILED", "REGRESSION"]):
                    return failure_result
            logging.warning("Build status is \"%s\", "
                            "but no failed steps found, reporting as error",
                            bstatus)
        else:
            logging.warning("Reporting build status \"%s\" as error", bstatus)
        return TestResult.ERROR

    # FIXME Clarify/fix argument names
    def build(self, baserepo=None, ref=None, baseconfig=None,
              message_id=None, subject=None, emails=set(), patch_url_list=[],
              makeopts=None):
        """
        Submit a build of a patch series.

        Args:
            baserepo:        Baseline Git repo URL.
            ref:             Baseline Git reference to test.
            baseconfig:      Kernel configuration URL.
            message_id:      Value of the "Message-Id" header of the e-mail
                             message representing the series, or None if
                             unknown.
            subject:         Subject of the message representing the series,
                             or None if unknown.
            emails:          Set of e-mail addresses involved with the series
                             to send notifications to.
            patch_url_list:  List of URLs pointing to patches to apply, in the
                             order they should be applied in.
            makeopts:        String of extra arguments to pass to the build's
                             make invocation.

        Returns:
            Submitted build number.
        """
        params = dict()
        if baserepo is not None:
            params["baserepo"] = baserepo

        if ref is not None:
            params["ref"] = ref

        if baseconfig is not None:
            params["baseconfig"] = baseconfig

        if message_id:
            params["message_id"] = message_id

        if subject:
            params["subject"] = subject

        if emails:
            params["emails"] = ",".join(emails)

        if patch_url_list:
            params["patchwork"] = " ".join(patch_url_list)

        if makeopts is not None:
            params["makeopts"] = makeopts

        logging.debug(params)
        job = self.__get_job()
        expected_id = self.__get_next_build_number(job)
        self.__build_job(params)
        build = self.find_build(params, expected_id)
        logging.info("submitted build: %s", build)
        return build.get_number()

    def is_build_complete(self, buildid):
        """
        Check if a build is complete.

        Args:
            buildid:    Jenkins build ID to get the status of.

        Return:
            True if the build is complete, False if not.
        """
        job = self.__get_job()
        build = self.__get_build(job, buildid)

        return not build.is_running()

    def _params_eq(self, build, params):
        try:
            build_params = build.get_actions()["parameters"]
        except (AttributeError, KeyError):
            return False

        for build_param in build_params:
            if (build_param["name"] in params
                    and build_param["value"] != params[build_param["name"]]):
                return False

        return True

    def find_build(self, params, eid=None):
        job = self.__get_job()
        lbuild = None

        while not lbuild:
            try:
                lbuild = job.get_last_build()
            except jenkinsapi.custom_exceptions.NoBuildData:
                time.sleep(1)

        if eid is not None:
            while lbuild.get_number() < eid:
                time.sleep(1)
                lbuild = self.__get_last_build(job, 10)
        if self._params_eq(lbuild, params):
            return lbuild

        # slowpath
        for bid in self.__get_build_ids(job):
            build = self.__get_build(job, bid)
            if self._params_eq(build, params):
                return build
        return None
