# DcmGet 2.8.2

DcmGet 是一个跨平台 DICOM C-MOVE 多任务工作台。单个应用窗口可同时管理多个下载任务，并通过基于 `pynetdicom` 的 C-STORE SCP 接收器池并行执行多个 `movescu`；默认并发 2 个检查号，可在 1-8 之间设置。相同接收 AE 与端口的任务复用一个并发接收器，不同端口则启动独立服务、暂存目录和接收日志；每个活动检查号仍按 Accession Number 和已绑定的 Study Instance UID 精确路由到各任务自己的目标目录，避免并发任务串片。收到的文件按可配置的 DICOM 元数据目录归档，下载结束后可自动排队生成包含原始 DICOM、`DICOMDIR`、中文 OHIF 和本地只读 HTTP 启动器的 PDI 便携目录。界面和命令行共用同一套配置、持久化、进程管理、下载与导出核心。

各版本新增内容见 [CHANGELOG.md](CHANGELOG.md)，也可以在主界面点击“版本说明”查看。

## 支持范围

- Windows x64、macOS ARM64/x86_64、Linux x86_64
- Python 3.10 或更高版本
- 发布部署使用 DCMTK 3.7.0；代码兼容本机 DCMTK 3.6.9，并使用 `dcmmkdir` 生成和校验 PDI 的 `DICOMDIR`
- 提供源码部署包、Windows x64 便携版和一键安装器

