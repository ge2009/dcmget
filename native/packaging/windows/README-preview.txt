DcmGet 4.0.0 Preview（Windows x64）
===================================

本包用于验证 Rust + GPUI 原生重构效果，与现有 DcmGet 3.x 独立安装。
安装、升级或卸载本预览版都不会删除现有 DcmGet 3.x、旧配置、任务或下载结果。

当前能力边界
------------

1. dcmget-desktop.exe 已连接原生 Rust Profile、任务恢复、Storage SCP 和 C-MOVE 下载路径。
2. 首次启动会只读备份并导入旧版 Profile 和未完成任务；旧文件不会被修改或删除。
3. dcmget-cli.exe 提供相同的原生 Rust C-MOVE / C-STORE 下载能力，不包含图形界面。
4. 本预览版暂不支持匿名化、PDI 和 DICOMDIR；检测到旧 Profile 启用这些能力时会明确拒绝启动，不会静默忽略。
5. 本包不包含 Python、DCMTK 或 Tauri；仅支持 Windows x64，Windows ARM64 可通过系统 x64 兼容层运行。
6. 原生桌面诊断日志位于 %LOCALAPPDATA%\DcmGet\native\logs\dcmget-native.log，单文件达到 20 MB 后保留一份轮转日志。

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
- 影像先接收到目标卷的 `.dcmget-staging`，随后按配置中的 Patient ID／请求检查号／Study UID 目录模板原子发布；归档失败会保留暂存文件并返回失败。
- 当前已验证无 PACS 失败恢复、持久化和端口释放；发布前仍必须用目标厂商 PACS 验证成功收图。
- 预览版不能替代当前生产版本；请先用测试 PACS、测试目录和非生产数据验证。
