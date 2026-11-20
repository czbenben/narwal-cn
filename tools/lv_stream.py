#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云鲸国行视频拉流客户端（阿里云 LinkVisual 通道）

完整链路（2026-01 逆向自 com.narwel.narwelrobots 2.11.80 + 真机抓包验证）：

  1. Narwal 云登录            -> session.access_token (test_nc.NarwalCloud)
  2. Narwal authCode          -> user UUID, 即 LinkVisual authCode
     GET  {media_server}/third-party-cloud-dock/linkVisual/v1/authCode?deviceKey=<did>
  3. 飞燕三方登录 loginbyoauth -> sid
     POST https://living-account.cn-shanghai.aliyuncs.com/api/prd/loginbyoauth.json
     body: loginByOauthRequest={"country":"CN","authCode":<uuid>,"oauthPlateform":23,
           "oauthAppKey":<appKey>,"riskControlInfo":{...}}
  4. createSessionByAuthCode  -> iotToken + identityId
     POST https://api.link.aliyun.com/account/createSessionByAuthCode
     {"request":{"authCode":<sid>,"accountType":"OA_SESSION","appKey":<appKey>}} apiVer=1.0.4
  5. vision/customer/stream/query -> relayUrl(RTMP) + relayDecryptKey{key,iv} + p2pInfo
     POST https://api.link.aliyun.com/vision/customer/stream/query  apiVer=2.2.0
     {"iotId": <deviceName>+"000000", "streamType":0, "clientType":"Android",
      "relayEncrypted":true, "forceIFrame":true, ...}   c.iotToken=<iotToken>

ilop 签名（x-ca-*）规范串：
  POST\n<Accept>\n<Content-MD5>\n<Content-Type>\n<Date>\n
  <排序后的x-ca签名头 k:v 每行一个>\n<路径?已编码query>
  HMAC-SHA1(appSecret) -> base64 -> x-ca-signature
  注意: resource 里 query 参数已 URL 编码; createSession/stream/query 的
  x-ca-request-id 会附加到 resource 上.

设备侧 ilop 凭据（从模拟器运行内存提取，梆梆加固下静态拿不到）:
  appKey    = 333958854
  appSecret = 0d14575e37b049b8917bdd1d1cf43a68

RTMP 加密流格式（relayEncrypted=true）:
  - FLV tag payload 结构: [变长明文头][AES-128-CBC 密文(16字节对齐)]
    明文头 = 包总长 % 16 字节, 以 [4B 剩余总长] 开头
    即: 整个明文单元 = [4B len][HEVC NAL(len 字节)][pad 到16对齐...]
    加密只覆盖尾部对齐部分, 开头 (len%16) 字节明文残留
    每个包独立用同一 iv 做 CBC 解密:  pt = P[:len%16] + CBC_dec(P[len%16:], iv)
  - extradata(hvcC) 同样用 key+iv 做 CBC 解密
  - 喂解码器: 长度前缀格式 pt[:4+len] (hvcC, 不要 annex-b 起始码)