源码部署包不携带第三方二进制，首次部署需要能访问 Python 包源、[OFFIS DCMTK 下载源](https://dicom.offis.de/en/dcmtk/dcmtk-tools/)和 npm 官方源以下载经过固定 SHA-256 校验的 OHIF。Windows 成品发布物已内置 OHIF 与本地启动器；PDI 导出完成后的阅片不需要访问互联网。

## Windows 一键安装

Windows 发布物拆分为三个独立下载项，获取安装器时不再同时下载重复的便携运行时：

- `DcmGet-2.8.2-Setup-x64.exe`：默认推荐的一键安装器，内置 Python 运行时、PyQt5、pynetdicom、DCMTK 3.7.0、离线中文 OHIF、PDI 本地只读 HTTP 启动器和 Microsoft Visual C++ x64 Runtime，并创建仅允许实际 `DcmGet.exe` 在域/专用网络建立入站连接的程序级防火墙规则，兼容设置中的自定义接收端口。
- `DcmGet-2.8.2-windows-x64-portable.exe`：无需安装的单文件便携版；首次启动需要等待程序解压运行环境，PDI 同样使用原始 DICOM 和离线 OHIF。
- `DcmGet-2.8.2-windows-x64.zip`：解压后直接运行的独立目录版，包含与安装版一致的离线 OHIF 和 PDI 启动器。

三种发布物的 PDI 阅片过程均不连接互联网：OHIF 静态资源已内置并校验，同时会复制一份到便携目录；DICOM 清单和影像数据只从用户选择的 PDI 根目录读取。

安装版不要求目标电脑预装 Python。再次运行新版安装包时，会识别原安装记录并在原目录完成覆盖升级；用户配置、注册码和试用计数保存在 Windows 用户数据目录，升级和卸载都不会覆盖或删除这些数据与下载结果。当前实现的是安全原位升级，不会在后台自动联网安装新版本。默认下载目录为“文档\DcmGet\Dicom”。当前发布物未进行商业代码签名，Windows SmartScreen 可能显示未知发布者提示。

维护者可在 GitHub Actions 中手动运行 `Windows Release` 工作流，也可在 Windows x64 构建机执行：

```powershell
python -m pip install -r requirements-build.txt
python scripts/download_dcmtk.py --platform windows-x86_64
python scripts/build_windows.py --version 2.8.2
```

PyInstaller 生成的可执行文件已包含 Python 解释器，因此不再额外运行独立的 Python 安装程序。

## 试用与软件注册

程序启动时不再要求输入日期口令。未注册电脑默认可免费启动 30 个批量下载任务；每个任务仅在第一次真正开始 C-MOVE 时扣一次，排队、暂停、恢复、重试失败项和 PDI 导出都不会重复扣次。配置错误、接收器启动失败或只打开界面不会扣次。试用结束后必须输入当前电脑的离线注册码。主界面会显示剩余次数，并可随时点击“软件注册”查看和复制机器码。

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

复制限制来自“机器码 + Ed25519 签名”。纯离线客户端无法阻止管理员清除全部本地试用状态，若需要不可重置的强试用限制，应增加在线授权服务。源码部署包便于内部部署，但持有源码的人能够修改校验逻辑；对外发放时应使用构建后的 EXE，而不是源码包。

## 快速部署

解压源码部署包后，在项目目录执行：

### Windows PowerShell

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_windows.ps1
.\scripts\run_ui.ps1
```

部署脚本会检查 Python 版本、重建干净的 `.venv`、安装 PyQt5 和 pynetdicom、下载 DCMTK 3.7.0、准备离线 OHIF 和 PDI 本地启动器、检查 VC++ Runtime，并在管理员模式下为该虚拟环境的 Python 接收进程创建仅限域/专用网络的程序级入站防火墙规则；该规则可覆盖接收器池使用的多个端口。重建虚拟环境不会改动配置、注册码、试用记录或下载结果。

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

macOS/Linux 使用与 Windows 相同的离线 OHIF 静态资源；启动器只为当前 PDI 目录提供本地只读 HTTP 服务，不对外网开放。

## 使用界面

1. 打开“设置”，填写 PACS 地址、端口和 PACS AE。三类 AE Title 必须是 1-16 个可打印 ASCII 字符，非法字符会在对应字段直接提示。新配置默认使用本机调用 AE `DCMGET`、接收 AE `DCMGET` 和接收端口 `6666`；如需下载后脱敏，可在同一页启用匿名处理并选择方案。
2. 点击左栏“新建任务”，选择或拖入 TXT，也可以直接粘贴多行检查号；空行会忽略，重复项会按首次出现顺序去重。选择目标目录、目录模板和 PDI 快捷选项后创建任务。
3. 新任务只执行配置、DCMTK 和目录等静态预检；端口与接收器在任务真正获得运行机会时检查。新任务会立即加入调度，有空闲并发槽时无需等待其他检查号结束即可开始。
4. 左侧任务列表显示各任务的状态、总进度、文件数、速度或排队信息，不显示患者姓名；右侧显示当前任务的检查号、速度、状态统计、PDI 状态和操作按钮。窗口小于 960 个 Qt 逻辑像素时会切换为整页任务列表/任务详情。
5. 每个任务可独立暂停、继续、停止、重试失败项、打开目标目录和打开 PDI。暂停任务会在当前检查号安全结束后停止领取新检查号，不影响其他任务；继续后重新加入并发调度。停止或退出不会删除已收到的文件。已完成、失败、已取消或可重试的任务还可从列表中删除；该操作只删除任务记录和进度，绝不删除 DICOM、PDI、日志或隔离文件。
6. 每个任务不超过 200 个检查号时显示逐项文件数、速度和耗时；超过 200 个时只显示聚合进度和状态统计，避免 40,000 条任务拖慢界面。所有归档文件统一以 `.dcm` 结尾。
7. 界面日志默认只显示错误，便于快速定位需要处理的问题；勾选“显示详细日志”后才显示调试、信息、成功和警告。日志仍可在“本任务/全部”之间切换；“清空显示”只清空界面缓存，磁盘中的任务日志与各接收器日志始终完整保留。
8. 如需交付 U 盘，可在新建任务时勾选“下载完成后生成 PDI 便携目录”并选择保存目录；首次使用仍需在设置中填写机构名称，阅片器等高级选项也在设置页管理。批次结束后点击“打开影像”，或用“打开导出目录”将整个目录复制到 U 盘。重启程序后也可点击“打开已有 PDI 目录”直接选择根目录阅片，无需寻找 JSON 文件。PDI 失败可单独重试，无需重新下载。

## 多任务调度与恢复

- DcmGet 采用单窗口任务中心。再次启动程序会唤醒已有窗口，不会再启动一个争抢接收 AE 和端口的独立实例。
- 接收层使用跨平台 pynetdicom C-STORE SCP 池。相同接收 AE 与端口的任务复用一个支持多个 association 的接收器并同时运行；不同端口会启动独立接收器，使用独立暂存、隔离和日志目录。调度器默认允许 2 个、最多 8 个 C-MOVE 并行运行，并公平地从不同可运行任务领取检查号。存在可运行任务时所需接收器按需启动，全部下载任务暂停或结束后自动停止。
- PDI 使用独立的单任务队列，可在后续下载继续进行时生成，但两个 PDI 不会同时导出。下载文件、日志、速度和 PDI 状态都按任务隔离。
- 所有任务和检查号恢复点保存在应用私有目录的 `tasks.sqlite3`。数据库使用 WAL 和短事务，调度通过 SQL 逐条取数，不会反复把 40,000 条检查号全部载入内存。旧版 `active-task.sqlite3` 会在首次启动时安全迁移并保留备份。
- 每处理完一个检查号都会持久化结果。重启或意外退出后会一次性恢复全部未完成任务；已完成项不会重复下载，退出时正在处理的检查号会重新进入队列。遗留的 DCMTK 子进程会先经过身份校验再清理，无法归属的接收文件会移入隔离目录，不会猜测归属或自动删除。
- 接收暂存目录与目标目录可以位于不同卷。Windows 从 `C:` 暂存归档到 `D:`、U 盘或网络共享，以及 macOS/Linux 的跨文件系统归档，都会先在目标目录写入并同步临时文件，再原子发布最终 `.dcm`；复制失败时保留源文件，避免跨卷重命名失败造成丢失。
- 每个任务保存创建时的完整配置快照，包括目标目录。存在未完成任务时，仍可修改 PACS 地址、PACS AE、本机调用 AE、接收 AE、接收端口和新任务目标目录，新值只影响之后创建的任务；DCMTK 路径与全局并发上限仍由当前应用统一管理。同一端口只能绑定一个接收 AE；使用不同端口即可启动多个 SCP，同一 AE 也可用于不同端口，但必须确保相应 PACS 的 Move Destination 映射会回传到正确端口。不同未完成任务不能包含相同检查号，已结束的历史任务不阻止重新下载。

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

## PDI 便携目录

任务主页可快速启用 PDI 并选择保存目录；设置页继续管理机构名称、离线阅片器等高级选项。启用后，每批下载归档完成会自动导出一个可整体复制到 U 盘的目录。此功能不生成 ISO，也不控制光驱或刻录机。默认输出到 `DICOM 保存目录/PDI/DCMGET_PDI_日期时间`；也可以选择独立输出位置。

典型目录结构如下：

```text
DCMGET_PDI_20260716_120000/
├── DICOMDIR
├── INDEX.HTM            # 目录说明与检查摘要，不承担 DICOM 解码
├── README.TXT
├── MANIFEST.SHA256
├── DICOM/                # 原始 DICOM 的 PDI 短路径副本
├── VIEWER/               # 程序运行数据与离线中文阅片资源，请勿删除
└── OPEN_VIEWER.*         # 当前平台的本地阅片启动器
```

- 普通下载结果仍以 `.dcm` 结尾；PDI 内部复制件使用符合 DICOM File ID 约束的无扩展名短路径，源文件不会被修改。
- 用户只需点击“打开已有 PDI 目录”选择根目录，或在当前任务中点击“打开影像”，不需要选择 JSON、`DICOMDIR` 或逐个影像文件。DcmGet 始终使用安装包内经过校验的 OHIF 与本地服务，只从所选 PDI 读取 DICOM 和内部索引；即使是旧版 PDI，也不会加载 U 盘中的网页脚本。预先生成的隐藏索引可避免每次启动重新扫描大量 DICOM。
- 离线阅片器直接读取 PDI 内的原始 DICOM，不生成 JPG 或 PNG 预览副本，也不携带 Weasis。阅片界面默认使用中文，支持序列、窗宽窗位、测量和多帧查看能力。
- 现代浏览器不允许 OHIF 通过 `file://` 自动枚举本机目录，因此请运行 `OPEN_VIEWER.exe`（Windows 安装版导出的首选入口，也可用 `OPEN_VIEWER.bat`）、`OPEN_VIEWER.command`（macOS）或 `OPEN_VIEWER.sh`（Linux）。启动器以当前 PDI 根目录为唯一入口，只绑定 `127.0.0.1` 随机端口并打开本机浏览器；地址使用 `/viewer/directory/`，不再显示 `dicomjson`。macOS/Linux 启动脚本依赖目标电脑已安装 Python 3；Windows 成品导出的 `OPEN_VIEWER.exe` 不需要另装 Python。
- 阅片过程不联网：主程序使用自身内置阅片资源，便携启动器使用目录内副本；内部索引和 DICOM 影像始终来自当前 PDI，不使用外部 CDN、在线 DICOMweb 服务或远程账号。
- DcmGet 会等本地目录服务通过就绪检查后再打开浏览器；服务连续空闲约 4 小时才自动退出。再次点击“打开影像”可重新打开已经运行的页面。
- 为避免超大批次耗尽内存，离线阅片索引最多 100,000 帧且估算不超过 64 MiB；超限时仍保留有效的 DICOMDIR 和原始 DICOM，并提示按批次拆分后重试阅片器导出。
- `dcmmkdir` 先尝试严格 Profile；遇到目录字段、传输语法或编码不兼容时会使用兼容模式重试，并在 `README.TXT` 和界面中明确警告，不会宣称严格 Profile 合规。
- PDF、SR、视频或当前 OHIF 不支持显示的对象仍会保留为原始 DICOM 并列入报告。`DICOMDIR` 失败则不会发布半成品目录。
- PDI 使用本批次实际归档文件，不会扫描并混入目标目录中的历史检查。PDI 失败不改变下载状态、不重新下载，也不额外消耗试用次数。
- 未匿名的 DICOM 可能包含患者隐私。外发前应启用合适的匿名方案并完成复核。

## 命令行

保留两个直接入口：

```bash
python DICOM_download_ui.py
python DICOM_download_script.py --config config.json
python DICOM_download_script.py --task-id <任务编号>
```

命令行作为单任务前台客户端，与界面共享 `tasks.sqlite3`、30 次试用计数和注册码，不再要求日期口令。只有一个未完成任务时可直接恢复；存在多个未完成任务且未指定 `--task-id` 时，程序会列出任务编号并退出，避免恢复错误任务。可用 `--license PATH` 指定注册码文件；`--accept-download-failures` 可接受当前下载结果并继续 PDI，`--discard-checkpoint` 可明确放弃所选恢复点。

命令行退出码：

- `0`：下载及启用的 PDI 导出全部完成
- `1`：配置、预检或接收器启动失败
- `2`：存在下载失败、部分成功，或启用的 PDI 导出失败/部分成功
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
| `storage_ae_title` / `storage_port` | C-STORE 接收 AE 与端口 |
| `max_concurrent_moves` | 同时运行的 C-MOVE 数量，默认 `2`，范围 `1-8` |
| `directory_template` | 目录组合模板；支持 `PatientID`、`AccessionNumber`、`StudyInstanceUID` |
| `anonymization_enabled` | 是否在最终归档前启用 DICOM 元数据处理，默认 `false` |
| `anonymization_profile` | `basic`、`research` 或 `strict`；默认 `research` |
| `pdi_export_enabled` | 是否在每批下载结束后自动生成 PDI 便携目录，升级配置默认 `false` |
| `pdi_institution_name` | PDI 首页和说明中的机构名称；启用 PDI 时必填 |
| `pdi_output_folder` | PDI 输出根目录；留空时使用 `DICOM 保存目录/PDI` |
| `pdi_include_ohif_viewer` | 是否加入中文 OHIF 和本地只读 HTTP 启动器；默认 `true` |
| `max_log_file_size_bytes` | 单个日志文件最大字节数 |

旧版配置会自动迁移到 v6；原 PACS、AE、目录、PDI 机构名称和输出位置会保留，并新增默认值为 `2` 的 `max_concurrent_moves`。v5 的 OHIF 迁移语义保持不变：旧 JPEG 预览和 Weasis 选项均关闭时继续关闭，任一开启时启用 OHIF，迁移后不再保存旧字段。DCMTK 的查找顺序是：配置目录、`.runtime/dcmtk` 部署目录、旧版 `dcmtk/bin`、系统 `PATH`。

## 下载流程与故障处理

程序从启动最早阶段开始写入独立诊断日志。`dcmget-diagnostics.log` 记录启动、Python、Qt 和后台线程异常，`dcmget-crash.log` 记录原生崩溃信息。即使主界面尚未显示，也可以直接查看：

- Windows：`%LOCALAPPDATA%\DcmGet\logs\dcmget-diagnostics.log`，安装版也可从开始菜单点击“DcmGet 诊断日志”。
- macOS：`~/Library/Application Support/DcmGet/logs/dcmget-diagnostics.log`。
- Linux：`$XDG_STATE_HOME/dcmget/logs/dcmget-diagnostics.log`，未设置时为 `~/.local/state/dcmget/logs/dcmget-diagnostics.log`。

主界面顶部的“诊断日志”可打开该固定目录；发生闪退时请同时提供上述两个文件。多任务下载会为每个任务写入 `task-<任务编号>.log`，每个接收器分别写入包含接收 AE 与端口的独立日志；匿名模式的日志写入应用私有状态目录。macOS 源码启动还会在创建界面前自动清除 iCloud 可能附加到平台插件的 hidden 标志，避免 Qt 找不到 Cocoa 插件后直接退出。

接收器池在应用私有状态目录为每个 AE/端口组合和活动检查号创建独立暂存区。程序确认对应 pynetdicom SCP 已监听后，再执行带连接与 DIMSE 超时的 `movescu --no-port`。共享同一接收器的 C-STORE 对象必须精确匹配活动检查号，或匹配此前由该检查号绑定的 Study Instance UID；无法可靠归属的文件进入隔离目录，绝不猜测写入其他任务。只有把应用全局并发数设为 `1` 时，才允许将缺失检查号但带 Study UID 的旧 PACS 对象绑定到唯一活动路由；非空但错误的检查号始终拒绝。每条 C-MOVE 完成后读取 DICOM 元数据，按任务配置快照中的目标目录和目录模板归档并补充 `.dcm` 后缀；关键元数据缺失时使用安全占位值，匿名失败文件会保留并写入日志。

pynetdicom 在 Windows、macOS 和 Linux 上使用线程化 association 接收，并接受全部标准 Storage SOP Class 及常见未压缩、JPEG、JPEG-LS、JPEG 2000、RLE 和视频传输语法。接收数据以分块方式保持原始传输语法写入，不需要为下载而解压像素。若 PACS 已返回待处理响应、对象无法归属或接收连接中止但没有有效落盘文件，任务会标记为失败而不是“无数据”，并可通过“重试失败项”再次执行。

速度按暂存目录中实际收到的原始 DICOM 字节计算；任务进行中每 0.5 秒采样一次，单个检查号结束后显示其平均传输速度。匿名转换和最终归档耗时不会计入网络下载速度。

- “接收端口已占用”：关闭占用程序或在设置中更换端口，并同步 PACS 的 Move Destination。
- 修改默认接收端口 `6666` 后：Windows 源码部署需由管理员重新运行部署脚本以同步入站防火墙规则；安装版的程序规则自动兼容自定义端口。
- “C-MOVE 完成但未收到文件”：检查 PACS 中接收 AE、客户端 IP、接收端口及防火墙映射。
- DCMTK 启动失败：并发 GUI 下载实际由 pynetdicom 接收，只调用 `movescu`；PDI 还需要 `dcmmkdir`、`dcmdump` 等工具，兼容的旧命令行流程仍需要 `storescp`。建议选择完整的 DCMTK 3.7.0 `bin` 目录。
- Windows 缺少 DLL：安装部署脚本提示的 Microsoft Visual C++ x64 Runtime。

## 开发与验证

```bash
python -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest
python scripts/build_deploy_bundle.py
```

真实 DICOM 集成测试覆盖 pynetdicom 并发 association、压缩传输语法原样接收以及 DCMTK `movescu` 流程，并检查输出文件的 `DICM` 标识。

## 许可

项目原仓库未提供独立开源许可证，因此本部署包没有擅自选择许可证。详见 [LICENSE](LICENSE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
