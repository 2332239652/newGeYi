# -*- encoding: utf-8 -*-
"""群聊图片理解：把消息里的图片取出来，变成能喂给多模态模型的数据。

实测结论（2026-09 用 ginka 两个渠道验证）：
- 两个渠道都支持图片输入，能准确读出图上的文字；
- 一张 42KB 的梗图约 **400~550 prompt token**（按尺寸算，不按 base64 长度算）；
- `detail` 参数（low/high/original）**不传**，传了也不省 token。

三条硬约束：
1. **图片只能出现在 user 消息里**（放进 system/assistant 会 400）；
2. QQ 图床的链接**会过期**，而且很快 —— 2026-11-03 实测：23:54 收到的图，00:30 再取
   直接返回 `{"retcode":-5503007,"retmsg":"download url has expired"}`（30 分钟就死了）。
   所以**必须在收到消息时立刻把字节抓下来**（`_prefetch`），不能等回复时再取——
   这正是「识图时好时坏」的真凶：当前这条消息自带的图（几秒内取）没事，
   回头"看看刚才那张图"几乎必失败。
3. 图片**只跟着当前这条消息走**：历史里的图片痕迹要整个丢掉（prompt_builder.strip_image_marks），
   既不能重发图（对话越长 token 涨得越离谱），也不能留 `【图片】` 占位——
   留着模型会认定「图没加载出来」而拒绝描述（实测踩过）。

配图规则（与消息窗口一致，不区分发送者）：
- 回溯范围 = 消息窗口内的所有图（群里默认 30 分钟，见 group_live_window_seconds）；
- 只取**时间最近的 image_max_per_turn 张**（默认 3）；
- 超出的张数会以「没收到」的形式写进提示词，免得她以为对方只发了一张。

体积策略（2026-11-03 改）：
- 单张超过 `image_max_bytes`（默认 4 MB）时**不再直接丢弃**，先用 Pillow 降到 1568px 以内的 JPEG
  （群里截图动辄 5~12 MB 的 PNG 原图，但模型读细节 1568px 就够）；
- 降不下来才丢，而且**同一个 ref 只记一条日志**（以前同一张 5 MB 的图每条消息都重下一次、刷一行）。
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from collections import deque
from io import BytesIO
from urllib.parse import unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.util import Timeout
    PINNING_AVAILABLE = True
except ImportError:
    # 裁剪过的宿主缺少连接能力时保持插件加载；网络图片按失败处理。
    PINNING_AVAILABLE = False

logger = logging.getLogger('newGeYi.images')

# 下载上限与重定向上限（2026-11-03 审查：以前没有任何体积上限、也不限制内网地址，
# 一个恶意/异常链接就能把内存吃满，或者借她的进程去访问本机服务）
DEFAULT_MAX_DOWNLOAD = 12 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 3
# 收到的是 OP 码或 CQ 码都可能（取决于 message_mode），两种都认
IMAGE_CODE_RE = re.compile(r'\[(?:OP|CQ):image([^\]]*)\]')
PARAM_RE = re.compile(r'([A-Za-z_]+)=((?:(?!,[A-Za-z_]+=)[^\]])*)')
# 「这条消息在说图」——只有这种消息才允许跨发送者回溯
IMAGE_WORD_RE = re.compile(r'立绘|图|照片|截图|相片|表情包|这张|那张|这图|看|这是啥|这是什么|发了啥|发了什么|上面|评价|识别')


def sniff_image_mime(payload: bytes) -> str:
    """按魔数判断图片类型；不是图片返回空串。"""
    data = bytes(payload[:16])
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'BM'):
        return 'image/bmp'
    return ''


def host_is_local(host: str) -> bool:
    """这个主机名/字面量 IP 是否指向本机或内网（防 SSRF）。"""
    name = str(host or '').strip().strip('[]').lower()
    if not name:
        return True
    if name in ('localhost', 'localhost.localdomain') or name.endswith(
            ('.localhost', '.local', '.internal', '.lan')):
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False                      # 普通域名由 _target 检查全部 DNS 结果。
    mapped = getattr(addr, 'ipv4_mapped', None)
    if mapped is not None:
        addr = mapped
    return not addr.is_global or addr.is_multicast or addr.is_unspecified


class _ImageSession(requests.Session):
    def resolve_redirects(self, *args, **kwargs):
        # requests 即使 allow_redirects=False 也会为 Response.next 预读跳转体；禁用该路径。
        return iter(())


class _PinnedAdapter(HTTPAdapter):
    """仅本次图片请求使用；TCP 直连验证过的 sockaddr，TLS 仍验证原域名。"""

    def __init__(self, scheme, host, port, address, deadline, monotonic):
        self._deadline = deadline
        self._monotonic = monotonic
        self._sockets = []
        self._socket_lock = threading.Lock()
        self._expired = False
        owner = self

        class PinnedConnection:
            def _new_conn(self):
                family, socktype, proto, sockaddr = address
                sock = socket.socket(family, socktype, proto)
                with owner._socket_lock:
                    if owner._expired:
                        sock.close()
                        raise TimeoutError('image deadline exceeded')
                    owner._sockets.append(sock)
                try:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError('image deadline exceeded')
                    sock.settimeout(remaining)
                    for option in self.socket_options or ():
                        sock.setsockopt(*option)
                    sock.connect(sockaddr)
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError('image deadline exceeded')
                    # TLS 握手继承重算后的剩余时间，而非 connect 开始时的预算。
                    sock.settimeout(remaining)
                    return sock
                except Exception:
                    sock.close()
                    raise

            def connect(self):
                super().connect()
                # HTTPS wrap_socket 创建了新 socket 对象；到期时也要关闭它。
                with owner._socket_lock:
                    if owner._expired:
                        self.sock.close()
                        raise TimeoutError('image deadline exceeded')
                    owner._sockets.append(self.sock)

        class PinnedHTTP(PinnedConnection, HTTPConnection):
            pass

        class PinnedHTTPS(PinnedConnection, HTTPSConnection):
            pass

        super().__init__(max_retries=0)
        if scheme == 'https':
            self._pool = HTTPSConnectionPool(host, port, maxsize=1, block=True,
                                            assert_hostname=host, server_hostname=host)
            self._pool.ConnectionCls = PinnedHTTPS
        else:
            self._pool = HTTPConnectionPool(host, port, maxsize=1, block=True)
            self._pool.ConnectionCls = PinnedHTTP
        self._timer = threading.Timer(max(0.001, deadline - monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        with self._socket_lock:
            self._expired = True
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def get_connection(self, url, proxies=None):
        if proxies:
            raise ValueError('image proxy is disabled')
        return self._pool

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if verify is not True or cert is not None:
            raise ValueError('image TLS verification is required')
        return self.get_connection(request.url, proxies)

    def send(self, request, **kwargs):
        if kwargs.get('verify', True) is not True or kwargs.get('proxies'):
            raise ValueError('image TLS/proxy policy violation')
        if self._monotonic() >= self._deadline:
            raise TimeoutError('image deadline exceeded')
        return super().send(request, **kwargs)

    def close(self):
        self._timer.cancel()
        self._expire()
        self._pool.close()
        super().close()


MIME_BY_SUFFIX = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
}
SUPPORTED_MIMES = set(MIME_BY_SUFFIX.values()) | {'image/jpg'}

DEFAULT_MAX_BYTES = 4 * 1024 * 1024
# 一次附带的**总体积**上限。实测：3 张 1.1MB 的大图（base64 后 ~4.4MB 请求体）
# 会直接把请求拖到超时；3 张 115KB 的小图完全没问题。
# 所以限制「几张」不够，还得限制「多重」——超出的按「没收到」如实告诉她。
# 7MB（2026-10-30 定）：3 张 2MB 的截图勉强收得下，再多就只留前几张。
DEFAULT_TOTAL_MAX_BYTES = 7 * 1024 * 1024
DEFAULT_TIMEOUT = 10
DEFAULT_CACHE_SECONDS = 300
DEFAULT_WINDOW_SECONDS = 1800
# 每个会话记住多少条带图消息（只存来源字符串，不存图片本体）。
# 要能数出窗口内到底有多少张图，才能如实告诉她「还有几张没收到」。
RECENT_IMAGE_MESSAGES = 100

# 2026-11-03 新增：
# 内存里缓存的 data URL 总字节上限（超过就丢最旧的）。一张 ≤4MB，50 条最多 200MB，
# 这台机器光 OlivOS 就占了几个 G，必须设个硬顶。
DEFAULT_CACHE_MAX_BYTES = 64 * 1024 * 1024
# 「取不到」的负缓存：QQ 图链过期是常态，不记住的话同一条消息每轮都会重下一次（还可能 5MB）
DEFAULT_FAIL_CACHE_SECONDS = 600
# 超大图降采样的最长边梯度：够读文字，又不至于把请求撑爆
DOWNSCALE_STEPS = (1568, 1280, 1024, 800, 640)


def _downscale(payload: bytes, max_bytes: int) -> tuple[bytes, str] | None:
    """把过大的图压到 max_bytes 以内（返回 JPEG 字节）。压不下来返回 None。

    为什么要这么做：群里截图动辄 5~12 MB 的 PNG 原图，而模型能读清的细节 1568px 就够。
    以前是直接丢弃，于是她"看不见"大多数截图（线上 11-01/11-02 各丢过好几次 5~12MB）。
    Pillow 在 OlivOS 的运行时里是有的；万一没有就老实返回 None（只丢图，不炸回复）。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - 没有 Pillow 只是不能压，不影响正常回复
        logger.info('没有 Pillow，超大图只能丢弃（装个 Pillow 就能把大图压进来）')
        return None
    try:
        img = Image.open(BytesIO(payload))
        img.load()
    except Exception:  # noqa: BLE001
        return None
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    base = max(img.size) or 1
    for side in DOWNSCALE_STEPS:
        scale = min(1.0, float(side) / base)
        candidate = img if scale >= 1.0 else img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
        for quality in (85, 75, 65):
            buffer = BytesIO()
            try:
                candidate.save(buffer, format='JPEG', quality=quality, optimize=True)
            except Exception:  # noqa: BLE001
                return None
            data = buffer.getvalue()
            if len(data) <= max_bytes:
                return data, 'image/jpeg'
    return None


