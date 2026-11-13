# -*- encoding: utf-8 -*-
"""世界书（Worldbook）模块。

设计目标（关键词才读取设定，减少消耗）：
- 世界书条目以 JSON 文件保存（data/worldbook.json）。
- 只有和用户消息**相关**的条目才注入 prompt。
- 开关由 config 的 worldbook_enabled 控制。
- 不与联网、搜索、RAG 耦合。

条目字段：
- keywords：强关键词数组，命中任一即注入（子串匹配，全小写比较，支持碰撞词屏蔽）。
- weak_keywords：可选，弱/泛化关键词数组（如“猫”“饭”“衣服”“任务”“学习”），
  需满足语境指向歌伊/异世界，或与其它关键词同时命中才触发，防止群友聊自己三次元日常时误触发。
- excludes：可选，负向排除词数组；匹配前先从文本中遮罩排除词（如“崇拜”不触发“崇”、“上海”不触发“海”）。
- links：可选，关联条目 id 数组；当本条目命中且名额未满时，给当前阶段可见的关联条目追加联动加权。
- priority：可选，同分或竞争时的优先级微调（浮点数，默认 0）。
- content：注入的正文，可多行（prompt_builder 会给续行加缩进）。
- enabled：false 的条目直接忽略。
- max_chars：可选，本条目的字数上限；不填用调用方的默认值。
- always：true 表示**常驻**——不看关键词，每次都注入（见下）。
- min_stage / max_stage：关系阶段门槛（0~5，见 relationship/stages.py）。私聊 galgame 系统里，
  深层设定（身世、心事）写 min_stage，低阶段**根本不进 prompt**——她说不出来，也套不出来；
  max_stage 用来写"对不熟的人只含糊地说"的版本。缺省 = 不设限。
  没有关系系统（群聊 / 功能关闭）时按阶段 0 处理：min_stage>0 的条目不会出现。
- scope：private / group / both（默认）。私人设定只在私聊注入。
- topic：true 表示这是一条"主动谈资"（今天发生的小事），不参与检索，
  由 RelationshipService 每天抽一条放进私聊 prompt。
- id：可选，条目的稳定标识（谈资去重与关联索引）；不填则用内容哈希。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from typing import Any

# 单条世界书内容的默认字数上限。详细设定（外观、常服、世界观）需要更大空间，
# 但也不能无限——命中的条目是直接进 prompt 的，上限就是最坏情况下的 token 开销。
DEFAULT_MAX_CHARS_PER_ENTRY = 800
DEFAULT_MAX_ENTRIES = 3
STORY_MAX_ENTRIES = 6
DEFAULT_MAX_TOTAL_CHARS = 2400
STORY_MAX_TOTAL_CHARS = 4200

# 剧情探索模式持久化前缀（存在 SQLite settings 表中，按用户 QQ 号与私聊会话双重记录）
STORY_MODE_USER_PREFIX = 'story_mode_user:'
STORY_MODE_SESSION_PREFIX = 'story_mode:'

# BM25 参数。中文条目长度差异很大（一条 100 字、一条 700 字），
# b 取小一点，减少对长条目的惩罚——长条目通常信息更多，不该被压。
BM25_K1 = 1.2
BM25_B = 0.5

# 命中强关键词的基准加成。
KEYWORD_BONUS = 12.0
# 命中弱/泛化关键词（通过语境校验后）的基准加成。
WEAK_KEYWORD_BONUS = 10.0
# 追问时前文命中关键词的继承加成（按距离衰减）。
CONTEXT_KEYWORD_BONUS = 8.0
# 前文 BM25 相对当前句的权重（当前句 : 前文 ≈ 3 : 1）。
CONTEXT_BM25_WEIGHT = 0.35
# 关联条目（links）联动加成。
LINK_BONUS = 5.0
STORY_LINK_BONUS = 7.0

# 纯 BM25（没命中关键词）时的额外闸门。
MIN_COVERAGE = 0.6
MIN_MATCHED_TERMS = 2
STORY_MIN_COVERAGE = 0.36
STORY_RARE_IDF = 2.1

_CJK_RUN = re.compile(r'[\u4e00-\u9fff]+')
_ASCII_WORD = re.compile(r'[a-z0-9]+')

# 口语虚词二元组：计算纯 BM25 覆盖率时剔除，避免“你们那边的猎人公会是怎么来的”
# 被虚词二元组（你们/那边/边的/是怎/怎么/么来/来的）按最大 IDF 撑大分母而误杀。
_STOP_BIGRAMS = frozenset({
    '什么', '怎么', '可以', '没有', '不是', '就是', '我们', '你们', '他们', '她们',
    '这个', '那个', '这些', '那些', '感觉', '觉得', '因为', '所以', '虽然', '但是',
    '如果', '一下', '今天', '明天', '昨天', '现在', '刚才', '平时', '知道', '告诉',
    '说说', '讲讲', '聊聊', '为什么', '为啥', '怎么会', '怎么样', '是个', '样的',
    '的人', '那他', '那她', '那它', '后来', '然后', '详细', '到底', '真的', '还有',
    '能不能', '是不是', '有没有', '好不好', '行不行',
})

# 单字/短词在现代日常词汇中的常见碰撞词：匹配短关键词前先从文本中遮罩，
# 防止“怪累”命中“怪”、“上海”命中“海”、“崇拜”命中“崇”、“没电/充电”命中“电”。
_DEFAULT_COLLISIONS = (
    # “怪”碰撞
    '奇怪', '怪不得', '难怪', '怪累', '怪我', '怪你', '怪他', '怪她', '怪事', '怪异',
    '怪怪', '怪好', '怪可', '怪有', '太怪', '真怪', '好怪', '古怪', '大惊小怪',
    # “海”碰撞
    '上海', '刘海', '脑海', '海报', '海量', '海鲜', '海口', '北海', '青海', '南海',
    '东海', '黄海', '渤海', '四海', '海绵', '海盗', '海豚', '海鸥', '海带', '海参',
    '海藻', '海外', '海关', '海军', '海峡', '海滩', '海岸', '海淀', '海风', '海浪',
    '海景', '海边', '海岛', '海湾', '人山人海',
    # “崇”碰撞
    '崇拜', '推崇', '崇尚', '崇高', '崇敬', '尊崇', '崇明', '崇礼',
    # “电”碰撞
    '充电', '没电', '省电', '断电', '停电', '掉电', '来电', '回电', '电影', '电视',
    '电话', '电梯', '电池', '电竞', '电商', '电音', '电网', '电瓶', '电扇', '电吹风',
    '电磁', '电线', '电缆', '电表', '电费', '电车', '电动车', '手电', '闪电', '静电',
    # “雷”碰撞
    '避雷', '踩雷', '雷人', '雷同', '手雷', '地雷', '鱼雷', '扫雷', '雷霆', '雷迪',
    '雷锋', '雷神', '雷军', '被雷到', '雷点',
    # “猫”碰撞
    '熊猫', '猫头鹰', '夜猫子', '躲猫猫', '猫腻', '招财猫', '猫眼', '猫步', '机器猫',
    # “龙/蛇/弓”碰撞
    '恐龙', '一条龙', '龙虾', '马卡龙', '沙龙', '龙卷风', '水龙头', '变色龙', '乌龙',
    '尼龙', '龙门', '龙套', '龙头', '龙眼', '车水马龙', '生龙活虎', '来龙去脉',
    '贪吃蛇', '打草惊蛇', '虎头蛇尾', '画蛇添足', '杯弓蛇影', '蛇精',
    '惊弓之鸟', '左右开弓', '弹弓',
    # 现代技术/日常短语碰撞（防“粒子特效/做苦力/学生协会/世界观三观/公会战”误触发）
    '粒子特效', '基本粒子', '高能物理', '量子物理', '做苦力', '当苦力', '苦力活', '卖苦力',
    '消费者协会', '学生会', '校友会', '志愿者协会', '足球协会', '篮球协会', '行业协会',
    '人生观', '价值观', '三观', '世界纪录', '世界杯', '世界大战', '新世界', '大世界',
    '公会战', '游戏公会', '行会战', '刷副本', '打副本', '下副本',
)

# 泛化日常词集合：即使写在 keywords 中，若缺乏角色/异世界指向且带有明显的
# 第一人称自述（“我今天吃饭…”）或第三方/现代游戏语境（“这款游戏的技能…”），自动按弱关键词拦截。
_AMBIENT_KEYWORDS = frozenset({
    # 单字泛词
    '怪', '海', '崇', '电', '雷', '猫', '饭', '饿', '龙', '蛇', '弓',
    # 穿搭与日常起居
    '穿的', '穿得', '穿搭', '袜子', '鞋子', '皮鞋', '日常', '每天', '作息',
    # 饮食泛词
    '吃的', '好吃', '早餐', '零食', '点心', '甜点', '甜食', '美食', '爱吃', '喜欢吃',
    '吃货', '贪吃', '苹果', '蜂蜜',
    # 现代物品与世界泛词
    '手机', '电脑', '机器', '火车', '科技', '文明', '下雨', '雨天',
    '你们那', '你的世界', '你们那边', '世界观', '国家', '城邦', '政体', '王国', '地图',
    # 学校与工作泛词
    '学校', '上学', '读书', '学习', '进修', '同学', '老师', '课程', '毕业',
    '任务', '出差', '合作', '战斗', '打怪', '小队', '危险',
    # 游戏/属性泛词
    '技能', '属性', '等级', '几级', '一级', '二级', '三级', '四级', '五级',
    '最强', '考核', '战斗力', '分级', '强不强', '委托', '协会', '公会', '雇佣', '福利', '招募',
    # 抽象情绪与人际泛词
    '朋友', '熟人', '好朋友', '爱好', '兴趣', '讨厌', '最怕', '害怕', '最爱', '喜好',
    '习惯', '弱点', '平时喜欢', '崇拜', '英雄', '钦佩', '佩服', '偶像', '榜样', '守护',
    '保护', '欣赏', '受伤', '治疗', '担心', '遗憾', '写信', '书信', '偏远',
    '以后', '未来', '梦想', '打算', '见面', '想见', '愿望', '心愿', '放弃', '报仇', '复仇',
    '灾难', '末日', '毁灭', '大灾', '分裂', '海洋', '大海', '传说', '王城', '混乱', '叛徒',
    '远方', '东方', '家里', '从小',
})

# 指向歌伊或异世界设定的锚点词：出现任一即说明话题与歌伊/她的世界相关，放行弱关键词。
_PERSONA_ANCHORS = (
    '你', '歌伊', '修女', '教会', '清泉', '圣职', '猎人', '猎人公会', '异世界', '你们那', '你那边',
    '你们世界', '你的世界', '人家', '月白', '逐影', '格伦', '米娅', '莱妮', '罗莎', '安娜',
    '面包屑', '天灾', '拟灾', '粒子', '化人', '麦穗', '渊海', '登仙楼', '玄门', '东陆',
    '西陆', '西大陆', '冒险家', '神圣粒子', '治愈粒子', '苦力怕', '末影',
)

# 群友自述或现代/第三方话题干扰词：当句子里没有 _PERSONA_ANCHORS，却出现这些词时，
# 说明群友在聊自己或现代事物（如“我今天吃了个汉堡”“这款游戏的技能树”“今天学习高数”），拦截弱关键词。
_SELF_OR_MODERN_DISTRACTORS = (
    '我', '俺', '咱', '我家', '我的', '咱们', '老子', '本人',
    '他', '她', '它', '他们', '她们', '它们',
    '游戏', '这款', '这个游戏', '打游戏', '玩游戏', '手游', '端游', '卡池', '抽卡', '公会战', '副本',
    '高数', '数学', '英语', '物理', '化学', '政治', '历史课', '专业课', '早八',
    '考研', '高考', '期末', '期中', '挂科', '学分', '论文', '答辩', '作业', '试卷',
    '上班', '加班', '打工', '公司', '老板', '领导', '同事', '室友', '客户', '工资',
    '今天穿', '这件t恤', 't恤', '卫衣', '牛仔裤', '外套', '外卖', '汉堡', '火锅', '奶茶',
    '刚才', '上海', '北京', '广州', '深圳', '杭州', '成都', '武汉', '南京', '重庆',
    '电影', '番剧', '动漫', '小说里', '电视剧',
)

# 追问/指代延续信号：当前句很短且包含这些词时，允许继承最近对话命中的世界书条目。
_FOLLOWUP_CUES = (
    '后来', '然后', '接着', '之后', '再后来', '为什么', '为啥', '怎么会', '怎么了',
    '咋了', '详细', '展开', '具体', '说说', '讲讲', '继续', '还有呢', '还有吗',
    '那他', '那她', '那它', '他呢', '她呢', '它呢', '他是', '她是', '它是',
    '他现在', '她现在', '他平时', '她平时', '在哪', '叫什么', '多大', '几岁',
    '厉害吗', '强吗', '怕不怕', '危险吗', '多不多', '常见吗',
    '真的吗', '当真', '什么样', '长什么', '是谁', '怎么认识', '怎么相认', '怎么治',
    '那你们', '那那边', '那里面', '有什么区别', '怎么区分', '哪种', '哪几个', '有哪些',
)

# 跨话题桥接信号：即使当前句出现了新的强关键词，只要带这些比较/指代词，仍保留上一轮主话题做对比。
_BRIDGE_CUES = (
    '他', '她', '它', '这位', '那位',
    '那他', '那她', '那它', '他呢', '她呢', '它呢', '和它', '跟它', '和他', '跟她',
    '与它', '比起来', '相比', '有什么区别', '怎么区分', '什么关系', '有什么关系',
    '那后来', '那然后', '那之后', '还有呢', '还有吗',
)

# 第三方人物条目的性别/指代归属，防止问“他”（哥哥/格伦）时串出月白，或问“她”时串出哥哥。
_MALE_NPC_ENTRIES = frozenset({'origin-brother', 'origin-vague', 'town-friends'})
_FEMALE_NPC_ENTRIES = frozenset({'friend-yuebai', 'friend-yuebai-worry', 'town-friends'})
_ALL_NPC_ENTRIES = _MALE_NPC_ENTRIES | _FEMALE_NPC_ENTRIES


def _positive_int(value: Any) -> int:
    """把配置里的 max_chars 解析成正整数；非法/缺省一律返回 0（表示用默认值）。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _float_or_zero(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def tokenize(text: str) -> list[str]:
    """中文切字符二元组，英文数字切词。

    为什么不上分词库：世界书只有几十条、查询是群聊短消息，二元组的召回够用，
    而结巴那类依赖不值得为它引入（装不上、版本打架都是真实成本）。
    """
    lowered = str(text or '').lower()
    tokens: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    tokens.extend(_ASCII_WORD.findall(lowered))
    return tokens


def _stage_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(5, parsed))


