"""
rmbgclip - remove image backgrounds straight from the Windows clipboard.

Watches the clipboard. When an image is copied, it is run through RMBG-1.4
(fp16 ONNX), the cut-out (transparent PNG) is put back on the clipboard, and a
Windows toast notification is shown.

Install:
    pip install onnxruntime pillow numpy pywin32 winotify
    (optional GPU: pip install onnxruntime-directml  -- instead of onnxruntime)

Usage:
    python rmbgclip.py            # keep running, process every copied image
    python rmbgclip.py --once     # process the image currently on the clipboard, then exit
    pythonw rmbgclip.py           # run silently in the background (no console window)
"""

import argparse
import ctypes
import io
import os
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import onnxruntime as ort
import win32clipboard
from PIL import Image, ImageGrab
from winotify import Notification

MODEL_URL = "https://huggingface.co/briaai/RMBG-1.4/resolve/main/onnx/model_fp16.onnx"
MODEL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "rmbgclip"
MODEL_PATH = MODEL_DIR / "model_fp16.onnx"
MODEL_SIZE = (1024, 1024)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
APP_ID = "rmbgclip"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def ensure_model() -> Path:
    if MODEL_PATH.exists():
        return MODEL_PATH
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading RMBG-1.4 (fp16) to {MODEL_PATH} ...")
    tmp = MODEL_PATH.with_suffix(".part")

    def progress(blocks, block_size, total):
        if total > 0:
            pct = min(100, blocks * block_size * 100 // total)
            print(f"\r  {pct}%", end="", flush=True)

    urllib.request.urlretrieve(MODEL_URL, tmp, progress)
    tmp.replace(MODEL_PATH)
    print("\nDownload complete.")
    return MODEL_PATH


def load_session() -> ort.InferenceSession:
    preferred = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    available = ort.get_available_providers()
    providers = [p for p in preferred if p in available] or ["CPUExecutionProvider"]
    return ort.InferenceSession(str(ensure_model()), providers=providers)


def remove_background(session: ort.InferenceSession, img: Image.Image) -> Image.Image:
    """Returns an RGBA image with the background made transparent."""
    rgb = img.convert("RGB")
    inp = session.get_inputs()[0]

    # Preprocess: resize to 1024x1024, scale to [0,1], subtract 0.5 (std = 1.0)
    resized = rgb.resize(MODEL_SIZE, Image.BILINEAR)
    arr = np.asarray(resized, dtype=np.float32) / 255.0 - 0.5
    arr = arr.transpose(2, 0, 1)[None, ...]
    arr = arr.astype(np.float16 if "float16" in inp.type else np.float32)

    out = session.run(None, {inp.name: arr})[0]
    mask = np.squeeze(out).astype(np.float32)

    # Postprocess: min-max normalise, resize back to the original size
    mask = (mask - mask.min()) / (mask.max() - mask.min() + 1e-8)
    mask_img = Image.fromarray((mask * 255).astype(np.uint8)).resize(rgb.size, Image.BILINEAR)

    result = rgb.copy()
    result.putalpha(mask_img)
    return result


# --------------------------------------------------------------------------- #
# Clipboard helpers
# --------------------------------------------------------------------------- #
def clipboard_seq() -> int:
    return ctypes.windll.user32.GetClipboardSequenceNumber()


def get_clipboard_image():
    """Returns a PIL image if the clipboard holds an image, else None."""
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return None  # clipboard briefly locked by another app, etc.

    if isinstance(data, Image.Image):
        return data
    if isinstance(data, list) and data:  # copied image *files* in Explorer
        p = Path(data[0])
        if p.suffix.lower() in IMAGE_EXTS:
            try:
                return Image.open(p)
            except Exception:
                return None
    return None


def set_clipboard_image(img: Image.Image) -> None:
    """Puts the image on the clipboard as PNG (keeps transparency) + DIB (fallback)."""
    png_buf = io.BytesIO()
    img.save(png_buf, "PNG")
    png_bytes = png_buf.getvalue()

    # Fallback for apps that only understand DIB: flatten onto white
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=img.getchannel("A"))
    bmp_buf = io.BytesIO()
    flat.save(bmp_buf, "BMP")
    dib_bytes = bmp_buf.getvalue()[14:]  # strip BITMAPFILEHEADER

    png_fmt = win32clipboard.RegisterClipboardFormat("PNG")

    for attempt in range(10):
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("Could not open the clipboard")

    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib_bytes)
        win32clipboard.SetClipboardData(png_fmt, png_bytes)
    finally:
        win32clipboard.CloseClipboard()


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #
def notify(message: str) -> None:
    try:
        Notification(app_id=APP_ID, title="rmbgclip", msg=message, duration="short").show()
    except Exception as e:
        print(f"(notification failed: {e})")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def process(session, img) -> None:
    t0 = time.time()
    result = remove_background(session, img)
    set_clipboard_image(result)
    print(f"Done in {time.time() - t0:.2f}s")
    notify("Background removed successfully")


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove image backgrounds from the clipboard with RMBG-1.4")
    parser.add_argument("--once", action="store_true", help="process the current clipboard image and exit")
    parser.add_argument("--interval", type=float, default=0.4, help="clipboard poll interval in seconds")
    args = parser.parse_args()

    session = load_session()

    if args.once:
        img = get_clipboard_image()
        if img is None:
            print("No image on the clipboard.")
            sys.exit(1)
        process(session, img)
        return

    print("rmbgclip running - copy an image to remove its background. Ctrl+C to quit.")
    last_seq = clipboard_seq()  # ignore whatever is on the clipboard right now

    while True:
        try:
            seq = clipboard_seq()
            if seq != last_seq:
                last_seq = seq
                time.sleep(0.15)  # let the copying app finish writing
                img = get_clipboard_image()
                if img is not None:
                    process(session, img)
                    last_seq = clipboard_seq()  # don't re-process our own output
            time.sleep(args.interval)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
            last_seq = clipboard_seq()
            time.sleep(1)


if __name__ == "__main__":
    main()