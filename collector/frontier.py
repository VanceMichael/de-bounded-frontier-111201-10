"""可持久化的授权发现边界。

License 描述站方许可：起始地址、允许的 origin 与路径前缀、查询参数语义、
最大深度与总请求预算，以及版本化更新（撤销）。Frontier 把发现到的地址
连同状态与理由存入 SQLite，重启后可继续合法前沿。Crawler 在每一跳重新
检查许可与 robots，按来源公平调度，并对拒绝地址绝不发请求。
"""

import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .domain import Links

AGENT = 'ResearchCollector'

# 前沿状态：queued 待执行，visited 已访问，denied 策略拒绝，
# paused 暂停（可恢复），exhausted 预算耗尽（可恢复）。
STATES = ('queued', 'visited', 'denied', 'paused', 'exhausted')

# 可查询理由
PENDING = 'pending'
FETCHED = 'fetched'
REDIRECTED = 'redirected'
ORIGIN_NOT_LICENSED = 'origin_not_licensed'
PATH_NOT_LICENSED = 'path_not_licensed'
LICENSE_REVOKED = 'license_revoked'
DEPTH_EXCEEDED = 'depth_exceeded'
ROBOTS_DISALLOWED = 'robots_disallowed'
ROBOTS_UNAVAILABLE = 'robots_unavailable'
FETCH_ERROR = 'fetch_error'
BUDGET_EXHAUSTED = 'budget_exhausted'
SCHEME_UNSUPPORTED = 'scheme_unsupported'

REDIRECT_STATUSES = (301, 302, 303, 307, 308)
DEFAULT_PORTS = {'http': 80, 'https': 443}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _origin(url):
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'


def path_matches(path, prefix):
    """路径前缀匹配：'/items/' 匹配其下内容，'/start' 匹配自身与子路径。"""
    if prefix.endswith('/'):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + '/')


class License:
    """站方许可的不可变视图；updates 按版本号依次应用。"""

    def __init__(self, data):
        self.version = data.get('version')
        self.origins = list(data.get('origins') or [])
        self.paths = list(data.get('paths') or [])
        self.tracking = set(data.get('tracking') or [])
        self.semantic = set(data.get('semantic_query') or [])
        self.budget = data.get('budget')
        self.depth = data.get('depth')
        self.revoked = list(data.get('revoked') or [])
        self.start = list(data.get('start') or [])
        if not self.origins:
            raise ValueError('许可缺少允许的 origin')
        if not self.paths:
            raise ValueError('许可缺少允许的路径')
        if not isinstance(self.budget, int) or self.budget < 1:
            raise ValueError('许可预算必须为正整数')
        if not isinstance(self.depth, int) or self.depth < 0:
            raise ValueError('许可深度必须为非负整数')
        if self.tracking & self.semantic:
            raise ValueError('跟踪参数与语义参数存在重叠')

    @classmethod
    def load(cls, path, version=None):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return cls.from_data(data, version)

    @classmethod
    def from_data(cls, data, version=None):
        data = json.loads(json.dumps(data))
        updates = sorted(data.get('updates') or [], key=lambda item: item['version'])
        available = [data.get('version')] + [item['version'] for item in updates]
        target = available[-1] if version is None else version
        if target not in available:
            raise ValueError(f'未知许可版本: {version}')
        revoked = list(data.get('revoked') or [])
        for update in updates:
            if update['version'] > target:
                continue
            if 'revoke_path' in update:
                revoked.append(update['revoke_path'])
            for key in ('budget', 'depth'):
                if key in update:
                    data[key] = update[key]
        data['version'] = target
        data['revoked'] = revoked
        return cls(data)

    def seeds(self):
        if self.start:
            return list(self.start)
        return [self.origins[0] + self.paths[0]]

    def canonicalize(self, url):
        """仅移除许可列出的跟踪参数，保留筛选条件；锚点、默认端口与
        参数顺序视为等价，其余差异（路径、语义参数）一律保留。"""
        try:
            url, _fragment = urldefrag(url)
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme not in ('http', 'https'):
                return None
            host = (parts.hostname or '').lower()
            if not host:
                return None
            port = parts.port
        except ValueError:
            return None
        netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f'{host}:{port}'
        pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                 if key not in self.tracking]
        pairs.sort()
        query = urlencode(pairs)
        return urlunsplit((scheme, netloc, parts.path or '/', query, ''))

    def check(self, url, depth):
        """返回 (是否允许, 理由)。每一跳都必须调用。"""
        parts = urlsplit(url)
        if _origin(url) not in self.origins:
            return False, ORIGIN_NOT_LICENSED
        if any(path_matches(parts.path, prefix) for prefix in self.revoked):
            return False, LICENSE_REVOKED
        if not any(path_matches(parts.path, prefix) for prefix in self.paths):
            return False, PATH_NOT_LICENSED
        if depth > self.depth:
            return False, DEPTH_EXCEEDED
        return True, 'ok'


