# 第三方组件说明

DcmGet 在部署、构建或运行时使用以下独立组件。源码部署 ZIP 不包含第三方二进制；部署脚本会按需下载并校验运行资源。Windows 安装器与独立目录版包含运行所需的 Python 包、PyInstaller bootloader、DCMTK 运行库以及准备成功时的 Weasis 便携查看器。为控制体积，单文件便携 EXE 不包含 Weasis，PDI 仍可生成 DICOMDIR 和网页预览。

- [DCMTK](https://dicom.offis.de/en/dcmtk/dcmtk-tools/)：Copyright OFFIS e.V.，遵循 DCMTK 自带许可。
- [PyQt5](https://www.riverbankcomputing.com/software/pyqt/)：由 Riverbank Computing 发布，使用者需按其 GPL 或商业许可条款使用。
- [cryptography](https://github.com/pyca/cryptography)：Apache License 2.0 / BSD 3-Clause 双许可。
- [pydicom](https://github.com/pydicom/pydicom)：MIT License。
- [psutil](https://github.com/giampaolo/psutil)：BSD 3-Clause License。
- [filelock](https://github.com/tox-dev/py-filelock)：The Unlicense。
- [PyInstaller](https://pyinstaller.org/)：GPL 2.0-or-later，并带有适用于所生成应用的 bootloader exception。
- [Weasis 4.7.1](https://github.com/nroduit/Weasis/tree/v4.7.1)：Eclipse Public License 2.0 或 Apache License 2.0。DcmGet 固定使用官方 Windows x64 MSI，校验 SHA-256 后提取便携 app-image，并对提取结果生成逐文件 SHA-256 清单；PDI 查看器目录随附 `LICENSE-Weasis.txt`、`THIRD_PARTY-Weasis.md` 和来源清单。
- Python 及标准库：遵循 [Python Software Foundation License](https://docs.python.org/3/license.html)。

闭源商业发布前必须确认已取得适用的 PyQt 商业许可；否则需按 PyQt/GPL 条款履行相应义务。各组件的完整许可文本与实际安装版本随附文件为准。