def _clean_str_list(raw: Any, lower: bool = True) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item or '').strip()
        if s:
            out.append(s.lower() if lower else s)
    return out


def is_story_mode(store: Any, session_key: str = '', user_id: str = '') -> bool:
    """检查当前用户或会话是否开启了「剧情模式」。"""
    if store is None:
        return False
    uid = str(user_id or '').strip()
    if uid:
        try:
            val = str(store.get_setting(STORY_MODE_USER_PREFIX + uid) or '').strip()
            if val in ('1', 'on', 'true', 'True'):
                return True
            if val in ('0', 'off', 'false', 'False'):
                return False
        except Exception:  # noqa: BLE001
            pass
    skey = str(session_key or '').strip()
    if skey:
        try:
            val = str(store.get_setting(STORY_MODE_SESSION_PREFIX + skey) or '').strip()
            if val in ('1', 'on', 'true', 'True'):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def set_story_mode(store: Any, session_key: str = '', user_id: str = '',
                   enabled: bool | None = None) -> None:
    """开启/关闭/重置某个用户（及私聊会话）的「剧情模式」。"""
    if store is None:
        return
    flag = '' if enabled is None else ('1' if enabled else '')
    uid = str(user_id or '').strip()
    if uid:
        try:
            store.set_setting(STORY_MODE_USER_PREFIX + uid, flag)
        except Exception:  # noqa: BLE001
            pass
    skey = str(session_key or '').strip()
    if skey and ':private:' in skey:
        try:
            store.set_setting(STORY_MODE_SESSION_PREFIX + skey, flag)
        except Exception:  # noqa: BLE001
            pass


