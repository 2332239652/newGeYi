# -*- encoding: utf-8 -*-
"""命令路由：权限在命令表里声明，不散落在每个 if 分支。"""

from __future__ import annotations

import copy
import logging
import os
import re
import threading
import time

from .. import config as plugin_config
from ..config import SKIP_ACTIVE
from ..context import aliases
from ..memory.exporter import export_memory
from ..memory.repository import SHARE_OVERRIDE_PREFIX
from ..memory.worldbook import is_story_mode, list_story_mode_users, set_story_mode
from ..models import IncomingMessage
from ..presentation.renderer import load_emote_table  # noqa: F401  （保留导出）
from ..runtime import sponsors

logger = logging.getLogger('newGeYi.commands')


def _mask_key(key: str) -> str:
    """回显时只显示掩码，明文不落聊天记录。"""
    key = key.strip()
    if len(key) <= 8:
        return '****'
    return key[:4] + '****' + key[-4:]


# 用量拆账用的中文标签（kind 是网关口径，别在别处再写一遍字面量）
KIND_LABELS = {
    'gate': '搭话判断',
    'active': '主动回复',
    'passive': '被动回复',
    'memory_curate': '记忆提炼',
    'affection': '好感评审',
    'chat': '聊天',
}

# 按会话维度拆账的标签
SCOPE_LABELS = {'group': '群聊', 'private': '私聊', 'other': '其它'}


def fmt_tokens(value) -> str:
    """token 数带上千分位。状态行里数字动辄六位，不加分隔符看着像瞎写。"""
    try:
        return format(int(value or 0), ',')
    except (TypeError, ValueError):
        return str(value)


def cache_rate_text(cached: int, prompt_total: int) -> str:
    """缓存命中率的展示文字。

    以前是 `cached * 100 // prompt_total` 的**整数地板除法**：真实 0.28% 显示成 0%、
    1.2% 显示成 1%。于是「命中几乎总是 0」这个印象有一半是显示纪律造成的，不是渠道。
    现在：低于 10% 保一位小数，低于 0.1% 说「<0.1%」，10% 以上取整（向下，
    免得 99.9% 显示成 100%），并且上限锁 100%——命中 token 不可能超过总输入。
    """
    if prompt_total <= 0:
        return '0%'
    rate = min(100.0, max(0.0, float(cached) * 100.0 / float(prompt_total)))
    if rate <= 0:
        return '0%'
    if rate < 0.1:
        return '<0.1%'
    if rate < 10:
        return '%.1f%%' % rate
    return '%d%%' % int(rate)


def cache_report_hint(today: dict) -> str:
    """「缓存命中 0」到底是没命中，还是渠道没上报。空串 = 不需要解释。"""
    inp = int(today.get('input_tokens') or 0)
    cached = int(today.get('cached_tokens') or 0)
    if not inp or cached:
        return ''
    if int(today.get('cache_reported_calls') or 0) > 0:
        return ''            # 有调用报了缓存字段，只是没命中——那是真 0，不用解释
    return '｜该渠道未上报缓存字段'


