"""Opt-in checks against a disposable PostgreSQL database."""
from collections import Counter
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from workload import Config, Schedule, Store, payload, report, seed_random, setup, start


@unittest.skipUnless(os.getenv('GENERATOR_TEST_POSTGRES') == '1', 'requires a disposable PostgreSQL database')
class PostgresTests(unittest.TestCase):
    def connect(self):
        conn = payload.get_db_connection()
        self.addCleanup(conn.close)
        return conn

    def table_counts(self, conn):
        with conn, conn.cursor() as cur:
            result = {}
            for region in ('au', 'uk', 'us'):
                for table in ('products', 'users', 'orders', 'line_items'):
                    cur.execute(f'SELECT count(*) FROM {region}.{table}')
                    result[region, table] = cur.fetchone()[0]
            return result

    def test_setup_checkpoint_lock_and_rollback(self):
        conn = self.connect()
        config = Config(str(uuid.uuid4()), workers=2, duration=60, bursts=(), baseline=3600)
        setup(conn, config)
        before = self.table_counts(conn)
        setup(conn, config)
        self.assertEqual(self.table_counts(conn), before)
        with self.assertRaises(ValueError):
            setup(conn, replace(config, workers=3))
        started = start(conn, config.run_id, 0)
        self.assertEqual(start(conn, config.run_id, 10), started)
        worker_conn = self.connect()
        store = Store(worker_conn, config.run_id, 0)
        other = self.connect()
        with self.assertRaisesRegex(ValueError, 'active process'):
            Store(other, config.run_id, 0)
        schedule = Schedule(config)
        events = schedule.events(0, 0)
        store.apply(schedule, events[:10], 0, 10)
        checkpoint = report(conn, config.run_id)['source_rows']
        self.assertEqual(checkpoint, 10)
        worker_conn.close()
        resumed = Store(self.connect(), config.run_id, 0)
        self.assertEqual(resumed.state['offset'], 10)
        before_failure = self.table_counts(conn)
        real_insert = __import__('workload').insert
        calls = 0

        def fail_second_insert(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated process failure')
            return real_insert(*args)

        with patch('workload.insert', side_effect=fail_second_insert):
            with self.assertRaisesRegex(RuntimeError, 'simulated process failure'):
                resumed.apply(schedule, events[10:30], 0, 30)
        self.assertEqual(report(conn, config.run_id)['source_rows'], checkpoint)
        self.assertEqual(self.table_counts(conn), before_failure)
        resumed.conn.close()
        resumed = Store(self.connect(), config.run_id, 0)
        self.assertEqual(resumed.state['offset'], 10)
        resumed.apply(schedule, events[10:30], 0, 30)
        self.assertEqual(report(conn, config.run_id)['source_rows'], 30)
        pools = []
        with conn, conn.cursor() as cur:
            cur.execute('SELECT state FROM generator_control.workers WHERE run_id=%s', (config.run_id,))
            for (state,) in cur.fetchall():
                pools.append({(r,t,i) for r, tables in state['pools'].items() for t, ids in tables.items() for i in ids})
        self.assertFalse(pools[0] & pools[1])
        seed_random(config, 'event:10')
        first = payload.generate_user('au')
        seed_random(config, 'event:10')
        self.assertEqual(first, payload.generate_user('au'))

    def test_local_multiprocess_run_counts_real_source_changes(self):
        conn = self.connect()
        config = Config(str(uuid.uuid4()), workers=3, duration=8, baseline=7200,
                        peak=14400, bursts=(2, 5), burst_duration=1)
        setup(conn, config)
        with conn, conn.cursor() as cur:
            cur.execute('CREATE TABLE generator_control.test_audit (region TEXT, operation TEXT, entity TEXT)')
            cur.execute('''CREATE FUNCTION generator_control.audit_change() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN INSERT INTO generator_control.test_audit VALUES (TG_TABLE_SCHEMA, lower(TG_OP), TG_TABLE_NAME);
                RETURN NULL; END $$''')
            for region in config.regions:
                for table in ('products','users','orders','line_items'):
                    cur.execute(f'''CREATE TRIGGER generator_test_audit AFTER INSERT OR UPDATE OR DELETE ON {region}.{table}
                        FOR EACH ROW EXECUTE FUNCTION generator_control.audit_change()''')
        def cleanup_audit():
            with conn, conn.cursor() as cur:
                cur.execute('DROP FUNCTION generator_control.audit_change() CASCADE')
                cur.execute('DROP TABLE generator_control.test_audit')
        self.addCleanup(cleanup_audit)
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'main.py'),
                   'run', '--run-id', config.run_id, '--delay', '2']
        process = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result = report(conn, config.run_id)
        self.assertTrue(result['complete'])
        self.assertEqual(result['source_rows'], 1200)
        with conn, conn.cursor() as cur:
            cur.execute('SELECT region,operation,entity,count(*) FROM generator_control.test_audit GROUP BY 1,2,3')
            actual = {f'{r}:{o}:{t}': n for r,o,t,n in cur.fetchall()}
        self.assertEqual(result['counts'], actual)
        operations, regions, tables = Counter(), Counter(), Counter()
        for key, count in actual.items():
            region, op, table = key.split(':')
            operations[op] += count
            regions[region] += count
            tables[table] += count
        self.assertEqual(operations, {'insert':600, 'update':540, 'delete':60})
        self.assertEqual(regions, {'au':400, 'uk':400, 'us':400})
        self.assertEqual(len(tables), 4)
        rerun = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        setup(conn, config)
        self.assertEqual(report(conn, config.run_id), result)
        with conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM generator_control.test_audit')
            self.assertEqual(cur.fetchone()[0], 1200)


if __name__ == '__main__':
    unittest.main()
