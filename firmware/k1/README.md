# UV-K1 6.3 Labs 定制固件

[下载 K1 固件](K1-6.3-Labs-CN-DTrac-PTT-AudioScope-test.bin)

本文件是基于 F4HWN 6.3 Labs 的修改版，包含中文界面、DTrac、PTT 和 AudioScope 相关修改。已用于本项目的 K1 画面和 AIOC 流程。此二进制来自本地迁移包。精确对应的修改版源码仍未找齐，上游源码链接用于追溯出处。随文件保留上游 Apache-2.0 许可和署名。 [F4HWN 上游源码](https://github.com/armel/uv-k1-k5v3-firmware-custom)

1. 确认机型是 UV-K1，备份信道、校准和设置，保留原固件。
2. 按 UV-K1 的说明进入升级模式，打开 UV Studio，使用普通「刷写固件」入口选择这里的程序固件。
3. 刷写完成后重启，检查屏幕和按键。现有字库显示正常时直接保留。
4. 接回 AIOC，在服务端选择对应 COM、38400 和 AIOC DTR。先验证画面及接收，再短按 PTT 确认能发射和退发。
5. 若黑屏或按键异常，重新进入升级模式，刷回备份的已验证固件。

[上游许可证](LICENSE) · [上游署名](UPSTREAM-NOTICE.txt) · [SHA-256](SHA256SUMS.txt)
