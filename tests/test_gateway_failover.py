# -*- encoding: utf-8 -*-
"""渠道故障转移的回归测试（离线、无网络、不碰线上状态）。

锁住 2026-11-01 骰主定的口径（**立刻锁定**这条路径，`retry_before_hold_seconds=0`）：

- 候选渠道一轮走到底（最后一个也挂了）→ **回到第一个再来一轮**（`failover.full_cycles`）；
- 整轮都挂 → 通知骰主 + **切到兜底渠道并锁定**（`gw.hold`），不再尝试其它渠道；
- 锁定期间：`_ordered_pairs` 只返回兜底那一个、`probe_once` 不探测、`_switch_locked` 不切换；
- 解除只有一条路：`切换渠道 <名称>` / `切换渠道 自动`。

2026-11-12 起线上默认改成**软降级**（先等 300 秒从锚点重试，仍失败才锁定），
那条新路径由 `SoftHoldTest` 覆盖；本文件其余用例显式写 `retry_before_hold_seconds=0`
来继续锁住老语义，避免两套行为互相掩盖。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

_SCRATCH = tempfile.mkdtemp(prefix='geyi_gateway_tests_')
os.environ['GEYI_DATA_DIR'] = _SCRATCH

from newGeYi.models import ModelRequest  # noqa: E402
from newGeYi.providers.llm_gateway import LLMGateway  # noqa: E402

PROVIDERS = [
    {'name': 'p1', 'endpoint': 'http://x1/v1/chat/completions', 'api_key': 'k1',
     'model': 'm1', 'priority': 1},
    {'name': 'p2', 'endpoint': 'http://x2/v1/chat/completions', 'api_key': 'k2',
     'model': 'm2', 'priority': 2},
    {'name': 'p3', 'endpoint': 'http://x3/v1/chat/completions', 'api_key': 'k3',
     'model': 'm3', 'priority': 3},
    {'name': 'p4', 'endpoint': 'http://x4/v1/chat/completions', 'api_key': 'k4',
     'model': 'm4', 'priority': 4},
    {'name': 'p5', 'endpoint': 'http://x5/v1/chat/completions', 'api_key': 'k5',
     'model': 'm5', 'priority': 5},
    {'name': 'official-chat', 'endpoint': 'http://x6/v1/chat/completions', 'api_key': 'k6',
     'model': 'deepseek-flash', 'priority': 6, 'manual_only': True},
]


class FakeStore:
    def __init__(self):
        self.settings = {}
        self.api_calls = []

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        self.settings[key] = value

    def add_api_call(self, **kwargs):
        self.api_calls.append(kwargs)


class FakeResp:
    def __init__(self, status):
        self.status_code = status
        self.text = 'upstream boom'

    def json(self):
        return {}


class FakeSession:
    def __init__(self, status):
        self.status = status
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        body = json or {}
        self.posts.append((body.get('model'), url))
        return FakeResp(self.status)


def build(status=500, **failover):
    store = FakeStore()
    cfg = {
        'providers': [dict(p) for p in PROVIDERS],
        'max_retries': 0,          # 测试里不要 sleep 重试
        'global_concurrency': 4,
        'failover': {
            'fail_threshold': 1,
            'full_cycles': 2,
            'last_resort_provider': 'official-chat',
            'notify_owner': True,
            'notify_cooldown_seconds': 0,
            'probe_success_needed': 1,
            'probe_timeout_seconds': 1,
            'probe_repair_per_round': 0,
            # 老语义：全挂立刻锁定。软降级由 SoftHoldTest 单独测。
            'retry_before_hold_seconds': 0,
        },
    }
    cfg['failover'].update(failover)
    gw = LLMGateway(store, cfg)
    session = FakeSession(status)
    gw._get_session = lambda: session          # type: ignore[assignment]
    notes = []
    gw.set_notify(lambda text: notes.append(text))
    return gw, store, session, notes


def ask(gw):
    return gw.chat(ModelRequest(messages=[{'role': 'user', 'content': 'hi'}], model='x'),
                   kind='chat', session_key='t')


class SessionProxyTest(unittest.TestCase):
    def test_gateway_session_does_not_inherit_environment_proxy(self):
        gw, store, fake_session, notes = build()
        gw._local.session = None
        session = LLMGateway._get_session(gw)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.headers['User-Agent'], 'newGeYi/2.0')


class RequestErrorTest(unittest.TestCase):
    """2026-11-03 审查：一个**坏请求**不能把渠道判死、更不能触发「全挂」锁定。

    以前 400/413/422（请求体坏了、图太大、参数不合法）和「空回复」都会走
    `_mark_failure` → 两轮之后切兜底 + 锁定 + 给骰主发「渠道全挂」。
    """

    def test_client_error_aborts_without_marking_providers(self):
        gw, store, session, notes = build(status=400)
        res = ask(gw)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, 'http_400')
        # 只试了当前渠道一次：换渠道问同一件事还是同样的错
        self.assertEqual([m for m, _ in session.posts], ['m1'])
        self.assertEqual(gw._hold_name, '', '一个坏请求就把网关锁到兜底渠道了')
        self.assertEqual(notes, [], '一个坏请求就给骰主发了「全挂」通知')

    def test_client_error_does_not_touch_provider_health(self):
        gw, store, session, notes = build(status=422)
        ask(gw)
        state = next(st for p, st in gw._pairs if p.name == 'p1')
        self.assertTrue(state.healthy)
        self.assertEqual(state.fail_count, 0)

    def test_empty_content_does_not_lock_the_gateway(self):
        # 200 但没有任何输出：模型/预算问题，不是渠道故障
        gw, store, session, notes = build(status=200)
        gw._parse_response = lambda provider, data: (False, '', 0, 0, 0, '思考链吃光预算')
        res = ask(gw)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, 'empty_content')
        self.assertEqual(gw._hold_name, '', '空回复把网关锁到兜底渠道了')
        self.assertEqual(notes, [])


class CycleAndEscalateTest(unittest.TestCase):
    def test_two_cycles_then_lock_official(self):
        gw, store, session, notes = build()
        res = ask(gw)
        self.assertFalse(res.ok)
        models = [m for m, _ in session.posts]
        # 5 个候选 × 2 轮，且兜底渠道（manual_only）不参与轮转
        self.assertEqual(models, ['m1', 'm2', 'm3', 'm4', 'm5'] * 2)
        # 整轮挂完之后：切到兜底 + 锁定 + 通知一次
        self.assertEqual(gw._current_name, 'official-chat')
        self.assertEqual(gw._hold_name, 'official-chat')
        self.assertEqual(store.settings.get('gw.hold'), 'official-chat')
        self.assertEqual(len(notes), 1)
        self.assertIn('所有模型渠道都连不上', notes[0])
        self.assertIn('official-chat', notes[0])

    def test_locked_gateway_only_uses_official(self):
        gw, store, session, notes = build()
        ask(gw)
        session.posts.clear()
        ask(gw)
        self.assertEqual([m for m, _ in session.posts], ['deepseek-flash'])
        # 锁定期间不再重复发「五连挂」通知，走冷却通知（cooldown=0 所以**这一次**会多一条）
        self.assertEqual(len(notes), 2, notes)

    def test_hold_is_persisted_and_restored(self):
        gw, store, session, notes = build()
        ask(gw)
        # 用同一个 store 重建网关：锁定状态要能恢复
        gw2 = LLMGateway(store, gw.config)
        self.assertEqual(gw2._hold_name, 'official-chat')
        pairs = gw2._ordered_pairs('chat')
        self.assertEqual([p.name for p, _ in pairs], ['official-chat'])

    def test_probe_and_auto_switch_disabled_while_locked(self):
        gw, store, session, notes = build()
        ask(gw)
        self.assertEqual(gw.probe_once(), [])
        self.assertFalse(gw._switch_locked('auto', 'test'))

    def test_manual_switch_unlocks(self):
        gw, store, session, notes = build()
        ask(gw)
        msg = gw.switch_to('p2')
        self.assertIn('解除', msg)
        self.assertEqual(gw._hold_name, '')
        pairs = gw._ordered_pairs('chat')
        self.assertEqual(pairs[0][0].name, 'p2')
        # 刚经历一轮全挂：别的渠道都在熔断冷却里，按设计先不出现在队列
        self.assertEqual([p.name for p, _ in pairs], ['p2'])
        # 冷却到期（这里直接清零模拟时间流逝）后，手动锚点的老语义不变：
        # 备份只在锚点及其以下找，p1 被排除；兜底渠道（manual_only）仍不进自动轮转
        with gw._lock:
            for _, st in gw._pairs:
                st.cooldown_until = 0.0
        pairs = gw._ordered_pairs('chat')
        self.assertEqual([p.name for p, _ in pairs], ['p2', 'p3', 'p4', 'p5'])

    def test_switch_auto_also_unlocks(self):
        gw, store, session, notes = build()
        ask(gw)
        gw.switch_to('自动')
        self.assertEqual(gw._hold_name, '')

    def test_no_last_resort_configured_just_notifies(self):
        gw, store, session, notes = build(last_resort_provider='')
        ask(gw)
        self.assertEqual(gw._hold_name, '')
        self.assertTrue(notes)

    def test_success_in_second_cycle_is_logged_and_returns(self):
        """第一轮全挂、第二轮第一个就通了：应当直接返回，不再往下试。"""
        gw, store, session, notes = build()
        state = {'n': 0}

        def post(url, headers=None, json=None, timeout=None):
            state['n'] += 1
            session.posts.append(((json or {}).get('model'), url))
            # 前 5 次（第一轮）返回 500，第 6 次返回 200 + 正常内容
            if state['n'] <= 5:
                return FakeResp(500)
            resp = FakeResp(200)
            resp.json = lambda: {
                'choices': [{'message': {'content': '收到'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
            }
            return resp

        session.post = post  # type: ignore[assignment]
        res = ask(gw)
        self.assertTrue(res.ok, res.error_code)
        self.assertEqual(res.text, '收到')
        self.assertEqual(gw._hold_name, '')      # 没降级
        self.assertFalse(notes)


class BackgroundKindTest(unittest.TestCase):
    """后台提炼（memory_curate）的超时不该把渠道判死（2026-11-07）。

    数据：提炼 17409 次调用失败 28%（其中 502/504/连接/超时占大头），
    聊天只有 1~3%。以前提炼一次超时（60s×2 重试=120s）就把整条渠道判死 5 分钟，
    聊天明明好的却被一起判了——「显示全部掉线」的主要来源。
    """

    @staticmethod
    def _timeout_session(gw):
        import requests

        class TimeoutSession:
            def __init__(self):
                self.posts = []

            def post(self, url, headers=None, json=None, timeout=None):
                self.posts.append(((json or {}).get('model'), url))
                raise requests.Timeout('boom')

        session = TimeoutSession()
        gw._get_session = lambda: session          # type: ignore[assignment]
        return session

    def test_curate_timeout_keeps_provider_healthy(self):
        gw, store, _session, _notes = build()
        self._timeout_session(gw)
        gw.chat(ModelRequest(messages=[{'role': 'user', 'content': 'hi'}], model='x'),
                kind='memory_curate', session_key='t')
        states = {p.name: st for p, st in gw._pairs}
        for name in ('p1', 'p2'):
            self.assertTrue(states[name].healthy, f'{name} 被提炼超时judged死了')
        self.assertEqual(states['p1'].fail_count, 0, '不该累积 fail_count')

    def test_chat_timeout_still_marks_provider(self):
        """聊天超时仍然算渠道故障（只有后台任务被豁免）。"""
        gw, store, _session, _notes = build(fail_threshold=1)
        self._timeout_session(gw)
        gw.chat(ModelRequest(messages=[{'role': 'user', 'content': 'hi'}], model='x'),
                kind='chat', session_key='t')
        states = {p.name: st for p, st in gw._pairs}
        self.assertFalse(states['p1'].healthy, '聊天超时应该判死')


class NotifyCooldownTest(unittest.TestCase):
    """「全挂」通知必须走冷却——以前切兜底那条路径是裸发的，挂一次发一条。"""

    def test_repeated_all_down_notifies_once(self):
        gw, store, session, notes = build(notify_cooldown_seconds=600)
        ask(gw)
        self.assertEqual(len(notes), 1)
        ask(gw)                       # 还在冷却里
        self.assertEqual(len(notes), 1, '冷却期内不该再发一条')
        # 冷却过后可以再发
        gw._last_down_notify = 0.0
        ask(gw)
        self.assertEqual(len(notes), 2)


class SoftHoldTest(unittest.TestCase):
    """2026-11-12：全挂先软降级 —— 等 300 秒从锚点重试，仍失败才通知 + 锁定。

    起因：骰主的渠道大多是中转站/反代，夜里抖十几秒很常见；以前一次「两轮全挂」
    就立刻通知 + 锁定，等于每次抖动都把机器人钉在贵的官方渠道上，得人工切回。
    """

    def _build(self, **extra):
        return build(retry_before_hold_seconds=300, **extra)

    def test_first_total_failure_does_not_lock_or_notify(self):
        gw, store, session, notes = self._build()
        res = ask(gw)
        self.assertFalse(res.ok)
        self.assertEqual(gw._hold_name, '', '第一次全挂就锁定了')
        self.assertEqual(store.settings.get('gw.hold'), '')
        self.assertEqual(notes, [], '第一次全挂就通知骰主了')
        self.assertGreater(gw.pending_retry_at(), time.time() + 200)

    def test_soft_switch_keeps_other_channels_candidates(self):
        gw, store, session, notes = self._build()
        ask(gw)
        self.assertEqual(gw._current_name, 'official-chat')
        self.assertEqual(gw._hold_name, '', '软降级不该置锁定')
        self.assertGreater(gw.pending_retry_at(), 0, '软降级应留下待重试闹钟')
        names = [p.name for p, _ in gw._ordered_pairs('chat')]
        self.assertEqual(names[0], 'official-chat', '兜底应该排在最前面')
        # 刚全挂过 → 其余渠道在熔断冷却里；冷却到期后必须回到候选
        # （这才是「没锁定」的实际含义：锁定时冷却到期也不会出现）
        with gw._lock:
            for _, st in gw._pairs:
                st.cooldown_until = 0.0
        names = [p.name for p, _ in gw._ordered_pairs('chat')]
        self.assertIn('p1', names, '没锁定，冷却到期后别的渠道要回到候选里')

    def test_retry_success_recovers_silently(self):
        gw, store, session, notes = self._build()
        ask(gw)
        session.status = 200
        self.assertEqual(gw.retry_pending_once(), 'recovered')
        self.assertEqual(gw._hold_name, '')
        self.assertEqual(notes, [])
        self.assertEqual(gw._current_name, 'p1')
        self.assertEqual(gw.pending_retry_at(), 0.0)

    def test_retry_failure_locks_and_notifies_once(self):
        gw, store, session, notes = self._build()
        ask(gw)
        self.assertEqual(gw.retry_pending_once(), 'held')
        self.assertEqual(gw._hold_name, 'official-chat')
        self.assertEqual(len(notes), 1)
        self.assertIn('所有模型渠道都连不上', notes[0])

    def _ok_session(self, session):
        """把假 session 换成「真的会回内容」的，好让某次调用成功。"""
        def post(url, headers=None, json=None, timeout=None):
            session.posts.append(((json or {}).get('model'), url))
            resp = FakeResp(200)
            resp.json = lambda: {
                'choices': [{'message': {'content': '收到'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
            }
            return resp

        session.post = post  # type: ignore[assignment]

    def test_success_during_cooldown_cancels_pending(self):
        gw, store, session, notes = self._build()
        ask(gw)
        with gw._lock:                      # 待重试期间已经切回普通渠道
            gw._current_name = 'p1'
            for _, st in gw._pairs:
                st.cooldown_until = 0.0
        self._ok_session(session)
        self.assertTrue(ask(gw).ok)
        self.assertEqual(gw.pending_retry_at(), 0.0, '链路自己好了还留着待锁定')
        self.assertEqual(gw._hold_name, '')
        self.assertEqual(notes, [])

    def test_last_resort_success_keeps_pending_retry(self):
        """兜底渠道自己通了**不算**链路恢复（2026-11-12）。

        待重试期间机器人被软切到兜底渠道上，它成功是理所当然的；此时若撤掉闹钟，
        `probe_once` 又因为「当前是 manual_only 且非锚点」完全不动手，就会安静地
        一直挂在官方 DeepSeek 上花钱。闹钟留着：到点探测一轮，真恢复才切回去。
        """
        gw, store, session, notes = self._build()
        ask(gw)
        self.assertEqual(gw._current_name, 'official-chat')
        self._ok_session(session)
        self.assertTrue(ask(gw).ok)
        self.assertGreater(gw.pending_retry_at(), 0.0, '兜底渠道一通就撤闹钟，会一直挂在官方渠道上')
        self.assertEqual(gw._hold_name, '', '兜底渠道成功不该顺手锁定')

    def test_repeated_failures_do_not_reset_the_timer(self):
        """冷却期里又挂一次不能把倒计时推后，否则晚上一直有人聊就永远不重试。"""
        gw, store, session, notes = self._build()
        ask(gw)
        first = gw.pending_retry_at()
        ask(gw)
        self.assertAlmostEqual(gw.pending_retry_at(), first, places=3)

    def test_manual_switch_cancels_pending(self):
        gw, store, session, notes = self._build()
        ask(gw)
        gw.switch_to('p2')
        self.assertEqual(gw.pending_retry_at(), 0.0)

    def test_status_text_shows_cooldown(self):
        gw, store, session, notes = self._build()
        ask(gw)
        self.assertIn('全挂冷却中', gw.status_text())


class ToleranceTest(unittest.TestCase):
    """链路抖动容忍：探测把 429 当活着、连接抖动重试一次、刚成功过的超时不计故障。"""

    def test_probe_treats_429_as_alive(self):
        gw, store, session, notes = build(status=429)
        provider = gw._pairs[0][0]
        self.assertTrue(gw._probe_provider(provider), '被限流说明渠道是通的，不该判死')

    def test_probe_retries_connection_error_once(self):
        import requests

        gw, store, session, notes = build()
        calls = {'n': 0}

        def post(url, headers=None, json=None, timeout=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise requests.ConnectionError('reset by peer')
            return FakeResp(200)

        session.post = post  # type: ignore[assignment]
        self.assertTrue(gw._probe_provider(gw._pairs[0][0]))
        self.assertEqual(calls['n'], 2)

    def test_failure_grace_ignores_timeout_right_after_success(self):
        import requests

        gw, store, session, notes = build(failure_grace_seconds=90)

        class TimeoutSession:
            def post(self, url, headers=None, json=None, timeout=None):
                raise requests.Timeout('boom')

        gw._get_session = lambda: TimeoutSession()   # type: ignore[assignment]
        for _p, state in gw._pairs:
            state.last_ok_at = time.time()
        ask(gw)
        states = {p.name: st for p, st in gw._pairs}
        for name in ('p1', 'p2', 'p3'):
            self.assertTrue(states[name].healthy, f'{name} 因为一次抖动被判死了')
            self.assertEqual(states[name].fail_count, 0)


class ManualPriorityTest(unittest.TestCase):
    """手动切换优先于已启动的后台探测，避免过期结果覆盖操作者选择。

    根因三条，都在这里钉住：
    1. 重试已经开跑后骰主又手动切了渠道 → 这次重试必须作废（不锁定、不通知）；
    2. 锚点若标了 `manual_only`（官方渠道就是），重试列表以前会把它过滤掉 → 从锚点重试却不试锚点；
    3. 真锁定时以前会清掉手动锚点 → 解锁后发现「我设的锚点没了」。
    """

    def _build(self):
        return build(retry_before_hold_seconds=300)

    def test_retry_aborts_when_manual_switch_happens_midway(self):
        gw, store, session, notes = self._build()
        ask(gw)                                  # 进入待重试
        calls = {'n': 0}

        def probe(provider):
            calls['n'] += 1
            if calls['n'] == 1:
                gw.switch_to('p3')               # 骰主在这轮探测期间手动切换
            return False

        gw._probe_provider = probe                # type: ignore[assignment]
        self.assertEqual(gw.retry_pending_once(), 'cancelled')
        self.assertEqual(gw._hold_name, '', '手动切换之后不该再锁定')
        self.assertEqual(notes, [], '手动切换之后不该再发全挂通知')
        self.assertEqual(gw._current_name, 'p3')

    def test_retry_targets_include_manual_only_anchor(self):
        gw, store, session, notes = self._build()
        gw.switch_to('official-chat')             # 官方渠道是 manual_only
        names = [p.name for p, _ in gw._anchor_retry_targets()]
        self.assertEqual(names[0], 'official-chat', '「从锚点重试」却没把锚点排进去')

    def test_escalation_keeps_manual_anchor(self):
        gw, store, session, notes = self._build()
        gw.switch_to('p1')
        ask(gw)
        self.assertEqual(gw.retry_pending_once(), 'held')
        self.assertEqual(gw._hold_name, 'official-chat')
        self.assertEqual(gw._anchor_name, 'p1', '锁定不该抹掉骰主设的锚点')


class CoolingTest(unittest.TestCase):
    """2026-11-12 骰主：「多次失败就移出队列等一段时间，通了再回来」。

    以前判死的渠道只是**排到队尾**（`rank` 里 `1 if not state.healthy else 0`），
    于是每条消息都还要在每条死渠道上各等一次 60 秒超时——现场四个死渠道就能把
    一条回复拖到 150 秒总预算上限，用户体感就是「歌伊一直在等」。
    现在改成熔断冷却：判死即整条移出候选，到期后由后台探测召回。
    """

    @staticmethod
    def _state(gw, name):
        return next(st for p, st in gw._pairs if p.name == name)

    def _cool(self, gw, name, seconds=600):
        st = self._state(gw, name)
        with gw._lock:
            st.healthy = False
            st.fail_count = 2
            st.last_error = 'timeout'
            st.cooldown_until = time.time() + seconds
        return st

    def test_cooling_provider_leaves_the_queue(self):
        gw, store, session, notes = build()
        self._cool(gw, 'p2')
        names = [p.name for p, _ in gw._ordered_pairs()]
        self.assertNotIn('p2', names, '冷却中的渠道还留在候选队列里，每条消息都要白等 60 秒')
        self.assertIn('p1', names)

    def test_expired_cooling_returns_to_queue(self):
        gw, store, session, notes = build()
        self._cool(gw, 'p2', seconds=-1)
        names = [p.name for p, _ in gw._ordered_pairs()]
        self.assertIn('p2', names, '冷却到期了却没回到队列')

    def test_all_cooling_still_returns_one_candidate(self):
        gw, store, session, notes = build()
        now = time.time()
        with gw._lock:
            for index, (provider, st) in enumerate(gw._pairs):
                st.healthy = False
                st.fail_count = 2
                st.cooldown_until = now + 600 + index * 10
        pairs = gw._ordered_pairs()
        self.assertEqual(len(pairs), 1, '全在冷却时应只硬试最早到期的一条，而不是一条都不发')
        self.assertEqual(pairs[0][0].name, 'p1')

    def test_success_clears_cooldown(self):
        gw, store, session, notes = build()
        st = self._cool(gw, 'p1')
        gw._mark_success(next(p for p, _ in gw._pairs if p.name == 'p1'), st)
        self.assertTrue(st.healthy)
        self.assertEqual(st.cooldown_until, 0.0)
        self.assertIn('p1', [p.name for p, _ in gw._ordered_pairs()])

    def test_manual_switch_clears_cooldown(self):
        gw, store, session, notes = build()
        self._cool(gw, 'p3')
        gw.switch_to('p3')
        self.assertEqual(self._state(gw, 'p3').cooldown_until, 0.0)
        self.assertEqual(gw._ordered_pairs()[0][0].name, 'p3', '骰主亲手点的渠道却没排到第一')

    def test_failure_cools_then_escalates_on_retry(self):
        gw, store, session, notes = build(
            fail_threshold=1, cooldown_base_seconds=60, cooldown_max_seconds=600)
        provider = next(p for p, _ in gw._pairs if p.name == 'p1')
        st = self._state(gw, 'p1')
        gw._mark_failure(provider, st, 'timeout')
        self.assertFalse(st.healthy)
        self.assertGreater(st.cooldown_until - time.time(), 50, '首次判死应关 60 秒')
        # 冷却到期后被召回，又失败：重新关起来并翻倍
        with gw._lock:
            st.cooldown_until = time.time() - 1
        gw._mark_failure(provider, st, 'timeout')
        left = st.cooldown_until - time.time()
        self.assertGreater(left, 110, '第二次失败应翻倍到 120 秒')
        self.assertLessEqual(left, 121)


    def test_zero_base_disables_circuit_breaker(self):
        """回滚口径：cooldown_base_seconds = 0 → 退回「排到队尾」的老行为。"""
        gw, store, session, notes = build(fail_threshold=1, cooldown_base_seconds=0)
        provider = next(p for p, _ in gw._pairs if p.name == 'p1')
        st = self._state(gw, 'p1')
        gw._mark_failure(provider, st, 'timeout')
        self.assertFalse(st.healthy)
        self.assertEqual(st.cooldown_until, 0.0, '关掉熔断后不该设冷却')
        self.assertIn('p1', [p.name for p, _ in gw._ordered_pairs()])


if __name__ == '__main__':
    unittest.main(verbosity=2)
