# -*- encoding: utf-8 -*-
"""F01: legacy keyword migration and persistent forget schema are idempotent."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_batch1_migrate_import_'))

from newGeYi.storage.database import SQLiteStore


class LegacyMigrationTest(unittest.TestCase):
    def test_legacy_keywords_first_upgrade_logs_without_name_error_and_reopen_is_noop(self):
        with tempfile.TemporaryDirectory(prefix='geyi_batch1_migrate_') as tmp:
            path = os.path.join(tmp, 'legacy.sqlite3')
            store = SQLiteStore(path)
            fact_id = store.add_user_fact('group', '10001', '旧库事实', keywords='本地,旧词')
            store._conn().execute("DELETE FROM settings WHERE key IN ('facts.keywords_cleanup', 'facts.keywords_backfilled')")
            store._conn().execute('DROP TABLE forget_state')
            store.close()
            # 真实触发旧库非零清理路径：修复前这里抛 NameError: logger。
            with self.assertLogs('newGeYi.database', level='INFO') as logs:
                store = SQLiteStore(path)
            self.assertIn('1 条', logs.output[0])
            self.assertEqual(store._conn().execute('SELECT keywords FROM user_facts WHERE id=?',
                                                   (fact_id,)).fetchone()[0], '')
            self.assertEqual(store.get_setting('facts.keywords_cleanup'), '1')
            self.assertEqual(store.get_setting('facts.keywords_backfilled'), '1')
            self.assertEqual([r[1] for r in store._conn().execute('PRAGMA table_info(forget_state)')],
                             ['scope', 'user_id', 'generation', 'buffer_id'])
            # 升级后模型给的新关键词在下一次打开时保持原值。
            store._conn().execute('UPDATE user_facts SET keywords=? WHERE id=?', ('模型,新词', fact_id))
            store.purge_user_facts('group', '20002')
            generation = store.forget_generation('global', '20002')
            store.close()
            for _ in range(3):
                store = SQLiteStore(path)
                self.assertEqual(store.backfill_fact_keywords(), 0)
                self.assertEqual(store._conn().execute('SELECT keywords FROM user_facts WHERE id=?',
                                                       (fact_id,)).fetchone()[0], '模型,新词')
                self.assertEqual(store.forget_generation('global', '20002'), generation)
                self.assertEqual(store._conn().execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                store.close()

    def test_keyword_cleanup_rolls_back_keywords_and_flags_on_failure(self):
        with tempfile.TemporaryDirectory(prefix='geyi_batch1_migrate_fail_') as tmp:
            store = SQLiteStore(os.path.join(tmp, 'state.sqlite3'))
            try:
                fact_id = store.add_user_fact('group', '10001', '旧库事实', keywords='保留,旧词')
                store._conn().execute("DELETE FROM settings WHERE key IN ('facts.keywords_cleanup', 'facts.keywords_backfilled')")
                store._conn().execute("CREATE TRIGGER fail_cleanup BEFORE INSERT ON settings "
                                       "WHEN NEW.key='facts.keywords_backfilled' "
                                       "BEGIN SELECT RAISE(ABORT, 'injected cleanup failure'); END")
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected cleanup failure'):
                    store.backfill_fact_keywords()
                self.assertEqual(store._conn().execute('SELECT keywords FROM user_facts WHERE id=?',
                                                       (fact_id,)).fetchone()[0], '保留,旧词')
                self.assertIsNone(store.get_setting('facts.keywords_cleanup'))
                self.assertIsNone(store.get_setting('facts.keywords_backfilled'))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
