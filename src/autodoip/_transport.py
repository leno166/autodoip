"""
@文件: _transport.py
@作者: 雷小鸥
@日期: 2026/6/3
@许可: MIT License
@描述: DoIp 传输层 — Sock + Protocol + Endpoint
       零外部依赖（除 stdlib socket + threading）
"""
import threading
import socket
import time
from typing import Literal, Iterator
import logging

from ._frame import recv_frame
from ._errors import ProtocolError
from ._config import Config

logger = logging.getLogger(__name__)


# ================== _Sock ==================

class _Sock:
    """单个 socket 封装，send/recv/close"""

    def __init__(self, sock: socket.socket,
                 byte_order: Literal['little', 'big'],
                 p6: float):
        self._sock = sock
        self._byte_order: Literal['little', 'big'] = byte_order
        self._p6 = p6
        self._sock.settimeout(p6)

    def send(self, msg: bytes) -> None:
        self._sock.sendall(msg)

    def recv(self) -> bytes:
        return recv_frame(self._sock, self._byte_order)

    def close(self) -> None:
        self._sock.close()


# ================== _Protocol ==================

class _Protocol:
    """DoIp 帧编解码，无状态。

    Raises:
        ProtocolError: 帧格式校验失败 — 版本反码、Payload Type、长度、地址不匹配。
    """

    def __init__(self, version: int, msg_type: int,
                 byte_order: Literal['little', 'big']):
        self._version = version
        self._msg_type = msg_type
        self._byte_order: Literal['little', 'big'] = byte_order

    def encode(self, payload: bytes, tester: int, ecu: int) -> bytes:
        inner = (
                tester.to_bytes(2, self._byte_order) +
                ecu.to_bytes(2, self._byte_order) +
                payload
        )
        header = (
                self._version.to_bytes(1, self._byte_order) +
                (~self._version & 0xFF).to_bytes(1, self._byte_order) +
                self._msg_type.to_bytes(2, self._byte_order) +
                len(inner).to_bytes(4, self._byte_order)
        )
        return header + inner

    def decode(self, frame: bytes, tester: int, ecu: int) -> bytes:
        if len(frame) < 12:
            raise ProtocolError(
                f"响应帧太短: {len(frame)} 字节 (至少需要 12)，"
                f"帧：{frame.hex(' ')}"
            )

        version = frame[0]
        inverse_version = frame[1]
        if inverse_version != (~version & 0xFF):
            raise ProtocolError(
                f"版本反码错误: version=0x{version:02X}, "
                f"inverse=0x{inverse_version:02X}，帧：{frame.hex(' ')}"
            )

        payload_type = int.from_bytes(frame[2:4], self._byte_order)
        if payload_type != 0x8001:
            raise ProtocolError(
                f"不支持的 Payload Type: 0x{payload_type:04X}，"
                f"帧：{frame.hex(' ')}"
            )

        payload_length = int.from_bytes(frame[4:8], self._byte_order)
        if payload_length != len(frame) - 8:
            raise ProtocolError(
                f"载荷长度不匹配: 头部 {payload_length}, "
                f"实际 {len(frame) - 8}，帧：{frame.hex(' ')}"
            )

        src_addr = int.from_bytes(frame[8:10], self._byte_order)
        if src_addr != ecu:
            raise ProtocolError(
                f"源地址不匹配: 0x{src_addr:04X}，"
                f"帧：{frame.hex(' ')}"
            )

        dst_addr = int.from_bytes(frame[10:12], self._byte_order)
        if dst_addr != tester:
            raise ProtocolError(
                f"目标地址不匹配: 0x{dst_addr:04X}，"
                f"帧：{frame.hex(' ')}"
            )

        return frame[12:]


# ================== Endpoint ==================

