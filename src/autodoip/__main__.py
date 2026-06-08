"""
@文件: __main__.py
@作者: 雷小鸥
@日期: 2026/6/8 21:01
@许可: MIT License
@描述:
    autodoip 命令行工具：发送 UDS 载荷并打印十六进制响应。
    用法示例:
      python -m autodoip --ecu 0x1001 --ecu-ip 192.168.1.10 --payload 1001

@版本: Version 0.1
"""
import argparse
import sys
from autodoip import Endpoint, Config, ProtocolError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DoIP diagnostic requester (ISO 13400 Tester-as-Server)"
    )
    parser.add_argument("--ip", default="0.0.0.0", help="监听 IP (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=13400, help="监听端口 (默认 13400)")
    parser.add_argument("--ecu", required=True, help="ECU 逻辑地址 (十六进制, 如 0x1001)")
    parser.add_argument("--ecu-ip", required=True, help="ECU 连接 IP")
    parser.add_argument("--payload", required=True, help="UDS 载荷 (十六进制, 如 1001)")
    parser.add_argument("--tester", default="0E80", help="Tester 逻辑地址 (十六进制)")
    parser.add_argument("--timeout", type=float, default=1.5, help="accept 超时 (秒)")
    args = parser.parse_args()

    try:
        ecu_addr = int(args.ecu, 16)
        payload = bytes.fromhex(args.payload)
        tester_addr = int(args.tester, 16)
    except ValueError as e:
        print(f"参数解析失败: {e}")
        sys.exit(1)

    config = Config(accept_timeout=args.timeout)
    endpoint = Endpoint(
        ip=args.ip,
        ecus={ecu_addr: (args.ecu_ip, 0)},   # port=0 忽略端口校验
        port=args.port,
        tester=tester_addr,
        config=config,
    )

    try:
        endpoint.start()
    except RuntimeError as e:
        print(f"启动失败: {e}")
        sys.exit(1)

    if not endpoint.select(ecu_addr):
        print(
            f"ECU 0x{ecu_addr:04X} 未连接。请确保 ECU 已主动连接到 "
            f"{args.ip}:{args.port}"
        )
        sys.exit(1)

    try:
        for response in endpoint.conversation(payload):
            print(response.hex(" "))
    except (ProtocolError, ConnectionError) as e:
        print(f"通信错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
