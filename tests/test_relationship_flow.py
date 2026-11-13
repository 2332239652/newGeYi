# -*- encoding: utf-8 -*-
"""好感度系统的端到端 + 面板契约测试（离线：假网关、假事件，不联网）。

端到端走的是真实的 `Runtime.process_job`：
消息 → prompt 拼装（关系块 / 世界书 / 记忆）→ 模型回复 → 越界审计与重写 → 发送 → 异步评审 → 入账。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path

os.environ['GEYI_DATA_DIR'] = tempfile.mkdtemp(prefix='geyi_relpanel_import_')

from newGeYi import config as C  # noqa: E402
from newGeYi.models import IncomingMessage, ModelResult, SessionKey  # noqa: E402
from newGeYi.panel import EVENT_NAME, Panel  # noqa: E402
from newGeYi.panel_helpers import PanelError, validate_patch  # noqa: E402
from newGeYi.runtime.app import Runtime  # noqa: E402

OWNER = '9000000001'


class FakeEvent:
    def __init__(self):
        self.sent = []

    def send(self, kind, target, text):
        self.sent.append((kind, target, text))
        return True


class ScriptedGateway:
    """按 kind 返回预设结果，并记下每次请求（用来断言 prompt）。"""

    def __init__(self, chat_replies, verdict=None):
        self.chat_replies = list(chat_replies)
        self.verdict = verdict
        self.chat_requests = []
        self.judge_calls = 0
        self.config = {}

    def chat(self, req, kind='chat', session_key=''):
        if kind == 'affection':
            self.judge_calls += 1
            if self.verdict is None:
                return ModelResult(ok=False, error_code='no_provider')
            return ModelResult(ok=True, text=json.dumps(self.verdict), model='fake')
        self.chat_requests.append(req)
        text = self.chat_replies.pop(0) if self.chat_replies else '嗯嗯～'
        return ModelResult(ok=True, text=text, model='fake', input_tokens=10, output_tokens=5)


def private_msg(text, uid, event, name='Oracle'):
    session = SessionKey('bot', 'qq', 'onebot', 'private', '-', uid)
    return IncomingMessage(session=session, message_id='1', sender_id=uid, sender_name=name,
                           text=text, raw_text=text, is_group=False, mentioned_bot=False,
                           event=event, proc=None, arrival_seq=1, sender_is_bot=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get('GEYI_DATA_DIR')
        self.temp = Path(tempfile.mkdtemp(prefix='geyi_relpanel_case_'))
        os.environ['GEYI_DATA_DIR'] = str(self.temp)
        C._CONFIG_STATE['error'] = ''
        self.rt = Runtime()
        self.rt.config.update({'private_enabled': True, 'galgame_enabled': True, 'galgame_shadow': False,
                               'worldbook_enabled': True, 'owner_ids': [OWNER], 'time_awareness_enabled': False,
                               'emote_enabled': False, 'voice_enabled': False})
        self.rt.prompt_builder.set_role_prompt('你是歌伊，测试人设。')
        self.svc = self.rt.relationship
        self.uid = str(int(time.time() * 1000) % 10 ** 9 + 300000)

    def tearDown(self):
        self.rt.scheduler.shutdown()
        self.rt.voice_manager._executor.shutdown(wait=False)
        self.rt.relationship.shutdown()
        self.rt.store.close()
        if self.previous is None:
            os.environ.pop('GEYI_DATA_DIR', None)
        else:
            os.environ['GEYI_DATA_DIR'] = self.previous

    def drain(self):
        self.svc._pool.submit(lambda: None).result(timeout=10)

    def run_job(self, text, uid=None, replies=('嗯嗯～',), verdict=None):
        uid = uid or self.uid
        gateway = ScriptedGateway(replies, verdict)
        self.rt.gateway = gateway
        self.svc.gateway = gateway
        event = FakeEvent()
        msg = private_msg(text, uid, event)
        self.rt.process_job(msg, 'passive')
        self.drain()
        return gateway, event


class EndToEndTest(Base):
    def test_stranger_rush_gets_overstep_rewrite_and_penalty(self):
        gateway, event = self.run_job(
            '我爱你，做我老婆吧',
            replies=('人家也喜欢你～（扑进你怀里）', '诶？！才刚认识呀，突然说这个我不知道该怎么回答……'),
            verdict={'delta': 2, 'tag': 'care', 'advance_level': 3, 'mood': '害羞', 'event_done': False,
                     'note': ''})
        first_system = gateway.chat_requests[0].messages[0]['content']
        self.assertIn('阶段：陌生', first_system)
        self.assertNotIn('【重写要求】', first_system)
        self.assertEqual(len(gateway.chat_requests), 2, '越界后应重写一次')
        self.assertIn('【重写要求】', gateway.chat_requests[1].messages[0]['content'])
        self.assertEqual(len(event.sent), 1)
        self.assertIn('才刚认识', event.sent[0][2])
        self.assertNotIn('喜欢你', event.sent[0][2])
        rel = self.svc.load(self.uid)
        self.assertEqual(rel.stage, 0)
        self.assertEqual(rel.bad_streak, 1)
        row = self.svc.recent_log(self.uid, 1)[0]
        self.assertEqual(row['tag'], 'flirt_early')
        self.assertLess(row['delta_applied'], 0.5)

    def test_rewrite_still_overstepping_is_sent_but_logged(self):
        _gateway, event = self.run_job('在吗', replies=('想你了～', '真的好想你呀～'),
                                       verdict={'delta': 0, 'tag': 'neutral', 'advance_level': 1,
                                                'mood': '', 'event_done': False, 'note': ''})
        self.assertEqual(len(event.sent), 1, '重写仍越界时保留原回复，而不是不回')

    def test_clean_reply_makes_one_chat_call_and_one_judge_call(self):
        gateway, event = self.run_job(
            '今天累不累呀，给你讲个笑话', replies=('诶，好呀好呀，你讲！',),
            verdict={'delta': 2, 'tag': 'care', 'advance_level': 1, 'mood': '开心', 'event_done': False,
                     'note': ''})
        self.assertEqual(len(gateway.chat_requests), 1)
        self.assertEqual(gateway.judge_calls, 1)
        self.assertGreater(self.svc.load(self.uid).affection, 0)
        self.assertEqual(self.svc.load(self.uid).mood, '开心')

    def test_second_turn_prompt_sees_mood_and_history(self):
        self.run_job('今天累不累呀，给你讲个笑话', replies=('诶，好呀好呀，你讲！',),
                     verdict={'delta': 1, 'tag': 'humor', 'advance_level': 1, 'mood': '开心',
                              'event_done': False, 'note': ''})
        gateway, _ = self.run_job('我讲完啦', replies=('哈哈哈～',),
                                  verdict={'delta': 0, 'tag': 'neutral', 'advance_level': 0, 'mood': '',
                                           'event_done': False, 'note': ''})
        system = gateway.chat_requests[0].messages[0]['content']
        self.assertIn('你此刻的心情：开心', system)
        self.assertTrue(any('给你讲个笑话' in m['content'] for m in gateway.chat_requests[0].messages[1:]
                            if isinstance(m['content'], str)))

    def test_judge_outage_changes_nothing_and_reply_still_goes_out(self):
        gateway, event = self.run_job('说点有意思的话题吧', replies=('好呀好呀～',), verdict=None)
        self.assertEqual(len(event.sent), 1)
        self.assertLessEqual(self.svc.load(self.uid).affection, 1.0)
        self.assertEqual(self.svc.recent_log(self.uid, 1)[0]['tag'], 'judge_fail')

    def test_shadow_mode_does_not_apply_but_logs(self):
        self.rt.config['galgame_shadow'] = True
        self.run_job('今天累不累呀，给你讲个笑话', replies=('好呀～',),
                     verdict={'delta': 2, 'tag': 'care', 'advance_level': 1, 'mood': '开心',
                              'event_done': False, 'note': ''})
        rel = self.svc.load(self.uid)
        self.assertLessEqual(rel.affection, 1.0)
        self.assertEqual(rel.mood, '')
        row = self.svc.recent_log(self.uid, 1)[0]
        self.assertEqual((row['shadow'], row['delta_raw']), (1, 2))

    def test_owner_is_exempt_end_to_end(self):
        gateway, event = self.run_job('我爱你', uid=OWNER, replies=('主人～我也是啦！',),
                                      verdict={'delta': -3, 'tag': 'rude', 'advance_level': 3, 'mood': '',
                                               'event_done': False, 'note': ''})
        self.assertNotIn('【此刻你与对方的关系', gateway.chat_requests[0].messages[0]['content'])
        self.assertEqual(gateway.judge_calls, 0)
        self.assertEqual(len(gateway.chat_requests), 1, '主人不做越界审计')
        self.assertEqual(self.svc.recent_log(OWNER), [])

    def test_group_message_never_touches_relationship(self):
        gateway = ScriptedGateway(['嗯～'], {'delta': 2, 'tag': 'care', 'advance_level': 1, 'mood': '',
                                             'event_done': False, 'note': ''})
        self.rt.gateway = self.svc.gateway = gateway
        session = SessionKey('bot', 'qq', 'onebot', 'group', '-', '8888')
        msg = IncomingMessage(session=session, message_id='1', sender_id=self.uid, sender_name='x',
                              text='歌伊你好', raw_text='歌伊你好', is_group=True, mentioned_bot=True,
                              event=FakeEvent(), proc=None, arrival_seq=1, sender_is_bot=False)
        self.rt.process_job(msg, 'passive')
        self.drain()
        self.assertEqual(gateway.judge_calls, 0)
        self.assertNotIn('【此刻你与对方的关系', gateway.chat_requests[0].messages[0]['content'])
        self.assertEqual(self.svc.recent_log(self.uid), [])

    def test_disabled_system_is_identical_to_old_behaviour(self):
        self.rt.config['galgame_enabled'] = False
        gateway, event = self.run_job('我爱你', replies=('人家也爱你～',),
                                      verdict={'delta': 2, 'tag': 'care', 'advance_level': 3, 'mood': '',
                                               'event_done': False, 'note': ''})
        self.assertNotIn('【此刻你与对方的关系', gateway.chat_requests[0].messages[0]['content'])
        self.assertEqual(gateway.judge_calls, 0)
        self.assertEqual(len(gateway.chat_requests), 1)
        self.assertIn('爱你', event.sent[0][2])

    def test_secret_lore_is_absent_from_stranger_prompt_in_real_flow(self):
        self.rt.worldbook.load()
        gateway, _ = self.run_job('你有哥哥吗？你的父母呢？小时候发生过什么？', replies=('这个嘛……',),
                                  verdict={'delta': 0, 'tag': 'neutral', 'advance_level': 0, 'mood': '',
                                           'event_done': False, 'note': ''})
        system = gateway.chat_requests[0].messages[0]['content']
        for word in ('逐影', '废墟', '发誓要复仇', '冒险家考核'):
            self.assertNotIn(word, system)
        self.svc.force_stage(self.uid, 3)
        gateway, _ = self.run_job('你有哥哥吗？你的父母呢？小时候发生过什么？', replies=('这个嘛……',),
                                  verdict={'delta': 0, 'tag': 'neutral', 'advance_level': 0, 'mood': '',
                                           'event_done': False, 'note': ''})
        system = gateway.chat_requests[0].messages[0]['content']
        self.assertIn('逐影', system)
        self.assertIn('废墟', system)

    def test_forget_me_resets_relationship(self):
        self.svc.force_stage(self.uid, 2)
        msg = private_msg('忘了我', self.uid, FakeEvent())
        self.rt.commands._cmd_forget_me(msg, force_confirm=True)
        self.assertEqual(self.svc.load(self.uid).stage, 0)


class PanelRelationshipTest(Base):
    def setUp(self):
        super().setUp()
        self.real_gateway = self.rt.gateway
        self.panel = Panel(self.rt)

    def tearDown(self):
        self.panel.close()
        super().tearDown()

    def call(self, action, request_id='r1', **params):
        self.rt.gateway = self.real_gateway     # 面板要读真实网关的渠道状态
        event = types.SimpleNamespace(
            data=types.SimpleNamespace(namespace='newGeYi', event=EVENT_NAME,
                                       payload=dict(params, action=action),
                                       webui={'request_id': request_id, 'session': 'b',
                                              'payload': dict(params, action=action)}),
            sent=[])
        event.send = lambda *a: event.sent.append(a) or True
        self.panel.handle_event(event, None)
        return event.sent[0][2]

    def seed(self):
        self.run_job('今天累不累呀，给你讲个笑话', replies=('好呀～',),
                     verdict={'delta': 2, 'tag': 'care', 'advance_level': 1, 'mood': '开心',
                              'event_done': False, 'note': ''})

    def test_list_and_get(self):
        self.seed()
        data = self.call('relationship_list')['data']
        self.assertTrue(data['enabled'])
        self.assertFalse(data['shadow'])
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['user_id'], self.uid)
        self.assertEqual(data['stage_counts']['陌生'], 1)
        self.assertEqual(data['stats_24h']['count'], 1)
        detail = self.call('relationship_get', user_id=self.uid)['data']
        self.assertEqual(detail['stage_name'], '陌生')
        self.assertEqual(detail['log'][0]['tag'], 'care')

    def test_writes_need_confirm_and_validate(self):
        self.seed()
        self.assertEqual(self.call('relationship_set_stage', user_id=self.uid, stage=3)['code'],
                         'confirmation_required')
        self.assertFalse(self.call('relationship_set_stage', request_id='r2', user_id=self.uid, stage=9,
                                   confirm=True)['ok'])
        self.assertFalse(self.call('relationship_get', request_id='r3', user_id='abc')['ok'])
        ok = self.call('relationship_set_stage', request_id='r4', user_id=self.uid, stage=3, confirm=True)
        self.assertTrue(ok['ok'])
        self.assertEqual(self.svc.load(self.uid).stage, 3)
        self.call('relationship_reset', request_id='r5', user_id=self.uid, confirm=True)
        self.assertEqual(self.svc.load(self.uid).stage, 0)
        # 重置不删流水（调参要用）
        self.assertTrue(self.svc.recent_log(self.uid))

    def test_session_get_includes_relationship_for_private_only(self):
        self.seed()
        key = SessionKey('bot', 'qq', 'onebot', 'private', '-', self.uid).as_text()
        self.assertEqual(self.call('session_get', session_key=key)['data']['relationship']['user_id'], self.uid)
        gkey = SessionKey('bot', 'qq', 'onebot', 'group', '-', '8888').as_text()
        self.assertIsNone(self.call('session_get', request_id='r2', session_key=gkey)['data']['relationship'])

    def test_overview_reports_galgame_flags(self):
        data = self.call('overview')['data']
        self.assertEqual(data['galgame'], {'enabled': True, 'shadow': False, 'worldbook': True})

    def test_config_whitelist_for_new_keys(self):
        cfg = self.call('config_get')['data']
        for key in ('galgame_enabled', 'galgame_shadow', 'galgame_thresholds', 'galgame_exempt_ids',
                    'galgame_judge_model', 'affection_provider'):
            self.assertIn(key, cfg['editable_keys'])
        current = dict(self.rt.config)
        validate_patch({'galgame_thresholds': [0, 20, 60, 120, 200, 290]}, current)
        validate_patch({'galgame_exempt_ids': ['9000000001']}, current)
        for bad in ({'galgame_thresholds': [0, 20, 60]},
                    {'galgame_thresholds': [0, 50, 10, 99, 100, 200]},
                    {'galgame_min_days': [0, 1, 3, 7, 14, -5]},
                    {'galgame_exempt_ids': ['abc']},
                    {'galgame_enabled': 'yes'}):
            with self.assertRaises(PanelError):
                validate_patch(bad, current)


class MildOverstepTest(Base):
    """轻微越界：只超前一点的**不扣分**，改成她下一轮一个「被打扰到」的小反应。

    数据背景（2026-11-07）：41 条扣分全部发生在 stage 0/1（允许 L1），
    其中 30 条是 L2——也就是「朋友间正常语气」（"你真可爱""给你比心"）。
    全按越界扣分等于每个新用户第一句话就踩雷，而且他完全不知道自己踩了。
    """

    def _judge(self, level):
        verdict = {'delta': 2, 'tag': 'care', 'advance_level': level,
                   'mood': '', 'event_done': False, 'note': ''}
        return lambda *a, **k: dict(verdict)

    def _run(self, text, level):
        msg = private_msg(text, self.uid, FakeEvent())
        out = self.svc.process_interaction(
            self.uid, 'Oracle', msg.session.as_text(), text, '嗯……？',
            judge=self._judge(level))
        return msg, out

    def test_gap_one_does_not_deduct(self):
        """陌生阶段说 L2（"你真可爱"）：不扣分、不加分，只记一笔。"""
        _msg, out = self._run('歌伊你真可爱呀', 2)
        self.assertEqual(out['delta_raw'], 0)
        row = self.svc.recent_log(self.uid, 1)[0]
        self.assertEqual(row['tag'], 'overstep')
        self.assertIn('mild_overstep', row['note'])
        # applied 里还含「今天第一次来」的 +1，所以只要求它不为负
        self.assertGreaterEqual(row['delta_applied'], 0.0, '轻微越界不该扣分')
        self.assertEqual(self.svc.load(self.uid).bad_streak, 0, '不该累积坏记录')

    def test_gap_two_still_penalised(self):
        """直接表白（L3，超前两档）：照旧扣分。"""
        _msg, out = self._run('我爱你，做我老婆吧', 3)
        self.assertLess(out['delta_raw'], 0)
        row = self.svc.recent_log(self.uid, 1)[0]
        self.assertEqual(row['tag'], 'flirt_early')
        self.assertLess(row['delta_raw'], 0)
        self.assertEqual(self.svc.load(self.uid).bad_streak, 1)

    def test_gap_two_loses_the_presence_bonus(self):
        """超前两档时，扣分把当天第一次来的 +1 抵消掉；轻微越界则保住那 +1。"""
        _msg, out = self._run('我爱你，做我老婆吧', 3)
        self.assertLessEqual(out['applied'], 0.5, '扣分应抵掉 presence 的 +1')

    def test_mild_gap_zero_restores_old_behaviour(self):
        self.rt.config['galgame_mild_gap'] = 0
        _msg, out = self._run('歌伊你真可爱呀', 2)
        self.assertLess(out['delta_raw'], 0, '宽容档设 0 就该回到「超前一点也扣」')

    def test_reaction_note_is_one_shot(self):
        """反应只出现一次：下一轮她有点被打扰到，再下一轮就正常了。"""
        msg, _out = self._run('歌伊你真可爱呀', 2)
        block = self.svc.block_for(msg)
        self.assertIn('刚才那句有点太亲近了', block)
        self.assertIn('阶段：陌生', block, '关系块本身不能少')
        # 第二次取就该没了
        self.assertNotIn('刚才那句有点太亲近了', self.svc.block_for(msg))

    def test_stale_reaction_is_dropped(self):
        """隔太久（>15 分钟）就不记仇了。"""
        msg, _out = self._run('歌伊你真可爱呀', 2)
        self.rt.store.set_setting(
            'rel_mild:' + msg.session.as_text(), '%.0f' % (time.time() - 3600))
        self.assertNotIn('刚才那句有点太亲近了', self.svc.block_for(msg))

    def test_normal_talk_has_no_reaction(self):
        """正常聊天不该冒出「太亲近了」。"""
        msg, out = self._run('今天好累啊', 1)
        self.assertGreaterEqual(out['delta_raw'], 0)
        self.assertNotIn('刚才那句有点太亲近了', self.svc.block_for(msg))


class ScoringFixTest(Base):
    """2026-11-10 抽查发现的三处不合理判定的回归测试。"""

    SESSION = None

    def setUp(self):
        super().setUp()
        self.SESSION = SessionKey('bot', 'qq', 'onebot', 'private', '-', self.uid).as_text()

    def _seed(self, affection=10.0, stage=1):
        rel = self.svc.load(self.uid)
        rel.affection = float(affection)
        rel.stage = int(stage)
        rel.gain_today = 0.0
        rel.last_day = ''
        self.svc.save(rel)

    def test_spam_no_longer_swallows_presence_bonus(self):
        """刷屏只该扣一次：以前连「今日首次互动 +1」也被跳过，等于罚两次。"""
        text = '投喂歌伊小蛋糕'
        for _ in range(3):
            self.rt.store.add_turn(self.SESSION, 'user', text)
        self._seed(affection=10.0)
        out = self.svc.process_interaction(self.uid, 'Oracle', self.SESSION, text, '诶？')
        self.assertEqual(out['tag'], 'spam')
        self.assertAlmostEqual(out['applied'], 0.0, 'presence +1 与 spam -1 应当抵消')
        self.assertAlmostEqual(self.svc.load(self.uid).affection, 10.0)

    def test_overstep_delta_is_capped_at_minus_one(self):
        """越界扣分封顶 -1：评审给 -2 也只扣 1（lie/rude 除外）。"""
        msg = private_msg('我爱你，做我老婆吧', self.uid, FakeEvent())
        verdict = {'delta': -2, 'tag': 'pushy', 'advance_level': 3,
                   'mood': '', 'event_done': False, 'note': ''}
        out = self.svc.process_interaction(
            self.uid, 'Oracle', msg.session.as_text(), msg.text, '……',
            judge=lambda *a, **k: dict(verdict))
        self.assertEqual(out['delta_raw'], -1, '越界不该一次扣 2 分')
        row = self.svc.recent_log(self.uid, 1)[0]
        self.assertEqual(row['tag'], 'flirt_early')

    def test_feeling_shows_today_progress(self):
        self._seed(affection=20.0, stage=1)
        rel = self.svc.load(self.uid)
        rel.gain_today = 12.0
        rel.last_day = time.strftime('%Y-%m-%d')
        self.svc.save(rel)
        text = self.svc.feeling(self.uid)
        self.assertIn('+12/12', text)
        self.assertIn('已经满啦', text)

    def test_feeling_ignores_yesterday_gain(self):
        """隔天还没说话时，gain_today 还是昨天的数——不能显示成"今天已满"。"""
        self._seed(affection=20.0, stage=1)
        rel = self.svc.load(self.uid)
        rel.gain_today = 12.0
        rel.last_day = '2026-01-01'
        self.svc.save(rel)
        text = self.svc.feeling(self.uid)
        self.assertNotIn('已经满啦', text)
        self.assertNotIn('+12/12', text)
        self.assertIn('今天还没聊', text)
        # 骰主的详情也不该把昨天算成今天
        self.assertIn('今日 +0.0', self.svc.describe(self.uid))

    def test_repeat_window_ignores_day_old_message(self):
        """三天前说过的话不该被判刷屏。"""
        text = '投喂歌伊小蛋糕'
        self.rt.store.add_turn(self.SESSION, 'user', text)
        old = int(time.time()) - 3 * 86400
        self.rt.store._conn().execute(
            'UPDATE turns SET created_at=? WHERE session_key=?', (old, self.SESSION))
        self.rt.store.add_turn(self.SESSION, 'user', '今天天气不错')
        self.rt.store.add_turn(self.SESSION, 'assistant', '是呀～')
        out = self.svc.process_interaction(self.uid, 'Oracle', self.SESSION, text, '诶？',
                                           judge=lambda *a, **k: {'delta': 1, 'tag': 'care',
                                                                  'advance_level': 1, 'mood': '',
                                                                  'event_done': False, 'note': ''})
        self.assertNotEqual(out['tag'], 'spam', '跨天的重复不该算刷屏')


if __name__ == '__main__':
    unittest.main()
