# autodoip 解耦设计方案

> 将 `terminal` 项目中的 DoIP 传输层从 Diag 库中剥离，使其成为独立的 Python 包 `autodoip`。

---

## 1. 现状分析

### 1.1 Diag 库文件结构

```
terminal/src/workspace/module/Diag/
├── __init__.py       # 导出 Session, Service, UdsResponse + Config
├── __main__.py       # 使用演示
├── doip.py           # DoIP 层: Sock → SocketManager → Protocol → DoIPEndpoint
├── uds.py            # UDS 层:  KeepAlive + Session
├── service.py        # 配置 dataclass + Service 业务层
├── helper.py         # 工具:   recv_exact, recv_frame, to_bytes
├── response.py       # UdsResponse 数据类
└── errors.py         # DoIpProtocolError
```

### 1.2 分层架构

```
Service (service.py)         ← UDS 标准方法（会话/安全/读写/例程）
Session (uds.py)             ← 持有 Endpoint + KeepAlive，ECU 路由
KeepAlive (uds.py)           ← 后台 TesterPresent 心跳线程
──────────────────────────────────────────
Endpoint (autodoip)          ← SocketManager + Protocol + Lock + 自动重连
Protocol (autodoip)          ← DoIP 帧编解码（ISO 13400）
SocketManager (autodoip)     ← Socket 生命周期 + 连接表 + 重连
Sock (autodoip)              ← 单 socket 封装
recv_frame                   ← 帧级 IO 收取
ProtocolError                ← 协议异常
──────────────────────────────────────────
UdsResponse (response.py)    ← 正/负响应解析 + NRC 描述
helper.to_bytes              ← 通用类型→bytes 转换
```

**分割线上下就是 DoIP 和 UDS 的天然边界。**

### 1.3 依赖关系图

```
                                 ┌──────────────┐
                                 │  stdlib only │
                                 └──────┬───────┘
                          ┌─────────────┼─────────────┐
                          │             │             │
                    errors.py      helper.py     helper.py
                  (ProtocolError  (recv_exact,   (to_bytes)
                      )            recv_frame)       │
                          │             │             │
                          └──────┬──────┘             │
                                 │                    │
                              doip.py                  │
                     (_Sock, _SocketManager,           │
                      _Protocol, Endpoint)             │
                                 │                    │
                          ┌──────┴──────┐             │
                          │             │             │
                        uds.py     service.py         │
                     (KeepAlive,  (Config,            │
                       Session)    KeepAliveConfig,   │
                                   RetryConfig,       │
                                   Service)           │
                          │             │             │
                          └──────┬──────┘             │
                                 │                    │
                           response.py ───────────────┘
                          (UdsResponse)
```

---

## 2. 解耦方案

### 2.1 提取原则

| 原则 | 说明 |
|------|------|
| DoIP 不感知 UDS | `autodoip` 只负责传输层：连接管理、帧编解码、收发。参数名用 `payload` 而非 `uds` |
| 零外部依赖 | 仅 Python 标准库 `socket` + `threading` + `dataclasses` |
| 最小公开 API | 只暴露 `Endpoint` / `Config` / `ProtocolError`，内部实现随时可改 |
| 库名即前缀 | 类名去掉 `DoIP`/`DoIp`，`from autodoip import Endpoint` 语义已足够 |

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

> Config 存储方案（单例 vs `self._config`）暂搁置，后续讨论。

### 2.4 autodoip 包结构

```
src/autodoip/
├── __init__.py        # 公开 3 个: Endpoint, Config, ProtocolError
├── _frame.py          # recv_exact + recv_frame（帧级 IO，内部）
├── _config.py         # Config 数据类（公开内容，模块名私有）
├── _errors.py         # ProtocolError（公开内容，模块名私有）
└── _transport.py      # Endpoint（公开）+ _Protocol + _SocketManager + _Sock（内部）
└── _util.py            # to_bytes — 类型→bytes 转换（内部）
```

### 2.4 命名对照

```
现名（terminal 旧）          改名（autodoip 新）           可见性
────────────────────────     ──────────────────────       ──────
DoIPEndpoint          →      Endpoint                     公开
DoIPConfig            →      Config                       公开
DoIpProtocolError     →      ProtocolError                公开
Protocol              →      _Protocol                    内部
SocketManager         →      _SocketManager               内部
Sock                  →      _Sock                        内部
recv_exact            →      recv_exact                   内部
recv_frame            →      recv_frame                   内部
```

### 2.5 各模块内容

#### `_frame.py` — 帧 IO 工具

| 函数 | 用途 |
|------|------|
| `recv_exact(sock, size) -> bytes` | 精确收取 size 字节，连接关闭时抛 `ConnectionError` |
| `recv_frame(sock) -> bytes` | 收取完整 DoIP 帧（8 字节头 + N 字节载荷） |

