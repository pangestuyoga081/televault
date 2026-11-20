import os
import subprocess
from server import app as fastapi_app
import gradio as gr

# 1. Jalankan Auto-Crawler Telegram di Background
try:
    subprocess.Popen(["python", "full_crawler.py"])
except Exception as e:
    print(f"Crawler background note: {e}")

# 2. Definisikan Gradio Blocks
with gr.Blocks(title="TeleVault Cloud") as demo:
    gr.HTML("""
    <iframe src="/index.html" style="position:fixed; top:0; left:0; bottom:0; right:0; width:100%; height:100%; border:none; margin:0; padding:0; overflow:hidden; z-index:999999;">
    </iframe>
    """)

# 3. Mount Gradio ke FastAPI utama
app = gr.mount_gradio_app(fastapi_app, demo, path="/")
