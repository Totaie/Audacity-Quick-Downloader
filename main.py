from downloader import download_youtube_audio
from importer import import_selected_mp3_files

# Main logic
if __name__ == "__main__":
    youtube_url = input("Enter YouTube Playlist URL: ")
    
    # Download all audio files from the playlist and get the list of downloaded files
    downloaded_files = download_youtube_audio(youtube_url)
    
    # Import only the MP3 files that were downloaded
    import_selected_mp3_files(downloaded_files)
