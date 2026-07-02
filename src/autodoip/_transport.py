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
from ._errors import ProtocolError, ReconnectionError
from ._config import Config

logger = logging.getLogger(__name__)


# ================== _Sock ==================

class _Sock:
    """单个 socket 封装，send/recv。资源由 CPython GC 回收。"""

    def __init__(self, sock: socket.socket,
                 byte_order: Literal['little', 'big'],
                 p6: float):
        self._sock = sock
        self._byte_order: Literal['little', 'big'] = byte_order
        self._sock.settimeout(p6)

    def __del__(self) -> None:
        self._sock.close()

    def send(self, msg: bytes) -> None:
        self._sock.sendall(msg)

    def recv(self) -> bytes:
        return recv_frame(self._sock, self._byte_order)


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
    """DoIp 端点：整合 Server Socket + 连接表 + Protocol + 双锁 + 自动重连。
    资源由 CPython GC 回收，仅 _Sock.__del__ 关闭底层 socket。

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
        self._state_lock = threading.Lock()
        self._session_lock = threading.Lock()

        # server socket
        self._server: socket.socket | None = None

    # --- 连接表 ---

    @property
    def connections(self) -> dict[int, tuple[str, int, bool]]:
        """返回全部 ECU 及其连接状态。
        {addr: (ip, port, connected), ...}
        """
        with self._state_lock:
            return {
                addr: (ip, port, self._socks[addr] is not None)
                for addr, (ip, port) in self._ecus.items()
            }

    @property
    def current(self) -> int | None:
        """当前选中的 ECU 逻辑地址。"""
        with self._state_lock:
            return self._current

    # --- 生命周期 ---

    def start(self) -> None:
        """启动 DoIp 监听，accept 等待 ECU 连接。幂等，重复调用无副作用。

        Raises:
            RuntimeError: 另一个操作正在进行中。
        """
        if not self._session_lock.acquire(blocking=False):
            raise RuntimeError("另一个操作正在进行中，无法启动")
        try:
            with self._state_lock:
                if self._server is not None:
                    return

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(self._config.accept_timeout)
            sock.bind((self._ip, self._port))
            sock.listen(self._config.listen_count)

            with self._state_lock:
                self._server = sock

            logger.info("DoIp 服务启动，监听 %s:%d，backlog %d", self._ip, self._port, self._config.listen_count)
            self._accept4connect()
        finally:
            self._session_lock.release()

    def select(self, addr: int) -> bool:
        """切换到指定逻辑地址的 ECU。
        成功返回 True；ECU 未连接或另一个操作正在进行中返回 False 且不改变当前选中。

        Raises:
            ValueError: addr 不在 ecus 表中。
        """
        if not self._session_lock.acquire(blocking=False):
            return False
        try:
            with self._state_lock:
                ecus = self._ecus
                socks = self._socks

            if addr not in ecus:
                raise ValueError(f"未知 ECU 逻辑地址: 0x{addr:04X}")
            if socks[addr] is None:
                logger.warning("ECU 0x%04X 尚未连接，保持当前选中 ECU", addr)
                return False

            with self._state_lock:
                self._current = addr

            return True
        finally:
            self._session_lock.release()

    # --- 收发 ---

    def conversation(self, payload: bytes) -> Iterator[bytes]:
        """发送 UDS 载荷，返回响应 bytes。
        通信失败时自动重连：重连成功抛出 ReconnectionError 通知上层重新同步状态；
        重连失败抛出 ConnectionError。

        Raises:
            RuntimeError: 另一个操作正在进行中。
            ReconnectionError: 连接断开后重连成功，上层需重新同步状态后重试。
            ConnectionError: 连接断开且重连失败。
        """
        if not self._session_lock.acquire(blocking=False):
            raise RuntimeError("另一个操作正在进行中，无法发送")
        try:
            # 读取状态（持 state_lock，微秒）
            with self._state_lock:
                if self._current is None:
                    raise ProtocolError('DoIp: 没有设置 ecu 逻辑地址')
                ecu = self._current
                frame = self._protocol.encode(payload, self._tester, ecu)
                sock = self._socks[ecu]

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('TX DoIp: %s', frame.hex(' '))

            # --- send ---
            try:
                sock.send(frame)
            except (ConnectionError, OSError, AttributeError) as e:
                logger.warning('DoIp send 失败，触发重连: %s', e)
                try:
                    self._reconnect(ecu)
                except Exception:
                    raise ConnectionError(
                        f'ECU 0x{ecu:04X} 连接断开且重连失败'
                    ) from e
                raise ReconnectionError(
                    f'ECU 0x{ecu:04X} 连接断开后已重连，请重新同步状态后重试'
                ) from e

            # --- 首帧（超时 = 无响应，直接返回） ---
            deadline = time.monotonic() + self._config.p6_star_timeout
            try:
                response = sock.recv()
            except TimeoutError:
                logger.debug('首帧无响应')
                return
            except (ConnectionError, OSError, AttributeError) as e:
                logger.warning('DoIp recv 失败，触发重连: %s', e)
                try:
                    self._reconnect(ecu)
                except Exception:
                    raise ConnectionError(
                        f'ECU 0x{ecu:04X} 连接断开且重连失败'
                    ) from e
                raise ReconnectionError(
                    f'ECU 0x{ecu:04X} 连接断开后已重连，请重新同步状态后重试'
                ) from e

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('RX DoIp: %s', response.hex(' '))
            yield self._protocol.decode(response, self._tester, ecu)

            # --- 后续帧（超时继续等，直到 deadline） ---
            while time.monotonic() < deadline:
                try:
                    response = sock.recv()
                except TimeoutError:
                    continue
                except (ConnectionError, OSError, AttributeError) as e:
                    logger.warning('DoIp recv 失败，触发重连: %s', e)
                    try:
                        self._reconnect(ecu)
                    except Exception:
                        raise ConnectionError(
                            f'ECU 0x{ecu:04X} 连接断开且重连失败'
                        ) from e
                    raise ReconnectionError(
                        f'ECU 0x{ecu:04X} 连接断开后已重连，请重新同步状态后重试'
                    ) from e

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug('RX DoIp: %s', response.hex(' '))
                yield self._protocol.decode(response, self._tester, ecu)
        finally:
            self._session_lock.release()

    # --- 内部 ---

    def _accept4connect(self) -> None:
        """
        单次 accept 循环，获取初始连接。必定超时返回
        按 ecus 表匹配 IP：匹配成功填入 sock，不在表中则丢弃（GC 回收）。

        调用方保证持有 _session_lock。
        """
        with self._state_lock:
            server = self._server

        if not server:
            raise RuntimeError("服务未启动")

        logger.debug('accept 启动')
        pending = []
        while True:
            try:
                sock, addr = server.accept()
                logger.debug('accept | addr: %s', addr)
                src_ip, src_port = addr
                pending.append((sock, src_ip, src_port))
            except TimeoutError:
                logger.debug('accept 超时退出，共收集 %d 条连接', len(pending))
                break
            except OSError:
                logger.debug('accept 中断（server 已关闭）')
                break

        if not pending:
            raise RuntimeError('没有任何连接端口，直接退出')

        # 阶段 2: 快照（持 state_lock，微秒）
        with self._state_lock:
            ecus_items = list(self._ecus.items())
            current_socks = self._socks.copy()
            byte_order = self._config.byte_order
            p6 = self._config.p6_timeout

        # 阶段 3: 匹配 & 创建 _Sock（不持锁）
        new_entries: dict[int, _Sock] = {}
        for sock, src_ip, src_port in pending:
            matched = next(
                (a for a, (e_ip, e_port) in ecus_items
                 if e_ip == src_ip and (e_port == 0 or e_port == src_port)),
                None
            )

            if matched is None:
                logger.warning("收到非预期连接 %s:%d，不在 ECU 表中，已丢弃", src_ip, src_port)
                continue

            old = current_socks.get(matched)
            if old is not None:
                logger.info("ECU 0x%04X 替换已有连接", matched)

            new_entries[matched] = _Sock(sock, byte_order, p6=p6)
            logger.info("ECU 已连接 0x%04X @ %s:%d", matched, src_ip, src_port)

        # 阶段 4: 写入（持 state_lock）
        with self._state_lock:
            if self._server is None:
                return
            self._socks.update(new_entries)

    def _reconnect(self, addr: int) -> _Sock:
        """重连指定 ECU。调 _accept4connect 循环 accept 填表，
        结束后检查目标是否已连接。

        调用方保证持有 _session_lock。
        """
        with self._state_lock:
            self._socks[addr] = None

        self._accept4connect()

        with self._state_lock:
            if self._socks[addr] is None:
                raise TimeoutError(f"ECU 0x{addr:04X} 重连超时")
            return self._socks[addr]
