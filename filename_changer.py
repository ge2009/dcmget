import os
import logging

# 设置日志
logging.basicConfig(filename='D:\\dcmget\\rename.log', level=logging.INFO, 
                    format='%(asctime)s %(levelname)s:%(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def rename_folders(filename_mapping, base_dir):
    # 1. 读取文件名映射
    mapping = {}
    with open(filename_mapping, 'r') as file:
        for line in file:
            if line.strip():  # 确保跳过空行
                original, new_name = line.strip().split()
                mapping[original] = new_name

    # 2. 遍历给定的目录，寻找匹配的文件夹进行重命名
    renamed_entries = []
    errors = []

    logging.info("Starting renaming process...")
    for dirpath, dirnames, filenames in os.walk(base_dir):
        for dirname in dirnames:
            original_path = os.path.join(dirpath, dirname)
            if dirname in mapping:
                new_path = os.path.join(dirpath, mapping[dirname])
                try:
                    os.rename(original_path, new_path)
                    renamed_entries.append((original_path, new_path))
                    logging.info(f"Renamed from '{dirname}' (original name) to '{mapping[dirname]}' (new name).")
                except Exception as e:
                    error_msg = f"Failed to rename '{original_path}' to '{new_path}': {str(e)}"
                    errors.append(error_msg)
                    logging.error(error_msg)
            else:
                logging.warning(f"No mapping found for '{dirname}', skipping...")

    # 3. 输出重命名汇总和错误
    logging.info("Renaming Summary:")
    for original_path, new_path in renamed_entries:
        logging.info(f"Success: Renamed '{original_path}' to '{new_path}'")
    
    if errors:
        logging.info("Errors encountered:")
        for error in errors:
            logging.error(error)

if __name__ == "__main__":
    # 要处理的文件路径和目标文件夹路径
    filename_mapping = 'D:\\dcmget\\filename.txt'
    base_dir = 'D:\\Dicom\\Dest'
    rename_folders(filename_mapping, base_dir)