class Endpoint:
    """DoIp 端点：整合 Server Socket + 连接表 + Protocol + Lock + 自动重连。

    Raises:
        ValueError: select 传入未知 ECU 地址。
        ProtocolError: ecu 未设置时调用 send。
        TimeoutError: 通信失败且重连后仍失败。
    """

    def __init__(self,
                 ip: str,
                 ecus: dict[int, tuple[str, int]],
                 port: int = 13400,
                 tester: int = 0x0E80,
                 config: Config | None = None):
        self._config = config or Config()

        # 身份参数
        self._ip = ip
        self._port = port
        self._tester = tester
        self._ecus: dict[int, tuple[str, int]] = ecus.copy()

        # 协议
        self._protocol = _Protocol(self._config.version,
                                   self._config.msg_type,
                                   self._config.byte_order)

        # 连接表 — 预建，sock 初始 None
        self._socks: dict[int, _Sock | None] = {
            addr: None for addr in self._ecus
        }
        self._current: int | None = None
        self._lock = threading.RLock()

        # server socket
        self._server: socket.socket | None = None

    # --- 连接表 ---

    @property
    def connections(self) -> dict[int, tuple[str, int, bool]]:
        """返回全部 ECU 及其连接状态。
        {addr: (ip, port, connected), ...}
        """
        with self._lock:
            return {
                addr: (ip, port, self._socks[addr] is not None)
                for addr, (ip, port) in self._ecus.items()
            }

    @property
    def current(self) -> int | None:
        """当前选中的 ECU 逻辑地址。"""
        with self._lock:
            return self._current

    # --- 生命周期 ---

    def start(self) -> None:
        """启动 DoIp 监听，accept 等待 ECU 连接。幂等，重复调用无副作用。"""
        with self._lock:
            if self._server is not None:
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(self._config.accept_timeout)
            sock.bind((self._ip, self._port))
            sock.listen(self._config.listen_count)
            self._server = sock
            logger.info("DoIp 服务启动，监听 %s:%d，backlog %d",
                        self._ip, self._port, self._config.listen_count)
            self._accept4connect()

    def stop(self) -> None:
        """关闭所有连接和 server socket。"""
        with self._lock:
            for addr, s in self._socks.items():
                if s is not None:
                    try:
                        s.close()
                        logger.debug("关闭 ECU 0x%04X 的 socket", addr)
                    except Exception as e:
                        logger.error("关闭 ECU 0x%04X 的 socket 时出错: %s",
                                     addr, e, exc_info=True)
            self._socks = {addr: None for addr in self._ecus}

            if self._server:
                try:
                    self._server.close()
                    logger.debug("关闭服务端 socket")
                except Exception as e:
                    logger.error("关闭服务端 socket 时出错: %s", e, exc_info=True)
                self._server = None

            self._current = None
            logger.info("所有 socket 已关闭")

    def select(self, addr: int) -> bool:
        """切换到指定逻辑地址的 ECU。
        成功返回 True；ECU 未连接返回 False 且不改变当前选中。
        """
        with self._lock:
            if addr not in self._ecus:
                raise ValueError(f"未知 ECU 逻辑地址: 0x{addr:04X}")
            if self._socks[addr] is None:
                logger.warning("ECU 0x%04X 尚未连接，保持当前选中 ECU", addr)
                return False
            self._current = addr
            return True

    # --- 收发 ---

    def conversation(self, payload: bytes) -> Iterator[bytes]:
        """发送 UDS 载荷，返回响应 bytes。
        通信失败（含 sock 为 None）→ _reconnect 抢救一次；
        抢救后重发仍失败 → 清空 sock 并抛异常。
        """
        with self._lock:
            if self._current is None:
                raise ProtocolError('DoIp: 没有设置 ecu 逻辑地址')

            ecu = self._current
            frame = self._protocol.encode(payload, self._tester, ecu)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('TX DoIp: %s', frame.hex(' '))
            sock = self._socks[ecu]

            # --- send（一次，带抢救） ---
            try:
                sock.send(frame)
            except (ConnectionError, OSError, AttributeError) as e:
                logger.warning('DoIp send 失败，触发重连: %s', e)
                sock = self._reconnect(ecu)
                try:
                    sock.send(frame)
                except (ConnectionError, OSError, AttributeError):
                    self._socks[ecu] = None
                    logger.error('重连后 send 仍失败，清空连接', exc_info=True)
                    raise ConnectionError(f'ECU 0x{ecu:04X} 连接断开')

            # --- recv 循环，p6_star 为总超时 ---
            deadline = time.monotonic() + self._config.p6_star_timeout
            first = True
            while True:
                if time.monotonic() >= deadline:
                    return

                try:
                    response = sock.recv()
                except TimeoutError:
                    if first:
                        return
                    continue
                except (ConnectionError, OSError, AttributeError) as e:
                    logger.warning('DoIp recv 失败，清空连接: %s', e)
                    self._socks[ecu] = sock = None
                    return

                first = False
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug('RX DoIp: %s', response.hex(' '))
                yield self._protocol.decode(response, self._tester, ecu)

    # --- 内部 ---

    def _accept4connect(self) -> None:
        """
        单次 accept 循环，获取初始连接。必定超时返回
        按 ecus 表匹配 IP：匹配成功填入 sock，不在表中则 warn + close。
        """
        if not self._server:
            raise RuntimeError("服务未启动")

        logger.debug('accept 启动')
        pending = []
        while True:
            try:
                sock, addr = self._server.accept()
                logger.debug('accept | addr: %s', addr)
                src_ip, src_port = addr
                pending.append((sock, src_ip, src_port))
            except TimeoutError:
                logger.debug('accept 超时退出，共收集 %d 条连接', len(pending))

                if len(pending) == 0:
                    raise RuntimeError('没有任何连接端口，直接退出')
                break

        # 阶段 2: 处理（原逻辑不变）
        for sock, src_ip, src_port in pending:

            matched = next(
                (a for a, (e_ip, e_port) in self._ecus.items()
                 if e_ip == src_ip and (e_port == 0 or e_port == src_port)),
                None
            )

            if matched is None:
                logger.warning("收到非预期连接 %s:%d，不在 ECU 表中，已关闭", src_ip, src_port)
                sock.close()
                continue

            old = self._socks[matched]
            if old is not None:
                old.close()
                logger.info("ECU 0x%04X 关闭已有连接", matched)

            self._socks[matched] = _Sock(sock, self._config.byte_order,
                                         p6=self._config.p6_timeout)
            logger.info("ECU 已连接 0x%04X @ %s:%d", matched, src_ip, src_port)

    def _reconnect(self, addr: int) -> _Sock:
        """重连指定 ECU。调 _accept4connect 循环 accept 填表，
        结束后检查目标是否已连接。"""
        old = self._socks[addr]
        if old is not None:
            old.close()
        self._socks[addr] = None
        self._accept4connect()
        if self._socks[addr] is None:
            raise TimeoutError(f"ECU 0x{addr:04X} 重连超时")
        return self._socks[addr]