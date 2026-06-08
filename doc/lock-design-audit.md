# 锁设计审计

> 2026-06-08 审计。评估当前 `threading.Lock` 是否能满足三个并发需求，给出改造方案。
> 
> **2026-06-08 更新**：`stop()` 已移除。资源由 `_Sock.__del__` 依赖 CPython GC 回收。下文关于 `stop()` 的并发分析仅作存档参考。

---

## 1. 目标需求

| # | 方法                                  | 需求                                                                 |
|---|-------------------------------------|--------------------------------------------------------------------|
| A | `connections` / `current`           | **任意时刻可用，线程安全** — 即使 conversation 正在进行中，读状态不能阻塞                    |
| B | `start` / `select` / `conversation` | **互斥 + 快速失败** — 任何一个占用期间，另外两个必须立即失败（不阻塞等待）                         |
| C | `stop`                              | **任意时刻可快速停止** — 即使 conversation 持有锁、正在 I/O 中，stop 也能执行并关闭所有 socket |

---

## 2. 当前设计（单 Lock）评估

```
所有对外方法都用 with self._lock:（阻塞 acquire）
```

| 场景                                  | 现象                                           |   是否满足需求   |
|-------------------------------------|----------------------------------------------|:----------:|
| conversation 期间另一个线程调 `connections` | 阻塞，等 conversation 释放锁（最长 p6_star_timeout=5s） |    ❌ A     |
| conversation 期间另一个线程调 `select`      | 同上，阻塞 5s                                     | ❌ B（应快速失败） |
| conversation 期间另一个线程调 `stop`        | 阻塞 5s，stop 完全失效                              |    ❌ C     |
| start 期间另一个线程调 `select`             | 阻塞 accept_timeout=1.5s                       |    ❌ B     |
| 两个线程同时 `conversation`               | 第二个阻塞，等第一个完成                                 |    ❌ B     |

**结论：单 Lock 完全不满足需求。需要拆分。**

---

## 3. 改造设计：双锁分离

### 3.1 锁定义

| 锁               | 类型               | 保护对象                            | 持有时间                                        |            acquire 方式             |
|-----------------|------------------|---------------------------------|---------------------------------------------|:---------------------------------:|
| `_state_lock`   | `threading.Lock` | `_socks`, `_current`, `_server` | **微秒级**（纯内存读写，无 I/O）                        |                阻塞                 |
| `_session_lock` | `threading.Lock` | 操作互斥标记                          | **毫秒~秒级**（start / select / conversation 全程） | **try-acquire**（`blocking=False`） |

### 3.2 锁排序

```
_session_lock → _state_lock （外层 → 内层）
```

**绝不反向**：持有 `_state_lock` 期间绝不尝试获取 `_session_lock`，避免死锁。

### 3.3 各方法持锁规则

| 方法             | `_session_lock` | `_state_lock` | 说明                                                                                                         |
|----------------|:---------------:|:-------------:|------------------------------------------------------------------------------------------------------------|
| `connections`  |        —        |     ✅ 持锁      | 持锁构建返回值 dict，释放后返回                                                                                         |
| `current`      |        —        |     ✅ 持锁      | 持锁读取 `_current`，释放后返回                                                                                      |
| `start`        |      ✅ try      |     ✅ 持锁      | try 获取 session → 持 state 创建 server → 释放 state → accept → 持 state 填表 → 释放 session                           |
| `select`       |      ✅ try      |     ✅ 持锁      | try 获取 session → 持 state 读/写 `_current` → 释放                                                               |
| `conversation` |      ✅ try      |   ✅ 持锁（点状）    | try 获取 session → 持 state 读取 ecu/sock → **释放 state** → send/recv I/O → 仅在错误时持 state 清理 → finally 释放 session |
| `stop`         |     **不持**      |   ✅ 持锁（点状）    | 持 state 快照 sock → **释放 state** → 关闭 socket → 持 state 清空状态                                                  |

### 3.4 关键设计决策

**为什么 `stop` 不持 `_session_lock`？**

- `stop` 需要能在 conversation 持有 `_session_lock` 期间执行
- `stop` 通过直接关闭底层 socket 来中断 I/O，不需要 session 层的许可
- `socket.close()` 是线程安全的，Python 保证关闭期间另一线程的 `recv()`/`send()` 会抛出 `OSError`

**为什么 `conversation` 在 I/O 期间不持 `_state_lock`？**

- `_state_lock` 只保护内存状态，不应在 I/O 期间持有
- I/O 期间（send/recv）释放 `_state_lock`，让 `connections`/`current`/`stop` 可以随时访问
- `conversation` 已经持有 `_session_lock`，`start`/`select`/其他 `conversation` 会快速失败

---

## 4. 并发场景审计