从 `helper.py` 原样移入，无修改。

#### `_util.py` — 类型转换

```python
def to_bytes(value, byte_order: Literal['little', 'big']) -> bytes:
    """统一类型 → bytes。支持 bytes/bytearray/str/int/None。
    byte_order 不设默认值，由调用方显式传入（来自 Config.byte_order）。"""
```

从 `helper.py` 复制一份，去掉 `byte_order` 的默认值。autodoip 内部使用（后续主动连接模式需要 hex 请求转换），也让 autodoip 完全自给自足。terminal 侧保留原有的不动。

#### `_config.py` — 传输调优参数

```python
@dataclass
class Config:
    """DoIP 传输层调优参数（不含身份参数：ip/port/tester/ecus）"""
    accept_timeout: float = 1.5
    recv_timeout: float = 3.0
    reconnect_timeout: float = 5.0
    listen_count: int = 10
    version: int = 0x02
    msg_type: int = 0x8001
    byte_order: Literal['little', 'big'] = 'big'
```

从 `service.py` 移入，去掉 `DoIP` 前缀，移除 `port` 和 `tester`（这两个是 Endpoint 的身份参数，在 Endpoint 签名中直接声明默认值）。原 `service.py` 改为 `from autodoip import Config as DoIPConfig`。

#### `_errors.py` — 异常

```python
class ProtocolError(Exception):
    """DoIP 协议层错误"""
```

从 `errors.py` 移入，`DoIpProtocolError` → `ProtocolError`。

#### `_transport.py` — 传输层

从 `doip.py` 移入，含 4 个类。核心变化：

**1. Endpoint 构造函数**

```python
class Endpoint:
    def __init__(self,
                 ip: str,                           # 本地监听 IP，必传，无默认
                 ecus: dict[int, tuple[str, int]],  # 逻辑地址 → (ECU_IP, ECU_port)，port=0 忽略
                 port: int = 13400,                 # 本地监听端口
                 tester: int = 0x0E80,              # tester 逻辑地址
                 config: Config | None = None):     # 传输调优，可选
        cfg = config or Config()
        self._ip = ip
        self._port = port
        self._tester = tester
        self._ecus = ecus.copy()
        self._current: int | None = None
        ...
```

**参数分类**

| 类别 | 参数 | 默认值 | 归属 |
|------|------|--------|------|
| **身份/连接** | `ip` | 无（必传） | Endpoint 签名 |
| | `ecus` | 无（必传） | Endpoint 签名 |
| | `port` | 13400 | Endpoint 签名 |
| | `tester` | 0x0E80 | Endpoint 签名 |
| **传输调优** | `accept_timeout` | 1.5 | Config |
| | `recv_timeout` | 3.0 | Config |
| | `reconnect_timeout` | 5.0 | Config |
| | `listen_count` | 10 | Config |
| | `version` | 0x02 | Config |
| | `msg_type` | 0x8001 | Config |
| | `byte_order` | 'big' | Config |

**2. 连接表内部结构**

启动时用 `ecus` 预建全表，所有 ECU 的 socket 初始为 `None`。accept 时按 IP 匹配填入实际 socket。

```python
# 内部数据结构
self._socks: dict[int, _Sock | None] = {addr: None for addr in ecus}
# 0x1301 → _Sock(sock)    ← 已连接
# 0x1302 → None           ← 声明了但还没连上
```

**3. accept 行为（`_accept_once`）**

对每个 accept 到的连接 `(sock, (src_ip, src_port))`：
1. 遍历 `_ecus` 表，按 IP 匹配（port 校验：0 跳过，非 0 需精确匹配）
2. **匹配成功** → `self._socks[addr] = _Sock(sock)`，连接就位
3. **匹配失败**（不在 ecus 表中）→ `logger.warning` + 关闭该 socket，不加入路由表
4. 声明了但未连上的 ECU 保持 `None`，**不删除、不报错**

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

**5. reconnect 行为**

```python
def reconnect(self, timeout: float) -> None:
    addr = self._current
    ip, port = self._ecus[addr]
    # accept 等待重连...
    sock, (src_ip, src_port) = self._server.accept()
    # 收到的 IP 必须在 ecus 表中（任意 addr 都行）
    matched = next((a for a, (e_ip, e_port) in self._ecus.items()
                    if e_ip == src_ip and (e_port == 0 or e_port == src_port)), None)
    if matched is None:
        sock.close()
        raise ConnectionError(f"重连收到非预期 IP: {src_ip}，不在 ECU 表中")
    # 更新 socket
    self._socks[matched] = _Sock(sock)
```

- 重连时严格校验：收到 IP 不在 ecus 表中 → 直接拒绝 + 抛异常
- 与 accept 不同：accept 宽松（只是 warn+close），reconnect 严格（直接报错，因为已经在通信中）

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

