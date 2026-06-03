# autodoip 设计文档

> DoIP (Diagnostics over IP) transport layer for automotive UDS — ISO 13400.

---

## 1. 设计原则

| 原则           | 说明                                                         |
|--------------|------------------------------------------------------------|
| DoIP 不感知 UDS | 只负责传输层：连接管理、帧编解码、收发。参数名用 `payload` 而非 `uds`                |
| 零外部依赖        | 仅 Python 标准库 `socket` + `threading` + `dataclasses`        |
| 最小公开 API     | 只暴露 `Endpoint` / `Config` / `ProtocolError`，内部实现随时可改       |
| 库名即前缀        | 类名无 `DoIP`/`DoIp` 前缀，`from autodoip import Endpoint` 语义已足够 |

### 1.1 分层边界

```
上层（UDS）           ← 不在本库范围内
──────────────────────────
Endpoint                      ← Socket + 连接表 + Protocol + Lock + 自动重连
_Protocol                     ← DoIP 帧编解码（ISO 13400）
_Sock                         ← 单 socket 封装
recv_frame                    ← 帧级 IO
ProtocolError                 ← 协议异常
```

### 2.2 连接拓扑：Tester-as-Server

#### 采用的架构

当前实现采用 **tester-as-server** 模式（ISO 13400 合法拓扑变体）：

```
                   ┌──────────────────┐
      ECU 0x1301 ──┤                  │
      (198.18.44.49)│                  │
                    │     Tester       │
      ECU 0x1302 ──┤     监听         │
      (198.18.44.50)│   ip:port      │
                    │                  │
      ECU 0x1303 ──┤                  │
                    └──────────────────┘
```

- Tester 在自己 IP 上监听**一个**端口（默认 13400）
- **所有 ECU 连接同一个 ip:port**，由 DoIP 帧头中的逻辑地址区分路由
- 一个 tester = 一个 ip:port = 一个 Endpoint

#### 不存在的架构

以下结构**不可能也不应该存在**：

```
 ✗ 多端口方案（不存在）                ✗ 多 Endpoint 方案（不存在）
 ┌──────────────────┐                 ep1 = Endpoint(port=13400)
 │ Tester :13400 ←── ECU_A           ep2 = Endpoint(port=13500)
 │ Tester :13500 ←── ECU_B           ep3 = Endpoint(port=13600)
 │ Tester :13600 ←── ECU_C
 └──────────────────┘
```

**原因**：DoIP 帧头已经包含 `tester_addr` + `ecu_addr`，协议本身就是**单端口多 ECU 路由**。如果每个 ECU 独占一个端口，端口号本身就能区分 ECU，帧头逻辑地址变成冗余——整个 DoIP 层失去意义，直接用 raw TCP 发 UDS 载荷即可。

因此一个端口 = 一个 Endpoint = 一份 Config，不存在多 Endpoint 多 Config 的场景。

### 2.4 autodoip 包结构

```
src/autodoip/
├── __init__.py        # 公开 3 个: Endpoint, Config, ProtocolError
├── _frame.py          # recv_exact + recv_frame（帧级 IO，内部）
├── _config.py         # Config 数据类（公开内容，模块名私有）
├── _errors.py         # ProtocolError（公开内容，模块名私有）
└── _transport.py      # Endpoint（公开）+ _Protocol + _Sock（内部）
```

### 2.4 命名与可见性

| 类/函数            | 可见性 | 说明          |
|-----------------|-----|-------------|
| `Endpoint`      | 公开  | DoIP 端点入口   |
| `Config`        | 公开  | 传输调优参数      |
| `ProtocolError` | 公开  | 协议异常        |
| `_Sock`         | 内部  | 单 socket 封装 |
| `_Protocol`     | 内部  | DoIP 帧编解码   |
| `recv_exact`    | 内部  | 精确收取        |
| `recv_frame`    | 内部  | 完整帧收取       |

### 2.5 各模块内容

#### `_frame.py` — 帧 IO 工具

| 函数                                | 用途                                    |
|-----------------------------------|---------------------------------------|
| `recv_exact(sock, size) -> bytes` | 精确收取 size 字节，连接关闭时抛 `ConnectionError` |
| `recv_frame(sock) -> bytes`       | 收取完整 DoIP 帧（8 字节头 + N 字节载荷）           |

