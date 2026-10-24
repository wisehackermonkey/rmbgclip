"""
rmbgclip - remove image backgrounds straight from the Windows clipboard (tray app).

Lives in the system tray. When an image is copied, it is run through RMBG-1.4,
the transparent cut-out is put back on the clipboard, and a toast is shown.

Tray menu:
    * Automatic background removal  (on/off - when off, you are asked each time)
    * Model quality                 (High / Medium / Low)
    * Set custom model URL...       (any RMBG-compatible .onnx URL or local path)
    * Quit

Install:
    pip install onnxruntime pillow numpy pywin32 winotify pystray
    (optional GPU: pip install onnxruntime-directml  -- instead of onnxruntime)

Run:
    pythonw rmbgclip.py      # no console window
    python  rmbgclip.py      # with console (for debugging)

Build a single standalone exe (Medium model bundled inside):
    pip install pyinstaller
    curl -L -o model_fp16.onnx https://huggingface.co/briaai/RMBG-1.4/resolve/main/onnx/model_fp16.onnx
    pyinstaller --onefile --noconsole --name rmbgclip --add-data "model_fp16.onnx;models" --hidden-import pystray._win32 rmbgclip.py
"""

import ctypes
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import onnxruntime as ort
import pystray
import win32clipboard
from PIL import Image, ImageDraw, ImageGrab
from pystray import Menu, MenuItem
from winotify import Notification

APP_ID = "rmbgclip"
MODEL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "rmbgclip"
CONFIG_PATH = MODEL_DIR / "config.json"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
# Folder holding models bundled by PyInstaller (or a "models" folder next to the script)
BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "models"

HF = "https://huggingface.co/briaai/RMBG-1.4/resolve/main/onnx/"
QUALITY_LEVELS = {
    # key: (menu label, url)
    "high": ("High (slow, highest quality)", HF + "model.onnx"),
    "medium": ("Medium (recommended)", HF + "model_fp16.onnx"),
    "low": ("Low (fastest, smallest)", HF + "model_quantized.onnx"),
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
DEFAULT_CONFIG = {"auto": True, "quality": "medium", "custom_url": "", "device": "auto"}

# key: (menu label, onnxruntime provider or None for "pick best")
DEVICES = {
    "auto": ("Auto (best available)", None),
    "cpu": ("CPU", "CPUExecutionProvider"),
    "dml": ("GPU - DirectML (any DirectX 12 GPU)", "DmlExecutionProvider"),
    "cuda": ("GPU - NVIDIA CUDA", "CUDAExecutionProvider"),
}
PROVIDER_NAMES = {
    "CPUExecutionProvider": "CPU",
    "DmlExecutionProvider": "GPU (DirectML)",
    "CUDAExecutionProvider": "GPU (CUDA)",
}


def device_available(key: str) -> bool:
    provider = DEVICES[key][1]
    return provider is None or provider in ort.get_available_providers()


def providers_for(device: str) -> list:
    available = ort.get_available_providers()
    wanted = DEVICES.get(device, DEVICES["auto"])[1]
    if wanted == "CPUExecutionProvider":
        return ["CPUExecutionProvider"]
    if wanted and wanted in available:
        return [wanted, "CPUExecutionProvider"]
    # auto (or the chosen GPU is no longer installed): best available
    preferred = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    return [p for p in preferred if p in available] or ["CPUExecutionProvider"]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
_cfg_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    if cfg["quality"] not in QUALITY_LEVELS:
        cfg["quality"] = DEFAULT_CONFIG["quality"]
    if cfg["device"] not in DEVICES:
        cfg["device"] = DEFAULT_CONFIG["device"]
    return cfg


def save_config() -> None:
    with _cfg_lock:
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"Could not save config: {e}")


cfg = load_config()


# --------------------------------------------------------------------------- #
# Notifications / dialogs
# --------------------------------------------------------------------------- #
def notify(message: str) -> None:
    try:
        Notification(app_id=APP_ID, title="rmbgclip", msg=message, duration="short").show()
    except Exception as e:
        print(f"(notification failed: {e}) {message}")


def message_box(text: str, style: int) -> int:
    # 0x40000 = MB_TOPMOST, 0x10000 = MB_SETFOREGROUND
    return ctypes.windll.user32.MessageBoxW(0, text, "rmbgclip", style | 0x40000 | 0x10000)


def ask_remove_background() -> bool:
    MB_YESNO, MB_ICONQUESTION, IDYES = 0x4, 0x20, 6
    return message_box(
        "An image was copied to the clipboard.\n\nRemove its background?",
        MB_YESNO | MB_ICONQUESTION,
    ) == IDYES