| 原名称 | 新名称 | 说明 |
|--------|--------|------|
| `Sock` | `_Sock` | 内部，前缀 `_` |
| `SocketManager` | `_SocketManager` | 可能合并进 Endpoint（表管理逻辑简化后独立类意义不大） |
| `Protocol` | `_Protocol` | 内部，前缀 `_` |
| `DoIPEndpoint` | `Endpoint` | 唯一公开的类 |

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

#### 决策 A：`Config` 归 autodoip 还是 terminal？

**归 autodoip。** 理由：
- 8 个字段全部是 DoIP 传输参数
- `terminal` 中 `Session.__init__` 将其解包后传入 `Endpoint` 构造器——Session 只是透传
- 移入 `autodoip` 后，用户可以 `from autodoip import Config` 直接用，无需装 terminal

#### 决策 B：`helper.py` 怎么拆？

| 函数 | 归属 | 理由 |
|------|------|------|
| `recv_exact` | → `autodoip._frame` | DoIP 帧收取需要 |
| `recv_frame` | → `autodoip._frame` | DoIP 帧收取需要 |
| `to_bytes` | 复制到 `autodoip._util`，同时保留在 `terminal` | autodoip 自给自足，不依赖 terminal 的工具函数 |

#### 决策 C：`KeepAlive` 是否随 DoIP 走？

**不留。** `KeepAlive` 用的是 UDS TesterPresent（0x3E 00），属于 UDS 层概念。它的构造函数接收 `fn: Callable[[bytes], bytes]`，已经与 Endpoint 解耦——不需要知道传输细节。

#### 决策 D：为什么只公开 3 个 API？

| 类 | 能否被外部直接使用？ | 决策 |
|----|-------------------|------|
| `Endpoint` | Session 创建它、用户也能直接用 | **公开** |
| `Config` | Session 解包配置、用户创建配置 | **公开** |
| `ProtocolError` | Endpoint 抛出、用户需要 catch | **公开** |
| `_Protocol` | 只被 Endpoint 内部使用 | **内部** |
| `_SocketManager` | 只被 Endpoint 内部使用 | **内部** |
| `_Sock` | 只被 _SocketManager 内部使用 | **内部** |

用户不会绕过 `Endpoint` 直接操作 socket 表或手拼 DoIP 帧。日后如果要支持"主动连接"模式（tester-as-client），也只需改 `_transport.py` 内部实现，公开 API 不变。

### 2.7 terminal 改动清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `doip.py` | **删除** | ~297 行移入 autodoip |
| `helper.py` | 删除 `recv_exact`、`recv_frame`，保留 `to_bytes` | 删 ~28 行 |
| `errors.py` | 删除 `DoIpProtocolError` | 删 2 行 |
| `service.py` | 删除 `DoIPConfig` dataclass，改为 `from autodoip import Config as DoIPConfig` | Config 字段减少（port/tester 已移除） |
| `uds.py` | Session 适配新的 Endpoint 接口 | 主要改动点（见下） |
| `__init__.py` | 可选 re-export 保持兼容 | 改 1 行 |
| 新增 `pyproject.toml` | 添加 `autodoip` 依赖 | 新增 |

**Session 适配要点：**

```python
# 旧：Session 持有 ip/port/tester，构造 DoIPEndpoint 时逐参数传入
# 旧：ecus 格式 {name: (ip, logical_addr)}
# 新：
class Session:
    def __init__(self, ip, ecus, config=None, keepalive=None):
        # ecus 仍是 {name: (ip, logical_addr)}，Session 内部转换格式传给 Endpoint
        autodoip_ecus = {addr: (ip, 0) for name, (ip, addr) in ecus.items()}
        self._endpoint = Endpoint(ip=ip, ecus=autodoip_ecus, config=config)
        # Session 额外维护 name → addr 映射用于 on(name) 方法

    def on(self, name: str) -> Self:
        ip, addr = self._ecus[name]
        self._endpoint.select(addr)   # 只传逻辑地址，Endpoint 内部查表
        ...
```

---

## 3. 包配置

### 3.1 pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "autodoip"
version = "0.1.0"
description = "DoIP (Diagnostics over IP) transport layer for automotive UDS communication — ISO 13400"
readme = "README.md"
requires-python = ">=3.10"
license = {text = "MIT"}
classifiers = [
    "Programming Language :: Python :: 3",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
    "Topic :: System :: Networking",
]
dependencies = []

