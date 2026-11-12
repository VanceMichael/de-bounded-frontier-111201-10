import base64
import html
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class _BlockRedirects(HTTPRedirectHandler):
    def redirect_request(self, *arguments):
        return None


class Client:
    def __init__(self, timeout=5, follow_redirects=True):
        self.timeout = timeout
        self.follow_redirects = follow_redirects

    def request(self, url, method='GET', headers=None, body=None):
        request = Request(url, data=body, headers=headers or {}, method=method)
        open_url = urlopen if self.follow_redirects else build_opener(_BlockRedirects).open
        try:
            response = open_url(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read()


class MappedClient:
    """把许可 origin 映射到本地镜像；策略判定始终针对原始许可地址。"""

    def __init__(self, client, mapping):
        self.client = client
        self.mapping = dict(mapping)

    def request(self, url, **kwargs):
        for origin, base in sorted(self.mapping.items(), key=lambda item: len(item[0]), reverse=True):
            if url == origin or url.startswith(origin + '/'):
                url = base.rstrip('/') + url[len(origin):]
                break
        return self.client.request(url, **kwargs)


def mirror_routes(site_map):
    """由站点地图构造本地镜像的响应脚本：页面转为链接列表，跳转转为 302。"""
    routes = {}
    for path, links in site_map.get('pages', {}).items():
        body = ''.join(f'<a href="{html.escape(link, quote=True)}">链接</a>' for link in links)
        routes[path] = [dict(body=body, headers={'Content-Type': 'text/html; charset=utf-8'})]
    for path, target in site_map.get('redirects', {}).items():
        routes[path] = [dict(status=302, headers={'Location': target})]
    return routes


@contextmanager
def replay_server(routes):
    positions = {}
    calls = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return None

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            with lock:
                calls.append(dict(method=self.command, path=self.path, headers=dict(self.headers), body=body.decode('utf-8', errors='replace')))
                replies = routes.get(self.path, [dict(status=404, body='未找到资源')])
                index = positions.get(self.path, 0)
                reply = replies[min(index, len(replies) - 1)]
                positions[self.path] = index + 1
            data = base64.b64decode(reply['base64']) if 'base64' in reply else reply.get('body', '').encode('utf-8')
            self.send_response(reply.get('status', 200))
            for name, value in reply.get('headers', {}).items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
