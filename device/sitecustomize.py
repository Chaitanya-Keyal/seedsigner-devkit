"""
Display and input hacks for a seedsigner-os -dev device. They wrap the
display drivers' show_image and the buttons' GPIO reads, so the app itself still renders
and reads input exactly as usual.

1. Cracked LCD: if /mnt/data/display-region exists ("SIZE X Y", e.g. "132 108 0"), each
   frame is scaled to SIZE x SIZE and drawn at (X, Y) on the LCD; the rest is black.
2. HDMI mirror: if /mnt/data/display-mirror exists ("SCALE VT", e.g. "1 3" for actual
   size on tty3), each frame is also drawn centred on a black HDMI framebuffer, scaled
   up SCALE times (nearest neighbour; "auto" = the largest whole-number scale that fits
   the framebuffer, which may be 16- or 32-bit), but only
   while virtual terminal VT is the active one (Alt+F<VT>), so it never draws over the
   shell on tty1. Switching to that VT repaints the last frame. The screen is drawn
   inside a simple sketch of the device (d-pad, KEY1-3, the display type as a title). Every
   driver is wrapped, so Settings > Display type (st7789 240x240 / 320x240, ili9341
   320x240) changes the mirror.
   The physical 240x240 panel is always drawn by the original driver: the app's frame
   letterboxed to fit, or "Showing on HDMI monitor" while the mirror is on screen.

3. USB keyboard: while the mirror is on and its VT is active, keys act as the hardware
   buttons: arrows = joystick, Enter/Space =
   joystick press, 1/2/3 = KEY1/KEY2/KEY3. On any other VT (e.g. the tty1 shell) the
   keyboard doesn't reach the app.

4. Service starts: when the service starts the app (boot or `seedsigner restart`), HDMI
   switches to the mirror's VT, and the app's stdout and stderr (otherwise /dev/null) go
   to /var/log/seedsigner.log, in RAM like the rest of /var/log; the previous run's log
   is kept as seedsigner.log.1. A run from a terminal is left alone.

The two config files are re-read as frames are drawn; delete one to switch that hack
off. This file lives in the app venv's site-packages, which ss-deploy never touches;
recreating the venv removes it; reinstall with host/ss-device-setup from seedsigner-devkit.
"""
import importlib.abc
import importlib.machinery
import os
import sys
import threading

# module -> display driver class wrapped in it
_TARGETS = {
    "seedsigner.hardware.displays.ST7789": "ST7789",
    "seedsigner.hardware.displays.st7789_mpy": "ST7789",
    "seedsigner.hardware.displays.ili9341": "ILI9341",
}
_ORIGINAL_ST7789 = "seedsigner.hardware.displays.ST7789"
_BUTTONS = "seedsigner.hardware.buttons"

# Set once the app's buttons module loads: returns the names of the held buttons
_pressed_buttons = None
_REGION_CONFIG = "/mnt/data/display-region"
_MIRROR_CONFIG = "/mnt/data/display-mirror"


def _read_ints(path, count):
    try:
        with open(path) as f:
            values = [int(v) for v in f.read().split()[:count]]
        return values if len(values) == count else None
    except (OSError, ValueError):
        return None


def _read_mirror_config():
    """Returns (scale or None for auto, vt number), or None if the mirror is off."""
    try:
        with open(_MIRROR_CONFIG) as f:
            fields = f.read().split()
        scale = None if fields[0] == "auto" else int(fields[0])
        return scale, int(fields[1])
    except (OSError, ValueError, IndexError):
        return None


_FONT = "/mnt/data/seedsigner/src/seedsigner/resources/fonts/OpenSans-SemiBold.ttf"


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(_FONT, size)
    except OSError:
        return ImageFont.load_default()


_ORANGE = (255, 159, 10)    # SeedSigner accent
_SS = 2                     # supersampling factor for smooth rounded edges


