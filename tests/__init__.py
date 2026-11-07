# -*- encoding: utf-8 -*-
"""newGeYi 离线回归测试（不联网、不碰线上状态目录）。

## 测试临时目录的清收（2026-11-07）

每个测试模块开头都是 `os.environ['GEYI_DATA_DIR'] = tempfile.mkdtemp(prefix='geyi_xxx_')`，
但**没有任何地方删它**。本机实测：跑了几十轮全量之后，`%TEMP%` 里堆了
**9961 个 geyi_* 目录、1.7 GB**，C 盘只剩 3.3 GB，随后 sqlite 开始报
`disk I/O error` / `unable to open database file` —— 表现是「测试莫名其妙红了」，
而且报错点全在 storage 层，看着像代码坏了，实际是盘满了。

所以在这里统一收口：import 本包时记一份「跑之前就存在」的快照，
进程退出时把**本次新增**的 geyi_* 目录删掉。只删新增的，
别人（或上一次没跑完的）留下的不动，避免误删正在用的状态目录。

想留现场排查就把 `GEYI_KEEP_TEST_DATA=1` 设上。
"""

from __future__ import annotations

import atexit
import glob
import os
import shutil
import tempfile

_PREFIX = 'geyi_*'


def _existing() -> set:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), _PREFIX)))


_BEFORE = _existing()


def _cleanup() -> None:
    if str(os.environ.get('GEYI_KEEP_TEST_DATA', '')).strip() not in ('', '0'):
        return
    for path in _existing() - _BEFORE:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup)
