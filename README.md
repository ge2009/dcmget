# DcmGet 2.3

DcmGet 是一个跨平台 DICOM C-MOVE 下载工作台。程序先启动 `storescp` 接收器，再由 `movescu` 逐个提交检查号，收到的文件按可配置的 DICOM 元数据目录归档。界面和命令行共用同一套配置、预检、进程管理和下载核心。

各版本新增内容见 [CHANGELOG.md](CHANGELOG.md)，也可以在主界面点击“版本说明”查看。

## 支持范围

- Windows x64、macOS ARM64/x86_64、Linux x86_64
- Python 3.10 或更高版本
- 发布部署使用 DCMTK 3.7.0；代码兼容本机 DCMTK 3.6.9
- 提供源码部署包、Windows x64 便携版和一键安装器

首次部署需要能访问 Python 包源与 [OFFIS DCMTK 下载源](https://dicom.offis.de/en/dcmtk/dcmtk-tools/)。

## Windows 一键安装

Windows 发布物提供两种形式：

- `DcmGet-2.3.0-Setup-x64.exe`：一键安装器，内置 Python 运行时、PyQt5、DCMTK 3.7.0 和 Microsoft Visual C++ x64 Runtime，并创建 `storescp` 默认端口 6666 的入站防火墙规则。
- `DcmGet-2.3.0-windows-x64-portable.exe`：无需安装的单文件便携版；首次启动需要等待程序解压运行环境。

安装版不要求目标电脑预装 Python。再次运行新版安装包时，会识别原安装记录并在原目录完成覆盖升级；用户配置、注册码和试用计数保存在 Windows 用户数据目录，升级和卸载都不会覆盖或删除这些数据与下载结果。默认下载目录为“文档\DcmGet\Dicom”。当前发布物未进行商业代码签名，Windows SmartScreen 可能显示未知发布者提示。

维护者可在 GitHub Actions 中手动运行 `Windows Release` 工作流，也可在 Windows x64 构建机执行：

```powershell
python -m pip install -r requirements-build.txt
python scripts/download_dcmtk.py --platform windows-x86_64
python scripts/build_windows.py --version 2.3.0
```

PyInstaller 生成的可执行文件已包含 Python 解释器，因此不再额外运行独立的 Python 安装程序。

## 登录与软件注册

程序每次启动都要求输入目标电脑本地日期组成的 8 位口令，例如 2026 年 7 月 14 日为 `20260714`。未注册电脑默认可免费启动 30 个批量下载任务；只有 `storescp` 成功监听后才扣次数，配置错误、接收器启动失败或只打开界面不会扣次，重试失败项会作为新任务计数。试用结束后必须输入当前电脑的离线注册码。主界面会显示剩余次数，并可随时点击“软件注册”查看和复制机器码。

授权人员在保存有私钥的 Mac 上生成注册码：

```bash
# 从源码运行注册机
python tools/dcmget_license_generator.py ABCDEF-123456-7890AB-CDEF12 --customer "示例医院"

# 可选：指定到期日并写入文件
python tools/dcmget_license_generator.py ABCDEF-123456-7890AB-CDEF12 \
  --customer "示例医院" --expires 2027-12-31 --output 示例医院.lic

# 构建仅供授权人员使用的 macOS 单文件注册机
python tools/build_license_generator_macos.py
```

注册机默认从 `~/.dcmget-license/ed25519-private.pem` 读取 Ed25519 私钥。私钥不会写入注册码、客户端、源码部署包或 Windows 安装包；请离线备份并且不要交给客户。macOS 构建产物位于 `release/license-generator/`。

客户端只保存签名后的注册码和机器绑定试用计数。试用计数采用文件锁和双份冗余状态；Windows 安装版的锚点位于共享程序数据目录，普通重装、切换用户或只删除主 `trial.json` 不会恢复次数，安装升级与卸载也不会删除这些文件。macOS/Linux 源码部署的锚点仍属于当前系统用户。

每日日期口令主要用于操作入口控制，真正的复制限制来自“机器码 + Ed25519 签名”。纯离线客户端无法阻止管理员清除全部本地试用状态，若需要不可重置的强试用限制，应增加在线授权服务。源码部署包便于内部部署，但持有源码的人能够修改校验逻辑；对外发放时应使用构建后的 EXE，而不是源码包。

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

1. 打开“设置”，填写 PACS 地址、端口和 PACS AE。新配置默认使用本机调用 AE `DCMGET`、接收 AE `DCMGET` 和接收端口 `6666`；如需下载后脱敏，可在同一页启用匿名处理并选择方案。
2. 在任务主页选择或拖入 TXT，也可以直接粘贴多行检查号；空行会忽略，重复项会按首次出现顺序去重。
3. 选择保存目录，确认预检中的 DCMTK、目录和接收端口均通过。
4. 在设置中选择或编辑目录模板。默认按 `PatientID/AccessionNumber/StudyInstanceUID` 组织，也可选择检查号、Study UID 等较短组合。
5. 点击“开始下载”。任务详情会显示每个检查号的文件数、实际接收速度和耗时。所有归档文件统一以 `.dcm` 结尾；普通模式日志位于 `保存目录/logs/`，匿名模式日志改存应用私有状态目录。
6. 点击“暂停”后，程序会先安全完成并归档当前检查号，再暂停启动下一项；点击“继续”即可处理剩余检查号。`storescp` 在暂停期间保持监听。
7. 部分失败时可点击“重试失败项”。停止或退出不会删除已收到的文件。

## 下载后匿名处理

匿名功能默认关闭，可在“设置 → 下载后匿名处理”中开启。最终目录名使用处理后的 Patient ID、检查号和 Study UID，文件名使用处理后的 SOP Instance UID。三个内置方案如下：

- “基础脱敏（院内）”：处理患者姓名、Patient ID、检查号、常见直接身份字段、人员姓名和私有标签，但保留日期、机构、描述及 DICOM UID。该档仍可能保留自由文本身份信息，`PatientIdentityRemoved` 会写为 `NO`，只适合受控院内流程。
- “研究匿名（推荐）”：在直接身份处理基础上稳定映射关联 UID、对同一患者一致偏移日期，并清理机构、描述和私有标签；保留部分人口学及设备信息。
- “严格元数据匿名”：在研究方案基础上继续清除日期、时间、人口学及设备字段。

这些方案参考 [DICOM PS3.15 Annex E](https://dicom.nema.org/medical/dicom/current/output/chtml/part15/chapter_e.html) 的常见元数据处理思路，不等同于完整的 PS3.15 合规认证。程序不会分析或修改像素中的烧录文字、人脸特征；研究/严格方案遇 `BurnedInAnnotation=YES`、`RecognizableVisualFeatures=YES`、PDF、SR、图形标注、缩略图、曲线或叠加层时会拒绝归档，而不是把未处理内容标成已匿名。外发前仍需按实际模态和数据类型复核。

启用匿名时，原始接收暂存、失败文件和运行日志不会写入结果目录，而是保存在当前系统用户的应用状态目录：

- Windows：`%LOCALAPPDATA%\DcmGet\`
- macOS：`~/Library/Application Support/DcmGet/`
- Linux：`$XDG_STATE_HOME/dcmget/`，未设置时为 `~/.local/state/dcmget/`

假名映射密钥为上述目录中的 `anonymization.key`。同一密钥会让重试和后续批次保持稳定映射；丢失或删除密钥后，新生成的 Patient ID、检查号和 UID 将与以前不同。匿名写入采用“生成临时文件 → 校验 DICM 与 SOP UID → 发布结果 → 删除原始暂存”的顺序，处理失败不会把半成品计入下载结果。

## 命令行

保留两个直接入口：

```bash
python DICOM_download_ui.py
DCMGET_DAILY_PASSWORD=20260714 python DICOM_download_script.py --config config.json
```

命令行同样要求当天口令，并与界面共享 30 次试用计数和注册码；它从 `DCMGET_DAILY_PASSWORD` 读取口令，交互式终端未设置环境变量时会安全提示输入。可用 `--license PATH` 指定注册码文件。

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
| `directory_template` | 目录组合模板；支持 `PatientID`、`AccessionNumber`、`StudyInstanceUID` |
| `anonymization_enabled` | 是否在最终归档前启用 DICOM 元数据处理，默认 `false` |
| `anonymization_profile` | `basic`、`research` 或 `strict`；默认 `research` |
| `max_log_file_size_bytes` | 单个日志文件最大字节数 |

旧版配置会自动迁移。DCMTK 的查找顺序是：配置目录、`.runtime/dcmtk` 部署目录、旧版 `dcmtk/bin`、系统 `PATH`。

## 下载流程与故障处理

每批任务使用独立暂存目录。普通模式暂存在保存目录下的 `.dcmget-staging`；匿名模式暂存在应用私有状态目录。程序确认 `storescp` 已监听后再执行 `movescu --no-port`。每条 C-MOVE 完成后读取 DICOM 元数据，按设置中的目录模板归档并补充 `.dcm` 后缀；关键元数据缺失时使用安全占位值，无法归属或匿名失败的暂存文件会保留并写入日志。

当当前 DCMTK 的 `storescp --help` 包含 `--fork` 时，Windows、macOS 和 Linux 都会启用每个 association 一个子进程的并发接收模式；旧版工具不支持时才回退单进程。若 PACS 已返回待处理响应或接收连接被中止但没有落盘文件，任务会标记为失败而不是“无数据”，并可通过“重试失败项”再次执行。

速度按暂存目录中实际收到的原始 DICOM 字节计算；任务进行中显示采样速度，单个检查号结束后显示其平均传输速度。匿名转换和最终归档耗时不会计入网络下载速度。

- “接收端口已占用”：关闭占用程序或在设置中更换端口，并同步 PACS 的 Move Destination。
- 修改默认接收端口 `6666` 后：Windows 需由管理员同步修改入站防火墙规则。
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