def list_story_mode_users(store: Any) -> list[str]:
    """列出当前开启了「剧情模式」的全部用户 QQ 号。"""
    if store is None or not hasattr(store, 'settings_with_prefix'):
        return []
    users: list[str] = []
    try:
        for key, value in store.settings_with_prefix(STORY_MODE_USER_PREFIX):
            uid = key[len(STORY_MODE_USER_PREFIX):].strip()
            if uid and str(value or '').strip() in ('1', 'on', 'true', 'True'):
                users.append(uid)
    except Exception:  # noqa: BLE001
        return []
    return sorted(set(users))


class _Entry:
    __slots__ = (
        'keywords', 'weak_keywords', 'excludes', 'links', 'priority',
        'content', 'max_chars', 'always', 'term_freq', 'length',
        'id', 'min_stage', 'max_stage', 'scope', 'topic',
    )

    def __init__(self, keywords, content, max_chars, always,
                 entry_id='', min_stage=None, max_stage=None, scope='both', topic=False,
                 weak_keywords=None, excludes=None, links=None, priority=0.0):
        self.keywords = list(keywords or [])
        self.weak_keywords = list(weak_keywords or [])
        self.excludes = list(excludes or [])
        self.links = list(links or [])
        self.priority = float(priority or 0.0)
        self.content = content
        self.max_chars = max_chars
        self.always = always
        self.id = entry_id or hashlib.md5(content.encode('utf-8')).hexdigest()[:10]
        self.min_stage = min_stage
        self.max_stage = max_stage
        self.scope = scope if scope in ('private', 'group') else 'both'
        self.topic = bool(topic)
        # 文档 = 强/弱关键词 + 正文。关键词也算进词频，但命中另有显式加成。
        all_kws = ' '.join(self.keywords + self.weak_keywords)
        tokens = tokenize(all_kws + ' ' + content)
        freq: dict[str, int] = {}
        for token in tokens:
            freq[token] = freq.get(token, 0) + 1
        self.term_freq = freq
        self.length = max(1, len(tokens))