def ask_custom_url(initial: str):
    """Returns the entered string, or None if cancelled."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return simpledialog.askstring(
            "rmbgclip - custom model",
            "URL of an RMBG-compatible .onnx model (or a local file path).\n"
            "Leave empty to go back to the quality presets.",
            initialvalue=initial,
            parent=root,
        )
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# Model management
# --------------------------------------------------------------------------- #
def current_source() -> str:
    return cfg["custom_url"] or QUALITY_LEVELS[cfg["quality"]][1]


def current_model_label() -> str:
    if cfg["custom_url"]:
        return "Custom"
    return QUALITY_LEVELS[cfg["quality"]][0].split(" (")[0]


def normalize_url(url: str) -> str:
    if "huggingface.co" in url:
        url = url.replace("/blob/", "/resolve/")
    return url


def ensure_local(src: str) -> Path:
    """Maps a URL/path to a local file, downloading it if necessary."""
    if not re.match(r"^https?://", src, re.I):
        p = Path(src).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Model file not found: {p}")
        return p

    url = normalize_url(src)
    name = Path(urlparse(url).path).name or "model.onnx"
    if url in (u for _, u in QUALITY_LEVELS.values()):
        bundled = BUNDLED_DIR / name
        if bundled.is_file():  # shipped inside the exe - no download needed
            return bundled
        dest = MODEL_DIR / name
    else:
        dest = MODEL_DIR / f"custom_{hashlib.sha1(url.encode()).hexdigest()[:10]}_{name}"

    # Model bundled inside the PyInstaller exe (see build command at the top)
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "models" / name
        if bundled.is_file():
            return bundled

    if dest.exists():
        return dest

    notify(f"Downloading model {name} ...")
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "rmbgclip"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1024 * 1024):
            f.write(chunk)
    tmp.replace(dest)
    return dest


class ModelManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._session = None
        self._key = None
        self.active = ""  # human-readable device the loaded model runs on

    def _make_session(self, path: Path, providers: list) -> ort.InferenceSession:
        opts = ort.SessionOptions()
        if providers[0] == "DmlExecutionProvider":  # required by DirectML
            opts.enable_mem_pattern = False
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return ort.InferenceSession(str(path), sess_options=opts, providers=providers)

    def get(self) -> ort.InferenceSession:
        with self._lock:
            key = (current_source(), cfg["device"])
            if self._session is None or self._key != key:
                path = ensure_local(key[0])
                providers = providers_for(key[1])
                try:
                    session = self._make_session(path, providers)
                except Exception as e:
                    if providers == ["CPUExecutionProvider"]:
                        raise
                    notify(f"GPU unavailable, using CPU instead ({str(e)[:100]})")
                    session = self._make_session(path, ["CPUExecutionProvider"])
                self._session = session
                self._key = key
                self.active = PROVIDER_NAMES.get(session.get_providers()[0], "CPU")
            return self._session


model = ModelManager()


def preload_model(announce: bool) -> None:
    def work():
        try:
            model.get()
            if announce:
                notify(f"Model ready: {current_model_label()} on {model.active}")
        except Exception as e:
            notify(f"Could not load model: {e}")

    threading.Thread(target=work, daemon=True).start()


# --------------------------------------------------------------------------- #
# Background removal
# --------------------------------------------------------------------------- #
def remove_background(session: ort.InferenceSession, img: Image.Image) -> Image.Image:
    rgb = img.convert("RGB")
    inp = session.get_inputs()[0]

    # Use the model's fixed input size if it has one (RMBG-1.4 = 1024x1024)
    h, w = 1024, 1024
    if len(inp.shape) == 4 and isinstance(inp.shape[2], int) and isinstance(inp.shape[3], int):
        h, w = inp.shape[2], inp.shape[3]

    arr = np.asarray(rgb.resize((w, h), Image.BILINEAR), dtype=np.float32) / 255.0 - 0.5
    arr = arr.transpose(2, 0, 1)[None, ...]
    arr = arr.astype(np.float16 if "float16" in inp.type else np.float32)

    out = session.run(None, {inp.name: arr})[0]
    mask = np.squeeze(out).astype(np.float32)
    mask = (mask - mask.min()) / (mask.max() - mask.min() + 1e-8)
    mask_img = Image.fromarray((mask * 255).astype(np.uint8)).resize(rgb.size, Image.BILINEAR)

    result = rgb.copy()
    result.putalpha(mask_img)
    return result


# --------------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------------- #
def clipboard_seq() -> int:
    return ctypes.windll.user32.GetClipboardSequenceNumber()


def get_clipboard_image():
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return None
    if isinstance(data, Image.Image):
        return data
    if isinstance(data, list) and data:  # image files copied in Explorer
        p = Path(data[0])
        if p.suffix.lower() in IMAGE_EXTS:
            try:
                return Image.open(p)
            except Exception:
                return None
    return None


def set_clipboard_image(img: Image.Image) -> None:
    """PNG (keeps transparency) + DIB flattened on white (fallback)."""
    png_buf = io.BytesIO()
    img.save(png_buf, "PNG")

    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=img.getchannel("A"))
    bmp_buf = io.BytesIO()
    flat.save(bmp_buf, "BMP")
    dib_bytes = bmp_buf.getvalue()[14:]  # strip BITMAPFILEHEADER

    png_fmt = win32clipboard.RegisterClipboardFormat("PNG")
    for _ in range(10):
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
        win32clipboard.SetClipboardData(png_fmt, png_buf.getvalue())
    finally:
        win32clipboard.CloseClipboard()


# --------------------------------------------------------------------------- #
# Clipboard watcher thread
# --------------------------------------------------------------------------- #
stop_event = threading.Event()


def watch_clipboard() -> None:
    last_seq = clipboard_seq()  # ignore whatever is on the clipboard right now
    while not stop_event.is_set():
        try:
            seq = clipboard_seq()
            if seq != last_seq:
                last_seq = seq
                time.sleep(0.15)  # let the copying app finish writing
                img = get_clipboard_image()
                if img is not None:
                    if cfg["auto"] or ask_remove_background():
                        result = remove_background(model.get(), img)
                        set_clipboard_image(result)
                        notify("Background removed successfully")
                    last_seq = clipboard_seq()  # don't react to our own output
        except Exception as e:
            print(f"Error: {e}")
            notify(f"Background removal failed: {e}")
            last_seq = clipboard_seq()
            time.sleep(1)
        stop_event.wait(0.4)


# --------------------------------------------------------------------------- #
# Tray
# --------------------------------------------------------------------------- #
def make_icon_image(active: bool) -> Image.Image:
    size, cell = 64, 8
    im = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    for y in range(0, size, cell):  # transparency checkerboard
        for x in range(0, size, cell):
            if (x // cell + y // cell) % 2 == 0:
                d.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(205, 205, 205, 255))
    color = (46, 160, 67, 255) if active else (120, 120, 120, 255)
    d.ellipse((14, 14, 50, 50), fill=color, outline=(255, 255, 255, 255), width=3)
    return im


def refresh_icon(icon: pystray.Icon) -> None:
    icon.icon = make_icon_image(cfg["auto"])
    icon.title = f"rmbgclip - auto {'ON' if cfg['auto'] else 'OFF'} - {current_model_label()}"
    icon.update_menu()


def toggle_auto(icon, item):
    cfg["auto"] = not cfg["auto"]
    save_config()
    refresh_icon(icon)


def quality_item(key: str) -> MenuItem:
    label = QUALITY_LEVELS[key][0]

    def choose(icon, item):
        cfg["quality"] = key
        cfg["custom_url"] = ""  # picking a preset replaces any custom model
        save_config()
        refresh_icon(icon)
        preload_model(announce=True)

    return MenuItem(
        label,
        choose,
        checked=lambda item: not cfg["custom_url"] and cfg["quality"] == key,
        radio=True,
    )


def device_item(key: str) -> MenuItem:
    label = DEVICES[key][0]

    def choose(icon, item):
        cfg["device"] = key
        save_config()
        refresh_icon(icon)
        preload_model(announce=True)  # reloads the model on the new device

    return MenuItem(label, choose, checked=lambda item: cfg["device"] == key, radio=True)


def build_device_menu() -> Menu:
    items = [device_item(k) for k in DEVICES if device_available(k)]
    if not any(DEVICES[k][1] in ("DmlExecutionProvider", "CUDAExecutionProvider") and device_available(k)
               for k in DEVICES):
        items.append(Menu.SEPARATOR)
        if getattr(sys, "frozen", False):  # running as a PyInstaller exe: pip is not an option
            items.append(MenuItem("GPU support not included in this build", None, enabled=False))
        else:
            items.append(MenuItem("No GPU provider installed", None, enabled=False))
            items.append(MenuItem("(pip install onnxruntime-directml)", None, enabled=False))
    return Menu(*items)


def set_custom_url(icon, item):
    def work():
        value = ask_custom_url(cfg["custom_url"])
        if value is None:  # cancelled
            return
        value = value.strip()
        if value and not re.match(r"^https?://", value, re.I) and not Path(value).expanduser().is_file():
            message_box("That is neither an http(s) URL nor an existing file.", 0x10)  # MB_ICONERROR
            return
        cfg["custom_url"] = value
        save_config()
        refresh_icon(icon)
        preload_model(announce=True)

    threading.Thread(target=work, daemon=True).start()


def quit_app(icon, item):
    stop_event.set()
    icon.stop()


def main() -> None:
    menu = Menu(
        MenuItem(
            lambda item: f"Model: {current_model_label()}" + (f" on {model.active}" if model.active else ""),
            None,
            enabled=False,
        ),
        Menu.SEPARATOR,
        MenuItem("Automatic background removal", toggle_auto, checked=lambda item: cfg["auto"]),
        MenuItem(
            "Model quality level",
            Menu(*(quality_item(k) for k in QUALITY_LEVELS)),
        ),
        MenuItem("Hardware acceleration", build_device_menu()),
        MenuItem(
            "Set custom model URL...",
            set_custom_url,
            checked=lambda item: bool(cfg["custom_url"]),
        ),
        Menu.SEPARATOR,
        MenuItem("Quit", quit_app),
    )

    icon = pystray.Icon(APP_ID, make_icon_image(cfg["auto"]), "rmbgclip", menu)

    def setup(icon):
        icon.visible = True
        refresh_icon(icon)
        preload_model(announce=False)  # download/load the model up front
        threading.Thread(target=watch_clipboard, daemon=True).start()

    icon.run(setup)


if __name__ == "__main__":
    main()