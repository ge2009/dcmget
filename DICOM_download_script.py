"""
DICOM Get Script
Version: 1.0
Date: 2023-12-08
Description: This script is used to download DICOM files from a PACS server based on access numbers listed in a text file.
"""


import subprocess
import os
import json

def get_log_file(base_path, max_size):
    """获取合适的日志文件，如果当前日志文件超过设定大小则创建新的日志文件"""
    log_index = 1
    while True:
        log_file = os.path.join(base_path, f'download_log_{log_index}.txt')
        if not os.path.exists(log_file) or os.path.getsize(log_file) < max_size:
            return log_file
        log_index += 1

def download_dicom(access_number, destination_path, config):
    specific_dest_path = os.path.join(destination_path, access_number)
    if not os.path.exists(specific_dest_path):
        os.makedirs(specific_dest_path)

    movescu_path = config['movescu_executable_path']
    command = f"{movescu_path} -v -d -aet {config['application_entity_title']} -aec {config['called_ae_title']} -aem {config['calling_ae_title']} --port {config['network_port']} -od {specific_dest_path} {config['pacs_server_ip']} {config['pacs_server_port']} -S -k QueryRetrieveLevel=STUDY -k 0008,0050={access_number}"
    result = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode('utf-8', 'ignore'), result.stderr.decode('utf-8', 'ignore')

def main(txt_file, destination_path, config):
    with open(txt_file, 'r', encoding='utf-8') as file:
        access_numbers = file.readlines()

    for i, access_number in enumerate(access_numbers, 1):
        access_number = access_number.strip()
        print(f"Downloading DICOM for access number: {access_number} ({i}/{len(access_numbers)})")
        stdout, stderr = download_dicom(access_number, destination_path, config)

        # 获取合适的日志文件
        log_file_path = get_log_file(destination_path, config['max_log_file_size_bytes'])
        with open(log_file_path, 'a', encoding='utf-8') as log_file:
            log_file.write(f"Access Number: {access_number}\n")
            log_file.write("Output:\n")
            log_file.write(stdout + "\n")
            if stderr:
                log_file.write("Error:\n")
                log_file.write(stderr + "\n")
            log_file.write("-" * 50 + "\n")

if __name__ == "__main__":
    with open('config.json', 'r', encoding='utf-8') as file:
        config = json.load(file)

    main(config['access_numbers_file_path'], config['dicom_destination_folder'], config)
