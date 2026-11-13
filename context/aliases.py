# -*- encoding: utf-8 -*-
"""用户自定义称呼：歌伊怎么叫这个人。

为什么要有：QQ 昵称经常是颜文字、ID 或乱码（`AAA平板批发商`、`.mrrp`），
提示词里 `昵称(QQ号)：内容` 那么叫出来既别扭，模型也容易把昵称当成人设的一部分。
让用户自己给一个称呼（全局一个，按 QQ 号存），她照着叫就行。

为什么**不**做进配置或数据库新表：这是一人一条的小偏好，`settings` 的 KV 正好够用
（键 `alias:<QQ号>`），不新增表、不需要迁移，`settings_with_prefix` 也能直接列出来。

安全考虑（都被 `sanitize()` 挡住）：
- 标签形状是 `昵称(QQ号)：内容`，所以称呼里不能有括号/冒号/方括号——否则群友可以
  用一个称呼伪造出「另一个人」的发言；
- 不能纯数字（会和 QQ 号混在一起）、不能叫「主人/骰主」（那是权限身份）；
- 不能拿「歌伊」当称呼（会让她把自己和对方搞混）。
"""

from __future__ import annotations

import re

ALIAS_PREFIX = 'alias:'
MAX_CHARS = 12

# 这些词直接对应"谁在说话/谁能指挥她"，拿来做称呼会让模型按错身份说话
RESERVED = frozenset({
    '歌伊', '主人', '骰主', '系统', '管理员', '官方', '所有人', '全体成员',
    '你', '我', '他', '她', '对方', '用户', 'AI',
})

# 标签结构字符：出现在称呼里就能伪造/断开 `昵称(QQ号)：内容`
_FORBIDDEN = frozenset('[]【】()（）<>《》{}「」『』:：@\r\n\t"\'`|')
_WS_RE = re.compile(r'\s+')


def sanitize(raw) -> tuple[str, str]:
    """校验并归一化称呼：返回 (称呼, 错误说明)；合法时错误说明为空串。"""
    text = _WS_RE.sub(' ', str(raw or '')).strip()
    if not text:
        return '', '要我怎么称呼你呀？例如「叫我 小明」。'
    if len(text) > MAX_CHARS:
        return '', '称呼最多 %d 个字（现在 %d 个），短一点更好叫。' % (MAX_CHARS, len(text))
    bad = sorted({ch for ch in text if ch in _FORBIDDEN})
    if bad:
        return '', '称呼里不能有这些符号：%s' % ' '.join(bad)
    if text.isdigit():
        return '', '纯数字容易被当成 QQ 号，换一个吧。'
    if text in RESERVED:
        return '', '「%s」这个称呼会让我搞不清你是谁，换一个吧。' % text
    if '歌伊' in text:
        return '', '别拿我的名字当称呼啦，换一个吧。'
    return text, ''


def get(store, user_id) -> str:
    """这个 QQ 号的自定义称呼；没设过返回空串。"""
    try:
        return str(store.get_setting(ALIAS_PREFIX + str(user_id)) or '').strip()
    except Exception:  # noqa: BLE001 - 读不到就当没设过，不能挡住出话
        return ''


def set_alias(store, user_id, alias) -> None:
    """写入/清除（None 或空串 = 清除，回到 QQ 昵称）。"""
    try:
        store.set_setting(ALIAS_PREFIX + str(user_id), str(alias or '').strip())
    except Exception:  # noqa: BLE001 - 写失败只影响这个偏好
        pass


def display(store, user_id, fallback) -> str:
    """取「该显示的名字」：自定义称呼优先，否则用传入的 QQ 昵称。"""
    return get(store, user_id) or str(fallback or '')


def listing(store) -> list[tuple[str, str]]:
    """[(QQ号, 称呼)]，空的（清除过的）跳过。"""
    items: list[tuple[str, str]] = []
    try:
        rows = store.settings_with_prefix(ALIAS_PREFIX)
    except Exception:  # noqa: BLE001
        return items
    for key, value in rows:
        uid = str(key)[len(ALIAS_PREFIX):]
        alias = str(value or '').strip()
        if uid and alias:
            items.append((uid, alias))
    return items
