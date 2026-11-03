# CampusRunning — iPhone 多设备 GPS 轨迹同步推送

[中文](#中文) · [English](#english)

基于 [sixzjd/fakeGPS-for-iPhone](https://github.com/sixzjd/fakeGPS-for-iPhone)（MIT）的 **多设备同步** 分支：
一台电脑插多台 iPhone，勾选任意几台，把同一条 GPX 轨迹**同时**推到所有被选设备上。

> 关键词：iPhone 虚拟定位、多设备同步、GPX 轨迹模拟、iOS 27 支持、免越狱、校园跑

---

## 中文

### 和上游的区别

| | 上游 FakeGPS | 本分支 CampusRunning |
|---|---|---|
| 设备选择 | 单选下拉框，只认第一台 | **多选复选框**，N 台任意勾选 |
| GPX 推送 | 一次一台 | **N 台并发**，同一轨迹同步开始 |
| 进度显示 | 假进度条 | 每台设备独立进度 + 总进度 |
| iOS 27.x | 隧道连不上（服务端口被拒） | **可用**（pymobiledevice3 11.45.0） |
| GPX 编码 | 跟随系统编码，中文/BOM 必崩 | BOM / UTF-16 / GBK / GB18030 / Big5 自动识别 |
| 隧道连接 | 每次操作泄漏一条 RSD | 复用并按设备显式关闭 |
| 停止播放 | 停不下来 | 一次停止全部设备 |

### 环境要求

- Windows 10/11 或 macOS，装有 iTunes / Apple Devices 驱动
- iPhone 用 USB 连接，**保持解锁亮屏**（iOS 17+ 锁屏后不再开放开发者服务端口）
- 首次运行需要管理员权限（iOS 17+ 的 tunneld 隧道要建虚拟网卡）
- 源码运行时：Python 3.11+，依赖见 `requirements.txt`

### 从源码运行

```bash
pip install -r requirements.txt
python run_gui.py
```

首次点 **Set Location** 时会弹一次 UAC / 密码框，批准后 tunneld 会在后台常驻。

### 使用多设备

1. 插上多台 iPhone，等设备列表出现（可点 ⟳ 刷新）
2. 设备列表里**勾选**要一起跑的设备，或用 `Select All` / `Deselect All`
3. 点地图选点或输入坐标 → **Set Location**（会同时下发到所有被选设备）
4. **Browse** 选一个 `.gpx` → **Play GPX** → 所有被选设备同步开始跑
5. 每台设备的进度条独立显示；**Stop** 一次停掉全部

### 打包

```bash
python -m PyInstaller fakegps.spec --clean -y
# 产物：dist/CampusRunning/CampusRunning.exe
```

### 已知限制

- 同一台 iPhone 同时只能有一个 LocationSimulation 客户端；不要用两个本程序窗口操作同一台设备
- GPX 里的 `<time>` 间隔超过 60 秒会被截断到 60 秒，避免界面看起来卡死
- 设备数量上限取决于 USB 带宽和 iOS `remoted`，一般 5 台以内很稳

### 许可

MIT License，见 [LICENSE](LICENSE)。原始版权归 **sunzhengnan**（FakeGPS 作者）所有；
本分支的修改同样以 MIT 发布，保留原始版权声明。

---

## English

A **multi-device** fork of [sixzjd/fakeGPS-for-iPhone](https://github.com/sixzjd/fakeGPS-for-iPhone) (MIT):
connect several iPhones to one computer, tick any subset of them, and push the same GPX
trajectory to all selected devices **simultaneously**.

### Differences from upstream

- **N-device checkbox selection** instead of a single-device dropdown
- **Parallel GPX playback** across every selected device, with per-device progress
- **iOS 27.x support** — `pymobiledevice3` bumped to 11.45.0 (earlier tunnel builds
  cannot forward per-service ports on iOS 27)
- **Robust GPX decoding** — BOM / UTF-16 / GBK / GB18030 / Big5
- **No RSD leak** — tunnel connections are reused and closed per device
- **Working Stop** — one click stops every device

### Requirements

- Windows 10/11 or macOS with Apple Devices / iTunes drivers
- iPhone over USB, **unlocked and screen on** (iOS 17+ closes developer service ports when locked)
- Administrator rights on first run (tunneld creates a virtual network interface)
- For source runs: Python 3.11+, see `requirements.txt`

### Run from source

```bash
pip install -r requirements.txt
python run_gui.py
```

### Multi-device usage

1. Plug in the iPhones and wait for the device list (⟳ to rescan)
2. Tick the devices you want, or use `Select All` / `Deselect All`
3. Pick a point and press **Set Location** — it is sent to every selected device
4. **Browse** a `.gpx` file → **Play GPX** — playback starts on all selected devices
5. **Stop** halts every device at once

### Build

```bash
python -m PyInstaller fakegps.spec --clean -y
# output: dist/CampusRunning/CampusRunning.exe
```

### License

MIT — see [LICENSE](LICENSE). Original copyright **sunzhengnan**; fork modifications
released under the same MIT terms with the original notice preserved.
