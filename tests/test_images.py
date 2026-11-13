# -*- encoding: utf-8 -*-
"""图片理解的回归测试（离线、无网络、不碰线上状态）。

2026-11-03 线上故障的教训：
- QQ 图床的 `url` **会过期**（实测 30 分钟就返回 `retcode -5503007 download url has expired`），
  而旧实现是**回复时才下载**：当前这条消息的图（几秒内取）没事，"看看刚才那张图"
  往回翻的图基本必失败 → 用户体感就是"识图时好时坏，明明没超大小限制"。
- 取失败也不缓存 → 同一张 5MB 的图每条消息都重下一次、刷一行日志。
- 单张 >4MB 直接丢 → 群里 5~12MB 的截图全看不见。

所以这里钉四件事：收图即下载、失败负缓存、超大图降采样、缓存命中不重复下载。
"""

from __future__ import annotations

import base64
import os
import random
import socket
import tempfile
import time
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_img_tests_'))

from newGeYi.config import DEFAULT_CONFIG  # noqa: E402
from newGeYi.context.images import ImageContext, _downscale  # noqa: E402
from newGeYi.models import IncomingMessage, SessionKey  # noqa: E402

SESSION = SessionKey(bot_hash='h', platform='qq', sdk='onebot',
                     session_type='group', host_id='-', target_id='10001')
URL = 'https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=AAA&rkey=BBB'
URL2 = 'https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=CCC&rkey=DDD'


class FakeResp:
    def __init__(self, status=200, content=b'', ctype='image/png', text=''):
        self.status_code = status
        self.content = content
        self.headers = {'Content-Type': ctype}
        self.text = text
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        self.closed = True


def public_dns(host, port, family=0, kind=socket.SOCK_STREAM):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]


class FakeHTTP:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, timeout=None, *, stream=True, allow_redirects=False):
        assert stream is True and allow_redirects is False
        self.calls.append(url)
        return self.mapping.get(url, FakeResp(404, b'', text='{"retmsg":"nf"}'))

    def count(self, url):
        return sum(1 for item in self.calls if item == url)


def make_msg(raw_text: str, text: str = '【图片】', sender='2332239652', name='歌靡'):
    return IncomingMessage(
        session=SESSION, message_id='1', sender_id=sender, sender_name=name,
        text=text, raw_text=raw_text, is_group=True, mentioned_bot=False,
        event=None, proc=None, arrival_seq=1, sender_is_bot=False,
    )


def png_bytes(width: int, height: int, noise: bool = True) -> bytes:
    from io import BytesIO

    from PIL import Image

    if noise:
        rnd = random.Random(1234)
        data = bytes(rnd.getrandbits(8) for _ in range(width * height * 3))
        img = Image.frombytes('RGB', (width, height), data)
    else:
        img = Image.new('RGB', (width, height), (10, 20, 30))
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    return buffer.getvalue()


