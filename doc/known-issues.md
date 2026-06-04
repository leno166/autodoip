# 已知问题 & 技术债务

> 2026-06-04 代码审查记录。标记为"未来修改"的问题汇总。

---

## 1. recv_frame 无载荷大小上限

**文件**: `src/autodoip/_frame.py:27-33`

**描述**: `recv_frame()` 直接信任帧头中的 `payload_length` 并调用 `recv_exact(sock, payload_length)`，无上限检查。恶意或故障 ECU 发送声称载荷 4GB 的帧头会导致内存耗尽（DoS）。

**状态**: 🔵 未来修改

---

## 2. Config 无输入校验

**文件**: `src/autodoip/_config.py`

**描述**: `Config` 是 dataclass，所有字段无校验。`accept_timeout` / `recv_timeout` 可传负数，`version` 可传无效值等。

**状态**: 🔵 未来修改

---

## 3. 中英文混用

**涉及文件**: 全部

**描述**: 
- `_transport.py` 中 `_accept4connect()` 的 `RuntimeError('没有任何连接端口，直接退出')` 为中文
- 部分注释中文、部分英文，风格不统一

**决策**: 未来所有字符串描述（异常信息、日志、注释）均使用中文。

**状态**: 🔵 未来逐步统一

---

## 4. ~~日志等级与副作用优化~~（已修 ✅）

**文件**: `src/autodoip/_transport.py` — `conversation()` 方法

**描述**: `conversation()` 中两处 `logger.debug()` 包含 `.hex(' ')` 调用，每次都会执行昂贵的 hex 转换。已添加 `logger.isEnabledFor(logging.DEBUG)` 守卫条件。

**状态**: ✅ 已修复（2026-06-04）

---

## 5. encode / decode 字节拼接优化

**文件**: `src/autodoip/_transport.py:56-68` (`_Protocol.encode`)

**描述**: `encode()` 中多次创建小 bytes 对象并拼接：

```python
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
```

每次 `to_bytes()` 创建一个临时 bytes 对象，`+` 拼接再创建新对象。高频调用时（如压力测试 35+ TPS）可考虑用 `bytearray` 预分配 + 切片写入，减少临时对象分配。

**状态**: 🔵 后续研究。目前无性能瓶颈，不修改。

---

## 6. 已讨论且确认不修改的设计决策

以下问题经过讨论，确认为设计意图，**不做修改**：

| # | 问题 | 决策 |
|---|------|------|
| 1 | `conversation()` 生成器持有锁跨 `yield` | 设计意图。上层多路复用 + 下层单一 ECU，必须整体加锁。35 TPS ≈ ECU 运行时间，锁粒度不做调整 |
| 2 | `_accept4connect()` 持锁阻塞 `start()` | 设计意图。初始化时间很短，无影响 |
| 3 | `_reconnect()` 异常类型不一致 | 设计意图。零连接（`RuntimeError`）说明后续流程无意义，及早抛出；目标不在则 `TimeoutError`，区分语义 |
| 4 | `conversation()` 用 `AttributeError` 做空 sock 检测 | 保持统一异常处理路径。当前无效率问题，不修改 |
| 5 | `send_loop` 可能死循环 | 测试代码，测试环境默认不会出现延迟指示死循环 |