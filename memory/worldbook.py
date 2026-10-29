# -*- encoding: utf-8 -*-
"""世界书（Worldbook）模块。

设计目标（关键词才读取设定，减少消耗）：
- 世界书条目以 JSON 文件保存（data/worldbook.json）。
- 只有和用户消息**相关**的条目才注入 prompt。
- 开关由 config 的 worldbook_enabled 控制。
- 不与联网、搜索、RAG 耦合。

条目字段：
- keywords：关键词数组，命中任一即注入（子串匹配，全小写比较）。
- content：注入的正文，可多行（prompt_builder 会给续行加缩进）。
- enabled：false 的条目直接忽略。
- max_chars：可选，本条目的字数上限；不填用调用方的默认值。
- always：true 表示**常驻**——不看关键词，每次都注入（见下）。

关于 always（2026-09 新增）：
纯关键词检索有个绕不过去的短板——**没命中时模型不是"不知道细节"，
而是"完全不知道有这么回事"**，于是顺着印象编。常驻一小段压缩过的人物档案，
模型至少知道边界在哪，细节不够时会说"这个说来话长"而不是瞎编。
所以 always 条目要写得**短而全**（人物关系一句话一个），不是把详细设定搬过来。

关于排序（2026-09 改）：
以前是「按文件顺序取前 N 个命中的条目」——命中 4 条时丢掉的那条是随机的。
现在按 BM25 打分排序，命中关键词的额外加权。BM25 是**词面**匹配（中文按字符
二元组切），它解决的是排序和部分重合，**不解决语义**：「讲讲你以前的事」
这种没有任何字面重合的问法，BM25 一样捞不回来，那种要靠把说法补进 keywords。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from typing import Any

# 单条世界书内容的默认字数上限。详细设定（外观、常服、世界观）需要更大空间，
# 但也不能无限——命中的条目是直接进 prompt 的，上限就是最坏情况下的 token 开销。
DEFAULT_MAX_CHARS_PER_ENTRY = 800

# BM25 参数。中文条目长度差异很大（一条 100 字、一条 700 字），
# b 取小一点，减少对长条目的惩罚——长条目通常信息更多，不该被压。
BM25_K1 = 1.2
BM25_B = 0.5
# 命中 keywords 的额外加成。给得很大是**故意的**：关键词是人工维护的强信号，
# 必须稳压纯统计相似度。实测「狛枝是个什么样的人」里，日向条目靠「什么/样的」
# 这种虚词能刷到 9.35 分，比真正命中的狛枝条目还高——不加成就本末倒置了。
KEYWORD_BONUS = 12.0
# 纯 BM25（没命中关键词）时的额外闸门。只用绝对分数阈值不好使：
# 实测误命中在 2.6~3.9、真问题在 8.3，中间没有干净的切点，调高会把正例一起杀掉。
# 改用**覆盖率**——查询里的词有多大比例真的在条目里出现过：
#   「你是不是AI」  → 你是/是不/不是/是ai 四个全中，覆盖率 1.00 ✓
#   「这个样子不太好吧」→ 只中「这个/不太」，覆盖率 0.29 ✗
#   「狛枝是个什么样的人」→ 日向条目只中「什么/样的」，覆盖率 0.25 ✗
# 这个判据和条目长短、和查询长短都无关，比分数稳得多。
MIN_COVERAGE = 0.6
MIN_MATCHED_TERMS = 2

_CJK_RUN = re.compile(r'[\u4e00-\u9fff]+')
_ASCII_WORD = re.compile(r'[a-z0-9]+')


def _positive_int(value: Any) -> int:
    """把配置里的 max_chars 解析成正整数；非法/缺省一律返回 0（表示用默认值）。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


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


