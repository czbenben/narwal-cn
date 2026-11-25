#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Narwal CN cloud API probe - verified endpoints (2026-11).

Usage: py tools\cloud_probe.py <mobile> <password>
"""
import json
import ssl
import sys
import urllib.parse
import urllib.request

API = "https://cn-app.narwaltech.com"
CTX = ssl.create_default_context()


def call(path, token=None, body=None, query=None, method=None):
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Auth-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    r = urllib.request.urlopen(req, timeout=15, context=CTX)
    return json.loads(r.read())


def login(mobile, password, area_code="86"):
    r = call("/user-authentication-server/v2/login/loginByPw",
             body={"area_code": area_code, "mobile": mobile, "password": password})
    if r.get("code") != 0:
        raise SystemExit(f"login failed: {r}")
    res = r["result"]
    print(f"login ok: uuid={res['uuid']} nickname={res.get('nickname')}")
    return res


def devices(token):
    r = call("/user-device-platform-server/device-info/getDeviceInfoList", token)
    out = {}
    pinfo = {p["productId"]: p for p in r["result"].get("productInfoList", [])}
    for d in r["result"]["deviceInfoList"]:
        p = pinfo.get(d["productId"], {})
        out[d["deviceId"]] = {
            "name": d.get("robotName"), "productId": d["productId"],
            "productModel": p.get("productModel"), "productGroup": p.get("productGroup"),
            "sn": d.get("snNumber"), "firmware": d.get("firmwareVersion"),
            "ble": (d.get("extend") or {}).get("BLEName"),
        }
    return out


def online(token, did, pid):
    r = call("/iot-platform/device-server/v1/device/getDeviceByParams", token,
             query={"deviceId": did, "productId": pid})
    res = r.get("result") or {}
    return res.get("onlineStatus"), res.get("productName")


def shadow(token, did, pid):
    r = call("/iot-platform/device-shadow-server/v1/get", token,
             query={"deviceId": did, "productId": pid})
    return r["result"]


def main():
    res = login(sys.argv[1], sys.argv[2])
    token = res["token"]
    devs = devices(token)
    for did, d in devs.items():
        st, pname = online(token, did, d["productId"])
        print(f"\n{d['name']}  model={d['productModel']} group={d['productGroup']} "
              f"pid={d['productId']}\n  did={did} sn={d['sn']} fw={d['firmware']} "
              f"ble={d['ble']} online={st} internal={pname}")
        sh = shadow(token, did, d["productId"])
        rep = sh.get("state", {}).get("reported", sh.get("reported", {}))
        keys = sorted(rep.keys()) if isinstance(rep, dict) else rep
        print("  shadow reported keys:", len(keys))
        for k in keys[:50]:
            print("   ", k, "=", rep[k])


if __name__ == "__main__":
    main()
