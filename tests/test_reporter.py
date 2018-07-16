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
"""Tests for the reporter.py."""
import os
import shutil
import tempfile
import unittest

import mock

from sktm.executable import DEFAULT_REPORT_INTRO, DEFAULT_REPORT_FOOTER
from sktm import reporter


class TestReporter(unittest.TestCase):
    """Test cases for the reporter module."""

    def test_empty_dir(self):
        """
        Make sure the reporter raises an Exception if the assets directory is
        empty.
        """
        parent_dir = tempfile.mkdtemp()
        report = reporter.MailReporter(parent_dir,
                                       'email@from',
                                       ['email@to'],
                                       DEFAULT_REPORT_INTRO,
                                       DEFAULT_REPORT_FOOTER)
        with self.assertRaisesRegexp(Exception, 'No merge results found in'):
            report.create_report()

        shutil.rmtree(parent_dir)

    @mock.patch('logging.info')
    def test_standalone_merge_results(self, mock_logging):
        """Make sure the reporter reports standalone merge results."""
        parent_dir = tempfile.mkdtemp()
        with open(os.path.join(parent_dir, 'merge.result'),
                  'w') as merge_result_file:
            merge_result_file.write('true')
        with open(os.path.join(parent_dir, 'merge.report'),
                  'w') as merge_report_file:
            merge_report_file.write('this is a report for merge stage')

        report = reporter.MailReporter(parent_dir,
                                       'email@from',
                                       ['email@to'],
                                       DEFAULT_REPORT_INTRO,
                                       DEFAULT_REPORT_FOOTER)
        report.create_report()

        self.assertIn('this is a report for merge stage',
                      report.report.as_string())
        mock_logging.assert_called_once()

        shutil.rmtree(parent_dir)

    @mock.patch('logging.info')
    def test_results_with_reference(self, mock_logging):
        """
        Make sure the reporter replaces the placeholder filenames in results
        and attaches the referenced files.
        """
        parent_dir = tempfile.mkdtemp()
        with open(os.path.join(parent_dir, 'merge.result'),
                  'w') as merge_result_file:
            merge_result_file.write('false')
        with open(os.path.join(parent_dir, 'merge.report'),
                  'w') as merge_report_file:
            merge_report_file.write('this is a report referencing {merge.log}')
        with open(os.path.join(parent_dir, 'merge.log'),
                  'w') as merge_log:
            merge_log.write('look at the fancy log!')

        report = reporter.MailReporter(parent_dir,
                                       'email@from',
                                       ['email@to'],
                                       DEFAULT_REPORT_INTRO,
                                       DEFAULT_REPORT_FOOTER)
        report.create_report()

        self.assertIn('this is a report referencing merge.log',
                      report.report.as_string())
        self.assertIn('Merge FAILED', report.report.as_string())
        self.assertIn('Content-Type: text/plain; charset="utf-8"',
                      report.report.as_string())
        self.assertIn('content-disposition: attachment; filename="merge.log"',
                      report.report.as_string())
        mock_logging.assert_called_once()

        shutil.rmtree(parent_dir)

    @mock.patch('logging.info')
    @mock.patch('logging.debug')
    def test_failed_build(self, mock_debug, mock_info):
        """
        Make sure the reporter doesn't replace strings enclosed in escaped
        curly brackets and correctly reports build results without attachments.
        """
        parent_dir = tempfile.mkdtemp()
        with open(os.path.join(parent_dir, 'merge.result'),
                  'w') as merge_result_file:
            merge_result_file.write('true')
        with open(os.path.join(parent_dir, 'merge.report'),
                  'w') as merge_report_file:
            merge_report_file.write('merge report')
        with open(os.path.join(parent_dir, 'build.result'),
                  'w') as build_result_file:
            build_result_file.write('false')
        with open(os.path.join(parent_dir, 'build.report'),
                  'w') as build_report_file:
            build_report_file.write(r'this thing \{should\} not be replaced')

        report = reporter.MailReporter(parent_dir,
                                       'email@from',
                                       ['email@to'],
                                       DEFAULT_REPORT_INTRO,
                                       DEFAULT_REPORT_FOOTER)
        report.create_report()

        self.assertIn(r'this thing \{should\} not be replaced',
                      report.report.as_string())
        self.assertIn('Build FAILED', report.report.as_string())
        self.assertEqual(len([part for part in report.report.walk()]), 2)

        mock_debug.assert_called_once()
        mock_info.assert_called_once()

        shutil.rmtree(parent_dir)

    @mock.patch('logging.info')
    @mock.patch('logging.debug')
    def test_full_multi_result(self, mock_debug, mock_info):
        """Make sure the reporter properly reports multiple runs."""
        parent_dir = tempfile.mkdtemp()
        with open(os.path.join(parent_dir, 'merge.result'),
                  'w') as merge_result_file:
            merge_result_file.write('true')
        with open(os.path.join(parent_dir, 'merge.report'),
                  'w') as merge_report_file:
            merge_report_file.write('merge report')

        for test_dir_name in ['true', 'false']:
            dirname = os.path.join(parent_dir, test_dir_name)
            os.mkdir(dirname)
            with open(os.path.join(dirname, 'build.result'),
                      'w') as build_result_file:
                build_result_file.write('true')
            with open(os.path.join(dirname, 'build.report'),
                      'w') as build_report_file:
                build_report_file.write('build report dir: %s' % test_dir_name)

            with open(os.path.join(dirname, 'run.result'),
                      'w') as run_result_file:
                run_result_file.write(test_dir_name)
            with open(os.path.join(dirname, 'run.report'),
                      'w') as run_report_file:
                run_report_file.write('run report dir: %s' % test_dir_name)

        report = reporter.MailReporter(parent_dir,
                                       'email@from',
                                       ['email@to'],
                                       DEFAULT_REPORT_INTRO,
                                       DEFAULT_REPORT_FOOTER)
        report.create_report()

        for test_dir_name in ['true', 'false']:
            self.assertIn('build report dir: %s' % test_dir_name,
                          report.report.as_string())
            self.assertIn('run report dir: %s' % test_dir_name,
                          report.report.as_string())
        self.assertIn('Testing FAILED', report.report.as_string())
        self.assertEqual(len([part for part in report.report.walk()]), 2)

        self.assertEqual(mock_debug.call_count, 2)
        mock_info.assert_called_once()

        shutil.rmtree(parent_dir)
