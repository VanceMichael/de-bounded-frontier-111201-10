import argparse
import json
from pathlib import Path
import tempfile

from .domain import collect
from .frontier import Crawler, Frontier, License
from .storage import Store
from .transport import Client, MappedClient, mirror_routes, replay_server


def _crawl(arguments):
    license = License.load(arguments.license, version=arguments.license_version)
    frontier = Frontier(arguments.db)
    try:
        frontier.seed(license)
        client = Client(follow_redirects=False)
        if arguments.mirror:
            client = MappedClient(client, {origin: arguments.mirror for origin in license.origins})
        report = Crawler(frontier, license, client).run(limit=arguments.limit)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        frontier.close()


def _frontier(arguments):
    frontier = Frontier(arguments.db)
    try:
        print(json.dumps(dict(
            requests_made=frontier.requests_made(),
            states=frontier.counts(),
            entries=frontier.entries(state=arguments.state),
        ), ensure_ascii=False, indent=2))
    finally:
        frontier.close()


def _crawl_demo(arguments):
    """本地镜像演示：v1 许可下限量运行，重启后按 v2 许可继续，
    以前沿状态与镜像请求记录证明拒绝地址从未被请求。"""
    site_map = json.loads(Path(arguments.site_map).read_text(encoding='utf-8'))
    routes = mirror_routes(site_map)
    routes['/robots.txt'] = [dict(body='User-agent: *\nAllow: /\n')]
    with tempfile.TemporaryDirectory() as directory, replay_server(routes) as (base, calls):
        database = Path(directory) / 'frontier.sqlite3'
        stages = []
        for version, limit in ((1, 1), (2, None)):
            license = License.load(arguments.license, version=version)
            frontier = Frontier(database)
            try:
                frontier.seed(license)
                client = MappedClient(Client(follow_redirects=False),
                                      {origin: base for origin in license.origins})
                stages.append(Crawler(frontier, license, client).run(limit=limit))
                if limit is None:
                    entries = frontier.entries()
                    fetches = frontier.fetches()
            finally:
                frontier.close()
        print(json.dumps(dict(
            stages=stages,
            frontier=entries,
            fetches=fetches,
            site_requests=[dict(method=call['method'], path=call['path']) for call in calls],
        ), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description='本地采集与凭据查看')
    parser.add_argument('command', choices=['demo', 'import', 'show', 'serve',
                                            'crawl', 'frontier', 'crawl-demo'])
    parser.add_argument('--input', default='fixtures/sample.json')
    parser.add_argument('--db', default='var/records.sqlite3')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--license', default='fixtures/access.json')
    parser.add_argument('--license-version', type=int, default=None)
    parser.add_argument('--site-map', default='fixtures/site-map.json')
    parser.add_argument('--mirror', default=None)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--state', default=None)
    arguments = parser.parse_args()
    if arguments.command == 'serve':
        from .api import serve
        serve(arguments.db, arguments.port)
        return
    if arguments.command == 'crawl':
        _crawl(arguments)
        return
    if arguments.command == 'frontier':
        _frontier(arguments)
        return
    if arguments.command == 'crawl-demo':
        _crawl_demo(arguments)
        return
    if arguments.command == 'demo':
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(arguments.input).read_bytes()
            with replay_server({'/source': [dict(body=payload.decode('utf-8'))]}) as (base, calls):
                status, headers, raw = Client().request(base + '/source')
                if status != 200:
                    raise RuntimeError('本地来源未返回成功响应')
                store = Store(Path(directory) / 'demo.sqlite3')
                rows = collect(json.loads(raw))
                digest = store.ingest('本地演示来源', raw, rows)
                print(json.dumps(dict(digest=digest, records=store.records(), requests=len(calls)), ensure_ascii=False, indent=2))
                store.close()
        return
    Path(arguments.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(arguments.db)
    try:
        if arguments.command == 'import':
            raw = Path(arguments.input).read_bytes()
            store.ingest(str(Path(arguments.input)), raw, collect(json.loads(raw)))
        print(json.dumps(store.records(), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