仅依赖 `socket` 标准库。

#### `_config.py` — 传输调优参数

```python
@dataclass
class Config:
    """DoIP 传输层调优参数（不含身份参数：ip/port/tester/ecus）"""
    accept_timeout: float = 1.5
    recv_timeout: float = 3.0
    listen_count: int = 5
    version: int = 0x02
    msg_type: int = 0x8001
    byte_order: Literal['little', 'big'] = 'big'
```

#### `_errors.py` — 异常

```python
class ProtocolError(Exception):
    """DoIP 协议层错误 — 帧格式校验失败"""
```

#### `_transport.py` — 传输层

含 4 个类：`_Sock` + `_Protocol` + `Endpoint`（整合了原 SocketManager）。

**1. Endpoint 构造函数**

```python
class Endpoint:
    def __init__(self,
                 ip: str,  # 本地监听 IP，必传，无默认
                 ecus: dict[int, tuple[str, int]],  # 逻辑地址 → (ECU_IP, ECU_port)，port=0 忽略
                 port: int = 13400,  # 本地监听端口
                 tester: int = 0x0E80,  # tester 逻辑地址
                 config: Config | None = None):  # 传输调优，可选
        cfg = config or Config()
        self._ip = ip
        self._port = port
        self._tester = tester
        self._ecus = ecus.copy()
        self._current: int | None = None
        ...
```

**参数分类**

| 类别        | 参数                  | 默认值    | 归属          |
|-----------|---------------------|--------|-------------|
| **身份/连接** | `ip`                | 无（必传）  | Endpoint 签名 |
|           | `ecus`              | 无（必传）  | Endpoint 签名 |
|           | `port`              | 13400  | Endpoint 签名 |
|           | `tester`            | 0x0E80 | Endpoint 签名 |
| **传输调优**  | `accept_timeout`    | 1.5    | Config      |
|           | `recv_timeout`      | 3.0    | Config      |
|           | `listen_count`      | 5      | Config      |
|           | `version`           | 0x02   | Config      |
|           | `msg_type`          | 0x8001 | Config      |
|           | `byte_order`        | 'big'  | Config      |

**2. 连接表内部结构**

启动时用 `ecus` 预建全表，所有 ECU 的 socket 初始为 `None`。accept 时按 IP 匹配填入实际 socket。

```python
# 内部数据结构
self._socks: dict[int, _Sock | None] = {addr: None for addr in ecus}
# 0x1301 → _Sock(sock)    ← 已连接
# 0x1302 → None           ← 声明了但还没连上
```

**3. accept 行为（`_accept4connect`）**

`start()` 启动时调一次 `_accept4connect`：`accept_timeout` 内循环 accept，匹配 ecus 填表，超时退出。

之后不再自动运行——只在 `send()` 出错时被触发。

**4. select 行为**

```python
def select(self, addr: int) -> bool:
    """切换到指定逻辑地址的 ECU。成功返回 True，失败返回 False 且不改变当前选中。"""
    if addr not in self._ecus:
        raise ValueError(f"未知 ECU 逻辑地址: 0x{addr:04X}")
    if self._socks[addr] is None:
        logger.warning("ECU 0x%04X 尚未连接，保持当前选中", addr)
        return False
    self._current = addr
    return True
```

- 目标 ECU 未连接 → 返回 `False`，**不切换、不抛异常、不退**
- 上层（Session）收到 `False` 可以自己决定处理——忽略、重试、或上报给用户

**5. send 与重连**

`sock.send()` 失败和 `sock` 为 None 统一经过同一个 except 处理：

```python
def send(self, payload: bytes) -> bytes:
    with self._lock:
        ...
        sock = self._socks[ecu]

        try:
            sock.send(frame)           # sock=None → AttributeError，
            response = sock.recv()     # 网络断开 → ConnectionError/TimeoutError/OSError
        except (ConnectionError, TimeoutError, OSError, AttributeError):
            sock = self._reconnect(ecu)   # 抢救一次
            try:
                sock.send(frame)          # 重发
                response = sock.recv()
            except (ConnectionError, TimeoutError, OSError):
                self._socks[ecu] = None   # 清空，下次 send 可再次触发重连
                raise ConnectionError(...)
    ...
```