[project.urls]
Homepage = "https://github.com/leno166/autodoip"
```

### 3.2 terminal 新增依赖

```toml
# terminal/pyproject.toml
dependencies = [
    "autodoip>=0.1.0",
]
```

---

## 4. 迁移路线

### Phase 1：创建 autodoip 包（本次）

1. 将 `doip.py` / `helper.recv_*` / `errors.py` / `DoIPConfig` 移入 `autodoip/src/autodoip/`
2. 按命名对照表改名、调整 import、去前缀
3. `__init__.py` 只暴露 3 个 API
4. 本地 `pip install -e .` 验证可导入
5. 发布到 PyPI（先 test.pypi.org）

### Phase 2：terminal 适配

1. `terminal` 添加 `autodoip` 依赖
2. 删除已迁移的代码，改为 import
3. 运行现有诊断流程验证功能正常

### Phase 3：后续演进

1. 添加"主动连接"模式（tester connect 到 ECU，先连接后监听）
2. 自动降级：先主动连，失败后 fallback 到被动监听
3. API 文档 + CI 自动发布

---

## 5. autodoip 公开 API

### 默认用法

```python
from autodoip import Endpoint

endpoint = Endpoint(
    ip='198.18.44.1',
    ecus={
        0x1301: ('198.18.44.49', 0),     # port=0，忽略端口校验
        0x1302: ('198.18.44.50', 13400), # 精确匹配 IP + port
    },
)

endpoint.start()

# 查看连接状态
print(endpoint.connections())
# {0x1301: ('198.18.44.49', 0, True), 0x1302: ('198.18.44.50', 13400, False)}

# 切换到已连接的 ECU
ok = endpoint.select(0x1301)      # → True（已连接，切换成功）
ok = endpoint.select(0x1302)      # → False（未连接，保持当前不变）

# 发送诊断请求
response = endpoint.send(bytes.fromhex('22DC06'))

endpoint.stop()
```

### 公开 API 速览

| 符号 | 说明 |
|------|------|
| `Endpoint(ip, ecus, ...)` | ip + ecus 必传；port/tester/config 可选 |
| `Endpoint.start()` / `stop()` | 启停 DoIP 监听 |
| `Endpoint.select(addr) -> bool` | 按逻辑地址切换 ECU，成功返 True；未连接返 False 且不切不退 |
| `Endpoint.send(payload) -> bytes` | 发送 UDS 载荷，返回响应 bytes |
| `Endpoint.connections() -> dict[int, tuple[str, int, bool]]` | `{addr: (ip, port, connected), ...}` 含全部声明 ECU |
| `Config(...)` | @dataclass，传输调优参数，全部有默认值 |
| `ProtocolError` | Exception，帧校验失败 |

### 参数归类

```
Endpoint 直接参数（身份/连接）        Config 参数（传输调优）
─────────────────────────────────    ──────────────────────
ip: str              必传，无默认     accept_timeout: 1.5
ecus: dict[int,(s,i)] 必传，无默认    recv_timeout:   3.0
port: int            默认 13400       reconnect_timeout: 5.0
tester: int          默认 0x0E80      listen_count:   10
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

| # | 事项 | 决定 |
|----|------|------|
| 1 | 参数命名 | `send(payload)` 不用 `send(uds)` |
| 2 | 类名去前缀 | 全部去掉 `DoIP`/`DoIp`，库名已表态 |
| 3 | 公开 API 数量 | 仅 3 个：`Endpoint` / `Config` / `ProtocolError` |
| 4 | 文件拆分 | `_transport.py` 合 4 个类在一个文件，暂不拆 |
| 5 | `to_bytes` | 复制到 autodoip；`byte_order` 参数必传，不设默认 |
| 6 | 被动监听 vs 主动连接 | 当前仅实现被动（tester-as-server），主动模式作为后续演进 |
| 7 | 参数分类 | 身份参数（ip/ecus/port/tester）在 Endpoint 签名；调优参数在 Config |
| 8 | Endpoint 构造 | `Endpoint(ip, ecus, port=13400, tester=0x0E80, config=None)` |
| 9 | `ecus` 设计 | `dict[int, tuple[str, int]]` — 逻辑地址→(ECU_IP, ECU_port)，port=0 忽略端口；**必传** |
| 10 | `select` 语义 | 目标未连接 → 返回 `False`，不切换、不抛异常、不退。上层自行决定 |
| 11 | 连接表 | 启动时预建全表，sock=None 占位；accept 匹配成功则填入；始终保留未连上的 ECU |
| 12 | accept 过滤 | 收到不在 ecus 表中的 IP → warn + 关闭该 socket；在表中但未连上的保持 None 不报错 |
| 13 | reconnect 过滤 | 收到不在 ecus 表中的 IP → 直接拒绝 + 抛 ConnectionError |
| 14 | `connections()` | 返回 `{addr: (ip, port, connected), ...}`，含全部声明 ECU 及连接状态 |
| 15 | 语义边界 | Endpoint 不感知 ECU 名称（`"mcu"`）。Session 自行维护 `name→addr` 映射给 `on(name)` 用 |
| 16 | Config | 移除 `port`/`tester`，这两个身份参数统一从 Endpoint 取 |