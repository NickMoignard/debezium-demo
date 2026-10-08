"""Bounded source-row workloads with PostgreSQL checkpoints per worker."""
import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import random
import time
import subprocess
import sys
from pathlib import Path

from psycopg2.extras import Json

import main as payload

TABLES = ('products', 'users', 'orders', 'line_items')
OPERATIONS = ('insert', 'update', 'delete')
PROFILES = {'normal': (5000, 100000), 'load': (20000, 400000)}
POOL_SIZE = 32
BATCH_SIZE = 100


def cycle(weights):
    """Spread integer weights through a deterministic, repeating schedule."""
    divisor = math.gcd(*weights)
    weights = tuple(w // divisor for w in weights)
    total = sum(weights)
    scores = [0] * len(weights)
    result = []
    for _ in range(total):
        scores = [s + w for s, w in zip(scores, weights)]
        chosen = max(range(len(weights)), key=lambda i: scores[i])
        scores[chosen] -= total
        result.append(chosen)
    return tuple(result)


@dataclass(frozen=True)
class Config:
    run_id: str
    seed: int = 1
    workers: int = 1
    baseline: int = 5000
    peak: int = 100000
    duration: int = 900
    bursts: tuple = (300, 600)
    burst_duration: int = 60
    regions: tuple = ('au', 'uk', 'us')
    region_weights: tuple = (1, 1, 1)
    operation_weights: tuple = (50, 45, 5)
    bad_data_rate: float = 0.02

    def __post_init__(self):
        if not 0 <= self.bad_data_rate <= 1:
            raise ValueError("bad-data-rate must be between zero and one")
        if not self.run_id or len(self.run_id) > 128:
            raise ValueError('run-id must contain 1 to 128 characters')
        if self.workers < 1 or self.duration < 1 or self.burst_duration < 1:
            raise ValueError('workers and durations must be positive')
        if self.baseline < 0 or self.peak < 0:
            raise ValueError('rates must be nonnegative')
        if set(self.regions) != {'au', 'uk', 'us'} or len(self.regions) != 3:
            raise ValueError('regions must contain au, uk, us exactly once')
        if len(self.region_weights) != 3 or len(self.operation_weights) != 3:
            raise ValueError('provide three region and three operation weights')
        if any(w <= 0 for w in self.region_weights + self.operation_weights):
            raise ValueError('weights must be positive integers')
        if sum(self.region_weights) > 10000 or sum(self.operation_weights) > 10000:
            raise ValueError('weight totals must not exceed 10000')
        if self.operation_weights[0] < self.operation_weights[2]:
            raise ValueError('insert weight must cover delete weight')
        if tuple(sorted(set(self.bursts))) != self.bursts:
            raise ValueError('burst starts must be sorted and unique')
        if any(b < 0 or b + self.burst_duration > self.duration for b in self.bursts):
            raise ValueError('bursts must fit inside the workload window')
        if any(b + self.burst_duration > c for b, c in zip(self.bursts, self.bursts[1:])):
            raise ValueError('bursts must not overlap')

    def rate(self, second):
        return self.peak if any(b <= second < b + self.burst_duration for b in self.bursts) else self.baseline

    def boundaries(self):
        # Carry fractions globally, before allocating rows to workers.
        total = 0
        result = [0]
        for second in range(self.duration):
            total += self.rate(second)
            result.append(total // 60)
        return result

    def json(self):
        return json.loads(json.dumps(asdict(self)))


class Schedule:
    def __init__(self, config):
        self.config = config
        self.bounds = config.boundaries()
        self.ops = cycle(config.operation_weights)
        self.regions = cycle(config.region_weights)
        self.block = len(self.ops) * len(self.regions)

    def events(self, second, worker):
        start, end = self.bounds[second:second + 2]
        # Assign complete operation/region cycles. A modulo per row would
        # leave some workers doing only deletes when worker count divides
        # the operation cycle length.
        return [i for i in range(start, end)
                if (i // self.block) % self.config.workers == worker]

    def event(self, index):
        op = self.ops[index % len(self.ops)]
        # Rotate regions for each complete operation cycle, avoiding a fixed
        # association between one operation and one region.
        region = self.regions[(index % len(self.ops) + index // len(self.ops)) % len(self.regions)]
        table = TABLES[(index // (self.block * self.config.workers)) % len(TABLES)]
        return self.config.regions[region], OPERATIONS[op], table


def seed_random(config, identity):
    value = int.from_bytes(hashlib.sha256(f'{config.seed}:{identity}'.encode()).digest()[:8], 'big')
    random.seed(value)
    for fake in payload.fakers.values():
        fake.seed_instance(value)


def insert(cur, region, table, anchors):
    if table == 'products':
        values = payload.generate_product(region)
    elif table == 'users':
        values = payload.generate_user(region)
    elif table == 'orders':
        values = payload.generate_order([anchors['users']])
    else:
        values = payload.generate_line_item(anchors['orders'], [anchors['products']])
    columns = ','.join(values)
    placeholders = ','.join(['%s'] * len(values))
    # Identifiers come only from the validated region list and TABLES.
    cur.execute(f'INSERT INTO {region}.{table} ({columns}) VALUES ({placeholders}) RETURNING id', list(values.values()))
    return cur.fetchone()[0]


def setup(conn, config):
    """Seed once per run, atomically with its immutable configuration."""
    payload.BAD_DATA_RATE = config.bad_data_rate
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(71249812)")
            cur.execute('CREATE SCHEMA IF NOT EXISTS generator_control')
            cur.execute('''CREATE TABLE IF NOT EXISTS generator_control.runs (
                run_id TEXT PRIMARY KEY, config JSONB NOT NULL, starts_at TIMESTAMPTZ)''')
            cur.execute('''CREATE TABLE IF NOT EXISTS generator_control.workers (
                run_id TEXT REFERENCES generator_control.runs, worker INTEGER,
                state JSONB NOT NULL, PRIMARY KEY (run_id, worker))''')
            cur.execute('SELECT config FROM generator_control.runs WHERE run_id=%s', (config.run_id,))
            existing = cur.fetchone()
            if existing:
                if existing[0] != config.json():
                    raise ValueError('run-id already exists with a different configuration')
                return
            payload.create_tables(conn)
            cur.execute('INSERT INTO generator_control.runs VALUES (%s,%s,NULL)', (config.run_id, Json(config.json())))
            for worker in range(config.workers):
                state = {'tick': 0, 'offset': 0, 'counts': {}, 'pools': {}, 'anchors': {}, 'missed': 0}
                seed_random(config, f'setup:{worker}')
                for region in config.regions:
                    anchors = {}
                    for table in TABLES[:3]:
                        anchors[table] = insert(cur, region, table, anchors)
                    state['anchors'][region] = anchors
                    state['pools'][region] = {
                        table: [insert(cur, region, table, anchors) for _ in range(POOL_SIZE)]
                        for table in TABLES
                    }
                cur.execute('INSERT INTO generator_control.workers VALUES (%s,%s,%s)', (config.run_id, worker, Json(state)))


def start(conn, run_id, delay):
    if delay < 0:
        raise ValueError('start delay must be nonnegative')
    with conn:
        with conn.cursor() as cur:
            cur.execute('''UPDATE generator_control.runs
                SET starts_at=COALESCE(starts_at,clock_timestamp() + %s * interval '1 second')
                WHERE run_id=%s RETURNING starts_at''', (delay, run_id))
            row = cur.fetchone()
            if not row:
                raise ValueError('run setup first')
            return row[0].isoformat()


class Store:
    def __init__(self, conn, run_id, worker):
        self.conn, self.run_id, self.worker = conn, run_id, worker
        with conn:
            with conn.cursor() as cur:
                cur.execute('SELECT config, starts_at FROM generator_control.runs WHERE run_id=%s', (run_id,))
                row = cur.fetchone()
                if not row or row[1] is None:
                    raise ValueError('run setup and start before launching workers')
                values = row[0]
                for key in ('bursts', 'regions', 'region_weights', 'operation_weights'):
                    values[key] = tuple(values[key])
                self.config = Config(**values)
                payload.BAD_DATA_RATE = self.config.bad_data_rate
                if not 0 <= worker < self.config.workers:
                    raise ValueError('worker-id is outside the configured worker count')
                cur.execute('SELECT pg_try_advisory_lock(hashtext(%s),%s)', (run_id, worker))
                if not cur.fetchone()[0]:
                    raise ValueError('this worker already has an active process')
                cur.execute('SELECT state FROM generator_control.workers WHERE run_id=%s AND worker=%s', (run_id, worker))
                self.state = cur.fetchone()[0]

    def elapsed(self):
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute('SELECT extract(epoch FROM clock_timestamp()-starts_at) FROM generator_control.runs WHERE run_id=%s', (self.run_id,))
                return float(cur.fetchone()[0])

    def save(self, cur):
        cur.execute('UPDATE generator_control.workers SET state=%s WHERE run_id=%s AND worker=%s',
                    (Json(self.state), self.run_id, self.worker))

    def skip(self, tick, missed):
        self.state.update(tick=tick, offset=0, missed=self.state['missed'] + missed)
        with self.conn:
            with self.conn.cursor() as cur:
                self.save(cur)

    def apply(self, schedule, events, tick, offset):
        with self.conn:
            with self.conn.cursor() as cur:
                remaining = self.config.duration - self.elapsed_in_transaction(cur)
                if remaining <= 0:
                    return False
                cur.execute("SELECT set_config('statement_timeout',%s,true)", (str(max(1, int(remaining * 1000))),))
                for index in events:
                    region, operation, table = schedule.event(index)
                    seed_random(self.config, f'event:{index}')
                    pool = self.state['pools'][region][table]
                    if operation == 'insert':
                        new_id = insert(cur, region, table, self.state['anchors'][region])
                        pool.append(new_id)
                        del pool[:-POOL_SIZE]
                    else:
                        if not pool:
                            raise RuntimeError('mutation pool exhausted; choose a less delete-heavy mix')
                        row_id = pool[0]
                        if operation == 'delete':
                            cur.execute(f'DELETE FROM {region}.{table} WHERE id=%s', (row_id,))
                            pool.pop(0)
                        else:
                            assignments = {
                                'products': 'stock_quantity=mod(stock_quantity+1,501)',
                                'users': 'updated_at=clock_timestamp()',
                                'orders': "order_status=CASE order_status WHEN 'pending' THEN 'paid' WHEN 'paid' THEN 'shipped' WHEN 'shipped' THEN 'delivered' ELSE 'pending' END, updated_at=clock_timestamp()",
                                'line_items': 'quantity=mod(quantity,5)+1, updated_at=clock_timestamp()',
                            }
                            cur.execute(f'UPDATE {region}.{table} SET {assignments[table]} WHERE id=%s', (row_id,))
                            pool.append(pool.pop(0))
                        if cur.rowcount != 1:
                            raise RuntimeError('owned source row is missing; batch rolled back')
                    key = f'{region}:{operation}:{table}'
                    self.state['counts'][key] = self.state['counts'].get(key, 0) + 1
                if self.elapsed_in_transaction(cur) >= self.config.duration:
                    raise TimeoutError('workload deadline passed; batch rolled back')
                self.state.update(tick=tick, offset=offset)
                self.save(cur)
        return True

    def elapsed_in_transaction(self, cur):
        cur.execute('SELECT extract(epoch FROM clock_timestamp()-starts_at) FROM generator_control.runs WHERE run_id=%s', (self.run_id,))
        return float(cur.fetchone()[0])


def run_worker(store, sleep=time.sleep):
    """Do not replay missed time slots or extend the measured window."""
    schedule = Schedule(store.config)
    while store.state['tick'] < store.config.duration:
        elapsed = store.elapsed()
        tick = store.state['tick']
        if elapsed < tick:
            sleep(min(tick - elapsed, 0.1))
            continue
        current = min(int(elapsed), store.config.duration)
        if current > tick:
            missed = sum(len(schedule.events(t, store.worker)) for t in range(tick, current)) - store.state['offset']
            store.skip(current, missed)
            continue
        events = schedule.events(tick, store.worker)
        offset = store.state['offset']
        batch = events[offset:offset + BATCH_SIZE]
        if not batch:
            store.skip(tick + 1, 0)
            continue
        if not store.apply(schedule, batch, tick, offset + len(batch)):
            continue
    while True:
        remaining = store.config.duration - store.elapsed()
        if remaining <= 0:
            break
        sleep(min(remaining, 0.1))
    return store.state['missed'] == 0


def report(conn, run_id):
    with conn:
        with conn.cursor() as cur:
            cur.execute('SELECT config, starts_at FROM generator_control.runs WHERE run_id=%s', (run_id,))
            config, started = cur.fetchone()
            cur.execute('SELECT worker,state FROM generator_control.workers WHERE run_id=%s ORDER BY worker', (run_id,))
            workers = cur.fetchall()
    counts = Counter()
    for _, state in workers:
        counts.update(state['counts'])
    return {'run_id': run_id, 'config': config, 'started_at': str(started),
            'source_rows': sum(counts.values()), 'counts': dict(counts),
            'workers': [{'worker': worker, 'tick': s['tick'], 'missed_rows': s['missed']} for worker, s in workers],
            'complete': all(s['tick'] == config['duration'] and s['missed'] == 0 for _, s in workers)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    setup_parser = sub.add_parser('setup')
    setup_parser.add_argument('--run-id', required=True)
    setup_parser.add_argument('--profile', choices=PROFILES, default='normal')
    setup_parser.add_argument('--seed', type=int, default=1)
    setup_parser.add_argument('--workers', type=int, default=1)
    setup_parser.add_argument('--baseline', type=int)
    setup_parser.add_argument('--peak', type=int)
    setup_parser.add_argument('--duration', type=int, default=900)
    setup_parser.add_argument('--bursts', default='300,600', help='comma-separated start seconds; empty disables bursts')
    setup_parser.add_argument('--burst-duration', type=int, default=60)
    setup_parser.add_argument('--regions', default='au,uk,us')
    setup_parser.add_argument('--region-weights', default='1,1,1')
    setup_parser.add_argument('--operation-mix', default='50,45,5', help='insert,update,delete integer weights')
    setup_parser.add_argument('--bad-data-rate', type=float, default=0.02)
    for name in ('start', 'worker', 'report', 'run'):
        command = sub.add_parser(name)
        command.add_argument('--run-id', required=True)
        if name in ('start', 'run'):
            command.add_argument('--delay', type=float, default=5)
        if name == 'worker':
            command.add_argument('--worker-id', type=int, required=True)
    args = parser.parse_args()
    conn = payload.get_db_connection()
    try:
        if args.command == 'setup':
            baseline, peak = PROFILES[args.profile]
            config = Config(args.run_id, args.seed, args.workers,
                            baseline if args.baseline is None else args.baseline,
                            peak if args.peak is None else args.peak,
                            args.duration, tuple(int(v) for v in args.bursts.split(',') if v),
                            args.burst_duration, tuple(args.regions.split(',')),
                            tuple(map(int, args.region_weights.split(','))),
                            tuple(map(int, args.operation_mix.split(','))), args.bad_data_rate)
            setup(conn, config)
            print(json.dumps(config.json()))
        elif args.command == 'run':
            start(conn, args.run_id, args.delay)
            run_config = report(conn, args.run_id)['config']
            children = []
            try:
                for worker in range(run_config['workers']):
                    children.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                        'worker', '--run-id', args.run_id, '--worker-id', str(worker)]))
                codes = [child.wait() for child in children]
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                for child in children:
                    child.wait()
            result = report(conn, args.run_id)
            print(json.dumps(result, indent=2))
            if any(codes) or not result['complete']:
                raise SystemExit('Run incomplete; inspect missed rows and worker errors')
        elif args.command == 'start':
            print(start(conn, args.run_id, args.delay))
        elif args.command == 'worker':
            if not run_worker(Store(conn, args.run_id, args.worker_id)):
                raise SystemExit('Worker missed scheduled rows; inspect the run report')
        else:
            print(json.dumps(report(conn, args.run_id), indent=2))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
