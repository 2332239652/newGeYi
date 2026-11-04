# -*- encoding: utf-8 -*-
"""OlivOS 事件入口：只做转发，业务全部在 runtime。

命名空间：newGeYi
消息模式：olivos_string（OP 码）
"""

import logging

from .runtime.app import Runtime

_logger = logging.getLogger('newGeYi.main')

# 插件加载时就要把 Runtime 建起来（init 之后才能拿到 Proc），但**不能让异常冒出去**：
# 以前这里是裸 `_runtime = Runtime()`，配置/数据库/依赖任何一处抛错，OlivOS 那边只会看到
# 「插件加载失败」，日志里连一行原因都没有。现在兜住并留下完整栈，同时给一个空壳，
# 让事件回调不至于 AttributeError 连环炸。
try:
    _runtime = Runtime()
    _load_error = ''
except Exception as _exc:  # noqa: BLE001 - 加载期任何异常都不能让插件静默消失
    _logger.exception('插件初始化失败（配置/数据库/依赖）')
    _load_error = '%s: %s' % (type(_exc).__name__, _exc)

    class _BrokenRuntime:  # pragma: no cover - 只在初始化失败时存在
        config: dict = {}

        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                _logger.error('插件初始化失败（%s），忽略事件 %s', _load_error, name)
                return None
            return _noop

    _runtime = _BrokenRuntime()


class Event(object):
    @staticmethod
    def init(plugin_event, Proc):
        _runtime.init(Proc)

    @staticmethod
    def init_after(plugin_event, Proc):
        _runtime.init_after(Proc)

    @staticmethod
    def group_message(plugin_event, Proc):
        _runtime.handle_message(plugin_event, Proc)

    @staticmethod
    def private_message(plugin_event, Proc):
        _runtime.handle_message(plugin_event, Proc)

    @staticmethod
    def poke(plugin_event, Proc):
        _runtime.handle_poke(plugin_event, Proc)

    @staticmethod
    def menu(plugin_event, Proc):
        _runtime.handle_menu(plugin_event, Proc)

    @staticmethod
    def save(plugin_event, Proc):
        _runtime.save(Proc)
