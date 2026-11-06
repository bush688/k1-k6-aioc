# K6 v1 SI4732 固件

推荐使用已实机验证的[按键与画面提速版](K6-v1-133HS-SI4732-keys-fast-screen.packed.bin)。普通通联和 SI4732 收音机界面均可回传画面及使用网页按键。画面优先发送变化的区域，并定期补发全屏。短波界面的远程按键按短按处理。AIOC 硬件 PTT 在通联界面使用。

## 刷机与验证

本固件适用于 UV-K6 v1、SI4732 硬件改装和符合 HS 要求的 EEPROM 扩容。先备份 EEPROM 和现用固件，关闭远程服务端释放串口。关机后按住机身 PTT 开机，确认机顶灯常亮。用 [K5Web 固件升级](https://k5.vicicode.com/#/tool/flash) 选对应串口和提速版固件，等待提示成功后再重启。

打开服务端网页，选择 UV-K6 v1 键盘。先测试普通通联界面，再进入 SI4732 收音机，用方向键调频并用 EXIT 退出。异常时可刷回[已验证的屏幕版](K6-v1-133HS-screen-only.packed.bin)或[原版 HS 恢复固件](LOSEHU133HS-upstream.bin)。

## 从源码构建

提速版使用上游 0.13.3 HS 配置与 Arm GCC 15.2.1。克隆对应标签后应用 [si4732-keys-fast-screen.patch](si4732-keys-fast-screen.patch)。其余构建参数见下文，将命令中的 screen-only.patch 替换为此补丁。各补丁独立应用于上游源码。

[固件 SHA-256](SHA256SUMS.txt) · [上游许可证](LICENSE)

## 历史版本

以 losehu 0.13.3 正式标签为基线。修改为接受 K5Viewer 画面请求，并按 64 像素条带回传普通手台界面。使用上游 HS 构建选项，保持官方发布配置的 ENABLE_4732SSB 值。程序镜像使用上游打包格式。

    git clone --branch 0.13.3 --depth 1 https://github.com/losehu/uv-k5-firmware-custom.git
    cd uv-k5-firmware-custom
    git apply --ignore-space-change ../screen-only.patch
    make build ENABLE_CHINESE_FULL=4 ENABLE_PINYIN=1 ENABLE_4732=1 ENABLE_4732SSB=0

需要 GNU Make、Arm GNU Toolchain、Python 3 与 crcmod。已发布的屏幕版用 Arm GCC 15.2.1 构建。执行构建目标后使用生成的打包固件。K6 屏幕版已实机验证画面回传和 AIOC 硬件 PTT。屏幕版中的协议只包含画面请求与回传。

[上游源码](https://github.com/losehu/uv-k5-firmware-custom/tree/0.13.3) · [原版发布页](https://github.com/losehu/uv-k5-firmware-custom/releases/tag/0.13.3) · [上游许可证](LICENSE)

### 遥控按键预览版

另有一份基于同一 HS 配置的预览固件，增加普通界面的远程按键。网页选择 UV-K6 v1 后显示 M、上下键、EXIT、数字键、F/# 和侧键。PTT 仍由 AIOC DTR 控制。该预览版已由上方的实机验证提速版替代。测试前保留已确认能启动的屏幕版，首轮只测试菜单和方向键。

[按键预览固件](K6-v1-133HS-screen-and-keys-preview.packed.bin) · [预览源码补丁](screen-and-keys.patch)

预览版从同一上游标签构建，将上述命令中的 screen-only.patch 换成 screen-and-keys.patch。两个补丁分别相对于上游源码应用。三个修改文件是 app/app.c、app/uart.c 和 app/uart.h，保留原版权声明。上游提交为 d4e88bc7de9c87e9b6305ab5beead6b3c346c439。

[原版 HS 恢复固件](LOSEHU133HS-upstream.bin)来自 0.13.3 正式发布，用于恢复开机。用户已通过 AIOC 将它刷入 K6 v1 并恢复开机。
