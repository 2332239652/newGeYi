# -*- encoding: utf-8 -*-
"""作息（休眠）：睡前提醒 + 睡着时的缄默。

为什么要有它（2026-11-06 骰主）：她 24 小时在线，凌晨三点还在秒回，太假了。

三段行为
--------
1. **睡前窗口**（`sleep_pre_minutes`，默认入睡前 30 分钟）：
   提示词里追加一段「快要去睡了」，让她自己收尾——回短、不开新话题、
   不答应陪人熬夜。这一段**照常回复**，只是语气带困意。
2. **休眠时段**（`sleep_window`，默认 23:30-07:00）：
   第一次被叫（@ 或私聊）回一句「睡着呢」，**之后完全缄默**——
   不接话、不主动搭话、不调模型（省 token，也避免半夜说话被邻居听见）。
3. 骰主指令永远不受影响：命令在 `try_handle` 里就返回了，根本走不到这一层。
   否则他自己把自己的开关锁死，只能去改 config.json。

两个容易踩的坑
--------------
- **跨午夜**：23:30-07:00 的区间必须按 `start > end` 走 `m >= start or m < end`，
  不能写成 `start <= m < end`（那样整段作息不生效，而且不会报错）。
- **提示只一次**：状态记的是「这一个睡眠段的起始时间戳」，不是日期。
  记日期的话，23:40 提示过一次、过了午夜换天，00:30 又会再提示一次。

时间一律本地时间（与 `prompt_builder.time_note` 一致），不碰 UTC。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger('newGeYi.sleep')

_MINUTES_PER_DAY = 24 * 60
_KEY_PREFIX = 'sleep.notice:'


def parse_window(raw: str) -> tuple[int, int]:
    """'23:30-07:00' → (1410, 420)；解析不了返回 (-1, -1)。

    故意和 `routing/attention._parse_window` 同格式同语义（`active_quiet_hours`
    也是这个写法）：调试台上填的是同一种串，就不该有两套解析规则。
    """
    try:
        left, right = str(raw or '').split('-', 1)
        start_h, start_m = left.strip().split(':')
        end_h, end_m = right.strip().split(':')
        start = int(start_h) * 60 + int(start_m)
        end = int(end_h) * 60 + int(end_m)
    except (ValueError, AttributeError):
        return -1, -1
    if not (0 <= start < _MINUTES_PER_DAY and 0 <= end < _MINUTES_PER_DAY):
        return -1, -1
    return start, end


def minute_of_day(now: float) -> int:
    local = time.localtime(now)
    return local.tm_hour * 60 + local.tm_min


def _midnight(now: float) -> float:
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def in_window(minute: int, start: int, end: int) -> bool:
    """分钟数落不落在 [start, end) 里；start > end 表示跨午夜。"""
    if start < 0 or end < 0 or start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def minutes_until(minute: int, target: int) -> int:
    """从当前分钟数到 target 还有多少分钟（永远取最近的一次，0~1439）。"""
    return (target - minute) % _MINUTES_PER_DAY


class SleepWindow:
    """当前该不该睡、该不该提醒、该不该出声。全部本地运算，0 调用。"""

    def __init__(self, store, config: dict):
        self.store = store
        self.config = config

    # ---------- 配置 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get('sleep_enabled', False))

    @property
    def window(self) -> tuple[int, int]:
        return parse_window(str(self.config.get('sleep_window', '23:30-07:00') or ''))

    @property
    def pre_minutes(self) -> int:
        try:
            value = int(float(self.config.get('sleep_pre_minutes', 30)))
        except (TypeError, ValueError):
            return 30
        return max(0, min(_MINUTES_PER_DAY, value))

    @property
    def notice_once(self) -> bool:
        return bool(self.config.get('sleep_notice_once', True))

    def notice_text(self) -> str:
        text = str(self.config.get('sleep_notice_text') or '').strip()
        return text or '（迷迷糊糊翻了个身）唔……已经睡下了……有事明天说嘛……'

    # ---------- 判断 ----------
    def phase(self, now: float | None = None) -> str:
        """'off' | 'awake' | 'pre' | 'asleep'。

        off 与 awake 对外都是"正常说话"，分开只是为了面板/命令能说清原因。
        """
        if not self.enabled:
            return 'off'
        start, end = self.window
        if start < 0 or start == end:
            return 'off'
        now = time.time() if now is None else float(now)
        minute = minute_of_day(now)
        if in_window(minute, start, end):
            return 'asleep'
        pre = self.pre_minutes
        if pre > 0 and minutes_until(minute, start) <= pre:
            return 'pre'
        return 'awake'

    def minutes_to_sleep(self, now: float | None = None) -> int:
        """距离入睡还有几分钟（睡着时返回 0）。"""
        if self.phase(now) == 'asleep':
            return 0
        start, _end = self.window
        if start < 0:
            return -1
        now = time.time() if now is None else float(now)
        return minutes_until(minute_of_day(now), start)

    def segment_start(self, now: float | None = None) -> int:
        """当前（或最近一个）睡眠段的起点时间戳。

        跨午夜时如果现在还没到入睡点（凌晨 2 点、入睡点是 23:30），
        说明这个睡眠段是**昨天**开始的，要减一天。
        """
        now = time.time() if now is None else float(now)
        start, _end = self.window
        if start < 0:
            return 0
        minute = minute_of_day(now)
        base = _midnight(now) + start * 60
        if minute < start:
            base -= 86400
        return int(base)

    def notice_once_now(self, session_key: str, now: float | None = None) -> bool:
        """这个会话在这个睡眠段里是不是还没提示过；是则记账并返回 True。"""
        if not self.notice_once:
            return True
        key = _KEY_PREFIX + str(session_key or '')
        try:
            current = str(self.segment_start(now))
            if str(self.store.get_setting(key) or '') == current:
                return False
            self.store.set_setting(key, current)
            return True
        except Exception:  # noqa: BLE001 - 记账失败宁可多说一句，也别整晚装死
            logger.exception('休眠提示记账失败')

        return True

    def reset_notice(self, session_key: str = '') -> None:
        """清掉"已提示过"的记录（手动叫醒用）。"""
        try:
            if session_key:
                self.store.set_setting(_KEY_PREFIX + str(session_key), '')
                return
            for key, _value in self.store.settings_with_prefix(_KEY_PREFIX):
                self.store.set_setting(str(key), '')
        except Exception:  # noqa: BLE001
            logger.exception('清除休眠提示记录失败')

    # ---------- 给提示词 ----------
    def pre_note(self, now: float | None = None) -> str:
        """睡前窗口里那段提示词；不在窗口里返回空串。"""
        if self.phase(now) != 'pre':
            return ''
        left = max(1, self.minutes_to_sleep(now))
        start, _end = self.window
        hhmm = '%02d:%02d' % (start // 60, start % 60)
        lines = [
            '【快到睡觉时间了】',
            f'再过一个多小时就到 {hhmm}，那时候你就要去睡了（大约还有 {left} 分钟）。',
            '从现在开始自然收尾：回得短一点、别再开新话题、别答应陪人熬夜、'
            '别接那种一时半会说不完的事。',
            '有人还想继续聊，你就困困地回一句「要去睡啦，明天再跟你说嘛」，'
            '不用道歉、也不用解释为什么。',
            '困了就写出来——打哈欠、句子变短、答非所问一点点都行，但别把「我要睡了」'
            '当成每句都要重复的口头禅。',
        ]
        return '\n'.join(lines)

    # ---------- 给面板/命令 ----------
    def status_text(self, now: float | None = None) -> str:
        now = time.time() if now is None else float(now)
        if not self.enabled:
            return '休眠：关（24 小时都在）'
        start, end = self.window
        if start < 0 or start == end:
            return '休眠：开，但 sleep_window 解析不了（%r），按不睡处理' % (
                self.config.get('sleep_window'),)
        span = '%02d:%02d-%02d:%02d' % (start // 60, start % 60, end // 60, end % 60)
        phase = self.phase(now)
        if phase == 'asleep':
            left = minutes_until(minute_of_day(now), end)
            state = '现在正在睡（约 %d 小时后起床）' % max(1, round(left / 60))
        elif phase == 'pre':
            state = '还有 %d 分钟就要去睡了' % self.minutes_to_sleep(now)
        else:
            state = '现在醒着（%d 分钟后入睡）' % self.minutes_to_sleep(now)
        pre = self.pre_minutes
        return '休眠：开 | %s | %s | 睡前 %d 分钟开始犯困 | 睡着时%s' % (
            span, state, pre,
            '只说一句"睡着了"' if self.notice_once else '每次被叫都应一句',
        )
