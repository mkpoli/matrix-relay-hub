#!/usr/bin/env python3
"""Disposable, network-isolated Matrix/IRC integration check."""
import base64
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
SYNAPSE = 'matrixdotorg/synapse@sha256:b1977204551e49b7e086eb7776ab70fea0be524ecc3753f2f169d687219995b7'
BRIDGE = 'matrixdotorg/matrix-appservice-irc@sha256:c571a02af827c5bc623a5ad3551d6107318a241b182f80bacbee4a0a7b3df1f7'
ERGO = 'ghcr.io/ergochat/ergo@sha256:ef885e44f7fa19101bbbc41baef11dc280dc8107465dccaf6f0860f41b48a682'
RUN = secrets.token_hex(12)
PREFIX = 'irc-proof-' + RUN
LABEL = 'local.irc-proof.run'
NETWORK = PREFIX + '-net'
resources = {}
network_id = None
work = None
uncertain = False
stage = 'initialization'
ROLES = ('generate', 'synapse', 'irc', 'bridge')
PROXIES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
           'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')

def cmd(*args, input=None, timeout=45):
    return subprocess.run(args, input=input, text=True, check=True, capture_output=True, timeout=timeout).stdout.strip()

def docker(*args, **kw):
    return cmd('docker', *args, **kw)

def safe_error(error):
    if isinstance(error, subprocess.CalledProcessError):
        return 'process exit ' + str(error.returncode) if isinstance(error.returncode, int) else 'process failed'
    if isinstance(error, subprocess.TimeoutExpired):
        return 'process timed out'
    if isinstance(error, InterruptedError):
        return 'interrupted'
    if isinstance(error, OSError):
        return 'operating system error' + ((' errno ' + str(error.errno)) if isinstance(error.errno, int) else '')
    return type(error).__name__

MATRIX_TO_IRC_PASS = 'PASS Matrix→IRC sender=M-alice destination=#lab content=matrix-to-irc-proof'

def partial_progress(output):
    if isinstance(output, bytes):
        output = output[:8192].decode('utf-8', errors='replace')
    elif isinstance(output, str):
        output = output[:8192]
    else:
        return False
    if MATRIX_TO_IRC_PASS in output.splitlines():
        print(MATRIX_TO_IRC_PASS, flush=True)
        return True
    return False

def mutate(*args, **kw):
    global uncertain
    try:
        return docker(*args, **kw)
    except BaseException:
        uncertain = True
        raise

def interrupted(signum, _frame):
    global uncertain
    uncertain = True
    raise InterruptedError('run interrupted')

def create(role, image, *options, command=()):
    global stage
    name = PREFIX + '-' + role
    network = ('--network', 'none') if role == 'generate' else ('--network', NETWORK, '--network-alias', role)
    proxies = tuple(value for key in PROXIES for value in ('-e', key + '='))
    stage = 'create ' + role + ' container'
    cid = mutate('create', '--pull=never', '--name', name, '--label', LABEL + '=' + RUN,
                 *network, '--memory', '384m', '--memory-swap', '384m',
                 '--pids-limit', '128', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                 *proxies, *options, image, *command)
    resources[name] = cid
    stage = 'start ' + role + ' container'
    mutate('start', cid)
    return cid

