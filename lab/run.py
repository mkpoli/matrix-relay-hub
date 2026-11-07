#!/usr/bin/env python3
"""Run an ephemeral Synapse application-service integration test."""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE = 'matrixdotorg/synapse@sha256:b1977204551e49b7e086eb7776ab70fea0be524ecc3753f2f169d687219995b7'
LAB = Path(__file__).resolve().parent
PREFIX = 'matrix-lab-' + secrets.token_hex(8)
ROLES = ('generate', 'a', 'b', 'synapse')
temp = None
cleanup_uncertain = False

def docker(*args, timeout=45):
    global cleanup_uncertain
    read_only = args[0] in ('ps', 'inspect') or args[:2] == ('network', 'ls')
    try:
        result = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        # A daemon request may finish after its CLI has been killed.
        cleanup_uncertain = True
        raise RuntimeError('Docker operation timed out: ' + args[0]) from None
    if result.returncode:
        # A failed CLI response does not prove a daemon-side mutation did not occur.
        if not read_only:
            cleanup_uncertain = True
        # Only allow specific secret-free test diagnostics; Docker output may contain secrets.
        if args[0] == 'exec' and result.stderr:
            last = result.stderr.splitlines()[-1]
            if re.fullmatch(r'(?:MatrixHTTPError: Matrix HTTP [0-9]+ for (?:[A-Z]+|request) /[^? ]*|AssertionError: [A-Za-z0-9 :_/-]+|urllib\.error\.URLError: <urlopen error \[Errno [0-9]+\] [A-Za-z ]+>)', last):
                print(last, file=sys.stderr, flush=True)
        raise RuntimeError(f'Docker operation failed: {args[0]} (exit {result.returncode})')
    return result.stdout.strip()

def start(role, *args):
    docker('run', '-d', '--name', PREFIX + '-' + role, '--label', 'lab.run=' + PREFIX,
           '--platform', 'linux/amd64', '--pull', 'never', '--memory', '768m',
           '--memory-swap', '768m', '--pids-limit', '128', '--network', PREFIX,
           '--network-alias', role, '--user', f'{os.getuid()}:{os.getgid()}',
           '-e', 'HTTP_PROXY=', '-e', 'HTTPS_PROXY=', '-e', 'ALL_PROXY=', '-e', 'NO_PROXY=*',
           '-e', 'http_proxy=', '-e', 'https_proxy=', '-e', 'all_proxy=', '-e', 'no_proxy=*',
           *args, timeout=45)

def own_containers():
    # Filter on our unguessable run label, then require an exact run/role name and full ID.
    output = docker('ps', '-a', '--no-trunc', '--filter', 'label=lab.run=' + PREFIX,
                    '--format', '{{.ID}} {{.Names}}', timeout=30)
    found = []
    allowed = {PREFIX + '-' + role for role in ROLES}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r'[0-9a-f]{64}', parts[0]) and parts[1] in allowed:
            found.append((parts[0], parts[1]))
    return found

def own_networks():
    output = docker('network', 'ls', '--no-trunc', '--filter', 'label=lab.run=' + PREFIX,
                    '--format', '{{.ID}} {{.Name}}', timeout=30)
    found = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r'[0-9a-f]{64}', parts[0]) and parts[1] == PREFIX:
            found.append((parts[0], parts[1]))
    return found

def on_signal(signum, _frame):
    global cleanup_uncertain
    cleanup_uncertain = True
    raise KeyboardInterrupt(f'signal {signum}')

for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, on_signal)

