"""连接验证工具 —— 检查云鲸扫地机的本地 WebSocket (9002) 是否可用。

用法:
    py tools\\probe_robot.py <机器人IP> [--product-key 产品密钥]

示例:
    py tools\\probe_robot.py 192.168.1.50
    py tools\\probe_robot.py 192.168.1.50 --product-key QoEsI5qYXO

需要:  pip install websockets bbpb
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from narwal_client import NarwalClient  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Narwal robot connection probe")
    parser.add_argument("host", help="机器人 IP 地址")
    parser.add_argument("--port", type=int, default=9002)
    parser.add_argument("--product-key", default=None, help="指定产品密钥（默认自动探测）")
    parser.add_argument("--timeout", type=float, default=20.0, help="发现超时（秒）")
    args = parser.parse_args()

    prefix = f"/{args.product_key}" if args.product_key else None
    client = NarwalClient(host=args.host, port=args.port, topic_prefix=prefix)

    print(f"[*] 连接 ws://{args.host}:{args.port} ...")
    try:
        await client.connect()
    except Exception as ex:
        print(f"[x] 连接失败: {type(ex).__name__}: {ex}")
        print("    端口不通。确认 IP 正确、机器人在线、与电脑同一局域网。")
        return 1

    print("[*] 已连接，正在发现设备（可能需要先唤醒机器人）...")
    try:
        device_id = await client.discover_device_id(timeout=args.timeout)
    except Exception as ex:
        print(f"[x] 设备发现超时: {type(ex).__name__}: {ex}")
        print("    机器人可能休眠或该固件不开放本地协议。")
        print("    请在云鲸 App 里把机器人唤醒（开始/暂停一次清扫）后重试。")
        await client.disconnect()
        return 2

    await client.drain_ws_buffer()

    try:
        info = await client.get_device_info()
    except Exception as ex:
        print(f"[!] get_device_info 失败: {ex}")
        info = None

    print("[+] 连接成功")
    if info:
        print(f"    product_key : {info.product_key or client.topic_prefix.lstrip('/')}")
        print(f"    device_id   : {info.device_id or device_id}")
        print(f"    固件版本    : {info.firmware_version or '未知'}")
    else:
        print(f"    device_id   : {device_id}")
        print(f"    product_key : {client.topic_prefix.lstrip('/')}")

    # 订阅广播 5 秒，拿实时状态（电量、工作状态）
    try:
        await client.subscribe_to_topics(duration=60)
        await asyncio.sleep(5)
        state = client.state
        if state.battery_level:
            print(f"    电量        : {state.battery_level}%")
        print(f"    工作状态    : {state.working_status.name}")
    except Exception as ex:
        print(f"    (状态读取跳过: {ex})")

    await client.disconnect()
    print("\n[✓] 该机器人支持本地协议，可以接入 Home Assistant。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
