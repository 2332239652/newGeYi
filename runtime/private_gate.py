# -*- encoding: utf-8 -*-
"""私聊回应的闸门：触发词 / 防连发 / 每日限额。

为什么需要（2026-11-05 骰主反馈「私聊多了并发好像有点扛不住了」）：
- 私聊以前是「每一句都回」。调度器只有 6 个 worker，被一群人同时私聊时，
  每条消息都要排队 + 一次模型调用，延迟叠起来就表现为"她不说话了"。
- 群聊有 `group_quota` 兜着，私聊一个闸都没有——补上同样性质的三道：
  ① **触发词**：可以设成"只在叫到名字/触发词时才回"，其余消息只当没看见；
  ② **防连发**：同一个人两条回复之间留最小间隔，窗口内连刷直接丢；
  ③ **每日限额**：单人每天最多回几条 + 全体私聊每天最多回几条（后者才是真正管并发的）。

设计取舍
--------
- **丢弃，不排队**：队列里排着的消息等她有空了照样要花一次模型调用，
  挡不住并发，只会把延迟变成"突然回一句十分钟前的话"。
- **在提交前记账**（`commit`），队列满/提交失败时不扣额度：
  否则"队列满被丢"会把额度白吃掉，用户到的是一条都没回、额度却没了。
- 全部状态走 `settings` 表（和 group_profile 一致），重启不丢；
  跨天自动归零，不需要定时清理。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger('newGeYi.private_gate')


def _today() -> str:
    return time.strftime('%Y%m%d')


class PrivateGate:
    """私聊三道闸。所有判断都是本地字符串/整数运算，0 延迟、0 调用。"""

    def __init__(self, store, config: dict):
        self.store = store
        self.config = config

    # ---------- 配置读取（都允许脏值） ----------
    def _num(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default) or 0)
        except (TypeError, ValueError):
            return float(default)

    def trigger_only(self) -> bool:
        return bool(self.config.get('private_trigger_only', False))

    def trigger_words(self) -> list[str]:
        words = list(self.config.get('triggers') or []) + \
            list(self.config.get('private_trigger_extra') or [])
        name = str(self.config.get('assistant_name') or '').strip()
        if name:
            words.append(name)
        out: list[str] = []
        for word in words:
            text = str(word).strip()
            if text and text not in out:      # 去重：triggers 和 assistant_name 常常是同一个词
                out.append(text)
        return out

    def hit_trigger(self, text: str) -> bool:
        body = str(text or '')
        return any(word in body for word in self.trigger_words())

    # ---------- 状态读写 ----------
    def _key(self, name: str, session_key: str) -> str:
        return 'priv.%s:%s' % (name, session_key)

    def _last_at(self, session_key: str) -> float:
        try:
            return float(self.store.get_setting(self._key('last', session_key)) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _burst(self, session_key: str) -> tuple[int, float]:
        """返回 (窗口内条数, 窗口起点)。存成 `count:start` 一行，省一次往返。"""
        raw = str(self.store.get_setting(self._key('burst', session_key)) or '')
        try:
            count, start = raw.split(':', 1)
            return int(count), float(start)
        except (ValueError, AttributeError):
            return 0, 0.0

    def _day_counter(self, key: str) -> int:
        raw = str(self.store.get_setting(key) or '')
        day, _, value = raw.partition(':')
        if day != _today():
            return 0
        try:
            return int(value or 0)
        except ValueError:
            return 0

    def used_today(self, session_key: str) -> int:
        return self._day_counter(self._day('user', session_key))

    def used_today_global(self) -> int:
        return self._day_counter('priv.gday')

    def _day(self, kind: str, session_key: str) -> str:
        return self._key('use', session_key) if kind == 'user' else 'priv.gday'

    # ---------- 判断 ----------
    def allow(self, msg) -> tuple[bool, str]:
        """能不能为这条私聊消息排一次回复。返回 (放行, 原因码)。"""
        session_key = msg.session.as_text()

        # ① 触发词模式：没叫她就只当没看见（消息仍会进记忆缓冲，她知道你在说啥）
        if self.trigger_only() and not (getattr(msg, 'mentioned_bot', False)
                                        or self.hit_trigger(getattr(msg, 'text', ''))):
            return False, 'trigger'

        # ② 防连发：两条回复之间留最小间隔
        min_interval = self._num('private_min_interval', 0)
        if min_interval > 0:
            last = self._last_at(session_key)
            if last and (time.time() - last) < min_interval:
                return False, 'interval'

        # ③ 防连发：窗口内条数超限（刷屏/连续追问）
        window = self._num('private_burst_window', 0)
        burst_max = self._num('private_burst_max', 0)
        if window > 0 and burst_max > 0:
            count, start = self._burst(session_key)
            if time.time() - start <= window and count >= burst_max:
                return False, 'burst'

        # ④ 单人每日限额。2026-11-07 起分两段：
        #   平时按 private_daily_quota；全局用量超过「总量×squeeze_at」之后，
        #   单人上限临时压到 private_quota_squeeze —— 保证晚来的人也还有得聊。
        #   为什么要这样：11-06 实测 12 个人 317 条，前两名吃掉 50.5%，
        #   全局限额一打满，后面来的人一条都排不上（不是总量不够，是分配不均）。
        user_quota = self._effective_user_quota(getattr(msg, 'sender_id', ''))
        if user_quota > 0 and self.used_today(session_key) >= user_quota:
            return False, 'quota_user'

        # ⑤ 全体私聊每日限额（真正管并发的那个闸）
        global_quota = self._num('private_daily_quota_global', 0)
        if global_quota > 0 and self.used_today_global() >= global_quota:
            return False, 'quota_global'
        return True, ''

    def squeezed(self) -> bool:
        """现在是不是处在「拥挤收紧」状态（全局用量过了阈值）。"""
        global_quota = self._num('private_daily_quota_global', 0)
        squeeze_at = self._num('private_quota_squeeze_at', 0)
        squeeze = self._num('private_quota_squeeze', 0)
        if global_quota <= 0 or squeeze <= 0 or not 0 < squeeze_at <= 1:
            return False
        return self.used_today_global() >= global_quota * squeeze_at

    def sponsor_ids(self) -> set:
        """支持者名单（爱发电赞助过的人）。

        **为什么是白名单而不是自动同步**：爱发电没有稳定的公开回调给这种小项目，
        而且"付了钱系统立刻生效"本身就把赠与变成了交易。人工加白名单既简单，
        也让这件事保持在「赞助的谢礼」这个位置上。
        """
        raw = self.config.get('sponsor_ids')
        if not isinstance(raw, (list, tuple)):
            return set()
        return {str(x).strip() for x in raw if str(x).strip()}

    def is_sponsor(self, sender_id: str) -> bool:
        return str(sender_id or '').strip() in self.sponsor_ids()

    def _effective_user_quota(self, sender_id: str = '') -> float:
        """当前生效的单人上限。0 表示不限。

        支持者的上限走 `private_daily_quota_sponsor`，而且**不受拥挤期收紧影响**——
        人家赞助是为了稳定，不能一挤就被压到 35。
        """
        quota = self._num('private_daily_quota', 0)
        sponsor = self._num('private_daily_quota_sponsor', 0)
        if sponsor > 0 and self.is_sponsor(sender_id):
            return max(quota, sponsor)
        if quota <= 0:
            return 0
        if self.squeezed():
            return min(quota, self._num('private_quota_squeeze', 0))
        return quota

    def commit(self, session_key: str) -> None:
        """真正排进队列之后才记账（提交失败不扣额度）。"""
        now = time.time()
        try:
            self.store.set_setting(self._key('last', session_key), str(now))
            window = self._num('private_burst_window', 0)
            count, start = self._burst(session_key)
            if window <= 0 or now - start > window:
                count, start = 0, now
            self.store.set_setting(self._key('burst', session_key), '%d:%.0f' % (count + 1, start))
        except Exception:  # noqa: BLE001 - 记账失败不能挡住回复
            logger.exception('私聊闸门记账失败（连发）')

        day = _today()
        try:
            used = self.used_today(session_key)
            self.store.set_setting(self._key('use', session_key), '%s:%d' % (day, used + 1))
            self.store.set_setting('priv.gday', '%s:%d' % (day, self.used_today_global() + 1))
        except Exception:  # noqa: BLE001
            logger.exception('私聊闸门记账失败（额度）')

    def reset(self, session_key: str = '') -> None:
        """清掉状态（换天/手动重置用）。不传 session 就只清全局计数。"""
        try:
            if session_key:
                for name in ('use', 'last', 'burst'):
                    self.store.set_setting(self._key(name, session_key), '')
                return
            self.store.set_setting('priv.gday', '')
        except Exception:  # noqa: BLE001
            logger.exception('私聊闸门重置失败')

    def usage_text(self, session_key: str = '') -> str:
        """给面板/命令看的一行摘要。"""
        lines = []
        uid = str(session_key).rsplit(':', 1)[-1] if session_key else ''
        quota = self._effective_user_quota(uid)
        gquota = self._num('private_daily_quota_global', 0)
        if session_key:
            used = self.used_today(session_key)
            if quota > 0:
                tags = ''
                if self.is_sponsor(uid):
                    tags = '，赞助者'
                elif self.squeezed():
                    tags = '，拥挤期已收紧'
                tip = '（限额 %d%s）' % (quota, tags)
            else:
                tip = '（未设单人限额）'
            lines.append('本会话今日私聊回复 %d 条%s' % (used, tip))
        used_global = self.used_today_global()
        if gquota > 0:
            lines.append('全体私聊今日 %d 条（限额 %d，已用 %.0f%%）%s' % (
                used_global, gquota, used_global / gquota * 100,
                ' ⚠ 拥挤期：单人上限已压到 %d' % self._num('private_quota_squeeze', 0)
                if self.squeezed() else ''))
        else:
            lines.append('全体私聊今日 %d 条（未设总限额）' % used_global)
        if self.trigger_only():
            lines.append('模式：只在命中触发词时回应（%s）' % '、'.join(self.trigger_words()))
        else:
            lines.append('模式：私聊每句都回')
        return '\n'.join(lines)