try:
    temp = Path(tempfile.mkdtemp(prefix='.run-', dir=LAB))
    os.chmod(temp, 0o700)
    data = temp / 'synapse'
    data.mkdir(mode=0o700)
    credentials = {'password': secrets.token_urlsafe(32)}
    for name in ('a', 'b'):
        credentials[name] = {'as_token': secrets.token_urlsafe(32), 'hs_token': secrets.token_urlsafe(32),
                             'read_token': secrets.token_urlsafe(32)}
        registration = {
            'id': 'service_' + name, 'url': f'http://{name}:9000',
            'as_token': credentials[name]['as_token'], 'hs_token': credentials[name]['hs_token'],
            'sender_localpart': name + '_bot',
            'namespaces': {'users': [{'exclusive': True, 'regex': '^@' + name + '_.*:lab\\.test$'}],
                           'aliases': [], 'rooms': []},
            'rate_limited': False,
        }
        (data / (name + '.yaml')).write_text(json.dumps(registration), encoding='utf-8')
        service_config = temp / ('service-' + name + '.json')
        service_config.write_text(json.dumps({'hs_token': credentials[name]['hs_token'],
                                              'read_token': credentials[name]['read_token']}), encoding='utf-8')
        service_config.chmod(0o600)
    (data / 'secrets.json').write_text(json.dumps(credentials), encoding='utf-8')
    for file in data.iterdir():
        file.chmod(0o600)
    print('Generating ephemeral Synapse configuration', flush=True)
    docker('run', '--name', PREFIX + '-generate', '--label', 'lab.run=' + PREFIX,
           '--platform', 'linux/amd64', '--pull', 'never', '--network', 'none',
           '--memory', '512m', '--memory-swap', '512m', '--pids-limit', '128',
           '--user', f'{os.getuid()}:{os.getgid()}', '-v', f'{data}:/data',
           '-e', 'SYNAPSE_SERVER_NAME=lab.test', '-e', 'SYNAPSE_REPORT_STATS=no', IMAGE, 'generate', timeout=60)
    config = data / 'homeserver.yaml'
    contents = config.read_text(encoding='utf-8')
    def replace_setting(key, value):
        global contents
        contents = re.sub(r'^' + re.escape(key) + r':[^\n]*(?:\n(?![A-Za-z_][A-Za-z_0-9]*:)[^\n]*)*', '', contents, flags=re.M)
        contents += '\n' + key + ': ' + value + '\n'
    for key, value in {
        'listeners': '[{"port": 8008, "bind_addresses": ["0.0.0.0"], "type": "http", "tls": false, "x_forwarded": false, "resources": [{"names": ["client"], "compress": false}]}]',
        'enable_registration': 'true',
        'enable_registration_without_verification': 'true',
        'report_stats': 'false',
        'trusted_key_servers': '[]',
        'suppress_key_server_warning': 'true',
        'federation_domain_whitelist': '[]',
        'url_preview_enabled': 'false',
        'app_service_config_files': '["/data/a.yaml", "/data/b.yaml"]',
    }.items():
        replace_setting(key, value)
    config.write_text(contents, encoding='utf-8')
    print('Creating internal Docker network and three bounded containers', flush=True)
    docker('network', 'create', '--internal', '--label', 'lab.run=' + PREFIX, PREFIX)
    for role in ('a', 'b'):
        service_config = temp / ('service-' + role + '.json')
        start(role, '-v', f'{service_config}:/service/config.json:ro',
              '-v', f'{LAB / "service.py"}:/service.py:ro',
              '--entrypoint', 'python', IMAGE, '/service.py')
        mounts = json.loads(docker('inspect', '--format', '{{json .Mounts}}', PREFIX + '-' + role))
        actual = {(m['Source'], m['Destination'], m['RW']) for m in mounts}
        expected_mounts = {(str(service_config), '/service/config.json', False),
                           (str(LAB / 'service.py'), '/service.py', False)}
        if actual != expected_mounts:
            raise RuntimeError('Unexpected service mount configuration: ' + role)
    print('Each service has only its own tokens and script as read-only mounts', flush=True)
    start('synapse', '-v', f'{data}:/data', '-v', f'{LAB / "check.py"}:/check.py:ro', IMAGE)
    print('Checking real Matrix event delivery and bounded echoes', flush=True)
    result = docker('exec', PREFIX + '-synapse', 'python', '-I', '/check.py', timeout=360)
    print(result, flush=True)
except (Exception, KeyboardInterrupt) as error:
    print(f'LAB FAILED: {error}', file=sys.stderr, flush=True)
    sys.exit(1)
finally:
    # Additional interrupts must not interrupt removal already in progress.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)
    cleanup_failed = False
    try:
        containers = own_containers()
    except Exception:
        containers = []
        cleanup_failed = True
    for cid, name in containers:
        try:
            docker('rm', '-f', cid, timeout=30)
        except Exception:
            print('Failed to remove lab container ' + name, file=sys.stderr)
            cleanup_failed = True
    try:
        networks = own_networks()
    except Exception:
        networks = []
        cleanup_failed = True
    for network_id, name in networks:
        try:
            docker('network', 'rm', network_id, timeout=30)
        except Exception:
            print('Failed to remove lab network ' + name, file=sys.stderr)
            cleanup_failed = True
    if temp:
        try:
            shutil.rmtree(temp)
        except Exception:
            print('Failed to remove temporary directory ' + str(temp), file=sys.stderr)
            cleanup_failed = True
    if cleanup_failed or cleanup_uncertain:
        print('Cleanup incomplete or uncertain; inspect resources labeled lab.run=' + PREFIX, file=sys.stderr)
        if temp:
            print('Temporary directory: lab/' + temp.name, file=sys.stderr)
        sys.exit(1)
    print('Cleanup complete', flush=True)
