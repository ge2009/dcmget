DcmGet 4.0.0 Preview（Windows x64）
===================================

本包用于验证 Rust + GPUI 原生重构效果，与现有 DcmGet 3.x 独立安装。
安装、升级或卸载本预览版都不会删除现有 DcmGet 3.x、旧配置、任务或下载结果。

当前能力边界
------------

1. dcmget-desktop.exe 是 GPUI 界面技术预览，目前显示演示数据，操作按钮尚未连接 PACS 下载引擎。
2. dcmget-cli.exe 已连接原生 Rust C-MOVE / C-STORE 下载路径，可用于受控测试。
3. 本包不包含 Python、DCMTK、Tauri、PDI 或 DICOMDIR。
4. 仅支持 Windows x64；Windows ARM64 可通过系统 x64 兼容层运行。

CLI 快速测试
------------

先复制 config.example.json 为 config.json，填写 PACS 地址、AE 和本机接收端口；
然后准备 UTF-8 编码的 access.txt，每行一个检查号。

检查配置：

  dcmget-cli.exe validate-config config.json

查看原生下载能力说明：

  dcmget-cli.exe readiness

开始下载：

  dcmget-cli.exe download --config config.json --accessions access.txt --destination D:\Dicom

重要提示
--------

- PACS 必须已将 storage_ae_title 映射到本机 IP 和 storage_port。
- 未知或当前 dicom-rs 注册表不支持的传输语法会被安全拒绝，本预览版不宣称兼容所有厂商对象。
- 预览版不能替代当前生产版本；请先用测试 PACS、测试目录和非生产数据验证。