def cleanup():
    # Finish reconciliation even if the user repeats an interrupt.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, signal.SIG_IGN)
    signal.alarm(0)
    ok = True
    for role in reversed(ROLES):
        name = PREFIX + '-' + role
        try:
            ids = docker('ps', '-a', '--no-trunc', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}').splitlines()
            if len(ids) > 1:
                raise RuntimeError('multiple containers')
            if ids:
                cid = ids[0]
                data = json.loads(docker('inspect', cid))[0]
                if (data['Id'] != cid or data['Name'] != '/' + name or
                    (data['Config'].get('Labels') or {}).get(LABEL) != RUN or
                    (name in resources and resources[name] != cid)):
                    raise RuntimeError('container identity mismatch')
                mutate('rm', '-f', cid, timeout=30)
                if docker('ps', '-a', '--no-trunc', '--filter', 'id=' + cid, '--format', '{{.ID}}'):
                    raise RuntimeError('container still exists')
        except Exception as e:
            ok = False
            print('Cleanup could not verify ' + name + ' (' + safe_error(e) + ')', file=sys.stderr)
    try:
        ids = docker('network', 'ls', '--no-trunc', '--filter', 'name=^' + NETWORK + '$', '--format', '{{.ID}}').splitlines()
        if len(ids) > 1:
            raise RuntimeError('multiple networks')
        if ids:
            nid = ids[0]
            data = json.loads(docker('network', 'inspect', nid))[0]
            if (data['Id'] != nid or data['Name'] != NETWORK or
                (data.get('Labels') or {}).get(LABEL) != RUN or
                (network_id is not None and network_id != nid)):
                raise RuntimeError('network identity mismatch')
            mutate('network', 'rm', nid)
            if docker('network', 'ls', '--no-trunc', '--filter', 'id=' + nid, '--format', '{{.ID}}'):
                raise RuntimeError('network still exists')
    except Exception as e:
        ok = False
        print('Cleanup could not verify ' + NETWORK + ' (' + safe_error(e) + ')', file=sys.stderr)
    if work and work.parent == ROOT and work.name.startswith('.run-' + RUN):
        try:
            shutil.rmtree(work)
        except OSError:
            ok = False
            print('Temporary data cleanup failed', file=sys.stderr)
    if ok and not uncertain:
        print('Cleanup verified for run ' + RUN, flush=True)
        return True
    print('Cleanup uncertain; inspect exact resource names and label ' + LABEL + '=' + RUN, file=sys.stderr)
    return False

SETUP = r'''
import json, urllib.request, time, sys
password=sys.stdin.readline().strip()
base='http://127.0.0.1:8008/_matrix/client/v3'
def request(method, path, body=None, token=None):
    data=None if body is None else json.dumps(body).encode()
    h={'Content-Type':'application/json'}
    if token: h['Authorization']='Bearer '+token
    with urllib.request.urlopen(urllib.request.Request(base+path, data=data, headers=h, method=method), timeout=5) as r: return json.load(r)
for i in range(60):
    try:
        request('GET','/versions')
    except Exception:
        try:
            urllib.request.urlopen('http://127.0.0.1:8008/health',timeout=2).read(); break
        except Exception: time.sleep(1)
    else: break
reg=request('POST','/register',{'username':'alice','password':password,'auth':{'type':'m.login.dummy'}})
token=reg['access_token']
room=request('POST','/createRoom',{'preset':'public_chat','visibility':'private','creation_content':{'m.federate':False}},token)['room_id']
print(json.dumps({'token':token,'room':room,'user':reg['user_id']}))
'''

TEST = r'''
import json, socket, time, urllib.request, urllib.parse, sys
p=json.loads(sys.stdin.readline()); token=p['token']; room=p['room']
base='http://127.0.0.1:8008/_matrix/client/v3'
def req(method,path,body=None):
    h={'Content-Type':'application/json','Authorization':'Bearer '+token}
    data=None if body is None else json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(base+path,data=data,headers=h,method=method),timeout=8) as r: return json.load(r)
s=socket.create_connection(('irc',6667),timeout=5); s.settimeout(1)
s.sendall(b'NICK IrcProbe\r\nUSER ircprobe 0 * :IRC Probe\r\n')
buf=b''; joined=False; sent=False; found=False; deadline=time.monotonic()+100
while time.monotonic()<deadline:
    try: data=s.recv(8192)
    except socket.timeout: continue
    if not data: raise Exception('IRC disconnected')
    buf+=data
    while b'\n' in buf:
        raw,buf=buf.split(b'\n',1); line=raw.decode(errors='replace').rstrip('\r')
        if line.startswith('PING '): s.sendall(('PONG '+line[5:]+'\r\n').encode())
        if ' 001 IrcProbe ' in line: s.sendall(b'JOIN #lab\r\n')
        if line.startswith(':IrcProbe!') and ' JOIN #lab' in line: joined=True
        if joined and not sent:
            req('PUT','/rooms/'+urllib.parse.quote(room,safe='')+'/send/m.room.message/m2i1',{'msgtype':'m.text','body':'matrix-to-irc-proof'})
            sent=True
        if ' PRIVMSG #lab :' in line and line.split(' PRIVMSG #lab :', 1)[1] == 'matrix-to-irc-proof':
            if not line.startswith(':M-alice!'): raise RuntimeError('unexpected IRC sender')
            print('PASS Matrix→IRC sender=M-alice destination=#lab content=matrix-to-irc-proof',flush=True)
            found=True; break
    if found: break
if not found: raise RuntimeError('Matrix→IRC message not observed')
sync=req('GET','/sync?timeout=0'); since=sync['next_batch']
s.sendall(b'PRIVMSG #lab :irc-to-matrix-proof\r\n')
deadline=time.monotonic()+90
while time.monotonic()<deadline:
    sync=req('GET','/sync?timeout=3000&since='+urllib.parse.quote(since,safe='')); since=sync['next_batch']
    for rid,info in sync.get('rooms',{}).get('join',{}).items():
        for event in info.get('timeline',{}).get('events',[]):
            if event.get('content',{}).get('body')=='irc-to-matrix-proof':
                if rid != room or event.get('sender') != '@irc_IrcProbe:lab.test' or event.get('type') != 'm.room.message':
                    raise RuntimeError('unexpected Matrix event sender, destination, or type')
                print('PASS IRC→Matrix sender='+event['sender']+' destination='+rid+' content=irc-to-matrix-proof',flush=True)
                s.close(); sys.exit(0)
raise Exception('IRC→Matrix message not observed')
'''

