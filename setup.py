from setuptools import setup

APP = ['DICOM_download_ui.py']
DATA_FILES = []
OPTIONS = {
    'argv_emulation': True,
    'packages': ['PyQt5'],
    'iconfile': 'logo.icns',  # 替换为你的图标文件路径
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
