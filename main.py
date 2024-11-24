from applemusicdownloader import download_apple_music
from importer import import_mp3_files_batch, import_youtube_mp3_files
from youtubedownloader import download_youtube_audio


def main():
    url = input("Enter URL: ")

    if "apple.com" in url:
        print("Importing from Apple Music...")
        downloaded_file = download_apple_music(url)

        print(f"File downloaded and converted to MP3: {downloaded_file}")
            
        import_mp3_files_batch(downloaded_file)
    elif "youtube.com" in url:
        print("Importing from YouTube...")
        downloaded_file = download_youtube_audio(url)
        print(f"File downloaded and converted to MP3: {downloaded_file}")
        import_youtube_mp3_files(downloaded_file)
    else:
        print("Invalid URL. Please provide a valid URL.")

if __name__ == "__main__":
    main()
