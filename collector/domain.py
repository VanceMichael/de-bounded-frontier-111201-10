from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag, urlsplit
from urllib.robotparser import RobotFileParser


class Links(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.targets = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'base' and 'href' in values:
            self.base = urljoin(self.base, values['href'])
        if tag == 'a' and 'href' in values:
            target = urldefrag(urljoin(self.base, values['href']))[0]
            if urlsplit(target).scheme in ('http', 'https'):
                self.targets.append(target)


def permitted(robots, url, agent='ResearchCollector'):
    policy = RobotFileParser()
    policy.parse(robots.splitlines())
    return policy.can_fetch(agent, url)


def collect(payload):
    parser = Links(payload['url'])
    parser.feed(payload['html'])
    return [dict(key=target, discovered_from=payload['url']) for target in sorted(set(parser.targets))]
