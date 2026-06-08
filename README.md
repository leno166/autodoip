# autodoip

**autodoip** 是一个零依赖、纯 Python 实现的 DoIP (Diagnostics over IP) 传输层，严格遵循 ISO 13400 标准，适用于汽车 UDS (Unified Diagnostic Services) 诊断通信。

采用 **Tester-as-Server** 拓扑：Tester 监听单一端口，所有 ECU 主动连接至此端口，帧路由通过 DoIP 头中的逻辑地址完成。

## 特性

- 完全符合 ISO 13400 帧格式（版本反码、Payload Type、地址校验）
- 自动连接管理、空闲超时与单次自动重连
- 支持多帧响应迭代（例如 0x7F … 0x78 延迟指示）
- 线程安全的状态切换与收发
- 零外部依赖，仅使用 Python 标准库

## 安装

```bash
pip install autodoip
```

要求 Python ≥ 3.14。

## 快速开始

```python
from autodoip import Endpoint

# 创建端点：监听 192.168.10.1:13400，预定义两个 ECU
endpoint = Endpoint(
    ip="192.168.10.1",
    ecus={
        0x1301: ("192.168.10.10", 0),  # port=0 表示不校验端口
        0x1302: ("192.168.10.20", 13400),
    },
)

endpoint.start()  # 开始接受 ECU 连接

# 查看连接状态
print(endpoint.connections())
# {0x1301: ('192.168.10.10', 0, True), 0x1302: ('192.168.10.20', 13400, False)}

# 切换到已连接的 ECU 并发送诊断请求
endpoint.select(0x1301)
for resp in endpoint.conversation(bytes.fromhex("22FF00")):
    print(resp.hex(" "))
```

## 命令行工具

安装后可直接通过 `python -m autodoip` 发送单次诊断请求：

```bash
python -m autodoip \
    --ecu 0x1301 \
    --ecu-ip 192.168.10.10 \
    --payload 22FF00
```

所有参数说明可通过 `--help` 查看。

## API 概览

| 类/函数                                                         | 说明                                   |
|--------------------------------------------------------------|--------------------------------------|
| `Endpoint(ip, ecus, port=13400, tester=0x0E80, config=None)` | DoIP 端点，`ip` 与 `ecus` 必填             |
| `Endpoint.start()`                                           | 启动监听，幂等；需重启则新建实例                     |
| `Endpoint.select(addr) -> bool`                              | 切换当前 ECU；未连接或操作冲突时返回 `False` 且不改变选中  |
| `Endpoint.conversation(payload) -> Iterator[bytes]`          | 发送 UDS 载荷，返回响应迭代器。连接中断自动重连一次         |
| `Endpoint.current -> int \| None`                            | 当前选中的 ECU 逻辑地址                       |
| `Endpoint.connections() -> dict`                             | `{addr: (ip, port, connected), ...}` |
| `Config(...)`                                                | 传输调优参数（见下方）                          |
| `ProtocolError`                                              | 帧校验失败时抛出的异常                          |

## 配置

```python
from autodoip import Config, Endpoint

config = Config(
    accept_timeout=1.5,   # accept 超时 (秒)
    p6_timeout=0.05,      # 单次 recv 超时 (秒)
    p6_star_timeout=5.0,  # 整体接收窗口超时 (秒)
    listen_count=5,       # socket listen backlog
    version=0x02,         # DoIP 协议版本
    msg_type=0x8001,      # Payload Type
    byte_order="big",     # 字节序 ("big" / "little")
)

# 将配置传入 Endpoint
endpoint = Endpoint(
    ip="192.168.10.1",
    ecus={
        0x1301: ("192.168.10.10", 0),
        0x1302: ("192.168.10.20", 13400),
    },
    config=config,
)
```

## 许可证

MIT License
