import sys
import subprocess
import os
import json
from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QLineEdit, QPushButton, QVBoxLayout, QGroupBox, QFormLayout

def load_config(json_path):
    """ 从JSON文件中加载配置 """
    with open(json_path, 'r', encoding='utf-8') as file:
        return json.load(file)

def save_config(json_path, config):
    """ 保存配置到JSON文件 """
    with open(json_path, 'w', encoding='utf-8') as file:
        json.dump(config, file, indent=4)

class DICOMDownloadApp(QWidget):
    def __init__(self, config, config_path):
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout()
        configGroup = QGroupBox("Configuration Settings")
        formLayout = QFormLayout()

        # 配置输入字段，预填充JSON配置数据
        self.movescuPathEdit = QLineEdit(self.config.get('movescu_executable_path', ''))
        self.accessNumbersFilePathEdit = QLineEdit(self.config.get('access_numbers_file_path', ''))
        self.destinationFolderEdit = QLineEdit(self.config.get('dicom_destination_folder', ''))
        self.pacsServerIPEdit = QLineEdit(self.config.get('pacs_server_ip', ''))
        self.pacsServerPortEdit = QLineEdit(self.config.get('pacs_server_port', ''))
        self.aetEdit = QLineEdit(self.config.get('application_entity_title', ''))
        self.aecEdit = QLineEdit(self.config.get('called_ae_title', ''))
        self.aemEdit = QLineEdit(self.config.get('calling_ae_title', ''))
        self.networkPortEdit = QLineEdit(self.config.get('network_port', ''))
        self.maxLogFileSizeBytesEdit = QLineEdit(str(self.config.get('max_log_file_size_bytes', '')))

        # 添加字段到表单布局
        formLayout.addRow(QLabel('movescu Executable Path'), self.movescuPathEdit)
        formLayout.addRow(QLabel('Access Numbers File Path'), self.accessNumbersFilePathEdit)
        formLayout.addRow(QLabel('DICOM Destination Folder'), self.destinationFolderEdit)
        formLayout.addRow(QLabel('PACS Server IP'), self.pacsServerIPEdit)
        formLayout.addRow(QLabel('PACS Server Port'), self.pacsServerPortEdit)
        formLayout.addRow(QLabel('Application Entity Title (AET)'), self.aetEdit)
        formLayout.addRow(QLabel('Called AE Title (AEC)'), self.aecEdit)
        formLayout.addRow(QLabel('Calling AE Title (AEM)'), self.aemEdit)
        formLayout.addRow(QLabel('Network Port'), self.networkPortEdit)
        formLayout.addRow(QLabel('Max Log File Size (Bytes)'), self.maxLogFileSizeBytesEdit)

        configGroup.setLayout(formLayout)
        layout.addWidget(configGroup)

        # 控制按钮
        saveButton = QPushButton('Save Configuration', self)
        saveButton.clicked.connect(self.saveConfiguration)
        startButton = QPushButton('Start Download', self)
        startButton.clicked.connect(self.runDownloadScript)

        layout.addWidget(saveButton)
        layout.addWidget(startButton)
        self.setLayout(layout)

    def runDownloadScript(self):
        # 执行同目录下的 DICOM_download_script.py 脚本
        script_path = os.path.join(os.path.dirname(__file__), 'DICOM_download_script.py')
        subprocess.Popen(['python', script_path])

    def saveConfiguration(self):
        # 保存当前配置到JSON文件
        config = {
            'movescu_executable_path': self.movescuPathEdit.text(),
            'access_numbers_file_path': self.accessNumbersFilePathEdit.text(),
            'dicom_destination_folder': self.destinationFolderEdit.text(),
            'pacs_server_ip': self.pacsServerIPEdit.text(),
            'pacs_server_port': self.pacsServerPortEdit.text(),
            'application_entity_title': self.aetEdit.text(),
            'called_ae_title': self.aecEdit.text(),
            'calling_ae_title': self.aemEdit.text(),
            'network_port': self.networkPortEdit.text(),
            'max_log_file_size_bytes': self.maxLogFileSizeBytesEdit.text()
        }
        save_config(self.config_path, config)
        print("Configuration saved.")

if __name__ == '__main__':
    app = QApplication(sys.argv)

    config_path = 'config.json'  # 配置文件路径
    config = load_config(config_path)

    ex = DICOMDownloadApp(config, config_path)
    ex.show()
    sys.exit(app.exec_())
