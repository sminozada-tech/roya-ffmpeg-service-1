import os
import json
import tempfile
import subprocess
import urllib.request
import base64
from datetime import datetime
from flask import Flask, request, jsonify
from google.cloud import storage

app = Flask(__name__)

# ---------------------------
# Helper: download file from URL
# ---------------------------
def download_file(url, dest_path):
    urllib.request.urlretrieve(url, dest_path)

# ---------------------------
# Helper: decode base64 audio to file
# ---------------------------
def decode_audio_base64(audio_b64, dest_path):
    audio_bytes = base64.b64decode(audio_b64)
    with open(dest_path, 'wb') as f:
        f.write(audio_bytes)

# ---------------------------
# Helper: create a text file with captions (for subtitles)
# ---------------------------
def create_subtitle_file(captions, scene_duration_seconds=8):
    # Simple subtitle format: each scene's text for its duration
    # Assumes 4 scenes, each exactly 8 seconds
    sub_content = ""
    start = 0
    for i, text in enumerate(captions[:4]):
        end = start + scene_duration_seconds
        sub_content += f"{i+1}\n{format_time(start)} --> {format_time(end)}\n{text}\n\n"
        start = end
    sub_path = tempfile.mktemp(suffix='.srt')
    with open(sub_path, 'w', encoding='utf-8') as f:
        f.write(sub_content)
    return sub_path

def format_time(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    millis = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

# ---------------------------
# Main stitching endpoint
# ---------------------------
@app.route('/stitch', methods=['POST'])
def stitch():
    try:
        data = request.get_json()
        video_urls = data.get('video_urls', [])
        audios = data.get('audios', [])          # list of base64 strings
        captions = data.get('captions', [])      # list of strings for each scene
        output_name = data.get('output_name', 'stitched-video')
        bucket_name = data.get('bucket_name', 'roya-rewires-videos')

        # Temporary directory for all files
        with tempfile.TemporaryDirectory() as tmpdir:
            # Download all video clips
            video_paths = []
            for i, url in enumerate(video_urls):
                path = os.path.join(tmpdir, f'video_{i}.mp4')
                download_file(url, path)
                video_paths.append(path)

            # Decode audio files
            audio_paths = []
            for i, audio_b64 in enumerate(audios):
                path = os.path.join(tmpdir, f'audio_{i}.mp3')
                decode_audio_base64(audio_b64, path)
                audio_paths.append(path)

            # Create subtitle file
            sub_path = create_subtitle_file(captions)

            # Prepare FFmpeg command:
            # 1. Concatenate videos
            concat_list = os.path.join(tmpdir, 'concat.txt')
            with open(concat_list, 'w') as f:
                for vpath in video_paths:
                    f.write(f"file '{vpath}'\n")

            # 2. Mix each audio with its corresponding video part (simple overlay)
            #    We'll create a single audio track by concatenating audio files first.
            concat_audio = os.path.join(tmpdir, 'concat_audio.txt')
            with open(concat_audio, 'w') as f:
                for apath in audio_paths:
                    f.write(f"file '{apath}'\n")

            mixed_audio = os.path.join(tmpdir, 'mixed_audio.mp3')
            # Concatenate audio files
            subprocess.run([
                'ffmpeg', '-f', 'concat', '-safe', '0', '-i', concat_audio,
                '-c', 'copy', mixed_audio
            ], check=True, capture_output=True)

            # Output final video path
            final_video_path = os.path.join(tmpdir, 'final.mp4')

            # 3. Combine video + audio + subtitles
            #    - map video from concatenated video
            #    - map audio from mixed audio
            #    - burn subtitles using subtitles filter
            concat_video = os.path.join(tmpdir, 'concat_video.ts')
            # First concatenate video without audio
            subprocess.run([
                'ffmpeg', '-f', 'concat', '-safe', '0', '-i', concat_list,
                '-c', 'copy', concat_video
            ], check=True, capture_output=True)

            # Now combine video with audio and burn subtitles
            subprocess.run([
                'ffmpeg', '-i', concat_video, '-i', mixed_audio,
                '-filter_complex', f"subtitles={sub_path}",
                '-c:v', 'libx264', '-c:a', 'aac', '-shortest',
                final_video_path
            ], check=True, capture_output=True)

            # ---------------------------
            # Upload to Google Cloud Storage
            # ---------------------------
            # Initialize GCS client
            gcs_json = os.environ.get('GCS_SERVICE_ACCOUNT_JSON')
            if gcs_json:
                service_account_info = json.loads(gcs_json)
                storage_client = storage.Client.from_service_account_info(service_account_info)
            else:
                # Fallback: use default credentials (if running on GCP)
                storage_client = storage.Client()

            bucket = storage_client.bucket(bucket_name)
            object_name = f"{output_name}-{int(datetime.now().timestamp())}.mp4"
            blob = bucket.blob(object_name)
            blob.upload_from_filename(final_video_path)

            # Make public (optional but convenient)
            blob.make_public()
            public_url = blob.public_url

            # Return the URL
            return jsonify({'videoUrl': public_url})

    except Exception as e:
        print("Error:", str(e))
        return jsonify({'error': str(e)}), 500

# ---------------------------
# Health check endpoint (optional)
# ---------------------------
@app.route('/', methods=['GET'])
def health():
    return "FFmpeg service is running", 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
