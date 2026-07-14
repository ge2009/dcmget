# 第三方组件说明

DcmGet 在部署、构建或运行时使用以下独立组件。源码部署 ZIP 不包含第三方二进制；Windows EXE/安装器会包含运行所需的 Python 包、PyInstaller bootloader 和 DCMTK 运行库。

- [DCMTK](https://dicom.offis.de/en/dcmtk/dcmtk-tools/)：Copyright OFFIS e.V.，遵循 DCMTK 自带许可。
- [PyQt5](https://www.riverbankcomputing.com/software/pyqt/)：由 Riverbank Computing 发布，使用者需按其 GPL 或商业许可条款使用。
- [cryptography](https://github.com/pyca/cryptography)：Apache License 2.0 / BSD 3-Clause 双许可。
- [pydicom](https://github.com/pydicom/pydicom)：MIT License。
- [filelock](https://github.com/tox-dev/py-filelock)：The Unlicense。
- [PyInstaller](https://pyinstaller.org/)：GPL 2.0-or-later，并带有适用于所生成应用的 bootloader exception。
- Python 及标准库：遵循 [Python Software Foundation License](https://docs.python.org/3/license.html)。

闭源商业发布前必须确认已取得适用的 PyQt 商业许可；否则需按 PyQt/GPL 条款履行相应义务。各组件的完整许可文本与实际安装版本随附文件为准。