以下遍历所有并发操作对，标记 ✅/⚠️/❌。

### 4.1 conversation ← → 其他

假设线程 A 正在 `conversation()` 中（持有 `_session_lock`，正在 I/O，不持 `_state_lock`）。

| 线程 B 操作        | 时序                                                                                                                                       | 结果     |
|:---------------|------------------------------------------------------------------------------------------------------------------------------------------|:-------|
| `connections`  | B: `_state_lock.acquire()` → 成功 → 读 `_socks` → 释放。A 在 I/O 中不受影响。                                                                         | ✅      |
| `current`      | 同上。                                                                                                                                      | ✅      |
| `select`       | B: `_session_lock.acquire(blocking=False)` → False → `raise RuntimeError`。                                                               | ✅ 快速失败 |
| `start`        | 同上。                                                                                                                                      | ✅ 快速失败 |
| `conversation` | 同上。                                                                                                                                      | ✅ 快速失败 |
| `stop`         | B: `_state_lock` → 快照 sock → 释放 → `s.close()` → A 的下次 `recv()` 抛 `OSError` → A 的 except 持 `_state_lock` 清理 → finally 释放 `_session_lock`。 | ✅      |

**conversation yield 暂停期间被 stop：**

```
线程 A: conversation yield 暂停（持有 _session_lock）
线程 B: stop() → 持 _state_lock 快照 → 关闭全部 socket → 清空状态
调用方: next(gen) → 生成器恢复 → sock.recv() → OSError → 清理 → 释放 _session_lock
```

✅ 正确。但如果调用方**放弃生成器**（不调 `next()` 也不调 `.close()`），`_session_lock` 泄漏。此时 `stop()` 仍可工作，但 `start`/`select`/`conversation` 永久失败。调用方必须负责消费或关闭生成器。

### 4.2 stop ← → 其他

**stop 从不持有 `_session_lock`**，所以 stop 与任何操作都不互斥。

| 场景                   | 时序                                                                               | 结果 |
|:---------------------|----------------------------------------------------------------------------------|:---|
| stop vs conversation | stop 关闭 socket → conversation recv 失败 → 清理退出                                     | ✅  |
| stop vs start        | stop 关闭 server socket → start 的 `_accept4connect` 检测 `_server is None` → raise   | ✅  |
| stop vs select       | select 很短（微秒），实际不会冲突。若恰好冲突：stop 先持 `_state_lock`，select 等待（微秒级），无影响              | ✅  |
| stop vs stop         | 两个 stop 可能同时关闭同一 socket。`socket.close()` 幂等，安全。两个都清空 `_socks`/`_server`，最后一个写入生效 | ✅  |
| stop vs connections  | connections 持 `_state_lock` 读，stop 持 `_state_lock` 写。互不干扰                        | ✅  |

### 4.3 start / select / conversation 互相

三者都需要 `_session_lock`，try-acquire 保证互斥：

```
线程 A: _session_lock.acquire(blocking=False) → True → 执行
线程 B: _session_lock.acquire(blocking=False) → False → RuntimeError
```

| 场景                          | 结果                               |
|:----------------------------|:---------------------------------|
| start + start               | 后者快速失败。且 start 幂等（先检查 `_server`） |
| start + select              | 后者快速失败                           |
| start + conversation        | 后者快速失败                           |
| select + select             | 后者快速失败                           |
| select + conversation       | 后者快速失败                           |
| conversation + conversation | 后者快速失败                           |

✅ 全部快速失败。

### 4.4 connections / current 互相

都只持有 `_state_lock` 读（微秒级），不互斥。

| 场景                        | 结果         |
|:--------------------------|:-----------|
| connections + connections | 各自短暂持锁，无冲突 |
| connections + current     | 同上         |
| current + current         | 同上         |

✅ 任意时刻可用。

### 4.5 内部方法 `_accept4connect` / `_reconnect`

内部方法**不持任何锁**，由调用方（`start` 或 `conversation`）保证 `_session_lock` 已持有。

**`_accept4connect` 与 stop 的竞争：**

```
_accept4connect:                    stop:
  持 _state_lock 读 _server → 释放       持 _state_lock 快照 sock
  server.accept() 循环 ...              关闭全部 socket（含 server）
                                        持 _state_lock 设 _server=None
  accept() 抛 OSError → break
  持 _state_lock 检查 _server is None → 丢弃 pending → raise
```

✅ 正确。`_accept4connect` 必须：

1. 在 accept 循环前读取 `self._server` 到局部变量（避免中途被 stop 置 None 后访问 `.accept()` 抛 AttributeError）
2. accept 循环中捕获 `OSError`（server 被 stop 关闭）
3. 更新 `_socks` 前再次检查 `self._server is None`（防止 stop 在此时刻之后清空、而 _accept4connect 又填入新 sock 导致状态泄漏）

