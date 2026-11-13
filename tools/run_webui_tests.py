# -*- encoding: utf-8 -*-
"""独立入口：先隔离运行目录，再发现测试，避免包导入先建生产 Runtime。"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _ensure_offline_requests_stub() -> None:
    """在未安装 requests 的裸 Python 解释器下提供离线单元测试桩。"""
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass

    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class HTTPError(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class Session:
        def __init__(self):
            self.headers = {}
            self.trust_env = True

        def request(self, *args, **kwargs):
            raise ConnectionError('offline test environment')

        def get(self, *args, **kwargs):
            return self.request('GET', *args, **kwargs)

        def post(self, *args, **kwargs):
            return self.request('POST', *args, **kwargs)

    req = types.ModuleType('requests')
    req.RequestException = RequestException
    req.Timeout = Timeout
    req.HTTPError = HTTPError
    req.ConnectionError = ConnectionError
    req.Session = Session
    req.exceptions = types.SimpleNamespace(
        RequestException=RequestException,
        Timeout=Timeout,
        HTTPError=HTTPError,
        ConnectionError=ConnectionError,
    )
    req.sessions = types.SimpleNamespace(Session=Session)
    req.get = lambda *a, **k: Session().get(*a, **k)
    req.post = lambda *a, **k: Session().post(*a, **k)
    sys.modules.setdefault('requests', req)
    sys.modules.setdefault('requests.exceptions', req.exceptions)
    sys.modules.setdefault('requests.sessions', req.sessions)


def main():
    _ensure_offline_requests_stub()
    root = Path(__file__).resolve().parents[1]
    scratch = Path(tempfile.mkdtemp(prefix='geyi_webui_tests_'))
    # 无条件覆盖；不要继承开发 shell 中可能指向生产数据的 GEYI_DATA_DIR。
    os.environ['GEYI_DATA_DIR'] = str(scratch)
    os.environ['GEYI_API_KEY'] = ''
    os.environ['GEYI_OWNER_IDS'] = ''
    for key in tuple(os.environ):
        if key.startswith('GEYI_KEY_'):
            os.environ[key] = ''
    (scratch / 'config.json').write_text('{}', encoding='utf-8')
    sys.path.insert(0, str(root.parent))
    print('Isolated test state:', scratch, flush=True)
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'), top_level_dir=str(root.parent))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('Temporary test state retained for diagnostics:', scratch)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
