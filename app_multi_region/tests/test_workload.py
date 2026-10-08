import unittest
from collections import Counter

from workload import Config, Schedule, run_worker


class FakeStore:
    def __init__(self, config, worker):
        self.config, self.worker = config, worker
        self.state = {'tick': 0, 'offset': 0, 'missed': 0}
        self.now = 0
        self.events = []

    def elapsed(self):
        return self.now

    def sleep(self, duration):
        self.now += duration

    def skip(self, tick, missed):
        self.state.update(tick=tick, offset=0, missed=self.state['missed'] + missed)

    def apply(self, schedule, events, tick, offset):
        assert 0 <= self.now < self.config.duration
        self.events.extend((self.now, i, schedule.event(i)) for i in events)
        self.state.update(tick=tick, offset=offset)
        return True


class WorkloadTests(unittest.TestCase):
    def test_profiles_at_execution_boundary(self):
        for baseline, peak, expected in [(5000, 100000, 265000), (20000, 400000, 1060000)]:
            config = Config('test', workers=7, baseline=baseline, peak=peak)
            all_events = []
            for worker in range(config.workers):
                store = FakeStore(config, worker)
                self.assertTrue(run_worker(store, store.sleep))
                all_events.extend(store.events)
                self.assertLessEqual(store.now, 900)
                before = len(store.events)
                self.assertTrue(run_worker(store, store.sleep))
                self.assertEqual(before, len(store.events))
            self.assertEqual(len(all_events), expected)
            self.assertEqual(len({i for _, i, _ in all_events}), expected)
            operations = Counter(event[1] for _, _, event in all_events)
            self.assertEqual(operations, {'insert': expected // 2, 'update': expected * 45 // 100, 'delete': expected // 20})
            regions = Counter(event[0] for _, _, event in all_events)
            self.assertLessEqual(max(regions.values()) - min(regions.values()), 1)
            per_second = Counter(int(t + 1e-6) for t, _, _ in all_events)
            bounds = config.boundaries()
            self.assertTrue(all(per_second[t] == bounds[t+1] - bounds[t] for t in range(900)))
            for t in (0, 299, 360, 599, 660, 899):
                self.assertEqual(config.rate(t), baseline)
            for t in (300, 359, 600, 659):
                self.assertEqual(config.rate(t), peak)

    def test_fractional_rates_and_many_workers_do_not_multiply_load(self):
        config = Config('slow', workers=11, baseline=7, peak=7, duration=60, bursts=())
        indexes = []
        for worker in range(11):
            store = FakeStore(config, worker)
            run_worker(store, store.sleep)
            indexes.extend(i for _, i, _ in store.events)
        self.assertEqual(sorted(indexes), list(range(7)))

    def test_late_worker_reports_missed_rows_without_catchup(self):
        store = FakeStore(Config('late', baseline=120, peak=120, duration=5, bursts=()), 0)
        store.now = 2.2
        self.assertFalse(run_worker(store, store.sleep))
        self.assertEqual(store.state['missed'], 4)
        self.assertEqual(len(store.events), 6)
        self.assertTrue(all(t >= 2 for t, _, _ in store.events))

    def test_restart_after_deadline_never_writes(self):
        store = FakeStore(Config('expired', duration=10, bursts=()), 0)
        store.now = 11
        self.assertFalse(run_worker(store, store.sleep))
        self.assertEqual(store.events, [])
        self.assertEqual(store.state['tick'], 10)

    def test_custom_weights(self):
        config = Config('weighted', region_weights=(2, 1, 1), operation_weights=(6, 3, 1))
        schedule = Schedule(config)
        events = [schedule.event(i) for i in range(400)]
        self.assertEqual(Counter(e[0] for e in events), {'au': 200, 'uk': 100, 'us': 100})
        self.assertEqual(Counter(e[1] for e in events), {'insert': 240, 'update': 120, 'delete': 40})
        self.assertEqual(set(e[2] for e in events), {'products', 'users', 'orders', 'line_items'})

    def test_workers_receive_all_operations_even_when_count_divides_mix_cycle(self):
        config = Config('ownership', workers=20, baseline=4800, duration=60, bursts=())
        schedule = Schedule(config)
        for worker in range(20):
            events = [schedule.event(i) for second in range(60) for i in schedule.events(second, worker)]
            self.assertEqual(Counter(e[1] for e in events), {'insert': 120, 'update': 108, 'delete': 12})
            self.assertEqual(Counter(e[0] for e in events), {'au': 80, 'uk': 80, 'us': 80})
            self.assertEqual(len(set(e[2] for e in events)), 4)

    def test_invalid_schedules(self):
        for overrides in ({'workers': 0}, {'baseline': -1}, {'bursts': (300, 320)},
                          {'regions': ('au', 'eu', 'us')}, {'region_weights': (1, 0, 1)},
                          {'operation_weights': (1, 1, 9)}):
            with self.assertRaises(ValueError):
                Config('invalid', **overrides)


if __name__ == '__main__':
    unittest.main()
