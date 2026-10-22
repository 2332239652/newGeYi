# -*- encoding: utf-8 -*-
"""OlivOS 事件入口：只做转发，业务全部在 runtime。

命名空间：newGeYi
消息模式：olivos_string（OP 码）
"""

from .runtime.app import Runtime

_runtime = Runtime()


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