class WorldbookService:
    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.Lock()
        self.entries: list[dict] = []
        self._docs: list[_Entry] = []
        self._by_id: dict[str, _Entry] = {}
        self._idf: dict[str, float] = {}
        self._avg_len = 1.0
        self._default_idf = 1.0
        self.load()

    @staticmethod
    def _visible(entry: '_Entry', stage: int | None, scope: str) -> bool:
        """这个条目在给定阶段/场景下能不能出现（逻辑见模块说明）。"""
        eff = 0 if stage is None else int(stage)
        if entry.scope != 'both' and scope in ('private', 'group') and entry.scope != scope:
            return False
        if entry.min_stage is not None and eff < entry.min_stage:
            return False
        if entry.max_stage is not None and eff > entry.max_stage:
            return False
        return True

    def load(self) -> None:
        entries: list[_Entry] = []
        raw_entries: list[dict] = []
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                # 兼容两种格式：裸列表，或 {"entries":[...]}
                if isinstance(raw, dict):
                    raw = raw.get('entries') or []
                if isinstance(raw, list):
                    for item in raw:
                        if not isinstance(item, dict) or not item.get('enabled', True):
                            continue
                        clean_keywords = _clean_str_list(item.get('keywords'), lower=True)
                        clean_weak = _clean_str_list(item.get('weak_keywords'), lower=True)
                        clean_excludes = _clean_str_list(item.get('excludes'), lower=True)
                        clean_links = _clean_str_list(item.get('links'), lower=False)
                        priority = _float_or_zero(item.get('priority'))
                        content = str(item.get('content', '')).strip()
                        always = bool(item.get('always'))
                        # 常驻或谈资条目允许没有 keywords
                        if (clean_keywords or clean_weak or always or item.get('topic')) and content:
                            entry = _Entry(
                                clean_keywords, content,
                                _positive_int(item.get('max_chars')), always,
                                entry_id=str(item.get('id') or '').strip(),
                                min_stage=_stage_or_none(item.get('min_stage')),
                                max_stage=_stage_or_none(item.get('max_stage')),
                                scope=str(item.get('scope') or 'both').strip().lower(),
                                topic=bool(item.get('topic')),
                                weak_keywords=clean_weak,
                                excludes=clean_excludes,
                                links=clean_links,
                                priority=priority,
                            )
                            entries.append(entry)
                            raw_item = {
                                'id': entry.id,
                                'keywords': clean_keywords,
                                'content': content,
                                'max_chars': _positive_int(item.get('max_chars')),
                                'always': always,
                                'min_stage': entry.min_stage,
                                'max_stage': entry.max_stage,
                                'scope': entry.scope,
                                'topic': entry.topic,
                            }
                            if clean_weak:
                                raw_item['weak_keywords'] = clean_weak
                            if clean_excludes:
                                raw_item['excludes'] = clean_excludes
                            if clean_links:
                                raw_item['links'] = clean_links
                            if priority:
                                raw_item['priority'] = priority
                            raw_entries.append(raw_item)
        except (OSError, UnicodeError, json.JSONDecodeError):
            entries, raw_entries = [], []

        idf, avg_len, default_idf = self._build_idf(entries)
        by_id = {e.id: e for e in entries}
        with self._lock:
            self.entries = raw_entries
            self._docs = entries
            self._by_id = by_id
            self._idf = idf
            self._avg_len = avg_len
            self._default_idf = default_idf

    @staticmethod
    def _build_idf(entries: list[_Entry]) -> tuple[dict[str, float], float, float]:
        total = len(entries)
        if not total:
            return {}, 1.0, 1.0
        doc_freq: dict[str, int] = {}
        for entry in entries:
            for token in entry.term_freq:
                doc_freq[token] = doc_freq.get(token, 0) + 1
        idf = {
            token: math.log(1.0 + (total - count + 0.5) / (count + 0.5))
            for token, count in doc_freq.items()
        }
        avg_len = sum(entry.length for entry in entries) / total
        default_idf = math.log(1.0 + (total + 0.5) / 0.5)
        return idf, max(1.0, avg_len), default_idf

    def _bm25(self, query_tokens: list[str], entry: _Entry) -> float:
        if not query_tokens:
            return 0.0
        score = 0.0
        norm = BM25_K1 * (1.0 - BM25_B + BM25_B * entry.length / self._avg_len)
        for token in set(query_tokens):
            freq = entry.term_freq.get(token)
            if not freq:
                continue
            idf = self._idf.get(token, 0.0)
            score += idf * (freq * (BM25_K1 + 1.0)) / (freq + norm)
        return score

    def _coverage(self, query_tokens: list[str], entry: _Entry) -> tuple[int, float]:
        """查询词有多大比例真的在条目里出现过：返回 (命中词数, 覆盖率)。

        剔除口语虚词二元组（_STOP_BIGRAMS），防止自然问句中的虚词把分母撑大；
        若全句都是虚词则回退到原集合。
        """
        unique = set(query_tokens)
        if not unique:
            return 0, 0.0
        core = {t for t in unique if t not in _STOP_BIGRAMS}
        if len(core) >= MIN_MATCHED_TERMS:
            unique = core
        total = sum(self._idf.get(token, self._default_idf) for token in unique)
        if total <= 0:
            return 0, 0.0
        matched = [token for token in unique if token in entry.term_freq]
        if not matched:
            return 0, 0.0
        hit_mass = sum(self._idf.get(token, self._default_idf) for token in matched)
        return len(matched), hit_mass / total

    @staticmethod
    def _allow_ambient(text: str, entry: _Entry | None = None,
                       allow_pronoun_followup: bool = False) -> bool:
        """判断弱/泛化关键词在当前句子中是否允许触发。

        规则：
        1. 若句子出现人称代词（他/她）且无第二人称（你/歌伊），按性别归属仅放行对应第三方人物条目，
           避免把“她喜欢吃什么”（问月白）误当成问歌伊本人，也避免把“他平时在哪”（问哥哥）串给月白。
        2. 若句子出现事物代词（它/它们）且无第二人称：直接关键词阶段不放行角色泛词，
           但多轮追问阶段（allow_pronoun_followup=True）允许指代前文的怪物/天灾/猫咪等条目。
        3. 若句子包含指向歌伊/异世界的锚点（你/歌伊/修女/教会/清泉/猎人…），直接放行。
        4. 若句子不含锚点，但带有明显的第一人称自述或现代生活/游戏干扰词（我今天…/我家…/这款游戏…/高数…），
           说明是群友聊自己的事，予以拦截。
        5. 其余情况（无主语提问如“平时喜欢吃什么？”或短句“这件衣服好看”），放行。
        """
        lowered = str(text or '').lower()
        has_second = any(a in lowered for a in ('你', '歌伊', '人家', '修女'))
        has_female = any(p in lowered for p in ('她', '她们'))
        has_male = any(p in lowered for p in ('他', '他们'))
        has_neuter = any(p in lowered for p in ('它', '它们'))
        if (has_female or has_male) and not has_second and entry is not None:
            allowed_npcs: set[str] = set()
            if has_female:
                allowed_npcs.update(_FEMALE_NPC_ENTRIES)
            if has_male:
                allowed_npcs.update(_MALE_NPC_ENTRIES)
            if entry.id not in allowed_npcs:
                # 已知专属其他性别的人物条目严禁串台（例如问“她”绝不命中哥哥，问“他”绝不命中月白）
                if entry.id in _ALL_NPC_ENTRIES or not allow_pronoun_followup:
                    return False
                # 对未登记在固定集合里的自定义/测试人物条目，仅在多轮追问且正文有对应人称特征时放行
                content_ok = (
                    (has_female and any(k in entry.content for k in ('她', '少女', '猫娘', '婶')))
                    or (has_male and any(k in entry.content for k in ('他', '哥哥', '兄', '大叔', '会长', '头领')))
                )
                if not content_ok:
                    return False
        if has_neuter and not has_second and not allow_pronoun_followup:
            if not any(anchor in lowered for anchor in _PERSONA_ANCHORS):
                return False
        if any(anchor in lowered for anchor in _PERSONA_ANCHORS):
            return True
        if allow_pronoun_followup:
            stripped_distractors = [
                d for d in _SELF_OR_MODERN_DISTRACTORS
                if d not in ('他', '她', '它', '他们', '她们', '它们')
            ]
            if any(d in lowered for d in stripped_distractors):
                return False
            return True
        if any(d in lowered for d in _SELF_OR_MODERN_DISTRACTORS):
            return False
        return True

    @classmethod
    def _is_continuation_query(cls, current: str, has_strong_direct: bool,
                               story_mode: bool = False) -> bool:
        """判断整句是否需要向多轮前文寻求话题继承。

        防止在剧情模式或普通对话中，用户已经明确切换到新话题（命中了强关键词且无桥接词），
        却仍把前几轮聊过的旧设定硬塞进当前轮。
        """
        text = str(current or '').strip().lower()
        if not text:
            return False
        has_bridge = any(cue in text for cue in _BRIDGE_CUES)
        has_cue = any(cue in text for cue in _FOLLOWUP_CUES)
        has_anaphora = any(
            p in text for p in ('他', '她', '它', '那', '这', '后来', '然后', '为什么', '为啥', '还有')
        )
        if not has_anaphora and not any(a in text for a in _PERSONA_ANCHORS):
            if any(d in text for d in _SELF_OR_MODERN_DISTRACTORS):
                return False
        if has_strong_direct:
            # 当前句已有明确的新话题强关键词：只有同时带跨话题桥接/承接信号（如“那猎人和它有什么区别”）才继承前文
            return has_bridge or text.startswith(('那', '所以', '而且', '不过', '那如果', '如果'))
        if has_bridge or has_cue:
            return True
        if len(text) <= 24 and text.endswith(('？', '?', '呢', '吗', '呀', '嘛', '吧')):
            return True
        if story_mode and len(text) <= 28:
            return True
        return False

    def _collect_recent_anchors(self, recent_lines: list[str], visible_docs: list[_Entry],
                                by_id: dict[str, _Entry], stage: int | None,
                                scope: str) -> dict[str, tuple[int, list[str]]]:
        """沿最近消息从近到远聚焦最近一个话题锚点簇，阻断更早轮次的过期话题串味。

        返回 {entry_id: (dist, matched_keywords)}。一旦在距离 dist 发现了明确话题锚点，
        只额外吸收与之相邻（dist + 1）或存在图谱连接（links）的前文条目，不再无差别翻旧账。
        """
        per_turn: list[tuple[int, dict[str, list[str]]]] = []
        for dist, r_line in enumerate(reversed(recent_lines)):
            turn_hits: dict[str, list[str]] = {}
            for entry in visible_docs:
                r_strong, r_weak = self._match_keywords(r_line, entry)
                r_ambient_ok = self._allow_ambient(r_line, entry) if r_weak else False
                r_eff_weak = r_weak if (r_strong or r_ambient_ok) else []
                if r_strong or r_eff_weak:
                    turn_hits[entry.id] = r_strong + r_eff_weak
            if turn_hits:
                per_turn.append((dist, turn_hits))

        if not per_turn:
            return {}

        anchor_dist, latest_hits = per_turn[0]
        focused: dict[str, tuple[int, list[str]]] = {
            eid: (anchor_dist, kws) for eid, kws in latest_hits.items()
        }
        focus_cluster: set[str] = set(latest_hits.keys())
        for eid in list(latest_hits.keys()):
            ent = by_id.get(eid)
            if ent:
                focus_cluster.update(ent.links)
        for cand in visible_docs:
            if any(link_id in focus_cluster for link_id in cand.links):
                focus_cluster.add(cand.id)

        for dist, turn_hits in per_turn[1:]:
            for eid, kws in turn_hits.items():
                if eid in focused:
                    continue
                # 相邻一轮（一问一答对）或属于同一设定图谱簇的条目允许保留；跨话题旧轮次直接截断
                if dist <= anchor_dist + 1 or eid in focus_cluster:
                    focused[eid] = (dist, kws)
        return focused

    @classmethod
    def _match_keywords(cls, text: str, entry: _Entry) -> tuple[list[str], list[str]]:
        """返回 (命中的强关键词列表, 命中的弱关键词列表)。

        匹配前会先遮罩 entry.excludes；对于单字或易碰撞短词，额外遮罩 _DEFAULT_COLLISIONS。
        """
        lowered = str(text or '').lower()
        if not lowered:
            return [], []

        masked = lowered
        for ex in entry.excludes:
            if ex and ex in masked:
                masked = masked.replace(ex, ' ')

        # 针对短词的碰撞防护副本：把“奇怪/上海/崇拜/充电”等固定搭配先抹掉
        collision_masked = masked
        for phrase in _DEFAULT_COLLISIONS:
            if phrase in collision_masked:
                collision_masked = collision_masked.replace(phrase, ' ')

        strong_hits: list[str] = []
        weak_hits: list[str] = []

        for kw in entry.keywords:
            if not kw:
                continue
            # 单字关键词或本身不在碰撞表里的短词，使用 collision_masked 检查
            target = collision_masked if (len(kw) <= 2 and kw not in _DEFAULT_COLLISIONS) else masked
            if kw in target:
                if kw in _AMBIENT_KEYWORDS:
                    weak_hits.append(kw)
                else:
                    strong_hits.append(kw)

        for kw in entry.weak_keywords:
            if not kw:
                continue
            target = collision_masked if (len(kw) <= 2 and kw not in _DEFAULT_COLLISIONS) else masked
            if kw in target and kw not in weak_hits:
                weak_hits.append(kw)

        return strong_hits, weak_hits

    @staticmethod
    def _is_followup(current: str, current_tokens: list[str], entry: _Entry) -> bool:
        """判断当前消息是否是对前文话题的追问或延续。"""
        text = str(current or '').strip().lower()
        if not text:
            return False
        # 若当前句转向了群友自己的三次元话题（含干扰词且无锚点/无指代词），不算追问
        has_anaphora = any(p in text for p in ('他', '她', '它', '那', '这', '后来', '然后', '为什么', '为啥'))
        if not has_anaphora and not any(a in text for a in _PERSONA_ANCHORS):
            if any(d in text for d in _SELF_OR_MODERN_DISTRACTORS):
                return False

        # 1. 含有明确的追问/指代提示词（如“那后来呢”“她平时喜欢干嘛”“为什么会这样”）
        if len(text) <= 36 and any(cue in text for cue in _FOLLOWUP_CUES):
            return True
        if len(text) <= 20 and text.endswith(('？', '?', '呢', '吗', '呀')):
            return True

        # 2. 当前句与条目正文存在非虚词的实词二元组重合
        core_tokens = [t for t in current_tokens if t not in _STOP_BIGRAMS and len(t) >= 2]
        if any(t in entry.term_freq for t in core_tokens):
            return True
        return False

    def always_entries(self, max_chars_per_entry: int = DEFAULT_MAX_CHARS_PER_ENTRY,
                       stage: int | None = None, scope: str = 'both') -> list[str]:
        """常驻条目的正文（不看关键词，每次注入）。"""
        with self._lock:
            docs = list(self._docs)
        out = []
        for entry in docs:
            if not entry.always or entry.topic:
                continue
            if not self._visible(entry, stage, scope):
                continue
            limit = entry.max_chars or max_chars_per_entry
            content = entry.content
            if limit and len(content) > limit:
                content = content[:limit] + '…'
            out.append(content)
        return out

    def topics(self, stage: int | None = None, scope: str = 'private') -> list[dict]:
        """主动谈资池：返回当前阶段/场景可用的 [{id, content}]。"""
        with self._lock:
            docs = list(self._docs)
        return [{'id': e.id, 'content': e.content} for e in docs
                if e.topic and self._visible(e, stage, scope)]

    def inspect_lookup(self, text: str, recent: list[str] | None = None,
                       max_entries: int | None = None,
                       max_chars_per_entry: int = DEFAULT_MAX_CHARS_PER_ENTRY,
                       stage: int | None = None, scope: str = 'both',
                       max_total_chars: int | None = None,
                       story_mode: bool = False) -> list[dict]:
        """带诊断信息的检索：返回命中的条目详情列表（供调试台与测试分析）。

        当 `story_mode=True`（剧情探索模式）时：
        1. 默认注入条数上限从 3 条提升至 6 条（总字数预算从 2400 提升至 4200）；
        2. 前文回溯窗口从 5 句扩至 8 句；
        3. 纯 BM25 覆盖率门槛放宽（0.6 → 0.36，或命中高稀有度核心设定词），便于探索式发问；
        4. `links` 关联图谱支持二跳扩散与更高联动权重，一次拉出完整的主题设定簇。
        """
        with self._lock:
            docs = list(self._docs)
            by_id = dict(self._by_id)
        if not docs:
            return []

        if max_entries is None:
            max_entries = STORY_MAX_ENTRIES if story_mode else DEFAULT_MAX_ENTRIES
        if max_total_chars is None:
            max_total_chars = STORY_MAX_TOTAL_CHARS if story_mode else DEFAULT_MAX_TOTAL_CHARS

        current = str(text or '').strip()
        current_tokens = tokenize(current)
        recent_window = 8 if story_mode else 5
        recent_lines = [
            str(r or '').strip() for r in list(recent or [])[-recent_window:] if str(r or '').strip()
        ]
        if not current_tokens and not recent_lines:
            return []

        visible_items: list[tuple[int, _Entry, list[str], list[str]]] = []
        has_strong_direct = False
        for index, entry in enumerate(docs):
            if entry.always or entry.topic:
                continue
            if not self._visible(entry, stage, scope):
                continue
            strong_hits, weak_hits = self._match_keywords(current, entry)
            ambient_ok = self._allow_ambient(current, entry) if weak_hits else False
            effective_weak = weak_hits if (strong_hits or ambient_ok) else []
            if strong_hits:
                has_strong_direct = True
            visible_items.append((index, entry, strong_hits, effective_weak))

        visible_docs = [entry for _, entry, _, _ in visible_items]
        allow_context = bool(recent_lines) and self._is_continuation_query(
            current, has_strong_direct, story_mode=story_mode
        )
        recent_anchors: dict[str, tuple[int, list[str]]] = {}
        context_tokens: list[str] = []
        if allow_context:
            recent_anchors = self._collect_recent_anchors(
                recent_lines, visible_docs, by_id, stage, scope
            )
            max_anchor_dist = max((d for d, _ in recent_anchors.values()), default=1)
            active_lines = list(reversed(recent_lines))[:max_anchor_dist + 1]
            for line in active_lines:
                context_tokens.extend(tokenize(line))

        scored_map: dict[str, dict] = {}
        for index, entry, strong_hits, effective_weak in visible_items:
            direct_hit = bool(strong_hits or effective_weak)
            context_hit = False
            matched_kws: list[str] = []
            source = 'bm25'
            tier = 3
            kw_bonus = 0.0

            if direct_hit:
                matched_kws = strong_hits + effective_weak
                base = KEYWORD_BONUS if strong_hits else WEAK_KEYWORD_BONUS
                extra_count = min(4.5, (len(matched_kws) - 1) * 1.5)
                max_len = max((len(k) for k in matched_kws), default=0)
                len_bonus = min(2.0, max_len * 0.25)
                kw_bonus = base + extra_count + len_bonus
                source = 'keyword' if strong_hits else 'weak_keyword'
                tier = 3
            elif allow_context and entry.id in recent_anchors:
                if (story_mode or self._is_followup(current, current_tokens, entry)) and self._allow_ambient(
                    current, entry, allow_pronoun_followup=True
                ):
                    dist, anchor_kws = recent_anchors[entry.id]
                    context_hit = True
                    matched_kws = anchor_kws
                    kw_bonus = CONTEXT_KEYWORD_BONUS * (0.85 ** dist)
                    source = 'context_keyword'
                    tier = 2

            bm25_cur = self._bm25(current_tokens, entry)
            bm25_ctx = (
                self._bm25(context_tokens, entry) * CONTEXT_BM25_WEIGHT
                if (allow_context and context_tokens) else 0.0
            )
            score = bm25_cur + bm25_ctx + kw_bonus

            if not direct_hit and not context_hit:
                # 没命中任何关键词：必须过当前句的覆盖率闸门，挡住虚词巧合
                matched, coverage = self._coverage(current_tokens, entry)
                if story_mode:
                    core_hits = [
                        t for t in set(current_tokens)
                        if t in entry.term_freq and t not in _STOP_BIGRAMS
                    ]
                    max_idf = max((self._idf.get(t, 0.0) for t in core_hits), default=0.0)
                    passed = (
                        (matched >= MIN_MATCHED_TERMS and coverage >= STORY_MIN_COVERAGE)
                        or (len(core_hits) >= 1 and coverage >= 0.42 and max_idf >= STORY_RARE_IDF)
                    )
                    if not passed:
                        continue
                else:
                    if matched < MIN_MATCHED_TERMS or coverage < MIN_COVERAGE:
                        continue
                if not self._allow_ambient(current, entry):
                    continue
                tier = 3

            if score <= 0:
                continue

            # 高阶段解锁的专属设定在同话题竞争时略微优先于通用表层设定
            stage_tiebreak = (entry.min_stage or 0) * 0.25 + entry.priority
            total_score = score + stage_tiebreak

            scored_map[entry.id] = {
                'entry': entry,
                'index': index,
                'tier': tier,
                'score': total_score,
                'matched_keywords': matched_kws,
                'source': source,
                'direct_or_context': direct_hit or context_hit,
            }

        # 关联条目（links）联动：按相关性分层排序，绝不挤占当前句直接命中或追问继承条目
        if scored_map:
            base_link_bonus = STORY_LINK_BONUS if story_mode else LINK_BONUS
            primary_items = sorted(
                scored_map.values(),
                key=lambda x: (x['tier'], x['score'], -x['index']),
                reverse=True,
            )[:max_entries]
            hop1_added: list[tuple[_Entry, float]] = []
            for item in primary_items:
                if not item['direct_or_context']:
                    continue
                src_entry: _Entry = item['entry']
                parent_boost = min(1.5, float(item['score']) * 0.05)
                for link_pos, linked_id in enumerate(src_entry.links):
                    target = by_id.get(linked_id)
                    if target is None or target.always or target.topic:
                        continue
                    if not self._visible(target, stage, scope):
                        continue
                    if linked_id in scored_map:
                        existing = scored_map[linked_id]
                        existing['score'] += base_link_bonus * 0.35
                        tag = f'link:{src_entry.id}'
                        if existing['source'] == 'link' and tag not in existing['matched_keywords']:
                            existing['matched_keywords'].append(tag)
                    else:
                        try:
                            target_idx = docs.index(target)
                        except ValueError:
                            target_idx = 999
                        ctx_affinity = (
                            self._bm25(context_tokens, target) * 0.25
                            if (allow_context and context_tokens) else 0.0
                        )
                        order_bonus = max(0.0, 0.45 - link_pos * 0.1)
                        link_score = (
                            base_link_bonus
                            + self._bm25(current_tokens, target) * 1.15
                            + ctx_affinity
                            + parent_boost
                            + order_bonus
                            + (target.min_stage or 0) * 0.25
                            + target.priority
                        )
                        scored_map[linked_id] = {
                            'entry': target,
                            'index': target_idx,
                            'tier': 1,
                            'score': link_score,
                            'matched_keywords': [f'link:{src_entry.id}'],
                            'source': 'link',
                            'direct_or_context': False,
                        }
                        hop1_added.append((target, link_score))

            # 剧情探索模式下支持二跳关联扩展：候选全部计算相关性后择优补位，且排在一跳之后
            if story_mode and hop1_added:
                hop1_sorted = sorted(hop1_added, key=lambda x: x[1], reverse=True)
                for hop1_entry, hop1_score in hop1_sorted:
                    hop1_boost = min(1.0, hop1_score * 0.04)
                    for link_pos, linked_id in enumerate(hop1_entry.links):
                        target = by_id.get(linked_id)
                        if target is None or target.always or target.topic:
                            continue
                        if not self._visible(target, stage, scope):
                            continue
                        if linked_id in scored_map:
                            existing = scored_map[linked_id]
                            if existing['tier'] == 0:
                                existing['score'] += base_link_bonus * 0.2
                                tag = f'link2:{hop1_entry.id}'
                                if tag not in existing['matched_keywords']:
                                    existing['matched_keywords'].append(tag)
                            continue
                        try:
                            target_idx = docs.index(target)
                        except ValueError:
                            target_idx = 999
                        ctx_affinity = (
                            self._bm25(context_tokens, target) * 0.2
                            if (allow_context and context_tokens) else 0.0
                        )
                        order_bonus = max(0.0, 0.3 - link_pos * 0.08)
                        link_score = (
                            base_link_bonus * 0.65
                            + self._bm25(current_tokens, target) * 1.15
                            + ctx_affinity
                            + hop1_boost
                            + order_bonus
                            + (target.min_stage or 0) * 0.25
                            + target.priority
                        )
                        scored_map[linked_id] = {
                            'entry': target,
                            'index': target_idx,
                            'tier': 0,
                            'score': link_score,
                            'matched_keywords': [f'link2:{hop1_entry.id}'],
                            'source': 'link',
                            'direct_or_context': False,
                        }

        ranked = sorted(
            scored_map.values(),
            key=lambda x: (x['tier'], x['score'], -x['index']),
            reverse=True,
        )

        results: list[dict] = []
        used_chars = 0
        for item in ranked:
            if len(results) >= max_entries:
                break
            entry: _Entry = item['entry']
            limit = entry.max_chars or max_chars_per_entry
            content = entry.content
            if limit and len(content) > limit:
                content = content[:limit] + '…'
            if max_total_chars and results and (used_chars + len(content) > max_total_chars):
                continue
            used_chars += len(content)
            results.append({
                'id': entry.id,
                'score': round(float(item['score']), 3),
                'source': item['source'],
                'matched_keywords': item['matched_keywords'],
                'min_stage': entry.min_stage,
                'max_stage': entry.max_stage,
                'scope': entry.scope,
                'content': content,
            })
        return results

    def lookup(self, text: str, recent: list[str] | None = None,
               max_entries: int | None = None,
               max_chars_per_entry: int = DEFAULT_MAX_CHARS_PER_ENTRY,
               stage: int | None = None, scope: str = 'both',
               max_total_chars: int | None = None,
               story_mode: bool = False) -> list[str]:
        """返回和消息相关的世界书内容；都不相关时返回空列表（零成本）。"""
        items = self.inspect_lookup(
            text, recent=recent, max_entries=max_entries,
            max_chars_per_entry=max_chars_per_entry, stage=stage, scope=scope,
            max_total_chars=max_total_chars, story_mode=story_mode,
        )
        return [item['content'] for item in items]
