# -*- encoding: utf-8 -*-
import os
import shutil
import tempfile
import time
import unittest

# ⚠️ 必须在 import newGeYi 之前：包初始化会执行 main.py 里的 Runtime()，
# 它会去打开**真实**的状态目录（配置 + 数据库）。以前这个文件没有隔离，
# 单独跑一次就会往线上 state 目录里摸一把（2026-11-03 审查提到）。
os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_mem_tests_'))

from newGeYi.models import IncomingMessage, SessionKey
from newGeYi.storage.database import SQLiteStore
from newGeYi.memory.repository import (
    MemoryRepository,
    relative_time,
    format_entry,
    relevance_score,
)
from newGeYi.memory.curator import MemoryCurator


class MemorySystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, 'test_memory.sqlite3')
        self.store = SQLiteStore(self.db_path)
        self.config = {
            'memory_inject_facts': 12,
            'memory_inject_self': 6,
            'memory_inject_summaries': 3,
            'memory_max_chars': 3200,
            'memory_relevance_enabled': True,
            'memory_relevance_weight': 0.25,
            'memory_temporal_anchor': True,
            'memory_cross_group_enabled': True,
            'memory_impression_enabled': True,
        }
        self.repo = MemoryRepository(self.store, self.config)

    def tearDown(self):
        try:
            self.store.close()          # 不关会刷一串 ResourceWarning
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_msg(self, text='你好', sender_id='10001', session_id='99999', is_group=True):
        sess = SessionKey('bot', 'qq', 'onebot', 'group' if is_group else 'private', host_id=session_id)
        return IncomingMessage(
            session=sess,
            message_id='m1',
            sender_id=sender_id,
            sender_name='测试群友',
            text=text,
            raw_text=text,
            is_group=is_group,
            mentioned_bot=False,
            event=None,
            proc=None,
            arrival_seq=1,
            sender_is_bot=False,
        )

    # 1. 相对时间与时间锚点
    def test_relative_time_formatting(self):
        now = 1700000000.0
        self.assertEqual(relative_time(now - 60, now), '刚刚')
        self.assertEqual(relative_time(now - 600, now), '10分钟前')
        self.assertEqual(relative_time(now - 7200, now), '2小时前')
        self.assertEqual(relative_time(now - 86400, now), '昨天')
        self.assertEqual(relative_time(now - 86400 * 2, now), '前天')
        self.assertEqual(relative_time(now - 86400 * 4, now), '4天前')
        self.assertEqual(relative_time(now - 86400 * 15, now), '2周前')
        self.assertEqual(relative_time(now - 86400 * 60, now), '2个月前')

    def test_format_entry(self):
        now = 1700000000.0
        entry = format_entry('喜欢喝乌龙茶', now - 86400 * 3, now, temporal=True)
        self.assertEqual(entry, '[3天前] 喜欢喝乌龙茶')

        entry_no_time = format_entry('喜欢喝乌龙茶', now - 86400 * 3, now, temporal=False)
        self.assertEqual(entry_no_time, '喜欢喝乌龙茶')

    # 2. 话题相关度打分
    def test_relevance_scoring(self):
        # 完全命中或子串命中
        self.assertEqual(relevance_score('猫咪', '养了一只可爱的猫咪'), 1.0)

        # 核心字重叠
        score_cat = relevance_score('我家猫今天不舒服', '家里养了一只小猫叫咪咪')
        self.assertGreater(score_cat, 0.2)

        # 不相关
        score_irrelevant = relevance_score('明天吃什么', '家里养了一只小猫叫咪咪')
        self.assertEqual(score_irrelevant, 0.0)

    # 3. 话题相关度对检索排序的加权
    def test_relevance_boosts_ranking(self):
        msg = self._make_msg(text='我想买点猫罐头')
        now = time.time()
        # 插入两条事实：一条平时更常用（命中多、更新），但跟猫无关；另一条跟猫直接相关但很少用
        self.store.add_user_fact(msg.session.as_text(), '10001', '是一名资深程序员', importance=0.8)
        self.store.add_user_fact(msg.session.as_text(), '10001', '家里养了两只布偶猫', importance=0.5)

        recalled = self.repo.recall(msg)
        facts = recalled['facts']
        self.assertEqual(len(facts), 2)
        # 因为 msg.text 包含“猫”，布偶猫的事实应该被相关度加成排到第一位！
        self.assertIn('布偶猫', facts[0]['fact'])

    # 4. 跨群全局记忆 (Cross-Group Memory)
    def test_cross_group_global_facts(self):
        # 在 global session 写入用户的固定特征（如生日/真实姓名）
        self.store.add_user_fact('global', '10001', '生日是10月24日', importance=0.9)
        # 在群 99999 写入群内梗
        msg_99999 = self._make_msg(text='hello', sender_id='10001', session_id='99999')
        self.store.add_user_fact(msg_99999.session.as_text(), '10001', '在群里担任跑团主持人', importance=0.7)

        # 用户在另一个群 88888 说话
        msg_in_other_group = self._make_msg(text='大家早', sender_id='10001', session_id='88888')
        recalled = self.repo.recall(msg_in_other_group)
        facts_text = [f['fact'] for f in recalled['facts']]

        # 应该能召回 global 里的生日，但不会把 99999 的专属群梗拉过来
        self.assertIn('生日是10月24日', facts_text)
        self.assertNotIn('在群里担任跑团主持人', facts_text)

    # 5. 高维群友印象卡片 (User Impression)
    def test_user_impression_card(self):
        self.store.set_user_impression('10001', '喜欢开玩笑的熟人群友，对单机游戏很有研究')
        msg = self._make_msg(text='歌伊下午好', sender_id='10001')
        block = self.repo.build_block(msg)
        self.assertIn('【群友印象卡片', block)
        self.assertIn('喜欢开玩笑的熟人群友', block)

    # 6. 大容量记忆注入（打破 900 字符限制）
    def test_large_memory_capacity_not_truncated(self):
        msg = self._make_msg(text='你好', sender_id='10001')
        self.repo.config['memory_inject_facts'] = 15
        topics = [
            '喜欢在周末带单反相机去郊外森林公园拍摄野生鸟类和微距昆虫照片，有一套极其昂贵且专业的长焦变焦镜头和专业碳纤维三脚架云台设备',
            '平时热衷于研究现代天体物理学与深空星云观测，经常在晴朗少云的冬夜用两百毫米口径的大反射式望远镜记录木星大红斑与土星光环结构',
            '非常擅长烘焙纯正的法式甜点和意式提拉米苏蛋糕，每次制作完成之后都会拍照分给群里的朋友们一起品尝分享并且认真收集口味评价与改进建议',
            '业余时间喜欢学习古典钢琴演奏肖邦夜曲与巴赫十二平均律，目前正在利用每个周末的闲暇时光认真准备业余最高级别钢琴水平认证考试',
            '在大学期间创办过开源技术俱乐部，独立开发过多款跨平台的移动端实用效率小工具并在GitHub开源获得了许多国际开发者的星标认可',
            '夏天喜欢去海边深潜考取了PADI进阶潜水员认证证书，记录过很多五彩斑斓的珊瑚礁群落与珍稀热带海洋生物的高清海底摄影视频短片',
            '特别喜欢在秋天组织朋友们一起去高山原始森林露营徒步，精通户外野外生火烹饪与轻量化帐篷搭建等一系列极具实用性的荒野生存野外技能',
            '日常有长期跑步与骑行公路自行车的运动习惯，参加过多场城市半程马拉松长跑比赛并且都取得了在同年龄组中非常优异瞩目的完赛名次',
            '对古典手冲精品咖啡非常有研究与讲究，平时只选浅度烘焙的埃塞俄比亚耶加雪菲日晒咖啡豆，并坚持使用精准电子秤自己研磨控温慢速萃取',
            '精通日文与西班牙文双语本地化翻译，经常在网络游戏社区协助海外独立游戏开发者汉化冷门小众但剧情极佳的叙事角色扮演独立精品作品',
            '收藏了许多复古机械键盘与客制化键帽轴体，自己动手焊接电路板、排线和润轴，组装制作过独一无二具有清脆打字音的铝坨坨客制化机械键盘',
            '对古典文学和神话史诗有着深厚的兴趣，阅读过大量关于北欧神话、希腊史诗与凯尔特传说的学术专著，经常在社群中撰写详尽的故事考据长文',
            '熟练掌握水彩画与油画棒技法，擅长绘制各种宁静清新的乡村风景水彩写生，在社交平台上积累了许多喜欢其治愈画风的艺术爱好者粉丝关注',
            '热爱收集与拼装各类复杂的乐高机械组模型，曾经花费数周时间独自拼装完成了数千块积木零件的大型航天飞机与方程式赛车精密机械传动模型',
        ]
        for t in topics:
            self.store.add_user_fact(msg.session.as_text(), '10001', t, importance=0.8)

        block = self.repo.build_block(msg)
        # 确保整体超过 900 字符且没有被截断
        self.assertGreater(len(block), 900)
        self.assertFalse(block.endswith('…'))

    # 7. Curator 淘汰过期旧事实与更新印象
    def test_curator_expires_old_facts(self):
        # 插入一条旧事实
        fid = self.store.add_user_fact('group:99999', '10001', '正在全职备考研究生', importance=0.8)
        self.assertEqual(len(self.store.list_user_facts('group:99999', '10001')), 1)

        curator = MemoryCurator(self.config, self.store, None)
        # 模拟模型输出：指明旧事实已失效（expired_fact_ids），并记入新事实与新印象
        mock_data = {
            'group_summary': '群友闲聊研究生生活',
            'user_facts': [
                {'user_id': '10001', 'fact': '考研成功上岸，正在读研一', 'category': '经历', 'importance': 0.8, 'scope': 'global'}
            ],
            'expired_fact_ids': [fid],
            'user_impressions': [
                {'user_id': '10001', 'summary': '积极向上的研究生群友'}
            ],
            'self_memories': []
        }

        stats = curator._store_results('group:99999', mock_data, {'10001'}, last_id=100)
        self.assertEqual(stats['expired'], 1)
        self.assertEqual(stats['facts'], 1)
        self.assertEqual(stats['impressions'], 1)

        # 检查旧事实是否被失效 (valid_to > 0)
        active_facts = self.store.list_user_facts('group:99999', '10001', include_expired=False, include_global=True)
        active_texts = [f['fact'] for f in active_facts]
        self.assertNotIn('正在全职备考研究生', active_texts)
        self.assertIn('考研成功上岸，正在读研一', active_texts)

        # 检查群友印象是否更新成功
        imp = self.store.get_user_impression('10001')
        self.assertIsNotNone(imp)
        self.assertEqual(imp['summary'], '积极向上的研究生群友')

    # 7b. 印象不再"盲写"：提炼时要把现有卡片一起喂回给模型（2026-11-04）
    def test_existing_impression_is_fed_back_to_model(self):
        self.store.set_user_impression('10001', '旧印象：爱在深夜聊 DND，说话带点自嘲')
        curator = MemoryCurator(self.config, self.store, None)
        block = curator._existing_impressions_block({'10001'})
        self.assertIn('爱在深夜聊 DND', block)
        self.assertIn('[10001]', block)
        # 关掉印象功能后整块不出现（与"不注入印象"保持一致）
        curator.config['memory_impression_enabled'] = False
        self.assertEqual(curator._existing_impressions_block({'10001'}), '')
        # 没有旧印象的人不占位
        self.assertEqual(curator._existing_impressions_block({'99999'}), '')

    def test_curator_prompt_contains_existing_impression(self):
        """不只是有个方法：提炼时旧印象真的进了 prompt。"""
        from newGeYi.models import ModelResult

        self.store.set_user_impression('10001', '旧印象：爱开玩笑')
        self.store.buffer_append('group:99999', 'user', '今天又去跑团了',
                                 sender_id='10001', sender_name='阿花')
        captured = {}

        class _Gateway:
            def chat(self, req, kind='chat', session_key=''):
                captured['prompt'] = req.messages[-1]['content']
                return ModelResult(ok=True, text='{}', model='stub',
                                   input_tokens=1, output_tokens=1, latency_ms=1)

        cfg = dict(self.config, memory_curate_messages=1, memory_curate_min_interval=0)
        curator = MemoryCurator(cfg, self.store, _Gateway())
        curator.run_once('group:99999')
        self.assertIn('旧印象：爱开玩笑', captured.get('prompt', ''))

    def test_short_or_identical_impression_does_not_overwrite(self):
        self.store.set_user_impression('10001', '原来的印象写得挺细的')
        curator = MemoryCurator(self.config, self.store, None)
        # 太短（"好人""无"）不许把写好的覆盖掉
        stats = curator._store_results(
            'group:99999', {'user_impressions': [{'user_id': '10001', 'summary': '好人'}]},
            {'10001'}, last_id=1)
        self.assertEqual(stats['impressions'], 0)
        self.assertEqual(self.store.get_user_impression('10001')['summary'], '原来的印象写得挺细的')
        # 一字未改也别刷新 updated_at（否则"多久没变"失真）
        before = self.store.get_user_impression('10001')['updated_at']
        stats = curator._store_results(
            'group:99999',
            {'user_impressions': [{'user_id': '10001', 'summary': '原来的印象写得挺细的'}]},
            {'10001'}, last_id=2)
        self.assertEqual(stats['impressions'], 0)
        self.assertEqual(self.store.get_user_impression('10001')['updated_at'], before)

    # 7c. 印象历史（只追加，能回溯）2026-11-04
    def test_impression_history_is_append_only(self):
        self.store.set_user_impression('10001', '第一版印象：爱开玩笑')
        self.store.set_user_impression('10001', '第二版印象：爱开玩笑，也爱照顾人')
        hist = self.store.impression_history('10001', limit=10)
        self.assertEqual([h['summary'] for h in hist],
                         ['第二版印象：爱开玩笑，也爱照顾人', '第一版印象：爱开玩笑'])
        self.assertEqual(hist[0]['reason'], 'update')
        self.assertEqual(hist[1]['reason'], 'create')
        # 主表只留最新那版
        self.assertEqual(self.store.get_user_impression('10001')['summary'],
                         '第二版印象：爱开玩笑，也爱照顾人')

    def test_identical_impression_writes_no_history(self):
        self.store.set_user_impression('10001', '同一句话')
        changed = self.store.set_user_impression('10001', '同一句话')
        self.assertFalse(changed)
        self.assertEqual(len(self.store.impression_history('10001', limit=10)), 1)

    def test_forget_me_clears_history_too(self):
        self.store.set_user_impression('10001', '会被忘掉的印象')
        self.store.add_user_fact('group:99999', '10001', '会被忘掉的事实')
        self.store.purge_user_facts('group:99999', '10001', include_global=True)
        self.assertEqual(self.store.impression_history('10001', limit=10), [])
        self.assertIsNone(self.store.get_user_impression('10001'))

    def test_panel_exposes_impression_history(self):
        from types import SimpleNamespace

        from newGeYi.panel import Panel

        self.store.set_user_impression('10001', '面板里的第一版')
        self.store.set_user_impression('10001', '面板里的第二版')

        class _Stub:
            def remaining(self, key):
                return 0

            def cursor(self, key):
                return 0

            def quota_for(self, key):
                return {}

        panel = Panel.__new__(Panel)          # 只借用 _session_get，不建真 Runtime
        panel.runtime = SimpleNamespace(store=self.store, rest=_Stub(),
                                        curator=_Stub(), group_profile=_Stub())
        # 私聊：target_id 就是对方，印象直接从它取
        data = panel._session_get({'session_key': 'h:qq:onebot:private:-:10001'})
        layers = [m['layer'] for m in data['memories']]
        self.assertIn('L4', layers)
        self.assertIn('L4-history', layers)
        current = [m for m in data['memories'] if m['layer'] == 'L4'][0]
        self.assertEqual(current['content'], '面板里的第二版')

    # 7d. 容量淘汰的事实进回收站，而不是直接消失（2026-11-04）
    def test_evicted_fact_is_archived(self):
        for i in range(15):
            self.store.add_user_fact('group:99999', '10001', '第%d条事实' % i, importance=0.5 + i * 0.01)
        removed = self.store.trim_user_facts('group:99999', '10001', max_per_user=12, max_per_group=0)
        self.assertEqual(removed, 3)
        self.assertEqual(self.store.count_user_facts('group:99999', '10001'), 12)
        archive = self.store.archived_facts('group:99999', '10001', limit=10)
        self.assertEqual(len(archive), 3)
        self.assertTrue(all('第' in row['fact'] for row in archive))
        self.assertEqual(archive[0]['reason'], 'capacity')

    # 8. 文本相似度计算测试
    def test_text_similarity(self):
        from newGeYi.storage.database import text_similarity
        # 完全相同
        self.assertEqual(text_similarity('喜欢喝可口可乐', '喜欢喝可口可乐'), 1.0)
        # 高度相似（同一个偏好，句式略不同）
        sim_high = text_similarity('喜欢喝无糖可乐', '平时只喝无糖可口可乐')
        self.assertGreaterEqual(sim_high, 0.35)
        # 完全无关
        sim_zero = text_similarity('喜欢喝无糖可乐', '家里养了一只布偶猫叫米粒')
        self.assertEqual(sim_zero, 0.0)

    # 9. 检索端 MMR 语义多样性去重过滤（防同类记忆霸屏）
    def test_retrieval_mmr_diversity(self):
        msg = self._make_msg(text='我想喝点饮料', sender_id='10001')
        sk = msg.session.as_text()

        # 插入 3 条相似的可乐记忆（重要度高，分值扎堆在最前）
        self.store.add_user_fact(sk, '10001', '平时非常喜欢喝冰镇无糖可乐', importance=0.9)
        self.store.add_user_fact(sk, '10001', '去便利店总会买无糖可口可乐', importance=0.88)
        self.store.add_user_fact(sk, '10001', '很爱喝无糖可乐不喝有糖的', importance=0.86)
        # 插入 2 条完全不同维度的其他记忆（重要度稍低）
        self.store.add_user_fact(sk, '10001', '家里养了一只英短银渐层猫咪叫米粒', importance=0.7)
        self.store.add_user_fact(sk, '10001', '职业是Web前端开发工程师在上海工作', importance=0.68)

        # 限制只注入 3 条记忆
        self.repo.config['memory_inject_facts'] = 3
        self.repo.config['memory_diversity_enabled'] = True
        self.repo.config['memory_diversity_threshold'] = 0.30

        recalled = self.repo.recall(msg)
        selected_facts = [f['fact'] for f in recalled['facts']]
        self.assertEqual(len(selected_facts), 3)

        # 核心断言：由于开启了 MMR 多样性过滤，3 条可乐事实只能入选 1 条最高分的！
        # 其余 2 个名额必须让给猫咪和前端工作！
        coke_count = sum(1 for f in selected_facts if '可乐' in f)
        self.assertEqual(coke_count, 1, f'可乐记忆被重复选入：{selected_facts}')
        self.assertTrue(any('猫咪' in f for f in selected_facts), f'猫咪记忆被挤掉：{selected_facts}')
        self.assertTrue(any('前端' in f for f in selected_facts), f'前端工作记忆被挤掉：{selected_facts}')

    # 10. 写入端模糊判重（Fuzzy Dedup）
    def test_write_time_fuzzy_dedup(self):
        sk = 'group:99999'
        self.store.add_user_fact(sk, '10001', '平时喜欢喝无糖可乐', importance=0.8)

        # 查找一个换了说法的相似事实
        similar = self.store.find_similar_user_fact(sk, '10001', '经常喝冰镇无糖可乐', threshold=0.35)
        self.assertIsNotNone(similar)
        self.assertEqual(similar['fact'], '平时喜欢喝无糖可乐')

        # 查找一个完全无关的事实
        unrelated = self.store.find_similar_user_fact(sk, '10001', '刚买了新的机械键盘', threshold=0.35)
        self.assertIsNone(unrelated)

    # 11. 数据库存量批量去重 (deduplicate_active_memories)
    def test_batch_deduplicate_active_memories(self):
        sk = 'group:99999'
        # 插入 3 条相似的自身记忆
        self.store.add_self_memory(sk, '喜欢在春天吃草莓大福', importance=0.7)
        self.store.add_self_memory(sk, '春天最喜欢吃草莓大福甜点', importance=0.7)
        self.store.add_self_memory(sk, '昨天画了一张星空水彩画', importance=0.6)

        stats = self.store.deduplicate_active_memories(threshold=0.45)
        self.assertGreaterEqual(stats['self_memories_expired'], 1)

        # 检查有效记忆列表：草莓大福只剩 1 条，星空画依然有效
        active = self.store.list_self_memories(sk, include_expired=False)
        self.assertEqual(len(active), 2)
        daifuku_count = sum(1 for m in active if '草莓大福' in m['content'])
        self.assertEqual(daifuku_count, 1)

    # 12. 查看记忆指令测试（无时间戳，包含印象与事实）
    def test_cmd_view_my_memory(self):
        from unittest.mock import MagicMock
        from newGeYi.routing.commands import CommandRouter

        msg = self._make_msg(text='我的记忆', sender_id='10001')
        sk = msg.session.as_text()

        # 空白记忆提示
        runtime_mock = MagicMock()
        runtime_mock.store = self.store
        runtime_mock.config = self.config
        runtime_mock.memory = self.repo
        handler = CommandRouter(runtime_mock)

        empty_res = handler._cmd_view_my_memory(msg)
        self.assertIn('一片空白', empty_res)

        # 写入印象和事实后
        self.store.set_user_impression('10001', '经常在群里活跃的热心老朋友')
        self.store.add_user_fact(sk, '10001', '喜欢喝无糖可口可乐')
        self.store.add_user_fact(sk, '10001', '家里养了一只小猫叫米粒')

        res = handler._cmd_view_my_memory(msg)
        self.assertIn('【歌伊的小本本 · 记忆档案】', res)
        self.assertIn('经常在群里活跃的热心老朋友', res)
        self.assertIn('喜欢喝无糖可口可乐', res)
        self.assertIn('家里养了一只小猫叫米粒', res)
        # 确认没有时间戳前缀
        self.assertNotIn('[刚刚]', res)
        self.assertNotIn('[今天]', res)

    def test_private_and_group_impressions_are_isolated(self):
        """群聊印象与私聊印象互不干扰、独立提炼。"""
        self.store.set_user_impression('10001', '群聊印象：很活泼爱水群', scope='group')
        self.store.set_user_impression('10001', '私聊印象：说话温和认真', scope='private')

        group_imp = self.store.get_user_impression('10001', scope='group')
        private_imp = self.store.get_user_impression('10001', scope='private')
        self.assertEqual(group_imp['summary'], '群聊印象：很活泼爱水群')
        self.assertEqual(private_imp['summary'], '私聊印象：说话温和认真')

        self.store.set_user_impression('10001', '群聊印象：更新后第二版', scope='group')
        group_hist = self.store.impression_history('10001', scope='group')
        private_hist = self.store.impression_history('10001', scope='private')
        self.assertEqual(len(group_hist), 2)
        self.assertEqual(len(private_hist), 1)

    def test_curator_isolates_private_and_group_sessions(self):
        """提炼器在私聊提炼私聊印象与私聊事实，在群聊提炼群聊印象与群聊事实。"""
        curator = MemoryCurator(self.config, self.store, None)
        priv_sk = 'bot:qq:onebot:private:-:10001'
        grp_sk = 'bot:qq:onebot:group:-:20001'

        curator._store_results(grp_sk, {
            'user_impressions': [{'user_id': '10001', 'summary': '群聊印象：群里的技术大佬'}],
            'user_facts': [{'user_id': '10001', 'fact': '群里经常写Python脚本', 'scope': 'group'}],
        }, {'10001'}, last_id=1)

        curator._store_results(priv_sk, {
            'user_impressions': [{'user_id': '10001', 'summary': '私聊印象：私底下是个喜欢二次元的可爱群友'}],
            'user_facts': [{'user_id': '10001', 'fact': '私聊时说过喜欢看动画电影', 'scope': 'global'}],
        }, {'10001'}, last_id=2)

        self.assertEqual(self.store.get_user_impression('10001', scope='group')['summary'],
                         '群聊印象：群里的技术大佬')
        self.assertEqual(self.store.get_user_impression('10001', scope='private')['summary'],
                         '私聊印象：私底下是个喜欢二次元的可爱群友')

        priv_facts = self.store.list_user_facts(priv_sk, '10001')
        self.assertTrue(all(f['session_key'] == priv_sk for f in priv_facts))

    def test_cross_scope_memory_recall_isolation_and_interconnection(self):
        """互通开关开启与关闭时，私聊与群聊记忆库召回的完整验证。"""
        from newGeYi.models import IncomingMessage, SessionKey

        repo = MemoryRepository(self.store, dict(self.config))
        priv_session = SessionKey('bot', 'qq', 'onebot', 'private', '-', '10001')
        grp_session = SessionKey('bot', 'qq', 'onebot', 'group', '-', '20001')
        priv_msg = IncomingMessage(
            session=priv_session, message_id='m1', sender_id='10001', sender_name='测试者',
            text='你好呀', raw_text='你好呀', is_group=False, mentioned_bot=True,
            event=None, proc=None
        )
        grp_msg = IncomingMessage(
            session=grp_session, message_id='m2', sender_id='10001', sender_name='测试者',
            text='大家好', raw_text='大家好', is_group=True, mentioned_bot=True,
            event=None, proc=None
        )

        self.store.set_user_impression('10001', '群聊印象卡片', scope='group')
        self.store.set_user_impression('10001', '私聊印象卡片', scope='private')
        self.store.add_user_fact(grp_session.as_text(), '10001', '群里爱吃辣')
        self.store.add_user_fact(priv_session.as_text(), '10001', '私聊说讨厌胡萝卜')

        # 1. 互通开关关闭：完全隔离
        repo.config['memory_private_enabled'] = False

        priv_block = repo.build_block(priv_msg)
        self.assertIn('私聊印象卡片', priv_block)
        self.assertNotIn('群聊印象卡片', priv_block)
        self.assertIn('私聊说讨厌胡萝卜', priv_block)
        self.assertNotIn('群里爱吃辣', priv_block)

        grp_block = repo.build_block(grp_msg)
        self.assertIn('群聊印象卡片', grp_block)
        self.assertNotIn('私聊印象卡片', grp_block)
        self.assertIn('群里爱吃辣', grp_block)
        self.assertNotIn('私聊说讨厌胡萝卜', grp_block)

        # 2. 只开总闸（默认口径）：**单向**——私聊能读到群聊，群聊读不到私聊
        repo.config['memory_private_enabled'] = True

        priv_cross_block = repo.build_block(priv_msg)
        self.assertIn('私聊印象卡片', priv_cross_block)
        self.assertIn('群聊印象卡片', priv_cross_block)
        self.assertIn('私聊说讨厌胡萝卜', priv_cross_block)
        self.assertIn('群里爱吃辣', priv_cross_block)

        grp_cross_block = repo.build_block(grp_msg)
        self.assertIn('群聊印象卡片', grp_cross_block)
        self.assertIn('群里爱吃辣', grp_cross_block)
        # 2026-11-05：单向共享——群里不该翻出私聊的记忆/印象（否则"好几个恋爱对象"）
        self.assertNotIn('私聊印象卡片', grp_cross_block)
        self.assertNotIn('私聊说讨厌胡萝卜', grp_cross_block)

        # 3. 再把反向闸打开：才恢复双向互通
        repo.config['memory_private_to_group'] = True
        grp_two_way = repo.build_block(grp_msg)
        self.assertIn('私聊印象卡片', grp_two_way)
        self.assertIn('私聊说讨厌胡萝卜', grp_two_way)


