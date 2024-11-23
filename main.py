from downloader import download_youtube_audio
from importer import do_command

url = input("Enter YouTube URL: ")
download_youtube_audio(url)

do_command('ImportAudio: Command=Audio')