class ImageContext:
    """按会话记住最近收到的图，并在需要时转成可发送的 data URL。"""

    def __init__(self, config: dict, http=None, clock=time.time, *, resolver=None,
                 monotonic=time.monotonic):
        self.config = config
        if not PINNING_AVAILABLE:
            logger.warning('宿主缺少 urllib3 IP 绑定能力，跳过网络图片')
        # http 只作离线测试传输注入；生产流量始终使用私有 pinned Session。
        self._http = http
        self._resolver = resolver or socket.getaddrinfo
        self._monotonic = monotonic
        self._dns_slots = threading.BoundedSemaphore(2)
        roots = config.get('image_local_allowed_roots', [])
        self._local_roots = tuple(os.path.normcase(os.path.realpath(root)) for root in roots
                                  if isinstance(root, str) and os.path.isabs(root) and os.path.isdir(root)) \
            if isinstance(roots, (list, tuple)) else ()
        self._clock = clock
        self._lock = threading.RLock()
        # session_key -> deque[(sender_id, [refs], ts)]，只留最近几张
        self._recent: dict[str, deque] = {}
        # ref -> (data_url, ts)：下载过的图缓存起来，避免同一张反复下载
        self._cache: dict[str, tuple[str, float]] = {}
        # 2026-11-03：收图即下载（QQ 图链会过期），所以有一个后台池子
        self._prefetch_pool: ThreadPoolExecutor | None = None
        self._prefetch_lock = threading.Lock()
        self._inflight: set = set()
        # ref -> 失败时间（负缓存：别对着一个过期链接反复下 5MB）
        self._failed: dict[str, float] = {}
        # 每个 ref 只打一条日志（以前同一张图每轮刷一行）
        self._logged: set = set()

    # ==================== 解析 ====================
    @staticmethod
    def extract_refs(raw_text: str) -> list[str]:
        """从消息原文里抠出图片的可用来源（url / base64 / 本地路径）。

        认这几种形态：
            [CQ:image,file=abc.jpg,url=https://...]
            [OP:image,file=https://...]
            [CQ:image,file=base64://xxxx]
            [CQ:image,file=C:\\path\\to\\img.png]
        """
        refs: list[str] = []
        for match in IMAGE_CODE_RE.finditer(str(raw_text or '')):
            params = {key.lower(): value for key, value in PARAM_RE.findall(match.group(1))}
            for key in ('url', 'file'):
                value = str(params.get(key) or '').strip()
                if not value:
                    continue
                if value.startswith('base64://'):
                    refs.append(value[len('base64://'):])
                    break
                if value.startswith(('http://', 'https://', 'data:')) or '\\' in value or '/' in value:
                    refs.append(value)
                    break
                # 只有文件名（xxx.image）→ 拿不到内容，跳过
        return refs

    # ==================== 记忆 ====================
    def remember(self, msg) -> None:
        """消息到达时记下它的图（只记来源，等真要用了才下载）。

        必须连**发送者**一起记：后来别人说「这张图怎么样」时，图是从别人的消息里
        回溯出来的，如果只留 ref，提示词里图的归属就会错成当前说话的人。
        """
        if not self.config.get('image_enabled', True):
            return
        refs = self.extract_refs(getattr(msg, 'raw_text', '') or '')
        if not refs:
            return
        session = msg.session.as_text()
        with self._lock:
            bucket = self._recent.setdefault(session, deque(maxlen=RECENT_IMAGE_MESSAGES))
            bucket.append((
                str(msg.sender_id or ''),
                str(getattr(msg, 'sender_name', '') or ''),
                refs,
                self._clock(),
            ))
            if len(self._recent) > 200:      # 会话太多时清一点，别无限涨
                self._recent.clear()
                self._recent[session] = bucket
        # 收到就抓：QQ 图链 30 分钟就过期，等回复时再取基本已经死了
        self._prefetch(refs)

    def remember_assistant(self, session_key: str, text: str) -> None:
        """机器人自己发出图片（立绘/表情包）时，也记进最近可视图队列。

        这样群友追问「你刚才发的图是什么 / 刚才的立绘好看吗」时，她能像看到群友发的图一样，
        通过视觉模型看到自己发出的立绘。发送者记为 `assistant`，提示词里会说「你自己发的」。
        """
        if not self.config.get('image_enabled', True):
            return
        refs = self.extract_refs(str(text or ''))
        if not refs:
            return
        with self._lock:
            bucket = self._recent.setdefault(session_key, deque(maxlen=RECENT_IMAGE_MESSAGES))
            bucket.append(('assistant', '歌伊', refs, self._clock()))
            if len(self._recent) > 200:
                self._recent.clear()
                self._recent[session_key] = bucket
        self._prefetch(refs)

    def _recent_from_same_sender(self, msg) -> list[str]:
        """（已废弃，保留给旧调用方）现在统一走 collect()。"""
        return self.collect(msg)['refs']

    def _window_seconds(self, msg) -> float:
        """图片的接收窗口**跟消息窗口一致**：群里就是 group_live_window_seconds。

        可用 image_window_seconds 单独覆盖（>0 时生效），一般不需要。
        """
        override = float(self.config.get('image_window_seconds', 0) or 0)
        if override > 0:
            return override
        return float(self.config.get('group_live_window_seconds', DEFAULT_WINDOW_SECONDS)
                     or DEFAULT_WINDOW_SECONDS)

    def collect(self, msg) -> dict:
        """算出这条消息该配哪些图。

        **配图不是"窗口里最近几张"那么简单**——线上实测踩过：一个 500 人群一天收
        164 张图、图与图中位间隔 36 秒，于是「窗口内的图」几乎永远存在，
        每一次调用手里都攥着一张图，她就变成了见图就评（用户反馈「被表情包过度吸引
        注意力，即使意义不大」）。

        所以加一道**相关性闸门**，只有三种情况才配图（其余一律不配，哪怕刚有人发过）：
          ① 当前这条消息自带图 —— 图就是这条消息的内容；
          ② 消息里明确提到图（图/照片/截图/这张…）—— 它在说图，回溯窗口内最近几张；
          ③ @ 了她/叫了她名字，且图是**刚刚**发的（默认 90 秒内）—— 别人甩张图就喊她看。
        窗口本身仍与消息窗口一致（②用的就是它），只是不再无差别地往每条消息上挂。

        返回 {'items': [...], 'refs': [...], 'total': N, 'skipped': M}：
        items 里每一项是 {'ref', 'sender_id', 'sender_name', 'ts'}，
        按时间由近到远；refs 只是 items 的来源列表（向后兼容）。
        """
        if not self.config.get('image_enabled', True):
            return {'items': [], 'refs': [], 'total': 0, 'skipped': 0}
        limit = max(0, int(self.config.get('image_max_per_turn', 5) or 0))
        if limit == 0:
            return {'items': [], 'refs': [], 'total': 0, 'skipped': 0}
        window = self._window_seconds(msg)
        now = self._clock()
        with self._lock:
            bucket = list(self._recent.get(msg.session.as_text(), ()))
        items: list[dict] = []
        seen: set = set()

        def _add(ref: str, sender_id: str, sender_name: str, ts: float) -> None:
            if ref and ref not in seen:
                seen.add(ref)
                items.append({'ref': ref, 'sender_id': str(sender_id or ''),
                              'sender_name': str(sender_name or ''), 'ts': float(ts)})

        # ① 当前这条自己的图永远算数（它们就是这条消息的内容）
        cur_sender = str(getattr(msg, 'sender_id', '') or '')
        cur_name = str(getattr(msg, 'sender_name', '') or '')
        for ref in self.extract_refs(getattr(msg, 'raw_text', '') or ''):
            _add(ref, cur_sender, cur_name, now)
        if not items:
            # ②③ 只有「在说图」或「刚被叫到且图很新」才回溯
            fresh = float(self.config.get('image_fresh_seconds', 120) or 0)
            if self._talks_about_image(msg):
                reach = window
            elif getattr(msg, 'mentioned_bot', False) and fresh > 0:
                reach = fresh
            else:
                return {'items': [], 'refs': [], 'total': 0, 'skipped': 0}
            for sender_id, sender_name, refs, ts in reversed(bucket):   # 由近到远
                if (now - ts) > reach:
                    continue
                for ref in refs:                                # 同一条消息内的多张按原顺序
                    _add(ref, sender_id, sender_name, ts)
        kept = items[:limit]
        return {'items': kept, 'refs': [item['ref'] for item in kept],
                'total': len(items), 'skipped': max(0, len(items) - limit)}

    @staticmethod
    def _talks_about_image(msg) -> bool:
        """这条消息是不是在说图（「来看看这张图」这类）。"""
        return bool(IMAGE_WORD_RE.search(str(getattr(msg, 'text', '') or '')))

    @classmethod
    def is_bare_image(cls, msg) -> bool:
        """这条消息是不是「只有图、没有字」（纯表情包）。

        纯表情包不该和「有人找你说话」同权：群里刷图是常态，把它当成搭话机会
        就会变成见图就评。调用方据此把插话概率打个折。

        判断要在**原始码**上做：`msg.text` 里的图已经被转成「【图片】」占位，
        拿它去掉图片码是去不掉的（这个坑当场被测试抓到）。
        """
        raw = str(getattr(msg, 'raw_text', '') or '')
        if not IMAGE_CODE_RE.search(raw):
            return False
        return not IMAGE_CODE_RE.sub('', raw).strip()

    # ==================== 收图即下载 ====================
    def _pool(self) -> ThreadPoolExecutor:
        with self._prefetch_lock:
            if self._prefetch_pool is None:
                self._prefetch_pool = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix='geyi-img')
            return self._prefetch_pool

    def _prefetch(self, refs) -> None:
        """把还没缓存、没在下载、也没刚失败过的图丢给后台线程去抓。

        **绝不能在这里做同步 IO**：这段跑在 OlivOS 的事件线程上，
        一次 5MB 的下载就能让整个群的消息处理卡住好几秒。
        """
        if not refs:
            return
        now = self._clock()
        with self._lock:
            todo = [str(ref) for ref in refs
                    if ref and not self._cache_get_locked(str(ref), now)
                    and not self._failed_locked(str(ref), now)
                    and str(ref) not in self._inflight]
            self._inflight.update(todo)
        for ref in todo:
            try:
                self._pool().submit(self._prefetch_one, ref)
            except Exception:  # noqa: BLE001 - 池子挂了就退回懒下载
                with self._lock:
                    self._inflight.discard(ref)
                logger.warning('图片预取提交失败（%s），退回懒下载', type(ref).__name__)

    def _prefetch_one(self, ref: str) -> None:
        try:
            payload, mime = self._fetch(ref)
            if not payload:
                self._note_failed(ref)
                return
            data_url = self._encode(ref, payload, mime)
            if data_url:
                self._cache_put(ref, data_url)
            else:
                self._note_failed(ref)
        except Exception as exc:  # noqa: BLE001 - 后台线程绝不能把异常抛出去
            self._note_failed(ref)
            self._log_once(ref, '取图失败（%s）：%s' % (type(exc).__name__, str(exc)[:120]), warn=True)
        finally:
            with self._lock:
                self._inflight.discard(ref)

    def _encode(self, ref: str, payload: bytes, mime: str) -> str:
        """字节 → data URL；超过单张上限时先尝试降采样。

        以前超过 4MB 直接丢（线上一天能丢好几次 5~12MB 的截图）。现在能压就压，
        压不下来才算"没收到"。
        """
        actual = sniff_image_mime(payload)
        if not actual or actual not in SUPPORTED_MIMES or len(payload) > self._input_cap():
            return ''
        if mime and self._normal_mime(mime) != actual:
            return ''
        mime = actual
        max_bytes = max(1, int(self.config.get('image_max_bytes', DEFAULT_MAX_BYTES) or DEFAULT_MAX_BYTES))
        if len(payload) > max_bytes:
            original_kb = len(payload) / 1024
            fitted = _downscale(payload, max_bytes)
            if not fitted:
                self._log_once(ref, '图片过大且压不下来，已跳过：%.0f KB > %.0f KB'
                               % (original_kb, max_bytes / 1024))
                return ''
            payload, mime = fitted
            self._log_once(ref, '图片过大已降采样：%.0f KB → %.0f KB（上限 %.1f MB）'
                           % (original_kb, len(payload) / 1024, max_bytes / 1048576))
        return f'data:{mime};base64,' + base64.b64encode(payload).decode('ascii')

    # ==================== 取图 ====================
    def plan_for(self, msg) -> dict:
        """返回喂给 prompt 的东西：{'urls': [...], 'items': [...], 'total': N, 'skipped': M}。

        `items` 与 `urls` 一一对应，带上发送者，调用方据此在正文里写明「图几是谁发的」。

        skipped > 0 时调用方要在消息里补一句「还有几张没收到」——
        否则模型会以为对方只发了一张，把「哪张好看」这种问题答错。

        这里还会卡**总体积**：张数够但图片太大时，宁可少发几张，
        也不能把一次请求撑成超时（实测 3 张 1.1MB 的图必超时）。
        """
        plan = self.collect(msg)
        budget = int(self.config.get('image_total_max_bytes', DEFAULT_TOTAL_MAX_BYTES) or 0)
        urls: list[str] = []
        items: list[dict] = []
        used = 0
        for item in plan['items']:
            data_url = self._to_data_url(item['ref'])
            if not data_url:
                continue                       # 取不到（下载失败/单张过大）＝ 没收到
            size = len(data_url) * 3 // 4      # base64 反推原始字节，粗算即可
            # `used` 参与判断是**刻意的**：第一张不受总量预算约束。
            # 理由：宁可多花点 token，也别让用户发一张大图就彻底看不到。
            # 代价是总量预算可能被单张撑破，所以超过时留一条日志备查。
            if budget and used and used + size > budget:
                logger.info('图片总体积超预算，跳过一张（%.0f KB）', size / 1024)
                continue
            if budget and not used and size > budget:
                logger.info('单张图片超过总量预算（%.1f MB > %.1f MB），仍保留这一张',
                            size / 1048576, budget / 1048576)
            urls.append(data_url)
            items.append(item)
            used += size
        return {'urls': urls, 'items': items, 'total': plan['total'],
                'skipped': max(0, plan['total'] - len(urls))}

    def images_for(self, msg) -> list[str]:
        """只要 data URL 列表的旧接口（内部走 plan_for）。"""
        return self.plan_for(msg)['urls']

    def _to_data_url(self, ref: str) -> str:
        ref = str(ref or '').strip()
        if not ref:
            return ''
        cached = self._cache_get(ref)
        if cached:
            return cached
        # 刚失败过（多半是 QQ 图链过期）就别再下了：以前同一张 5MB 的图每条消息都重下一次
        if self._is_failed(ref):
            return ''
        try:
            payload, mime = self._fetch(ref)
        except Exception as exc:  # noqa: BLE001 - 取图失败只是没有图，不该影响回复
            self._note_failed(ref)
            self._log_once(ref, '取图失败（%s）：%s' % (type(exc).__name__, str(exc)[:120]), warn=True)
            return ''
        if not payload:
            self._note_failed(ref)
            return ''
        data_url = self._encode(ref, payload, mime)
        if not data_url:
            self._note_failed(ref)
            return ''
        self._cache_put(ref, data_url)
        return data_url

    def _input_cap(self):
        # 0/负值不再表示无限；绝对上限也约束管理员误配。
        configured = int(self.config.get('image_download_max_bytes', DEFAULT_MAX_DOWNLOAD) or 0)
        return min(configured, DEFAULT_MAX_DOWNLOAD) if configured > 0 else DEFAULT_MAX_DOWNLOAD

    @staticmethod
    def _normal_mime(mime):
        mime = str(mime or '').split(';', 1)[0].strip().lower()
        return 'image/jpeg' if mime == 'image/jpg' else mime

    def _validated(self, payload, declared=''):
        mime = sniff_image_mime(payload)
        declared = self._normal_mime(declared)
        if not payload or len(payload) > self._input_cap() or mime not in SUPPORTED_MIMES:
            return b'', ''
        if declared and declared not in ('application/octet-stream', 'binary/octet-stream') and declared != mime:
            return b'', ''
        return payload, mime

    def _decode_base64(self, encoded, declared=''):
        cap = self._input_cap()
        if not encoded or len(encoded) > 4 * ((cap + 2) // 3):
            return b'', ''
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return b'', ''
        return self._validated(payload, declared)

    def _fetch(self, ref: str) -> tuple[bytes, str]:
        """本地、data、HTTP 入口都走有限读取和魔数/MIME 校验。"""
        ref = str(ref or '').strip()
        if ref.startswith(('http://', 'https://')):
            return self._fetch_url(ref)
        if ref.startswith('data:'):
            header, sep, encoded = ref.partition(',')
            declared = header[5:-7] if header.endswith(';base64') else ''
            if not sep or self._normal_mime(declared) not in SUPPORTED_MIMES:
                return b'', ''
            return self._decode_base64(encoded, declared)
        if ref.startswith('base64://'):
            return self._decode_base64(ref[9:])
        # 仅管理员确认的 adapter 缓存目录；没有可信 root 就跳过本地引用。
        local = unquote(ref)
        if ref.startswith('file:'):
            parsed = urlparse(ref)
            if parsed.netloc or parsed.query or parsed.fragment:
                return b'', ''
            local = unquote(parsed.path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:/', local):
                local = local[1:]
        if os.path.isabs(local):
            if not self._local_roots or local.startswith(('\\\\', '//', '\\\\?\\', '\\\\.\\')):
                return b'', ''
            real = os.path.normcase(os.path.realpath(local))
            # Windows ADS 也不当作缓存文件。
            if os.name == 'nt' and ':' in os.path.splitdrive(real)[1]:
                return b'', ''
            try:
                if not any(os.path.commonpath((root, real)) == root for root in self._local_roots):
                    return b'', ''
                if not os.path.isfile(real) or os.path.getsize(real) > self._input_cap():
                    return b'', ''
                with open(real, 'rb') as handle:
                    if os.fstat(handle.fileno()).st_size > self._input_cap():
                        return b'', ''
                    payload = handle.read(self._input_cap() + 1)
                return self._validated(payload)
            except (OSError, ValueError):
                return b'', ''
        return self._decode_base64(ref)

    def _resolve(self, host, port, deadline):
        """调用方等待有界；系统 DNS 可能继续阻塞，最多留两个 daemon 查询。"""
        if not self._dns_slots.acquire(blocking=False):
            return []
        done = threading.Event()
        result = []

        def query():
            try:
                result.extend(self._resolver(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM))
            except Exception:  # DNS 失败只丢图
                pass
            finally:
                self._dns_slots.release()
                done.set()

        try:
            thread = threading.Thread(target=query, name='geyi-image-dns', daemon=True)
            thread.start()
        except Exception:
            self._dns_slots.release()
            return []
        if not done.wait(max(0, deadline - self._monotonic())):
            return []
        return result if self._monotonic() < deadline else []

    def _target(self, url, deadline):
        if any(char.isspace() or ord(char) < 32 for char in url) or '\\' in url:
            return None
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None:
                return None
            host = (parsed.hostname or '').encode('idna').decode('ascii').lower()
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
            if not 0 < port <= 65535 or '%' in host or host_is_local(host):
                return None
            records = self._resolve(host, port, deadline)
            if not records:
                return None
            addresses = []
            for family, kind, proto, _, sockaddr in records:
                if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
                    return None
                ipaddress.ip_address(sockaddr[0])  # sockaddr 必须是 IP，绝不再次解析名字。
                if sockaddr[1] != port or host_is_local(sockaddr[0]):
                    return None
                address = (family, kind, proto, sockaddr)
                if address not in addresses:
                    addresses.append(address)
            return parsed.scheme, host, port, addresses[0]
        except (ValueError, UnicodeError, IndexError, TypeError):
            return None

    def _request_pinned(self, url, target, deadline):
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError('image deadline exceeded')
        if self._http is not None:
            # 测试注入也必须接受禁跳转语义；不降级到会自动跳转的 get(url)。
            return self._http.get(url, timeout=remaining, stream=True, allow_redirects=False), None
        if not PINNING_AVAILABLE:
            raise RuntimeError('image IP pinning unavailable')
        scheme, host, port, address = target
        session = _ImageSession()
        session.trust_env = False  # 不走环境代理/netrc，防代理端再次解析域名。
        adapter = _PinnedAdapter(scheme, host, port, address, deadline, self._monotonic)
        session.mount(scheme + '://', adapter)
        authority = '[' + host + ']' if ':' in host else host
        if port != (443 if scheme == 'https' else 80):
            authority += ':' + str(port)
        try:
            response = session.get(url, headers={'Host': authority, 'Accept-Encoding': 'identity'},
                                   stream=True, allow_redirects=False, verify=True, proxies={},
                                   timeout=Timeout(total=remaining, connect=remaining, read=remaining))
            return response, session
        except Exception:
            session.close()
            raise

    def _fetch_url(self, url: str) -> tuple[bytes, str]:
        """每跳连接前验证全部 DNS 结果；下载共享 deadline、绝对字节上限。"""
        timeout = float(self.config.get('image_download_timeout', DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        timeout = min(max(timeout, 0.001), 60.0)
        deadline = self._monotonic() + timeout
        cap = self._input_cap()
        max_redirects = min(max(0, int(self.config.get('image_download_max_redirects',
                                                     DEFAULT_MAX_REDIRECTS) or 0)), DEFAULT_MAX_REDIRECTS)
        for hop in range(max_redirects + 1):
            target = self._target(url, deadline)
            if target is None or self._monotonic() >= deadline:
                return b'', ''
            resp = session = None
            try:
                resp, session = self._request_pinned(url, target, deadline)
                if self._monotonic() >= deadline:
                    return b'', ''
                headers = resp.headers or {}
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = str(headers.get('Location', '') or '')
                    if not location or hop >= max_redirects:
                        return b'', ''
                    url = urljoin(url, location)
                    continue
                if resp.status_code != 200:
                    # 不读取错误响应的 text/content；错误页也可能无限大。
                    return b'', ''
                ctype = self._normal_mime(headers.get('Content-Type', ''))
                if ctype and ctype not in SUPPORTED_MIMES | {'application/octet-stream', 'binary/octet-stream'}:
                    return b'', ''
                encoding = str(headers.get('Content-Encoding', 'identity')).strip().lower()
                if encoding not in ('', 'identity'):
                    return b'', ''  # 不让压缩流在解压器里先膨胀。
                length = headers.get('Content-Length')
                if length is not None and (int(length) < 0 or int(length) > cap):
                    return b'', ''
                payload = self._read_capped(resp, cap, deadline)
                return self._validated(payload, ctype)
            except Exception as exc:
                self._log_once(url, '图片下载失败（%s）' % type(exc).__name__)
                return b'', ''
            finally:
                try:
                    if resp is not None:
                        resp.close()
                finally:
                    if session is not None:
                        session.close()
        return b'', ''

    def _read_capped(self, resp, cap: int, deadline: float) -> bytes:
        chunks = []
        total = 0
        # 不访问 response.content/text（它们会先完整读入内存）。
        for chunk in resp.iter_content(chunk_size=min(65536, cap + 1)):
            if self._monotonic() >= deadline:
                return b''
            if not chunk:
                continue
            total += len(chunk)
            if total > cap:
                return b''
            chunks.append(chunk)
        return b''.join(chunks) if self._monotonic() < deadline else b''

    # ==================== 缓存 ====================
    def _cache_get_locked(self, ref: str, now: float | None = None) -> str:
        """持锁版本（`_prefetch` 里已经在锁里了，别再重复进锁）。"""
        ttl = float(self.config.get('image_cache_seconds', DEFAULT_CACHE_SECONDS) or 0)
        if ttl <= 0:
            return ''
        item = self._cache.get(ref)
        if not item:
            return ''
        data_url, ts = item
        if (now or self._clock()) - ts > ttl:
            self._cache.pop(ref, None)
            return ''
        return data_url

    def _failed_locked(self, ref: str, now: float | None = None) -> bool:
        ttl = float(self.config.get('image_fail_cache_seconds', DEFAULT_FAIL_CACHE_SECONDS) or 0)
        if ttl <= 0:
            return False
        ts = self._failed.get(ref)
        return bool(ts) and (now or self._clock()) - ts <= ttl

    def _is_failed(self, ref: str) -> bool:
        with self._lock:
            return self._failed_locked(ref)

    def _note_failed(self, ref: str) -> None:
        with self._lock:
            self._failed[ref] = self._clock()
            if len(self._failed) > 200:
                for key, _ in sorted(self._failed.items(), key=lambda kv: kv[1])[:100]:
                    self._failed.pop(key, None)

    def _log_once(self, ref: str, message: str, warn: bool = False) -> None:
        """同一个 ref 只记一条——同一张 5MB 的图每条消息刷一行日志，比不下更烦人。"""
        with self._lock:
            if ref in self._logged:
                return
            self._logged.add(ref)
            if len(self._logged) > 500:
                self._logged.clear()
                self._logged.add(ref)
        (logger.warning if warn else logger.info)('%s', message)

    def _cache_get(self, ref: str) -> str:
        with self._lock:
            return self._cache_get_locked(ref)

    def _cache_put(self, ref: str, data_url: str) -> None:
        ttl = float(self.config.get('image_cache_seconds', DEFAULT_CACHE_SECONDS) or 0)
        if ttl <= 0:
            return
        cap = int(self.config.get('image_cache_max_bytes', DEFAULT_CACHE_MAX_BYTES) or 0)
        with self._lock:
            self._cache[ref] = (data_url, self._clock())
            total = sum(len(item[0]) for item in self._cache.values())
            while len(self._cache) > 1 and (len(self._cache) > 50 or (cap and total > cap)):
                oldest_key, oldest = min(self._cache.items(), key=lambda kv: kv[1][1])
                total -= len(oldest[0])
                self._cache.pop(oldest_key, None)

    def clear_session(self, session_key: str) -> None:
        with self._lock:
            self._recent.pop(session_key, None)
