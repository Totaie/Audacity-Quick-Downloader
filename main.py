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
        runAgain()

    elif "youtube.com" in url:
        if "&list" in url:
            download_full_playlist = input("Download full playlist? (y/n): ")
            if download_full_playlist.lower() == "y":
                print("Importing from YouTube...")
                downloaded_files = download_youtube_audio(url, noplaylist=False)
                print(f"Files downloaded and converted to MP3: {downloaded_files}")
                import_youtube_mp3_files(downloaded_files)
                runAgain()
            else:
                print("Importing from YouTube...")
                downloaded_file = download_youtube_audio(url, noplaylist=True)
                print(f"File downloaded and converted to MP3: {downloaded_file}")
                import_youtube_mp3_files(downloaded_file)
                runAgain()
        else:
            print("Importing from YouTube...")
            downloaded_file = download_youtube_audio(url, noplaylist=True)
            print(f"File downloaded and converted to MP3: {downloaded_file}")
            import_youtube_mp3_files(downloaded_file)
            runAgain()
    else:
        print("Invalid URL. Please provide a valid URL.")
        runAgain()


def runAgain():
    if input("Would you like to download another song? (y/n): ").lower() == "y":
        main()


if __name__ == "__main__":
    main()
