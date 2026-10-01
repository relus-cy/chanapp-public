"""Start a repeatable demo browser fixture; transport guards never replace business APIs.

chanapp/scripts/test_browser.sh starts this on a free port, runs the browser acceptance and
cleans up. To run it by hand, from the package parent: python -m
chanapp.tests.support.demo_offline_server --root chanapp/tmp/demo-offline-browser --port 18942. On macOS, prefix the command
with sandbox-exec -p '(version 1)(allow default)(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))' for an OS-level outbound ban.
Python socket/DNS and curl attempts are recorded without destination or credentials.
Without the optional OS sandbox, native SDK system calls are not guarded.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, default=18942)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Keep credentials inherited; the real sample initializer owns the isolated state directory.
    for key in ('CHANAPP_INSTANCE_CONFIG', 'CHANAPP_CACHE_DIR', 'WATCHLIST_PATH',
                'VIEW_LOG_PATH', 'ANALYSIS_CACHE_DIR'):
        os.environ.pop(key, None)
    from chanapp.engine.kline.seed_demo import initialize
    initialize(root / 'instance')
    os.environ.update(CHANAPP_INSTANCE_CONFIG=str(root / 'instance/instance.json'), COLLECTOR_ENABLED='1')
    attempts = root / 'outbound.jsonl'
    attempts.write_text('')

    def deny(kind):
        with attempts.open('a') as stream:
            stream.write(json.dumps({'transport': kind}) + '\n')
        raise OSError('offline browser acceptance forbids outbound transport')

    def local(host):
        if host in ('localhost', None):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    for name in ('connect', 'connect_ex'):
        original = getattr(socket.socket, name)
        def connect(self, address, _original=original):
            if isinstance(address, tuple) and not local(address[0]):
                deny('socket')
            return _original(self, address)
        setattr(socket.socket, name, connect)
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex'):
        original = getattr(socket, name)
        def resolve(host, *args, _original=original, **kwargs):
            if not local(host):
                deny('dns')
            return _original(host, *args, **kwargs)
        setattr(socket, name, resolve)
    try:
        from curl_cffi import Curl
    except ImportError:
        pass
    else:
        Curl.perform = lambda self, *args, **kwargs: deny('curl')
    import uvicorn
    uvicorn.run('chanapp.api.main:app', host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
