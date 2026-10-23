# -*- encoding: utf-8 -*-
"""群聊图片理解：把消息里的图片取出来，变成能喂给多模态模型的数据。

实测结论（2026-09 用 ginka 两个渠道验证）：
- 两个渠道都支持图片输入，能准确读出图上的文字；
- 一张 42KB 的梗图约 **400~550 prompt token**（按尺寸算，不按 base64 长度算）；
- `detail` 参数（low/high/original）**被忽略**，传了也不省 token，所以不发。

三条硬约束：
1. **图片只能出现在 user 消息里**（放进 system/assistant 会 400）；
2. QQ 图床的链接有时效与防盗链，交给服务商去抓不可靠 → **自己下载后转 base64 内联**；
3. 图片**只跟着当前这条消息走**：历史里的图片痕迹要整个丢掉（prompt_builder.strip_image_marks），
   既不能重发图（对话越长 token 涨得越离谱），也不能留 `【图片】` 占位——
   留着模型会认定「图没加载出来」而拒绝描述（实测踩过）。

配图规则（与消息窗口一致，不区分发送者）：
- 回溯范围 = 消息窗口内的所有图（群里默认 30 分钟，见 group_live_window_seconds）；
- 只取**时间最近的 image_max_per_turn 张**（默认 3）；
- 超出的张数会以「没收到」的形式写进提示词，免得她以为对方只发了一张。
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from collections import deque

import requests

logger = logging.getLogger('newGeYi.images')

# 收到的是 OP 码或 CQ 码都可能（取决于 message_mode），两种都认
IMAGE_CODE_RE = re.compile(r'\[(?:OP|CQ):image([^\]]*)\]')
PARAM_RE = re.compile(r'([A-Za-z_]+)=([^,\]]*)')
# 「这条消息在说图」——只有这种消息才允许跨发送者回溯
IMAGE_WORD_RE = re.compile(r'立绘|图|照片|截图|相片|表情包|这张|那张|这图|看|这是啥|这是什么|发了啥|发了什么|上面|评价|识别')

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
DEFAULT_TOTAL_MAX_BYTES = 1536 * 1024
DEFAULT_TIMEOUT = 10
DEFAULT_CACHE_SECONDS = 300
DEFAULT_WINDOW_SECONDS = 1800
# 每个会话记住多少条带图消息（只存来源字符串，不存图片本体）。
# 要能数出窗口内到底有多少张图，才能如实告诉她「还有几张没收到」。
RECENT_IMAGE_MESSAGES = 100


def _guess_mime(source: str, content_type: str = '') -> str:
    """按 Content-Type 优先、其次按后缀猜 MIME。"""
    ctype = (content_type or '').split(';')[0].strip().lower()
    if ctype in SUPPORTED_MIMES:
        return 'image/jpeg' if ctype == 'image/jpg' else ctype
    lowered = str(source or '').lower().split('?')[0]
    for suffix, mime in MIME_BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return mime
    return 'image/jpeg'


class ImageContext:
    """按会话记住最近收到的图，并在需要时转成可发送的 data URL。"""

    def __init__(self, config: dict, http=None, clock=time.time):
        self.config = config
        self._http = http or requests
        self._clock = clock
        self._lock = threading.RLock()
        # session_key -> deque[(sender_id, [refs], ts)]，只留最近几张
        self._recent: dict[str, deque] = {}
        # ref -> (data_url, ts)：下载过的图缓存起来，避免同一张反复下载
        self._cache: dict[str, tuple[str, float]] = {}

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
        """消息到达时记下它的图（只记来源，等真要用了才下载）。"""
        if not self.config.get('image_enabled', True):
            return
        refs = self.extract_refs(getattr(msg, 'raw_text', '') or '')
        if not refs:
            return
        session = msg.session.as_text()
        with self._lock:
            bucket = self._recent.setdefault(session, deque(maxlen=RECENT_IMAGE_MESSAGES))
            bucket.append((str(msg.sender_id or ''), refs, self._clock()))
            if len(self._recent) > 200:      # 会话太多时清一点，别无限涨
                self._recent.clear()
                self._recent[session] = bucket

    def remember_assistant(self, session_key: str, text: str) -> None:
        """机器人自己发出图片（立绘/表情包）时，也记进最近可视图队列。

        这样群友追问「你刚才发的图是什么 / 刚才的立绘好看吗」时，她能像看到群友发的图一样，
        通过视觉模型看到自己发出的立绘。
        """
        if not self.config.get('image_enabled', True):
            return
        refs = self.extract_refs(str(text or ''))
        if not refs:
            return
        with self._lock:
            bucket = self._recent.setdefault(session_key, deque(maxlen=RECENT_IMAGE_MESSAGES))
            bucket.append(('assistant', refs, self._clock()))
            if len(self._recent) > 200:
                self._recent.clear()
                self._recent[session_key] = bucket

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

        返回 {'refs': [...], 'total': N, 'skipped': M}，refs 按时间由近到远。
        """
        if not self.config.get('image_enabled', True):
            return {'refs': [], 'total': 0, 'skipped': 0}
        limit = max(0, int(self.config.get('image_max_per_turn', 3) or 0))
        if limit == 0:
            return {'refs': [], 'total': 0, 'skipped': 0}
        window = self._window_seconds(msg)
        now = self._clock()
        with self._lock:
            bucket = list(self._recent.get(msg.session.as_text(), ()))
        ordered: list[str] = []
        seen: set = set()

        def _add(ref: str) -> None:
            if ref and ref not in seen:
                seen.add(ref)
                ordered.append(ref)

        # ① 当前这条自己的图永远算数（它们就是这条消息的内容）
        for ref in self.extract_refs(getattr(msg, 'raw_text', '') or ''):
            _add(ref)
        if not ordered:
            # ②③ 只有「在说图」或「刚被叫到且图很新」才回溯
            fresh = float(self.config.get('image_fresh_seconds', 90) or 0)
            if self._talks_about_image(msg):
                reach = window
            elif getattr(msg, 'mentioned_bot', False) and fresh > 0:
                reach = fresh
            else:
                return {'refs': [], 'total': 0, 'skipped': 0}
            for _owner, refs, ts in reversed(bucket):          # 由近到远
                if (now - ts) > reach:
                    continue
                for ref in refs:                                # 同一条消息内的多张按原顺序
                    _add(ref)
        return {'refs': ordered[:limit], 'total': len(ordered),
                'skipped': max(0, len(ordered) - limit)}

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

    # ==================== 取图 ====================
    def plan_for(self, msg) -> dict:
        """返回喂给 prompt 的东西：{'urls': [...], 'total': N, 'skipped': M}。

        skipped > 0 时调用方要在消息里补一句「还有几张没收到」——
        否则模型会以为对方只发了一张，把「哪张好看」这种问题答错。

        这里还会卡**总体积**：张数够但图片太大时，宁可少发几张，
        也不能把一次请求撑成超时（实测 3 张 1.1MB 的图必超时）。
        """
        plan = self.collect(msg)
        budget = int(self.config.get('image_total_max_bytes', DEFAULT_TOTAL_MAX_BYTES) or 0)
        urls: list[str] = []
        used = 0
        for ref in plan['refs']:
            data_url = self._to_data_url(ref)
            if not data_url:
                continue                       # 取不到（下载失败/单张过大）＝ 没收到
            size = len(data_url) * 3 // 4      # base64 反推原始字节，粗算即可
            if budget and used and used + size > budget:
                logger.info('图片总体积超预算，跳过一张（%.0f KB）', size / 1024)
                continue
            urls.append(data_url)
            used += size
        return {'urls': urls, 'total': plan['total'],
                'skipped': max(0, plan['total'] - len(urls))}

    def images_for(self, msg) -> list[str]:
        """只要 data URL 列表的旧接口（内部走 plan_for）。"""
        return self.plan_for(msg)['urls']

    def _to_data_url(self, ref: str) -> str:
        ref = str(ref or '').strip()
        if not ref:
            return ''
        if ref.startswith('data:'):
            return ref
        cached = self._cache_get(ref)
        if cached:
            return cached
        try:
            payload, mime = self._fetch(ref)
        except Exception as exc:  # noqa: BLE001 - 取图失败只是没有图，不该影响回复
            logger.warning('取图失败（%s）：%s', type(exc).__name__, str(exc)[:120])
            return ''
        if not payload:
            return ''
        max_bytes = int(self.config.get('image_max_bytes', DEFAULT_MAX_BYTES) or DEFAULT_MAX_BYTES)
        if len(payload) > max_bytes:
            logger.info('图片过大已跳过：%.1f KB > %.1f KB', len(payload) / 1024, max_bytes / 1024)
            return ''
        data_url = f'data:{mime};base64,' + base64.b64encode(payload).decode('ascii')
        self._cache_put(ref, data_url)
        return data_url

    def _fetch(self, ref: str) -> tuple[bytes, str]:
        """把一个来源取成 (字节, MIME)。"""
        if ref.startswith(('http://', 'https://')):
            timeout = float(self.config.get('image_download_timeout', DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
            resp = self._http.get(ref, timeout=timeout)
            if getattr(resp, 'status_code', 200) != 200:
                return b'', ''
            mime = _guess_mime(ref, resp.headers.get('Content-Type', ''))
            return resp.content or b'', mime
        # 本地路径（支持跨平台 file:/// 协议与常规绝对路径）
        import os
        import urllib.parse
        import urllib.request
        local = ref
        if local.startswith('file://'):
            parsed = urllib.parse.urlparse(local)
            local = urllib.request.url2pathname(parsed.path)
            if os.name == 'nt' and local.startswith('\\') and len(local) > 2 and local[2] == ':':
                local = local[1:]
        else:
            local = urllib.parse.unquote(local)
        if os.path.isfile(local):
            with open(local, 'rb') as handle:
                return handle.read(), _guess_mime(local)
        if os.path.isfile(ref):
            with open(ref, 'rb') as handle:
                return handle.read(), _guess_mime(ref)
        # 裸 base64
        try:
            payload = base64.b64decode(ref, validate=False)
        except Exception:  # noqa: BLE001
            return b'', ''
        return payload, _guess_mime('')

    # ==================== 缓存 ====================
    def _cache_get(self, ref: str) -> str:
        ttl = float(self.config.get('image_cache_seconds', DEFAULT_CACHE_SECONDS) or 0)
        if ttl <= 0:
            return ''
        with self._lock:
            item = self._cache.get(ref)
            if not item:
                return ''
            data_url, ts = item
            if self._clock() - ts > ttl:
                self._cache.pop(ref, None)
                return ''
            return data_url

    def _cache_put(self, ref: str, data_url: str) -> None:
        ttl = float(self.config.get('image_cache_seconds', DEFAULT_CACHE_SECONDS) or 0)
        if ttl <= 0:
            return
        with self._lock:
            self._cache[ref] = (data_url, self._clock())
            if len(self._cache) > 50:
                oldest = sorted(self._cache.items(), key=lambda kv: kv[1][1])[:20]
                for key, _ in oldest:
                    self._cache.pop(key, None)

    def clear_session(self, session_key: str) -> None:
        with self._lock:
            self._recent.pop(session_key, None)
