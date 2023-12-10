import sys
import subprocess
import os
import json
from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QLineEdit, QPushButton, QVBoxLayout

class DICOMDownloadApp(QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout()

        # 配置输入字段
        self.movescuPathEdit = QLineEdit(self)
        self.accessNumbersFilePathEdit = QLineEdit(self)
        self.destinationFolderEdit = QLineEdit(self)
        self.pacsServerIPEdit = QLineEdit(self)
        self.pacsServerPortEdit = QLineEdit(self)
        self.aetEdit = QLineEdit(self)
        self.aecEdit = QLineEdit(self)
        self.aemEdit = QLineEdit(self)
        self.networkPortEdit = QLineEdit(self)
        self.maxLogFileSizeBytesEdit = QLineEdit(self)

        startButton = QPushButton('Start Download', self)
        startButton.clicked.connect(self.startDownload)

        # 添加界面元素到布局
        layout.addWidget(QLabel('movescu Executable Path'))
        layout.addWidget(self.movescuPathEdit)
        layout.addWidget(QLabel('Access Numbers File Path'))
        layout.addWidget(self.accessNumbersFilePathEdit)
        layout.addWidget(QLabel('DICOM Destination Folder'))
        layout.addWidget(self.destinationFolderEdit)
        layout.addWidget(QLabel('PACS Server IP'))
        layout.addWidget(self.pacsServerIPEdit)
        layout.addWidget(QLabel('PACS Server Port'))
        layout.addWidget(self.pacsServerPortEdit)
        layout.addWidget(QLabel('Application Entity Title (AET)'))
        layout.addWidget(self.aetEdit)
        layout.addWidget(QLabel('Called AE Title (AEC)'))
        layout.addWidget(self.aecEdit)
        layout.addWidget(QLabel('Calling AE Title (AEM)'))
        layout.addWidget(self.aemEdit)
        layout.addWidget(QLabel('Network Port'))
        layout.addWidget(self.networkPortEdit)
        layout.addWidget(QLabel('Max Log File Size (Bytes)'))
        layout.addWidget(self.maxLogFileSizeBytesEdit)
        layout.addWidget(startButton)

        self.setLayout(layout)

    def startDownload(self):
        # 这里需要添加实际的下载逻辑
        print("Starting download...")

        # 示例：打印出配置信息
        print("movescu Executable Path:", self.movescuPathEdit.text())
        # ... (打印出其他配置信息)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = DICOMDownloadApp()
    ex.show()
    sys.exit(app.exec_())
