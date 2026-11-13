"""F09/F10：纯离线图片入口、DNS/跳转、IP pinning 和 TLS 语义回归。"""
import base64
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_batch2_images_'))

import requests
from newGeYi.context.images import ImageContext, _ImageSession, _PinnedAdapter, host_is_local

# 有效 1x1 PNG；所有传输和 DNS 均为受控 fixture。
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')
URL = 'https://images.example.test/a.png'


def public_dns(host, port, family=0, kind=socket.SOCK_STREAM):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]


class Response:
    def __init__(self, status=200, body=PNG, **headers):
        self.status_code = status
        self.headers = {'Content-Type': 'image/png', **headers}
        self.body = body
        self.closed = False
        self.reads = 0

    @property
    def content(self):
        raise AssertionError('unbounded content access')

    @property
    def text(self):
        raise AssertionError('unbounded text access')

    def iter_content(self, chunk_size):
        self.reads += 1
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class HTTP:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs['stream'] is True
        assert kwargs['allow_redirects'] is False
        self.calls.append((url, kwargs))
        return self.mapping[url]


def context(mapping=None, resolver=public_dns, **config):
    http = HTTP(mapping or {})
    ctx = ImageContext(config, http=http, resolver=resolver)
    return ctx, http


class ImageInputsTest(unittest.TestCase):
    def test_data_and_base64_use_same_validation(self):
        ctx, _ = context()
        data = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        self.assertEqual(ctx._to_data_url(data), data)
        self.assertEqual(ctx._fetch(base64.b64encode(PNG).decode()), (PNG, 'image/png'))
        self.assertEqual(ctx._fetch('base64://' + base64.b64encode(PNG).decode()), (PNG, 'image/png'))
        self.assertEqual(ctx.extract_refs('[CQ:image,file=' + data + ']'), [data])
        for bad in ('data:text/plain;base64,' + base64.b64encode(PNG).decode(),
                    data.replace('image/png', 'image/jpeg'), data + '!',
                    data[:30] + '\n' + data[30:], 'data:image/png,hello',
                    'data:image/png;base64,' + base64.b64encode(b'hello').decode()):
            self.assertEqual(ctx._to_data_url(bad), '', bad)

    def test_input_byte_limit_precedes_decode_or_open(self):
        ctx, _ = context(image_download_max_bytes=8)
        with patch('newGeYi.context.images.base64.b64decode', side_effect=AssertionError('decode')):
            self.assertEqual(ctx._fetch('data:image/png;base64,' + 'A' * 100), (b'', ''))
        for cap in (0, -1):
            bounded, _ = context(image_download_max_bytes=cap)
            self.assertGreater(bounded._input_cap(), 0)
        self.assertEqual(ctx._encode('a', PNG, 'image/png'), '')
        self.assertEqual(ctx._encode('a', b'text', 'image/png'), '')

    def test_local_default_denies_before_open(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'a.png')
            with open(path, 'wb') as handle:
                handle.write(PNG)
            ctx, _ = context()
            with patch('builtins.open', side_effect=AssertionError('open')):
                self.assertEqual(ctx._fetch(path), (b'', ''))

    def test_local_realpath_containment_magic_and_cap(self):
        with tempfile.TemporaryDirectory() as root:
            trusted = os.path.join(root, 'adapter-cache')
            sibling = os.path.join(root, 'adapter-cache-other')
            os.mkdir(trusted)
            os.mkdir(sibling)
            path = os.path.join(trusted, 'wrong-extension.txt')
            outside = os.path.join(sibling, 'a.png')
            for item in (path, outside):
                with open(item, 'wb') as handle:
                    handle.write(PNG)
            ctx, _ = context(image_local_allowed_roots=[trusted])
            self.assertEqual(ctx._fetch(path), (PNG, 'image/png'))
            self.assertEqual(ctx._fetch(outside), (b'', ''))
            self.assertEqual(ctx._fetch(os.path.join(trusted, '..', 'adapter-cache-other', 'a.png')), (b'', ''))
            uri = 'file:///' + path.replace('\\', '/').lstrip('/')
            self.assertEqual(ctx._fetch(uri), (PNG, 'image/png'))
            self.assertEqual(ctx._fetch('file://remote/share/a.png'), (b'', ''))
            ctx2, _ = context(image_local_allowed_roots=[trusted], image_download_max_bytes=8)
            with patch('builtins.open', side_effect=AssertionError('oversize open')):
                self.assertEqual(ctx2._fetch(path), (b'', ''))
            with open(path, 'wb') as handle:
                handle.write(b'not an image')
            self.assertEqual(ctx._fetch(path), (b'', ''))

    def test_mocked_realpath_escape_denied_before_open(self):
        with tempfile.TemporaryDirectory() as root:
            trusted = os.path.join(root, 'cache')
            os.mkdir(trusted)
            ctx, _ = context(image_local_allowed_roots=[trusted])
            candidate = os.path.join(trusted, 'link.png')
            with patch('newGeYi.context.images.os.path.realpath', return_value=os.path.join(root, 'outside.png')), \
                    patch('builtins.open', side_effect=AssertionError('escaped open')):
                self.assertEqual(ctx._fetch(candidate), (b'', ''))

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as root:
            trusted = os.path.join(root, 'cache')
            os.mkdir(trusted)
            outside = os.path.join(root, 'a.png')
            with open(outside, 'wb') as handle:
                handle.write(PNG)
            link = os.path.join(trusted, 'link.png')
            try:
                os.symlink(outside, link)
            except OSError:
                self.skipTest('host does not grant symlink creation')
            ctx, _ = context(image_local_allowed_roots=[trusted])
            self.assertEqual(ctx._fetch(link), (b'', ''))


