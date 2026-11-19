# 云鲸扫地机 Home Assistant 本地集成（国行适配版）

基于社区逆向成果 [sytchi/NarwalIntegration](https://github.com/sytchi/NarwalIntegration)（MIT）
适配中国版机型：**无需云鲸账号、不走云端**，通过局域网 WebSocket（9002 端口）直连机器人。

## 相比上游的改动

- **补全国行产品密钥表**：从云鲸智能 App（CN APK）`config_list.json` 提取的
  官方完整映射，新增 AX11 / Y1(备用) / X30·X31 备用 / J64 / J65 / J72 / J73 /
  J78 / J79 / X38 / X39 / J3 等 20+ 个密钥，自动识别覆盖国行全家桶。
- **中文型号下拉**：逍遥001/逍遥002/逍遥002 Max/J5/J4/J3 等中文名直接选。
- **mDNS 自动发现**：manifest 注册 `_narwal_sweeper._tcp.local`，机器人广播时
  HA 会自动弹出发现通知（实测部分固件不常开广播，手动加 IP 永远可用）。
- **简体中文界面**：新增 `translations/zh-Hans.json`，实体名/状态/故障码全中文。

## 支持型号（本地 WS :9002）

| 型号 | 状态 |
|---|---|
| 云鲸逍遥001 / Narwal Flow (AX12) | ✅ 协议已确认 |
| 云鲸逍遥002 / Flow 2 (X30) | ✅ 已确认 |
| 云鲸逍遥002 Max (CX7) | ✅ 国行变体已确认（见 rudyll/narwal_r） |
| 云鲸 J5 | 🟡 密钥已提取，待实测 |
| Freo Z10 Ultra (CX4) / X10 Pro (AX15) / Z10 Pro (AX26) | ✅ 社区确认 |
| Freo 20 (J64) / Freo 20 Edge (R32) | ✅ 社区确认 |
| J6x / J7x / X3x 新型号 | 🟡 密钥已内置，auto-detect 尝试 |
| 云鲸 J3 / J4 / J4 Lite | ☁️ 仅云端，本地 9002 不通 |
| Freo X Ultra (AX18/19) | ☁️ 仅云端（ZeroMQ :6789，非 WS） |

## 安装

### 方式一：手动复制（最直接）

1. 把 `custom_components/narwal/` 整个目录复制到 HA 配置目录下的
   `custom_components/` 中（HAOS 可用 Samba 加载项、SSH 或 File Editor）。
2. 重启 Home Assistant。
3. **设置 → 设备与服务 → 添加集成 → 搜索 "Narwal"**。
4. 输入机器人 IP（路由器 DHCP 列表里查，或主机名 `NARWAL_xxxxxx.local`），
   选择型号；不确定选「其他 / Auto-detect」。
5. 两台扫地机各添加一次即可（逍遥001 和 J5）。

### 方式二：HACS 自定义仓库

把本目录推到你的 GitHub 仓库后，HACS → 自定义存储库 → 填入仓库地址，
类型选 Integration，搜索 Narwal 安装，重启。

> 也可以直接用现成国行仓库 `https://github.com/rudyll/narwal_r`（同协议），
> 但本包的密钥表更新、覆盖型号更多。

## 实体一览

- **vacuum**：开始/暂停/继续/停止/回充、吸力档位、按房间清扫
- **camera**：实时地图（机器人位置叠加），`map_scale` 选项调清晰度
- **sensor**：电量、清扫面积/时长/进度、固件版本、充电状态、尘袋寿命、基站活动、故障码（带中文描述）
- **binary_sensor**：停靠状态、充电中
- **select**：拖布湿度、清洁模式
- **switch**：地图绘制开关（轨迹/已扫区域/家具/激光墙）
- **button**：清洗拖布、烘干拖布、集尘、唤醒
- **服务**：`narwal.clean_zone`（划区清扫）、`narwal.clean_rooms`（指定房间）、`narwal.resume`

## 诊断工具（电脑上直接跑，不需要 HA）

```bat
pip install websockets bbpb

:: 1) mDNS 扫描局域网内的云鲸设备（可能扫不到，机器人不常开广播）
py tools\mdns_scan.py

:: 2) 验证 9002 连通性，读型号/电量/状态 —— 装集成前先跑这个！
py tools\probe_robot.py 192.168.x.x

:: 3) 不知道产品密钥时被动监听广播提取
py tools\discover_product_key.py 192.168.x.x --wake
```

## 注意事项

- **只能一个本地连接**：机器人只允许一个 WS 客户端。调试脚本和 HA 集成不要同时连。
- **休眠**：机器人睡死后可能不响应。先用 App 唤醒（或集成里的「唤醒」按钮），再发指令。
- **J5 待验证**：密钥已在表里，如果 J5 连不上请先用 `probe_robot.py` 确认 9002 是否开放。
- **固件差异**：逍遥001 实测固件 v01.07.22+ 需要已建图才能 `vacuum.start`。
- **云鲸账号**：完全不需要，本集成不访问任何云端接口。

## 协议备忘（逆向自 CN APK v2026-11）

- 局域网：WebSocket `:9002`，帧 `0x01 | len+2 | 0x22 | topic_len | topic | protobuf`。
- topic 形如 `/{product_key}/{device_id}/clean/plan/start`，密钥表见
  `narwal_client/const.py: KNOWN_PRODUCT_KEYS`。
- 发现：mDNS `_narwal_sweeper._tcp`；配对 SoftAP：`wss://192.168.4.1:443/device_config`。
- 云端（本集成不用）：MQTT5 `cn-mqtt.narwaltech.com`，topic `/pid/did/thing/...` + `/general/*`，
  REST `https://cn-app.narwaltech.com`。
- 老机型（J1/J2）走 `nr_networking_legacy`，协议不同，不支持。

## 致谢

协议逆向与参考实现：[@sjmotew](https://github.com/sjmotew/NarwalIntegration)、
[@sytchi](https://github.com/sytchi/NarwalIntegration)、
[@rudyll](https://github.com/rudyll/narwal_r)。MIT License 见 LICENSE。
