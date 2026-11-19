# 云鲸扫地机 Home Assistant 集成（国行云端版）

基于 [nadavbau/narwal-integration](https://github.com/nadavbau/narwal-integration)（MIT）适配中国大陆云端。

**走云鲸云（`cn-mqtt.narwaltech.com` MQTT5/TLS）**，不需要机器人开机 9002，
不需要局域网可达——只要云鲸 App 能控制，本集成就能控制。
已实测 **逍遥001（hEA7OEshlx）** 和 **J5（Uuug39n0fD）** 国行固件应答正常。

## 相比上游的改动

- **手机号登录**：支持 `loginByPw`（手机号 + 区号，默认 86），邮箱登录保留；
  国行 App 默认手机号注册，邮箱路径适用于海外区。
- **默认中国区**：config flow region 默认 `cn`（`cn-app` / `cn-mqtt`）。
- **简体中文界面**：`translations/zh-Hans.json` 全中文（实体名、状态、选项）。

## 实体一览

- **vacuum**：开始/暂停/继续/停止/回充、吸力、清洁模式、拖布湿度
- **sensor**：电量、状态、故障、清扫时长/面积/进度、当前房间、水箱/尘盒/清洁液状态、耗材到期
- **binary_sensor**：充电中、已回基站、故障、需保养
- **button**：寻找机器人、唤醒、洗拖布、烘拖布、洗烘、集尘
- **camera**：实时地图（房间、轨迹、位置）
- **select/switch/number**：清洁模式、拖布湿度、烘干强度、洗布频率、地毯策略、
  童锁、自动集尘、静音集尘、自动加清洁液、地毯增压、宠物模式、
  智能深度清洁、热水洗布、勿扰、音量、清扫次数

## 安装（HACS）

1. HACS → ⋮ → 自定义存储库 → `https://github.com/czbenben/narwal-cn`，类型 **Integration** → 下载 → **重启 HA**
2. **设置 → 设备与服务 → 添加集成 → "Narwal"**
3. 填云鲸 App 登录信息：
   - **手机号**（国行选这个）或邮箱，密码相同
   - 手机区号默认 `86` 不用改
   - 区域默认 `cn`
4. 列出绑定设备 → 每台添加一次（逍遥001 和 J5 各加一次）

> 老版本（本地 WS 版）升级的用户：先删掉旧的配置条目再重新添加——
> 新旧数据格式不兼容，旧条目会在启动时报错。

## 手动安装

把 `custom_components/narwal` 复制到 HA 的 `config/custom_components/`，重启后同上添加。

## 已验证

| 机型 | productId | 固件 | MQTT 应答 |
|---|---|---|---|
| 云鲸逍遥001 (CX1) | `hEA7OEshlx` | v01.08.10.05 | ✅ get_device_info / base_status |
| 云鲸 J5 (AX8) | `Uuug39n0fD` | v01.04.60.01 | ✅ get_device_info / base_status |

实测数据：逍遥001 电量/基站状态、J5 清扫中状态（`TASK_TYPE_SWEEP`）均正常解析。

## 注意事项

- **账号安全**：密码仅用于向你自己的云鲸账号换取 token，存储在 HA 本地
  `config_entries` 中，不会发送到第三方。
- **深度睡眠**：基站待机时机器人 MQTT 仍在线但会忽略指令；集成会自动执行
  唤醒序列（`notify_app_event` → `active_robot_publish`）。
- **多客户端**：同一账号的其他客户端（App/另一台 HA）会收到全部响应，
  集成用 MQTT5 CorrelationData 匹配自己的应答，互不干扰。
- token 约 30 天过期，集成会自动 refresh，失败时用存储的账号密码重新登录。

## 调试工具

`tools/cloud_probe.py`：登录 + 设备列表 + 影子状态一次性验证

```bash
python tools/cloud_probe.py <手机号> <密码>
```
