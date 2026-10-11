"""Install before importing any application module. Never imports the real session."""
import importlib.abc
import os
import socket
import sys
import types

BLOCKED = []
def deny(*args, **kwargs):
    BLOCKED.append('network_or_broker_session')
    raise RuntimeError('offline replay: network and broker session unavailable')

class BlockBrokerSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('SmartApi', 'smartapi', 'smartapi_python', 'pyotp'):
            raise ImportError('broker SDK forbidden in offline work')

def install():
    for key in list(os.environ):
        if key.startswith(('ANGEL_', 'SMARTAPI_', 'TELEGRAM_', 'MARKETAUX_')):
            os.environ.pop(key, None)
    os.environ.pop('KADIMA_INTRADAY_TREND_LIVE', None)
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.socket.sendto = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny
    sys.meta_path.insert(0, BlockBrokerSDK())
    module = types.ModuleType('kadima.session')
    class Session:
        api = None
        jwt = refresh_token = feed_token = None
        ensure = _login = login = logout = deny
    module.Session = Session
    module.SESSION = Session()
    sys.modules['kadima.session'] = module
