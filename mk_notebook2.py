import json

python_code = """
# @title 🚀 Ultimate Colab JDownloader2 & Telegram Leecher
# @markdown Fill in your Source Link and Telegram Destination. (Make sure you uploaded your `.env` containing `API_ID`, `API_HASH`, and `USER_SESSION_STRING`)
SOURCE_LINK = "https://torbox.app/download?id=4053953&type=torrents&name=Photoshop.2025" # @param {type:"string"}
DESTINATION = "https://t.me/c/3974110944/2/3" # @param {type:"string"}

# @markdown ---
# @markdown ### ⚙️ Advanced JD2 Configuration (Optional)
# @markdown If you want to use true WZML Bot MyJDownloader API logic, add `JD_EMAIL` and `JD_PASS` in your `.env`. Otherwise, the script will use `aria2c` as a blazing fast direct alternative for JD2.
USE_ARIA2_FALLBACK = True # @param {type:"boolean"}
# @markdown ---

import os
import sys
import time
import shutil
import subprocess
import asyncio
import re
from pathlib import Path

# ==========================================
# 0. Install Dependencies
# ==========================================
print("[*] Installing necessary dependencies...")
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyrogram", "TgCrypto", "python-dotenv", "nest_asyncio", "aiohttp"])
subprocess.run(["apt-get", "update", "-qq"])
subprocess.run(["apt-get", "install", "-qq", "-y", "aria2", "default-jre-headless"])

import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
from pyrogram import Client

# ==========================================
# 1. Load Environment & Validate
# ==========================================
if os.path.exists(".env"):
    load_dotenv(".env")
    print("[*] Loaded .env file.")
else:
    print("[!] No .env file found. Make sure you upload it in the Files pane!")

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("USER_SESSION_STRING")
JD_EMAIL = os.environ.get("JD_EMAIL")
JD_PASS = os.environ.get("JD_PASS")

if not (API_ID and API_HASH and SESSION_STRING):
    raise ValueError("Missing Pyrogram API_ID, API_HASH, or USER_SESSION_STRING in .env!")

DL_FOLDER = "/content/downloads"
JD_FOLDER = "/content/JDownloader"
os.makedirs(DL_FOLDER, exist_ok=True)
os.makedirs(JD_FOLDER, exist_ok=True)

# ==========================================
# 2. Leech & Upload Logic
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

async def upload_file(app: Client, file_path: Path, chat_id: int, topic_id: int):
    print(f"\\n[*] Preparing to upload: {file_path.name}")
    try:
        def progress(current, total, filename):
            sys.stdout.write(f"\\r[*] Uploading {filename}: {current * 100 / total:.1f}%")
            sys.stdout.flush()

        await app.send_document(
            chat_id=chat_id,
            document=str(file_path),
            reply_to_message_id=topic_id,
            force_document=True,
            progress=progress,
            progress_args=(file_path.name,)
        )
        print(f"\\n[+] Successfully uploaded: {file_path.name}")
        os.remove(file_path)
        print(f"[*] Deleted from Colab: {file_path.name}")
    except Exception as e:
        print(f"\\n[!] Failed to upload {file_path.name}: {e}")

async def uploader_task_loop(app, output_dir, chat_id, topic_id, download_process_event):
    uploaded_files = set()
    while not download_process_event.is_set():
        files = [f for f in Path(output_dir).rglob('*') if f.is_file()]
        for f in files:
            path_str = str(f)
            # Ignore temp files
            if f.suffix in ['.part', '.aria2', '.temp']:
                continue
            if path_str in uploaded_files:
                continue
            
            # Check stability to ensure it's fully written
            size1 = f.stat().st_size
            await asyncio.sleep(2)
            if not f.exists():
                continue
            size2 = f.stat().st_size
            
            if size1 == size2 and size1 > 0:
                # File is stable
                uploaded_files.add(path_str)
                await upload_file(app, f, chat_id, topic_id)
                
        await asyncio.sleep(5)
        
    # Final sweep
    files = [f for f in Path(output_dir).rglob('*') if f.is_file()]
    for f in files:
        path_str = str(f)
        if f.suffix not in ['.part', '.aria2', '.temp'] and path_str not in uploaded_files:
            uploaded_files.add(path_str)
            await upload_file(app, f, chat_id, topic_id)

# ==========================================
# 3. Download Logic
# ==========================================
async def download_task(url, output_dir, download_process_event):
    print(f"[*] Starting download from: {url}")
    if JD_EMAIL and JD_PASS and not USE_ARIA2_FALLBACK:
        print("[*] Using JDownloader2 logic...")
        if not os.path.exists(f"{JD_FOLDER}/JDownloader.jar"):
            subprocess.run(["wget", "-q", "-O", f"{JD_FOLDER}/JDownloader.jar", "http://installer.jdownloader.org/JDownloader.jar"])
        
        fw_dir = os.path.join(JD_FOLDER, "folderwatch")
        os.makedirs(fw_dir, exist_ok=True)
        crawljob_path = os.path.join(fw_dir, "leech.crawljob")
        with open(crawljob_path, "w") as f:
            f.write(f"text={url}\\n")
            f.write(f"downloadFolder={output_dir}\\n")
            f.write("autoStart=TRUE\\n")
            f.write("autoConfirm=TRUE\\n")
            
        print("[*] Starting headless JD2...")
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
            if stagnant_count > 5:
                break
        try:
            p.kill()
        except Exception:
            pass
    else:
        print("[*] Using aria2c...")
        process = await asyncio.create_subprocess_exec(
            "aria2c", "--console-log-level=warn", "--max-connection-per-server=8", "--split=8", "--dir", output_dir, url
        )
        await process.communicate()
        
    print("[*] Download Finished/Aborted!")
    download_process_event.set()

# ==========================================
# 4. Main Execution
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
    print(f"\\n[+] All processes completed in {time.time() - start_time:.2f} seconds.")
    print("[+] Colab runtime is clear of the downloaded files.")

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

print("Updated JDownloader2_Leecher.ipynb successfully!")
