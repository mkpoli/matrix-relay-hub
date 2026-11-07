#!/usr/bin/env python3
"""Minimal in-memory Matrix application service for the integration test."""
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

with open('/service/config.json', encoding='utf-8') as f:
    config = json.load(f)
token = config['hs_token']
read_token = config['read_token']
events = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != '/events':
            self.respond(404, {})
            return
        authorization = self.headers.get('Authorization', '')
        supplied = authorization.removeprefix('Bearer ')
        if not authorization.startswith('Bearer ') or not hmac.compare_digest(supplied.encode(), read_token.encode()):
            self.respond(403, {})
            return
        self.respond(200, events)

    def do_PUT(self):
        url = urlsplit(self.path)
        supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
        if not supplied:
            supplied = parse_qs(url.query).get('access_token', [''])[0]
        if not url.path.startswith('/_matrix/app/v1/transactions/') or not hmac.compare_digest(supplied.encode(), token.encode()):
            self.respond(403, {})
            return
        length = int(self.headers.get('Content-Length', '0'))
        if length > 2_000_000:
            self.respond(413, {})
            return
        batch = json.loads(self.rfile.read(length))
        events.extend(batch.get('events', []))
        self.respond(200, {})

ThreadingHTTPServer(('0.0.0.0', 9000), Handler).serve_forever()
