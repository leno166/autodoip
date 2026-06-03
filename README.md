# autodoip

DoIP (Diagnostics over IP) transport layer for automotive UDS communication — ISO 13400.

## Installation

```bash
pip install autodoip
```

## Quick Start

```python
from autodoip import Endpoint

# 创建端点 — 监听 198.18.44.1:13400，等待 ECU 连接
endpoint = Endpoint(
    ip='198.18.44.1',
    ecus={
        0x1301: ('198.18.44.49', 0),     # port=0 忽略端口校验
        0x1302: ('198.18.44.50', 13400), # 精确匹配 IP + port
    },
)

endpoint.start()

# 查看连接状态
print(endpoint.connections())
# {0x1301: ('198.18.44.49', 0, True), 0x1302: ('198.18.44.50', 13400, False)}

# 切换到已连接的 ECU，发送诊断请求
endpoint.select(0x1301)
response = endpoint.send(bytes.fromhex('22DC06'))
print(response.hex(' '))

endpoint.stop()
```

## API

| 符号 | 说明 |
|------|------|
| `Endpoint(ip, ecus, port, tester, config)` | DoIP 端点。`ip` + `ecus` 必传 |
| `Endpoint.start()` | 启动监听，幂等 |
| `Endpoint.stop()` | 关闭所有连接 |
| `Endpoint.select(addr) -> bool` | 切换 ECU，成功返 `True`；未连接返 `False` 不切不退 |
| `Endpoint.send(payload) -> bytes` | 发送 UDS 载荷，返回响应。通信中断自动重连一次 |
| `Endpoint.connections() -> dict` | `{addr: (ip, port, connected), ...}` |
| `Config(accept_timeout=..., recv_timeout=..., ...)` | 传输调优参数，全部有默认值 |
| `ProtocolError` | 帧格式校验失败 |

## Configuration

```python
from autodoip import Config

Config(
    accept_timeout=1.5,   # accept 超时 (s)
    recv_timeout=3.0,     # recv 超时 (s)
    listen_count=5,       # socket backlog
    version=0x02,         # DoIP 协议版本
    msg_type=0x8001,      # Payload Type
    byte_order='big',     # 字节序
)
```

## Requirements

- Python >= 3.14
- Zero external dependencies (stdlib only)

## License

MIT