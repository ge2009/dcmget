# 第三方组件说明

DcmGet 在部署、构建或运行时使用以下独立组件。源码部署 ZIP 不包含第三方二进制；部署脚本会按需下载并校验运行资源。Windows 安装器、独立目录版和单文件便携 EXE 均包含运行所需的 Python 包、PyInstaller bootloader、DCMTK 运行库、DcmGet 局域网 Web 服务、PDI 本地服务以及准备成功时的 OHIF Viewer 离线资源。

- [DCMTK](https://dicom.offis.de/en/dcmtk/dcmtk-tools/)：Copyright OFFIS e.V.，遵循 DCMTK 自带许可。
- [cryptography](https://github.com/pyca/cryptography)：Apache License 2.0 / BSD 3-Clause 双许可。
- [FastAPI](https://github.com/fastapi/fastapi)：MIT License。
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
