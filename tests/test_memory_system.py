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


if __name__ == '__main__':
    unittest.main()
