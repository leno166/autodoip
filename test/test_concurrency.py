"""
并发测试：锁竞争、对话互斥、状态操作并发、异常重连并发

全部使用 mock socket，无需真实 ECU 硬件。
"""
import threading
import time
import pytest
from unittest.mock import MagicMock

from autodoip import Endpoint
from autodoip._transport import _Protocol, _Sock

TESTER = 0x0E80
ECU = 0x1001


# ============================================================
# 辅助
# ============================================================

class _Raise:
    """recv 序列中的哨兵：遇到时抛出指定异常。"""
    def __init__(self, exc: BaseException):
        self.exc = exc


def _make_recv(sequence: list):
    """根据序列创建 MagicMock side_effect 函数。

    - bytes         → 返回该值
    - _Raise(exc)   → raise exc
    - threading.Event → 等待 Event 被 set，然后 raise TimeoutError
    """
    it = iter(sequence)

    def _recv():
        item = next(it)
        if isinstance(item, _Raise):
            raise item.exc
        if isinstance(item, threading.Event):
            item.wait(timeout=10)
            raise TimeoutError()
        return item

    return _recv


def _resp_frame(payload: bytes, tester: int = TESTER, ecu: int = ECU) -> bytes:
    """构造合法 DoIP 响应帧（ECU→Tester，源=ecu，目标=tester）。"""
    proto = _Protocol(version=0x02, msg_type=0x8001, byte_order='big')
    return proto.encode(payload, ecu, tester)


def _make_endpoint(tester: int = TESTER, ecu: int = ECU) -> Endpoint:
    """构造已注入 mock _Sock 的 Endpoint（跳过 start/accept）。"""
    ep = Endpoint(ip='0.0.0.0', ecus={ecu: ('10.0.0.1', 0)}, tester=tester)
    mock_raw = MagicMock()
    mock_sock = _Sock(mock_raw, byte_order='big')
    ep._socks[ecu] = mock_sock
    ep._current = ecu
    ep._server = MagicMock()
    return ep


def _consume_one(ep: Endpoint, payload: bytes) -> bytes:
    """消耗 conversation 的第一条响应（其余丢弃）。"""
    gen = ep.conversation(payload)
    resp = next(gen)
    gen.close()
    return resp


# ============================================================
# 1. 对话互斥验证
# ============================================================

def test_mutual_exclusion():
    """两个线程同时 conversation()，第二条必须等第一条完成，响应不交错。"""
    ep = _make_endpoint()
    sock = ep._socks[ECU]

    sock.send = MagicMock()
    sock.recv = MagicMock(side_effect=_make_recv([
        _resp_frame(b'\x62'),
        _Raise(TimeoutError()),
        _resp_frame(b'\x7E'),
        _Raise(TimeoutError()),
    ]))

    order: list[str] = []
    results_a: list[bytes] = []
    results_b: list[bytes] = []

    def thread_a():
        order.append('A-enter')
        for resp in ep.conversation(b'\x22'):
            results_a.append(resp)
        order.append('A-exit')

    def thread_b():
        order.append('B-enter')
        for resp in ep.conversation(b'\x3E'):
            results_b.append(resp)
        order.append('B-exit')

    ta = threading.Thread(target=thread_a, name='A')
    tb = threading.Thread(target=thread_b, name='B')
    ta.start()
    time.sleep(0.05)  # 确保 A 先拿到锁
    tb.start()

    ta.join(timeout=5)
    tb.join(timeout=5)

    assert results_a == [b'\x62']
    assert results_b == [b'\x7E']
    assert order == ['A-enter', 'A-exit', 'B-enter', 'B-exit'], \
        f"对话交错: {order}"


# ============================================================
# 2. 对话暂停期间其他操作不被阻塞
# ============================================================

@pytest.mark.xfail(
    reason=(
        "已知限制：当前单一 RLock 设计，conversation() 持有锁跨 yield，"
        "select()/connections()/current 在另一个线程调用会阻塞直到对话结束。"
        "若后续拆分 state_lock 与 session_lock，此用例应改为 pass。"
    ),
    strict=True,
)
def test_select_not_blocked_during_conversation():
    """对话暂停（yield 后），另一线程 select() 应在 0.5s 内完成。"""
    ep = _make_endpoint()
    sock = ep._socks[ECU]

    recv_done = threading.Event()  # 初始未 set，recv 会阻塞
    sock.send = MagicMock()
    sock.recv = MagicMock(side_effect=_make_recv([
        _resp_frame(b'\x62'),  # 第 1 次 recv：立即返回
        recv_done,             # 第 2 次 recv：阻塞直到 set
    ]))

    a_got_first = threading.Event()

    def thread_a():
        gen = ep.conversation(b'\x22')
        first = next(gen)
        assert first == b'\x62'
        a_got_first.set()
        # 第二次 recv 会阻塞在 recv_done.wait()
        try:
            next(gen)
        except StopIteration:
            pass

    ta = threading.Thread(target=thread_a, name='A')
    ta.start()
    assert a_got_first.wait(timeout=3), "线程 A 未能收到第一帧"

    # 线程 B：尝试 select —— 期望 0.5s 内完成
    b_time = [999.0]
    b_done = threading.Event()

    def thread_b():
        t0 = time.perf_counter()
        try:
            ep.select(ECU)
        finally:
            b_time[0] = time.perf_counter() - t0
            b_done.set()

    tb = threading.Thread(target=thread_b, name='B')
    tb.start()
    tb.join(timeout=0.5)

    # 清理
    recv_done.set()
    ta.join(timeout=5)

    assert b_done.is_set(), \
        f"select() 被阻塞超过 0.5s（实际 {b_time[0]:.1f}s），锁粒度过粗"
    assert b_time[0] < 0.3, \
        f"select() 耗时 {b_time[0]:.2f}s，应 <0.3s"


