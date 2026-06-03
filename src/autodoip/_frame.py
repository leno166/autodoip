"""
@文件: _frame.py
@作者: 雷小鸥
@日期: 2026/6/3
@许可: MIT License
@描述: DoIP 帧级 IO — 精确收取 + 完整帧收取
"""
import socket
from typing import Literal


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """精确收取 size 字节。

    Raises:
        ConnectionError: 连接在收齐数据前关闭。
    """
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError('连接已关闭')
        data.extend(chunk)
    return bytes(data)


def recv_frame(sock: socket.socket,
               byte_order: Literal['little', 'big'] = 'big') -> bytes:
    """收取完整 DoIP 帧（8 字节头 + N 字节载荷）。"""
    header = recv_exact(sock, 8)
    payload_length = int.from_bytes(header[4:8], byte_order)
    payload = recv_exact(sock, payload_length)
    return header + payload