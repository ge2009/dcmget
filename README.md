# DcmGet 3.7.1

DcmGet 是一个默认离线运行的 DICOM C-MOVE 下载工作站。当前唯一工作台使用 Vite、React 和 TypeScript 构建，继续复用现有 Python/FastAPI 服务、任务核心与 DCMTK：Windows 本机通过独立 WebView2 窗口操作，不再默认弹出系统浏览器；局域网电脑仍可使用浏览器访问。每个 Profile 进程只运行一个下载任务，并独立启动一个 DCMTK `storescp`；任务中的检查号按顺序执行 `movescu`。关闭、刷新或断开界面不会停止后台下载，重新打开即可查看实时状态。

应用仍支持多个持久 Profile（`i1`、`i2`……）。Windows 安装版使用固定端口 `8786` 的统一工作台，通过紧凑 Profile 卡片进入实例，并可在页面顶部快速切换 Profile；任务、设置、日志、PDI 和运维功能始终留在同一个管理页面。每个 Profile 仍是独立后台进程，配置、Web 端口、接收 AE/端口、任务恢复点和日志互相隔离。不同 Profile 必须使用不同的 Web 端口和接收端口，通常也应配置独立接收 AE，并在 PACS 中分别建立 Move Destination 映射。收到的文件按可配置的 DICOM 元数据目录归档，下载结束后可自动生成包含原始 DICOM、`DICOMDIR`、中文 OHIF 和本地只读 HTTP 启动器的 PDI 便携目录。

各版本新增内容见 [CHANGELOG.md](CHANGELOG.md)，也可以在 Web 控制台点击“版本说明”查看。

## 支持范围

- Windows x64、macOS ARM64/x86_64、Linux x86_64；不支持任何 32 位系统或运行时
- 64 位 Python 3.10 或更高版本；Windows 源码运行必须使用 AMD64/x64 Python
- 发布部署使用 DCMTK 3.7.0；代码兼容本机 DCMTK 3.6.9。Windows 成品仅携带运行所需的 `movescu`、`storescp`、`dcmmkdir`、`dcmdump` 及其 DLL、字符集和许可证数据
- 当前自动发布只生成 Windows x64 一键安装器、便携版和 ZIP；macOS/Linux 仍可从源码运行，但暂不生成成品安装包
- 主控制台、API、字体和图标均随程序本地提供，运行时不需要 Node.js，也不依赖 CDN 或云服务；仅当 Windows 管理中心启用自动更新时会访问固定的 HTTPS 发布源
- 3.0.0 起不再包含或依赖 PyQt5；旧版 Qt 说明仅保留在历史版本记录中