# ============================================================
# 3. 对话中调用 stop() 的安全性
# ============================================================

@pytest.mark.xfail(
    reason=(
        "已知限制：stop() 需要获取 self._lock，而 conversation 生成器持有该锁跨 yield，"
        "从另一线程调用 stop() 会阻塞直到对话结束。"
        "若后续拆分锁，此用例应改为 pass。"
    ),
    strict=True,
)
def test_stop_during_conversation():
    """对话暂停时 stop() 能正常返回，之后生成器 next() 抛异常，无死锁。"""
    ep = _make_endpoint()
    sock = ep._socks[ECU]

    recv_done = threading.Event()
    sock.send = MagicMock()
    sock.recv = MagicMock(side_effect=_make_recv([
        _resp_frame(b'\x62'),
        recv_done,
    ]))

    a_got_first = threading.Event()
    a_error: list[BaseException | None] = [None]

    def thread_a():
        gen = ep.conversation(b'\x22')
        first = next(gen)
        assert first == b'\x62'
        a_got_first.set()
        try:
            next(gen)
        except Exception as e:
            a_error[0] = e

    ta = threading.Thread(target=thread_a, name='A')
    ta.start()
    assert a_got_first.wait(timeout=3)

    # 线程 B：stop —— 期望 1s 内返回
    b_done = threading.Event()
    b_error: list[BaseException | None] = [None]

    def thread_b():
        try:
            ep.stop()
        except Exception as e:
            b_error[0] = e
        finally:
            b_done.set()

    tb = threading.Thread(target=thread_b, name='B')
    tb.start()
    tb.join(timeout=1)

    # 在放行 recv 之前检查 —— stop 此时应已完成（不阻塞）
    # 当前单锁设计下 stop() 会阻塞，此断言预期失败 → xfail
    assert b_done.is_set(), "stop() 被阻塞超过 1s（死锁或锁粒度过粗）"

    # 清理
    recv_done.set()
    ta.join(timeout=5)

    assert b_error[0] is None, f"stop() 抛异常: {b_error[0]}"


# ============================================================
# 4. 高频率状态切换与对话交替
# ============================================================

