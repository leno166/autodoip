"""
@文件: _util.py
@作者: 雷小鸥
@日期: 2026/6/3
@许可: MIT License
@描述: 类型转换工具
"""
from typing import Literal


def to_bytes(value: bytes | bytearray | str | int | None,
             byte_order: Literal['little', 'big']) -> bytes:
    """统一类型 → bytes。byte_order 必须显式传入，不设默认值。

    Raises:
        TypeError: 传入不支持的类型。
    """
    if value is None:
        return b''

    if isinstance(value, (bytes, bytearray)):
        return bytes(value)

    if isinstance(value, str):
        cleaned = value.replace(' ', '').replace('0x', '').replace('0X', '')
        if len(cleaned) % 2:
            cleaned = '0' + cleaned
        return bytes.fromhex(cleaned)

    if isinstance(value, int):
        length = (value.bit_length() + 7) // 8 or 1
        return value.to_bytes(length, byte_order)

    raise TypeError(f"Unsupported type: {type(value)}")