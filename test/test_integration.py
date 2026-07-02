"""
集成测试：echo ECU 主动连接 tester，验证 Endpoint 收发。
"""
import socket
import threading
import time
from autodoip import Endpoint, Config

HOST = '127.0.0.1'
PORT = 13400
TESTER = 0x0E80
ECU = 0x1001
BYTE_ORDER = 'big'


class EchoEcu:
    """模拟 ECU：连接 tester:13400，收 DoIP 帧后翻转源/目标地址回发。"""

    def __init__(self, ecu_addr: int = ECU, host: str = HOST, port: int = PORT):
        self._addr = ecu_addr
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((host, port))
        self._sock.settimeout(0.5)

    def recv_frame(self) -> bytes | None:
        try:
            header = self._recv_exact(8)
        except (TimeoutError, ConnectionError, OSError):
            return None
        payload_len = int.from_bytes(header[4:8], BYTE_ORDER)
        try:
            return header + self._recv_exact(payload_len)
        except (TimeoutError, ConnectionError, OSError):
            return None

    def send_frame(self, payload: bytes) -> None:
        inner = (self._addr.to_bytes(2, BYTE_ORDER) +
                 TESTER.to_bytes(2, BYTE_ORDER) + payload)
        header = (b'\x02\xfd\x80\x01' +
                  len(inner).to_bytes(4, BYTE_ORDER))
        self._sock.sendall(header + inner)

    def echo_payload(self) -> bytes:
        """收一帧，原样回传 payload。返回收到的 payload。"""
        frame = self.recv_frame()
        if frame is None:
            return b''
        payload = frame[12:]
        self.send_frame(payload)
        return payload

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass

    def _recv_exact(self, size: int) -> bytes:
        buf = b''
        while len(buf) < size:
            chunk = self._sock.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("连接断开")
            buf += chunk
        return buf


def start_with_ecu(ep: Endpoint, ecu_addr: int = ECU) -> EchoEcu:
    """后台连接 EchoEcu 同时调 ep.start()。"""
    ecu = None

    def _connect():
        nonlocal ecu
        time.sleep(0.02)
        ecu = EchoEcu(ecu_addr)

    t = threading.Thread(target=_connect)
    t.start()
    ep.start()
    t.join()
    assert ecu is not None
    return ecu


# ============================================================

def test_basic_echo():
    """基本收发。"""
    print("--- 1. 基本收发 ---")
    ep = Endpoint(ip=HOST, ecus={ECU: (HOST, 0)})
    ecu = start_with_ecu(ep)
    assert ep.select(ECU)

    gen = ep.conversation(b'\x22\xFF\x00')
    result = []

    def _echo():
        result.append(ecu.echo_payload())
    t = threading.Thread(target=_echo)
    t.start()

    resp = next(gen)
    t.join()
    assert resp == b'\x22\xFF\x00', f"不匹配: {resp.hex(' ')}"
    print(f"  发送 22FF00 -> 收到 {resp.hex(' ')}")
    gen.close()
    ecu.close()
    print("  PASS\n")


def test_multi_frame():
    """多帧响应。"""
    print("--- 2. 多帧响应 ---")
    ep = Endpoint(ip=HOST, ecus={ECU: (HOST, 0)},
                  config=Config(p6_star_timeout=2.0))
    ecu = start_with_ecu(ep)
    ep.select(ECU)

    gen = ep.conversation(b'\x3E\x00')

    def _respond():
        ecu.recv_frame()
        ecu.send_frame(b'\x7F\x3E\x78')
        time.sleep(0.02)
        ecu.send_frame(b'\x7E\x00\x12\x34')

    t = threading.Thread(target=_respond)
    t.start()

    results = list(gen)
    t.join()
    assert len(results) == 2, f"应收到 2 帧，实际 {len(results)}"
    assert results[0] == b'\x7F\x3E\x78'
    assert results[1] == b'\x7E\x00\x12\x34'
    print(f"  收到 {len(results)} 帧: {[r.hex(' ') for r in results]}")
    ecu.close()
    print("  PASS\n")


