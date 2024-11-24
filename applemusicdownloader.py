import subprocess
import os
from pathlib import Path

def convert_to_mp3(input_file, output_file):
    """Converts the downloaded M4A file to MP3 using ffmpeg."""
    try:
        subprocess.run(
            ['ffmpeg', '-i', str(input_file), '-codec:a', 'libmp3lame', '-qscale:a', '2', str(output_file)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        print(f"Converted to MP3: {output_file}")
        return output_file
    except subprocess.CalledProcessError as e:
        print("Error converting to MP3:", e.stderr.decode())

def find_m4a_files(output_dir):
    """Finds all M4A files in the output directory and its subdirectories."""
    m4a_files = []
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            if file.endswith('.m4a'):
                m4a_files.append(Path(root) / file)
    return m4a_files

def clean_filename(filepath):
    """Removes leading numbers and spaces from filename."""
    path = Path(filepath)
    filename = path.name
    # Remove leading numbers and spaces, keeping the rest of the filename
    clean_name = ' '.join(part for part in filename.split(' ') if not part.isdigit())
    return path.parent / clean_name

def download_apple_music(url):
    output_dir = os.path.join(os.getcwd(), 'Downloads')

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    try:
        print("Downloading from Apple Music...")
        subprocess.run(
            ['gamdl', url, '-o', output_dir],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        downloaded_m4a_files = find_m4a_files(output_dir)
        
        for m4a_file in downloaded_m4a_files:
            mp3_output_file = m4a_file.with_suffix('.mp3')
            # Convert to MP3
            output_file = convert_to_mp3(m4a_file, mp3_output_file)
            
            # Clean the filename and rename
            clean_output_file = clean_filename(output_file)
            os.rename(output_file, clean_output_file)
            
            # Remove the original M4A file
            os.remove(m4a_file)
            print(f"Successfully downloaded and converted: {clean_output_file.name}")

        return clean_output_file
    except subprocess.CalledProcessError as e:
        print("Error downloading from Apple Music:", e.stderr.decode())
        return None

if __name__ == "__main__":
    apple_music_url = input("Enter Apple Music URL: ")
    downloaded_folder = download_apple_music(apple_music_url)
    
    if downloaded_folder:
        print(f"Files downloaded and converted to MP3 in {downloaded_folder}")
