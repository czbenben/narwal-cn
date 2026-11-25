"""产品密钥发现工具 —— 被动监听机器人的 WebSocket 广播，自动提取 product_key。

机器人唤醒时每 1.5 秒广播一次状态，topic 形如 /{product_key}/{device_id}/status/...。
本脚本只监听，不发送任何指令。

用法:
    py tools\\discover_product_key.py <机器人IP> [--wake] [--timeout 30]

    --wake    10 秒无广播时，对所有已知产品密钥发送唤醒/订阅指令
    --timeout 总监听时长（默认 30 秒）

需要:  pip install websockets bbpb
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from narwal_client.const import KNOWN_PRODUCT_KEYS  # noqa: E402
from narwal_client.protocol import ProtocolError, build_frame, parse_frame  # noqa: E402


async def listen(host: str, port: int, timeout: float, wake: bool) -> dict | None:
    uri = f"ws://{host}:{port}"
    print(f"[*] 连接 {uri}，监听广播 {timeout:.0f} 秒...")
    found: dict | None = None
    woke = False

    async with websockets.connect(uri, ping_interval=None) as ws:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            if wake and not woke and (deadline - remaining) > 10:
                # 10 秒无广播 → 用每个已知密钥发 active_robot_publish 唤醒
                woke = True
                print("[*] 10 秒无广播，发送唤醒指令...")
                for key in KNOWN_PRODUCT_KEYS:
                    try:
                        await ws.send(
                            build_frame(f"/{key}/broadcast/common/active_robot_publish", b"\x08\x01")
                        )
                    except Exception:
                        break
                    await asyncio.sleep(0.05)
            try:
                data = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 2.0))
            except asyncio.TimeoutError:
                continue
            if isinstance(data, str):
                continue
            try:
                msg = parse_frame(data)
            except ProtocolError:
                continue
            parts = msg.topic.split("/")
            if len(parts) >= 4 and parts[1]:
                found = {
                    "product_key": parts[1],
                    "device_id": parts[2],
                    "topic": msg.topic,
                }
                break

    return found


async def main() -> int:
    parser = argparse.ArgumentParser(description="Narwal product key discovery")
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=9002)
    parser.add_argument("--wake", action="store_true", help="10 秒无广播时主动唤醒")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    try:
        found = await listen(args.host, args.port, args.timeout, args.wake)
    except Exception as ex:
        print(f"[x] 连接失败: {type(ex).__name__}: {ex}")
        print("    确认 IP 正确、机器人在线、与电脑同一局域网。")
        return 1

    if found:
        print(f"\n[✓] 发现 product_key: {found['product_key']}")
        print(f"    device_id: {found['device_id']}")
        print(f"    topic:     {found['topic']}")
        return 0

    print("\n[x] 超时未收到广播。机器人可能在休眠。")
    print("    在云鲸 App 中唤醒它后重试，或加 --wake 参数主动唤醒。")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