class NetworkGuardsTest(unittest.TestCase):
    def test_global_address_policy(self):
        for host in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '100.64.0.1', '0.0.0.0',
                     '::1', '::ffff:127.0.0.1', 'fe80::1', '224.0.0.1', 'localhost'):
            self.assertTrue(host_is_local(host), host)
        self.assertFalse(host_is_local('93.184.216.34'))

    def test_all_resolved_addresses_checked_before_request(self):
        for bad in ('10.0.0.1', '127.0.0.1', '100.64.0.1', '169.254.169.254'):
            def dns(host, port, *args):
                return public_dns(host, port) + [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (bad, port))]
            ctx, http = context({URL: Response()}, resolver=dns)
            self.assertEqual(ctx._fetch(URL), (b'', ''))
            self.assertEqual(http.calls, [])

    def test_redirect_checked_before_next_connection(self):
        for target in ('http://127.0.0.1/a', 'file:///a', 'ftp://images.example.test/a',
                       'https://u:p@images.example.test/a', 'https://images.example.test:0/a'):
            response = Response(302, **{'Location': target})
            ctx, http = context({URL: response})
            self.assertEqual(ctx._fetch(URL), (b'', ''))
            self.assertEqual(len(http.calls), 1)
            self.assertTrue(response.closed)

    def test_redirect_dns_changes_and_relative_redirect(self):
        first, second = Response(302, **{'Location': '/b.png'}), Response()
        ctx, http = context({URL: first, URL.replace('a.png', 'b.png'): second})
        self.assertEqual(ctx._fetch(URL), (PNG, 'image/png'))
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(first.closed and second.closed)
        calls = []
        def dns(host, port, *args):
            calls.append(host)
            return public_dns(host, port) if len(calls) == 1 else [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
        ctx, http = context({URL: Response(302, **{'Location': '/b.png'})}, resolver=dns)
        self.assertEqual(ctx._fetch(URL), (b'', ''))
        self.assertEqual(len(http.calls), 1)

    def test_size_type_errors_and_closure(self):
        cases = [Response(body=b'fake', **{'Content-Type': 'image/png'}),
                 Response(**{'Content-Type': 'image/jpeg'}),
                 Response(**{'Content-Length': '99999'}),
                 Response(**{'Content-Encoding': 'gzip'}), Response(400),
                 Response(body=PNG * 4)]
        for response in cases:
            ctx, _ = context({URL: response}, image_download_max_bytes=len(PNG) + 1)
            self.assertEqual(ctx._fetch(URL), (b'', ''))
            self.assertTrue(response.closed)
        self.assertEqual(cases[2].reads, 0)
        good = Response(**{'Content-Type': 'application/octet-stream'})
        ctx, _ = context({URL: good})
        self.assertEqual(ctx._fetch(URL), (PNG, 'image/png'))
        self.assertTrue(good.closed)

    def test_redirect_limit_and_dns_stall_bound(self):
        response = Response(302, **{'Location': URL})
        ctx, http = context({URL: response}, image_download_max_redirects=1)
        self.assertEqual(ctx._fetch(URL), (b'', ''))
        self.assertEqual(len(http.calls), 2)
        release = threading.Event()
        def stalled(*args):
            release.wait(1)
            return public_dns('a', 443)
        ctx, http = context(resolver=stalled, image_download_timeout=0.02)
        started = time.monotonic()
        self.assertEqual(ctx._fetch(URL), (b'', ''))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(http.calls, [])
        release.set()

    def test_stream_total_deadline(self):
        class Slow(Response):
            def iter_content(self, chunk_size):
                time.sleep(0.03)
                yield PNG
        response = Slow()
        ctx, _ = context({URL: response}, image_download_timeout=0.01)
        self.assertEqual(ctx._fetch(URL), (b'', ''))
        self.assertTrue(response.closed)


class PinnedTransportTest(unittest.TestCase):
    def test_missing_pinning_capability_warns_and_skips_network(self):
        with patch('newGeYi.context.images.PINNING_AVAILABLE', False), \
                self.assertLogs('newGeYi.images', level='WARNING'):
            ctx = ImageContext({}, resolver=public_dns)
        with patch('newGeYi.context.images._PinnedAdapter', side_effect=AssertionError('adapter')):
            self.assertEqual(ctx._fetch(URL), (b'', ''))

    def test_socket_uses_validated_address_without_second_dns(self):
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, ('93.184.216.34', 8443))
        adapter = _PinnedAdapter('https', 'images.example.test', 8443, address,
                                 time.monotonic() + 5, time.monotonic)
        self.addCleanup(adapter.close)
        self.assertEqual(adapter._pool.host, 'images.example.test')
        self.assertEqual(adapter._pool.assert_hostname, 'images.example.test')
        self.assertEqual(adapter._pool.conn_kw['server_hostname'], 'images.example.test')
        fake = unittest.mock.MagicMock()
        with patch('newGeYi.context.images.socket.socket', return_value=fake), \
                patch('newGeYi.context.images.socket.getaddrinfo', side_effect=AssertionError('second DNS')):
            conn = adapter._pool._new_conn()
            self.assertIs(conn._new_conn(), fake)
        fake.connect.assert_called_once_with(('93.184.216.34', 8443))
        request = requests.Request('GET', 'https://images.example.test:8443/a').prepare()
        adapter.cert_verify(adapter._pool, request.url, True, None)
        self.assertEqual(adapter._pool.cert_reqs, 'CERT_REQUIRED')
        self.assertTrue(adapter._pool.ca_certs or adapter._pool.ca_cert_dir)
        with self.assertRaises(ValueError):
            adapter.send(request, verify=False)
        with self.assertRaises(ValueError):
            adapter.send(request, proxies={'https': 'http://proxy'})

    def test_tls_connect_passes_original_sni_hostname_and_verification(self):
        import ssl
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, ('93.184.216.34', 443))
        adapter = _PinnedAdapter('https', 'images.example.test', 443, address,
                                 time.monotonic() + 5, time.monotonic)
        self.addCleanup(adapter.close)
        adapter.cert_verify(adapter._pool, URL, True, None)
        import urllib3.connection as connection
        fake = unittest.mock.MagicMock()
        if hasattr(connection, '_ssl_wrap_socket_and_match_hostname'):
            tls_name = 'urllib3.connection._ssl_wrap_socket_and_match_hostname'
            with patch('newGeYi.context.images.socket.socket', return_value=fake), \
                    patch(tls_name, return_value=(fake, True)) as wrap:
                conn = adapter._pool._new_conn()
                conn.connect()
            self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'images.example.test')
            self.assertEqual(wrap.call_args.kwargs['assert_hostname'], 'images.example.test')
            self.assertIn(wrap.call_args.kwargs['cert_reqs'], ('CERT_REQUIRED', ssl.CERT_REQUIRED))
        else:
            tls_name = 'urllib3.connection.ssl_wrap_socket'
            fake.getpeercert.return_value = {'subjectAltName': [('DNS', 'images.example.test')]}
            with patch('newGeYi.context.images.socket.socket', return_value=fake), \
                    patch(tls_name, return_value=fake) as wrap, \
                    patch('urllib3.connection._match_hostname') as match:
                conn = adapter._pool._new_conn()
                conn.connect()
            self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'images.example.test')
            self.assertEqual(wrap.call_args.kwargs['ssl_context'].verify_mode, ssl.CERT_REQUIRED)
            match.assert_called_once_with(fake.getpeercert.return_value, 'images.example.test')
            with patch.object(conn, '_new_conn', return_value=fake), \
                    patch(tls_name, return_value=fake), \
                    patch('urllib3.connection._match_hostname',
                          side_effect=ssl.SSLCertVerificationError('hostname mismatch')):
                with self.assertRaises(ssl.SSLCertVerificationError):
                    conn.connect()
        self.assertTrue(conn.is_verified)
        with patch.object(conn, '_new_conn', return_value=fake), \
                patch(tls_name, side_effect=ssl.SSLCertVerificationError('hostname mismatch')):
            with self.assertRaises(ssl.SSLCertVerificationError):
                conn.connect()

    def test_requests_disabled_redirect_does_not_read_body(self):
        class Adapter(requests.adapters.BaseAdapter):
            def send(self, request, **kwargs):
                response = requests.Response()
                response.status_code = 302
                response.headers['Location'] = 'http://127.0.0.1/blocked'
                response.raw = unittest.mock.MagicMock()
                response.raw.stream.side_effect = AssertionError('redirect body read')
                response.request = request
                response.url = request.url
                return response
            def close(self):
                pass
        session = _ImageSession()
        session.trust_env = False
        session.mount('https://', Adapter())
        self.addCleanup(session.close)
        response = session.get(URL, stream=True, allow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response._content_consumed)
        response.close()

    def test_deadline_watchdog_closes_pinned_socket(self):
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, ('93.184.216.34', 80))
        adapter = _PinnedAdapter('http', 'images.example.test', 80, address,
                                 time.monotonic() + 0.03, time.monotonic)
        self.addCleanup(adapter.close)
        closed = threading.Event()
        fake = unittest.mock.MagicMock()
        fake.close.side_effect = closed.set
        with patch('newGeYi.context.images.socket.socket', return_value=fake):
            conn = adapter._pool._new_conn()
            conn._new_conn()
        self.assertTrue(closed.wait(0.3))
        fake.shutdown.assert_called_with(socket.SHUT_RDWR)

    def test_session_disables_proxy_env_and_preserves_host_port(self):
        target = ('https', 'images.example.test', 8443,
                  (socket.AF_INET, socket.SOCK_STREAM, 6, ('93.184.216.34', 8443)))
        ctx = ImageContext({}, resolver=public_dns)
        fake = unittest.mock.MagicMock()
        with patch('newGeYi.context.images._ImageSession', return_value=fake), \
                patch('newGeYi.context.images._PinnedAdapter'):
            _, session = ctx._request_pinned('https://images.example.test:8443/a', target, time.monotonic() + 1)
        self.assertIs(session, fake)
        self.assertFalse(fake.trust_env)
        kwargs = fake.get.call_args.kwargs
        self.assertEqual(kwargs['headers']['Host'], 'images.example.test:8443')
        self.assertTrue(kwargs['verify'])
        self.assertEqual(kwargs['proxies'], {})
        self.assertFalse(kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main()