源码部署不携带第三方二进制，首次部署需要能访问 Python 包源、[OFFIS DCMTK 下载源](https://dicom.offis.de/en/dcmtk/dcmtk-tools/)和 npm 官方源以下载经过固定 SHA-256 校验的 OHIF。Windows 成品发布物已内置 OHIF 与本地启动器；PDI 导出完成后的阅片不需要访问互联网。

## Windows 一键安装

Windows 发布物拆分为三个独立下载项，获取安装器时不再同时下载重复的便携运行时：

- `DcmGet-3.7.1-Setup-x64.exe`：默认推荐的一键安装器，内置 x64 Python 运行时、FastAPI/Uvicorn、React/TypeScript 离线工作台、Windows WebView 外壳、Microsoft x64 WebView2 Evergreen 离线运行时、精简的 x64 DCMTK 3.7.0 运行集、离线中文 OHIF、PDI 本地只读 HTTP 启动器、Microsoft Visual C++ x64 Runtime 和 `kayisoft-dcmget` Windows 服务。服务随系统自动启动管理中心，并只恢复用户明确选择运行的 Profile；安装器创建 `DcmGet Web TCP` 与 `DcmGet Receiver TCP` 两条仅限域/专用网络的程序级入站规则。
- `DcmGet-3.7.1-windows-x64-portable.exe`：无需安装的单文件便携版；首次启动会把逐文件 SHA-256 校验通过的精简 x64 DCMTK 运行集发布到 `%LOCALAPPDATA%\DcmGet\runtime\dcmtk\<版本与清单哈希>\`，以后启动和软件升级会复用内容相同的稳定用户级路径。两个进程同时首次启动时也只会原子发布一份运行集。PDI 同样使用原始 DICOM 和离线 OHIF。便携版不注册 Windows 服务，也不自动常驻管理中心。
- `DcmGet-3.7.1-windows-x64.zip`：解压后直接运行的独立目录版，包含与安装版一致的 React/TypeScript 离线工作台与 Windows WebView 外壳、精简 DCMTK 运行集、离线 OHIF 和 PDI 启动器，但不自动注册 Windows 服务或常驻管理中心。

Windows 32 位系统、32 位 Python 和 x86 应用载荷均不受支持。安装器使用 `x64compatible` 限制目标架构：可在 x64 Windows 原生运行，也允许 Windows 11 ARM64 通过系统的 x64 兼容层运行；不需要也不接受原生 ARM64 Python。Inno Setup 6 的安装引导程序自身是 x86 兼容程序，但它会拒绝 32 位 Windows，且只安装经过校验的 AMD64 DcmGet、Python 和 DCMTK。构建脚本会同时验证构建 Python、`DcmGet.exe`、`DcmGetPdiServer.exe`、`storescp.exe` 和 `movescu.exe` 的 PE 架构，任何一项不是 AMD64 都会停止发布；PDI 的 Python 回退启动器也会拒绝 32 位运行时。

便携版和 ZIP 不会自动取得管理员权限创建防火墙规则；需要从其他主机打开 Web 控制台或接收 C-STORE 时，请手工允许 `DcmGet.exe` 和实际运行的 `storescp.exe`，或优先使用安装版。单文件便携版应放行便携 EXE 本身，以及 `%LOCALAPPDATA%\DcmGet\runtime\dcmtk\<版本与清单哈希>\dcmtk-3.7.0-win64-dynamic\bin\storescp.exe`；ZIP 版应放行解压目录中的 `DcmGet.exe` 和 `_internal\.runtime\dcmtk\windows-x86_64\dcmtk-3.7.0-win64-dynamic\bin\storescp.exe`。不要放行每次启动都会变化的 PyInstaller `_MEI...` 临时路径。

三种发布物的 PDI 阅片过程均不连接互联网：OHIF 静态资源已内置并校验，同时会复制一份到便携目录；DICOM 清单和影像数据只从用户选择的 PDI 根目录读取。

安装版不要求目标电脑预装 Python。再次运行新版安装包时，会识别原安装记录并在原目录完成覆盖升级；用户配置、注册码和试用计数保存在 Windows 用户数据目录，升级和卸载都不会覆盖或删除这些数据与下载结果。默认下载目录为“文档\DcmGet\Dicom”。

### Windows 自动与增量更新

- 自动更新只在 Windows x64 安装版的 `8786` 统一管理中心启用。便携版、ZIP 和源码运行不会自动替换程序文件。
- 默认策略为外网可用时后台检查。没有外网时只记录“离线”状态，不阻塞管理中心、Profile 或 DICOM 下载。专网环境可在“软件更新”中关闭自动检查；关闭后不会访问更新服务器。
- 更新客户端只访问由 `bwg-snell` 提供的固定地址 `https://dcmget.v2ex.com.cn/updates/`，不依赖 GitHub Releases。地址不可达时显示离线状态并等待下次检查，不影响 DICOM 下载。
- 管理中心优先选择与当前版本精确匹配的组件增量包，只下载并替换发生变化的 `DcmGet.exe` 或 `_internal/**` 文件。增量条件不成立时自动选择完整安装包。
- 稳定通道使用单文件 `UPDATE-MANIFEST.signed.json`。客户端先使用内置的独立 Ed25519 更新公钥验证清单，再校验版本、下载地址边界、文件大小、SHA-256、允许替换路径和安装树哈希；更新私钥不放入客户端或更新服务器，也不与注册码私钥共用。
- 组件更新在受限暂存目录中逐文件备份、替换和校验；失败时回滚，并按更新前状态恢复 `kayisoft-dcmget` 服务。Profile 配置、任务恢复点、日志、授权/试用数据和下载影像都不在可替换路径中。
- 3.6.0 及更旧安装未内置新的 Ed25519 更新公钥，首次升级到 3.6.1 需要手动运行一次完整测试安装包。完成这次引导升级后，后续小版本即可通过管理中心下载组件更新；只有 Python/DCMTK、Windows 服务或安装布局变化时才需要完整安装包。

安装完成后，`kayisoft-dcmget` 以 LocalSystem 自动服务运行，但固定使用安装用户的 `%APPDATA%`、`%LOCALAPPDATA%` 和 `%USERPROFILE%`，因此会继续读取原有 Profile、恢复点和配置。桌面和开始菜单的“DcmGet”主入口会启动本机 WebView2 窗口并连接 `http://127.0.0.1:8786/`；关闭该窗口不停止后台服务或下载任务。开始菜单另提供“DcmGet 启动后台服务”和“DcmGet 停止后台服务”。内置服务权限允许普通本机用户查询、启动和停止服务，不需要应用密码或再次提升权限。注册、升级和卸载 Windows 服务本身仍属于系统级安装操作，首次运行安装器时需要管理员/UAC。

同一局域网的管理电脑可打开 `http://<DcmGet Windows 主机 IP>:8786/`。任务、设置、日志、PDI 和运维功能都在统一工作台内完成；管理中心只通过本机回环地址访问各 Profile 的独立 Web 端口，不会把浏览器跳转到其他端口。为避免与管理中心冲突，Profile 的 SCP 和 Web 端口均不能使用 `8786`。

新建 Profile 默认保持停止。用户在统一工作台明确启动后，运行选择会写入独立的管理状态文件，`kayisoft-dcmget` 在 Windows 或服务重启后只恢复这些 Profile；用户明确停止后则保持停止。受监管的 Profile 异常退出时仍会自动拉起，删除 Profile 前必须先停止。切换当前 Profile 只改变右侧页面内容，不会停止其他正在执行的下载任务。

发布门禁会复核 Windows 运行时和应用载荷为 AMD64，核对精简 DCMTK 白名单，并输出 `RELEASE-MANIFEST.json` 和 `SHA256SUMS.txt`。GitHub Actions 配置 `DCMGET_SIGN_CERTIFICATE_BASE64` 与 `DCMGET_SIGN_CERTIFICATE_PASSWORD` 机密后，会对 EXE 载荷和最终发布物执行 Authenticode 签名、时间戳及签名复核。未配置证书时仍可生成内部测试包，但发布清单会明确标记 `UNSIGNED`，Windows SmartScreen 也可能显示未知发布者；对外商业交付应只发放清单为 `SIGNED` 的发布物。

维护者可在 GitHub Actions 中手动运行 `Windows Release` 工作流，也可在 Windows x64 构建机执行：

```powershell
python -m pip install -r requirements-build.txt
python scripts/download_dcmtk.py --platform windows-x86_64
python scripts/build_windows.py --version 3.7.1
```

PyInstaller 生成的可执行文件已包含 Python 解释器，因此不再额外运行独立的 Python 安装程序。

## 局域网 Web 访问与安全边界

- Windows 安装版管理中心固定监听 `0.0.0.0:8786`；首个 Profile 默认监听 `0.0.0.0:8787`，后续 Profile 使用各自的 `web_port`。本机优先从 `http://127.0.0.1:8786/` 进入，局域网管理端使用 Windows 主机 IP 访问同一端口。
- 按当前产品决策，Web 控制台使用裸 HTTP，不生成证书，也不启用 HTTPS。患者相关数据、配置和操作内容在网络中**未加密传输**，只能部署在受信任的医院内网或隔离 VLAN；禁止把端口映射到公网、访客 Wi-Fi 或不受信任网络。
- 控制台不再要求设置或输入应用密码。浏览器会自动取得绑定当前客户端地址的短期会话，修改操作仍要求同源请求与 CSRF 令牌；这些机制不能替代传输加密，也不能把裸 HTTP 安全地暴露到公网。
- 主控制台不向第三方发送遥测，不引用 CDN。PACS、DICOM、日志、配置、任务状态和 PDI 均留在部署主机或用户明确选择的目录。
- Windows 安装器的两条入站规则只允许域/专用网络并阻止边缘遍历；如网络被 Windows 标记为“公用”，远端访问会被拒绝。便携版和 ZIP 需由管理员按相同边界手工配置。

## 试用与软件注册

程序启动时不再要求输入日期口令。未注册电脑默认可免费启动 30 个批量下载任务；每个任务仅在第一次真正开始 C-MOVE 时扣一次，暂停、恢复、重试失败项和 PDI 导出都不会重复扣次。配置错误、`storescp` 启动失败或只打开页面不会扣次。试用次数和注册码属于整台电脑，多开 Profile 不会各自获得额外次数。试用结束后必须输入当前电脑的离线注册码。Web 控制台会显示剩余次数，并可随时点击“软件注册”查看和复制机器码。

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

使用源码时，在项目目录执行：

### Windows PowerShell

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_windows.ps1
.\scripts\run_ui.ps1
```

部署脚本会检查 AMD64/x64 Python 3.10+、拒绝 32 位和原生 ARM64 Python、重建干净的 `.venv`、安装 FastAPI/Uvicorn 等运行依赖、下载 x64 DCMTK 3.7.0、准备离线 Web 控制台、离线 OHIF 和 PDI 本地启动器，并检查 VC++ Runtime。管理员模式下会为主程序和实际监听的 `storescp.exe` 创建仅限域/专用网络的程序级入站规则；规则不固定端口，可覆盖不同 Profile 配置。Windows 11 ARM64 用户应安装 x64 Python 并通过系统兼容层运行。重建虚拟环境不会改动配置、注册码、试用记录或下载结果。

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

macOS/Linux 使用与 Windows 相同的离线 OHIF 静态资源；启动器只为当前 PDI 目录提供本地只读 HTTP 服务，不对外网开放。

## 使用 Web 控制台

1. 启动 DcmGet。Windows 安装版由 `kayisoft-dcmget` 服务自动启动统一管理中心，并恢复用户上次明确选择运行的 Profile；新 Profile 需要在工作台中手动启动。点击 DcmGet 快捷方式会打开统一工作台；源码版、便携版和 ZIP 版仍由程序进程启动 Web 服务。全程不要求应用密码，页面顶部始终提示当前为“仅限可信内网、HTTP 未加密”。
2. 打开“设置”，填写 PACS 地址、端口和 PACS AE。三类 AE Title 必须是 1-16 个可打印 ASCII 字符，非法字符会在对应字段直接提示。首个实例默认使用本机调用 AE `DCMGET`、接收 AE `DCMGET` 和接收端口 `6666`；如需下载后脱敏，可在同一页启用匿名处理并选择方案。
3. 选择或拖入 TXT、CSV 或 XLSX，也可以直接粘贴多行检查号。CSV/XLSX 可选择表头列；空行会忽略，重复项会按首次出现顺序去重，公式单元格会被拒绝。选择服务器上的目标目录、目录模板和 PDI 快捷选项后开始任务。
4. 开始前会检查配置、DCMTK、目标目录、磁盘保留空间和当前 Profile 的接收端口。`storescp` 就绪后，程序按检查号顺序执行 `movescu`。
5. 任务主页显示总进度、当前检查号、文件数、下载速度、状态统计、PDI 状态和操作按钮。可暂停、继续、停止、重试失败项、打开目标目录、打开 PDI 和查看默认脱敏的验收报告；关闭浏览器不会停止后台任务，也不会删除已收到文件。
6. 需要同时运行另一个任务时，在统一工作台左侧点击“新建”，先配置新 Profile，再明确启动。新 Profile 使用独立后台进程；请改用不同 Web 端口和接收端口，通常也要使用独立接收 AE，并同步配置 PACS 的 Move Destination。不同 Profile 可以选择不同目标目录和 PDI 设置。源码版和便携版仍可使用 `--profile N` 明确启动指定 Profile。
7. 任务不超过 200 个检查号时显示逐项文件数、速度和耗时；超过 200 个时只显示聚合进度和状态统计，避免 40,000 条任务拖慢浏览器。所有归档文件统一以 `.dcm` 结尾。
8. 运行日志默认只显示错误，便于快速定位需要处理的问题；开启“显示详细日志”后才显示调试、信息、成功和警告。“清空显示”只清空浏览器视图，当前 Profile 的磁盘日志始终完整保留。
9. 如需交付 U 盘，可在任务主页勾选“下载完成后生成 PDI 便携目录”并选择保存目录；首次使用仍需在设置中填写机构名称，阅片器等高级选项也在设置页管理。批次结束后点击“打开影像”，或用“打开导出目录”将整个目录复制到 U 盘。重启程序后也可点击“打开已有 PDI 目录”直接选择根目录阅片，无需寻找 JSON 文件。PDI 失败可单独重试，无需重新下载。

## 多实例运行与恢复

- DcmGet Web 版采用“一个 Profile 后台进程对应一个任务”模式，没有进程内任务列表或全局调度器。每次启动都会优先选择含未完成恢复点且当前未被占用的 Profile，否则选择编号最小的空闲 Profile。
- Profile 以 `i1`、`i2` 等编号持久保存。每个 Profile 分别拥有配置文件、Web 端口、任务恢复点、会话状态和日志目录；关闭界面不会结束进程或删除任何状态。也可以用 `--profile N` 明确打开指定 Profile；若它已经运行，Windows 会重新打开连接现有地址的 WebView 窗口，不会创建重复后台进程。
- 管理中心的“Profile 工作台”可为任一 Profile 生成固定使用 `--profile N` 的桌面快捷方式。默认名称取该 Profile 的接收端口和接收 AE，例如 `dcmget-6666-DCMGET`；以后点击该快捷方式会启动对应进程或打开其现有页面。安装器创建的通用 DcmGet 快捷方式则固定打开 `8786` 管理中心。
- 每个运行实例只启动一个外部 DCMTK `storescp`，任务中的 `movescu` 逐条顺序执行。不同实例必须使用不同监听端口；通常还应使用不同接收 AE，例如 `DCMGET:6666` 与 `DCMGET2:6667`，并在 PACS 中把两个 Move Destination 分别映射到本机 IP 和对应端口。端口冲突会在预检时给出明确错误，不会自动共用接收器。
- 因为一个实例同一时刻只处理一个 C-MOVE，当前接收时间窗内新到达的 DICOM 会直接作为当前检查号结果接收，不要求返回文件中的 `AccessionNumber` 与请求值一致；缺少该标签也不会再被隔离。程序仍会验证 DICOM 文件结构、SOP Instance UID 和最终文件完整性，损坏文件不会伪装成成功结果。
- 每处理完一个检查号都会写入当前 Profile 的恢复点。主机重启、进程异常或服务端临时中断后，再次启动会优先取得该 Profile 并继续未完成任务；已完成项不会重复下载，退出时正在处理的检查号会重新执行。浏览器关闭不属于任务中断。遗留的 DCMTK 子进程会先经过身份校验再清理；只有异常退出后无法确定属于哪个 Profile 的遗留暂存文件才会移入隔离目录，不会猜测归属或自动删除。
- 从 2.8.x 升级时，旧 `tasks.sqlite3` 中的未完成任务会一次性拆分迁移到独立实例槽；从 2.7.x 直接升级时，全局 `active-task.sqlite3` 也会迁移。迁移保留任务编号、已完成结果、PDI 阶段和试用扣次状态；原数据库和迁移记录会保留，防止重复迁移或数据丢失。
- 接收暂存目录与目标目录可以位于不同卷。Windows 从 `C:` 暂存归档到 `D:`、U 盘或网络共享，以及 macOS/Linux 的跨文件系统归档，都会先在目标目录写入并同步临时文件，再原子发布最终 `.dcm`；复制失败时保留源文件，避免跨卷重命名失败造成丢失。
- 多个实例可以选择同一个目标根目录。最终文件发布使用跨进程锁；相同 SOP UID 且内容相同的文件会去重，内容不同的同名对象不会互相覆盖。为了便于区分任务和减少目录竞争，仍建议为不同实例选择独立目标目录。

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
├── VIEWER/               # 程序运行数据、64 位校验脚本与离线中文阅片资源，请勿删除
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
- 可按介质容量生成 `DCMGET_PDI_SET_日期时间`。每个 `VOLUME_###` 都是包含独立 `DICOMDIR`、清单和阅片入口的完整介质目录；同一 Study 永远不跨卷，超过单卷容量的 Study 会独占一卷并明确告警。容量设为 `0` 时不分卷。
- 将 PDI 复制到 U 盘或其他介质后，使用“运维工具 → 验证已复制 PDI”核对 SHA-256 清单、`DICOMDIR` 内部引用和离线阅片资源。验证结果不等于临床诊断适用性或匿名合规证明；验收报告会写到介质目录之外，不改动已复制内容。
- 未匿名的 DICOM 可能包含患者隐私。外发前应启用合适的匿名方案并完成复核。

## 命令行

保留两个直接入口：

```bash
python DICOM_download_ui.py
python DICOM_download_ui.py --profile 2
python DICOM_download_script.py --config config.json
python DICOM_download_script.py --config config.json --accessions 检查清单.xlsx --accession-column 检查号
python DICOM_download_script.py --task-state <独立任务恢复点路径>

# 运维命令互斥，不启动下载任务
python DICOM_download_script.py --config config.json --health-check
python DICOM_download_script.py --config config.json --support-bundle DcmGet-support.zip
python DICOM_download_script.py --backup-profiles DcmGet-profiles.zip
python DICOM_download_script.py --restore-profiles DcmGet-profiles.zip
python DICOM_download_script.py --verify-pdi E:\DCMGET_PDI_20260718_120000
```

Web 入口使用持久 Profile：未指定 `--profile` 时自动取得空闲或待恢复 Profile；指定 `--profile N` 时只使用对应 Profile，若它已被另一个进程占用则打开现有 Web 地址后退出新进程。`--config PATH` 只作为首次创建该 Profile 时的配置初始化模板，不会把 Profile 的长期身份改到模板所在目录；旧版本曾在自定义配置目录创建的 Profile 会在首次打开时自动迁移到规范目录，并保留原文件。命令行继续作为单任务前台客户端运行，并保留显式 `--task-state` 恢复参数；`--task-id` 仅用于尚未迁移的 2.8 旧任务目录。默认旧任务目录迁移到实例 Profile 后，命令行会拒绝再次打开它，避免与 Web 后台重复执行同一批任务。所有入口共享整机 30 次试用计数和注册码，不再要求日期口令。可用 `--license PATH` 指定注册码文件；`--accept-download-failures` 可接受当前下载结果并继续 PDI，`--discard-checkpoint` 可明确放弃所选恢复点。

`--health-check` 输出运行时、DCMTK、目录、磁盘、端口、进程和 PACS TCP 可达性检查；这是网络可达性检查，不是 DICOM C-ECHO。`--support-bundle` 生成默认脱敏的诊断 ZIP。Profile 备份包含配置和可选显示名，不包含注册码、试用计数、匿名密钥、下载结果或任务恢复点；恢复前会自动创建现有配置与显示名快照，旧版 v1 备份仍可兼容恢复。管理中心的“Profile 工作台”可复制、重命名、删除安全空闲 Profile，并启动 Profile 或创建固定 `--profile N` 快捷方式；复制时会自动分配未占用的接收端口和 Web 端口，接收 AE 与 PACS Move Destination 映射仍必须由用户确认。CLI 和 Web 控制台的 PDI 验证都可直接选择单卷目录，或选择含 `VOLUME_SET.json` 的分卷集根目录并严格按清单逐卷验证。

命令行退出码：

- `0`：下载及启用的 PDI 导出全部完成
- `1`：配置、预检或接收器启动失败
- `2`：存在下载失败、部分成功，或启用的 PDI 导出失败/部分成功
- `130`：用户取消

## 配置

首次部署会从 `config.example.json` 创建 `config.json`。Web 入口传入的 `--config PATH` 仅用于初始化尚未创建的 Profile，之后始终读写该 Profile 的规范配置目录；旧版自定义目录中的 Profile 会自动迁移且不覆盖原文件。主要字段：

| 字段 | 说明 |
| --- | --- |
| `dcmtk_bin_dir` | DCMTK 的 bin 目录；留空时自动查找 |
| `access_numbers_file_path` | 检查号 TXT、CSV 或 XLSX 路径；多列表格需明确选择检查号列 |
| `dicom_destination_folder` | DICOM 保存目录 |
| `pacs_server_ip` / `pacs_server_port` | PACS 地址与端口 |
| `calling_ae_title` | movescu 本机调用 AE |
| `pacs_ae_title` | PACS AE |
| `storage_ae_title` / `storage_port` | C-STORE 接收 AE 与端口 |
| `web_bind_address` | Web 监听地址；局域网版默认 `0.0.0.0` |
| `web_port` | Web 控制台 HTTP 端口；首个 Profile 默认 `8787`，不同 Profile 必须不同，且不能使用管理中心保留的 `8786` |
| `web_open_browser` | 后台启动后是否自动打开本机界面；Windows 使用 WebView2，macOS/Linux 源码运行使用系统浏览器 |
| `web_session_timeout_minutes` | 自动安全会话的空闲超时时间 |
| `max_concurrent_moves` | 仅为兼容 2.8.x 配置保留，自 2.9.0 起不再生效；每个实例始终顺序执行 C-MOVE |
| `directory_template` | 目录组合模板；支持 `PatientID`、`AccessionNumber`、`StudyInstanceUID` |
| `anonymization_enabled` | 是否在最终归档前启用 DICOM 元数据处理，默认 `false` |
| `anonymization_profile` | `basic`、`research` 或 `strict`；默认 `research` |
| `pdi_export_enabled` | 是否在每批下载结束后自动生成 PDI 便携目录，升级配置默认 `false` |
| `pdi_institution_name` | PDI 首页和说明中的机构名称；启用 PDI 时必填 |
| `pdi_output_folder` | PDI 输出根目录；留空时使用 `DICOM 保存目录/PDI` |
| `pdi_include_ohif_viewer` | 是否加入中文 OHIF 和本地只读 HTTP 启动器；默认 `true` |
| `pdi_volume_size_bytes` | PDI 单卷目标容量；`0` 表示不分卷，同一 Study 不会被拆分 |
| `minimum_free_space_bytes` | 下载前和任务中必须保留的磁盘空间；默认 2 GiB，`0` 关闭保护 |
| `auto_retry_attempts` | 单个检查号瞬时故障的自动重试次数；默认 `2` |
| `auto_retry_backoff_seconds` | 自动重试的基础退避秒数；默认 `3` |
| `circuit_breaker_failures` | 连续瞬时失败到达该数量时暂停后续队列；默认 `5` |
| `max_log_file_size_bytes` | 单个日志文件最大字节数 |

旧版配置会自动迁移到 v8，且不覆盖现有设置；原 PACS、AE、目录、PDI 机构名称和输出位置会保留。v8 为 Web 监听、本机界面自动打开和会话超时补入默认值；v7 的 PDI 分卷、磁盘保护、重试和熔断语义保持不变。`max_concurrent_moves` 字段仅为读取旧配置兼容而保留，自 2.9.0 起不显示该设置，也不会据此并发 C-MOVE。首次创建 `i1` 时会从现有配置初始化；后续新 Profile 以首个 Profile 配置为模板，并自动选择可用接收端口和 Web 端口，再由用户确认接收 AE、目标目录以及 PACS Move Destination 映射。v5 的 OHIF 迁移语义保持不变：旧 JPEG 预览和 Weasis 选项均关闭时继续关闭，任一开启时启用 OHIF，迁移后不再保存旧字段。DCMTK 的查找顺序是：Profile 配置目录、`.runtime/dcmtk` 部署目录、旧版 `dcmtk/bin`、系统 `PATH`。

## 下载流程与故障处理

程序从启动最早阶段开始写入独立诊断日志。`dcmget-diagnostics-<进程号>.log` 记录启动、Python、Web 服务和后台线程异常，`dcmget-crash-<进程号>.log` 记录原生崩溃信息；不同进程不会争用同一个轮转日志文件。即使 Web 服务尚未就绪，也可以直接查看：

- Windows：`%LOCALAPPDATA%\DcmGet\logs\dcmget-diagnostics-<进程号>.log`，安装版也可从开始菜单点击“DcmGet 诊断日志”。
- macOS：`~/Library/Application Support/DcmGet/logs/dcmget-diagnostics-<进程号>.log`。
- Linux：`$XDG_STATE_HOME/dcmget/logs/dcmget-diagnostics-<进程号>.log`，未设置时为 `~/.local/state/dcmget/logs/dcmget-diagnostics-<进程号>.log`。

Profile Web 的“管理”页可打开该固定目录；启动失败时请提供对应时间和进程号的诊断、崩溃文件。非匿名任务的下载与接收日志写入影像目标目录下的 `_DcmGetLogs`，便于与结果一同交付；匿名任务日志始终位于 Profile 私有状态目录 `iN/logs`，不会泄露到结果目录。目标日志目录不可写时也会回退到该 Profile 私有目录。

每个实例创建带微秒级唯一名称的独立暂存子目录，先启动 `storescp -aet <接收AE> -od <暂存目录> +xa <端口>`，确认进程存活且端口就绪后，再顺序执行带连接与 DIMSE 超时的 `movescu --no-port`。当前检查号完成后读取本次新增 DICOM 的元数据，按该实例配置中的目标目录和目录模板归档并补充 `.dcm` 后缀；关键元数据缺失时使用安全占位值，匿名失败文件会保留并写入日志。无法可靠归属的文件进入隔离目录，绝不猜测写入其他任务。

每个任务还会写入独立的 WAL SQLite 验收台账。在可选匿名化前，程序记录返回对象的 Accession Number、Study/Series/SOP UID、字节数和元数据错误，将归属标记为“核对通过”、“内容不一致”或“无法核验”。这是下载后的审计证据，不是接收前置条件：核验告警不会丢弃已收到的有效 DICOM，也不会擅自把传输成功改成失败。任务结束后会在目标目录的 `_DcmGetReports/task-<任务编号前8位>/` 输出默认脱敏的 HTML、CSV 和 JSON 验收报告。

DCMTK `storescp` 在 Windows、macOS 和 Linux 上接收 C-STORE association，并按原始传输语法写入，不需要为下载而解压像素。若 PACS 已返回待处理响应、对象无法归属或接收连接中止但没有有效落盘文件，任务会标记为失败而不是“无数据”，并可通过“重试失败项”再次执行。

开始前和执行期间都会检查磁盘可用空间，低于保留阈值时安全停止后续下载。只有被识别为瞬时故障的检查号才会按配置自动退避重试；连续失败达到熔断阈值时会暂停后续队列，避免在 PACS、网络或接收端持续异常时快速刷完整批失败。

速度按暂存目录中实际收到的原始 DICOM 字节计算；任务进行中每 0.5 秒采样一次，单个检查号结束后显示其平均传输速度。匿名转换和最终归档耗时不会计入网络下载速度。

- “接收端口已占用”：关闭占用程序或在设置中更换端口，并同步 PACS 的 Move Destination。
- 修改默认接收端口 `6666` 后：Windows 安装版和默认源码部署的 `storescp.exe` 程序规则会自动兼容所有自定义端口；如果在设置中改用另一套 DCMTK 路径，需要为那一份 `storescp.exe` 单独放行域/专用网络入站连接。
- “C-MOVE 完成但未收到文件”：检查 PACS 中接收 AE、客户端 IP、接收端口及防火墙映射。
- DCMTK 启动失败：下载需要同一套 `storescp` 和 `movescu`，PDI 还需要 `dcmmkdir`、`dcmdump`。Windows 3.7.1 成品已包含这四个程序及所需 DLL、字符集和许可证数据，不包含 `dcmj2pnm`、`dcmdjpeg` 等未使用工具；源码部署或自定义路径请选择同时包含这四个程序的 DCMTK 3.7.0 `bin` 目录。
- Windows 本机界面启动失败：DcmGet 只使用 Edge Chromium WebView，不回退到旧版 IE 内核或默认浏览器。请安装或修复 Microsoft Edge WebView2 Runtime 后重新打开 DcmGet；后台服务和已经运行的下载不受窗口启动失败影响。
- Windows 缺少 DLL：安装部署脚本提示的 Microsoft Visual C++ x64 Runtime。

## 开发与验证

```bash
python -m pip install -r requirements-dev.txt

cd frontend
npm ci
npm test
npm run build
cd ..

python -m pytest
python DICOM_download_ui.py --web-self-test --config build/web-self-test.json
python scripts/build_deploy_bundle.py
```

Node.js 只用于开发和发布阶段编译前端；DcmGet 安装版、便携版、ZIP 和源码运行时均不启动 Node.js，也不从 CDN 加载脚本、样式、字体或图标。前端构建使用固定文件名输出到 `dcmget/webui-react/index.html`、`dcmget/webui-react/app.js`、`dcmget/webui-react/app.css` 和 `dcmget/webui-react/theme.js`，以便 Windows 组件增量更新只替换实际变化的应用文件。

从 3.6.1 升级到首个 React 工作台版本时，由于安装内容会移除 NiceGUI 运行库和旧静态资源，需要手工运行一次完整 Windows 安装包；完成这次迁移后，前端和 Python 小版本可继续通过管理中心下载小体积组件增量包。

`build_deploy_bundle.py` 仅保留为维护者本地生成源码归档的工具；CI 与发布工作流不会上传源码、macOS 或 Linux 安装包，当前发布物只有 Windows x64 三种形式。

自动测试覆盖免密码会话/CSRF、Web API、离线静态资源、浏览器断开后的后台任务生命周期和任务恢复。真实 DICOM 集成测试覆盖每 Profile 独立 `storescp`、顺序 `movescu`、多进程端口隔离、压缩传输语法原样接收和跨进程安全归档，并检查输出文件的 `DICM` 标识。

## 许可

项目原仓库未提供独立开源许可证，因此本部署包没有擅自选择许可证。详见 [LICENSE](LICENSE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