def test_high_frequency_alternation():
    """多线程反复执行 select/connections/current/conversation，无异常。"""
    ep = _make_endpoint()
    sock = ep._socks[ECU]

    sock.send = MagicMock()
    sock.recv = MagicMock(return_value=_resp_frame(b'\x62'))

    iterations = 2000
    errors: list[str] = []
    error_lock = threading.Lock()

    def worker():
        for i in range(iterations):
            try:
                choice = i % 5
                if choice == 0:
                    ep.select(ECU)
                elif choice == 1:
                    ep.connections()
                elif choice == 2:
                    _ = ep.current
                elif choice == 3:
                    _consume_one(ep, b'\x22')
                else:
                    gen = ep.conversation(b'\x22')
                    next(gen)
                    gen.close()
            except Exception as e:
                with error_lock:
                    errors.append(f"{threading.current_thread().name} #{i}: {e}")

    threads = [
        threading.Thread(target=worker, name=f'W-{j}')
        for j in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    if errors:
        pytest.fail(f"{len(errors)} 个错误:\n" + "\n".join(errors[:10]))

    conns = ep.connections()
    assert conns[ECU][2] is True  # 仍然 connected


# ============================================================
# 5. 模拟 socket 异常与重连并发
# ============================================================

def test_reconnect_race_with_select():
    """send 失败触发重连时，另一线程 select 不导致状态混乱。"""
    ep = _make_endpoint()
    sock = ep._socks[ECU]

    # 第一次 send 失败触发重连
    send_count = [0]

    def failing_send(_frame):
        send_count[0] += 1
        if send_count[0] == 1:
            raise ConnectionError('gone')

    sock.send = MagicMock(side_effect=failing_send)
    sock.recv = MagicMock(side_effect=[
        _resp_frame(b'\x62'),
        _Raise(TimeoutError()),
    ])

    # mock _reconnect 返回新 sock，跳过真实 accept
    new_mock = MagicMock()
    new_mock.send = MagicMock()
    new_mock.recv = MagicMock(side_effect=[
        _resp_frame(b'\x62'),
        _Raise(TimeoutError()),
    ])
    import autodoip._transport as tmod
    original_reconnect = ep._reconnect
    ep._reconnect = MagicMock(return_value=new_mock)

    errors: list[str] = []
    error_lock = threading.Lock()
    select_done = threading.Event()

    def thread_a():
        try:
            _consume_one(ep, b'\x22')
        except Exception as e:
            with error_lock:
                errors.append(f"A: {e}")

    def thread_b():
        try:
            ep.select(ECU)
        except Exception as e:
            with error_lock:
                errors.append(f"B: {e}")
        finally:
            select_done.set()

    ta = threading.Thread(target=thread_a, name='A')
    tb = threading.Thread(target=thread_b, name='B')
    ta.start()
    time.sleep(0.02)
    tb.start()

    ta.join(timeout=5)
    tb.join(timeout=5)

    if errors:
        pytest.fail(f"并发异常:\n" + "\n".join(errors))

    assert select_done.is_set(), "select 未完成"


def test_recv_error_during_conversation_clears_sock():
    """recv 断开后 sock 被清空，另一线程能观测到 disconnected 状态。"""
    ep = _make_endpoint()
    sock = ep._socks[ECU]

    sock.send = MagicMock()
    sock.recv = MagicMock(side_effect=_make_recv([
        _resp_frame(b'\x62'),
        _Raise(ConnectionError('gone')),
    ]))

    a_got_first = threading.Event()

    def thread_a():
        gen = ep.conversation(b'\x22')
        first = next(gen)
        assert first == b'\x62'
        a_got_first.set()
        try:
            next(gen)
        except StopIteration:
            pass

    ta = threading.Thread(target=thread_a, name='A')
    ta.start()
    assert a_got_first.wait(timeout=3)
    ta.join(timeout=5)

    assert ep._socks[ECU] is None
    _, _, connected = ep.connections()[ECU]
    assert connected is False


# ============================================================
# 压力测试（需要真实 ECU 硬件）
# ============================================================

@pytest.mark.skip(reason="需要真实 DoIP ECU 硬件环境")
def test_concurrency_stress():
    """多线程并发调用 conversation，不应出现异常或线程冲突。"""
    LOOPS_PER_THREAD = 200
    THREADS_PER_TYPE = 3
    THREAD_CONFIGS = [
        ('DC06', (0x22FF00).to_bytes(3, 'big'), b'\x62'),
        ('3E00', (0x3E00).to_bytes(2, 'big'), b'\x7e'),
        ('22F187', (0x22F187).to_bytes(3, 'big'), b'\x7f'),
    ]

    endpoint = Endpoint(
        ecus={0x1001: ('198.18.18.88', 0)},
        ip='198.18.18.19',
    )
    endpoint.start()
    if not endpoint.select(0x1001):
        endpoint.stop()
        pytest.fail("ECU 0x1001 未连接")

    errors: list[str] = []
    error_lock = threading.Lock()

    def send_loop(req, expected_prefix, name):
        for i in range(LOOPS_PER_THREAD):
            try:
                final_resp = None
                for resp in endpoint.conversation(req):
                    if len(resp) > 3 and resp[0] == 0x7F and resp[-1] == 0x78:
                        continue
                    final_resp = resp
                    break
                else:
                    raise RuntimeError("未收到有效响应（全部为延迟指示或空流）")

                assert isinstance(final_resp, bytes)
                assert len(final_resp) >= 1
                assert final_resp[:1] == expected_prefix

            except Exception as e:
                with error_lock:
                    errors.append(f"[{name}] 第{i + 1}次失败: {e}")
                break

    start_time = time.perf_counter()

    threads = []
    for name_prefix, req, expected in THREAD_CONFIGS:
        for t_idx in range(THREADS_PER_TYPE):
            t_name = f"{name_prefix}-{t_idx + 1}"
            t = threading.Thread(
                target=send_loop, args=(req, expected, t_name), name=t_name,
            )
            threads.append(t)
            t.start()

    for t in threads:
        t.join()

    endpoint.stop()

    elapsed = time.perf_counter() - start_time
    total_calls = LOOPS_PER_THREAD * len(threads)

    if errors:
        pytest.fail(f"{len(errors)} 个错误:\n" + "\n".join(errors[:10]))

    print(f"\n✅ {total_calls} 次调用，无线程冲突")
    print(f"⏱️  总耗时: {elapsed:.2f}s  |  TPS: {total_calls / elapsed:.2f}")