def run():
    global work, network_id, stage
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, interrupted)
    signal.alarm(420)
    print('Run label ' + LABEL + '=' + RUN + '; resource prefix ' + PREFIX, flush=True)
    # Do not initiate implicit pulls; verify all digests locally before creating anything.
    stage = 'check cached images'
    for image in (SYNAPSE, BRIDGE, ERGO):
        docker('image', 'inspect', image)
    stage = 'prepare temporary configuration'
    work = Path(tempfile.mkdtemp(prefix='.run-' + RUN + '-', dir=ROOT))
    hs = work / 'synapse'; bridge = work / 'bridge'
    hs.mkdir(); bridge.mkdir()
    a, h = secrets.token_hex(32), secrets.token_hex(32)
    registration = f'''id: irc\nurl: http://bridge:9999\nas_token: {a}\nhs_token: {h}\nsender_localpart: ircbot\nrate_limited: false\nnamespaces:\n  users:\n    - exclusive: true\n      regex: '@irc_.*:lab\\.test'\n  aliases: []\n'''
    (hs / 'registration.yaml').write_text(registration)
    (bridge / 'registration.yaml').write_text(registration)
    key = base64.urlsafe_b64encode(secrets.token_bytes(128)).rstrip(b'=').decode()
    (bridge / 'signingkey.jwk').write_text(json.dumps({'kty':'oct','k':key,'alg':'HS512','key_ops':['sign','verify'],'ext':True}))
    stage = 'create internal network'
    network_id = mutate('network', 'create', '--internal', '--label', LABEL + '=' + RUN, NETWORK)
    gen = create('generate', SYNAPSE, '--user', f'{os.getuid()}:{os.getgid()}', '--mount', f'type=bind,src={hs},dst=/data', '-e', 'SYNAPSE_SERVER_NAME=lab.test', '-e', 'SYNAPSE_REPORT_STATS=no', command=('generate',))
    stage = 'generate Synapse configuration'
    code = docker('wait', gen, timeout=40)
    if code != '0':
        # docker wait returns the container exit status as text, not a CLI error.
        raise subprocess.CalledProcessError(int(code) if code.isdecimal() else 1, 'generation')
    stage = 'configure Synapse'
    config_path = hs / 'homeserver.yaml'
    config_text = config_path.read_text()
    settings = {
        'listeners': '[{"port":8008,"bind_addresses":["0.0.0.0"],"type":"http","tls":false,"x_forwarded":false,"resources":[{"names":["client"],"compress":false}]}]',
        'app_service_config_files': '["/data/registration.yaml"]',
        'enable_registration': 'true',
        'enable_registration_without_verification': 'true',
        'report_stats': 'false',
        'trusted_key_servers': '[]',
        'suppress_key_server_warning': 'true',
        'federation_domain_whitelist': '[]',
        'url_preview_enabled': 'false',
        'rc_registration': '{"per_second":100,"burst_count":100}',
    }
    for setting, value in settings.items():
        config_text = re.sub(r'^' + re.escape(setting) + r':[^\n]*(?:\n(?![A-Za-z_][A-Za-z_0-9]*:)[^\n]*)*', '', config_text, flags=re.M)
        config_text += '\n' + setting + ': ' + value + '\n'
    config_path.write_text(config_text)
    syn = create('synapse', SYNAPSE, '--user', f'{os.getuid()}:{os.getgid()}', '--mount', f'type=bind,src={hs},dst=/data')
    irc = create('irc', ERGO, '--init', '--tmpfs', '/ircd:rw,size=32m')
    stage = 'create Matrix user and room'
    setup = json.loads(mutate('exec', '-i', syn, 'python', '-I', '-c', SETUP, input=secrets.token_urlsafe(32) + '\n', timeout=100))
    stage = 'configure bridge'
    if not re.fullmatch(r'![A-Za-z0-9]+:lab\.test', setup['room']):
        raise RuntimeError('invalid local room ID')
    config = f'''homeserver:\n  url: http://synapse:8008\n  domain: lab.test\n  enablePresence: false\n  bindPort: 9999\nircService:\n  servers:\n    irc:\n      name: LocalIRC\n      port: 6667\n      ssl: false\n      botConfig:\n        enabled: true\n        nick: MatrixBot\n        joinChannelsIfNoUsers: true\n      dynamicChannels:\n        enabled: false\n      mappings:\n        "#lab":\n          roomIds: ["{setup['room']}"]\n      membershipLists:\n        enabled: true\n        floodDelayMs: 100\n        global:\n          ircToMatrix:\n            initial: false\n            incremental: false\n          matrixToIrc:\n            initial: true\n            incremental: true\n      matrixClients:\n        userTemplate: "@irc_$NICK"\n      ircClients:\n        nickTemplate: "M-$LOCALPART"\n  logging:\n    level: info\n    toConsole: true\n  provisioning:\n    enabled: false\n  mediaProxy:\n    signingKeyPath: /data/signingkey.jwk\n    ttlSeconds: 3600\n    bindPort: 11111\n    publicUrl: http://bridge:11111\ndatabase:\n  engine: nedb\n  connectionString: "nedb:///tmp/ircbridge"\n'''
    (bridge / 'config.yaml').write_text(config)
    b = create('bridge', BRIDGE, '--user', '0:0', '--mount', f'type=bind,src={bridge / "config.yaml"},dst=/data/config.yaml,readonly', '--mount', f'type=bind,src={bridge / "registration.yaml"},dst=/data/appservice-registration-irc.yaml,readonly', '--mount', f'type=bind,src={bridge / "signingkey.jwk"},dst=/data/signingkey.jwk,readonly')
    stage = 'wait for bridge listener'
    mutate('exec', syn, 'python', '-I', '-c', "import socket,time\nfor i in range(45):\n try:\n  socket.create_connection(('bridge',9999),timeout=1).close(); break\n except OSError: time.sleep(1)\nelse: raise RuntimeError('bridge did not listen')", timeout=50)
    stage = 'Matrix→IRC forwarding'
    try:
        result = mutate('exec', '-i', syn, 'python', '-I', '-c', TEST, input=json.dumps(setup)+'\n', timeout=215)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        if partial_progress(e.stdout):
            stage = 'IRC→Matrix forwarding or Matrix sync'
        raise
    second_pass = 'PASS IRC→Matrix sender=@irc_IrcProbe:lab.test destination=' + setup['room'] + ' content=irc-to-matrix-proof'
    if result.splitlines() != [MATRIX_TO_IRC_PASS, second_pass]:
        raise RuntimeError('unexpected test output')
    print(MATRIX_TO_IRC_PASS)
    print(second_pass)
    print('Run complete; removing owned containers, network and temporary data.')

if __name__ == '__main__':
    status = 0
    try:
        run()
    except BaseException as e:
        uncertain = True
        status = 1
        print('Proof failed at ' + stage + ' (' + safe_error(e) + '); inspect the run label below.', file=sys.stderr)
    finally:
        if not cleanup():
            status = 1
    sys.exit(status)