iotId 规则: deviceName + "000000" 右补 (如 tQJx38DnvJUdFPRDOy8l -> tQJx38DnvJUdFPRDOy8l000000)
"""

import base64
import hashlib
import hmac
import json
import struct
import time
import urllib.parse
import urllib.request
import uuid

APP_KEY = "333958854"
APP_SECRET = "0d14575e37b049b8917bdd1d1cf43a68"
OAUTH_HOST = "living-account.cn-shanghai.aliyuncs.com"
IOT_HOST = "api.link.aliyun.com"


def _api_post(host: str, path: str, params: dict, iot_token: str = None,
              api_ver: str = "1.0.0") -> dict:
    """ilop JSON-RPC 风格 POST + x-ca 签名"""
    body = {
        "a": str(uuid.uuid4()), "b": "1.0",
        "c": {"apiVer": api_ver, "language": "zh-CN"},
        "d": params, "id": str(uuid.uuid4()),
        "params": {"$ref": "$.d"}, "request": {"$ref": "$.c"}, "version": "1.0",
    }
    if iot_token:
        body["c"]["iotToken"] = iot_token
    raw = json.dumps(body, separators=(",", ":")).encode()
    md5 = base64.b64encode(hashlib.md5(raw).digest()).decode()
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    ct = "application/octet-stream; charset=utf-8"
    ac = "application/json; charset=utf-8"
    resource = f"{path}?x-ca-request-id={uuid.uuid4()}"
    sh = {"x-ca-key": APP_KEY, "x-ca-nonce": nonce,
          "x-ca-signature-method": "HmacSHA1", "x-ca-timestamp": ts}
    canon = "\n".join(["POST", ac, md5, ct, date]
                      + [f"{k}:{sh[k]}" for k in sorted(sh)] + [resource])
    sig = base64.b64encode(
        hmac.new(APP_SECRET.encode(), canon.encode(), hashlib.sha1).digest()).decode()
    headers = {
        "date": date, "x-ca-key": APP_KEY, "x-ca-nonce": nonce,
        "x-ca-timestamp": ts, "x-ca-signature": sig,
        "x-ca-signature-headers": ",".join(sorted(sh)),
        "x-ca-signature-method": "HmacSHA1", "ca_version": "1",
        "content-type": ct, "accept": ac, "content-md5": md5,
        "user-agent": "ALIYUN-ANDROID-DEMO",
    }
    req = urllib.request.Request(f"https://{host}{resource}",
                                 data=raw, headers=headers, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _oauth_login(auth_code: str) -> str:
    """loginbyoauth -> sid"""
    form = {
        "loginByOauthRequest": json.dumps({
            "country": "CN", "authCode": auth_code, "oauthPlateform": 23,
            "oauthAppKey": APP_KEY,
            "riskControlInfo": {
                "appVersion": "2157", "USE_OA_PWD_ENCRYPT": "true", "utdid": "",
                "netType": "wifi", "umidToken": "", "locale": "zh_CN",
                "appVersionName": "2.11.80", "deviceId": str(uuid.uuid4()),
                "routerMac": "02:00:00:00:00:00", "platformVersion": "32",
                "appAuthToken": "", "appID": "com.narwel.narwelrobots",
                "signType": "RSA", "sdkVersion": "3.4.2", "model": "PGBM10",
                "USE_H5_NC": "true", "platformName": "android", "brand": "OPPO",
                "yunOSId": "",
            },
        }, separators=(",", ":")),
    }
    raw = urllib.parse.urlencode(form).encode()
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    ct = "application/x-www-form-urlencoded; charset=utf-8"
    ac = "application/json; charset=utf-8"
    resource = "/api/prd/loginbyoauth.json?" + "&".join(
        f"{k}={v}" for k, v in sorted(form.items()))
    sh = {"x-ca-key": APP_KEY, "x-ca-nonce": nonce,
          "x-ca-signature-method": "HmacSHA1", "x-ca-timestamp": ts}
    canon = "\n".join(["POST", ac, "", ct, date]
                      + [f"{k}:{sh[k]}" for k in sorted(sh)] + [resource])
    sig = base64.b64encode(
        hmac.new(APP_SECRET.encode(), canon.encode(), hashlib.sha1).digest()).decode()
    headers = {
        "date": date, "x-ca-key": APP_KEY, "x-ca-nonce": nonce,
        "x-ca-timestamp": ts, "x-ca-signature": sig,
        "x-ca-signature-headers": ",".join(sorted(sh)),
        "x-ca-signature-method": "HmacSHA1", "ca_version": "1",
        "content-type": ct, "accept": ac, "user-agent": "ALIYUN-ANDROID-DEMO",
    }
    req = urllib.request.Request(
        f"https://{OAUTH_HOST}/api/prd/loginbyoauth.json",
        data=raw, headers=headers, method="POST")
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return resp["data"]["data"]["loginSuccessResult"]["sid"]


class LinkVisualSession:
    """一次视频会话: 建链 -> RTMP url + 解密密钥 -> 解密包迭代器"""

    def __init__(self, auth_code: str, device_name: str):
        self.auth_code = auth_code
        self.iot_id = device_name.ljust(len(device_name) + 6, "0")  # +"000000"
        self.sid = None
        self.iot_token = None
        self.stream_info = None

    def connect(self):
        self.sid = _oauth_login(self.auth_code)
        sess = _api_post(IOT_HOST, "/account/createSessionByAuthCode",
                         {"request": {"authCode": self.sid,
                                      "accountType": "OA_SESSION",
                                      "appKey": APP_KEY}},
                         api_ver="1.0.4")
        self.iot_token = sess["data"]["iotToken"]
        self.stream_info = _api_post(
            IOT_HOST, "/vision/customer/stream/query",
            {"relayEncryptType": 0, "needDomainName": True,
             "enablePortPredict": True, "iotId": self.iot_id,
             "streamType": 0, "clientType": "Android",
             "relayEncrypted": True, "forceIFrame": True,
             "enableWebSocket": True, "cacheDuration": 0},
            iot_token=self.iot_token, api_ver="2.2.0")["data"]
        return self

    @property
    def relay_url(self) -> str:
        return self.stream_info["relayUrl"]

    @property
    def key_iv(self):
        dk = self.stream_info["relayDecryptKey"]
        return base64.b64decode(dk["key"]), base64.b64decode(dk["iv"])


def decrypt_packet(pkt: bytes, key: bytes, iv: bytes) -> bytes:
    """解密单个视频包 -> 明文 [4B len][NAL][pad]"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    hl = len(pkt) % 16
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return pkt[:hl] + d.update(pkt[hl:]) + d.finalize()


