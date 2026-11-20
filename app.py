import os
import subprocess
import gradio as gr
from fastapi.responses import HTMLResponse, FileResponse
from server import app as fastapi_app

# Start crawler di background jika belum berjalan
try:
    subprocess.Popen(["python", "full_crawler.py"])
except Exception as e:
    print(f"Crawler background note: {e}")

import os
import subprocess
import gradio as gr
from server import app as fastapi_app

# Start crawler di background jika belum berjalan
try:
    subprocess.Popen(["python", "full_crawler.py"])
except Exception as e:
    print(f"Crawler background note: {e}")

# Mount UI TeleVault ke Gradio Blocks
with gr.Blocks(title="TeleVault Cloud") as demo:
    gr.HTML("""
    <iframe src="/index.html" style="position:fixed; top:0; left:0; bottom:0; right:0; width:100%; height:100%; border:none; margin:0; padding:0; overflow:hidden; z-index:999999;">
    </iframe>
    """)

demo.launch(ssr=False)
