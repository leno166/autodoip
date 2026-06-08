"""
模拟 ECU — 主动连接 tester:13400，收到 DoIP 帧后原样回复。
断线自动重连。
"""
import socket
import time

HOST = '127.0.0.1'
PORT = 13400
BYTE_ORDER = 'big'


def recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = b''
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("连接断开")
        buf += chunk
    return buf


def recv_frame(sock: socket.socket) -> bytes | None:
    try:
        header = recv_exact(sock, 8)
    except (TimeoutError, ConnectionError, OSError):
        return None
    payload_len = int.from_bytes(header[4:8], BYTE_ORDER)
    try:
        body = recv_exact(sock, payload_len)
    except (TimeoutError, ConnectionError, OSError):
        return None
    return header + body


def flip_frame(frame: bytes) -> bytes:
    """交换帧头中源/目标地址（字节 8-9 是源，10-11 是目标）。"""
    header = bytearray(frame[:12])
    header[8], header[9], header[10], header[11] = \
        header[10], header[11], header[8], header[9]
    return bytes(header) + frame[12:]


def run():
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((HOST, PORT))
            sock.settimeout(0.5)
            print(f"[已连接] {HOST}:{PORT}")

            while True:
                frame = recv_frame(sock)
                if frame is None:
                    continue
                print(f"  收到: {frame.hex(' ')}")
                reply = flip_frame(frame)
                sock.sendall(reply)
                print(f"  回复: {reply.hex(' ')}")

        except (ConnectionError, OSError) as e:
            print(f"[断开] {e}，1s 后重连...")
            time.sleep(1)
        except KeyboardInterrupt:
            print("\n退出")
            break
        finally:
            try:
                sock.close()
            except Exception:
                pass


if __name__ == '__main__':
    run()