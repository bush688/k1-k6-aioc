# K1 / K6 AIOC 远程电台

用 Windows 电脑连接 AIOC，在电脑或手机浏览器上收听、查看手台屏幕和控制电台。AIOC 提供 USB 声卡与硬件 PTT，画面和按键通过串口传输。

| 功能 | UV-K1 | UV-K6 v1（SI4732 改装） |
| --- | --- | --- |
| AIOC 接收音频和 DTR PTT | 已使用 | 已实机确认 PTT |
| 普通手台屏幕回传 | K5Viewer 兼容固件 | 0.13.3 HS 屏幕版已实机确认 |
| 网页遥控键盘 | K1 布局 | K6 布局，按键固件待实测 |
| 短波收音 | 依机型和固件 | 保留 HS 本机功能，短波界面远程回传待实现 |
| 信道写频及亚音 | 支持项目验证过的 K1 存储布局 | K6 存储布局未验证，请使用对应写频工具 |

服务端支持电脑耳机和话筒、手机收听与 PTT、预制语音和设备选择。网页键盘可在 UV-K1 与 UV-K6 v1 之间切换，并记住本浏览器的选择。云希语音合成会将待朗读文字发送给微软服务，录音和已缓存的语音可以本地使用。

## Windows 服务端

安装 Python 和依赖后，从仓库根目录启动。

    py -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    .venv\Scripts\python.exe gui.py

1. 选择 AIOC 对应的 COM 口，波特率 38400，PTT 方式选择 **AIOC DTR**。
2. 接收采集选择 **麦克风 (AIOC Audio)**，发射输出选择 **扬声器 (AIOC Audio)**。电脑耳机和电脑话筒分别选择自己的设备。
3. 在 Windows 窗口打开控制页。手机使用窗口显示的局域网地址，访问令牌等同于控制权限，请妥善保管。
4. 先测试接收和本机画面，再按住 PTT 测试发射，松手确认退发。仅通过已获许可的频率和功率发射。

## K1 刷机

已安装并能回传画面的 K1 可以直接使用。需要更新时，先备份信道、设置和校准。按所选 K1 固件的上游说明进入升级模式，通过 UV Studio 普通固件刷写入口选择匹配 UV-K1 的程序固件。完成后检查机型键盘布局，再验证画面、按键及 AIOC。对应修改版源码尚未核对齐全，本仓库链接上游 K1 固件与说明。 [F4HWN K1 上游](https://github.com/armel/uv-k1-k5v3-firmware-custom)

## K6 v1 刷机与恢复

已实测的屏幕版适用于 **UV-K6 v1、SI4732 硬件改装、满足 HS 要求的 EEPROM 扩容**。上游 HS 要求至少 2 Mbit EEPROM。先备份 EEPROM、信道和校准，并准备已验证可启动的原版 HS 固件。

1. 关掉远程服务端释放串口，K6 关机，按住机身 PTT 开机，确认机顶灯常亮。
2. 使用 Chrome 或 Edge 打开 K5Web 的「固件升级」，选择写频线对应串口和本仓库的 K6 屏幕版文件。 [K5Web](https://k5.vicicode.com/)
3. 等到工具显示升级成功后重启。检查机身画面和短波，再接 AIOC 验证网页画面与 PTT。
4. 如果升级停滞，确认进度确实停止，再重新进入升级模式。优先用已验证原版恢复。原字库正常时可直接保留，字库和 SI4732 SSB 补丁是独立的 EEPROM 内容。 [螺丝虎原版 0.13.3 HS](https://github.com/losehu/uv-k5-firmware-custom/releases/tag/0.13.3)

[下载 K6 屏幕版](firmware/k6-v1/K6-v1-133HS-screen-only.packed.bin) · [K6 源码基线与构建说明](firmware/k6-v1/README.md)

## 开源协议

本仓库的原创服务端代码和 K6 修改采用 Apache-2.0。K6 基于 losehu 的 Apache-2.0 固件，保留上游许可、作者标注和修改补丁。依赖和上游固件各自适用原协议。 [Apache-2.0 许可](LICENSE) · [AIOC 上游](https://github.com/skuep/AIOC)

如需本机打包 Windows EXE，安装 PyInstaller 后运行。

    .venv\Scripts\python.exe -m pip install pyinstaller
    .venv\Scripts\python.exe -m PyInstaller --onefile --windowed --name K1RemoteReceiveGUI-AIOC gui.py
