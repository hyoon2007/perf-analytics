from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import subprocess
import time
import os

WATCH_DIR = "/ftp/incoming"

class NewFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return

        filepath = event.src_path

        if not filepath.endswith((".csv", ".json", ".xlsx")):
            return

        time.sleep(10)  # 파일 전송 완료 대기

        subprocess.run([
            "python",
            "/opt/anomaly_report/generate_report.py",
            "--input-file",
            filepath
        ], check=True)

observer = Observer()
observer.schedule(NewFileHandler(), WATCH_DIR, recursive=False)
observer.start()

try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    observer.stop()

observer.join()