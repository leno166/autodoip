"""
@文件: _errors.py
@作者: 雷小鸥
@日期: 2026/6/3
@许可: MIT License
@描述: DoIP 协议异常
"""


class ProtocolError(Exception):
    """DoIP 协议层错误 — 帧格式校验失败（版本反码、Payload Type、
    载荷长度、源/目标地址不匹配等）"""