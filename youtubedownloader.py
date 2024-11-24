import yt_dlp
import os
import shutil

def download_youtube_audio(url, output_dir="Downloads"):
    # Create the output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'noplaylist': False,  # Ensure playlist is downloaded
        'embedthumbnail': True,  # Embed thumbnail into the audio file
        'addmetadata': True,  # Add metadata (e.g., title, artist) to the MP3
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Extract information without downloading
        info_dict = ydl.extract_info(url, download=False)
        
        # Check if it's a playlist or a single video
        is_playlist = 'entries' in info_dict
        playlist_name = info_dict.get('title', 'playlist')
        downloaded_files = []

        if is_playlist:
            # Handle playlist download
            playlist_dir = os.path.join(output_dir, playlist_name)
            if not os.path.exists(playlist_dir):
                os.makedirs(playlist_dir)

            # Update the output template for playlist downloads
            ydl_opts['outtmpl'] = os.path.join(playlist_dir, '%(title)s.%(ext)s')
            with yt_dlp.YoutubeDL(ydl_opts) as ydl_playlist:
                ydl_playlist.download([url])
            
            # Move downloaded MP3 files out of the playlist folder
            for filename in os.listdir(playlist_dir):
                if filename.endswith('.mp3'):
                    file_path = os.path.join(playlist_dir, filename)
                    new_path = os.path.join(output_dir, filename)
                    shutil.move(file_path, new_path)  # Move the file
                    downloaded_files.append(os.path.abspath(new_path))
            
            # Remove the playlist folder after moving the files
            shutil.rmtree(playlist_dir)

        else:
            # Handle single video download
            single_file_dir = os.path.join(output_dir, "single_mp3")
            if not os.path.exists(single_file_dir):
                os.makedirs(single_file_dir)

            # Update the output template for single video downloads
            ydl_opts['outtmpl'] = os.path.join(single_file_dir, '%(title)s.%(ext)s')
            with yt_dlp.YoutubeDL(ydl_opts) as ydl_single:
                ydl_single.download([url])
            
            # Move the single MP3 file from the temporary folder to Downloads
            for filename in os.listdir(single_file_dir):
                if filename.endswith('.mp3'):
                    file_path = os.path.join(single_file_dir, filename)
                    new_path = os.path.join(output_dir, filename)
                    shutil.move(file_path, new_path)  # Move the file
                    downloaded_files.append(os.path.abspath(new_path))
            
            # Remove the temporary folder for the single MP3
            shutil.rmtree(single_file_dir)

        return downloaded_files
