# Copyright (c) 2013 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test of DB API"""

import mock
from oslo.config import cfg
import unittest2 as unittest

import quantum.db.api as db


class DBTestCase(unittest.TestCase):
    def setUp(self):
        cfg.CONF.set_override('sql_max_retries', 1, 'DATABASE')
        cfg.CONF.set_override('reconnect_interval', 0, 'DATABASE')

    def tearDown(self):
        db._ENGINE = None
        cfg.CONF.reset()

    def test_db_reconnect(self):
        with mock.patch.object(db, 'register_models') as mock_register:
            mock_register.return_value = False
            db.configure_db()

    def test_warn_when_no_connection(self):
        with mock.patch.object(db, 'register_models') as mock_register:
            mock_register.return_value = False
            with mock.patch.object(db.LOG, 'warn') as mock_log:
                mock_log.return_value = False
                db.configure_db()
                self.assertEquals(mock_log.call_count, 1)
                args = mock_log.call_args
                self.assertNotEqual(args.find('sql_connection'), -1)
