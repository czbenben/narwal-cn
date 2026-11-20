"""Aliyun LinkVisual live-video client for CN-region Narwal robots.

Video pipeline (reverse-engineered from com.narwel.narwelrobots 2.11.80 and
verified against a live robot):

  1. Media-server device identity:
     GET {api}/iot-application-platform/app-product-media-server/v1/getDeviceInfo
         ?productId=<pk>&deviceId=<did>            (Auth-Token header)
     -> {"productKey": mpk, "deviceName": mdn, "deviceSecret": mds}

  2. MQTT `multimedia/streaming_command` tells the robot to start pushing to
     Aliyun LinkVisual.  Payload (app format):
       frame   = 0x01 + varint(len) + {f1:uuid, f2:uuid,
                                       f5:{f1:response_topic, f2:req_uuid}}
       request = {f2:{f1:mpk, f2:mdn, f3:mds}, f3:""}
     plus MQTT5 props ResponseTopic=<topic>/response, CorrelationData.

  3. ilop session (x-ca HMAC-SHA1 signed, appKey/appSecret below):
       loginbyoauth             -> sid
       createSessionByAuthCode  -> iotToken
       vision/customer/stream/query (iotId = mdn + "000000")
         -> relayUrl (RTMP) + relayDecryptKey{key,iv} + p2pInfo

  4. RTMP encryption (relayEncrypted):
       FLV tag payload = [(len%16) cleartext bytes][AES-128-CBC rest]
       plaintext unit  = [4B len][HEVC NAL][pad to 16]
       extradata (hvcC) decrypts the same way.
       Feed the decoder length-prefixed payloads pt[:4+len].
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import ssl
import struct
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

APP_KEY = "333958854"
APP_SECRET = "0d14575e37b049b8917bdd1d1cf43a68"
OAUTH_HOST = "living-account.cn-shanghai.aliyuncs.com"
IOT_HOST = "api.link.aliyun.com"

MEDIA_INFO_PATH = (
    "/iot-application-platform/app-product-media-server/v1/getDeviceInfo"
)


class LinkVisualError(Exception):
    """LinkVisual chain failed."""


def _api_post(host: str, path: str, params: dict, iot_token: str | None = None,
              api_ver: str = "1.0.0") -> dict:
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
    ctx = ssl.create_default_context()
    return json.loads(urllib.request.urlopen(req, timeout=15, context=ctx).read())


def _oauth_login(auth_code: str) -> str:
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
    ctx = ssl.create_default_context()
    resp = json.loads(urllib.request.urlopen(req, timeout=15, context=ctx).read())
    return resp["data"]["data"]["loginSuccessResult"]["sid"]


@dataclass
class MediaIdentity:
    """The robot's LinkVisual device identity (from getDeviceInfo)."""

    product_key: str
    device_name: str
    device_secret: str


def fetch_media_identity(api_host: str, access_token: str,
                         product_key: str, device_name: str) -> MediaIdentity:
    url = (f"https://{api_host}{MEDIA_INFO_PATH}"
           f"?productId={product_key}&deviceId={device_name}")
    req = urllib.request.Request(url, headers={"Auth-Token": access_token})
    ctx = ssl.create_default_context()
    resp = json.loads(urllib.request.urlopen(req, timeout=15, context=ctx).read())
    if resp.get("code") != 0 or not resp.get("result"):
        raise LinkVisualError(f"getDeviceInfo failed: {resp.get('msg')}")
    r = resp["result"]
    return MediaIdentity(r["productKey"], r["deviceName"], r["deviceSecret"])


def build_streaming_command(uuid_str: str, resp_topic: str, req_uuid: str,
                            start: bool = True) -> bytes:
    """Full payload for multimedia/streaming_command.

    START: frame + {f1:1, f2:versionJson, f3:2, f4:viewerJson, f5:0}
    STOP:  frame + {f1:0, f4:viewerJson}
    (byte-level replica of the app; the robot's reply carries the media
    identity in f2, not the command.)
    """
    def pbs(field: int, val) -> bytes:
        b = val.encode() if isinstance(val, str) else val
        from .proto import encode_varint
        return encode_varint(field << 3 | 2) + encode_varint(len(b)) + b

    def pbv(field: int, val: int) -> bytes:
        from .proto import encode_varint
        return encode_varint(field << 3) + encode_varint(val)

    from .proto import encode_varint
    env = (pbs(1, uuid_str) + pbs(2, uuid_str)
           + pbs(5, pbs(1, resp_topic) + pbs(2, req_uuid)))
    frame = b"\x01" + encode_varint(len(env)) + env
    viewer = ('{"userID":"' + uuid_str + '","deviceID":"Home Assistant"}')
    if start:
        return (frame + pbv(1, 1)
                + pbs(2, '{"version":1,"data":{"regionID":"0"}}')
                + pbv(3, 2) + pbs(4, viewer) + pbv(5, 0))
    return frame + pbv(1, 0) + pbs(4, viewer)