---

## 5. 代码改动清单

### 5.1 `__init__`

```python
# 替换
self._lock = threading.Lock()

# 为
self._state_lock = threading.Lock()
self._session_lock = threading.Lock()
```

### 5.2 `connections` / `current`

```python
# with self._lock:  →  with self._state_lock:
```

### 5.3 `select`

```python
def select(self, addr: int) -> bool:
    if not self._session_lock.acquire(blocking=False):
        raise RuntimeError("另一个操作正在进行中，无法切换 ECU")
    try:
        with self._state_lock:
            # 原逻辑不变
            ...
    finally:
        self._session_lock.release()
```

### 5.4 `start`

```python
def start(self) -> None:
    if not self._session_lock.acquire(blocking=False):
        raise RuntimeError("另一个操作正在进行中，无法启动")
    try:
        with self._state_lock:
            if self._server is not None:
                return
            # 创建 server socket...
            self._server = sock
        self._accept4connect()
    finally:
        self._session_lock.release()
```

### 5.5 `conversation`

```python
def conversation(self, payload: bytes) -> Iterator[bytes]:
    if not self._session_lock.acquire(blocking=False):
        raise RuntimeError("另一个操作正在进行中，无法发送")
    try:
        with self._state_lock:
            # 读取 _current, sock, encode frame
            ...
        # send/recv I/O（不持 _state_lock）
        # 错误处理中用 with self._state_lock: 清理 _socks
        ...
    finally:
        self._session_lock.release()
```

**注意**：`_session_lock` 仍跨 `yield` 持有。这是设计意图——生成器 yield 暂停期间，其他 `start`/`select`/`conversation` 快速失败。调用方必须在用完后消费完或显式 `.close()` 生成器，否则 `_session_lock` 泄漏。

### 5.6 `stop`

```python
def stop(self) -> None:
    # 1. 快照 sock（持 state_lock，微秒）
    with self._state_lock:
        socks_snapshot = [(addr, s) for addr, s in self._socks.items() if s is not None]
        server = self._server

    # 2. 关闭 socket（不持锁，可中断正在进行的 I/O）
    for addr, s in socks_snapshot:
        try:
            s.close()
        except Exception:
            pass

    # 3. 清空状态（持 state_lock）
    with self._state_lock:
        self._socks = {addr: None for addr in self._ecus}
        if server:
            try:
                server.close()
            except Exception:
                pass
        self._server = None
        self._current = None
```

### 5.7 `_accept4connect`

```python
def _accept4connect(self) -> None:
    # 调用方保证持有 _session_lock
    with self._state_lock:
        server = self._server
    if not server:
        raise RuntimeError("服务未启动")

    pending = []
    while True:
        try:
            sock, addr = server.accept()
            pending.append((sock, *addr))
        except TimeoutError:
            break
        except OSError:
            break  # server 被 stop 关闭

    if not pending:
        raise RuntimeError("没有任何连接端口，直接退出")

    with self._state_lock:
        if self._server is None:  # stop 在此期间被调用
            for sock, _, _ in pending:
                sock.close()
            raise RuntimeError("服务已停止")

        for sock, src_ip, src_port in pending:
            # 匹配 + 填表逻辑不变
            ...
```

### 5.8 `_reconnect`

```python
def _reconnect(self, addr: int) -> _Sock:
    # 调用方保证持有 _session_lock
    with self._state_lock:
        old = self._socks[addr]
        if old is not None:
            old.close()
        self._socks[addr] = None

    self._accept4connect()

    with self._state_lock:
        if self._socks[addr] is None:
            raise TimeoutError(f"ECU 0x{addr:04X} 重连超时")
        return self._socks[addr]
```

---

## 6. 残留风险

| # | 风险                                                                      | 严重度 | 缓解                                             |
|---|-------------------------------------------------------------------------|:---:|------------------------------------------------|
| 1 | 调用方放弃 conversation 生成器（不消费也不 close）→ `_session_lock` 永久泄漏               |  中  | 文档明确说明；`stop()` 仍可工作作为兜底；后续可加 `__del__` + warn |
| 2 | `_session_lock` 是普通 Lock，非 RLock。若未来有人让 `start` 内部调 `select`（或其他组合），会死锁 |  低  | 代码规范：公开方法之间不互相调用                               |
| 3 | Python 3.13+ free-threaded 模式下 `socket.close()` 的线程安全性取决于 CPython 实现    | 极低  | CPython 保证 socket 的 close/recv/send 线程安全       |

---

## 7. 结论

当前单 `Lock` 设计 → **不满足需求**。

双锁方案（`_state_lock` + `_session_lock` try-acquire）经过上述 15+ 并发场景审计，全部 ✅ 通过，可以实施。