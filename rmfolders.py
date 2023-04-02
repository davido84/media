from pathlib import Path
import os
import sys

def remove_empty_folders(root_folder: str):
    folders_to_remove = []
    for file in os.listdir(root_folder):
        d = os.path.join(root_folder, file)
        if os.path.isdir(d) and not os.listdir(d):
            folders_to_remove.append(d)

    for folder in folders_to_remove:
        if Path(folder).exists():
            os.rmdir(folder)
            print(f'Removed folder: {folder}')

if __name__ == "__main__":
    remove_empty_folders(sys.argv[1])