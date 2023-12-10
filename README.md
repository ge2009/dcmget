# DICOM Download Script

![](https://image-1256178063.cos.ap-shanghai.myqcloud.com/upload2022/202312081440110.png)





## 描述

这个 Python 脚本是基于dcmtk开发的，用于从 PACS 服务器自动下载 DICOM 图像。它读取一个包含多个访问号码的文本文件，然后使用 `movescu` 工具从 PACS 服务器下载相应的 DICOM 图像。此外，脚本提供日志记录功能，记录下载进度和相关错误信息。

开源地址: https://github.com/ge2009/dcmget

## 功能

- 读取包含访问号码的文本文件。
- 自动从 PACS 服务器下载对应的 DICOM 图像。
- 为每个访问号码创建一个单独的目录。
- 日志文件记录下载过程和错误。
- 日志文件自动分割功能。

## 配置文件

脚本使用 `config.json` 配置文件，包含以下配置项：

- `movescu_executable_path`: `movescu.exe` 的绝对路径。
- `access_numbers_file_path`: 存储访问号码的文本文件路径。
- `dicom_destination_folder`: DICOM 文件存储的目标文件夹路径。
- `pacs_server_ip`: PACS 服务器地址。
- `pacs_server_port`: PACS 服务器端口。
- `application_entity_title`: AET 配置。
- `called_ae_title`: AEC 配置。
- `calling_ae_title`: AEM 配置。
- `network_port`: 网络端口。
- `max_log_file_size_bytes`: 日志文件的最大大小（字节）。

## 部署方式（命令行）

1. 确保 Python 环境已安装在您的系统上。

2. 将 `dicomget_script.py` 和 `config.json` 文件放置在同一目录下。

3. 根据您的环境和需求，编辑 `config.json` 文件。

4. 修改要批量下载的access文件，里面传入访问号格式如图![](https://image-1256178063.cos.ap-shanghai.myqcloud.com/upload2022/202312081130385.png)

5. 运行脚本：

   ```python
   python DICOM_download_script.py
   ```

![image-20231208143936988](https://image-1256178063.cos.ap-shanghai.myqcloud.com/upload2022/202312081439012.png)

## UI界面编译

```sh
pip install pyqt5
pip install pyinstaller
pyinstaller --onefile --windowed DICOM_download_ui.py
```

- 编译完成后运行里面的exe程序即可



![界面截图](https://image-1256178063.cos.ap-shanghai.myqcloud.com/upload2022/202312100954786.png)



## 注意事项

- 确保 `movescu.exe` 可执行文件的路径在 `config.json` 中正确设置，如果已经安装过dcmtk的可以忽略配置。
- 文本文件中的访问号码应该每行一个，无其他多余字符。
- 检查目标文件夹路径是否存在，脚本将在这里创建子目录并存储下载的 DICOM 文件。
- 根据需要调整日志文件的最大大小限制。