def test_timeout():
    """ECU 不回复，首帧超时直接返回。"""
    print("--- 3. 超时无响应 ---")
    ep = Endpoint(ip=HOST, ecus={ECU: (HOST, 0)},
                  config=Config(p6_timeout=0.3, p6_star_timeout=0.5))
    ecu = start_with_ecu(ep)
    ep.select(ECU)

    gen = ep.conversation(b'\x22\xFF\x00')
    t0 = time.perf_counter()
    results = list(gen)
    elapsed = time.perf_counter() - t0

    assert results == [], f"应超时无响应，实际 {len(results)} 帧"
    assert elapsed < 3.0, f"超时过长: {elapsed:.2f}s"
    print(f"  正确超时 ({elapsed:.2f}s)")
    ecu.close()
    print("  PASS\n")


def test_reconnect():
    """sock 为 None → 自动重连成功 → ReconnectionError，上层重试成功。"""
    print("--- 4. sock=None 自动重连 ---")
    from autodoip import ReconnectionError

    ep = Endpoint(ip=HOST, ecus={ECU: (HOST, 0)},
                  config=Config(accept_timeout=0.3, p6_timeout=0.5))

    # 启动 — accept 超时（没有 ECU），_socks[ECU] 仍为 None
    try:
        ep.start()
    except RuntimeError:
        pass  # 预期：没有连接

    # 手动设置 current
    ep._current = ECU

    # 连接 ECU — 进入 server backlog
    ecu = EchoEcu()

    # 第一次：sock=None → _reconnect → 重连成功 → ReconnectionError
    try:
        gen = ep.conversation(b'\x22\xDE\xAD')
        next(gen)
        pytest.fail("应抛出 ReconnectionError")
    except ReconnectionError:
        print("  收到 ReconnectionError，连接已恢复")

    # 验证 sock 已恢复
    assert ep._socks[ECU] is not None, "重连后 sock 应为非 None"
    _, _, connected = ep.connections[ECU]
    assert connected is True, "重连后应处于已连接状态"

    # 重试：sock 已恢复，正常收发
    gen = ep.conversation(b'\x22\xDE\xAD')
    result = []

    def _echo():
        result.append(ecu.echo_payload())

    t = threading.Thread(target=_echo)
    t.start()

    resp = next(gen)
    t.join()
    assert resp == b'\x22\xDE\xAD', f"不匹配: {resp.hex(' ')}"
    print(f"  重试后收到: {resp.hex(' ')}")
    gen.close()
    ecu.close()
    print("  PASS\n")


def test_select_fail_fast():
    """conversation 期间 select 快速返回 False。"""
    print("--- 5. select 快速失败 ---")
    ep = Endpoint(ip=HOST, ecus={ECU: (HOST, 0)},
                  config=Config(p6_star_timeout=3.0))
    ecu = start_with_ecu(ep)
    ep.select(ECU)

    started = threading.Event()

    def _converse():
        gen = ep.conversation(b'\x22')
        started.set()
        for _ in gen:
            pass

    t = threading.Thread(target=_converse)
    t.start()
    started.wait()

    t0 = time.perf_counter()
    ok = ep.select(ECU)
    elapsed = time.perf_counter() - t0

    assert ok is False, f"应为 False，实际 {ok}"
    assert elapsed < 0.3, f"应快速失败，实际 {elapsed:.2f}s"
    print(f"  select 在 {elapsed:.3f}s 内返回 False")
    t.join(timeout=5)
    ecu.close()
    print("  PASS\n")


def test_connections_during_conversation():
    """conversation 期间 connections/current 正常。"""
    print("--- 6. connections/current 任意可用 ---")
    ep = Endpoint(ip=HOST, ecus={ECU: (HOST, 0)},
                  config=Config(p6_star_timeout=3.0))
    ecu = start_with_ecu(ep)
    ep.select(ECU)

    started = threading.Event()

    def _converse():
        gen = ep.conversation(b'\x22')
        started.set()
        for _ in gen:
            pass

    t = threading.Thread(target=_converse)
    t.start()
    started.wait()

    conns = ep.connections
    cur = ep.current
    assert cur == ECU
    assert conns[ECU][2] is True
    print(f"  connections: {conns}")
    print(f"  current: 0x{cur:04X}")
    t.join(timeout=5)
    ecu.close()
    print("  PASS\n")


if __name__ == '__main__':
    test_basic_echo()
    test_multi_frame()
    test_timeout()
    test_reconnect()
    test_select_fail_fast()
    test_connections_during_conversation()
    print("=" * 40)
    print("全部通过")
