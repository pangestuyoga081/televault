import os
import subprocess
from server import app as fastapi_app
import gradio as gr

# Jalankan Realtime Watcher & Auto-Resolver Telegram di Background
try:
    subprocess.Popen(["python", "sync_manager.py"])
except Exception as e:
    print(f"Sync Manager background note: {e}")

# Definisikan UI TeleVault
with gr.Blocks(title="TeleVault Cloud") as demo:
    gr.HTML("""
    <iframe src="/index.html" style="position:fixed; top:0; left:0; bottom:0; right:0; width:100%; height:100%; border:none; margin:0; padding:0; overflow:hidden; z-index:999999;">
    </iframe>
    """)

# Mount ke FastAPI
app = gr.mount_gradio_app(fastapi_app, demo, path="/")

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, show_error=True)