**`_reconnect` 逻辑**：清旧 sock → 置 None → 调 `_accept4connect` 循环 accept → 检查目标是否连上 → 连上返 sock，未连抛 `TimeoutError`。

**恢复机制**：清空后下次 `send()`，sock 为 None → `AttributeError` 落入同一个 except → `_reconnect` 再次尝试——ECU 恢复连接就能自动续上。

**6. connections 返回**

```python
def connections(self) -> dict[int, tuple[str, int, bool]]:
    """返回所有 ECU 及其连接状态。
    {addr: (ip, port, connected), ...}
    """
    return {
        addr: (ip, port, self._socks[addr] is not None)
        for addr, (ip, port) in self._ecus.items()
    }
```

返回所有声明的 ECU（含未连接的），上层能一眼看到全局状态。

**7. 内部组件接收具体值，不接触 Config**

`_Protocol` / `_SocketManager` 的构造函数接收 `version`、`msg_type`、`byte_order` 等具体值，不依赖也不引用 `Config`。依赖链单向：`Config` → `Endpoint` → 内部组件。

**8. 命名与改名**

| 原名称             | 新名称              | 说明                              |
|-----------------|------------------|---------------------------------|
| `Sock`          | `_Sock`          | 内部，前缀 `_`                       |
| `SocketManager` | `_SocketManager` | 可能合并进 Endpoint（表管理逻辑简化后独立类意义不大） |
| `Protocol`      | `_Protocol`      | 内部，前缀 `_`                       |
| `DoIPEndpoint`  | `Endpoint`       | 唯一公开的类                          |

参数名调整：

- `DoIPEndpoint.send(uds)` → `Endpoint.send(payload)`
- `Protocol.encode(uds, ...)` → `_Protocol.encode(payload, ...)`

#### `__init__.py` — 公开 API

```python
from ._transport import Endpoint as Endpoint
from ._config import Config as Config
from ._errors import ProtocolError as ProtocolError

__all__ = ["Endpoint", "Config", "ProtocolError"]
```

### 2.6 关键设计决策

#### 为什么只公开 3 个 API？

| 类               | 能否被外部直接使用？       | 决策     |
|-----------------|------------------|--------|
| `Endpoint`      | 用户直接使用           | **公开** |
| `Config`        | 用户创建配置           | **公开** |
| `ProtocolError` | 用户需要 catch       | **公开** |
| `_Protocol`     | 只被 Endpoint 内部使用 | **内部** |
| `_Sock`         | 只被 Endpoint 内部使用 | **内部** |

用户不需要绕过 `Endpoint` 直接操作 socket 或手拼 DoIP 帧。后续支持"主动连接"模式也只需改内部实现，公开 API 不变。

### 2.7 后续演进

1. 添加"主动连接"模式（tester connect 到 ECU）
2. 自动降级：先主动连，失败后 fallback 到被动监听

---

## 5. autodoip 公开 API

### 默认用法

```python
from autodoip import Endpoint

endpoint = Endpoint(
    ip='198.18.44.1',
    ecus={
        0x1301: ('198.18.44.49', 0),  # port=0，忽略端口校验
        0x1302: ('198.18.44.50', 13400),  # 精确匹配 IP + port
    },
)

endpoint.start()

# 查看连接状态
print(endpoint.connections())
# {0x1301: ('198.18.44.49', 0, True), 0x1302: ('198.18.44.50', 13400, False)}

# 切换到已连接的 ECU
ok = endpoint.select(0x1301)  # → True（已连接，切换成功）
ok = endpoint.select(0x1302)  # → False（未连接，保持当前不变）

# 发送诊断请求
response = endpoint.send(bytes.fromhex('22DC06'))

endpoint.stop()
```

### 公开 API 速览

| 符号                                                           | 说明                                             |
|--------------------------------------------------------------|------------------------------------------------|
| `Endpoint(ip, ecus, ...)`                                    | ip + ecus 必传；port/tester/config 可选             |
| `Endpoint.start()` / `stop()`                                | 启停 DoIP 监听                                     |
| `Endpoint.select(addr) -> bool`                              | 按逻辑地址切换 ECU，成功返 True；未连接返 False 且不切不退          |
| `Endpoint.send(payload) -> bytes`                            | 发送 UDS 载荷，返回响应 bytes                           |
| `Endpoint.connections() -> dict[int, tuple[str, int, bool]]` | `{addr: (ip, port, connected), ...}` 含全部声明 ECU |
| `Config(...)`                                                | @dataclass，传输调优参数，全部有默认值                       |
| `ProtocolError`                                              | Exception，帧校验失败                                |