class CommandRouter:
    FORGET_CONFIRM_WINDOW = 60.0

    def __init__(self, runtime):
        self.r = runtime
        self._pending_forget: dict[str, float] = {}

    # ------------- 公共 -------------
    def _save_and_reload(self, changes: dict | None = None) -> None:
        """改配置 + 写盘 + 热更新（配置字典 identity 不变，所有组件立刻看到新值）。

        - `changes` 是要改的键值；**先快照再改**，所以写盘失败能把内存一起回滚
          （否则会出现「命令说没写盘、行为却已经变了」这种更难查的状态）。
        - 写盘失败抛 `ConfigWriteBlocked`，由 `try_handle` 统一回一句「没有写盘」，
          **绝不**用默认配置覆盖用户手改的文件。
        """
        cfg = self.r.config
        snapshot = copy.deepcopy(cfg)
        if changes:
            cfg.update(changes)
        try:
            plugin_config.save_config(cfg)
        except Exception:
            cfg.clear()
            cfg.update(snapshot)
            raise
        reload_fn = getattr(self.r, 'reload_config', None)
        if callable(reload_fn):
            error = reload_fn()
            if error:
                logger.warning('配置热更新有问题：%s', error)

    def _cmd_menu(self, msg: IncomingMessage) -> str:
        """公开菜单：**只放普通用户用得上的指令**。

        2026-11-12 拆分：以前一张菜单把骰主/群管的 20 多条指令和用户指令混在一起，
        普通用户在群里发一次「歌伊菜单」会刷掉半屏，还是别人的开关说明。
        现在管理指令整体挪到「歌伊管理菜单」（`_cmd_admin_menu`），这里只留一句指路。
        """
        lines = [
            f'--- 歌伊菜单 v{plugin_config.PLUGIN_VERSION} ---',
            '',
            '[状态] 歌伊状态 | 今日用量',
            '[对话] 我的记忆 / 查看记忆 | 删除记忆 <序号> | 忘了我',
            '[称呼] 叫我 <名字> | 我的称呼',
            '[私聊] 关系（看看她现在怎么看你） | 今日额度 | 群聊记忆开/关（私聊里要不要带上群里的事）',
            '[世界书] 世界书状态 | 剧情模式 | 剧情模式开/关（探索歌伊的世界与故事）',
            '',
            '群管和骰主的指令：发「歌伊管理菜单」',
        ]
        # 2026-11-11 骰主反转了 11-08 的「菜单里不放赞助内容」：
        # 爱发电页面上已经公开承诺「一档的歌伊菜单末尾鸣谢里有你的名字」，
        # 承诺写在用户看得到的地方，就不能只在后台生效。
        # 收敛在两条线以内：**只出现名字，不出现档位、人数、额度**——
        # 「凭什么他有 100 我只有 60」这个副作用依然要挡住。
        thanks = self._sponsor_thanks_line()
        if thanks:
            lines.extend(['', thanks])
        return '\n'.join(lines)

    def _cmd_admin_menu(self, msg: IncomingMessage) -> str:
        """管理菜单：群管能用「本群」那几条，其余需要骰主/主人。"""
        cfg = self.r.config
        wb_state = '开' if cfg.get('worldbook_enabled') else '关'
        active_state = '开' if cfg.get('active_enabled') else '关'
        if cfg.get('active_enabled') and cfg.get('active_shadow', True):
            active_state = '开(影子)'          # 只记录不真发，别让人以为已经上线了
        memory_state = '开' if cfg.get('memory_curator_enabled', True) else '关'
        gateway = getattr(self.r, 'gateway', None)
        channel = gateway.current_summary() if gateway is not None else '未配置'
        lines = [
            f'--- 歌伊管理菜单 v{plugin_config.PLUGIN_VERSION} ---',
            '（「本群」几条群管就能用，其余需要骰主/主人）',
            '',
            '[本群] 本群被动开/关 | 本群主动开/关 | 查看群背景 | 设置群背景 <文本>',
            '[私聊] 开启/关闭私聊 | 私聊用量（额度与触发词模式） | 私聊触发词开/关',
            '[记忆] 记忆状态 | 导出记忆 | 群聊记忆列表 | 群聊记忆重置 <QQ/全部>',
            '[称呼] 称呼列表 | 称呼重置 <QQ>',
            '[世界书] 世界书开/关 | 世界书重载 | 剧情模式列表 | 剧情模式重置 <QQ/全部>',
            '[渠道] 渠道状态 | 切换渠道 <名称/序号> | 切换渠道 自动',
            '[全局] 全局被动开/关 | 主动搭话开/关 | 搭话观察 | 搭话影子开/关',
            '[全局] 重置上下文 | 重载设定 | 设置key <API Key>（仅私聊）',
            '[关系] 好感度开/关 | 好感度影子开/关 | 关系详情 <QQ> | 关系设置 <QQ> <0-5> | 关系重置 <QQ>',
            '[维护] 静默列表 | 解除静默 [号码]（私聊退场是无限期，只能手动放）',
            '[维护] 测试通知 | 面板地址 | 图片理解开/关 | 添加/删除触发词 <词> | 查看触发词',
            '[表情] 表情包状态 | 开启/关闭表情包 | 加载表情包 <路径> | 表情包重载',
            '[语音] 语音状态 | 测试语音 [情绪] [日文] | 开启/关闭语音 | 语音重载',
            '[作息] 休眠状态 | 休眠开/关 | 休眠时间 <HH:MM-HH:MM> | 休眠重置',
            '[赞助] 赞助名单 | 赞助加 <QQ> [1/2] | 赞助删 <QQ>',
            '',
            f'当前渠道: {channel}',
            f'世界书: {wb_state} | 主动搭话: {active_state} | 自动记忆: {memory_state}',
            f'图片理解: {"开" if cfg.get("image_enabled", True) else "关"} | 伴生语音: {"开" if cfg.get("voice_enabled", True) else "关"}',
        ]
        return '\n'.join(lines)

    # 菜单里最多列几个名字：再多人就折成「等 N 位支持者」，
    # 否则赞助者一多，菜单末尾会变成一个比正文还长的名单。
    THANKS_MENU_LIMIT = 8

    def _sponsor_thanks_line(self) -> str:
        """菜单末尾那行鸣谢：有「叫我」称呼用称呼，没有就用脱敏号码。"""
        ids = sponsors.thanks_ids(self.r.config)
        if not ids:
            return ''
        store = getattr(self.r, 'store', None)
        names = []
        for uid in ids:
            name = aliases.get(store, uid) if store is not None else ''
            names.append(name or sponsors.mask_id(uid))
        text = '鸣谢：' + '、'.join(names[:self.THANKS_MENU_LIMIT])
        if len(names) > self.THANKS_MENU_LIMIT:
            text += ' 等 %d 位支持者' % len(names)
        return text

    def _cmd_status(self, msg: IncomingMessage) -> str:
        cfg = self.r.config
        today = self.r.store.today_stats()
        budget = int(cfg.get('daily_call_budget', 999999))
        state = {
            'private': '开' if cfg.get('private_enabled') else '关',
            'passive': '开' if cfg.get('passive_enabled') else '关',
            'active': '开' if cfg.get('active_enabled') else '关',
            'active_shadow': '影子' if cfg.get('active_shadow', True) else '真发',
            'memory': '开' if cfg.get('memory_enabled') else '关',
            'worldbook': '开' if cfg.get('worldbook_enabled') else '关',
            'emote': '开' if cfg.get('emote_enabled', True) else '关',
            'image': '开' if cfg.get('image_enabled', True) else '关',
        }
        gateway = getattr(self.r, 'gateway', None)
        channel = gateway.current_summary() if gateway is not None else '未配置'
        # Key 状态要看**渠道**：多渠道路由下每个渠道有自己的 Key，
        # 只看顶层 api_key 会显示「未配置」而其实跑得好好的。
        names = gateway.provider_names() if gateway is not None else []
        missing = gateway.missing_keys() if gateway is not None else []
        if not names:
            key_line = 'API Key: 未配置（用「设置key」或 config.json 的 providers）'
        elif missing:
            key_line = 'API Key: %d/%d 个渠道已配置，缺 %s' % (
                len(names) - len(missing), len(names), '、'.join(missing))
        else:
            key_line = 'API Key: 已配置（%d 个渠道）' % len(names)
        # 记忆库概况（读失败也不能让状态命令炸掉）
        try:
            curator = getattr(self.r, 'curator', None)
            session_key = msg.session.as_text()
            facts = self.r.store.count_user_facts(session_key)
            pending = self.r.store.buffer_pending(session_key, curator.cursor(session_key)) if curator else 0
            memory_line = f'记忆库: 用户事实 {facts} 条 | 待提炼 {pending} 条'
        except Exception:  # noqa: BLE001
            memory_line = '记忆库: 读取失败'
        # 本群额度（按群人数算）——被限流时最想知道的就是这一行
        try:
            profile = getattr(self.r, 'group_profile', None)
            quota_line = profile.status_text(msg.session.as_text()) if profile else '本群额度: 未启用'
        except Exception:  # noqa: BLE001
            quota_line = '本群额度: 读取失败'
        # 自我保护静默：她自己在群里退场时最容易被误判成「插件坏了」
        try:
            rest = getattr(self.r, 'rest', None)
            left = rest.remaining(msg.session.as_text()) if rest else 0
            if left < 0:
                reason = rest.reason(msg.session.as_text()) or '未说明'
                rest_line = f'自我保护静默: 本会话无限期（{reason}），发「解除静默」恢复'
            elif left:
                reason = rest.reason(msg.session.as_text()) or '未说明'
                rest_line = f'自我保护静默: 本会话还剩 {left // 60} 分 {left % 60} 秒（{reason}）'
            else:
                rest_line = '自我保护静默: 未触发'
        except Exception:  # noqa: BLE001
            rest_line = '自我保护静默: 读取失败'
        # 调度队列：回答「她是不是卡在队列里了」——2026-11-01 那次故障就卡在这里，
        # 而当时没有任何一行能看出这一点。
        try:
            scheduler = getattr(self.r, 'scheduler', None)
            snap = scheduler.pending_snapshot() if scheduler is not None else {}
            if snap.get('executor_down'):
                queue_line = '队列: 线程池已关闭（异常，重载插件可恢复）'
            elif snap.get('total'):
                queue_line = '队列: 待处理 %d 条（%d 个会话在排队）' % (
                    snap['total'], len(snap.get('sessions') or {}))
            else:
                queue_line = '队列: 空闲'
        except Exception:  # noqa: BLE001
            queue_line = '队列: 读取失败'
        return '\n'.join([
            f'--- 歌伊状态 v{plugin_config.PLUGIN_VERSION} ---',
            key_line,
            f'当前渠道: {channel}',
            f'私聊: {state["private"]} | 被动: {state["passive"]} | 主动: {state["active"]}',
            f'搭话模式: {state["active_shadow"]}（影子=只记录不真发，发「搭话观察」看决策）',
            f'记忆: {state["memory"]} | 世界书: {state["worldbook"]} | 立绘: {state["emote"]}',
            f'图片理解: {state["image"]}（群里发图时她能不能看懂）',
            memory_line,
            quota_line,
            rest_line,
            queue_line,
            self._token_line(today),
            f'今日调用: {today["calls"]}/{budget} | 主动搭话: {today["active_calls"]} 次',
            f'触发词: {", ".join(cfg.get("triggers", [])) or "歌伊"}',
        ])

    def _token_line(self, today: dict) -> str:
        """状态里那一行总 token。

        骰主要的是「今天一共消耗多少 token」这一个数，所以**合计放最前面**，
        输入/输出/缓存命中作为解释跟在后面。缓存命中率是省钱改造的尺子，但要看懂它得
        先分清两件事：渠道**没上报**缓存字段，和渠道报了但**没命中**。两者在旧版里
        都显示 0%，于是「几乎总是 0」既可能是真没命中、也可能是白白怀疑自己。
        """
        total = int(today.get('total_tokens') or 0)
        inp = int(today.get('input_tokens') or 0)
        out = int(today.get('output_tokens') or 0)
        cached = int(today.get('cached_tokens') or 0)
        return ('今日 token: %s（输入 %s + 输出 %s｜缓存命中 %s，%s%s）'
                % (fmt_tokens(total), fmt_tokens(inp), fmt_tokens(out),
                   fmt_tokens(cached), cache_rate_text(cached, inp),
                   cache_report_hint(today)))

    def _cmd_test_notify(self, msg: IncomingMessage) -> str:
        """骰主自查：私聊通道到底通不通（退场通知、渠道全挂通知都走这条路）。"""
        notify = getattr(self.r, '_notify_owner', None)
        if not callable(notify):
            return '通知模块未初始化'
        owners = sorted(self.r.permissions._owner_set())
        ok = notify(
            '🔔 这是一条测试通知\n'
            '如果你看到这句话，说明歌伊的骰主私聊通道是通的——'
            '「自我保护静默」「所有模型渠道都挂了」都会用这条通道通知你。'
        )
        who = '、'.join(owners) if owners else '（未配置任何骰主）'
        if ok:
            return f'✅ 已尝试私聊 {who}，去私聊窗口看看有没有收到'
        return (f'❌ 没发出去（收件人：{who}）\n'
                f'原因看 OlivOS 日志里「通知发不出去」那条；最常见是 config.json 的 '
                f'owner_ids 是空的，填上你的 QQ 号再「重载设定」')

    @staticmethod
    def _rest_target_parts(session_key: str) -> tuple:
        """解析会话键 → (种类, 号码)。

        `models.SessionKey.as_text()` 用的是**冒号**：`h:qq:onebot:private:-:9333111`。
        从右边切三段，前面就算带冒号也不影响（类名/机器人哈希里偶尔会有怪字符）。
        """
        parts = str(session_key or '').rsplit(':', 3)
        kind = parts[1] if len(parts) == 4 else ''
        target = parts[-1] if parts else str(session_key)
        return kind, target

    @classmethod
    def _rest_target_label(cls, session_key: str) -> str:
        kind, target = cls._rest_target_parts(session_key)
        return f'私聊 {target}' if kind == 'private' else f'群 {target}'

    def _cmd_rest_list(self, msg: IncomingMessage) -> str:
        """骰主可查：现在都有谁被静默着（私聊的无限期静默只能这样发现）。"""
        rest = getattr(self.r, 'rest', None)
        if rest is None:
            return '静默模块未初始化'
        rows = rest.resting_sessions()
        if not rows:
            return '当前没有任何会话处于静默中'
        lines = [f'🛑 正在静默的会话 {len(rows)} 个：']
        for key, left, reason, forever in rows[:20]:
            span = '无限期（等手动解除）' if forever else f'{left // 60} 分 {left % 60} 秒'
            lines.append(f'• {self._rest_target_label(key)}｜{span}'
                         f'{"｜" + reason if reason else ""}')
        if len(rows) > 20:
            lines.append(f'…另有 {len(rows) - 20} 个')
        lines.append('解除：解除静默 <QQ号/群号>；或在该会话里直接发「解除静默」')
        return '\n'.join(lines)

    def _cmd_clear_rest_any(self, msg: IncomingMessage, target: str) -> str:
        """骰主按号码解除静默。

        必须存在的原因：私聊退场是无限期的，而「解除静默」只作用于**发命令的那个
        会话**。陌生人私聊骚扰她之后，那个会话只有对方能"在那里发命令"——骰主的
        窗口碰不到它，等于永久封口且无法恢复。
        """
        rest = getattr(self.r, 'rest', None)
        if rest is None:
            return '静默模块未初始化'
        tid = str(target or '').strip()
        if not tid.isdigit():
            return '格式: 解除静默 <QQ号 或 群号>（不带号码就解除当前会话）'
        cleared = []
        for key, _left, _reason, _forever in rest.resting_sessions():
            _kind, target = self._rest_target_parts(key)
            if target == tid and rest.clear(key):
                cleared.append(self._rest_target_label(key))
        if not cleared:
            return f'没找到 {tid} 的静默记录（发「静默列表」看现在都有谁）'
        return f'✅ 已解除：{"、".join(cleared)}\n她现在会正常回话了'

    def _cmd_panel_url(self, msg: IncomingMessage) -> str:
        """骰主自查：本地面板地址（核心没有 OlivOS WebUI 时唯一的入口）。"""
        server = getattr(self.r, 'panel_server', None)
        if server is None:
            return ('本地面板没有在跑：要么 `panel_local_enabled` 被关掉了，'
                    '要么端口被占用（看日志「本地面板启动失败」）。')
        url = getattr(server, 'url', '')
        if not url:
            return '本地面板未启动（端口可能被占用，改 config.json 的 panel_local_port 或设 0）'
        return (f'🖥 歌伊调试台（只监听本机）\n{url}\n'
                f'直接粘到本机浏览器打开；换端口改 panel_local_port，关掉改 panel_local_enabled=false。\n'
                f'如果核心是 OlivOS 1.0.x，优先用原生「插件页面 → 歌伊调试台」。')

    def _cmd_clear_rest(self, msg: IncomingMessage) -> str:
        """提前解除本会话的自我保护静默（骰主专用）。

        私聊的静默是**无限期**的，所以这条命令就是唯一的恢复开关。
        """
        rest = getattr(self.r, 'rest', None)
        if rest is None:
            return '静默模块未初始化'
        session_key = msg.session.as_text()
        left = rest.remaining(session_key)
        reason = rest.reason(session_key)
        had = rest.clear(session_key)
        if not had:
            return '本会话当前不在静默中'
        tail = f'{"，" + reason if reason else ""}'
        if left < 0:
            return (f'✅ 已解除本会话的无限期静默{tail}\n她现在会正常回话了')
        return (f'✅ 已解除本会话的自我保护静默（原本还剩 {left // 60} 分 {left % 60} 秒'
                f'{tail}）\n她现在会正常回话了')

    def _cmd_channels(self, msg: IncomingMessage) -> str:
        gateway = getattr(self.r, 'gateway', None)
        if gateway is None:
            return '网关未初始化'
        return gateway.status_text()

    def _cmd_memory_status(self, msg: IncomingMessage) -> str:
        """骰主可查：她到底记住了什么（含别人的事实，所以限主人）。"""
        curator = getattr(self.r, 'curator', None)
        if curator is None:
            return '记忆系统未初始化'
        return curator.status_text(msg.session.as_text())

    def _cmd_active_observe(self, msg: IncomingMessage) -> str:
        """看主动搭话的决策分布——判断该不该结束影子模式就看这个。"""
        attention = getattr(self.r, 'attention', None)
        if attention is None:
            return '主动搭话模块未初始化'
        return attention.stats_text(msg.session.as_text())

    def _cmd_active_shadow(self, msg: IncomingMessage, on: bool) -> str:
        self._save_and_reload({'active_shadow': on})
        if getattr(self.r, 'attention', None):
            self.r.attention.config = self.r.config
        if on:
            return '🔍 已进入影子模式：决策照记，消息不真发（发「搭话观察」看结果）'
        return '✅ 已关闭影子模式，之后主动搭话会真的发出去'

    def _cmd_switch_channel(self, msg: IncomingMessage, arg: str) -> str:
        gateway = getattr(self.r, 'gateway', None)
        if gateway is None:
            return '网关未初始化'
        arg = arg.strip()
        if not arg:
            return ('格式: 切换渠道 <渠道名或序号>\n'
                    '手动切换会把它设为「锚点」：故障时只在它以下的渠道里找备份，'
                    '恢复也只回到这里。发「切换渠道 自动」取消锚点。')
        return gateway.switch_to(arg)

    def _cmd_usage(self, msg: IncomingMessage) -> str:
        today = self.r.store.today_stats()
        budget = int(self.r.config.get('daily_call_budget', 999999))
        out_budget = int(self.r.config.get('daily_output_token_budget', 99999999))
        input_tokens = int(today.get('input_tokens') or 0)
        output_tokens = int(today.get('output_tokens') or 0)
        total_tokens = int(today.get('total_tokens') or 0)
        cached = int(today.get('cached_tokens') or 0)
        lines = [
            '今日合计 %s tokens（输入 %s + 输出 %s）' % (
                fmt_tokens(total_tokens), fmt_tokens(input_tokens), fmt_tokens(output_tokens)),
            '调用 %s/%s 次（成功 %s / 失败 %s），输出 %s/%s tokens' % (
                fmt_tokens(today['calls']), fmt_tokens(budget),
                fmt_tokens(today.get('ok_calls')), fmt_tokens(today.get('failed_calls')),
                fmt_tokens(output_tokens), fmt_tokens(out_budget)),
            # 缓存命中率是省钱改造的尺子。分母是**含缓存的全部输入**（网关已归一），
            # 0% 时补一句"未上报"，否则分不清是渠道没透传还是真没命中。
            '输入里缓存命中 %s（%s）%s' % (
                fmt_tokens(cached), cache_rate_text(cached, input_tokens),
                cache_report_hint(today)),
            '其中主动搭话触发 %s 次' % fmt_tokens(today['active_calls']),
        ]
        # 按会话拆账：同样一个「被动回复」，群里接话和私聊长聊的成本差着几倍，
        # 只按 kind 拆是看不出来的（两类共用 passive 这个 kind）。
        try:
            scope_parts = []
            for row in self.r.store.today_stats_by_scope():
                label = SCOPE_LABELS.get(row['scope'], row['scope'] or '未知')
                scope_parts.append('%s %s 次 / %s' % (
                    label, fmt_tokens(row['calls']), fmt_tokens(row['total_tokens'])))
            if scope_parts:
                lines.append('按场景: ' + ' · '.join(scope_parts))
        except Exception:  # noqa: BLE001 - 拆账失败不该让用量命令报错
            pass
        # 拆账：判断/主动/被动/提炼的单价差一个量级，混在一起看不出钱花在哪
        try:
            parts = []
            for row in self.r.store.today_stats_by_kind()[:5]:
                label = KIND_LABELS.get(row['kind'], row['kind'] or '未知')
                parts.append('%s %s次/%s' % (
                    label, fmt_tokens(row['calls']), fmt_tokens(row.get('total_tokens'))))
            if parts:
                lines.append('按用途: ' + ' · '.join(parts))
        except Exception:  # noqa: BLE001 - 拆账失败不该让用量命令报错
            pass
        return '\n'.join(lines)

    # ==================== 赞助名单 ====================
    # 为什么做成命令而不是只改 config.json：加人这件事是**手工、偶发、要核对**的，
    # 而 config.json 有两个问题——手改容易碰坏别的东西（整份文件重写），
    # 而且改完还得重载。做成命令，一步到位，还有回执。
    #
    # 档位写法的取舍：`赞助加 123 1` 太隐晦，`赞助加 123 一档` 要处理各种别名。
    # 折中——不写档位默认一档，写「2」「二档」都认，其余明确报错。
    _TIER_ALIAS = {
        '': 1, '1': 1, '一': 1, '1档': 1, '一档': 1, '一天饭': 1,
        '2': 2, '二': 2, '2档': 2, '二档': 2, '半个月': 2, '房子': 2,
    }

    def _sponsor_key(self, tier: int) -> str:
        return 'sponsor_tier%d_ids' % (2 if int(tier) >= 2 else 1)

    def _cmd_sponsor_list(self, msg: IncomingMessage) -> str:
        cfg = self.r.config
        lines = ['--- 赞助档位 ---']
        for tier in (1, 2):
            key = self._sponsor_key(tier)
            ids = sorted(sponsors.id_set(cfg, key))
            quota = int(sponsors.quota_for(cfg, ids[0])) if ids else 0
            lines.append('%s：%d 人%s' % (
                sponsors.TIER_NAMES[tier], len(ids),
                ('（额度 %d）' % quota) if ids else ''))
            lines.extend('  · ' + uid for uid in ids[:30])
            if len(ids) > 30:
                lines.append('  …另有 %d 人' % (len(ids) - 30))
        legacy = sorted(sponsors.id_set(cfg, 'sponsor_ids'))
        if legacy:
            lines.append('旧名单 sponsor_ids（按一档算）：%d 人' % len(legacy))
        beta = sorted(sponsors.id_set(cfg, 'beta_ids'))
        if beta:
            lines.append('内测额外名单：%d 人' % len(beta))
        lines.append('')
        lines.append('加：赞助加 <QQ> [1/2]   删：赞助删 <QQ>')
        lines.append('两档都免拥挤期收紧；额度 %d / %d 条'
                     % (int(cfg.get('private_daily_quota_sponsor_tier1', 100) or 0),
                        int(cfg.get('private_daily_quota_sponsor_tier2', 120) or 0)))
        lines.append('二档额外：记忆不裁剪 + 记忆召回下限 %d 条 + 内测优先'
                     % int(cfg.get('sponsor_fact_floor', 8) or 0))
        lines.append('')
        # 鸣谢文案只在这里露面。公开命令里一个字都不提赞助（骰主 2026-11-08 定），
        # 所以这段文字要给人看的时候，是贴到爱发电页面或群里，不是靠菜单自动显示。
        thanks = str(cfg.get('sponsor_thanks_text') or '').strip()
        lines.append('鸣谢文案: ' + (thanks if thanks else '（未设置）'))
        lines.append('改法：面板改 sponsor_thanks_text，或手改 config.json 后「重载设定」')
        return '\n'.join(lines)

    @staticmethod
    def _parse_sponsor_arg(arg: str) -> tuple:
        parts = str(arg or '').split()
        if not parts:
            return '', 0
        uid = parts[0].strip()
        if not uid.isdigit():
            return '', 0
        alias = parts[1].strip() if len(parts) > 1 else ''
        return uid, CommandRouter._TIER_ALIAS.get(alias, 0)

    def _cmd_sponsor_add(self, msg: IncomingMessage, arg: str) -> str:
        uid, tier = self._parse_sponsor_arg(arg)
        if not uid:
            return '格式: 赞助加 <QQ号> [1 或 2]（不写档位按一档算）'
        if not tier:
            return '档位只认 1 / 2（或「一档」「二档」）。1 = 6 元档，2 = 20 元档。'
        cfg = self.r.config
        changes = {}
        # 同一人只该出现在一个档位里（档位是累计的，二档已经包含一档的权益），
        # 所以加进新档时把它从别的名单里摘掉——否则降档后旧权益还在。
        for key in ('sponsor_ids', self._sponsor_key(1), self._sponsor_key(2)):
            current = sponsors.id_set(cfg, key)
            if key == self._sponsor_key(tier):
                current.add(uid)
            elif uid in current:
                current.discard(uid)
            else:
                continue
            changes[key] = sorted(current)
        self._save_and_reload(changes)
        quota = int(sponsors.quota_for(self.r.config, uid))
        extra = ''
        if tier >= 2:
            extra = '，记忆不裁剪 + 记忆召回下限 %d 条 + 内测优先' % int(
                self.r.config.get('sponsor_fact_floor', 8) or 0)
        return ('✅ 已加入「%s」\n'
                '私聊额度 %d，拥挤期不收紧%s\n'
                '（爱发电那边确认收到钱再加人，别提前加）'
                % (sponsors.TIER_NAMES[tier], quota, extra))

    def _cmd_sponsor_remove(self, msg: IncomingMessage, arg: str) -> str:
        uid = str(arg or '').strip().split()[0] if str(arg or '').strip() else ''
        if not uid.isdigit():
            return '格式: 赞助删 <QQ号>'
        changes = {}
        hit = False
        for key in ('sponsor_ids', self._sponsor_key(1), self._sponsor_key(2)):
            current = sponsors.id_set(self.r.config, key)
            if uid in current:
                current.discard(uid)
                changes[key] = sorted(current)
                hit = True
        if not hit:
            return '这个号不在任何赞助名单里（赞助名单 可看当前成员）'
        self._save_and_reload(changes)
        return '✅ 已把 %s 从赞助名单移除，额度恢复为普通用户' % uid

    def _cmd_remember(self, msg: IncomingMessage, arg: str) -> str:
        # 墓碑：显式「记住 xxx」已被废除，避免提示词注入与越狱投毒风险。
        return '现在不用专门喊「记住」啦，平时正常和我聊天我就会自然记住你的习惯和爱好哦～（发送「我的记忆」可查看）'

    def _cmd_view_my_memory(self, msg: IncomingMessage) -> str:
        """查看歌伊对当前用户的记忆与印象。"""
        uid = str(getattr(msg, 'sender_id', '') or '').strip()
        if not uid:
            return '唔……未能获取到你的账号信息哦。'
        session_key = msg.session.as_text()

        # 1. 查找高维印象（按 scope 隔离；互相能看到时双向呈现）
        impression_text = ''
        is_group = bool(getattr(msg, 'is_group', True))
        # 和记忆注入用同一套方向判断：单向共享时，群聊里不该翻出私聊的印象
        from ..memory.repository import cross_scope_enabled, relative_time
        override = None
        if not is_group:
            # 和记忆注入用同一个覆盖：否则「我的记忆」显示的跟实际注入的不一致
            try:
                override = self.r.memory.share_override(session_key)
            except Exception:  # noqa: BLE001 - 记忆模块异常不该挡住这条命令
                override = None
        cross_enabled = cross_scope_enabled(self.r.config, is_group, override=override)
        primary_scope = 'group' if is_group else 'private'

        if self.r.config.get('memory_impression_enabled', True):
            try:
                from ..memory.repository import relative_time
                imp_parts = []
                p_imp = self.r.store.get_user_impression(uid, scope=primary_scope)
                if p_imp and p_imp.get('summary'):
                    text = str(p_imp['summary']).strip()
                    stamp = int(p_imp.get('updated_at') or 0)
                    versions = len(self.r.store.impression_history(uid, scope=primary_scope, limit=200))
                    meta = f'（{relative_time(stamp)}更新' + (f'，共 {versions} 版）' if versions > 1 else '）') if stamp else ''
                    tag = '群聊印象' if is_group else '私聊印象'
                    imp_parts.append(f'🌟 歌伊对你的{tag}{meta}：\n  {text}')

                if cross_enabled:
                    cross_scope = 'private' if is_group else 'group'
                    c_imp = self.r.store.get_user_impression(uid, scope=cross_scope)
                    if c_imp and c_imp.get('summary'):
                        c_text = str(c_imp['summary']).strip()
                        c_stamp = int(c_imp.get('updated_at') or 0)
                        c_versions = len(self.r.store.impression_history(uid, scope=cross_scope, limit=200))
                        c_meta = f'（{relative_time(c_stamp)}更新' + (f'，共 {c_versions} 版）' if c_versions > 1 else '）') if c_stamp else ''
                        c_tag = '私聊印象（互通）' if is_group else '群聊印象（互通）'
                        imp_parts.append(f'🌟 歌伊对你的{c_tag}{c_meta}：\n  {c_text}')

                if imp_parts:
                    impression_text = '\n'.join(imp_parts)
            except Exception:
                pass

        # 2. 查找事实（包含当前会话事实与跨群全局偏好，按互通开关召回）
        fact_items: list[tuple[int, str]] = []
        try:
            include_global = self.r.config.get('memory_cross_group_enabled', True)
            rows = self.r.store.list_user_facts(
                session_key, uid, limit=20, include_global=include_global,
                cross_scope=cross_enabled,
            )
            seen = set()
            for r in rows:
                f = str(r.get('fact') or '').strip()
                if f and f not in seen:
                    seen.add(f)
                    fact_items.append((int(r['id']), f))
        except Exception:
            pass

        if not impression_text and not fact_items:
            return '唔……歌伊的小本本上关于你还是一片空白呢～\n平时多和我聊聊天，我就会自然记住你的习惯和爱好啦！（眨眼）'

        lines = ['【歌伊的小本本 · 记忆档案】']
        if impression_text:
            lines.append(impression_text)
        if fact_items:
            lines.append(f'📝 记住的事（共 {len(fact_items)} 条）：')
            for idx, (_, f) in enumerate(fact_items, 1):
                lines.append(f'  {idx}. {f}')
        lines.append('💡 提示：发送「删除记忆 <序号>」可删除单条；发送「忘了我」清空全部～')
        return '\n'.join(lines)

    def _cmd_delete_memory(self, msg: IncomingMessage, arg: str) -> str:
        raw_idx = str(arg or '').strip().lstrip('#').lstrip('：').lstrip(':').strip()
        if not raw_idx.isdigit():
            return '提示：请发送「删除记忆 <序号>」，例如「删除记忆 1」。（序号可在「我的记忆」中查看）'
        idx = int(raw_idx)
        session_key = msg.session.as_text()
        uid = str(msg.sender_id)
        is_group = bool(getattr(msg, 'is_group', True))
        from ..memory.repository import cross_scope_enabled
        override = None
        if not is_group:
            try:
                override = self.r.memory.share_override(session_key)
            except Exception:
                override = None
        cross_enabled = cross_scope_enabled(self.r.config, is_group, override=override)

        fact_items: list[tuple[int, str]] = []
        try:
            include_global = self.r.config.get('memory_cross_group_enabled', True)
            rows = self.r.store.list_user_facts(
                session_key, uid, limit=20, include_global=include_global,
                cross_scope=cross_enabled,
            )
            seen = set()
            for r in rows:
                f = str(r.get('fact') or '').strip()
                if f and f not in seen:
                    seen.add(f)
                    fact_items.append((int(r['id']), f))
        except Exception:
            pass

        if not fact_items:
            return '歌伊的小本本上现在没有关于你的记忆哦～'
        if idx < 1 or idx > len(fact_items):
            return f'找不到序号为 {idx} 的记忆呢。请发送「我的记忆」确认序号（目前共有 1~{len(fact_items)} 条）～'

        fact_id, fact_text = fact_items[idx - 1]
        deleted = self.r.store.delete_user_fact_by_id(fact_id, uid)
        if deleted:
            return f'✅ 已为你删除第 {idx} 条记忆：「{fact_text}」'
        return '删除失败，该条记忆可能已被删除。请发送「我的记忆」刷新查看～'

    def _cmd_forget_me(self, msg: IncomingMessage, force_confirm: bool = False) -> str:
        uid = str(msg.sender_id)
        session_key = msg.session.as_text()
        now = time.time()
        confirm_key = f'{session_key}:{uid}'
        last_req = self._pending_forget.get(confirm_key, 0.0)

        # 处于二次确认状态：发了“确认忘了我”或 60s 内连发两次“忘了我”
        if force_confirm or (now - last_req <= self.FORGET_CONFIRM_WINDOW):
            self._pending_forget.pop(confirm_key, None)
            return self._execute_forget_me(msg)

        # 首次触发：记录待确认状态，返回警告与确认指导
        self._pending_forget[confirm_key] = now
        return (
            '⚠️ 【重要确认】确定要让我彻底忘记你吗？\n'
            '这将会彻底清空关于你的：\n'
            '  · 记住的全部爱好与习惯（含跨群偏好）\n'
            '  · 自定义称呼（改回 QQ 昵称）\n'
            '  · 私聊好感度与关系阶段（全部重置为陌生）\n\n'
            '👉 若确认彻底清空，请在 60 秒内发送：确认忘了我（或再发一次 忘了我）\n'
            '💡 如果只想删除某条具体的习惯，可以发送「我的记忆」，然后发送「删除记忆 <序号>」哦～'
        )

    def _execute_forget_me(self, msg: IncomingMessage) -> str:
        # 连**全局范围**的事实一起清（跨群偏好存在 session_key='global' 里），
        # 否则换个群她还记得你（2026-11-03 审查发现）。
        count = self.r.memory.clear_user(msg)
        relationship = getattr(self.r, 'relationship', None)
        if relationship is not None:
            relationship.forget(msg.sender_id)        # 关系数据和流水一起清
        had_alias = aliases.get(self.r.store, msg.sender_id)
        aliases.set_alias(self.r.store, msg.sender_id, None)   # 称呼也是个人数据，一起清
        text = (f'✅ 已彻底重置关于你的全部档案：\n'
                f'  · 忘记了关于你的 {count} 条记忆（含跨群偏好）\n'
                f'  · 私聊好感度与关系阶段已重置为「陌生」')
        if had_alias:
            text += '\n  · 自定义称呼已重置（改回 QQ 昵称）'
        # 私聊里说「忘了我」只清得掉这个私聊（+ 跨群偏好）；群里那一份仍会被召回。
        # 既然这个私聊可能还开着「群聊记忆」，就把它说清楚，别让人以为没生效。
        if not msg.is_group:
            try:
                info = self.r.memory.share_status(msg.session.as_text())
            except Exception:  # noqa: BLE001 - 状态读不到就不多这句
                info = None
            if info and info['enabled']:
                text += ('\n（这个私聊还开着「群聊记忆」，群里说过的事我仍会想起来；'
                         '发「群聊记忆关」可以关掉）')
        return text

    # ------------- 自定义称呼（歌伊怎么叫你）-------------
    def _cmd_alias_show(self, msg: IncomingMessage) -> str:
        alias = aliases.get(self.r.store, msg.sender_id)
        if alias:
            return ('我现在叫你「%s」。\n换一个：发「叫我 新名字」（中间空一格）；'
                    '想改回 QQ 昵称：发「称呼重置」。' % alias)
        return ('我现在按 QQ 昵称叫你「%s」。\n想让我换个叫法：发「叫我 小明」（中间空一格）。'
                % (msg.sender_name or '你'))

    def _cmd_alias_set(self, msg: IncomingMessage, raw: str) -> str:
        alias, error = aliases.sanitize(raw)
        if error:
            return '唔……' + error
        aliases.set_alias(self.r.store, msg.sender_id, alias)
        return '✅ 好呀，以后就叫你「%s」啦。\n（想改回 QQ 昵称：发「称呼重置」）' % alias

    def _cmd_alias_reset(self, msg: IncomingMessage) -> str:
        current = aliases.get(self.r.store, msg.sender_id)
        if not current:
            return '你现在就是按 QQ 昵称叫的呀，不用重置。'
        aliases.set_alias(self.r.store, msg.sender_id, None)
        return '✅ 已经改回按 QQ 昵称叫你啦（下一条消息起生效）。'

    def _cmd_alias_list(self, msg: IncomingMessage) -> str:
        items = aliases.listing(self.r.store)
        if not items:
            return '还没有人设过自定义称呼（大家都按 QQ 昵称叫）。'
        lines = ['【自定义称呼 · 共 %d 个】' % len(items)]
        for uid, alias in items[:40]:
            lines.append(f'  · {uid} → {alias}')
        if len(items) > 40:
            lines.append(f'  ……还有 {len(items) - 40} 个')
        return '\n'.join(lines)

    def _cmd_alias_admin_reset(self, msg: IncomingMessage, arg: str) -> str:
        target = str(arg or '').strip()
        if not target.isdigit():
            return '格式：称呼重置 <QQ号>'
        current = aliases.get(self.r.store, target)
        if not current:
            return f'QQ {target} 本来就没有自定义称呼。'
        aliases.set_alias(self.r.store, target, None)
        return f'✅ 已清除 QQ {target} 的称呼「{current}」（回到 QQ 昵称）'

    # ------------- 私聊：群聊记忆共享（会话级）-------------
    _SHARE_GROUP_HINT = '这个要私聊里单独设置哦～（在群里说，会把大家的事混在一起）'

    def _cmd_share_status(self, msg: IncomingMessage) -> str:
        """「群聊记忆」：这个私聊到底会不会把群里的事带进来。"""
        if msg.is_group:
            return self._SHARE_GROUP_HINT
        relation = None
        try:
            relation = self.r.relationship.memory_policy(msg)
        except Exception:  # noqa: BLE001 - 关系模块没开/没初始化都不该挡住这条命令
            relation = None
        info = self.r.memory.share_status(msg.session.as_text(), relation)
        where = '你设的' if info['source'] == 'user' else '默认'
        if info['enabled']:
            head = '【群聊记忆】开启（%s）' % where
            body = '私聊里我会记得你在群里说过的事，也接得上群里的话题。'
        else:
            head = '【群聊记忆】关闭（%s）' % where
            body = '私聊里我只记得我们俩聊过的事，群里的记忆不会带进来。'
        lines = [head, body]
        if info['stage_blocked']:
            lines.append('（不过我们还不算熟，群里的事我只隐约记得一点；多聊聊就会放开。）')
        lines.append('发「群聊记忆开」/「群聊记忆关」切换；「群聊记忆默认」恢复默认设置。')
        return '\n'.join(lines)

    def _cmd_share_toggle(self, msg: IncomingMessage, enabled: bool) -> str:
        if msg.is_group:
            return self._SHARE_GROUP_HINT
        self.r.memory.set_share_override(msg.session.as_text(), enabled)
        if enabled:
            return ('✅ 已开启「群聊记忆」：私聊里我会接着群里的话题。\n'
                    '（群聊和私聊的记忆仍然分开保存，只是我私聊时会想起来。）')
        return ('✅ 已关闭「群聊记忆」：私聊里我只记得我们俩聊过的事，'
                '群里的事不会带进来。\n想恢复默认：发「群聊记忆默认」。')

    def _cmd_share_default(self, msg: IncomingMessage) -> str:
        if msg.is_group:
            return self._SHARE_GROUP_HINT
        self.r.memory.set_share_override(msg.session.as_text(), None)
        default = bool(self.r.config.get('memory_private_enabled', False))
        return '✅ 已清除本私聊的单独设置（重新跟随默认：%s）' % ('开启' if default else '关闭')

    def _share_overrides(self) -> list[tuple[str, str]]:
        """[(session_key, '开'/'关')]；清除过的键会留一行空串，这里跳过。"""
        items: list[tuple[str, str]] = []
        for key, value in self.r.store.settings_with_prefix(SHARE_OVERRIDE_PREFIX):
            session = key[len(SHARE_OVERRIDE_PREFIX):]
            text = str(value or '').strip()
            if not session or not text:
                continue
            items.append((session, '开' if text in ('1', 'on', 'true', 'True') else '关'))
        return items

    def _cmd_share_list(self, msg: IncomingMessage) -> str:
        default = '开启' if self.r.config.get('memory_private_enabled', False) else '关闭'
        items = self._share_overrides()
        if not items:
            return '还没有谁单独设置过「群聊记忆」（全部跟随默认：%s）' % default
        lines = ['【群聊记忆 · 已单独设置的私聊】']
        for session, label in items[:40]:
            _, target = self._rest_target_parts(session)
            lines.append(f'  · 私聊 {target}：{label}')
        if len(items) > 40:
            lines.append(f'  ……还有 {len(items) - 40} 个')
        lines.append('（没列出的跟随默认：%s）' % default)
        return '\n'.join(lines)

    def _cmd_share_reset(self, msg: IncomingMessage, arg: str) -> str:
        target = str(arg or '').strip()
        items = self._share_overrides()
        if target in ('', '全部', 'all', 'ALL'):
            for session, _ in items:
                self.r.memory.set_share_override(session, None)
            return '✅ 已清除全部 %d 个私聊的单独设置（都回到默认）' % len(items)
        if not target.isdigit():
            return '格式：群聊记忆重置 <QQ号>，或「群聊记忆重置 全部」'
        hit = [session for session, _ in items if session.rsplit(':', 1)[-1] == target]
        for session in hit:
            self.r.memory.set_share_override(session, None)
        if not hit:
            return f'没找到 QQ {target} 的单独设置（本来就在跟随默认）。'
        return f'✅ 已清除 QQ {target} 的单独设置（重新跟随默认）'

    def _cmd_worldbook_status(self, msg: IncomingMessage) -> str:
        on = self.r.config.get('worldbook_enabled', False)
        count = len(self.r.worldbook.entries)
        normal_n = int(self.r.config.get('worldbook_max_entries', 3))
        story_n = int(self.r.config.get('worldbook_story_max_entries', 6))
        story_on = is_story_mode(self.r.store, msg.session.as_text(), msg.sender_id)
        mode_str = f'剧情探索模式（单轮最多 {story_n} 条）' if story_on else f'日常模式（单轮最多 {normal_n} 条）'
        return (f'世界书: {"开" if on else "关"} | 已加载条目: {count} | 当前你的模式: {mode_str}\n'
                f'（发「剧情模式开」可深入探索歌伊的世界与故事；未命中零成本）')

    def _cmd_story_mode_status(self, msg: IncomingMessage) -> str:
        story_on = is_story_mode(self.r.store, msg.session.as_text(), msg.sender_id)
        normal_n = int(self.r.config.get('worldbook_max_entries', 3))
        story_n = int(self.r.config.get('worldbook_story_max_entries', 6))
        state_text = '开启' if story_on else '关闭（日常模式）'
        lines = [
            f'【剧情探索模式】当前状态：{state_text}',
            f'· 效果：与歌伊对话时，世界书设定注入上限由 {normal_n} 条提升至 {story_n} 条，'
            '自动展开关联设定簇，她也会多聊一些细节与见闻。',
            '· 可试着聊起的话题线索：',
            '  1. 清泉镇风物：清泉镇、教会日常、麦穗面包坊、圣职学院、丰收祭典',
            '  2. 身边的人：好朋友月白、老猎人格伦、同屋修女米娅、莱妮院长、橘猫「面包屑」',
            '  3. 异世界与本职：治愈粒子与净化术、猎人公会与等级、冒险家协会、外派小队任务',
            '  4. 历史与传说：天灾、拟灾、渊海、玄门之战（崇）、公会历史、化人与主和派',
            '  （私聊里随着关系一步步变亲近，还能慢慢解锁她不轻易对外人提起的身世与心愿）',
            '· 指令：发「剧情模式开」开启，「剧情模式关」切回日常模式。',
        ]
        return '\n'.join(lines)

    def _cmd_story_mode_toggle(self, msg: IncomingMessage, enabled: bool) -> str:
        set_story_mode(self.r.store, msg.session.as_text(), msg.sender_id, enabled)
        story_n = int(self.r.config.get('worldbook_story_max_entries', 6))
        normal_n = int(self.r.config.get('worldbook_max_entries', 3))
        if enabled:
            return (
                f'📖 已为你开启「剧情探索模式」（单轮世界书注入上限提升至 {story_n} 条，开启关联设定展开）！\n'
                '现在你可以向歌伊打听「清泉镇」「教会日常」「麦穗面包坊」「猎人公会」「治愈粒子」「天灾传说」或她身边的朋友啦～\n'
                '（发「剧情模式」可查看话题线索，发「剧情模式关」随时退回日常模式）'
            )
        return f'✅ 已切回日常聊天模式（世界书按默认最多 {normal_n} 条精简注入）。'

    def _cmd_story_mode_list(self, msg: IncomingMessage) -> str:
        users = list_story_mode_users(self.r.store)
        if not users:
            return '目前没有人开启「剧情探索模式」（全部处于默认日常模式）。'
        lines = [f'【剧情探索模式 · 已开启用户（共 {len(users)} 人）】']
        for uid in users[:40]:
            alias = aliases.get(self.r.store, uid)
            suffix = f'（{alias}）' if alias else ''
            lines.append(f'  · {uid}{suffix}')
        if len(users) > 40:
            lines.append(f'  ……还有 {len(users) - 40} 人')
        return '\n'.join(lines)

    def _cmd_story_mode_reset(self, msg: IncomingMessage, arg: str) -> str:
        target = str(arg or '').strip()
        users = list_story_mode_users(self.r.store)
        if target in ('', '全部', 'all', 'ALL'):
            for uid in users:
                set_story_mode(self.r.store, '', uid, None)
            for key, _ in self.r.store.settings_with_prefix('story_mode:'):
                self.r.store.set_setting(key, '')
            return f'✅ 已重置全部 {len(users)} 位用户的剧情模式（均回到日常模式）'
        if not target.isdigit():
            return '格式：剧情模式重置 <QQ号>，或「剧情模式重置 全部」'
        set_story_mode(self.r.store, '', target, None)
        for key, _ in self.r.store.settings_with_prefix('story_mode:'):
            if key.rsplit(':', 1)[-1] == target:
                self.r.store.set_setting(key, '')
        return f'✅ 已重置 QQ {target} 的剧情模式（回到日常模式）'

    # ------------- 私聊关系（galgame）-------------
    def _cmd_relation(self, msg: IncomingMessage) -> str:
        """"关系"：只给阶段名和她的一句感受，不给数字（避免变成刷分攻略）。"""
        rel = getattr(self.r, 'relationship', None)
        if msg.is_group:
            return '这个要私聊我才能说哦……在大家面前，我会害羞的。'
        if rel is None or not rel.enabled():
            return '嗯？你想问什么呀？'
        if rel.is_exempt(msg.sender_id):
            return '你是我的主人呀，这还用问嘛。'
        return rel.feeling(msg.sender_id)

    def _cmd_relation_admin(self, msg: IncomingMessage, text: str) -> str:
        rel = self.r.relationship
        if text in ('好感度开', '好感度关'):
            on = text.endswith('开')
            self._save_and_reload({'galgame_enabled': on})
            return '✅ 私聊好感度系统已' + ('开启' if on else '关闭')
        if text in ('好感度影子开', '好感度影子关'):
            on = text.endswith('开')
            self._save_and_reload({'galgame_shadow': on})
            return ('✅ 影子模式开：评审只记账不生效' if on
                    else '✅ 影子模式关：评审结果开始真实生效')
        parts = text.split()
        if parts[0] == '关系详情' and len(parts) == 2:
            return rel.describe(parts[1])
        if parts[0] == '关系重置' and len(parts) == 2:
            rel.reset(parts[1])
            return f'✅ 已重置 {parts[1]} 的关系（流水保留）'
        if parts[0] == '关系设置' and len(parts) == 3 and parts[2].isdigit():
            return '✅ ' + rel.force_stage(parts[1], int(parts[2]))
        return '用法：关系详情 <QQ> | 关系设置 <QQ> <0-5> | 关系重置 <QQ>'

    # ------------- 主人 -------------
    # 「切换模型」已删除：早期「智慧歌伊模式」的快速/推理双模型早就名存实亡，
    # 现在模型由渠道决定（掉线时在渠道间按稳定性切换），模型池与它对不上了。

    def _cmd_global_passive(self, msg: IncomingMessage, on: bool) -> str:
        self._save_and_reload({'passive_enabled': on})
        return '✅ 已开启全局被动回复' if on else '🔕 已关闭全局被动回复'

    def _cmd_global_active(self, msg: IncomingMessage, on: bool) -> str:
        self._save_and_reload({'active_enabled': on})
        return '✅ 已开启全局主动搭话' if on else '🔕 已关闭全局主动搭话'

    def _cmd_private(self, msg: IncomingMessage, on: bool) -> str:
        self._save_and_reload({'private_enabled': on})
        return '✅ 已开启私聊' if on else '🔕 已关闭私聊'

    def _cmd_reset_context(self, msg: IncomingMessage) -> str:
        self.r.store.clear_turns(msg.session.as_text())
        return '✅ 已重置当前会话上下文'

    def _cmd_reload(self, msg: IncomingMessage) -> str:
        """重新读盘并热更新：配置 / 角色设定 / 世界书 / 语音 / 静默 / 记忆。

        以前漏了记忆、静默、语音三个组件，而且解析失败时照样回「✅ 已重载」——
        用户以为配好了，实际是最容易踩的坑（2026-11-03 审查发现）。
        """
        error = ''
        reload_fn = getattr(self.r, 'reload_config', None)
        if callable(reload_fn):
            error = reload_fn() or ''
        self.r.prompt_builder.set_role_prompt(self.r.load_role_prompt())
        self.r.worldbook.load()
        if self.r.gateway:
            self.r.gateway.reload(self.r.config)
        if error:
            return error + '\n（其它组件已按内存里现有的配置重新读过；磁盘文件没有被覆盖）'
        return '✅ 已重载 配置/角色设定/世界书/权限/群额度/记忆/静默/语音'

    def _cmd_trigger_add(self, msg: IncomingMessage, arg: str) -> str:
        arg = arg.strip()
        if not arg:
            return '格式: 添加触发词 <词>'
        triggers = list(self.r.config.get('triggers', []))
        if arg not in triggers:
            triggers.append(arg)
            self._save_and_reload({'triggers': triggers})
        return f'✅ 触发词: {", ".join(self.r.config.get("triggers", []))}'

    def _cmd_trigger_del(self, msg: IncomingMessage, arg: str) -> str:
        arg = arg.strip()
        if not arg:
            return '格式: 删除触发词 <词>'
        triggers = [t for t in self.r.config.get('triggers', []) if t != arg]
        self._save_and_reload({'triggers': triggers})
        return f'✅ 触发词: {", ".join(triggers) or "（仅剩默认「歌伊」）"}'

    def _cmd_worldbook_toggle(self, msg: IncomingMessage, on: bool) -> str:
        self._save_and_reload({'worldbook_enabled': on})
        return '✅ 世界书已开启（命中关键词才读取设定）' if on else '🔕 世界书已关闭'

    def _cmd_worldbook_reload(self, msg: IncomingMessage) -> str:
        self.r.worldbook.load()
        return f'✅ 世界书已重载，共 {len(self.r.worldbook.entries)} 条'

    def _cmd_emote_toggle(self, msg: IncomingMessage, on: bool) -> str:
        self._save_and_reload({'emote_enabled': on})
        self.r.renderer.config = self.r.config
        return '✅ 立绘已开启' if on else '🔕 立绘已关闭'

    def _cmd_image_toggle(self, msg: IncomingMessage, on: bool) -> str:
        self._save_and_reload({'image_enabled': on})
        images = getattr(self.r, 'images', None)
        if images is not None:
            images.config = self.r.config
        return '✅ 已开启图片理解（群里发图她会看）' if on else '🔕 已关闭图片理解（图片只留占位符，省 token）'

    def _cmd_export_memory(self, msg: IncomingMessage) -> str:
        """导出四层记忆：一份人可读的文本 + 一个自洽的数据库快照。

        产物落在 **state 目录**的「导出」子文件夹。服务器上就是：

            <OlivOS根>/plugin/data/newGeYi/导出/

        拷走那个文件夹即可。注意**没有** `data/` 这一层——
        `get_state_dir()` 只在 `GEYI_DATA_DIR` override 时才走 `state/data`，
        正常部署就是 `plugin/data/newGeYi/`。

        快照走 SQLite 的在线备份 API（`store.snapshot_to`），
        所以**自动包含 WAL 里还没落盘的最新写入**——比手工拷 .sqlite3 安全。
        """
        gateway = getattr(self.r, 'gateway', None)
        channel = gateway.current_summary() if gateway is not None else ''
        try:
            result = export_memory(
                self.r.store, self.r.config,
                worldbook=getattr(self.r, 'worldbook', None),
                channel=channel,
            )
        except Exception as exc:  # noqa: BLE001 - 导出失败不该把插件带崩
            return f'❌ 导出失败：{exc}'
        return result['summary'] + '\n（库里含群聊原文和真实 QQ 号，外发前先脱敏）'

    def _cmd_load_emotes(self, msg: IncomingMessage, arg: str) -> str:
        arg = arg.strip()
        if not arg:
            return '格式: 加载表情包 <文件夹路径>'
        copied = self.r.renderer.setup_emotes(arg)
        total = len(self.r.renderer.emote_table)
        tip = ''
        if copied < total:
            tip = ('\n（整合包里的文件名如果和立绘名称对不上，就在 '
                   'data/emotes.json 里给对应条目写上 file，再跑一次）')
        return f'已加载 {copied}/{total} 张表情包\n来源: {arg}{tip}'

    def _cmd_emote_reload(self, msg: IncomingMessage) -> str:
        count = self.r.renderer.reload_emotes()
        loaded, _lines = self.r.renderer.emote_status()
        return f'✅ 表情包表已重载：{count} 条，其中 {loaded} 张图已就位'

    def _cmd_emote_status(self, msg: IncomingMessage) -> str:
        loaded, lines = self.r.renderer.emote_status()
        total = len(self.r.renderer.emote_table)
        on = '开' if self.r.config.get('emote_enabled', True) else '关'
        dirs = '\n'.join(f'  {path}' for path in self.r.renderer.emote_locations())
        # 装好的不逐条列（21 行太吵），只报缺的
        missing = [line.strip() for line in lines if line.strip().startswith('[--]')]
        parts = [
            '--- 表情包状态 ---',
            f'表情包开关: {on} | 已加载 {loaded}/{total}',
            self.r.renderer.emote_budget(msg.session.as_text()),
            f'查找目录:\n{dirs}',
        ]
        if missing:
            parts.append('缺图:\n  ' + '\n  '.join(missing))
        return '\n'.join(parts)

    # ------------- 群管理员 -------------
    def _cmd_group_passive(self, msg: IncomingMessage, on: bool) -> str:
        key = f'group_passive:{msg.session.as_text()}'
        self.r.store.set_setting(key, '1' if on else '0')
        return '✅ 本群被动回复已开启' if on else '🔕 本群被动回复已关闭'

    def _cmd_group_active(self, msg: IncomingMessage, on: bool) -> str:
        key = f'group_active:{msg.session.as_text()}'
        self.r.store.set_setting(key, '1' if on else '0')
        if on and getattr(self.r, 'attention', None) is not None:
            # 重新打开本群主动 = 手工清零「连续没人接话」的连击与退避，
            # 否则被停止过的群（连击到上限）永远开不回来
            self.r.attention.reset_group(msg.session.as_text())
        return '✅ 本群主动搭话已开启' if on else '🔕 本群主动搭话已关闭'

    def _cmd_group_background_set(self, msg: IncomingMessage, arg: str) -> str:
        arg = arg.strip()
        if not arg:
            return '格式: 设置群背景 <文本>'
        self.r.store.set_background(msg.session.as_text(), arg)
        return '✅ 已设置本群背景'

    def _cmd_group_background_view(self, msg: IncomingMessage) -> str:
        bg = self.r.store.get_background(msg.session.as_text())
        return f'本群背景:\n{bg}' if bg else '本群尚未设置背景'

    # ------------- 伴生原声语音 -------------
    def _cmd_voice_status(self, msg: IncomingMessage) -> str:
        vm = getattr(self.r, 'voice_manager', None)
        if vm is None:
            return '语音子系统未加载'
        return vm.get_status_report()

    def _cmd_voice_test(self, msg: IncomingMessage, arg: str = '') -> str:
        vm = getattr(self.r, 'voice_manager', None)
        if vm is None:
            return '语音子系统未加载'

        from ..voice.presets import normalize_emotion, get_random_preset
        parts = arg.strip().split(maxsplit=1)
        if len(parts) == 0:
            emotion = "日常"
            japanese_text = get_random_preset(emotion)
        elif len(parts) == 1:
            emotion = normalize_emotion(parts[0])
            japanese_text = get_random_preset(emotion)
        else:
            emotion = normalize_emotion(parts[0])
            japanese_text = parts[1].strip()

        self.r.reply(msg, f"正在向本地主机请求合成测试语音 [{emotion}|{japanese_text}]…")

        def _worker():
            group_id = str(msg.session.target_id) if msg.is_group else ""
            sender_name = msg.sender_name
            cq, info = vm.client.synthesize(
                japanese_text,
                emotion,
                group_id=group_id,
                sender_name=sender_name,
                # 运维手动测试：无视熔断窗口硬试，否则网关刚挂时会直接回「离线保护期」
                force=True,
            )
            b64 = info.get("base64")
            if not b64 and cq:
                m = re.search(r'base64://([A-Za-z0-9+/=]+)', cq)
                if m:
                    b64 = m.group(1)

            if b64:
                vm.note_probe_result(True)
                sess_type = 'group' if msg.is_group else 'private'
                sent = vm.send_voice_message(sess_type, msg.session.target_id, b64, event=msg.event)
                if not sent:
                    self.r.reply(msg, "❌ 语音发送失败（底层驱动未响应）")
            else:
                # 只有网关真的不可用才把状态翻成离线（业务类错误不该触发降级通知）
                if not vm.client.health_cached()[0]:
                    vm.note_probe_result(False, str(info.get('error') or '测试合成失败'))
                err = info.get('error', '未知错误')
                self.r.reply(msg, f"❌ 测试语音合成失败: {err}")

        threading.Thread(target=_worker, daemon=True).start()
        return ""

    def _cmd_voice_toggle(self, msg: IncomingMessage, enable: bool) -> str:
        self._save_and_reload({'voice_enabled': enable})
        if hasattr(self.r, 'voice_manager'):
            self.r.voice_manager.reload(self.r.config)
        return f"✅ 已{'开启' if enable else '关闭'}伴生语音输出"

    def _cmd_voice_reload(self, msg: IncomingMessage) -> str:
        reload_fn = getattr(self.r, 'reload_config', None)
        if callable(reload_fn):
            reload_fn()
        if hasattr(self.r, 'voice_manager'):
            self.r.voice_manager.reload(self.r.config)
        return "✅ 已重新加载语音配置与预设词库"

    # ------------- 分发 -------------
    _OWNER_CMD_EXACT = {
        '全局被动开', '全局被动关', '开启私聊', '关闭私聊',
        '私聊用量', '私聊触发词开', '私聊触发词关',
        '休眠状态', '休眠开', '休眠关', '休眠重置',
        '重置上下文', '重载设定', '世界书开', '世界书关', '世界书重载',
        '好感度开', '好感度关', '好感度影子开', '好感度影子关',
        '立绘状态', '立绘重载', '开启立绘', '关闭立绘',
        '表情包状态', '表情包重载', '开启表情包', '关闭表情包',
        '主动搭话开', '主动搭话关',
        '图片理解开', '图片理解关',
        '语音状态', '开启语音', '关闭语音', '语音重载', '测试语音',
        '.voice status', '.voice test',
        '渠道状态', '记忆状态', '导出记忆', '搭话观察',
        '赞助名单', '群聊记忆列表', '称呼列表', '剧情模式列表',
        '解除静默', '解除本群静默', '静默列表', '测试通知', '通知测试',
        '面板地址', '歌伊面板', '调试台',
        '搭话影子开', '搭话影子关',
        '切换模型',
    }
    _OWNER_CMD_PREFIX = (
        '添加触发词 ', '删除触发词 ', '加载立绘 ', '加载表情包 ', '设置key ', '切换渠道 ', '切换模型 ',
        '测试语音 ', '.voice test ', '解除静默 ',
        '关系详情 ', '关系设置 ', '关系重置 ',
        '赞助加 ', '赞助删 ',
        '休眠时间 ',
        '群聊记忆重置 ',
        '称呼重置 ',
        '剧情模式重置 ',
    )

    @classmethod
    def _is_owner_cmd(cls, text: str) -> bool:
        if text in cls._OWNER_CMD_EXACT:
            return True
        return any(text.startswith(p) for p in cls._OWNER_CMD_PREFIX)

    def try_handle(self, msg: IncomingMessage) -> bool:
        """命令入口：任何异常都不能把用户的消息吞掉（既不回复也不报错）。"""
        try:
            return self._dispatch(msg)
        except plugin_config.ConfigWriteBlocked as exc:
            logger.warning('配置写盘被拒绝：%s', exc)
            self.r.reply(msg, '⚠ 没有写盘：%s' % exc)
            return True
        except Exception:  # noqa: BLE001 - 命令出错退回聊天流程，别整条消息消失
            logger.exception('命令处理失败：%r', (getattr(msg, 'text', '') or '')[:60])
            return False

    def _dispatch(self, msg: IncomingMessage) -> bool:
        text = msg.text.strip()
        if not text:
            return False

        # B站漫游专属指令互斥拦截：交由附属插件处理，本体直接让出不回复
        # 核心防误判：如果含有视频链接、卡片或BV号，说明是视频分享，必须由本体回复，不得拦截
        is_video_share = bool(re.search(r'(?:BV[0-9A-Za-z]{10}|b23\.tv|bilibili\.com)', text, re.I))
        if not is_video_share:
            v_ctx = getattr(self.r, 'video_context', None)
            if v_ctx is not None and hasattr(v_ctx, 'is_wander_command') and v_ctx.is_wander_command(text):
                return True
            triggers = ('.bili wander', '歌伊刷个视频', '歌伊看个视频', '歌伊看视频', '歌伊随机看个视频', '歌伊去刷b站', '歌伊去刷会儿b站', '歌伊看b站', '歌伊去摸鱼', '歌伊摸鱼')
            if any(cmd in text.lower() for cmd in triggers):
                return True

        p = self.r.permissions
        deny = '这个只有骰主或主人能用哦~'
        is_master = p.is_master(msg)

        # 骰主/主人命令：未授权者一律明确拒绝，不再落入聊天流程
        if self._is_owner_cmd(text) and not is_master:
            self.r.reply(msg, deny)
            return True

        # 公共命令
        if text == '歌伊菜单':
            self.r.reply(msg, self._cmd_menu(msg))
            return True
        if text in ('歌伊管理菜单', '管理菜单', '管理员菜单', '群管菜单'):
            # 群管或骰主才看得到；普通用户给一句明确的指路，不静默丢弃
            if not (p.is_master(msg) or p.is_group_admin(msg)):
                self.r.reply(msg, '这个菜单是群管和骰主用的哦～想看我能做什么，发「歌伊菜单」。')
                return True
            self.r.reply(msg, self._cmd_admin_menu(msg))
            return True
        if text in ('歌伊状态', '查看状态'):
            self.r.reply(msg, self._cmd_status(msg))
            return True
        if text == '今日用量':
            self.r.reply(msg, self._cmd_usage(msg))
            return True
        if text.startswith('记住 '):
            self.r.reply(msg, self._cmd_remember(msg, text[3:]))
            return True
        if text in ('我的记忆', '查看记忆', '歌伊看我'):
            self.r.reply(msg, self._cmd_view_my_memory(msg))
            return True
        for prefix in ('删除记忆 ', '删除记忆：', '删除记忆:'):
            if text.startswith(prefix):
                self.r.reply(msg, self._cmd_delete_memory(msg, text[len(prefix):]))
                return True
        if text.startswith('删除记忆') and len(text) > 4 and text[4:].strip().isdigit():
            self.r.reply(msg, self._cmd_delete_memory(msg, text[4:]))
            return True
        if text == '删除记忆':
            self.r.reply(msg, '提示：请发送「删除记忆 <序号>」，例如「删除记忆 1」。（序号可在「我的记忆」中查看）')
            return True
        if text == '忘了我':
            self.r.reply(msg, self._cmd_forget_me(msg, force_confirm=False))
            return True
        if text in ('确认忘了我', '确认忘记我'):
            self.r.reply(msg, self._cmd_forget_me(msg, force_confirm=True))
            return True
        # 自定义称呼：改的是"歌伊怎么叫你"（全局一个，按 QQ 号存）
        if text in ('我的称呼', '叫我'):
            self.r.reply(msg, self._cmd_alias_show(msg))
            return True
        # 必须带分隔符才当指令：「叫我起床」这种是正常说话，不能吃掉
        for prefix in ('叫我 ', '叫我：', '叫我:'):
            if text.startswith(prefix):
                self.r.reply(msg, self._cmd_alias_set(msg, text[len(prefix):]))
                return True
        if text == '称呼重置':
            self.r.reply(msg, self._cmd_alias_reset(msg))
            return True
        # 私聊自己决定「要不要把群聊记忆带进来」（会话级，任何人可改自己的）
        if text in ('群聊记忆', '群聊记忆状态'):
            self.r.reply(msg, self._cmd_share_status(msg))
            return True
        if text in ('群聊记忆开', '群聊记忆关'):
            self.r.reply(msg, self._cmd_share_toggle(msg, text == '群聊记忆开'))
            return True
        if text in ('群聊记忆默认', '群聊记忆跟随默认', '群聊记忆重置'):
            self.r.reply(msg, self._cmd_share_default(msg))
            return True
        if text in ('关系', '好感度'):
            self.r.reply(msg, self._cmd_relation(msg))
            return True
        # 今日私聊额度：以前只有骰主的「私聊用量」能看，用户被限额挡住时完全不知情
        if text in ('今日额度', '我的额度', '私聊额度', '额度'):
            gate = getattr(self.r, 'private_gate', None)
            if gate is None:
                self.r.reply(msg, '额度信息暂时读不到，等一会儿再试试～')
            elif msg.is_group:
                self.r.reply(msg, '私聊里才能看今日额度哦～想聊的话直接私信我吧。')
            else:
                self.r.reply(msg, gate.user_usage_text(msg.session.as_text()))
            return True
        if text == '世界书状态':
            self.r.reply(msg, self._cmd_worldbook_status(msg))
            return True
        if text in ('剧情模式', '剧情模式状态', '探索模式', '故事模式'):
            self.r.reply(msg, self._cmd_story_mode_status(msg))
            return True
        if text in ('剧情模式开', '开启剧情模式', '进入剧情模式', '探索模式开', '开启探索模式', '故事模式开'):
            self.r.reply(msg, self._cmd_story_mode_toggle(msg, True))
            return True
        if text in ('剧情模式关', '关闭剧情模式', '退出剧情模式', '探索模式关', '关闭探索模式', '故事模式关'):
            self.r.reply(msg, self._cmd_story_mode_toggle(msg, False))
            return True
        if text == '查看触发词':
            self.r.reply(msg, '触发词: ' + '、'.join(self.r.config.get('triggers', [])))
            return True

        # 群管理员命令（主人或群管）：必须在「非主人早退」之前判断，
        # 否则非主人的群管永远走不到这里
        if (text in ('本群被动开', '本群被动关', '本群主动开', '本群主动关')
                or text == '查看群背景' or text.startswith('设置群背景 ')):
            if not (is_master or p.is_group_admin(msg)):
                self.r.reply(msg, deny)
                return True
            if text in ('本群被动开', '本群被动关'):
                self.r.reply(msg, self._cmd_group_passive(msg, text.endswith('开')))
            elif text in ('本群主动开', '本群主动关'):
                self.r.reply(msg, self._cmd_group_active(msg, text.endswith('开')))
            elif text == '查看群背景':
                self.r.reply(msg, self._cmd_group_background_view(msg))
            else:
                self.r.reply(msg, self._cmd_group_background_set(msg, text[6:]))
            return True

        # 骰主/主人命令
        if not is_master:
            return False  # 未授权且未命中公开命令 -> 继续走聊天流程
        if text == '切换模型' or text.startswith('切换模型 '):
            # 墓碑：这指令删了，但主人多半会照着旧习惯敲一次——
            # 落到聊天流程里她会用角色口吻答一句莫名其妙的话，不如直接说清楚
            self.r.reply(msg, '「切换模型」已经删掉啦（早期智慧歌伊模式的残留）。\n'
                              '现在模型由渠道决定：发「渠道状态」看列表、「切换渠道 <名称/序号>」手动切。')
            return True
        if text == '渠道状态':
            self.r.reply(msg, self._cmd_channels(msg))
            return True
        if text == '赞助名单':
            self.r.reply(msg, self._cmd_sponsor_list(msg))
            return True
        if text.startswith('赞助加 '):
            self.r.reply(msg, self._cmd_sponsor_add(msg, text[4:]))
            return True
        if text.startswith('赞助删 '):
            self.r.reply(msg, self._cmd_sponsor_remove(msg, text[4:]))
            return True
        if text == '私聊用量':
            gate = getattr(self.r, 'private_gate', None)
            if gate is None:
                self.r.reply(msg, '私聊闸门还没初始化（重载一次试试）。')
            else:
                key = msg.session.as_text() if not msg.is_group else ''
                self.r.reply(msg, '【私聊并发保护】\n' + gate.usage_text(key))
            return True
        if text in ('私聊触发词开', '私聊触发词关'):
            on = text.endswith('开')
            self._save_and_reload({'private_trigger_only': on})
            gate = getattr(self.r, 'private_gate', None)
            tip = ('现在私聊只在命中触发词时才回（%s）' % '、'.join(gate.trigger_words())
                   if on and gate else '现在私聊每句都回')
            self.r.reply(msg, '✅ ' + tip)
            return True
        if text == '休眠状态':
            sleep = getattr(self.r, 'sleep', None)
            if sleep is None:
                self.r.reply(msg, '作息模块还没初始化（重载一次试试）。')
            else:
                self.r.reply(msg, '【作息】\n' + sleep.status_text())
            return True
        if text in ('休眠开', '休眠关'):
            on = text.endswith('开')
            self._save_and_reload({'sleep_enabled': on})
            sleep = getattr(self.r, 'sleep', None)
            head = '✅ 休眠已开启' if on else '✅ 休眠已关闭（24 小时都在）'
            self.r.reply(msg, head if sleep is None else head + '\n' + sleep.status_text())
            return True
        if text.startswith('休眠时间'):
            raw = text[len('休眠时间'):].strip()
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d', raw):
                self.r.reply(msg, '格式：休眠时间 <HH:MM-HH:MM>，例如「休眠时间 23:30-07:00」\n'
                                  '（可以跨午夜；起止相同视为不睡）')
                return True
            start, end = raw.split('-', 1)
            if start.strip() == end.strip():
                self.r.reply(msg, '起止时间相同等于不睡，改用「休眠关」更清楚。')
                return True
            self._save_and_reload({'sleep_window': raw})
            sleep = getattr(self.r, 'sleep', None)
            self.r.reply(msg, '✅ 休眠时段已改为 %s%s' % (
                raw, '' if sleep is None else '\n' + sleep.status_text()))
            return True
        if text == '休眠重置':
            sleep = getattr(self.r, 'sleep', None)
            if sleep is None:
                self.r.reply(msg, '作息模块还没初始化（重载一次试试）。')
            else:
                sleep.reset_notice(msg.session.as_text())
                self.r.reply(msg, '✅ 本会话的「已提示过」记录清掉了——下次被叫她会再应一句。')
            return True
        if text.startswith('解除静默 '):
            self.r.reply(msg, self._cmd_clear_rest_any(msg, text[len('解除静默 '):]))
            return True
        if text in ('解除静默', '解除本群静默'):
            self.r.reply(msg, self._cmd_clear_rest(msg))
            return True
        if text == '静默列表':
            self.r.reply(msg, self._cmd_rest_list(msg))
            return True
        if text in ('面板地址', '歌伊面板', '调试台'):
            self.r.reply(msg, self._cmd_panel_url(msg))
            return True
        if text in ('测试通知', '通知测试'):
            self.r.reply(msg, self._cmd_test_notify(msg))
            return True
        if text == '记忆状态':
            self.r.reply(msg, self._cmd_memory_status(msg))
            return True
        if text == '群聊记忆列表':
            self.r.reply(msg, self._cmd_share_list(msg))
            return True
        if text.startswith('群聊记忆重置 '):
            self.r.reply(msg, self._cmd_share_reset(msg, text[len('群聊记忆重置 '):]))
            return True
        if text == '称呼列表':
            self.r.reply(msg, self._cmd_alias_list(msg))
            return True
        if text.startswith('称呼重置 '):
            self.r.reply(msg, self._cmd_alias_admin_reset(msg, text[len('称呼重置 '):]))
            return True
        if text == '剧情模式列表':
            self.r.reply(msg, self._cmd_story_mode_list(msg))
            return True
        if text.startswith('剧情模式重置 '):
            self.r.reply(msg, self._cmd_story_mode_reset(msg, text[len('剧情模式重置 '):]))
            return True
        if text == '导出记忆':
            self.r.reply(msg, self._cmd_export_memory(msg))
            return True
        if text == '搭话观察':
            self.r.reply(msg, self._cmd_active_observe(msg))
            return True
        if text == '搭话影子开':
            self.r.reply(msg, self._cmd_active_shadow(msg, True))
            return True
        if text == '搭话影子关':
            self.r.reply(msg, self._cmd_active_shadow(msg, False))
            return True
        if text.startswith('切换渠道 '):
            self.r.reply(msg, self._cmd_switch_channel(msg, text[len('切换渠道 '):]))
            return True
        if text.startswith('设置key '):
            key = text[len('设置key '):].strip()
            if not key:
                self.r.reply(msg, '格式: 设置key <API Key>')
                return True
            if msg.is_group:
                # 群里发 key = 把钥匙贴在墙上（群历史里会留一份明文）
                self.r.reply(msg, '这个请私聊里发：「设置key <API Key>」。群里发等于把钥匙贴在墙上。')
                return True
            # 只写**当前正在用的那一个渠道**。以前会把同一个 key 灌进所有「还没填 key」
            # 的渠道，而那些渠道可能是别家厂商——key 一通、全线 401，等于把钥匙到处撒。
            # 在副本上改，再一次性提交：写盘失败时内存也不会被带歪。
            providers = copy.deepcopy(self.r.config.get('providers') or [])
            name = str(getattr(self.r.gateway, '_current_name', '') or '')
            hit = False
            for prov in providers:
                if isinstance(prov, dict) and name and prov.get('name') == name:
                    prov['api_key'] = key
                    hit = True
            changes = {'api_key': key}
            if hit:
                changes['providers'] = providers
            self._save_and_reload(changes)
            if self.r.gateway:
                self.r.gateway.reload(self.r.config)
            where = f'渠道「{name}」' if hit else '单渠道模式（api_key）'
            self.r.reply(msg, f'✅ 已设置 {where} 的 API Key（{_mask_key(key)}），已重载网关并立即生效\n'
                              f'数据保存在 {plugin_config.get_config_path()}')
            return True
        if text == '全局被动开':
            self.r.reply(msg, self._cmd_global_passive(msg, True))
            return True
        if text == '全局被动关':
            self.r.reply(msg, self._cmd_global_passive(msg, False))
            return True
        if text == '主动搭话开':
            self.r.reply(msg, self._cmd_global_active(msg, True))
            return True
        if text == '主动搭话关':
            self.r.reply(msg, self._cmd_global_active(msg, False))
            return True
        if text == '开启私聊':
            self.r.reply(msg, self._cmd_private(msg, True))
            return True
        if text == '关闭私聊':
            self.r.reply(msg, self._cmd_private(msg, False))
            return True
        if text == '重置上下文':
            self.r.reply(msg, self._cmd_reset_context(msg))
            return True
        if text == '重载设定':
            self.r.reply(msg, self._cmd_reload(msg))
            return True
        if text.startswith('添加触发词 '):
            self.r.reply(msg, self._cmd_trigger_add(msg, text[5:]))
            return True
        if text.startswith('删除触发词 '):
            self.r.reply(msg, self._cmd_trigger_del(msg, text[5:]))
            return True
        if text == '世界书开':
            self.r.reply(msg, self._cmd_worldbook_toggle(msg, True))
            return True
        if text == '世界书关':
            self.r.reply(msg, self._cmd_worldbook_toggle(msg, False))
            return True
        if text == '世界书重载':
            self.r.reply(msg, self._cmd_worldbook_reload(msg))
            return True
        if (text in ('好感度开', '好感度关', '好感度影子开', '好感度影子关')
                or text.startswith(('关系详情 ', '关系设置 ', '关系重置 '))):
            self.r.reply(msg, self._cmd_relation_admin(msg, text))
            return True
        # 立绘：新叫法是「立绘」，旧的「表情包」保留成别名（老用户/肌肉记忆）
        if text in ('开启立绘', '开启表情包'):
            self.r.reply(msg, self._cmd_emote_toggle(msg, True))
            return True
        if text in ('关闭立绘', '关闭表情包'):
            self.r.reply(msg, self._cmd_emote_toggle(msg, False))
            return True
        for prefix in ('加载立绘 ', '加载表情包 '):
            if text.startswith(prefix):
                self.r.reply(msg, self._cmd_load_emotes(msg, text[len(prefix):]))
                return True
        if text in ('立绘状态', '表情包状态'):
            self.r.reply(msg, self._cmd_emote_status(msg))
            return True
        if text in ('立绘重载', '表情包重载'):
            self.r.reply(msg, self._cmd_emote_reload(msg))
            return True
        if text == '图片理解开':
            self.r.reply(msg, self._cmd_image_toggle(msg, True))
            return True
        if text == '图片理解关':
            self.r.reply(msg, self._cmd_image_toggle(msg, False))
            return True
        # 伴生原声语音指令
        if text in ('语音状态', '.voice status'):
            self.r.reply(msg, self._cmd_voice_status(msg))
            return True
        if text in ('测试语音', '.voice test'):
            res = self._cmd_voice_test(msg, '')
            if res:
                self.r.reply(msg, res)
            return True
        for prefix in ('测试语音 ', '.voice test '):
            if text.startswith(prefix):
                res = self._cmd_voice_test(msg, text[len(prefix):])
                if res:
                    self.r.reply(msg, res)
                return True
        if text == '开启语音':
            self.r.reply(msg, self._cmd_voice_toggle(msg, True))
            return True
        if text == '关闭语音':
            self.r.reply(msg, self._cmd_voice_toggle(msg, False))
            return True
        if text == '语音重载':
            self.r.reply(msg, self._cmd_voice_reload(msg))
            return True
        return False