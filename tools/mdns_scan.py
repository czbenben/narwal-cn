"""mDNS 扫描工具 —— 在局域网上发现云鲸扫地机（_narwal_sweeper._tcp.local）。

云鲸机器人通过 mDNS 广播自身服务（官方 App 的 device_channel 即通过它发现设备）。
本脚本发送 PTR 查询并解析应答，打印每台机器人的 IP 和端口。

用法:
    py tools\\mdns_scan.py [--timeout 6]

无任何第三方依赖。
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
SERVICE = "_narwal_sweeper._tcp.local"


def encode_name(name: str) -> bytes:
    out = b""
    for part in name.rstrip(".").split("."):
        out += bytes([len(part)]) + part.encode()
    return out + b"\x00"


def decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a possibly-compressed DNS name. Returns (name, next_offset)."""
    labels = []
    jumped = False
    end = offset
    while True:
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end = offset
            break
        if length & 0xC0 == 0xC0:
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            continue
        labels.append(data[offset + 1 : offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length
        if not jumped:
            end = offset
    return ".".join(labels), end


def parse_response(data: bytes) -> dict:
    """Extract PTR/SRV/TXT/A records from a DNS response."""
    result: dict = {"ptr": [], "srv": [], "a": [], "txt": []}
    if len(data) < 12:
        return result
    qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
    offset = 12
    for _ in range(qd):
        _, offset = decode_name(data, offset)
        offset += 4
    for _ in range(an + ns + ar):
        try:
            owner, offset = decode_name(data, offset)
            rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[offset : offset + 10])
            offset += 10
            rdata_offset = offset
            offset += rdlen
            if rtype == 12:  # PTR
                name, _ = decode_name(data, rdata_offset)
                result["ptr"].append(name)
            elif rtype == 33:  # SRV
                if rdlen >= 6:
                    port = struct.unpack(">H", data[rdata_offset + 4 : rdata_offset + 6])[0]
                    target, _ = decode_name(data, rdata_offset + 6)
                    result["srv"].append({"port": port, "target": target})
            elif rtype == 16:  # TXT — list of <len><bytes> key=value pairs
                strings = []
                p = rdata_offset
                end = rdata_offset + rdlen
                while p < end:
                    ln = data[p]
                    strings.append(data[p + 1 : p + 1 + ln].decode("utf-8", "replace"))
                    p += 1 + ln
                if strings:
                    result["txt"].append({"owner": owner, "strings": strings})
            elif rtype == 1 and rdlen == 4:  # A
                result["a"].append(socket.inet_ntoa(data[rdata_offset : rdata_offset + 4]))
        except Exception:
            break
    return result


def build_query(name: str, qu: bool) -> bytes:
    # Header: id=0, flags=0 (query), qd=1 — standard mDNS query
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    # QTYPE=PTR(12); QCLASS=IN(1)。qu=True 时置 0x8000 要求单播应答
    qclass = 0x8001 if qu else 1
    question = encode_name(name) + struct.pack(">HH", 12, qclass)
    return header + question


def main() -> int:
    parser = argparse.ArgumentParser(description="Narwal mDNS scanner")
    parser.add_argument("--timeout", type=float, default=6.0, help="监听秒数")
    parser.add_argument("--service", default=SERVICE, help="mDNS 服务类型")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    bound_5353 = False
    try:
        # 优先绑定 5353 并加入组播组：多播应答只会发到 5353 端口
        try:
            sock.bind(("", MDNS_PORT))
            bound_5353 = True
        except OSError:
            # 5353 被占用（如系统 mDNS 服务）→ 退到临时端口 + QU 单播查询
            sock.bind(("", 0))
        mreq = socket.inet_aton(MDNS_ADDR) + socket.inet_aton("0.0.0.0")
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass

        query = build_query(args.service, qu=not bound_5353)
        print(f"[*] 查询 {args.service}，监听 {args.timeout:.0f} 秒...")
        # 发两次查询，提高可靠性
        for _ in range(2):
            sock.sendto(query, (MDNS_ADDR, MDNS_PORT))
            time.sleep(0.3)

        found: dict[str, dict] = {}
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            resp = parse_response(data)
            for srv in resp["srv"]:
                key = srv["target"]
                entry = found.setdefault(key, {"host": srv["target"], "port": srv["port"], "ips": set(), "txt": []})
                entry["port"] = srv["port"]
            for txt in resp["txt"]:
                entry = found.setdefault(txt["owner"], {"host": txt["owner"], "port": None, "ips": set(), "txt": []})
                for s in txt["strings"]:
                    if s not in entry["txt"]:
                        entry["txt"].append(s)
            for name in resp["ptr"]:
                entry = found.setdefault(name, {"host": name, "port": None, "ips": set(), "txt": []})
            for ip in resp["a"]:
                # A 记录归属哪个实例不易精确关联，附加到最近条目 + 按源地址兜底
                if found:
                    for e in found.values():
                        e["ips"].add(ip)
                else:
                    found[ip] = {"host": ip, "port": None, "ips": {ip}}

        if not found:
            print("[x] 未发现云鲸设备。")
            print("    可能原因：机器人未联网/休眠，或电脑与机器人不在同一网段（组播不通）。")
            print("    不影响使用——可以直接用 IP 手动添加。")
            return 1

        print(f"[✓] 发现 {len(found)} 台设备:")
        for e in found.values():
            port = e["port"] or "?"
            ips = ", ".join(sorted(e["ips"])) or "?"
            print(f"    {e['host']}  ->  {ips}  端口 {port}")
            for s in e.get("txt", []):
                print(f"        TXT: {s}")
        print("\n如果端口是 9002，说明该机器人支持本地协议，可以接入 Home Assistant。")
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