def build_streaming_heartbeat(uuid_str: str) -> bytes:
    """streaming_heartbeat payload ({f1:ts_ms, f2:1, f3:2, f4:0, f5:0})."""
    def pbs(field: int, val) -> bytes:
        b = val.encode() if isinstance(val, str) else val
        from .proto import encode_varint
        return encode_varint(field << 3 | 2) + encode_varint(len(b)) + b

    def pbv(field: int, val: int) -> bytes:
        from .proto import encode_varint
        return encode_varint(field << 3) + encode_varint(val)

    from .proto import encode_varint
    auth = pbs(1, uuid_str) + pbs(2, uuid_str)
    return (b"\x01" + encode_varint(len(auth)) + auth
            + pbv(1, int(time.time() * 1000)) + pbv(2, 1) + pbv(3, 2)
            + pbv(4, 0) + pbv(5, 0))


class LinkVisualSession:
    """One ilop session: oauth -> iotToken -> stream/query."""

    def __init__(self, auth_code: str, media_device_name: str) -> None:
        self.auth_code = auth_code
        self.iot_id = media_device_name + "000000"
        self.iot_token: str | None = None
        self.stream_info: dict | None = None

    def connect(self) -> "LinkVisualSession":
        sid = _oauth_login(self.auth_code)
        sess = _api_post(IOT_HOST, "/account/createSessionByAuthCode",
                         {"request": {"authCode": sid,
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
    def key_iv(self) -> tuple[bytes, bytes]:
        dk = self.stream_info["relayDecryptKey"]
        return base64.b64decode(dk["key"]), base64.b64decode(dk["iv"])


def decrypt_packet(pkt: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt one video tag -> plaintext [4B len][NAL][pad]."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    hl = len(pkt) % 16
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return pkt[:hl] + d.update(pkt[hl:]) + d.finalize()


def decrypt_extradata(ex: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return d.update(ex[: len(ex) // 16 * 16])


def open_video_stream(relay_url: str):
    """av.open the RTMP relay -> (container, video_stream)."""
    import av
    cont = av.open(relay_url, timeout=10, options={"rtmp_live": "1"})
    vs = next(s for s in cont.streams if s.type == "video")
    return cont, vs


def iter_nal_payloads(cont, vs, key: bytes, iv: bytes):
    """Yield (length-prefixed NAL payload, pts, dts) for each good video tag."""
    for pkt in cont.demux(vs):
        pt = decrypt_packet(bytes(pkt), key, iv)
        if len(pt) < 4:
            continue
        ln = struct.unpack(">I", pt[:4])[0]
        if ln == 0 or 4 + ln > len(pt):
            continue
        yield pt[: 4 + ln], pkt.pts, pkt.dts


def grab_first_frame_jpeg(session: LinkVisualSession, max_packets: int = 60,
                          timeout_s: float = 15.0) -> bytes | None:
    """Open the relay, decode until the first frame, return JPEG bytes."""
    import av

    key, iv = session.key_iv
    cont, vs = open_video_stream(session.relay_url)
    try:
        ex = bytes(vs.codec_context.extradata or b"")
        codec = av.CodecContext.create("hevc", "r")
        codec.extradata = decrypt_extradata(ex, key, iv)
        codec.open()
        import io
        deadline = time.monotonic() + timeout_s
        n = 0
        for payload, pts, dts in iter_nal_payloads(cont, vs, key, iv):
            n += 1
            if n > max_packets or time.monotonic() > deadline:
                break
            ap = av.Packet(payload)
            ap.pts, ap.dts = pts, dts
            try:
                for frame in codec.decode(ap):
                    img = frame.to_image()
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    return buf.getvalue()
            except Exception:  # noqa: BLE001 - corrupt tag, keep going
                continue
        return None
    finally:
        try:
            cont.close()
        except Exception:  # noqa: BLE001
            pass
