# CampusRunning v1.0.0

首个版本。基于 [FakeGPS](https://github.com/sixzjd/fakeGPS-for-iPhone)（MIT）重做的**多设备同步**分支：
一台电脑连多台 iPhone，勾选任意几台，把同一条 GPX 轨迹**同时**推给所有被选设备。

## 亮点

- **N 台设备并行**：设备列表改成复选框，可全选 / 只选一台 / 任意子集，数量无上限
- **同步播放 GPX**：所有被选设备同时开始、同一速度、同一轨迹
- **每台独立进度**：一条设备一行真实进度条，另有一条总进度；`Stop` 一次停全部
- **Set Location / Clear Location 也批量生效**
- **iOS 27.x 可用**：`pymobiledevice3` 升级到 11.45.0

## 修复

- **iOS 27 隧道**：旧版 pmd3 建的隧道无法转发"服务端口"，表现为 `ConnectionRefused` 或
  `device channel was not ready`；11.45.0 已解决（实测 `RSD.connect()` 1.26s 成功）
- **GPX 编码**：改为按字节解码，自动识别 BOM / UTF-16 / GBK / GB18030 / Big5 / UTF-8；
  旧版跟随系统编码，中文或带 BOM 的文件必崩
- **隧道连接泄漏**：`DvtServiceProvider.close()` 只关 DTX、不关底层 RSD，之前每次操作漏一条隧道，
  导致"第一次能定位、之后全废"；现在按设备显式关闭
- **重复开通道**：Play GPX 复用 Set Location 的通道，不再抢第二条
- **可中断的节奏控制**：按 GPX `<time>` 播放，单个间隔上限 60 秒，Stop 即时生效

## 其它

- 程序名、图标文案、日志与配置文件统一为 CampusRunning；配置文件 `~/.campusrunning_config.json`，
  首次启动会回退读取旧版 `~/.fakegps_config.json`，已有高德 Key 不会丢
- 内置更新器改为指向本仓库的 `CampusRunning-*` 资产，避免把本程序替换成上游 FakeGPS
- 许可证：MIT，保留原作者 sunzhengnan 版权声明

## 安装

1. 下载 `CampusRunning-v1.0.0-Windows.zip` 解压
2. 双击 `CampusRunning.exe`，首次会弹一次 UAC（tunneld 需要创建虚拟网卡）
3. 插上 iPhone，**保持解锁亮屏**（iOS 17+ 锁屏会关闭开发者服务端口）
4. 勾选设备 → `Set Location` → `Browse` 选 GPX → `Play GPX`

## 已知限制

- 同一台 iPhone 同时只能有一个 LocationSimulation 客户端，不要用两个窗口操作同一台设备
- 设备数量上限取决于 USB 带宽与 iOS `remoted`，一般 5 台内很稳
- 仅提供 Windows 构建；macOS 需自行用 `fakegps.spec` 打包

## 校验

- `CampusRunning.exe --selftest` → exit 0（9 个 pmd3 模块导入正常）
- 源码侧：多设备 API、GPX 解析（895 点样例）、无设备时的错误路径、前端 JS 语法均通过
- 多设备真机联调尚未在发布环境执行，欢迎反馈
