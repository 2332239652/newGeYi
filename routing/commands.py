# -*- encoding: utf-8 -*-
"""命令路由：权限在命令表里声明，不散落在每个 if 分支。"""

from __future__ import annotations

import os
import re
import threading
import time

from .. import config as plugin_config
from ..config import SKIP_ACTIVE
from ..memory.exporter import export_memory
from ..models import IncomingMessage
from ..presentation.renderer import load_emote_table  # noqa: F401  （保留导出）


def _mask_key(key: str) -> str:
    """回显时只显示掩码，明文不落聊天记录。"""
    key = key.strip()
    if len(key) <= 8:
        return '****'
    return key[:4] + '****' + key[-4:]


class CommandRouter:
    def __init__(self, runtime):
        self.r = runtime

    # ------------- 公共 -------------
    def _cmd_menu(self, msg: IncomingMessage) -> str:
        cfg = self.r.config
        wb_state = '开' if cfg.get('worldbook_enabled') else '关'
        active_state = '开' if cfg.get('active_enabled') else '关'
        if cfg.get('active_enabled') and cfg.get('active_shadow', True):
            active_state = '开(影子)'          # 只记录不真发，别让人以为已经上线了
        memory_state = '开' if cfg.get('memory_curator_enabled', True) else '关'
        gateway = getattr(self.r, 'gateway', None)
        channel = gateway.current_summary() if gateway is not None else '未配置'
        lines = [
            f'--- 歌伊菜单 v{plugin_config.PLUGIN_VERSION} ---',
            '',
            '[状态] 歌伊状态 | 今日用量',
            '[对话] 记住 <内容> | 忘了我',
            '[世界书] 世界书状态',
            '',
            '[骰主/主人] 渠道状态 | 切换渠道 <名称/序号> | 切换渠道 自动',
            '[骰主/主人] 解除静默（提前结束本群的自我保护静默） | 测试通知',
            '[骰主/主人] 记忆状态 | 导出记忆',
            '[骰主/主人] 全局被动开/关 | 开启/关闭私聊',
            '[骰主/主人] 主动搭话开/关（全局） | 搭话观察 | 搭话影子开/关',
            '[骰主/主人] 设置key <API Key> | 重置上下文 | 重载设定',
            '[骰主/主人] 添加/删除触发词 <词> | 查看触发词',
            '[骰主/主人] 世界书开/关 | 世界书重载',
            '[骰主/主人] 图片理解开/关',
            '[骰主/主人] 表情包状态 | 开启/关闭表情包 | 加载表情包 <路径> | 表情包重载',
            '[骰主/主人] 语音状态 | 测试语音 [情绪] [日文] | 开启/关闭语音 | 语音重载',
            '',
            '[群管/主人] 本群被动开/关 | 本群主动开/关',
            '[群管/主人] 设置群背景 <文本> | 查看群背景',
            '',
            f'当前渠道: {channel}',
            f'世界书: {wb_state} | 主动搭话: {active_state} | 自动记忆: {memory_state}',
            f'图片理解: {"开" if cfg.get("image_enabled", True) else "关"} | 伴生语音: {"开" if cfg.get("voice_enabled", True) else "关"}',
        ]
        return '\n'.join(lines)

    def _cmd_status(self, msg: IncomingMessage) -> str:
        cfg = self.r.config
        today = self.r.store.today_stats()
        budget = int(cfg.get('daily_call_budget', 500))
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
            if left:
                reason = rest.reason(msg.session.as_text()) or '未说明'
                rest_line = f'自我保护静默: 本群还剩 {left // 60} 分 {left % 60} 秒（{reason}）'
            else:
                rest_line = '自我保护静默: 未触发'
        except Exception:  # noqa: BLE001
            rest_line = '自我保护静默: 读取失败'
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
            f'今日调用: {today["calls"]}/{budget} | 今日输出token: {today["output_tokens"]}',
            f'今日主动搭话: {today["active_calls"]} 次',
            f'触发词: {", ".join(cfg.get("triggers", [])) or "歌伊"}',
        ])

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

    def _cmd_clear_rest(self, msg: IncomingMessage) -> str:
        """提前解除本会话的自我保护静默（骰主专用）。"""
        rest = getattr(self.r, 'rest', None)
        if rest is None:
            return '静默模块未初始化'
        session_key = msg.session.as_text()
        left = rest.remaining(session_key)
        reason = rest.reason(session_key)
        had = rest.clear(session_key)
        if not had:
            return '本会话当前不在静默中'
        return (f'✅ 已解除本会话的自我保护静默（原本还剩 {left // 60} 分 {left % 60} 秒'
                f'{"，" + reason if reason else ""}）\n她现在会正常回话了')

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
        cfg = self.r.config
        cfg['active_shadow'] = on
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
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
        budget = int(self.r.config.get('daily_call_budget', 500))
        out_budget = int(self.r.config.get('daily_output_token_budget', 100000))
        return (
            f'今日调用 {today["calls"]}/{budget} 次，输出 {today["output_tokens"]}/{out_budget} tokens\n'
            f'其中主动搭话触发 {today["active_calls"]} 次'
        )

    def _cmd_remember(self, msg: IncomingMessage, arg: str) -> str:
        fact = arg.strip()
        if not fact:
            return '格式: 记住 <内容>，例如「记住我喜欢喝牛奶」'
        self.r.memory.add_fact(msg, fact)
        return '✅ 记住啦~（「忘了我」可以清空）'

    def _cmd_forget_me(self, msg: IncomingMessage) -> str:
        count = self.r.memory.clear_user(msg)
        return f'✅ 已忘记关于你的 {count} 条记忆'

    def _cmd_worldbook_status(self, msg: IncomingMessage) -> str:
        on = self.r.config.get('worldbook_enabled', False)
        count = len(self.r.worldbook.entries)
        return f'世界书: {"开" if on else "关"} | 已加载条目: {count} | 命中关键词才注入，未命中零成本'

    # ------------- 主人 -------------
    # 「切换模型」已删除：早期「智慧歌伊模式」的快速/推理双模型早就名存实亡，
    # 现在模型由渠道决定（掉线时在渠道间按稳定性切换），模型池与它对不上了。

    def _cmd_global_passive(self, msg: IncomingMessage, on: bool) -> str:
        cfg = self.r.config
        cfg['passive_enabled'] = on
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
        return '✅ 已开启全局被动回复' if on else '🔕 已关闭全局被动回复'

    def _cmd_global_active(self, msg: IncomingMessage, on: bool) -> str:
        cfg = self.r.config
        cfg['active_enabled'] = on
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
        return '✅ 已开启全局主动搭话' if on else '🔕 已关闭全局主动搭话'

    def _cmd_private(self, msg: IncomingMessage, on: bool) -> str:
        cfg = self.r.config
        cfg['private_enabled'] = on
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
        return '✅ 已开启私聊' if on else '🔕 已关闭私聊'

    def _cmd_reset_context(self, msg: IncomingMessage) -> str:
        self.r.store.clear_turns(msg.session.as_text())
        return '✅ 已重置当前会话上下文'

    def _cmd_reload(self, msg: IncomingMessage) -> str:
        self.r.config = plugin_config.load_config()
        role = self.r.load_role_prompt()
        self.r.prompt_builder.config = self.r.config
        self.r.prompt_builder.set_role_prompt(role)
        self.r.worldbook.load()
        self.r.renderer.config = self.r.config
        if self.r.gateway:
            self.r.gateway.reload(self.r.config)
        if getattr(self.r, 'curator', None):
            self.r.curator.config = self.r.config
        if getattr(self.r, 'images', None):
            self.r.images.config = self.r.config
        if getattr(self.r, 'attention', None):
            self.r.attention.config = self.r.config
        if getattr(self.r, 'permissions', None):
            self.r.permissions.config = self.r.config
        if getattr(self.r, 'group_profile', None):
            self.r.group_profile.config = self.r.config
        return '✅ 已重载 配置/角色设定/世界书/权限/群额度'

    def _cmd_trigger_add(self, msg: IncomingMessage, arg: str) -> str:
        arg = arg.strip()
        if not arg:
            return '格式: 添加触发词 <词>'
        cfg = self.r.config
        triggers = list(cfg.get('triggers', []))
        if arg not in triggers:
            triggers.append(arg)
            cfg['triggers'] = triggers
            plugin_config.save_config(cfg)
            self.r.config = plugin_config.load_config()
        return f'✅ 触发词: {", ".join(cfg.get("triggers", []))}'

    def _cmd_trigger_del(self, msg: IncomingMessage, arg: str) -> str:
        arg = arg.strip()
        if not arg:
            return '格式: 删除触发词 <词>'
        cfg = self.r.config
        triggers = [t for t in cfg.get('triggers', []) if t != arg]
        cfg['triggers'] = triggers
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
        return f'✅ 触发词: {", ".join(triggers) or "（仅剩默认「歌伊」）"}'

    def _cmd_worldbook_toggle(self, msg: IncomingMessage, on: bool) -> str:
        cfg = self.r.config
        cfg['worldbook_enabled'] = on
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
        return '✅ 世界书已开启（命中关键词才读取设定）' if on else '🔕 世界书已关闭'

    def _cmd_worldbook_reload(self, msg: IncomingMessage) -> str:
        self.r.worldbook.load()
        return f'✅ 世界书已重载，共 {len(self.r.worldbook.entries)} 条'

    def _cmd_emote_toggle(self, msg: IncomingMessage, on: bool) -> str:
        cfg = self.r.config
        cfg['emote_enabled'] = on
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
        self.r.renderer.config = self.r.config
        return '✅ 立绘已开启' if on else '🔕 立绘已关闭'

    def _cmd_image_toggle(self, msg: IncomingMessage, on: bool) -> str:
        cfg = self.r.config
        cfg['image_enabled'] = on
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
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
            )
            b64 = info.get("base64")
            if not b64 and cq:
                m = re.search(r'base64://([A-Za-z0-9+/=]+)', cq)
                if m:
                    b64 = m.group(1)

            if b64:
                sess_type = 'group' if msg.is_group else 'private'
                sent = vm.send_voice_message(sess_type, msg.session.target_id, b64, event=msg.event)
                if not sent:
                    self.r.reply(msg, "❌ 语音发送失败（底层驱动未响应）")
            else:
                err = info.get('error', '未知错误')
                self.r.reply(msg, f"❌ 测试语音合成失败: {err}")

        threading.Thread(target=_worker, daemon=True).start()
        return ""

    def _cmd_voice_toggle(self, msg: IncomingMessage, enable: bool) -> str:
        cfg = self.r.config
        cfg['voice_enabled'] = enable
        plugin_config.save_config(cfg)
        self.r.config = plugin_config.load_config()
        if hasattr(self.r, 'voice_manager'):
            self.r.voice_manager.reload(self.r.config)
        return f"✅ 已{'开启' if enable else '关闭'}伴生语音输出"

    def _cmd_voice_reload(self, msg: IncomingMessage) -> str:
        self.r.config = plugin_config.load_config()
        if hasattr(self.r, 'voice_manager'):
            self.r.voice_manager.reload(self.r.config)
        return "✅ 已重新加载语音配置与预设词库"

    # ------------- 分发 -------------
    _OWNER_CMD_EXACT = {
        '全局被动开', '全局被动关', '开启私聊', '关闭私聊',
        '重置上下文', '重载设定', '世界书开', '世界书关', '世界书重载',
        '立绘状态', '立绘重载', '开启立绘', '关闭立绘',
        '表情包状态', '表情包重载', '开启表情包', '关闭表情包',
        '主动搭话开', '主动搭话关',
        '图片理解开', '图片理解关',
        '语音状态', '开启语音', '关闭语音', '语音重载', '测试语音',
        '.voice status', '.voice test',
        '渠道状态', '记忆状态', '导出记忆', '搭话观察',
        '解除静默', '解除本群静默', '测试通知', '通知测试',
        '搭话影子开', '搭话影子关',
        '切换模型',
    }
    _OWNER_CMD_PREFIX = (
        '添加触发词 ', '删除触发词 ', '加载立绘 ', '加载表情包 ', '设置key ', '切换渠道 ', '切换模型 ',
        '测试语音 ', '.voice test ',
    )

    @classmethod
    def _is_owner_cmd(cls, text: str) -> bool:
        if text in cls._OWNER_CMD_EXACT:
            return True
        return any(text.startswith(p) for p in cls._OWNER_CMD_PREFIX)

    def try_handle(self, msg: IncomingMessage) -> bool:
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
        if text in ('歌伊状态', '查看状态'):
            self.r.reply(msg, self._cmd_status(msg))
            return True
        if text == '今日用量':
            self.r.reply(msg, self._cmd_usage(msg))
            return True
        if text.startswith('记住 '):
            self.r.reply(msg, self._cmd_remember(msg, text[3:]))
            return True
        if text == '忘了我':
            self.r.reply(msg, self._cmd_forget_me(msg))
            return True
        if text == '世界书状态':
            self.r.reply(msg, self._cmd_worldbook_status(msg))
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
        if text in ('解除静默', '解除本群静默'):
            self.r.reply(msg, self._cmd_clear_rest(msg))
            return True
        if text in ('测试通知', '通知测试'):
            self.r.reply(msg, self._cmd_test_notify(msg))
            return True
        if text == '记忆状态':
            self.r.reply(msg, self._cmd_memory_status(msg))
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
            cfg = self.r.config
            cfg['api_key'] = key
            # 同步更新当前正在使用的渠道中的 api_key
            current_provider_name = getattr(self.r.gateway, 'current_provider_name', None) or getattr(self.r.gateway, '_current_name', None)
            if 'providers' in cfg and isinstance(cfg['providers'], list):
                for prov in cfg['providers']:
                    if isinstance(prov, dict):
                        if current_provider_name and prov.get('name') == current_provider_name:
                            prov['api_key'] = key
                        elif not prov.get('api_key'):
                            prov['api_key'] = key
            plugin_config.save_config(cfg)
            self.r.config = plugin_config.load_config()
            if self.r.gateway:
                self.r.gateway.reload(self.r.config)
            self.r.reply(msg, f'✅ 已设置 API Key（{_mask_key(key)}），已重载网关并立即生效\n数据保存在 data 目录 config.json')
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