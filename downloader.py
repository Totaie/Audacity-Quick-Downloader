import yt_dlp
import os

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
        'outtmpl': f'{output_dir}/%(title)s.%(ext)s',
        'noplaylist': True,  # Avoid downloading entire playlists
        'embedthumbnail': True,  # Embed thumbnail into the audio file
        'addmetadata': True,  # Add metadata (e.g., title, artist) to the MP3
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)
        title = info_dict.get('title', None)
        
        # Check if file already exists in the directory
        output_file = f"{output_dir}/{title}.mp3"
        if os.path.exists(output_file):
            print(f"File '{title}.mp3' already exists. Skipping download.")
            return output_file
        
        # If not, download the audio
        ydl.download([url])
        print(f"Downloaded audio file: {output_file}")
        return output_file

if __name__ == "__main__":
    youtube_url = input("Enter YouTube URL: ")
    downloaded_audio = download_youtube_audio(youtube_url)
    print(f"Downloaded audio file: {downloaded_audio}")