def _draw_board(width, height, pressed=frozenset(), title=""):
    """
    A simple flat sketch of the device around a width x height screen: a d-pad on the
    left, KEY1-3 on the right, and `title` (the display type) above. `pressed` holds the
    buttons to light up: up/down/left/right/press/key1/key2/key3.
    Returns (image, screen top-left corner), with the screen area left black.
    """
    from PIL import Image, ImageDraw

    pad, left_w, right_w, title_h, btn = 22, 128, 104, 36, 34
    board_w = pad + left_w + pad + width + pad + right_w + pad
    board_h = pad + height + pad
    W, H = board_w, title_h + board_h

    def s(v):
        return round(v * _SS)

    image = Image.new("RGB", (s(W), s(H)), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    idle, glyph, glyph_held = (58, 61, 68), (205, 207, 212), (30, 24, 14)

    if title:
        draw.text((s(W / 2), s(title_h / 2)), title, font=_font(s(17)), fill=(170, 174, 182),
                  anchor="mm")

    top = title_h
    draw.rounded_rectangle((0, s(top), s(board_w) - 1, s(top + board_h) - 1), radius=s(18),
                           fill=(26, 28, 32))

    # Screen: plain black
    sx, sy = pad + left_w + pad, top + pad
    draw.rectangle((s(sx), s(sy), s(sx + width) - 1, s(sy + height) - 1), fill=(0, 0, 0))

    # D-pad: four arrow buttons around a centre (press) button
    cx, cy = pad + left_w / 2, top + board_h / 2
    gap = btn + 4
    for name, (dx, dy) in (("up", (0, -1)), ("down", (0, 1)), ("left", (-1, 0)),
                           ("right", (1, 0))):
        bx, by = cx + dx * gap, cy + dy * gap
        held = name in pressed
        draw.rounded_rectangle((s(bx - btn / 2), s(by - btn / 2), s(bx + btn / 2),
                                s(by + btn / 2)), radius=s(8), fill=_ORANGE if held else idle)
        t = 8   # arrow half-size
        if dy:
            pts = [(bx, by + dy * t), (bx - t, by - dy * t * 0.6), (bx + t, by - dy * t * 0.6)]
        else:
            pts = [(bx + dx * t, by), (bx - dx * t * 0.6, by - t), (bx - dx * t * 0.6, by + t)]
        draw.polygon([(s(x), s(y)) for x, y in pts], fill=glyph_held if held else glyph)
    held = "press" in pressed
    r = btn / 2 - 2
    draw.ellipse((s(cx - r), s(cy - r), s(cx + r), s(cy + r)), fill=_ORANGE if held else idle)
    draw.ellipse((s(cx - 6), s(cy - 6), s(cx + 6), s(cy + 6)),
                 fill=glyph_held if held else glyph)

    # KEY1-3: rounded buttons with their label inside
    kx = pad + left_w + pad + width + pad + right_w / 2
    key_w, key_h, key_gap = 82, 34, 18
    font = _font(s(14))
    for i, name in enumerate(("key1", "key2", "key3")):
        ky = top + board_h / 2 + (i - 1) * (key_h + key_gap)
        held = name in pressed
        draw.rounded_rectangle((s(kx - key_w / 2), s(ky - key_h / 2), s(kx + key_w / 2),
                                s(ky + key_h / 2)), radius=s(key_h / 2),
                               fill=_ORANGE if held else idle)
        draw.text((s(kx), s(ky)), name.upper(), font=font,
                  fill=glyph_held if held else glyph, anchor="mm")

    image = image.resize((W, H), Image.LANCZOS)
    return image, (sx, sy)


class _HdmiMirror:
    """Draws frames onto /dev/fb0 while a chosen virtual terminal is active."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_frame = None
        self._fb = None
        self._active_vt = None
        self._last_box = None
        self._boards = {}
        self._pressed = frozenset()
        self.title = ""
        self._vt_change_callbacks = []
        threading.Thread(target=self._watch_vt, daemon=True).start()
        threading.Thread(target=self._watch_buttons, daemon=True).start()

    def on_vt_change(self, callback):
        self._vt_change_callbacks.append(callback)

    @classmethod
    def is_active(cls):
        """True while the mirror is configured and its VT is the one on screen."""
        config = _read_mirror_config()
        return bool(config) and cls._current_vt() == f"tty{config[1]}"

    @staticmethod
    def _current_vt():
        try:
            with open("/sys/class/tty/tty0/active") as f:
                return f.read().strip()
        except OSError:
            return None

    def _open_fb(self):
        import fcntl
        import mmap
        import struct
        import numpy as np

        fd = open("/dev/fb0", "r+b", buffering=0)
        var = struct.unpack("20I", fcntl.ioctl(fd, 0x4600, bytes(160))[:80])
        xres, yres, bpp = var[0], var[1], var[6]
        if bpp not in (16, 32):
            raise RuntimeError(f"unsupported framebuffer depth {bpp}")
        # (offset, length) of red, green, blue and alpha, e.g. RGB565 or 32-bit BGRA
        fields = [(var[i], var[i + 1]) for i in (8, 11, 14, 17)]
        dtype = np.uint16 if bpp == 16 else np.uint32
        with open("/sys/class/graphics/fb0/stride") as f:
            stride = int(f.read())
        mem = mmap.mmap(fd.fileno(), stride * yres)
        pixels = np.frombuffer(mem, dtype=dtype).reshape(yres, stride * 8 // bpp)
        pixels = pixels[:, :xres]
        self._fb = (fd, mem, pixels, fields)

    @staticmethod
    def _hide_cursor(vt):
        try:
            with open(f"/dev/{vt}", "w") as tty:
                tty.write("\033[?25l\033[2J")
            with open("/sys/class/graphics/fbcon/cursor_blink", "w") as f:
                f.write("0")
        except OSError:
            pass

    def _draw(self, frame, clear=False):
        import numpy as np

        config = _read_mirror_config()
        if not config or frame is None:
            return
        scale, vt = config
        if self._current_vt() != f"tty{vt}":
            return
        if self._fb is None:
            self._open_fb()
        pixels = self._fb[2]
        if clear:
            pixels[:] = 0

        key = (frame.size, self._pressed, self.title)
        if key not in self._boards:
            if len(self._boards) > 64:
                self._boards.clear()
            self._boards[key] = _draw_board(*frame.size, pressed=self._pressed, title=self.title)
        board, screen_pos = self._boards[key]
        composite = board.copy()
        composite.paste(frame.convert("RGB"), screen_pos)

        if scale is None:
            scale = max(1, min(pixels.shape[1] // composite.width,
                               pixels.shape[0] // composite.height))

        rgb = np.asarray(composite, dtype=pixels.dtype)
        rgb = rgb.repeat(scale, axis=0).repeat(scale, axis=1)
        (r_off, r_len), (g_off, g_len), (b_off, b_len), (a_off, a_len) = self._fb[3]
        packed = (((rgb[..., 0] >> (8 - r_len)) << r_off)
                  | ((rgb[..., 1] >> (8 - g_len)) << g_off)
                  | ((rgb[..., 2] >> (8 - b_len)) << b_off)
                  | pixels.dtype.type(((1 << a_len) - 1) << a_off))
        h = min(packed.shape[0], pixels.shape[0])
        w = min(packed.shape[1], pixels.shape[1])
        y0 = (pixels.shape[0] - h) // 2
        x0 = (pixels.shape[1] - w) // 2
        if (y0, x0, h, w) != self._last_box:
            # The picture changed size (display type or scale changed): clear what the
            # previous, possibly bigger, picture left behind
            pixels[:] = 0
            self._last_box = (y0, x0, h, w)
        pixels[y0:y0 + h, x0:x0 + w] = packed[:h, :w]

    def show(self, frame):
        if frame is None:
            return
        with self._lock:
            self._last_frame = frame.copy()
            try:
                self._draw(self._last_frame)
            except Exception as e:
                print(f"hdmi mirror: {e}", file=sys.stderr)

    def _watch_buttons(self):
        """Redraw the board when a button (physical or keyboard) is pressed or released."""
        import time

        while True:
            time.sleep(0.04)
            if _pressed_buttons is None or not self.is_active():
                continue
            pressed = _pressed_buttons()
            if pressed != self._pressed:
                with self._lock:
                    self._pressed = pressed
                    try:
                        self._draw(self._last_frame)
                    except Exception as e:
                        print(f"hdmi mirror: {e}", file=sys.stderr)

    def _watch_vt(self):
        import time

        allocated = None
        while True:
            time.sleep(0.3)
            config = _read_mirror_config()
            # Alt+F<n> ignores a VT nothing has opened yet, and after boot nothing opens
            # the mirror's; opening its tty device allocates it.
            if config and config[1] != allocated:
                try:
                    open(f"/dev/tty{config[1]}").close()
                    allocated = config[1]
                except OSError:
                    pass
            vt = self._current_vt()
            if config and vt == f"tty{config[1]}" and vt != self._active_vt:
                self._hide_cursor(vt)
                with self._lock:
                    try:
                        self._draw(self._last_frame, clear=True)
                    except Exception as e:
                        print(f"hdmi mirror: {e}", file=sys.stderr)
            if vt != self._active_vt:
                for callback in self._vt_change_callbacks:
                    try:
                        callback()
                    except Exception as e:
                        print(f"vt change: {e}", file=sys.stderr)
            self._active_vt = vt


class _Keyboard:
    """Tracks held keys on every USB keyboard via evdev, in a background thread."""

    # struct input_event on 32-bit ARM: long sec, long usec, u16 type, u16 code, s32 value
    _EVENT = "llHHi"
    _EV_KEY = 1

    def __init__(self):
        self._held = set()
        self._lock = threading.Lock()
        self._devices = {}
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def active():
        """The keyboard only drives the app while the mirror is on and its VT on screen."""
        return _HdmiMirror.is_active()

    def is_held(self, codes):
        with self._lock:
            return any(code in self._held for code in codes)

    def _scan(self):
        import glob
        import os

        for path in glob.glob("/dev/input/event*"):
            if path in self._devices:
                continue
            try:
                self._devices[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                pass

    def _run(self):
        import os
        import select
        import struct
        import time

        size = struct.calcsize(self._EVENT)
        last_scan = 0
        while True:
            if time.monotonic() - last_scan > 3:
                self._scan()
                last_scan = time.monotonic()
            if not self._devices:
                time.sleep(1)
                continue
            ready, _, _ = select.select(list(self._devices.values()), [], [], 1)
            for fd in ready:
                try:
                    data = os.read(fd, size * 64)
                except OSError:
                    # Unplugged: forget it and drop any keys it was holding
                    for path, dev_fd in list(self._devices.items()):
                        if dev_fd == fd:
                            del self._devices[path]
                    os.close(fd)
                    with self._lock:
                        self._held.clear()
                    continue
                for offset in range(0, len(data) - size + 1, size):
                    _, _, ev_type, code, value = struct.unpack_from(self._EVENT, data, offset)
                    if ev_type != self._EV_KEY:
                        continue
                    with self._lock:
                        if value:          # 1 = press, 2 = autorepeat
                            self._held.add(code)
                        else:              # 0 = release
                            self._held.discard(code)


class _GpioWithKeyboard:
    """Stands in for RPi.GPIO in HardwareButtons: a mapped key held = that pin LOW."""

    def __init__(self, gpio, keyboard, key_map):
        self._gpio = gpio
        self._keyboard = keyboard
        self._key_map = key_map

    def input(self, pin):
        codes = self._key_map.get(pin)
        if codes and self._keyboard.active() and self._keyboard.is_held(codes):
            return self._gpio.LOW
        return self._gpio.input(pin)

    def __getattr__(self, name):
        return getattr(self._gpio, name)


def _patch_buttons(module):
    HardwareButtons = module.HardwareButtons
    original_get_instance = HardwareButtons.get_instance.__func__
    keyboard = _Keyboard()

    # Linux input keycodes
    KEY_ENTER, KEY_KPENTER, KEY_SPACE = 28, 96, 57
    KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 103, 108, 105, 106
    KEY_1, KEY_2, KEY_3 = 2, 3, 4
    key_map = {
        HardwareButtons.KEY_UP_PIN: (KEY_UP,),
        HardwareButtons.KEY_DOWN_PIN: (KEY_DOWN,),
        HardwareButtons.KEY_LEFT_PIN: (KEY_LEFT,),
        HardwareButtons.KEY_RIGHT_PIN: (KEY_RIGHT,),
        HardwareButtons.KEY_PRESS_PIN: (KEY_ENTER, KEY_KPENTER, KEY_SPACE),
        HardwareButtons.KEY1_PIN: (KEY_1,),
        HardwareButtons.KEY2_PIN: (KEY_2,),
        HardwareButtons.KEY3_PIN: (KEY_3,),
    }

    def get_instance(cls):
        instance = original_get_instance(cls)
        if not isinstance(instance.GPIO, _GpioWithKeyboard):
            instance.GPIO = _GpioWithKeyboard(instance.GPIO, keyboard, key_map)
        return instance

    HardwareButtons.get_instance = classmethod(get_instance)

    pin_names = {
        HardwareButtons.KEY_UP_PIN: "up", HardwareButtons.KEY_DOWN_PIN: "down",
        HardwareButtons.KEY_LEFT_PIN: "left", HardwareButtons.KEY_RIGHT_PIN: "right",
        HardwareButtons.KEY_PRESS_PIN: "press", HardwareButtons.KEY1_PIN: "key1",
        HardwareButtons.KEY2_PIN: "key2", HardwareButtons.KEY3_PIN: "key3",
    }

    def pressed():
        # Read through the app's (wrapped) GPIO, so physical buttons and the keyboard
        # both count; nothing until the app has set the buttons up
        instance = HardwareButtons._instance
        if instance is None:
            return frozenset()
        gpio = instance.GPIO
        return frozenset(name for pin, name in pin_names.items() if gpio.input(pin) == gpio.LOW)

    global _pressed_buttons
    _pressed_buttons = pressed


_mirror = None

# The physical 240x240 ST7789 panel: the original driver class and its unwrapped
# show_image, the driver instance drawing on it, and the last app frame
_lcd = {}
_lcd_lock = threading.Lock()


def _monitor_message(width, height):
    """What the LCD shows while the app is mirrored to HDMI."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(image)
    font = _font(34)
    draw.multiline_text((width // 2, height // 2), "Showing on\nHDMI\nmonitor", font=font,
                        fill="#FF9F0A", anchor="mm", align="center", spacing=8)
    return image


def _fit_to_panel(frame, size=240):
    """Letterbox a frame of any size (240x240 or 320x240) into the square panel."""
    from PIL import Image

    frame = frame.convert("RGB")
    scale = min(size / frame.width, size / frame.height)
    if scale != 1:
        frame = frame.resize((round(frame.width * scale), round(frame.height * scale)),
                             Image.LANCZOS)
    panel = Image.new("RGB", (size, size), "black")
    panel.paste(frame, ((size - frame.width) // 2, (size - frame.height) // 2))
    return panel


def _draw_physical_lcd():
    """
    Draws the last app frame, or the HDMI message while mirrored, on the physical
    240x240 panel, always through the original ST7789 driver. Call with _lcd_lock held.
    """
    from PIL import Image

    frame = _lcd.get("frame")
    if frame is None:
        return
    if "instance" not in _lcd:
        # The app is emulating another display size (st7789 320x240), whose driver
        # would only draw garbage on this panel. Drive the real panel with the original
        # 240x240 driver instead; its init resets the panel to 240x240.
        importlib.import_module(_ORIGINAL_ST7789)
        _lcd["instance"] = _lcd["cls"](_width=240, _height=240)

    image = _monitor_message(240, 240) if _mirror.is_active() else _fit_to_panel(frame)
    region = _read_ints(_REGION_CONFIG, 3)
    if region:
        size, x, y = region
        small = image.resize((size, size), Image.LANCZOS)
        image = Image.new("RGB", (240, 240), "black")
        image.paste(small, (x, y))
    _lcd["show"](_lcd["instance"], image, 0, 0)


def _redraw_physical_lcd():
    # Swap between the app and the "Showing on HDMI" message right away on a VT
    # switch, not on the app's next frame
    with _lcd_lock:
        _draw_physical_lcd()


def _patch(module):
    global _mirror
    if _mirror is None:
        _mirror = _HdmiMirror()
        _mirror.on_vt_change(_redraw_physical_lcd)

    driver_cls = getattr(module, _TARGETS[module.__name__])

    if module.__name__ == _ORIGINAL_ST7789:
        _lcd["cls"] = driver_cls
        _lcd["show"] = driver_cls.show_image
        original_post_init = driver_cls.__post_init__

        def __post_init__(self):
            original_post_init(self)
            # The first instance (the app's own, at 240x240) owns the panel
            _lcd.setdefault("instance", self)

        driver_cls.__post_init__ = __post_init__
    else:
        # Another driver's setup (its __post_init__, and ILI9341's begin()) sends the
        # panel its own orientation and colour commands, possibly after our 240x240
        # driver already set it up. Once it's done, set the panel back up for 240x240.
        def reclaim_panel_after(method):
            def wrapper(self, *args, **kwargs):
                result = method(self, *args, **kwargs)
                with _lcd_lock:
                    if "instance" in _lcd:
                        _lcd["instance"].init()
                        _draw_physical_lcd()
                return result
            return wrapper

        driver_cls.__post_init__ = reclaim_panel_after(driver_cls.__post_init__)
        if hasattr(driver_cls, "begin"):
            driver_cls.begin = reclaim_panel_after(driver_cls.begin)

    def show_image(self, image=None, *args, **kwargs):
        # Every display size goes to the HDMI mirror as-is. The physical panel only
        # ever gets drawn by the original 240x240 driver.
        if image is None:
            # ILI9341 convention: redraw the driver's own buffer
            image = getattr(self, "buffer", None)
            if image is None:
                return
        _mirror.title = f"{driver_cls.__name__}  ·  {image.width}×{image.height}"
        _mirror.show(image)
        with _lcd_lock:
            _lcd["frame"] = image.copy()
            _draw_physical_lcd()

    driver_cls.show_image = show_image


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        if module.__name__ == _BUTTONS:
            _patch_buttons(module)
        else:
            _patch(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname not in _TARGETS and fullname != _BUTTONS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _PatchingLoader(spec.loader)
        return spec


sys.meta_path.insert(0, _Finder())


# Started by the service (boot or `seedsigner restart`), whose output goes to /dev/null
_LOG = "/var/log/seedsigner.log"
if sys.argv[0].endswith("main.py") and os.path.realpath("/proc/self/fd/2") == "/dev/null":
    _mirror_config = _read_mirror_config()
    if _mirror_config:
        import fcntl
        with open("/dev/tty0") as _tty:
            fcntl.ioctl(_tty, 0x5606, _mirror_config[1])  # VT_ACTIVATE: app on HDMI
    try:
        os.replace(_LOG, _LOG + ".1")
    except OSError:
        pass
    _fd = os.open(_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.dup2(_fd, 1)
    os.dup2(_fd, 2)
    os.close(_fd)
    # stdout to a file is block-buffered, which would hold prints back
    sys.stdout.reconfigure(line_buffering=True)
