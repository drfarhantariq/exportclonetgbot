import json

python_code = """
# @title 🚀 Ultimate Colab JDownloader2 & Telegram Leecher
# @markdown Fill in your Source Link and Telegram Destination.
SOURCE_LINK = "https://torbox.app/download?id=4053953&type=torrents&name=Photoshop.2025" # @param {type:"string"}
DESTINATION = "https://t.me/c/3974110944/2/3" # @param {type:"string"}
# @markdown Check this box to automatically retry failed uploads up to 3 times before giving up.
RETRY_FAILED = True # @param {type:"boolean"}

# @markdown ---
# @markdown ### 🔑 Authentication Configuration
# @markdown Specify the path to your `.env` file containing `API_ID`, `API_HASH`, `USER_SESSION_STRING`, `JD_EMAIL`, and `JD_PASS`. By default, it expects the `.env` file in the root of your mounted Google Drive.
ENV_FILE_PATH = "/content/drive/MyDrive/.env" # @param {type:"string"}

# @markdown ---
# @markdown ### ⚙️ Advanced JD2 Configuration
# @markdown If JD credentials are in your `.env`, this registers the Colab instance on [MyJDownloader](https://my.jdownloader.org/) to remote manage Premium Accounts and links while the cell runs.
USE_ARIA2_FALLBACK = False # @param {type:"boolean"}
# @markdown *(If turned off and credentials provided, runs authentic JDownloader2 core. Runs aria2c otherwise.)*
# @markdown ---

import os
import sys
import time
import shutil
import subprocess
import asyncio
import re
import random
import json
import math
import mimetypes
from pathlib import Path

# ==========================================
# 0. Mount Google Drive & Install Dependencies
# ==========================================
# If user wants to use a file in /content/drive, we must ensure it is mounted
if ENV_FILE_PATH.startswith("/content/drive"):
    print("[*] Mounting Google Drive to access .env file...")
    from google.colab import drive
    drive.mount('/content/drive')

print("[*] Installing necessary dependencies...")
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyrogram", "TgCrypto", "python-dotenv", "nest_asyncio", "aiohttp"])
subprocess.run(["apt-get", "update", "-qq"])
subprocess.run(["apt-get", "install", "-qq", "-y", "aria2", "default-jre-headless", "ffmpeg"])

import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
from pyrogram import Client

# ==========================================
# 1. Load Environment & Validate
# ==========================================
if os.path.exists(ENV_FILE_PATH):
    load_dotenv(ENV_FILE_PATH)
    print(f"[*] Loaded .env file from {ENV_FILE_PATH}")
else:
    print(f"[!] No .env file found at {ENV_FILE_PATH}. Make sure the path is correct!")

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("USER_SESSION_STRING")
JD_EMAIL = os.environ.get("JD_EMAIL")
JD_PASS = os.environ.get("JD_PASS")

if not (API_ID and API_HASH and SESSION_STRING):
    raise ValueError(f"Missing Pyrogram API_ID, API_HASH, or USER_SESSION_STRING in {ENV_FILE_PATH}!")

DL_FOLDER = "/content/downloads"
JD_FOLDER = "/content/JDownloader"
MAX_TG_SIZE = 4000000000 # Approx 3.9GB, safely under 4.0GB max limit
os.makedirs(DL_FOLDER, exist_ok=True)
os.makedirs(JD_FOLDER, exist_ok=True)

upload_stats = {
    "success": 0,
    "failed": 0,
    "current": ""
}

# ==========================================
# 2. Split Logic (WZML-X Inspired)
# ==========================================
def get_video_duration(video_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    try:
        output = subprocess.check_output(cmd).decode().strip()
        return float(output)
    except:
        return 0

def split_file_logic(file_path):
    size = os.path.getsize(file_path)
    if size <= MAX_TG_SIZE:
        return [file_path]
    
    parts = math.ceil(size / MAX_TG_SIZE)
    mime = mimetypes.guess_type(file_path)[0]
    out_files = []
    
    # Try FFMPEG split for videos
    if mime and mime.startswith('video/'):
        print(f"\n[*] Splitting large video (>&4GB) into {parts} parts... {file_path}")
        duration = get_video_duration(file_path)
        if duration > 0:
            part_duration = duration / parts
            base, ext = os.path.splitext(file_path)
            cmd = [
                "ffmpeg", "-y", "-i", file_path, "-c", "copy", "-map", "0", 
                "-f", "segment", "-segment_time", str(part_duration), 
                "-reset_timestamps", "1", f"{base}.part%03d{ext}"
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for i in range(parts):
                p = f"{base}.part{i:03d}{ext}"
                if os.path.exists(p):
                    out_files.append(p)
            if out_files:
                os.remove(file_path) # Delete original to save space
                return out_files

    # Fallback/Document generic split
    print(f"\n[*] Binary Splitting large file (>&4GB) into {parts} parts... {file_path}")
    base = file_path + ".part"
    cmd = ["split", "--numeric-suffixes=1", "--suffix-length=3", f"--bytes={MAX_TG_SIZE}", file_path, base]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out_files = sorted([str(p) for p in Path(os.path.dirname(file_path)).glob(os.path.basename(file_path) + ".part*")])
    if out_files:
        os.remove(file_path) # Delete original
    return out_files

# ==========================================
# 3. Leech & Upload Logic
# ==========================================
def parse_destination(link: str):
    match = re.search(r't\.me/c/([^/]+)/([^/]+)', link)
    if match:
        chat_id = int(f"-100{match.group(1)}")
        reply_to_msg = int(match.group(2))
        return chat_id, reply_to_msg
    match2 = re.search(r't\.me/([^/]+)/([^/]+)', link)
    if match2:
        return match2.group(1), int(match2.group(2))
    raise ValueError(f"Could not parse telegram link: {link}")

async def upload_file(app: Client, file_path: str, chat_id: int, topic_id: int):
    f_path = Path(file_path)
    upload_stats["current"] = f_path.name
    MAX_RETRIES = 3 if RETRY_FAILED else 1
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            def progress(current, total, filename):
                sys.stdout.write(f"\r[STATUS -> ✅ : {upload_stats['success']} | ❌ : {upload_stats['failed']}] Uploading '{filename}' (Attempt {attempt}/{MAX_RETRIES}): {current * 100 / total:.1f}%")
                sys.stdout.flush()

            await app.send_document(
                chat_id=chat_id,
                document=file_path,
                reply_to_message_id=topic_id,
                force_document=True,
                progress=progress,
                progress_args=(f_path.name,)
            )
            upload_stats["success"] += 1
            sys.stdout.write(f"\r[STATUS -> ✅ : {upload_stats['success']} | ❌ : {upload_stats['failed']}] Uploaded '{f_path.name}' successfully!                           \n")
            sys.stdout.flush()
            if os.path.exists(file_path):
                os.remove(file_path)
            return True
        except Exception as e:
            if attempt < MAX_RETRIES:
                sys.stdout.write(f"\n[!] Failed to upload {f_path.name}. Retrying ({attempt}/{MAX_RETRIES}). Error: {e}\n")
                await asyncio.sleep(3)
            else:
                upload_stats["failed"] += 1
                sys.stdout.write(f"\n[!] Failed to upload {f_path.name} after {MAX_RETRIES} attempts. Error: {e}\n")
    return False

async def uploader_task_loop(app, output_dir, chat_id, topic_id, download_process_event):
    uploaded_files = set()
    while not download_process_event.is_set():
        files = [f for f in Path(output_dir).rglob('*') if f.is_file()]
        for f in files:
            path_str = str(f)
            # Ignore temp files
            if f.suffix in ['.aria2', '.temp'] or path_str.endswith('.part'):
                continue
            if path_str in uploaded_files:
                continue
            
            size1 = f.stat().st_size
            await asyncio.sleep(2)
            if not f.exists():
                continue
            size2 = f.stat().st_size
            
            if size1 == size2 and size1 > 0:
                uploaded_files.add(path_str)
                # Split logic
                split_parts = await asyncio.to_thread(split_file_logic, path_str)
                for part in split_parts:
                    success = await upload_file(app, part, chat_id, topic_id)
                    uploaded_files.add(part)
                
        await asyncio.sleep(5)
        
    # Final sweep
    files = [f for f in Path(output_dir).rglob('*') if f.is_file()]
    for f in files:
        path_str = str(f)
        if f.suffix not in ['.aria2', '.temp'] and not path_str.endswith('.part') and path_str not in uploaded_files:
            uploaded_files.add(path_str)
            split_parts = await asyncio.to_thread(split_file_logic, path_str)
            for part in split_parts:
                if part not in uploaded_files:
                    success = await upload_file(app, part, chat_id, topic_id)
                    uploaded_files.add(part)

# ==========================================
# 4. Download Logic
# ==========================================
async def download_task(url, output_dir, download_process_event):
    print(f"[*] Starting download task...")
    if JD_EMAIL and JD_PASS and not USE_ARIA2_FALLBACK:
        if not os.path.exists(f"{JD_FOLDER}/JDownloader.jar"):
            subprocess.run(["wget", "-q", "-O", f"{JD_FOLDER}/JDownloader.jar", "http://installer.jdownloader.org/JDownloader.jar"])
        
        cfg_dir = os.path.join(JD_FOLDER, "cfg")
        os.makedirs(cfg_dir, exist_ok=True)
        device_name = f"Colab_Bot_{random.randint(100, 999)}"
        
        jdata = {"autoconnectenabledv2": True, "password": JD_PASS, "devicename": device_name, "email": JD_EMAIL}
        with open(os.path.join(cfg_dir, "org.jdownloader.api.myjdownloader.MyJDownloaderSettings.json"), "w") as f:
            json.dump(jdata, f)
            
        remote_data = {"externinterfaceenabled": True, "deprecatedapilocalhostonly": True, "deprecatedapienabled": True, "jdanywhereapienabled": True, "externinterfacelocalhostonly": False}
        with open(os.path.join(cfg_dir, "org.jdownloader.api.RemoteAPIConfig.json"), "w") as f:
            json.dump(remote_data, f)
            
        print(f"[+] MyJDownloader Connected! Log in to https://my.jdownloader.org to see device: '{device_name}'")

        if url and url.strip():
            fw_dir = os.path.join(JD_FOLDER, "folderwatch")
            os.makedirs(fw_dir, exist_ok=True)
            crawljob_path = os.path.join(fw_dir, "leech.crawljob")
            with open(crawljob_path, "w") as f:
                f.write(f"text={url}\n")
                f.write(f"downloadFolder={output_dir}\n")
                f.write("autoStart=TRUE\n")
                f.write("autoConfirm=TRUE\n")
            
        p = await asyncio.create_subprocess_exec("java", "-Djava.awt.headless=true", "-jar", "JDownloader.jar", cwd=JD_FOLDER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        prev_size = -1
        stagnant_count = 0
        while True:
            await asyncio.sleep(10)
            size = sum(f.stat().st_size for f in Path(output_dir).rglob('*') if f.is_file() and f.suffix not in ['.part', '.aria2'])
            if size == prev_size and size > 0:
                stagnant_count += 1
            else:
                stagnant_count = 0
            prev_size = size
            threshold = 10 if size > 0 else 60
            if stagnant_count > threshold:
                break
                
        try:
            p.kill()
        except Exception:
            pass
            
    else:
        if url and url.strip():
            process = await asyncio.create_subprocess_exec("aria2c", "--console-log-level=warn", "--max-connection-per-server=8", "--split=8", "--dir", output_dir, url)
            await process.communicate()
        
    print("\n[*] Download Stream Concluded!")
    download_process_event.set()

# ==========================================
# 5. Main Execution
# ==========================================
async def main():
    if os.path.exists(DL_FOLDER):
        shutil.rmtree(DL_FOLDER)
    os.makedirs(DL_FOLDER, exist_ok=True)
    
    chat_id, topic_id = parse_destination(DESTINATION)
    print(f"[*] Parsed Destination: Chat: {chat_id}, Topic/ReplyTo: {topic_id}")
    
    print("[*] Authenticating with Telegram...")
    app = Client("colab_session", api_id=int(API_ID), api_hash=API_HASH, session_string=SESSION_STRING)
    await app.start()
    
    start_time = time.time()
    download_event = asyncio.Event()
    
    dl_task = asyncio.create_task(download_task(SOURCE_LINK, DL_FOLDER, download_event))
    up_task = asyncio.create_task(uploader_task_loop(app, DL_FOLDER, chat_id, topic_id, download_event))
    
    await asyncio.gather(dl_task, up_task)
    
    await app.stop()
    print(f"\n[+] All processes completed in {time.time() - start_time:.2f} seconds.")
    print(f"[+] Final Statistics -> ✅ Successful: {upload_stats['success']} | ❌ Failed: {upload_stats['failed']}")
    print("[+] Colab runtime is clear of the uploaded files.")

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
"""

notebook = {
  "cells": [
    {
      "cell_type": "code",
      "execution_count": None,
      "metadata": {
        "id": "JDLeecher_Cell1"
      },
      "outputs": [],
      "source": [line + "\n" for line in python_code.split("\n")]
    }
  ],
  "metadata": {
    "colab": {
        "name": "JDownloader2_Leecher.ipynb",
        "provenance": []
    },
    "kernelspec": {
      "display_name": "Python 3",
      "name": "python3"
    },
    "language_info": {
      "name": "python"
    }
  },
  "nbformat": 4,
  "nbformat_minor": 0
}

with open("JDownloader2_Leecher.ipynb", "w", encoding="utf-8") as f:
    json.dump(notebook, f, indent=2)

print("Updated JDownloader2_Leecher.ipynb for GDrive Env support successfully!")
