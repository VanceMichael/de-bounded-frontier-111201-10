import json
import tempfile
import unittest
from pathlib import Path

from collector.frontier import (
    BUDGET_EXHAUSTED,
    Crawler,
    DEPTH_EXCEEDED,
    FETCHED,
    Frontier,
    LICENSE_REVOKED,
    License,
    ORIGIN_NOT_LICENSED,
    PATH_NOT_LICENSED,
    REDIRECTED,
    ROBOTS_DISALLOWED,
    ROBOTS_UNAVAILABLE,
)
from collector.transport import Client, MappedClient, mirror_routes, replay_server

ORIGIN = 'https://museum.example'
ALLOW_ALL = 'User-agent: *\nAllow: /\n'


def license_data(**overrides):
    data = dict(version=1, origins=[ORIGIN], paths=['/'], tracking=['utm_source'],
                semantic_query=['type'], budget=20, depth=4,
                start=[ORIGIN + '/start'])
    data.update(overrides)
    return data


class LicenseTests(unittest.TestCase):
    def test_canonicalize_strips_only_tracking(self):
        license = License.from_data(license_data())
        self.assertEqual(
            license.canonicalize(ORIGIN + '/items/a?utm_source=x&type=scan#top'),
            ORIGIN + '/items/a?type=scan')
        self.assertEqual(license.canonicalize(ORIGIN + '/items/a?fbclid=1'),
                         ORIGIN + '/items/a?fbclid=1')

    def test_canonicalize_equivalence(self):
        license = License.from_data(license_data())
        self.assertEqual(license.canonicalize('HTTPS://MUSEUM.EXAMPLE:443/items/a'),
                         ORIGIN + '/items/a')
        self.assertEqual(license.canonicalize(ORIGIN + '/items/a?b=2&a=1'),
                         license.canonicalize(ORIGIN + '/items/a?a=1&b=2'))
        self.assertIsNone(license.canonicalize('mailto:x@example.invalid'))

    def test_fixture_license_versions(self):
        v1 = License.load('fixtures/access.json', version=1)
        self.assertEqual(v1.check(ORIGIN + '/items/a', 1), (True, 'ok'))
        v2 = License.load('fixtures/access.json', version=2)
        self.assertEqual(v2.check(ORIGIN + '/items/a', 1), (False, LICENSE_REVOKED))
        self.assertEqual(License.load('fixtures/access.json').version, 2)
        with self.assertRaises(ValueError):
            License.load('fixtures/access.json', version=99)

    def test_check_reasons(self):
        license = License.from_data(license_data(paths=['/items/'], depth=1))
        self.assertEqual(license.check('https://outside.example/items/a', 0)[1],
                         ORIGIN_NOT_LICENSED)
        self.assertEqual(license.check(ORIGIN + '/about', 0)[1], PATH_NOT_LICENSED)
        self.assertEqual(license.check(ORIGIN + '/items/a', 2)[1], DEPTH_EXCEEDED)

    def test_invalid_license_rejected(self):
        with self.assertRaises(ValueError):
            License.from_data(license_data(tracking=['type']))
        with self.assertRaises(ValueError):
            License.from_data(license_data(budget=0))


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'frontier.sqlite3'
        self.license = License.from_data(license_data())
        self.frontier = Frontier(self.path)

    def tearDown(self):
        self.frontier.close()
        self.temporary.cleanup()

    def test_dedup_equivalent_urls(self):
        self.assertEqual(self.frontier.add(ORIGIN + '/items/a', 1, None, self.license), 'queued')
        self.assertEqual(
            self.frontier.add(ORIGIN + '/items/a?utm_source=x', 1, None, self.license),
            'duplicate')
        self.assertEqual(len(self.frontier.entries()), 1)

    def test_denied_recorded_with_reason(self):
        outcome = self.frontier.add('https://outside.example/x', 1, None, self.license)
        self.assertEqual(outcome, f'denied:{ORIGIN_NOT_LICENSED}')
        entry = self.frontier.entries(state='denied')[0]
        self.assertEqual(entry['reason'], ORIGIN_NOT_LICENSED)

    def test_fair_pop_across_sources(self):
        for suffix in ('/a1', '/a2'):
            self.frontier.add(ORIGIN + suffix, 1, 'P1', self.license)
        self.frontier.add(ORIGIN + '/b1', 1, 'P2', self.license)
        first = self.frontier.pop()
        self.assertTrue(first['url'].endswith('/a1'))
        self.frontier.set_state(first['url'], 'visited', FETCHED, 1)
        second = self.frontier.pop()
        self.assertTrue(second['url'].endswith('/b1'))
        self.frontier.set_state(second['url'], 'visited', FETCHED, 1)
        third = self.frontier.pop()
        self.assertTrue(third['url'].endswith('/a2'))

    def test_revalidate_revokes_queued(self):
        self.frontier.add(ORIGIN + '/items/a', 1, None, self.license)
        v2 = License.from_data(license_data(version=2, revoked=['/items/a']))
        self.frontier.revalidate(v2)
        entry = self.frontier.entries(state='denied')[0]
        self.assertEqual(entry['reason'], LICENSE_REVOKED)
        self.assertEqual(entry['license_version'], 2)

    def test_persistence_across_reopen(self):
        self.frontier.add(ORIGIN + '/items/a', 1, None, self.license)
        self.frontier.close()
        reopened = Frontier(self.path)
        try:
            self.assertEqual(len(reopened.entries(state='queued')), 1)
        finally:
            reopened.close()


class CrawlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'frontier.sqlite3'

    def tearDown(self):
        self.temporary.cleanup()

    def run_stage(self, base, data, version=None, limit=None):
        """单次运行；每阶段重开 Frontier，模拟进程重启后继续。"""
        license = License.from_data(data, version=version)
        frontier = Frontier(self.path)
        try:
            frontier.seed(license)
            client = MappedClient(Client(follow_redirects=False), {ORIGIN: base})
            report = Crawler(frontier, license, client).run(limit=limit)
            return report, frontier.entries(), frontier.fetches()
        finally:
            frontier.close()

    def test_fixture_site_full_run(self):
        site_map = json.loads(Path('fixtures/site-map.json').read_text(encoding='utf-8'))
        routes = mirror_routes(site_map)
        routes['/robots.txt'] = [dict(body=ALLOW_ALL)]
        access = json.loads(Path('fixtures/access.json').read_text(encoding='utf-8'))
        with replay_server(routes) as (base, calls):
            report, entries, fetches = self.run_stage(base, access, version=1)
        by_url = {entry['url']: entry for entry in entries}
        self.assertEqual(by_url[ORIGIN + '/start']['state'], 'visited')
        self.assertEqual(by_url[ORIGIN + '/items/a']['state'], 'visited')
        # 日历不在许可路径内，登记为拒绝且从不请求
        calendar = by_url[ORIGIN + '/calendar?month=1']
        self.assertEqual((calendar['state'], calendar['reason']),
                         ('denied', PATH_NOT_LICENSED))
        outside = by_url['https://outside.example/blocked']
        self.assertEqual((outside['state'], outside['reason']),
                         ('denied', ORIGIN_NOT_LICENSED))
        # 跟踪参数等价地址只登记一次
        self.assertEqual(len([e for e in entries if e['url'].startswith(ORIGIN + '/items/a')]), 1)
        self.assertEqual(report['outcomes'].get('duplicate'), 1)
        # 拒绝地址从未出现在请求记录中
        paths = [call['path'] for call in calls]
        self.assertEqual(sorted(paths), ['/items/a', '/robots.txt', '/start'])
        self.assertTrue(all(fetch['url'].startswith(ORIGIN) for fetch in fetches))

    def test_revoked_queue_entry_never_requested(self):
        site_map = json.loads(Path('fixtures/site-map.json').read_text(encoding='utf-8'))
        routes = mirror_routes(site_map)
        routes['/robots.txt'] = [dict(body=ALLOW_ALL)]
        access = json.loads(Path('fixtures/access.json').read_text(encoding='utf-8'))
        with replay_server(routes) as (base, calls):
            self.run_stage(base, access, version=1, limit=1)
            report, entries, fetches = self.run_stage(base, access, version=2)
        by_url = {entry['url']: entry for entry in entries}
        revoked = by_url[ORIGIN + '/items/a']
        self.assertEqual((revoked['state'], revoked['reason']), ('denied', LICENSE_REVOKED))
        self.assertEqual(revoked['license_version'], 2)
        # 撤销生效后不再发出任何页面请求
        self.assertEqual([call['path'] for call in calls], ['/robots.txt', '/start'])

    def test_robots_unavailable_pauses_then_recovers(self):
        routes = {'/robots.txt': [dict(status=503, body='维护中'), dict(body=ALLOW_ALL)],
                  '/start': [dict(body='<a href="/a">甲</a>')],
                  '/a': [dict(body='完成')]}
        with replay_server(routes) as (base, calls):
            _, entries, _ = self.run_stage(base, license_data())
            self.assertEqual((entries[0]['state'], entries[0]['reason']),
                             ('paused', ROBOTS_UNAVAILABLE))
            self.assertEqual([call['path'] for call in calls], ['/robots.txt'])
            _, entries, _ = self.run_stage(base, license_data())
        by_url = {entry['url']: entry for entry in entries}
        self.assertEqual(by_url[ORIGIN + '/start']['state'], 'visited')
        self.assertEqual(by_url[ORIGIN + '/a']['state'], 'visited')

    def test_robots_disallow_denies_without_request(self):
        routes = {'/robots.txt': [dict(body='User-agent: *\nDisallow: /private')],
                  '/private/x': [dict(body='保密')]}
        data = license_data(start=[ORIGIN + '/private/x'])
        with replay_server(routes) as (base, calls):
            _, entries, _ = self.run_stage(base, data)
        self.assertEqual((entries[0]['state'], entries[0]['reason']),
                         ('denied', ROBOTS_DISALLOWED))
        self.assertEqual([call['path'] for call in calls], ['/robots.txt'])

    def test_redirect_outside_not_followed(self):
        routes = {'/robots.txt': [dict(body=ALLOW_ALL)],
                  '/go': [dict(status=302,
                               headers={'Location': 'https://outside.example/blocked'})]}
        data = license_data(start=[ORIGIN + '/go'])
        with replay_server(routes) as (base, calls):
            _, entries, fetches = self.run_stage(base, data)
        by_url = {entry['url']: entry for entry in entries}
        self.assertEqual(by_url[ORIGIN + '/go']['reason'], REDIRECTED)
        outside = by_url['https://outside.example/blocked']
        self.assertEqual((outside['state'], outside['reason']), ('denied', ORIGIN_NOT_LICENSED))
        self.assertEqual([call['path'] for call in calls], ['/robots.txt', '/go'])
        self.assertTrue(all(fetch['url'].startswith(ORIGIN) for fetch in fetches))

    def test_redirect_targets_rechecked_each_hop(self):
        routes = {'/robots.txt': [dict(body='User-agent: *\nDisallow: /blocked')],
                  '/to-ok': [dict(status=302, headers={'Location': '/ok'})],
                  '/to-denied': [dict(status=302, headers={'Location': '/blocked'})],
                  '/ok': [dict(body='可以')],
                  '/blocked': [dict(body='不行')]}
        data = license_data(start=[ORIGIN + '/to-ok', ORIGIN + '/to-denied'])
        with replay_server(routes) as (base, calls):
            _, entries, _ = self.run_stage(base, data)
        by_url = {entry['url']: entry for entry in entries}
        self.assertEqual(by_url[ORIGIN + '/ok']['state'], 'visited')
        self.assertEqual(by_url[ORIGIN + '/blocked']['reason'], ROBOTS_DISALLOWED)
        paths = [call['path'] for call in calls]
        self.assertIn('/ok', paths)
        self.assertNotIn('/blocked', paths)

    def test_budget_exhaustion_and_resume(self):
        routes = {'/robots.txt': [dict(body=ALLOW_ALL)],
                  '/start': [dict(body='<a href="/a">甲</a><a href="/b">乙</a>')],
                  '/a': [dict(body='甲')], '/b': [dict(body='乙')]}
        with replay_server(routes) as (base, calls):
            report, entries, _ = self.run_stage(base, license_data(budget=2))
            self.assertEqual(report['stop'], 'budget')
            states = {entry['url']: entry['state'] for entry in entries}
            self.assertEqual(states[ORIGIN + '/a'], 'exhausted')
            self.assertEqual(states[ORIGIN + '/b'], 'exhausted')
            self.assertEqual(len(calls), 2)
            _, entries, _ = self.run_stage(base, license_data(budget=10))
        states = {entry['url']: entry['state'] for entry in entries}
        self.assertEqual(states[ORIGIN + '/a'], 'visited')
        self.assertEqual(states[ORIGIN + '/b'], 'visited')

    def test_depth_limit(self):
        routes = {'/robots.txt': [dict(body=ALLOW_ALL)],
                  '/start': [dict(body='<a href="/a">甲</a>')],
                  '/a': [dict(body='甲')]}
        with replay_server(routes) as (base, calls):
            _, entries, _ = self.run_stage(base, license_data(depth=0))
        by_url = {entry['url']: entry for entry in entries}
        self.assertEqual(by_url[ORIGIN + '/a']['reason'], DEPTH_EXCEEDED)
        self.assertEqual([call['path'] for call in calls], ['/robots.txt', '/start'])


if __name__ == '__main__':
    unittest.main()