def open_video_stream(relay_url: str, key: bytes, iv: bytes):
    """av.open RTMP -> (container, video_stream, decrypted_extradata)"""
    import av
    cont = av.open(relay_url, timeout=10, options={"rtmp_live": "1"})
    vs = next(s for s in cont.streams if s.type == "video")
    ex = bytes(vs.codec_context.extradata or b"")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    dex = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(
        ex[: len(ex) // 16 * 16])
    return cont, vs, dex


def iter_decoded_packets(cont, vs, key, iv):
    """迭代解密后的长度前缀 NAL payload (pt[:4+len])"""
    for pkt in cont.demux(vs):
        pt = decrypt_packet(bytes(pkt), key, iv)
        if len(pt) < 4:
            continue
        ln = struct.unpack(">I", pt[:4])[0]
        if ln == 0 or 4 + ln > len(pt):
            continue
        yield pt[: 4 + ln], pkt.pts, pkt.dts


if __name__ == "__main__":
    # 端到端验证: 登录 -> getDeviceInfo -> MQTT 起流 -> LinkVisual -> 解密 -> 解码 3 帧
    # 依赖 D:/YJ/test_nc (NarwalCloud/NarwalClient) — 仅调试用
    import sys, time as _t
    sys.path.insert(0, "D:/YJ")
    sys.path.insert(0, "D:/YJ/narwal-cn/custom_components/narwal/narwal_client")
    from test_nc import NarwalCloud
    from test_nc.client import NarwalClient
    import urllib.request as _u
    from paho.mqtt.properties import Properties as _Props
    from paho.mqtt.packettypes import PacketTypes as _PT

    ACCT, PWD, AREA = "13433725159", "LYben123", "86"
    PK, DID = "hEA7OEshlx", "2ff03b16330b4bda92b9ddcd7284458a"
    BASE = f"/{PK}/{DID}"

    def _pbs(f, s):
        from test_nc.proto import encode_varint
        b = s.encode() if isinstance(s, str) else s
        return encode_varint(f << 3 | 2) + encode_varint(len(b)) + b

    def _pbv(f, v):
        from test_nc.proto import encode_varint
        return encode_varint(f << 3) + encode_varint(v)

    import asyncio
    async def main():
        c = NarwalCloud(region="cn")
        s = c.login(ACCT, PWD, AREA)
        uuid_s = s.user_uuid

        ident_req = _u.Request(
            f"https://cn-app.narwaltech.com/iot-application-platform/"
            f"app-product-media-server/v1/getDeviceInfo?productId={PK}&deviceId={DID}",
            headers={"Auth-Token": s.access_token})
        import json as _j
        r = _j.loads(_u.urlopen(ident_req, timeout=15).read())["result"]
        mpk, mdn = r["productKey"], r["deviceName"]
        print("media:", mpk, mdn, flush=True)

        cli = NarwalClient(PK, DID, uuid_s, uuid_s, s.access_token,
                           broker="cn-mqtt.narwaltech.com")
        await cli.connect()
        req_uuid = str(uuid.uuid1())
        resp_topic = f"{BASE}/multimedia/streaming_command/response"
        env = _pbs(1, uuid_s) + _pbs(2, uuid_s) + _pbs(5, _pbs(1, resp_topic) + _pbs(2, req_uuid))
        from test_nc.proto import encode_varint
        frame = b"\x01" + encode_varint(len(env)) + env
        viewer = '{"userID":"%s","deviceID":"lv_stream tool"}' % uuid_s
        body = (_pbv(1, 1) + _pbs(2, '{"version":1,"data":{"regionID":"0"}}')
                + _pbv(3, 2) + _pbs(4, viewer) + _pbv(5, 0))
        props = _Props(_PT.PUBLISH)
        props.ResponseTopic = resp_topic
        props.CorrelationData = _pbs(1, req_uuid) + _pbv(3, 0) + _pbv(4, int(_t.time() * 1000))
        cli._client.publish(f"{BASE}/multimedia/streaming_command",
                            frame + body, qos=1, properties=props)
        print("streaming_command sent", flush=True)

        async def hb():
            while True:
                au = _pbs(1, uuid_s) + _pbs(2, uuid_s)
                pl = (b"\x01" + encode_varint(len(au)) + au
                      + _pbv(1, int(_t.time() * 1000)) + _pbv(2, 1) + _pbv(3, 2)
                      + _pbv(4, 0) + _pbv(5, 0))
                cli._client.publish(f"{BASE}/multimedia/streaming_heartbeat", pl, qos=0)
                await asyncio.sleep(5)
        hb_task = asyncio.create_task(hb())
        await asyncio.sleep(4)

        sess = LinkVisualSession(uuid_s, mdn).connect()
        print("relay:", sess.relay_url[:70], flush=True)
        key, iv = sess.key_iv

        import av
        deadline = _t.monotonic() + 120  # robot takes a while to start pushing
        cont = None
        while _t.monotonic() < deadline:
            try:
                cont, vs, dex = open_video_stream(sess.relay_url, key, iv)
                break
            except Exception:
                _t.sleep(2)
        if cont is None:
            print("FAIL: relay never carried data")
            return
        codec = av.CodecContext.create("hevc", "r")
        codec.extradata = dex
        codec.open()
        saved = 0
        for payload, pts, dts in iter_decoded_packets(cont, vs, key, iv):
            ap = av.Packet(payload)
            ap.pts, ap.dts = pts, dts
            try:
                for f in codec.decode(ap):
                    f.to_image().save(f"D:/YJ/lv_frame_{saved}.jpg")
                    print("frame", saved, f.width, "x", f.height, flush=True)
                    saved += 1
                    if saved >= 3:
                        raise SystemExit
            except SystemExit:
                raise
            except Exception:
                pass
        hb_task.cancel()
        await cli.disconnect()

    asyncio.run(main())
