# DcmGet 2.0

DcmGet 是一个跨平台 DICOM C-MOVE 下载工作台。程序先启动 `storescp` 接收器，再由 `movescu` 逐个提交检查号，收到的文件按检查号归档。界面和命令行共用同一套配置、预检、进程管理和下载核心。

## 支持范围

- Windows x64、macOS ARM64/x86_64、Linux x86_64
- Python 3.10 或更高版本
- 发布部署使用 DCMTK 3.7.0；代码兼容本机 DCMTK 3.6.9
- 当前交付源码部署包，不包含 Windows EXE、Python 安装程序或 DCMTK 二进制

首次部署需要能访问 Python 包源与 [OFFIS DCMTK 下载源](https://dicom.offis.de/en/dcmtk/dcmtk-tools/)。

## 快速部署

解压源码部署包后，在项目目录执行：

### Windows PowerShell

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_windows.ps1
.\scripts\run_ui.ps1
```

部署脚本会检查 Python 版本、创建 `.venv`、安装 PyQt5、下载 DCMTK 3.7.0、检查 VC++ Runtime，并在管理员模式下创建 `storescp` 入站防火墙规则。

### macOS

```bash
./scripts/bootstrap_macos.sh
./scripts/run_ui.sh
```

### Linux

```bash
./scripts/bootstrap_linux.sh
./scripts/run_ui.sh
```

Linux 桌面环境若缺少 Qt 运行库，请按发行版安装常用的 XCB/OpenGL 运行库。

## 使用界面

1. 打开“设置”，填写 PACS 地址、端口、PACS AE、本机调用 AE、接收 AE 和接收端口。
2. 在任务主页选择或拖入 TXT，也可以直接粘贴多行检查号；空行会忽略，重复项会按首次出现顺序去重。
3. 选择保存目录，确认预检中的 DCMTK、目录和接收端口均通过。
4. 点击“开始下载”。结果位于 `保存目录/<检查号>/`，日志位于 `保存目录/logs/`。
5. 部分失败时可点击“重试失败项”。停止或退出不会删除已收到的文件。

## 命令行

保留两个直接入口：

```bash
python DICOM_download_ui.py
python DICOM_download_script.py --config config.json
```

命令行退出码：

- `0`：全部处理完成
- `1`：配置、预检或接收器启动失败
- `2`：存在失败或部分成功的检查号
- `130`：用户取消

## 配置

首次部署会从 `config.example.json` 创建 `config.json`。主要字段：

| 字段 | 说明 |
| --- | --- |
| `dcmtk_bin_dir` | DCMTK 的 bin 目录；留空时自动查找 |
| `access_numbers_file_path` | 检查号 TXT 路径 |
| `dicom_destination_folder` | DICOM 保存目录 |
| `pacs_server_ip` / `pacs_server_port` | PACS 地址与端口 |
| `calling_ae_title` | movescu 本机调用 AE |
| `pacs_ae_title` | PACS AE |
| `storage_ae_title` / `storage_port` | storescp 接收 AE 与端口 |
| `max_log_file_size_bytes` | 单个日志文件最大字节数 |

旧版配置会自动迁移。DCMTK 的查找顺序是：配置目录、`.runtime/dcmtk` 部署目录、旧版 `dcmtk/bin`、系统 `PATH`。

## 下载流程与故障处理

每批任务使用独立 `.dcmget-staging` 目录。程序确认 `storescp` 已监听后再执行 `movescu --no-port`。每条 C-MOVE 完成后，新文件移动到对应检查号目录；无法归属的暂存文件会保留并写入日志。

- “接收端口已占用”：关闭占用程序或在设置中更换端口，并同步 PACS 的 Move Destination。
- “C-MOVE 完成但未收到文件”：检查 PACS 中接收 AE、客户端 IP、接收端口及防火墙映射。
- DCMTK 启动失败：在设置中选择同时包含 `movescu`、`storescp` 的 bin 目录。
- Windows 缺少 DLL：安装部署脚本提示的 Microsoft Visual C++ x64 Runtime。

## 开发与验证

```bash
python -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest
python scripts/build_deploy_bundle.py
```

真实 DICOM 集成测试会在本机可找到 `movescu` 与 `storescp` 时运行，并检查输出文件的 `DICM` 标识。

## 许可

项目原仓库未提供独立开源许可证，因此本部署包没有擅自选择许可证。详见 [LICENSE](LICENSE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
