# Samsung Find（查找我的手机）

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

把**三星「查找我的手机」**（findmymobile.samsung.cn，中国大陆版）接入 Home Assistant：把手机、平板、手表、耳机、SmartTag 的位置变成 `device_tracker`，并附带电量、在线状态、锁屏状态、响铃按钮等实体。

> ⚠️ 只支持**中国大陆**三星账号（服务地址 `findmymobile.samsung.cn`）。港台/国际版账号（`smartthingsfind.samsung.com`）不适用。

---

## 功能

- **位置追踪**：每个设备的最后已知位置 → `device_tracker`（GPS 源），可直接用于 `person`、地图卡片、自动化到家/离家。
- **两种登录方式**：三星账号登录（推荐，自动续期）或手动粘贴 Cookie。
- **被动 / 主动两种定位模式**：被动只读三星已有位置（几乎不耗电）；主动会额外让设备当场定位（更快，更耗电），两者可分别对「手机/平板/手表/耳机」和「SmartTag」设置。
- **电量、在线状态、锁屏状态**实体，以及 **立即刷新位置 / 响铃 / 停止响铃**按钮。
- **只读不改**：除响铃和主动定位外，不会对设备做任何操作（不会远程锁定、不会擦除）。

## 支持的设备

任何在「查找我的手机」里能看到的三星设备都行，包括 Galaxy 手机 / 平板 / 手表 / Buds、SmartTag 系列。

---

## 安装（HACS）

前置条件：已经装好 [HACS](https://hacs.xyz/docs/setup/prerequisites)，Home Assistant **2024.1** 或更新。

1. 打开 **HACS** → 顶部选 **集成**（Integrations）。
2. 右上角 **⋮** → **自定义存储库**（Custom repositories）。
3. 在「存储库」里填：
   ```
   https://github.com/jimgo188/smartthings_find
   ```
   「类别」选 **集成**（Integration），点 **添加**。
4. 关掉弹窗，在 HACS 集成页搜索 **Samsung Find**，点进去 → 右下角 **下载**（Download）。
5. **重启 Home Assistant**（设置 → 系统 → 右上角电源按钮 → 重启）。

### 手动安装（不用 HACS）

把仓库里的 `custom_components/smartthings_find` 整个文件夹复制到 Home Assistant 配置目录的 `custom_components/` 下：

```
/config/custom_components/smartthings_find/
├── __init__.py
├── auth.py
├── ...
├── manifest.json
└── translations/
```

然后重启 Home Assistant。

---

## 配置

**设置 → 设备与服务 → 添加集成 → 搜索「Samsung Find」**

会先让你选登录方式：

### 方式一：三星账号登录（推荐）

登录一次，之后 Home Assistant 会自动续期，基本不用再管。密码和二次验证**只输入在三星自己的页面上**，不会经过 Home Assistant。

1. 在新的浏览器标签页按 `F12` → 切到 **Network（网络）** → 勾上 **Keep log / 保留日志**。
2. 在集成配置页复制 **① 登录链接**（点一下输入框 → `Ctrl+A` → `Ctrl+C`），粘到刚打开的新标签页地址栏里打开，**在三星的页面上完成登录**（含二次验证）。
   > 链接很长（约 1800 字符），别从页面文字里右键复制，那样只会拿到 Home Assistant 自己的地址。链接 15 分钟内有效，过期可重新生成。
3. 登录完成后浏览器会试图打开 `ms-app://…`（打不开、看着像没反应，这是正常的）。回到 `F12` 的 Network 面板，过滤框输入 `ms-app`，右键那条请求 → **Copy → Copy URL**。
4. 把整条 `ms-app://…` 地址粘到 **②** 里，填个 **③ 设备名称**（会显示在三星账号的「登录的设备」列表里，便于日后识别/移除），提交。

### 方式二：粘贴 Cookie（简单，但会过期）

1. 在电脑浏览器登录 https://findmymobile.samsung.cn 。
2. 按 `F12` → **Application（应用）** → **Storage → Cookies → https://findmymobile.samsung.cn**。
3. 复制 **`JSESSIONID`** 的值（它是 `HttpOnly`，在 Console 里看不到），粘贴到配置页。
   > 也接受整行的 `Cookie: …` 请求头。

实测静置约 18 小时后会失效，需要回到集成选项里粘贴新的 Cookie，或改用三星账号登录。

---

## 选项

添加后在集成卡片上点 **配置**（Configure）：

| 选项 | 默认 | 说明 |
|---|---|---|
| 手机/平板/手表/耳机的模式 | 被动 | 被动 = 只读三星已有位置；主动 = 每轮让设备定位一次 |
| SmartTag 的模式 | 主动 | SmartTag 靠附近 Galaxy 设备中继定位，主动模式耗电增加不明显 |
| 刷新间隔 | 300 秒 | 每隔多久从三星读取一次数据，最小 30 秒 |
| 自动刷新最小间隔 | 300 秒 | 同一台设备两次「自动请求定位」之间的最小间隔，最小 60 秒 |
| 位置最大有效期 | 0（不限制） | 超过这个时间的位置不再当作当前位置（实体变未知），让 `person` 回退到其它定位设备 |

不想开主动模式也能随时刷新一次：用设备卡片上的 **「立即刷新位置」** 按钮。

---

## 实体

每个设备会生成以下实体（`<设备名>` 为三星里的设备名）：

| 实体 | 类型 | 说明 |
|---|---|---|
| `<设备名>` | device_tracker | 最后已知位置，GPS 源 |
| `<设备名> 电量` | sensor | 电量百分比（来自上次连接检查，可能滞后） |
| `<设备名> 最后更新` | sensor | 三星记录的最后定位时间（timestamp） |
| `<设备名> 连接状态` | binary_sensor | 上次连接检查是否在线 |
| `<设备名> 锁屏状态` | binary_sensor | 上次连接检查时是否锁屏（SmartTag 无此实体） |
| `<设备名> 立即刷新位置` | button | 立刻向设备请求一次定位 |
| `<设备名> 响铃` / `停止响铃` | button | 让设备响铃（SmartTag 不支持） |

---

## 常见问题

**实体一直是「未知」/ 位置不更新？**
打开 https://findmymobile.samsung.cn ，如果弹出「位置 / 个人信息使用」条款窗口，勾选同意，然后重新加载集成。没同意的设备，三星会受理请求但设备不回传位置——集成里会同时出现一条「有设备未允许上报位置」的修复提示。

**提示授权失效（重新认证）？**
在三星账号里改了密码、退出了设备或撤销授权都会这样。点集成卡片上的 **重新认证**，重新走一次登录即可，已有设备和实体不会丢。

**位置更新太慢？**
三星只有在手机自己上报时才会更新位置。把模式改成「主动」可以让设备当场定位，或者在自动化里调用「立即刷新位置」按钮。

**能同时加两个三星账号吗？**
可以，重复添加即可，每个账号一个集成条目。

---

## 免责声明

本项目通过模拟三星网页端的私有接口工作，与三星无任何关联，接口随时可能变更导致失效。请自行承担使用风险，仅用于自己账号下的设备。集成不会远程锁定或擦除设备。

## 致谢
本项目基于 fapgomes/ha-smartthings-find 修改