def make_ctx(mapping, **overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({'image_enabled': True, 'image_cache_seconds': 1800})
    cfg.update(overrides)
    http = FakeHTTP(mapping)
    return ImageContext(cfg, http=http, resolver=public_dns), http


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class ExtractRefsTest(unittest.TestCase):
    def test_url_file_and_base64(self):
        refs = ImageContext.extract_refs(
            '[OP:image,file=abc.jpg,url=https://x/y.jpg]')
        self.assertEqual(refs, ['https://x/y.jpg'])
        refs = ImageContext.extract_refs('[CQ:image,file=base64://QUJD]')
        self.assertEqual(refs, ['QUJD'])
        refs = ImageContext.extract_refs('[CQ:image,file=C:\\pics\\a.png]')
        self.assertEqual(refs, ['C:\\pics\\a.png'])

    def test_bare_filename_is_skipped(self):
        self.assertEqual(ImageContext.extract_refs('[CQ:image,file=abc.image]'), [])


class PrefetchTest(unittest.TestCase):
    """收图即下载：这是「只看新图」变回「能看刚才那张图」的关键。"""

    def test_remember_prefetches_into_cache(self):
        blob = png_bytes(32, 32)
        ctx, http = make_ctx({URL: FakeResp(content=blob)})
        ctx.remember(make_msg('[OP:image,file=a.jpg,url=%s]' % URL))
        self.assertTrue(wait_for(lambda: bool(ctx._cache_get(URL))), '预取没进缓存')
        self.assertEqual(http.count(URL), 1)
        data_url = ctx._to_data_url(URL)
        self.assertTrue(data_url.startswith('data:image/png;base64,'))
        # 命中缓存，不该再下一次
        self.assertEqual(http.count(URL), 1)

    def test_prefetch_does_not_block_caller(self):
        """remember() 必须在事件线程上立刻返回，不能等下载。"""
        blob = png_bytes(32, 32)

        class SlowHTTP(FakeHTTP):
            def get(self, url, timeout=None, *, stream=True, allow_redirects=False):
                time.sleep(0.6)
                return super().get(url, timeout, stream=stream, allow_redirects=allow_redirects)

        cfg = dict(DEFAULT_CONFIG)
        cfg.update({'image_enabled': True})
        ctx = ImageContext(cfg, http=SlowHTTP({URL: FakeResp(content=blob)}), resolver=public_dns)
        started = time.time()
        ctx.remember(make_msg('[OP:image,file=a.jpg,url=%s]' % URL))
        cost = time.time() - started
        self.assertLess(cost, 0.3, 'remember() 被下载拖住了（%.2fs）' % cost)
        self.assertTrue(wait_for(lambda: bool(ctx._cache_get(URL))))

    def test_expired_url_is_not_retried(self):
        """过期链接（HTTP 400）只下一次，之后走负缓存。"""
        ctx, http = make_ctx({URL2: FakeResp(
            400, b'', ctype='application/json',
            text='{"retcode":-5503007,"retmsg":"download url has expired"}')})
        ctx.remember(make_msg('[OP:image,file=a.jpg,url=%s]' % URL2))
        self.assertTrue(wait_for(lambda: ctx._is_failed(URL2)), '失败没记进负缓存')
        self.assertEqual(http.count(URL2), 1)
        self.assertEqual(ctx._to_data_url(URL2), '')
        self.assertEqual(http.count(URL2), 1, '过期链接被重复下载了')


class DownscaleTest(unittest.TestCase):
    def test_big_png_is_downscaled_under_limit(self):
        blob = png_bytes(1800, 1800, noise=True)
        self.assertGreater(len(blob), 1024 * 1024, '样本不够大，测试无意义')
        limit = 512 * 1024
        fitted = _downscale(blob, limit)
        self.assertIsNotNone(fitted, '超大图没压下来')
        data, mime = fitted
        self.assertLessEqual(len(data), limit)
        self.assertEqual(mime, 'image/jpeg')

    def test_oversize_goes_through_encode(self):
        blob = png_bytes(1600, 1600, noise=True)
        ctx, _http = make_ctx({}, image_max_bytes=300 * 1024)
        data_url = ctx._encode(URL, blob, 'image/png')
        self.assertTrue(data_url.startswith('data:image/jpeg;base64,'))
        payload = base64.b64decode(data_url.split(',', 1)[1])
        self.assertLessEqual(len(payload), 300 * 1024)

    def test_small_image_is_untouched(self):
        blob = png_bytes(32, 32, noise=False)
        ctx, _http = make_ctx({})
        data_url = ctx._encode(URL, blob, 'image/png')
        self.assertTrue(data_url.startswith('data:image/png;base64,'))
        self.assertEqual(base64.b64decode(data_url.split(',', 1)[1]), blob)


class FetchGuardTest(unittest.TestCase):
    """下载侧的硬约束（2026-11-03 审查补）：体积上限、内网地址、非图片载荷。

    以前 `_fetch` 对任何非 URL/非文件的字符串都 `b64decode(validate=False)`，
    于是"一串普通文字"也会被当成图片塞进请求体；下载也没有体积上限、
    不拦内网地址——一个异常链接就能吃满内存或借她的进程访问本机服务。
    """

    def test_non_image_base64_is_rejected(self):
        ctx, _http = make_ctx({})
        self.assertEqual(ctx._fetch('这不是图片也不是 base64，只是一句普通的中文'), (b'', ''))
        # 真 base64、但解出来不是图片 → 同样丢弃
        junk = base64.b64encode(b'hello world, this is not an image at all' * 4).decode()
        self.assertEqual(ctx._fetch(junk), (b'', ''))

    def test_image_base64_is_accepted(self):
        blob = png_bytes(16, 16, noise=False)
        ctx, _http = make_ctx({})
        payload, mime = ctx._fetch(base64.b64encode(blob).decode())
        self.assertEqual(payload, blob)
        self.assertEqual(mime, 'image/png')

    def test_local_hosts_are_blocked(self):
        ctx, http = make_ctx({})
        for url in ('http://127.0.0.1:15991/send_msg',
                    'http://localhost/secret.png',
                    'http://192.168.1.10/a.png',
                    'http://[::1]/a.png'):
            self.assertEqual(ctx._fetch(url), (b'', ''), url)
        self.assertEqual(http.calls, [], '内网地址根本不该发请求')

    def test_download_size_cap(self):
        blob = png_bytes(400, 400, noise=True)
        ctx, _http = make_ctx({URL: FakeResp(content=blob)}, image_download_max_bytes=1024)
        self.assertEqual(ctx._fetch(URL), (b'', ''), '超过上限的图没被丢掉')

    def test_non_image_content_type_is_rejected(self):
        ctx, _http = make_ctx({URL: FakeResp(content=b'<html>hi</html>',
                                             ctype='text/html')})
        self.assertEqual(ctx._fetch(URL), (b'', ''))


class PlanForTest(unittest.TestCase):
    def test_own_image_and_total_budget(self):
        small = png_bytes(24, 24, noise=False)
        raw = ('[OP:image,file=a.jpg,url=%s][OP:image,file=b.jpg,url=%s]'
               % (URL, URL2))
        ctx, _http = make_ctx({URL: FakeResp(content=small),
                               URL2: FakeResp(content=small)},
                              image_total_max_bytes=len(small) + 10,
                              image_max_bytes=len(small) + 10)
        plan = ctx.plan_for(make_msg(raw))
        # 第一张永远保留（首张豁免总量预算），第二张超预算被跳过
        self.assertEqual(plan['total'], 2)
        self.assertEqual(len(plan['urls']), 1)
        self.assertEqual(plan['skipped'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