class _Entry:
    __slots__ = ('keywords', 'content', 'max_chars', 'always', 'term_freq', 'length')

    def __init__(self, keywords, content, max_chars, always):
        self.keywords = keywords
        self.content = content
        self.max_chars = max_chars
        self.always = always
        # 文档 = 关键词 + 正文。关键词也算进词频，但命中另有显式加成。
        tokens = tokenize(' '.join(keywords) + ' ' + content)
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
        self._idf: dict[str, float] = {}
        self._avg_len = 1.0
        self._default_idf = 1.0
        self.load()

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
                        keywords = item.get('keywords') or []
                        content = str(item.get('content', '')).strip()
                        always = bool(item.get('always'))
                        # 常驻条目允许没有 keywords
                        if (keywords or always) and content:
                            clean_keywords = [
                                str(k).strip().lower() for k in keywords if str(k).strip()
                            ]
                            entries.append(_Entry(
                                clean_keywords, content,
                                _positive_int(item.get('max_chars')), always,
                            ))
                            raw_entries.append({
                                'keywords': clean_keywords,
                                'content': content,
                                'max_chars': _positive_int(item.get('max_chars')),
                                'always': always,
                            })
        except (OSError, UnicodeError, json.JSONDecodeError):
            entries, raw_entries = [], []

        idf, avg_len, default_idf = self._build_idf(entries)
        with self._lock:
            self.entries = raw_entries
            self._docs = entries
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
        # 「在任何条目里都没出现过」的词的 idf —— 覆盖率分母必须用它来算。
        # 踩过的坑：一开始对未知词取 0，结果「这个样子不太好吧」里
        # 5 个没命中的词贡献了 0 分母，覆盖率被算成 1.0，虚词巧合照样放行。
        # 没出现过的词信息量最大，就该按最大 idf 计入分母。
        default_idf = math.log(1.0 + (total + 0.5) / 0.5)
        return idf, max(1.0, avg_len), default_idf

    def _bm25(self, query_tokens: list[str], entry: _Entry) -> float:
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

        覆盖率按 idf 加权——命中一个很稀有的词比命中一个到处都是的词更有意义；
        没出现过的词按 default_idf 计入分母（见 _build_idf 里的说明）。
        """
        unique = set(query_tokens)
        if not unique:
            return 0, 0.0
        total = sum(self._idf.get(token, self._default_idf) for token in unique)
        matched = [token for token in unique if token in entry.term_freq]
        if not matched:
            return 0, 0.0
        hit_mass = sum(self._idf.get(token, self._default_idf) for token in matched)
        return len(matched), hit_mass / total

    def always_entries(self, max_chars_per_entry: int = DEFAULT_MAX_CHARS_PER_ENTRY) -> list[str]:
        """常驻条目的正文（不看关键词，每次注入）。"""
        with self._lock:
            docs = list(self._docs)
        out = []
        for entry in docs:
            if not entry.always:
                continue
            limit = entry.max_chars or max_chars_per_entry
            content = entry.content
            if limit and len(content) > limit:
                content = content[:limit] + '…'
            out.append(content)
        return out

    def lookup(self, text: str, recent: list[str] | None = None,
               max_entries: int = 3,
               max_chars_per_entry: int = DEFAULT_MAX_CHARS_PER_ENTRY) -> list[str]:
        """返回和消息相关的世界书内容；都不相关时返回空列表（零成本）。

        recent 是最近几条消息（可选）：群聊里「那她后来呢？」这种追问本身不含关键词，
        但前文含——把它们按低权重并进查询，能把这部分捞回来。
        """
        with self._lock:
            docs = list(self._docs)
        if not docs:
            return []

        current = str(text or '')
        # 前文只作辅助：权重压低，避免前文的话题把当前问题盖掉
        query_tokens = tokenize(current)
        if recent:
            context_tokens: list[str] = []
            for line in list(recent)[-5:]:
                context_tokens.extend(tokenize(line))
            query_tokens = query_tokens * 3 + context_tokens
        if not query_tokens:
            return []

        lowered = current.lower()
        scored: list[tuple[float, int, _Entry]] = []
        for index, entry in enumerate(docs):
            if entry.always:
                continue          # 常驻条目走 always_entries，不参与检索
            hit = any(kw in lowered for kw in entry.keywords if kw)
            score = self._bm25(query_tokens, entry)
            if hit:
                score += KEYWORD_BONUS
            else:
                # 没命中关键词：必须过覆盖率闸门，挡住「这个样子」这类虚词巧合
                matched, coverage = self._coverage(query_tokens, entry)
                if matched < MIN_MATCHED_TERMS or coverage < MIN_COVERAGE:
                    continue
            if score <= 0:
                continue
            scored.append((score, -index, entry))

        # 分数高的在前；同分时保持文件顺序（-index 让小的 index 排前面）
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        hits = []
        for _score, _order, entry in scored[:max_entries]:
            limit = entry.max_chars or max_chars_per_entry
            content = entry.content
            if limit and len(content) > limit:
                content = content[:limit] + '…'
            hits.append(content)
        return hits