### 参数归类

```
Endpoint 直接参数（身份/连接）        Config 参数（传输调优）
─────────────────────────────────    ──────────────────────
ip: str              必传，无默认     accept_timeout: 1.5
ecus: dict[int,(s,i)] 必传，无默认    recv_timeout:   3.0
port: int            默认 13400       
tester: int          默认 0x0E80      listen_count:   5
                                      version:        0x02
                                      msg_type:       0x8001
                                      byte_order:     'big'
```

- Endpoint 签名的 `port`/`tester` 有默认值但不在 Config 中——它们是连接身份而非调优参数
- Config 只含行为调优参数，全部有默认值
- `to_bytes(value, byte_order)` 不设默认，`byte_order` 由 Endpoint 从 Config 取后传入
- 内部组件（`_Protocol`、`_SocketManager`）接收具体值，不接触 Config

---

## 6. 已确认决策

| #   | 事项              | 决定                                                                           |
|-----|-----------------|------------------------------------------------------------------------------|
| 1   | 参数命名            | `send(payload)` 不用 `send(uds)`                                               |
| 2   | 类名去前缀           | 全部去掉 `DoIP`/`DoIp`，库名已表态                                                     |
| 3   | 公开 API 数量       | 仅 3 个：`Endpoint` / `Config` / `ProtocolError`                                |
| 4   | 文件拆分            | `_transport.py` 合 4 个类在一个文件，暂不拆                                              |
| 5   | `to_bytes`      | 复制到 autodoip；`byte_order` 参数必传，不设默认                                          |
| 6   | 被动监听 vs 主动连接    | 当前仅实现被动（tester-as-server），主动模式作为后续演进                                         |
| 7   | 参数分类            | 身份参数（ip/ecus/port/tester）在 Endpoint 签名；调优参数在 Config                          |
| 8   | Endpoint 构造     | `Endpoint(ip, ecus, port=13400, tester=0x0E80, config=None)`                 |
| 9   | `ecus` 设计       | `dict[int, tuple[str, int]]` — 逻辑地址→(ECU_IP, ECU_port)，port=0 忽略端口；**必传**    |
| 10  | `select` 语义     | 目标未连接 → 返回 `False`，不切换、不抛异常、不退。上层自行决定                                        |
| 11  | 连接表             | 启动时预建全表，sock=None 占位；accept 匹配成功则填入；始终保留未连上的 ECU                             |
| 12  | accept 过滤       | 收到不在 ecus 表中的 IP → warn + close；在表中未连上的保持 None                               |
| 13  | 重连机制            | `_accept4connect` 在 `start()` 和 `send()` 出错时调用。`_reconnect` 封装清旧→accept→检查逻辑         |
| 13a | send 错误统一        | `ConnectionError/TimeoutError/OSError/AttributeError` 统一走 except → `_reconnect` → 重发一次        |
| 14  | `connections()` | 返回 `{addr: (ip, port, connected), ...}`，含全部声明 ECU 及连接状态                      |
| 15  | 语义边界            | Endpoint 不感知 ECU 名称（`"mcu"`）。Session 自行维护 `name→addr` 映射给 `on(name)` 用       |
| 16  | Config          | 移除 `port`/`tester`/`reconnect_timeout`——身份参数从 Endpoint 取，重连复用 accept_timeout |

---

## 7. 已知硬伤

> 2026-06-03 代码审查发现。2026-06-04 全部修完。

### ~~#1 `_reconnect` 单次 accept~~（已修）

`_reconnect` 改为调用 `_accept4connect`，循环 accept + 填表，自然支持多 ECU 并发重连。

### ~~#2 重复匹配逻辑~~（已修）

`_reconnect` 复用 `_accept4connect`，不再自写 accept 逻辑。

### ~~#3 `send()` 中 `sock` 可能为 None~~（已修）

`sock` 为 None 时 `sock.send()` 抛 `AttributeError`，与网络错误统一走同一个 except → `_reconnect` 抢救。
