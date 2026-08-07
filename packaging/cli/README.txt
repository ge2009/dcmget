DcmGetCLI 独立 DICOM 下载器
============================

本目录是独立产品，不依赖 DcmGet 图形工作台，也不包含 PDI、OHIF、WebView、
Profile 管理或自动更新。

首次使用
--------

1. 用记事本编辑 config.json，填写 PACS 地址、端口、AE Title、接收 AE、
   接收端口和保存目录。
2. 在 PACS 中把 storage_ae_title 映射到本机 IP 和 storage_port。
3. 确保 Windows 防火墙允许 dcmtk\bin\storescp.exe 接收入站 TCP 连接。
4. 编辑 access.txt，每行填写一个检查号。
5. 在本目录打开命令提示符并运行：

   DcmGetCLI.exe access.txt

也可指定其他配置：

   DcmGetCLI.exe access.txt --config D:\DcmGetCLI\config.json

运行与恢复
----------

- 默认控制台只显示进度、警告和错误；增加 --verbose 可查看详细状态。
- 每完成一个检查号都会立即保存恢复点。异常退出或 Ctrl+C 后，再次运行相同
  命令会继续未完成项，不会重新执行已经完成的检查号。
- 如果确实要放弃旧任务并按当前 access.txt 重新开始，请增加 --reset。
- 下载日志保存在目标目录的 _DcmGetLogs 中；若该目录不可写，则回退到
  %LOCALAPPDATA%\DcmGetCLI\logs。
- 普通下载文件始终以 .dcm 结尾。

退出码
------

0    全部完成或无数据
1    配置、DCMTK、端口或接收器启动失败
2    存在失败、部分成功或安全暂停，可再次运行继续
130  用户取消，恢复点已保留

独立 CLI 不读取图形版注册码和试用次数。