class SummaryTrimTest(unittest.TestCase):
    """L1 摘要表必须有上限。

    2026-11-10：`SQLiteStore.trim_group_summaries()` 早就写好了，但**全仓库从没调用**
    ——线上大群的 `group_summaries` 涨到 1800+ 条（实际只会注入最近 3~6 条）。
    现在提炼器写完摘要顺手裁一次，这里钉住"确实裁了"和"默认上限够用"。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='geyi_summary_trim_')
        self.store = SQLiteStore(os.path.join(self.tmp, 'trim.sqlite3'))
        self.session = 'h:qq:onebot:group:-:123456'

    def tearDown(self):
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _curator(self, keep):
        return MemoryCurator({'assistant_name': '歌伊', 'memory_summary_keep': keep},
                             self.store, None)

    def test_keeps_only_configured_amount(self):
        cur = self._curator(2)
        for i in range(5):
            cur._store_results(self.session, {'group_summary': '第 %d 件公共事件' % i},
                               set(), i + 1)
        rows = self.store.recent_group_summaries(self.session, limit=50)
        self.assertEqual(len(rows), 2, '旧摘要没有被裁掉')
        self.assertIn('第 4 件', rows[-1]['content'], '裁掉的应当是最旧的，保留最新的')

    def test_default_keep_does_not_truncate_normal_volume(self):
        cur = self._curator(200)
        for i in range(3):
            cur._store_results(self.session, {'group_summary': '小事 %d' % i}, set(), i + 1)
        self.assertEqual(len(self.store.recent_group_summaries(self.session, limit=50)), 3)


if __name__ == '__main__':
    unittest.main()
