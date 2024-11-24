import os
import shutil
import re
import sys
import time

# Set up pipe paths depending on platform
if sys.platform == 'win32':
    print("pipe-test.py, running on windows")
    TONAME = '\\\\.\\pipe\\ToSrvPipe'
    FROMNAME = '\\\\.\\pipe\\FromSrvPipe'
    EOL = '\r\n\0'
else:
    print("pipe-test.py, running on linux or mac")
    TONAME = '/tmp/audacity_script_pipe.to.' + str(os.getuid())
    FROMNAME = '/tmp/audacity_script_pipe.from.' + str(os.getuid())
    EOL = '\n'

print("Write to  \"" + TONAME +"\"")
if not os.path.exists(TONAME):
    print(" ..does not exist. Ensure Audacity is running with mod-script-pipe.")
    sys.exit()

print("Read from \"" + FROMNAME +"\"")
if not os.path.exists(FROMNAME):
    print(" ..does not exist. Ensure Audacity is running with mod-script-pipe.")
    sys.exit()

print("-- Both pipes exist. Good.")

# Open the pipes with UTF-8 encoding
TOFILE = open(TONAME, 'w', encoding='utf-8')
print("-- File to write to has been opened")
FROMFILE = open(FROMNAME, 'rt', encoding='utf-8')
print("-- File to read from has now been opened too\r\n")

def send_command(command):
    """Send a single command."""
    print("Send: >>> \n" + command)
    TOFILE.write(command + EOL)
    TOFILE.flush()

def get_response():
    """Return the command response."""
    result = ''
    line = ''
    while True:
        result += line
        line = FROMFILE.readline()
        if line == '\n' and len(result) > 0:
            break
    return result

def do_command(command):
    """Send one command, and return the response."""
    send_command(command)
    response = get_response()
    print("Rcvd: <<< \n" + response)
    return response

def get_all_mp3_files_in_directory(directory):
    """Retrieve all .mp3 files in the given directory and its subdirectories."""
    mp3_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".mp3"):
                mp3_files.append(os.path.join(root, file))
    return mp3_files

def move_mp3_to_downloads(mp3_file):
    """Move the MP3 file to the Downloads folder, remove leading numbers from its name, and return new path."""
    downloads_folder = os.path.join(os.getcwd(), 'Downloads')
    
    # Ensure the Downloads folder exists
    if not os.path.exists(downloads_folder):
        os.makedirs(downloads_folder)
    
    # Remove leading numbers from the filename
    file_name = os.path.basename(mp3_file)
    new_file_name = re.sub(r'^\d+\s*', '', file_name)
    
    # Define the new location in the Downloads folder
    new_location = os.path.join(downloads_folder, new_file_name)
    
    # Move the file to the Downloads folder
    shutil.move(mp3_file, new_location)
    print(f"Moved and renamed {mp3_file} to {new_location}")
    return new_location

def delete_folder(folder_path):
    """Delete the folder and all its contents."""
    try:
        shutil.rmtree(folder_path)
        print(f"Deleted folder: {folder_path}")
    except Exception as e:
        print(f"Error deleting folder: {e}")

def import_selected_mp3_files(mp3_file):
    """Import the selected MP3 file into Audacity."""
    if ' ' in str(mp3_file):
        mp3_file = f'"{mp3_file}"'
    print(f"Importing file: {mp3_file}")
    do_command(f"Import2: Filename={mp3_file}")

def import_mp3_files_batch(download_dir):
    """Process the downloaded MP3 files, import them into Audacity, and move them to Downloads."""
    
    # Add a small delay to ensure files are ready
    time.sleep(0.1)
    
    # Get both parent directory paths
    parent_dir = os.path.dirname(download_dir)
    grandparent_dir = os.path.dirname(parent_dir)
    
    # Step 1: Find all MP3 files in the directory
    mp3_files = get_all_mp3_files_in_directory(download_dir)
    print(f"Found MP3 files: {mp3_files}")
    
    if not mp3_files:
        print("No MP3 files found. Checking parent directory...")
        mp3_files = get_all_mp3_files_in_directory(parent_dir)
        print(f"Found MP3 files in parent directory: {mp3_files}")
    
    # Step 2: Import and move each MP3 file
    for mp3_file in mp3_files:
        print(f"Processing file: {mp3_file}")
        import_selected_mp3_files(mp3_file)  # Import to Audacity
        move_mp3_to_downloads(mp3_file)  # Move to Downloads
    
    # Step 3: Clean up all directories
    if os.path.isdir(download_dir):
        delete_folder(download_dir)
    if os.path.isdir(parent_dir):
        delete_folder(parent_dir)
    if os.path.isdir(grandparent_dir) and grandparent_dir != os.getcwd():
        delete_folder(grandparent_dir)

# Main script logic
def process_album_or_playlist(download_dir):
    """Process an album or playlist by importing MP3s and cleaning up the folder."""
    import_mp3_files_batch(download_dir)

def import_youtube_mp3_files(downloaded_files):
    """Import the MP3 files into Audacity from a list of absolute file paths."""
    for file_path in downloaded_files:
        # Ensure the file path is properly formatted for Audacity (with quotes if spaces)
        if ' ' in file_path:
            file_path = f'"{file_path}"'

        print(f"Importing file: {file_path}")
        do_command(f"Import2: Filename={file_path}")