# 第三方组件说明

DcmGet 在部署、构建或运行时使用以下独立组件。源码部署 ZIP 不包含第三方二进制；部署脚本会按需下载并校验运行资源。Windows 安装器、独立目录版和单文件便携 EXE 均包含运行所需的 Python 包、PyInstaller bootloader、DCMTK 运行库、DcmGet 局域网 Web 服务、PDI 本地服务以及准备成功时的 OHIF Viewer 离线资源。

- [DCMTK](https://dicom.offis.de/en/dcmtk/dcmtk-tools/)：Copyright OFFIS e.V.，遵循 DCMTK 自带许可。
- [cryptography](https://github.com/pyca/cryptography)：Apache License 2.0 / BSD 3-Clause 双许可。
- [FastAPI](https://github.com/fastapi/fastapi)：MIT License。
- [React](https://github.com/facebook/react) 与 [React DOM](https://www.npmjs.com/package/react-dom)：MIT License。DcmGet 使用 React 构建唯一的本地 Web 工作台并渲染到浏览器或 WebView2。
- [Base UI for React](https://github.com/mui/base-ui)：MIT License。DcmGet 使用 `@base-ui/react` 提供无样式、可访问的交互组件基础，并由本项目样式统一呈现医疗工作台界面。
- [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss)：MIT License。仅在构建阶段生成工作台样式，运行时不加载 Tailwind、Node.js 或外部 CDN。
- [Motion](https://github.com/motiondivision/motion)：MIT License。用于工作台中克制的状态与界面过渡，并遵循系统的减少动态效果设置。
- [Zod](https://github.com/colinhacks/zod)：MIT License。用于校验前端接收的 API 数据和用户输入边界。
- [Lucide](https://github.com/lucide-icons/lucide)：ISC License。DcmGet 使用 `lucide-react` 提供随前端一起编译的本地图标，不从图标服务或 CDN 加载资源。
- [lucide-animated](https://github.com/pqoqubbw/icons)：MIT License。DcmGet 参考其公开的 Lucide + Motion 动效参数，并在本项目统一的 `AnimatedIcon` 封装中进行适配；运行时不访问其网站、注册表或 CDN。
- [Vite](https://github.com/vitejs/vite)：MIT License；[TypeScript](https://github.com/microsoft/TypeScript)：Apache License 2.0。两者只用于开发和发布阶段生成固定文件名的离线前端资源，不作为客户端运行时依赖。
- [pywebview](https://github.com/r0x0r/pywebview)：BSD 3-Clause License。Windows 交互入口使用 pywebview 调用系统 WebView2 Runtime 承载本地 React 工作台。
- [pythonnet](https://github.com/pythonnet/pythonnet)：MIT License。由 pywebview 在 Windows 上用于 .NET 互操作。
- [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)：遵循 Microsoft 软件许可条款；DcmGet 不内置该运行时，Windows 交互入口使用系统现有的 WebView2 Runtime。
- [Starlette](https://github.com/Kludex/starlette)：BSD 3-Clause License。
- [Uvicorn](https://github.com/Kludex/uvicorn)：BSD 3-Clause License。
- [Pydantic](https://github.com/pydantic/pydantic)：MIT License。
- [pydicom](https://github.com/pydicom/pydicom)：MIT License。
- [pynetdicom](https://github.com/pydicom/pynetdicom)：MIT License。
- [psutil](https://github.com/giampaolo/psutil)：BSD 3-Clause License。
- [filelock](https://github.com/tox-dev/py-filelock)：The Unlicense。
- [PyInstaller](https://pyinstaller.org/)：GPL 2.0-or-later，并带有适用于所生成应用的 bootloader exception。
- [OHIF Viewer 3.12.6](https://github.com/OHIF/Viewers/tree/v3.12.6)：MIT License。DcmGet 固定使用官方 `@ohif/app` npm tarball，校验字节数和 SHA-256 后仅安全解包 `package/dist`，覆盖仅允许本地 DICOM JSON 数据源的配置并生成逐文件 SHA-256 清单；PDI 查看器目录随附 `LICENSE-OHIF.txt`、`THIRD_PARTY-OHIF.md` 和来源清单。
- [WinSW 2.12.0](https://github.com/winsw/winsw/releases/tag/v2.12.0)：MIT License。Windows x64 安装版固定下载并校验官方 `WinSW-x64.exe`，将其重命名为 `kayisoft-dcmget.exe`，用于注册和控制同名 Windows 服务；安装目录随附 `LICENSE-WINSW.txt`。
- Python 及标准库：遵循 [Python Software Foundation License](https://docs.python.org/3/license.html)。

各组件的完整许可文本与实际安装版本随附文件为准。
