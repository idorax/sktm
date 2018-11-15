# Copyright (c) 2018 Red Hat, Inc. All rights reserved. This copyrighted
# material is made available to anyone wishing to use, modify, copy, or
# redistribute it subject to the terms and conditions of the GNU General Public
# License v.2 or later.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
"""Tests for the __init__.py."""
import tempfile
import unittest

import mock
from mock import Mock

import sktm


class TestInit(unittest.TestCase):
    """Test cases for the __init__ module."""

    @mock.patch('sktm.jenkins.JenkinsProject', Mock())
    def setUp(self):
        """Test fixtures for testing __init__."""
        self.database_dir = tempfile.mkdtemp()
        self.database_file = "{}/testdb.sqlite".format(self.database_dir)

        jenkins_project = sktm.jenkins.JenkinsProject(
            name="sktm_jenkins_job",
            url="http://example.com/jenkins",
            username="username",
            password="password"
        )

        self.watcher_obj = sktm.watcher(
            jenkins_project,
            dbpath=self.database_file,
            patch_filter=None,
            makeopts=None
        )

    @mock.patch('logging.warning')
    def test_cleanup(self, mock_logger):
        """Ensure cleanup() logs a warning."""
        self.watcher_obj.pj = [(1, 2, 3)]
        self.watcher_obj.cleanup()
        mock_logger.assert_called_with(
            "Quiting before job completion: %d/%d", 2, 1
        )

    def test_set_baseline(self):
        """Ensure set_baseline() sets variables properly."""
        baserepo = 'git://example.com/repo'
        baseref = 'master'
        cfgurl = "http://example.com/config.txt"

        self.watcher_obj.set_baseline(
            repo=baserepo,
            ref=baseref,
            cfgurl=cfgurl
        )
        self.assertEqual(self.watcher_obj.baserepo, baserepo)
        self.assertEqual(self.watcher_obj.baseref, baseref)
        self.assertEqual(self.watcher_obj.cfgurl, cfgurl)

    @mock.patch('sktm.watcher.check_pending', Mock(return_value=True))
    @mock.patch('logging.info')
    def test_wait_for_pending_done(self, mock_logging):
        """Ensure wait_for_pending() logs a message when jobs are complete."""
        self.watcher_obj.wait_for_pending()
        mock_logging.assert_called_with('no more pending jobs')

    def test_get_commit_hash(self):
        """Ensure get_commit_hash gets always a git commit hash"""
        expected_hash = '123deadc0de321'
        commit_hash = self.watcher_obj.get_commit_hash('url', expected_hash)
        self.assertEqual(expected_hash, commit_hash)
        deadbeaf = 'deadbeaf' * 5
        expected_ls_remote = '{} master'.format(deadbeaf)
        mock_check_output = Mock(return_value=expected_ls_remote)
        with mock.patch('subprocess.check_output', mock_check_output):
            master_hash = self.watcher_obj.get_commit_hash('url', 'master')
            self.assertEqual(deadbeaf, master_hash)

    @mock.patch('logging.info')
    def test_check_baseline(self, mock_logging):
        """
        Ensure enqueue_baseline_job only enqueues a new job if it wasn't
        checked already and check if the job is enqueued when force option is
        set.
        """
        baserepo = 'git://example.com/repo'
        baseref = 'master'
        cfgurl = "http://example.com/config.txt"

        self.watcher_obj.set_baseline(
            repo=baserepo,
            ref=baseref,
            cfgurl=cfgurl
        )

        self.watcher_obj.get_commit_hash = Mock(return_value='c0de4bee4')
        self.watcher_obj.enqueue_baseline_job()
        self.watcher_obj.jk.build.assert_called_with(
            baseconfig=cfgurl,
            baserepo=baserepo,
            makeopts=None,
            ref='c0de4bee4',
        )

        self.watcher_obj.get_commit_hash = Mock(return_value='deadcode')
        self.watcher_obj.db.get_last_checked_baseline = Mock(
            return_value='deadcode'
        )
        self.watcher_obj.enqueue_baseline_job()
        mock_logging.assert_called_with('Baseline %s@%s [%s] already tested',
                                        baserepo, baseref, 'deadcode')

        self.watcher_obj.jk.build.reset_mock()
        self.watcher_obj.set_baseline(
            repo=baserepo,
            ref=baseref,
            cfgurl=cfgurl,
            force=True,
        )
        self.watcher_obj.enqueue_baseline_job()
        self.watcher_obj.jk.build.assert_called_with(
            baseconfig=cfgurl,
            baserepo=baserepo,
            makeopts=None,
            ref='deadcode',
        )
