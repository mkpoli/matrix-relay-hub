#!/usr/bin/env python3
"""Exercise the running homeserver from inside its Docker network."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

with open('/data/secrets.json', encoding='utf-8') as f:
    secrets = json.load(f)
BASE = 'http://synapse:8008'

class MatrixHTTPError(RuntimeError):
    def __init__(self, status, method, path):
        self.status = status
        super().__init__(f'Matrix HTTP {status} for {method or "request"} {path.split("?")[0]}')

def request(base, path, data=None, token=None, method=None):
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        # Avoid including URLs, tokens, or server response bodies in diagnostics.
        raise MatrixHTTPError(e.code, method, path) from None

def matrix(path, data=None, token=None, method=None):
    return request(BASE, '/_matrix/client/v3' + path, data, token, method)

def q(value):
    return urllib.parse.quote(value, safe='')

def events(service, reader=None):
    return request('http://' + service + ':9000', '/events',
                   token=secrets[reader or service]['read_token'])

def wait_until(description, predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except MatrixHTTPError as error:
            if not 500 <= error.status < 600:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.25)
    raise AssertionError('Timed out: ' + description)

wait_until('homeserver readiness', lambda: bool(request(BASE, '/_matrix/client/versions')), 50)
for service in ('a', 'b'):
    wait_until('service readiness ' + service, lambda: isinstance(events(service), list))
normal = matrix('/register', {'username': 'normal', 'password': secrets['password'],
                               'auth': {'type': 'm.login.dummy'}})
user_token = normal['access_token']
user_id = normal['user_id']
ghosts = {}
for service in ('a', 'b'):
    auth = secrets[service]['as_token']
    result = matrix('/register', {'type': 'm.login.application_service', 'username': service + '_ghost'}, auth)
    ghosts[service] = result['user_id']
room = matrix('/createRoom', {'preset': 'private_chat', 'creation_content': {'m.federate': False}}, user_token)['room_id']
for service in ('a', 'b'):
    matrix('/rooms/' + q(room) + '/invite', {'user_id': ghosts[service]}, user_token)
    matrix('/join/' + q(room) + '?user_id=' + q(ghosts[service]), {}, secrets[service]['as_token'])

expected = {}
for name, sender, token in [('normal', user_id, user_token),
                            ('a', ghosts['a'], secrets['a']['as_token']),
                            ('b', ghosts['b'], secrets['b']['as_token'])]:
    query = '' if name == 'normal' else '?user_id=' + q(sender)
    result = matrix('/rooms/' + q(room) + '/send/m.room.message/' + name + query,
                    {'msgtype': 'm.text', 'body': 'lab message ' + name}, token, 'PUT')
    event_id = result['event_id']
    assert event_id not in expected, 'message event IDs must be distinct'
    expected[event_id] = (sender, 'lab message ' + name)

def matching(service):
    return [event for event in events(service) if event.get('event_id') in expected]

for service in ('a', 'b'):
    wait_until('three messages delivered to ' + service,
               lambda: set(e['event_id'] for e in matching(service)) == set(expected))
    received = matching(service)
    for event in received:
        sender, body = expected[event['event_id']]
        assert event.get('sender') == sender, 'wrong sender at ' + service
        assert event.get('room_id') == room, 'wrong room at ' + service
        assert event.get('type') == 'm.room.message', 'wrong event type at ' + service
        assert event.get('content', {}).get('msgtype') == 'm.text', 'wrong message type at ' + service
        assert event.get('content', {}).get('body') == body, 'wrong message body at ' + service
    print(f'{service}: received normal, a-ghost, b-ghost with expected sender and room', flush=True)

# A separate non-federated room has only the normal user and ghost A.
other = matrix('/createRoom', {'preset': 'private_chat', 'creation_content': {'m.federate': False}}, user_token)['room_id']
matrix('/rooms/' + q(other) + '/invite', {'user_id': ghosts['a']}, user_token)
matrix('/join/' + q(other) + '?user_id=' + q(ghosts['a']), {}, secrets['a']['as_token'])
private_id = matrix('/rooms/' + q(other) + '/send/m.room.message/private',
                    {'msgtype': 'm.text', 'body': 'private control'}, user_token, 'PUT')['event_id']
wait_until('private control delivered to a', lambda: any(e.get('event_id') == private_id for e in events('a')))
assert next(e for e in events('a') if e.get('event_id') == private_id)['sender'] == user_id, 'wrong private control sender'

# Both endpoints contain events, including A's private control event.
for service, other_service in (('a', 'b'), ('b', 'a')):
    try:
        events(service, other_service)
    except MatrixHTTPError as error:
        assert error.status == 403, 'wrong cross-service read rejection at ' + service
    else:
        raise AssertionError('cross-service read token accepted at ' + service)
    print(f'{service}: rejected the other service read token', flush=True)

# No synthetic service sends messages. Check for unforeseen messages/echoes after delivery.
time.sleep(5)
assert not any(e.get('room_id') == other for e in events('b')), 'b received an event from the unrelated room'
for service in ('a', 'b'):
    messages = [e for e in events(service) if e.get('room_id') == room and e.get('type') == 'm.room.message']
    assert {e.get('event_id') for e in messages} == set(expected), 'unexpected room message/echo at ' + service
    print(f'{service}: no unexpected shared-room message during 5-second observation', flush=True)
print('b: no events from a-only room during observation', flush=True)