class Frontier:
    """SQLite 持久化发现边界；状态与理由可查询，重启后可继续。"""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS frontier(
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL UNIQUE,
                depth INTEGER NOT NULL,
                discovered_from TEXT,
                state TEXT NOT NULL,
                reason TEXT,
                http_status INTEGER,
                license_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS robots(
                origin TEXT PRIMARY KEY,
                status INTEGER NOT NULL,
                body TEXT NOT NULL,
                fetched_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fetches(
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                kind TEXT NOT NULL,
                status INTEGER,
                at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _meta_get(self, key, default=None):
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default

    def _meta_set(self, key, value):
        self.connection.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def requests_made(self):
        return int(self._meta_get('requests_made', '0'))

    def record_fetch(self, url, kind, status):
        with self.transaction():
            self.connection.execute(
                "INSERT INTO fetches(url,kind,status,at) VALUES (?,?,?,?)",
                (url, kind, status, _now()))
            self._meta_set('requests_made', self.requests_made() + 1)

    def _exists(self, url):
        return self.connection.execute(
            "SELECT 1 FROM frontier WHERE url=?", (url,)).fetchone() is not None

    def _insert(self, url, depth, discovered_from, state, reason, version):
        self.connection.execute(
            "INSERT INTO frontier(url,depth,discovered_from,state,reason,license_version,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (url, depth, discovered_from, state, reason, version, _now()))

    def add(self, url, depth, discovered_from, license):
        """登记发现地址；等价地址去重，越界地址记为拒绝且永不请求。"""
        canon = license.canonicalize(url)
        key = canon if canon is not None else urldefrag(url)[0]
        if self._exists(key):
            return 'duplicate'
        if canon is None:
            self._insert(key, depth, discovered_from, 'denied', SCHEME_UNSUPPORTED, license.version)
            return f'denied:{SCHEME_UNSUPPORTED}'
        ok, reason = license.check(canon, depth)
        if ok:
            self._insert(canon, depth, discovered_from, 'queued', PENDING, license.version)
            return 'queued'
        self._insert(canon, depth, discovered_from, 'denied', reason, license.version)
        return f'denied:{reason}'

    def seed(self, license):
        return [self.add(url, 0, None, license) for url in license.seeds()]

    def set_state(self, url, state, reason, version, http_status=None):
        self.connection.execute(
            "UPDATE frontier SET state=?, reason=?, http_status=?, license_version=?, updated_at=?"
            " WHERE url=?",
            (state, reason, http_status, version, _now(), url))

    def revalidate(self, license):
        """许可版本更新后重检未完结地址：不再授权的转拒绝；
        暂停与预算耗尽的地址在仍合法时回到队列，等待重试。"""
        rows = self.connection.execute(
            "SELECT url, depth, state FROM frontier WHERE state IN ('queued','paused','exhausted')"
        ).fetchall()
        with self.transaction():
            for row in rows:
                ok, reason = license.check(row['url'], row['depth'])
                if not ok:
                    self.set_state(row['url'], 'denied', reason, license.version)
                elif row['state'] != 'queued':
                    self.set_state(row['url'], 'queued', PENDING, license.version)
            self._meta_set('license_version', license.version)

    def pop(self):
        """按来源公平调度：已访问份额最少的来源优先，同来源按发现顺序。"""
        rows = self.connection.execute(
            "SELECT * FROM frontier WHERE state='queued' ORDER BY id").fetchall()
        if not rows:
            return None
        served = {row[0]: row[1] for row in self.connection.execute(
            "SELECT discovered_from, count(*) FROM frontier WHERE state='visited'"
            " GROUP BY discovered_from")}
        best = min(rows, key=lambda row: (served.get(row['discovered_from'], 0), row['id']))
        return dict(best)

    def exhaust_remaining(self, version):
        self.connection.execute(
            "UPDATE frontier SET state='exhausted', reason=?, license_version=?, updated_at=?"
            " WHERE state='queued'",
            (BUDGET_EXHAUSTED, version, _now()))

    def robots_get(self, origin):
        row = self.connection.execute(
            "SELECT status, body FROM robots WHERE origin=?", (origin,)).fetchone()
        return (row['status'], row['body']) if row else None

    def robots_put(self, origin, status, body):
        self.connection.execute(
            "INSERT INTO robots VALUES (?,?,?,?) ON CONFLICT(origin) DO UPDATE SET"
            " status=excluded.status, body=excluded.body, fetched_at=excluded.fetched_at",
            (origin, status, body, _now()))

    def entries(self, state=None):
        if state:
            rows = self.connection.execute(
                "SELECT * FROM frontier WHERE state=? ORDER BY id", (state,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM frontier ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def counts(self):
        rows = self.connection.execute(
            "SELECT state, count(*) AS n FROM frontier GROUP BY state").fetchall()
        return {row['state']: row['n'] for row in rows}

    def fetches(self):
        rows = self.connection.execute("SELECT * FROM fetches ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def close(self):
        self.connection.close()


class BudgetExhausted(Exception):
    pass


class Crawler:
    """在许可与 robots 双重约束下推进前沿；所有请求经 client 发出。"""

    def __init__(self, frontier, license, client, agent=AGENT):
        self.frontier = frontier
        self.license = license
        self.client = client
        self.agent = agent
        self._robots_memo = {}

    def _request(self, url, kind):
        if self.frontier.requests_made() >= self.license.budget:
            raise BudgetExhausted
        status, headers, body = self.client.request(url)
        self.frontier.record_fetch(url, kind, status)
        headers = {name.lower(): value for name, value in headers.items()}
        return status, headers, body

    def _robots(self, origin):
        """获取并缓存 robots；暂不可用返回 None（调用方按暂停处理）。"""
        if origin in self._robots_memo:
            return self._robots_memo[origin]
        cached = self.frontier.robots_get(origin)
        if cached is not None:
            policy = RobotFileParser()
            policy.parse(cached[1].splitlines())
            self._robots_memo[origin] = policy
            return policy
        try:
            status, _headers, body = self._request(origin + '/robots.txt', 'robots')
        except OSError:
            self._robots_memo[origin] = None
            return None
        if status == 404:
            self.frontier.robots_put(origin, status, '')
            policy = RobotFileParser()
            policy.parse([])
        elif 200 <= status < 300:
            text = body.decode('utf-8', errors='replace')
            self.frontier.robots_put(origin, status, text)
            policy = RobotFileParser()
            policy.parse(text.splitlines())
        else:
            policy = None
        self._robots_memo[origin] = policy
        return policy

    def run(self, limit=None):
        """推进前沿直到枯竭、达到本次上限或预算耗尽。返回运行报告。"""
        self.frontier.revalidate(self.license)
        outcomes = Counter()
        fetched = 0
        stop = 'drained'
        while True:
            if limit is not None and fetched >= limit:
                stop = 'limit'
                break
            entry = self.frontier.pop()
            if entry is None:
                stop = 'drained'
                break
            url = entry['url']
            ok, reason = self.license.check(url, entry['depth'])
            if not ok:
                self.frontier.set_state(url, 'denied', reason, self.license.version)
                outcomes[f'denied:{reason}'] += 1
                continue
            try:
                policy = self._robots(_origin(url))
            except BudgetExhausted:
                self.frontier.exhaust_remaining(self.license.version)
                stop = 'budget'
                break
            if policy is None:
                self.frontier.set_state(url, 'paused', ROBOTS_UNAVAILABLE, self.license.version)
                outcomes[f'paused:{ROBOTS_UNAVAILABLE}'] += 1
                continue
            if not policy.can_fetch(self.agent, url):
                self.frontier.set_state(url, 'denied', ROBOTS_DISALLOWED, self.license.version)
                outcomes[f'denied:{ROBOTS_DISALLOWED}'] += 1
                continue
            try:
                status, headers, body = self._request(url, 'page')
            except BudgetExhausted:
                self.frontier.exhaust_remaining(self.license.version)
                stop = 'budget'
                break
            except OSError:
                self.frontier.set_state(url, 'paused', FETCH_ERROR, self.license.version)
                outcomes[f'paused:{FETCH_ERROR}'] += 1
                continue
            fetched += 1
            location = headers.get('location')
            if status in REDIRECT_STATUSES and location:
                self.frontier.set_state(url, 'visited', REDIRECTED, self.license.version,
                                        http_status=status)
                outcomes[self.frontier.add(urljoin(url, location), entry['depth'],
                                           url, self.license)] += 1
                continue
            self.frontier.set_state(url, 'visited', FETCHED, self.license.version,
                                    http_status=status)
            if status == 200:
                parser = Links(url)
                parser.feed(body.decode('utf-8', errors='replace'))
                for target in parser.targets:
                    outcomes[self.frontier.add(target, entry['depth'] + 1,
                                               url, self.license)] += 1
        return dict(license_version=self.license.version, stop=stop, fetched=fetched,
                    requests_made=self.frontier.requests_made(),
                    outcomes=dict(outcomes), states=self.frontier.counts())
