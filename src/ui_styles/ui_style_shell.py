#!/usr/bin/env python3
"""
ui_style_shell.py — Config-driven PyQt5 UI shell (TinyImgApp style).

VISUAL IDENTITY: "Mono Glass" — pure BLACK & WHITE frosted glass.
  * NO blue, NO colour of any kind (greyscale only)
  * every colour + transparency value lives in section [1.1]
  * REAL backdrop blur (emulated `backdrop-filter: blur(...)`)
  * all text black; only greys used for muted states
  * 16px radii, 56px tall fields, frameless, no shadow

v6.0.0 CHANGES  (what you asked for)
  1) DROPDOWN FLOATS OVER THE WHOLE WINDOW
       - the popup is its own top-level Qt.Popup window, so it is NOT
         limited by the card / window height any more; it may cover the
         entire window box and hang outside it in any direction
       - the window itself no longer has any height cap (config "size"
         height is ignored, the card just grows to fit its rows)
  2) FULL-HEIGHT MENU, NO UP/DOWN SWITCH
       - `combobox-popup: 0` puts Qt in list mode -> the native
         QComboBoxPrivateScroller up/down arrow strips never appear
         (any that slip through are force-hidden)
       - height = SUM of every real row height (measured, not guessed)
       - scrollbars hard-off; a slim bar only appears if the list is
         physically taller than the whole screen
       - mouse-wheel over the closed combo no longer changes the value
  3) FUZZIER BACKGROUND
       - multi-pass blur (BACKDROP_PASSES) + heavier radius
       - stronger down-scale (extra softness for free) + bigger blooms

Only the visual layer is themed. Config schema, worker thread, param
widgets, validation, drag and run flow are unchanged.

--------------------------------------------------------------------------
QUICK TUNING CHEAT-SHEET  (everything is in section [1.1])
--------------------------------------------------------------------------
  CARD_TINT_RGB       window colour      (255,255,255)=white (240,240,240)=grey
  CARD_ALPHA          window opacity     1.00 = solid   0.60 = very glassy
  BACKDROP_ENABLED    real blur on/off   True / False
  BACKDROP_BLUR       blur strength      18 = frosty   48 = very fuzzy
  BACKDROP_PASSES     blur passes        1 = fast      3 = dreamy mush
  BACKDROP_DOWNSCALE  1 = sharp, 4 = extra fuzzy + fast
  FIELD_ALPHA         input/button glass lower = more see-through
  BLOOM_STRENGTH      fake-blur blobs    0.00 = off
  SHEEN_ALPHA         diagonal highlight 0.00 = off
  BORDER_ALPHA        hairline darkness  0.30 = strong black outlines
  GLASS_RADIUS        corner radius      16
  POPUP_SHOW_ALL      never scroll menus True / False
  POPUP_MAY_OVERLAP   menu may cover the window box   True / False
--------------------------------------------------------------------------
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from PyQt5.QtCore import (
    Qt, QEvent, QPoint, QPointF, QRect, QRectF, QThread, QTimer, pyqtSignal,
)
from PyQt5.QtGui import (
    QColor, QImage, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
    QLinearGradient,
)
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QComboBox,
    QCheckBox, QVBoxLayout, QHBoxLayout, QFileDialog, QMessageBox,
    QProgressBar, QDialog, QSizePolicy, QAbstractButton, QAbstractSlider,
    QAbstractItemView, QListView, QStyledItemDelegate, QFrame,
    QGraphicsScene, QGraphicsPixmapItem, QGraphicsBlurEffect,
)

__all__ = ["FunctionShell", "run_shell", "Theme", "load_config"]
__version__ = "6.0.0"

logger = logging.getLogger(__name__)

QWIDGETSIZE_MAX = 16777215


# =========================================================================== #
# [1] DESIGN TOKENS — the ONLY section you need to edit
# =========================================================================== #

# --------------------------------------------------------------------------- #
# [1.0] tiny colour helpers (used by every token below)
# --------------------------------------------------------------------------- #

def rgba(rgb: tuple[int, int, int], alpha: float) -> str:
    """(r,g,b) + alpha 0..1  ->  'rgba(r, g, b, a)' CSS string."""
    r, g, b = rgb
    return f"rgba({r}, {g}, {b}, {round(float(alpha), 3)})"


def _shade(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """Darken an (r,g,b) tint by `amount` (0..1). Keeps it greyscale-safe."""
    return tuple(max(0, min(255, int(round(c * (1.0 - amount)))))
                 for c in rgb)  # type: ignore[return-value]


def _grey(value: int) -> tuple[int, int, int]:
    """Pure greyscale tuple, 0 = black, 255 = white."""
    v = max(0, min(255, int(value)))
    return (v, v, v)


# --------------------------------------------------------------------------- #
# [1.1] ★ TINT & TRANSPARENCY ★  — main knobs, black & white only
#       Keep R == G == B in every *_RGB value to stay mono.
# --------------------------------------------------------------------------- #

# ---- shared corner radius (CSS: border-radius: 16px) ---------------------- #
GLASS_RADIUS        = 16

# ---- REAL backdrop blur  (CSS: backdrop-filter: blur(Npx)) --------------- #
BACKDROP_ENABLED    = True     # False -> fall back to painted blobs only
BACKDROP_BLUR       = 70.0    # ★ blur radius in screen px (was 18 -> fuzzier)
BACKDROP_PASSES     = 2        # ★ extra gaussian passes = creamy mush
BACKDROP_DOWNSCALE  = 4        # ★ 1 = sharp, 4 = very soft + fast
BACKDROP_ON_MOVE    = False    # re-grab the desktop after each window drag
POPUP_USE_BACKDROP  = False    # popups sit on the card -> keep them opaque

# ---- window / card surface ------------------------------------------------ #
CARD_TINT_RGB       = _grey(252)   # window base colour (252 = near-white)
CARD_ALPHA          = 0.90         # ★ a bit more see-through: blur shows more
CARD_GRAD_SPREAD    = 0.06         # how much darker the bottom is (0 = flat)

# ---- fields: pickers, inputs, selects, toggles ---------------------------- #
FIELD_TINT_RGB      = _grey(255)   # field glass colour
FIELD_ALPHA         = 0.70         # field opacity (resting)
FIELD_ALPHA_HOVER   = 0.92         # field opacity (mouse over)

# ---- dropdown popup ------------------------------------------------------- #
POPUP_TINT_RGB      = _grey(255)
POPUP_ALPHA         = 1       # high: the menu floats over the card
POPUP_SHEEN_ALPHA   = 0.07
POPUP_BORDER_ALPHA  = 0.16

# ---- message dialog ------------------------------------------------------- #
DIALOG_TINT_RGB     = _grey(255)
DIALOG_ALPHA        = 0.97

# ---- outlines ------------------------------------------------------------- #
BORDER_TINT_RGB     = _grey(0)     # black hairlines around fields
BORDER_ALPHA        = 0.13         # raise to 0.30 for hard black outlines
RIM_TINT_RGB        = _grey(255)   # bright rim on the card edge
RIM_ALPHA           = 0.85

# ---- painted blooms (extra fuzz on top of the real blur) ------------------ #
BLOOM_STRENGTH      = 0.75         # ★ stronger fuzz (0 = disable blooms)
SHEEN_ALPHA         = 0.12         # diagonal white highlight, 0 = disable

# soft blobs: (x 0..1, y 0..1, size factor, grey 0-255, alpha 0..1)
GLASS_BLOBS = (
    (0.14, 0.06, 0.90, 255, 0.36),   # top-left white bloom
    (0.92, 0.16, 0.76, 245, 0.28),   # top-right pale bloom
    (0.60, 0.52, 1.05, 205, 0.16),   # centre grey haze
    (0.06, 0.84, 0.88, 250, 0.26),   # bottom-left light bloom
    (0.96, 0.94, 0.82, 214, 0.18),   # bottom-right grey bloom
)


# --------------------------------------------------------------------------- #
# [1.2] TEXT COLOURS — all black; greys only for muted states
# --------------------------------------------------------------------------- #
TEXT_PRIMARY        = "#000000"    # titles, values, captions, labels
TEXT_SECONDARY      = "#000000"    # field captions / picker labels
TEXT_SUBTITLE       = "#000000"    # header subtitle
TEXT_PLACEHOLDER    = "#9A9A9A"    # empty-input hint (grey = still mono)
TEXT_DISABLED       = "#9A9A9A"    # disabled run button label
TEXT_ON_INK         = "#FFFFFF"    # text sitting on the black button

INK                 = "#000000"    # primary black fill (run btn, toggles)
INK_HOVER           = "#1A1A1A"    # black hover
ACCENT              = "#FFFFFF"    # kept for API compat (on-dark text)
ACCENT_HOVER        = "#FFFFFF"


# --------------------------------------------------------------------------- #
# [1.3] TYPOGRAPHY — one line per widget kind
# --------------------------------------------------------------------------- #
FONT_FAMILY          = 'Arial, sans-serif'

TITLE_FONT_SIZE      = 40     # header title
TITLE_FONT_WEIGHT    = 7

SUBTITLE_FONT_SIZE   = 19    # header subtitle
SUBTITLE_FONT_WEIGHT = 600

PICKER_FONT_SIZE     = 16     # folder / file picker buttons
PICKER_FONT_BOLD     = False

SELECT_FONT_SIZE     = 16     # dropdown current value
SELECT_FONT_BOLD     = False
SELECT_LABEL_DIM     = 1.0    # caption size = PICKER_FONT_SIZE * this

PILL_FONT_SIZE       = 16     # toggles
PILL_FONT_BOLD       = False

INPUT_FONT_SIZE      = 16     # QLineEdit
INPUT_FONT_BOLD      = False

RUN_FONT_SIZE        = 15     # black run button
RUN_FONT_BOLD        = True
RUN_LETTER_SPACING   = 1      # px

POPUP_FONT_SIZE      = 16     # dropdown rows
DIALOG_FONT_SIZE     = 16     # dialog body
CARET_SIZE           = 13     # ▾ glyph size
CLOSE_FONT_SIZE      = 20     # ✕ glyph size
CLOSE_FONT_BOLD      = False


# --------------------------------------------------------------------------- #
# [1.4] GEOMETRY — sizes, radii, spacing
# --------------------------------------------------------------------------- #
ROW_GAP             = 12                  # vertical gap between rows
CARD_MARGIN         = (28, 12, 28, 28)    # card inset (L, T, R, B)
WINDOW_RADIUS       = GLASS_RADIUS        # card corner radius (16px)
WINDOW_BORDER       = 1                   # card border thickness

BTN_HEIGHT          = 56    # shared field / select / toggle height
RUN_HEIGHT          = 56    # run button height
PROGRESS_HEIGHT     = 6     # progress bar height

FIELD_RADIUS        = GLASS_RADIUS    # pickers / selects / toggles
INPUT_RADIUS        = GLASS_RADIUS    # line edits
BTN_RADIUS          = GLASS_RADIUS    # generic buttons
RUN_RADIUS          = GLASS_RADIUS    # run button

FIELD_PADDING_H     = 20    # field horizontal padding
FIELD_PADDING_V     = 16    # field vertical padding
BTN_MARGIN          = 0     # outer margin on fields
RUN_PADDING         = 16
BTN_PADDING         = 16

# backward-compat aliases
PILL_RADIUS         = RUN_RADIUS
PILL_PADDING        = RUN_PADDING


# --------------------------------------------------------------------------- #
# [1.5] COMPONENTS — per-widget details
# --------------------------------------------------------------------------- #

# ── close button (top-right ✕) ─────────────────────────────────────────────
CLOSE_BTN_SIZE      = 40
CLOSE_HOVER_BG      = rgba(_grey(0), 0.06)
CLOSE_HOVER_COLOR   = "#000000"
CLOSE_MARGIN        = 6

# ── dropdown popup ────────────────────────────────────────────────────────
POPUP_RADIUS        = GLASS_RADIUS            # 16px
POPUP_PADDING       = 6                       # inner padding of the list
POPUP_ITEM_HEIGHT   = 40                      # minimum row height
POPUP_ITEM_RADIUS   = 10
POPUP_SHOW_ALL      = True     # ★ never scroll: render every row, full height
POPUP_MAY_OVERLAP   = True     # ★ menu may cover the window box / field
POPUP_MAX_ITEMS     = 14       # cap used only when POPUP_SHOW_ALL = False
POPUP_MIN_WIDTH     = 140
POPUP_GAP           = 6                       # gap below the select box
POPUP_SCREEN_MARGIN = 8                       # keep off the screen edge
POPUP_EXTRA_PAD     = 2                       # safety px so no bar appears
POPUP_SEP_COLOR     = "#BFBFBF"               # grey dashed separator
POPUP_SEP_INSET     = 12
POPUP_SEP_DASH      = (3, 4)
POPUP_HILITE        = rgba(_grey(0), 0.07)    # grey row highlight
POPUP_SCROLL_HANDLE = rgba(_grey(0), 0.18)
POPUP_WHEEL_ON_COMBO = False   # False -> wheel over closed combo does nothing
CARET_GLYPH         = "▾"

# ── dialog ────────────────────────────────────────────────────────────────
DIALOG_RADIUS       = GLASS_RADIUS
DIALOG_BORDER       = 1
DIALOG_MIN_W        = 360
DIALOG_SYMBOL_SIZE  = 40
DIALOG_SYMBOLS      = {"info": "●", "warning": "▲", "error": "■"}

# ── toggle switch ─────────────────────────────────────────────────────────
TOGGLE_W            = 40
TOGGLE_H            = 22
TOGGLE_TRACK_OFF    = rgba(_grey(0), 0.10)

# ── progress bar ──────────────────────────────────────────────────────────
PROGRESS_TRACK      = rgba(_grey(0), 0.08)

# ── logo ──────────────────────────────────────────────────────────────────
LOGO_MAX_W          = 500
LOGO_MAX_H          = 800


# --------------------------------------------------------------------------- #
# [1.6] DERIVED COLOUR STRINGS — auto-built from [1.1]; no need to edit
# --------------------------------------------------------------------------- #

# card gradient (kept for API compat / non-painted fallbacks)
CARD_GRAD_TOP = rgba(CARD_TINT_RGB, CARD_ALPHA)
CARD_GRAD_MID = rgba(_shade(CARD_TINT_RGB, CARD_GRAD_SPREAD * 0.35), CARD_ALPHA)
CARD_GRAD_LOW = rgba(_shade(CARD_TINT_RGB, CARD_GRAD_SPREAD * 0.70), CARD_ALPHA)
CARD_GRAD_BOT = rgba(_shade(CARD_TINT_RGB, CARD_GRAD_SPREAD), CARD_ALPHA)
CARD_BORDER   = rgba(RIM_TINT_RGB, RIM_ALPHA)

# fields
GLASS_FILL       = rgba(FIELD_TINT_RGB, FIELD_ALPHA)
GLASS_FILL_HOVER = rgba(FIELD_TINT_RGB, FIELD_ALPHA_HOVER)
GLASS_BORDER     = rgba(BORDER_TINT_RGB, BORDER_ALPHA)
INPUT_BG         = GLASS_FILL
INPUT_BORDER     = GLASS_BORDER
INPUT_FOCUS_BORDER = rgba(BORDER_TINT_RGB, min(1.0, BORDER_ALPHA + 0.22))

# popup / dialog
POPUP_BG      = rgba(POPUP_TINT_RGB, POPUP_ALPHA)
POPUP_BORDER  = rgba(BORDER_TINT_RGB, POPUP_BORDER_ALPHA)
DIALOG_BG     = rgba(DIALOG_TINT_RGB, DIALOG_ALPHA)
DIALOG_BORDER_COLOR = rgba(BORDER_TINT_RGB, 0.10)

# text aliases used across the file
LABEL_INK       = TEXT_PRIMARY
FIELD_TEXT      = TEXT_SECONDARY
PLACEHOLDER_INK = TEXT_PLACEHOLDER
DIVIDER_INK     = rgba(_grey(0), 0.85)   # header hairline


# =========================================================================== #
# [2] BACKDROP BLUR — the `backdrop-filter: blur()` engine  (now multi-pass)
# =========================================================================== #

def _blur_once(pm: QPixmap, radius: float) -> QPixmap:
    """One QGraphicsBlurEffect pass over a pixmap."""
    if pm.isNull() or radius <= 0:
        return pm
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pm)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(float(radius))
    effect.setBlurHints(QGraphicsBlurEffect.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)

    img = QImage(pm.size(), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.drawPixmap(0, 0, pm)                       # base: keeps edges opaque
    scene.render(p, QRectF(img.rect()), QRectF(pm.rect()))
    p.end()
    return QPixmap.fromImage(img)


def _blur_pixmap(pm: QPixmap, radius: float, passes: int = 1) -> QPixmap:
    """Stacked gaussian passes -> much fuzzier, still cheap."""
    out = pm
    for _ in range(max(1, int(passes))):
        out = _blur_once(out, radius)
    return out


class _Backdrop:
    """Captures the desktop once, blurs it hard, and lets any widget paint
    the slice of blur that sits directly behind it. This is the Qt
    equivalent of CSS `backdrop-filter: blur(Npx)`.

    Failure (e.g. macOS screen-recording permission denied) is silent:
    surfaces simply fall back to tint + painted blooms.
    """

    _pm: QPixmap | None = None
    _origin: QPoint = QPoint(0, 0)
    _scale: float = 1.0

    # ---- capture ---------------------------------------------------------- #
    @classmethod
    def available(cls) -> bool:
        return bool(BACKDROP_ENABLED and cls._pm is not None
                    and not cls._pm.isNull())

    @classmethod
    def capture(cls, widget: QWidget | None = None,
                hide_window: bool = False) -> None:
        if not BACKDROP_ENABLED:
            cls._pm = None
            return
        app = QApplication.instance()
        if app is None:
            return

        screen = None
        if widget is not None:
            try:
                screen = widget.screen()          # Qt >= 5.14
            except Exception:
                screen = None
            if screen is None:
                try:
                    screen = QApplication.screenAt(
                        widget.mapToGlobal(QPoint(0, 0)))
                except Exception:
                    screen = None
        screen = screen or QApplication.primaryScreen()
        if screen is None:
            return

        win = widget.window() if widget is not None else None
        restore_opacity: float | None = None
        raw: QPixmap | None = None
        try:
            if hide_window and win is not None and win.isVisible():
                restore_opacity = win.windowOpacity()
                win.setWindowOpacity(0.0)
                app.processEvents()
                time.sleep(0.05)
                app.processEvents()
            geo = screen.geometry()
            raw = screen.grabWindow(0, geo.x(), geo.y(),
                                    geo.width(), geo.height())
            cls._origin = geo.topLeft()
        except Exception as exc:                          # pragma: no cover
            logger.debug("backdrop capture failed: %s", exc)
            raw = None
        finally:
            if restore_opacity is not None and win is not None:
                win.setWindowOpacity(restore_opacity)

        if raw is None or raw.isNull():
            cls._pm = None
            return

        down = max(1, int(BACKDROP_DOWNSCALE))
        small = raw
        if down > 1:
            small = raw.scaled(max(1, raw.width() // down),
                               max(1, raw.height() // down),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        blurred = _blur_pixmap(small, max(1.0, BACKDROP_BLUR / down),
                               BACKDROP_PASSES)
        blurred.setDevicePixelRatio(1.0)
        cls._pm = blurred
        logical_w = max(1, screen.geometry().width())
        cls._scale = blurred.width() / float(logical_w)

    # ---- paint ------------------------------------------------------------ #
    @classmethod
    def draw(cls, painter: QPainter, widget: QWidget) -> bool:
        """Paint the blurred desktop slice behind `widget`. True if drawn."""
        if not cls.available():
            return False
        pm = cls._pm
        assert pm is not None
        w, h = widget.width(), widget.height()
        if w <= 0 or h <= 0:
            return False
        try:
            gp = widget.mapToGlobal(QPoint(0, 0))
        except Exception:                                 # pragma: no cover
            return False
        s = cls._scale
        src = QRectF((gp.x() - cls._origin.x()) * s,
                     (gp.y() - cls._origin.y()) * s, w * s, h * s)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(QRectF(0, 0, w, h), pm, src)
        return True


# =========================================================================== #
# [3] GLASS SURFACE — rounded frosted panel (blur + tint + blooms + rim)
# =========================================================================== #

class _GlassSurface(QWidget):
    """A rounded, frosted panel.

    Paint order:  blurred backdrop -> tint gradient -> blooms -> sheen -> rim.
    Nothing here touches app logic; children paint on top.
    """

    def __init__(self, parent: QWidget | None = None, *,
                 radius: int = GLASS_RADIUS,
                 tint: tuple[int, int, int] = CARD_TINT_RGB,
                 alpha: float = CARD_ALPHA,
                 grad_spread: float = CARD_GRAD_SPREAD,
                 blooms: bool = False,
                 sheen: float = 0.0,
                 rim_rgb: tuple[int, int, int] = RIM_TINT_RGB,
                 rim_alpha: float = RIM_ALPHA,
                 inner_rim: bool = False,
                 use_backdrop: bool = True) -> None:
        super().__init__(parent)
        self._radius = int(radius)
        self._tint = tint
        self._alpha = float(alpha)
        self._spread = float(grad_spread)
        self._blooms = bool(blooms)
        self._sheen = float(sheen)
        self._rim_rgb = rim_rgb
        self._rim_alpha = float(rim_alpha)
        self._inner_rim = bool(inner_rim)
        self._use_backdrop = bool(use_backdrop)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    # ---- painting --------------------------------------------------------- #
    def paintEvent(self, event) -> None:  # noqa: D401
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        r = self._radius
        span = float(max(w, h))

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        outline = QPainterPath()
        outline.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), r, r)

        p.save()
        p.setClipPath(outline)
        p.setPen(Qt.NoPen)

        # 1) REAL blur (backdrop-filter)
        if self._use_backdrop:
            _Backdrop.draw(p, self)

        # 2) translucent tint on top of the blur
        tint = QLinearGradient(0.0, 0.0, w * 0.35, float(h))
        a = self._alpha
        tint.setColorAt(0.00, QColor(*self._tint, int(255 * a)))
        tint.setColorAt(0.35, QColor(*_shade(self._tint, self._spread * 0.35),
                                     int(255 * a)))
        tint.setColorAt(0.68, QColor(*_shade(self._tint, self._spread * 0.70),
                                     int(255 * a)))
        tint.setColorAt(1.00, QColor(*_shade(self._tint, self._spread),
                                     int(255 * a)))
        p.setBrush(tint)
        p.drawRect(0, 0, w, h)

        # 3) soft greyscale blooms (extra fuzz)
        if self._blooms and BLOOM_STRENGTH > 0:
            for cx, cy, rf, grey, alpha in GLASS_BLOBS:
                aa = max(0.0, min(1.0, alpha * BLOOM_STRENGTH))
                cr, cg, cb = _grey(grey)
                grad = QRadialGradient(QPointF(w * cx, h * cy), span * rf)
                grad.setColorAt(0.00, QColor(cr, cg, cb, int(255 * aa)))
                grad.setColorAt(0.45, QColor(cr, cg, cb, int(255 * aa * 0.45)))
                grad.setColorAt(0.75, QColor(cr, cg, cb, int(255 * aa * 0.14)))
                grad.setColorAt(1.00, QColor(cr, cg, cb, 0))
                p.setBrush(grad)
                p.drawRect(0, 0, w, h)

        # 4) diagonal white sheen -> glassy highlight
        if self._sheen > 0:
            sheen = QLinearGradient(0.0, 0.0, w * 0.85, float(h))
            sheen.setColorAt(0.00, QColor(255, 255, 255,
                                          int(255 * self._sheen)))
            sheen.setColorAt(0.38, QColor(255, 255, 255,
                                          int(255 * self._sheen * 0.30)))
            sheen.setColorAt(1.00, QColor(255, 255, 255, 0))
            p.setBrush(sheen)
            p.drawRect(0, 0, w, h)
        p.restore()

        # 5) rim hairlines
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(*self._rim_rgb, int(255 * self._rim_alpha)), 1))
        p.drawPath(outline)
        if self._inner_rim:
            p.setPen(QPen(QColor(*self._rim_rgb,
                                 int(255 * self._rim_alpha * 0.55)), 1))
            inner = QPainterPath()
            inner.addRoundedRect(QRectF(1.5, 1.5, w - 3.0, h - 3.0),
                                 max(1, r - 1), max(1, r - 1))
            p.drawPath(inner)
        p.end()


class _GlassCard(_GlassSurface):
    """The app card: real blur + white tint + blooms + sheen + bright rim."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent,
                         radius=WINDOW_RADIUS,
                         tint=CARD_TINT_RGB,
                         alpha=CARD_ALPHA,
                         grad_spread=CARD_GRAD_SPREAD,
                         blooms=True,
                         sheen=SHEEN_ALPHA,
                         rim_rgb=RIM_TINT_RGB,
                         rim_alpha=RIM_ALPHA,
                         inner_rim=True,
                         use_backdrop=True)
        # the card hosts real widgets -> it must receive mouse events
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)


# =========================================================================== #
# [4] THEME — stylesheet builders (values come only from section [1])
# =========================================================================== #

class Theme:
    def __init__(self, accent: str = ACCENT,
                 accent_hover: str = ACCENT_HOVER, ink: str = INK) -> None:
        self.accent = accent
        self.accent_hover = accent_hover
        self.ink = ink

    # ---- card + generic pickers + line edits ----------------------------- #
    def root(self) -> str:
        picker_weight = "bold" if PICKER_FONT_BOLD else "normal"
        input_weight = "bold" if INPUT_FONT_BOLD else "normal"
        return f"""
            /* the card itself is painted in _GlassCard.paintEvent
               (blur + tint + blooms), so QSS must stay transparent */
            QWidget#App {{
                background: transparent;
                border: none;
                border-radius: {WINDOW_RADIUS}px;
            }}
            QWidget {{
                font-family: {FONT_FAMILY};
                background-color: transparent;
                color: {TEXT_PRIMARY};
                border: none;
            }}
            /* glass folder / file pickers */
            QPushButton {{
                background-color: {GLASS_FILL};
                color: {TEXT_SECONDARY};
                font-weight: {picker_weight};
                font-size: {PICKER_FONT_SIZE}px;
                border: 1px solid {GLASS_BORDER};
                border-radius: {FIELD_RADIUS}px;
                padding: {FIELD_PADDING_V}px {FIELD_PADDING_H}px;
                margin: {BTN_MARGIN}px;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: {GLASS_FILL_HOVER};
            }}
            /* glass value boxes */
            QLineEdit {{
                border: 1px solid {INPUT_BORDER};
                border-radius: {INPUT_RADIUS}px;
                padding: {FIELD_PADDING_V}px {FIELD_PADDING_H}px;
                margin: {BTN_MARGIN}px;
                background-color: {INPUT_BG};
                color: {TEXT_PRIMARY};
                font-weight: {input_weight};
                font-size: {INPUT_FONT_SIZE}px;
            }}
            QLineEdit:focus {{
                border: 1px solid {INPUT_FOCUS_BORDER};
                background-color: {GLASS_FILL_HOVER};
            }}
        """

    # ---- select container: caption + combo + caret ----------------------- #
    def select_pill(self) -> str:
        weight = "bold" if SELECT_FONT_BOLD else "normal"
        label_size = max(12, round(PICKER_FONT_SIZE * SELECT_LABEL_DIM))
        return f"""
            QWidget#SelectPill {{
                background-color: {GLASS_FILL};
                border: 1px solid {GLASS_BORDER};
                border-radius: {FIELD_RADIUS}px;
                margin: {BTN_MARGIN}px;
            }}
            QWidget#SelectPill:hover {{
                background-color: {GLASS_FILL_HOVER};
            }}
            QWidget#SelectPill QLabel#SelectCaption {{
                background: transparent;
                border: none;
                color: {TEXT_SECONDARY};
                font-size: {label_size}px;
                font-weight: {weight};
            }}
            QWidget#SelectPill QLabel#SelectCaret {{
                background: transparent;
                border: none;
                color: {TEXT_SECONDARY};
                font-size: {CARET_SIZE}px;
            }}
            QWidget#SelectPill QComboBox {{
                background: transparent;
                border: none;
                color: {TEXT_PRIMARY};
                font-size: {SELECT_FONT_SIZE}px;
                font-weight: {weight};
                padding: 6px 4px;
                /* ★ list-mode popup: kills the native up/down scroller
                   arrow strips (QComboBoxPrivateScroller) completely */
                combobox-popup: 0;
            }}
            /* native arrow hidden — we draw our own caret label */
            QWidget#SelectPill QComboBox::drop-down {{
                border: none;
                width: 0px;
            }}
            QWidget#SelectPill QComboBox::down-arrow {{
                image: none;
                width: 0px;
                height: 0px;
            }}
        """

    # ---- dropdown popup -------------------------------------------------- #
    # NOTE: background + 16px rounded border are painted by the
    # _GlassSurface underlay, so the view must be fully transparent.
    def popup(self) -> str:
        return f"""
            QListView {{
                background: transparent;
                border: none;
                border-radius: {POPUP_RADIUS}px;
                padding: {POPUP_PADDING}px;
                color: {TEXT_PRIMARY};
                font-family: {FONT_FAMILY};
                font-size: {POPUP_FONT_SIZE}px;
                outline: 0;
                show-decoration-selected: 0;
            }}
            QListView::viewport {{
                background: transparent;
            }}
            QListView::item {{
                min-height: {POPUP_ITEM_HEIGHT}px;
                border: none;
                border-radius: {POPUP_ITEM_RADIUS}px;
                padding-left: 14px;
                padding-right: 14px;
                color: {TEXT_PRIMARY};
            }}
            QListView::item:selected,
            QListView::item:hover {{
                background-color: {POPUP_HILITE};
                color: {TEXT_PRIMARY};
            }}
            /* a slim bar exists only if the list is taller than the screen */
            QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: {POPUP_PADDING}px 2px {POPUP_PADDING}px 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {POPUP_SCROLL_HANDLE};
                border-radius: 3px;
                min-height: 24px;
            }}
            /* ★ no arrow buttons anywhere */
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
                height: 0px;
                width: 0px;
                border: none;
            }}
            QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical {{
                background: transparent;
                image: none;
                height: 0px;
                width: 0px;
            }}
        """

    # ---- solid black primary run button --------------------------------- #
    def cooking(self) -> str:
        weight = "bold" if RUN_FONT_BOLD else "normal"
        return f"""
            QPushButton#RunButton {{
                font-size: {RUN_FONT_SIZE}px;
                font-weight: {weight};
                letter-spacing: {RUN_LETTER_SPACING}px;
                color: {TEXT_ON_INK};
                background-color: {self.ink};
                border: 1px solid {self.ink};
                border-radius: {RUN_RADIUS}px;
                padding: 0px;
                margin: 0px {BTN_MARGIN}px;
                text-align: center;
            }}
            QPushButton#RunButton:hover {{
                background-color: {INK_HOVER};
                border-color: {INK_HOVER};
            }}
            QPushButton#RunButton:pressed {{
                background-color: {INK};
            }}
            QPushButton#RunButton:disabled {{
                background-color: {rgba(FIELD_TINT_RGB, 0.45)};
                border-color: {GLASS_BORDER};
                color: {TEXT_DISABLED};
            }}
        """

    def progress(self) -> str:
        return f"""
            QProgressBar {{
                border: none;
                border-radius: {PROGRESS_HEIGHT // 2}px;
                background: {PROGRESS_TRACK};
                height: {PROGRESS_HEIGHT}px;
                margin: {BTN_MARGIN}px;
            }}
            QProgressBar::chunk {{
                background: {self.ink};
                border-radius: {PROGRESS_HEIGHT // 2}px;
            }}
        """

    # ---- dialog: white glass card, black text, black OK ----------------- #
    def message_box(self) -> str:
        return f"""
            QWidget#Dialog {{
                background-color: {DIALOG_BG};
                border: {DIALOG_BORDER}px solid {DIALOG_BORDER_COLOR};
                border-radius: {DIALOG_RADIUS}px;
            }}
            QWidget#Dialog QLabel {{
                background: transparent;
                border: none;
                color: {TEXT_PRIMARY};
            }}
            QWidget#Dialog QPushButton {{
                background-color: {INK};
                color: {TEXT_ON_INK};
                border: none;
                border-radius: {INPUT_RADIUS}px;
                padding: 12px 34px;
                margin: 0px;
                font-size: {DIALOG_FONT_SIZE}px;
                font-weight: bold;
            }}
            QWidget#Dialog QPushButton:hover {{
                background-color: {INK_HOVER};
                color: {TEXT_ON_INK};
            }}
        """


# =========================================================================== #
# [5] DROPDOWN — floats over the whole window, always full height
# =========================================================================== #

class _DashedItemDelegate(QStyledItemDelegate):
    """Paints a grey dashed separator under every row except the last."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(POPUP_ITEM_HEIGHT, size.height()))
        return size

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        model = index.model()
        if model is None or index.row() >= model.rowCount() - 1:
            return
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        pen = QPen(QColor(POPUP_SEP_COLOR))
        pen.setWidth(1)
        pen.setStyle(Qt.CustomDashLine)
        pen.setDashPattern(list(POPUP_SEP_DASH))
        painter.setPen(pen)
        y = rect.bottom()
        painter.drawLine(rect.left() + POPUP_SEP_INSET, y,
                         rect.right() - POPUP_SEP_INSET, y)
        painter.restore()


class _GlassComboBox(QComboBox):
    """QComboBox whose popup:
       * is a borderless top-level Qt.Popup window -> it can float OVER the
         whole app card and hang outside the window box in any direction
       * shows EVERY item at its true height: no clipping, no scrollbar,
         no native up/down scroller strips
       * is a 16px rounded frosted panel with one hairline border
       * opens flush beneath its anchor with the SAME width; flips above,
         or overlaps the window, whenever there is not enough room below
    """

    def __init__(self, parent: QWidget | None = None,
                 anchor: QWidget | None = None,
                 popup_qss: str = "") -> None:
        super().__init__(parent)
        self._anchor = anchor

        view = QListView()
        view.setFrameShape(QFrame.NoFrame)
        view.setFrameShadow(QFrame.Plain)
        view.setLineWidth(0)
        view.setMidLineWidth(0)
        view.setUniformItemSizes(False)          # measure each row for real
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setSelectionMode(QAbstractItemView.SingleSelection)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        view.setResizeMode(QListView.Adjust)
        view.setMouseTracking(True)
        view.setAttribute(Qt.WA_Hover, True)
        view.setItemDelegate(_DashedItemDelegate(view))
        view.setStyleSheet(popup_qss)
        view.viewport().setAutoFillBackground(False)
        view.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self.setView(view)

        # ask for "all rows visible" up-front so Qt installs no height cap
        self.setMaxVisibleItems(max(1, POPUP_MAX_ITEMS))

        # frameless + translucent popup window so rounded corners show and
        # the menu is free of the parent window's geometry
        popup = view.window()
        popup.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint
                             | Qt.NoDropShadowWindowHint)
        popup.setAttribute(Qt.WA_TranslucentBackground, True)
        popup.setAttribute(Qt.WA_NoSystemBackground, True)
        self._popup = popup

        container = view.parentWidget() or popup   # QComboBoxPrivateContainer
        self._container = container
        if container is not None:
            container.setContentsMargins(0, 0, 0, 0)
            container.setStyleSheet(
                "QFrame { background: transparent; border: none; margin: 0px; }")
            lay = container.layout()
            if lay is not None:
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(0)
            container.installEventFilter(self)

        # frosted 16px panel painted BEHIND the (transparent) list view
        self._underlay = _GlassSurface(
            container,
            radius=POPUP_RADIUS,
            tint=POPUP_TINT_RGB,
            alpha=POPUP_ALPHA,
            grad_spread=0.02,
            blooms=False,
            sheen=POPUP_SHEEN_ALPHA,
            rim_rgb=BORDER_TINT_RGB,
            rim_alpha=POPUP_BORDER_ALPHA,
            use_backdrop=POPUP_USE_BACKDROP,
        )
        self._underlay.lower()

    # ---- housekeeping ----------------------------------------------------- #
    def eventFilter(self, obj, event):
        """Keep the frosted underlay glued to the container at all times."""
        if obj is self._container and event.type() in (
                QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self._sync_underlay()
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:
        """Wheel over the CLOSED combo must not silently change the value."""
        if POPUP_WHEEL_ON_COMBO:
            super().wheelEvent(event)
        else:
            event.ignore()

    def _sync_underlay(self) -> None:
        if self._underlay is None or self._container is None:
            return
        self._underlay.setGeometry(0, 0, self._container.width(),
                                   self._container.height())
        self._underlay.lower()
        self._underlay.update()

    @staticmethod
    def _screen_rect(widget: QWidget) -> QRect:
        try:
            scr = QApplication.screenAt(widget.mapToGlobal(QPoint(0, 0)))
            if scr is not None:
                return scr.availableGeometry()
        except Exception:
            pass
        try:
            return QApplication.desktop().availableGeometry(widget)
        except Exception:                                  # pragma: no cover
            return QRect(0, 0, 1920, 1080)

    def _unlock(self, *widgets: QWidget | None) -> None:
        """Strip every min/max size constraint Qt may have installed."""
        for wdg in widgets:
            if wdg is None:
                continue
            wdg.setMinimumHeight(0)
            wdg.setMaximumHeight(QWIDGETSIZE_MAX)
            wdg.setMinimumWidth(0)
            wdg.setMaximumWidth(QWIDGETSIZE_MAX)

    def _kill_scrollers(self) -> None:
        """Hide Qt's native popup scroller strips (the up/down switches)."""
        if self._container is None:
            return
        for child in self._container.children():
            if not isinstance(child, QWidget):
                continue
            name = child.metaObject().className()
            if "Scroller" in name:
                child.setFixedHeight(0)
                child.hide()

    def _content_height(self, rows: int) -> int:
        """Exact pixel height needed to show `rows` rows completely."""
        view = self.view()
        total = 0
        for i in range(rows):
            h = POPUP_ITEM_HEIGHT
            try:
                h = max(h, int(view.sizeHintForRow(i)))
            except Exception:
                pass
            try:
                idx = self.model().index(i, self.modelColumn())
                hint = view.itemDelegate().sizeHint(
                    view.viewOptions(), idx).height()
                h = max(h, int(hint))
            except Exception:
                pass
            total += max(1, h)
        spacing = max(0, int(view.spacing())) * 2 * max(0, rows - 1)
        frame = POPUP_PADDING * 2 + 2 + POPUP_EXTRA_PAD
        return total + spacing + frame

    # ---- geometry: whole menu, same width, floating over the window ------- #
    def showPopup(self) -> None:
        n = max(1, self.count())
        wanted = n if POPUP_SHOW_ALL else min(n, POPUP_MAX_ITEMS)
        if self.maxVisibleItems() < wanted:
            self.setMaxVisibleItems(wanted)      # removes Qt's height cap

        super().showPopup()
        self._place_popup()
        # some styles re-layout the container right after show -> re-apply
        QTimer.singleShot(0, self._place_popup)

    def _place_popup(self) -> None:
        view = self.view()
        popup = view.window()
        container = view.parentWidget() or popup
        anchor = self._anchor if self._anchor is not None else self
        if popup is None or not popup.isVisible():
            return

        self._unlock(popup, container, view)
        self._kill_scrollers()

        n = max(1, self.count())
        rows = n if POPUP_SHOW_ALL else min(n, POPUP_MAX_ITEMS)
        full_h = self._content_height(rows)

        screen = self._screen_rect(anchor)
        margin = POPUP_SCREEN_MARGIN
        avail_h = max(POPUP_ITEM_HEIGHT + POPUP_PADDING * 2,
                      screen.height() - 2 * margin)

        # the ONLY situation where we shrink: taller than the whole screen
        height = full_h
        clipped = False
        if height > avail_h:
            height = avail_h
            clipped = True

        width = max(anchor.width(), POPUP_MIN_WIDTH)
        gp = anchor.mapToGlobal(QPoint(0, 0))

        y_below = gp.y() + anchor.height() + POPUP_GAP
        y_above = gp.y() - POPUP_GAP - height

        if y_below + height <= screen.bottom() - margin:
            y = y_below                                  # 1) below (default)
        elif y_above >= screen.top() + margin:
            y = y_above                                  # 2) flip above
        elif POPUP_MAY_OVERLAP:
            # 3) keep FULL height and just slide it into the screen —
            #    it is allowed to cover the field / the whole window box
            y = screen.bottom() - margin - height
            y = max(screen.top() + margin, y)
        else:
            y = min(y_below, screen.bottom() - margin - height)
            y = max(screen.top() + margin, y)

        x = min(max(gp.x(), screen.left() + margin),
                max(screen.left() + margin, screen.right() - margin - width))

        # scrollbar exists only when physically unavoidable
        view.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if clipped else Qt.ScrollBarAlwaysOff)

        popup.setGeometry(int(x), int(y), int(width), int(height))
        if container is not popup:
            container.setGeometry(0, 0, int(width), int(height))
        view.setGeometry(0, 0, int(width), int(height))
        view.setFixedHeight(int(height))         # nothing may shrink it back

        self._sync_underlay()
        if self._underlay is not None:
            self._underlay.show()

        # never start scrolled: the first row must be visible
        if not clipped:
            view.scrollToTop()
            bar = view.verticalScrollBar()
            if bar is not None:
                bar.setValue(bar.minimum())
        else:
            idx = self.model().index(self.currentIndex(), self.modelColumn())
            if idx.isValid():
                view.scrollTo(idx, QAbstractItemView.EnsureVisible)

    def hidePopup(self) -> None:
        view = self.view()
        try:
            view.setMinimumHeight(0)
            view.setMaximumHeight(QWIDGETSIZE_MAX)
        except Exception:
            pass
        super().hidePopup()


# =========================================================================== #
# [6] CLOSE BUTTON — minimal ✕
# =========================================================================== #

class CloseButton(QPushButton):
    def __init__(self, parent: QWidget, theme: Theme,
                 size: int = CLOSE_BTN_SIZE,
                 font_size: int = CLOSE_FONT_SIZE) -> None:
        super().__init__("✕", parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        weight = "bold" if CLOSE_FONT_BOLD else "normal"
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {TEXT_PRIMARY};
                border: none;
                border-radius: {size // 2}px;
                font-weight: {weight};
                font-size: {font_size}px;
                margin: {CLOSE_MARGIN}px;
                padding: 0px;
                text-align: center;
            }}
            QPushButton:hover {{
                background-color: {CLOSE_HOVER_BG};
                color: {CLOSE_HOVER_COLOR};
            }}
        """)
        self.clicked.connect(parent.close)


# =========================================================================== #
# [7] LOGO RESOLUTION — robust cover image search
# =========================================================================== #

_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp")


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS          # PyInstaller temp dir
    return os.path.dirname(os.path.abspath(sys.argv[0] or __file__))


def find_logo(hint: str | None) -> str | None:
    """Resolve a logo image: explicit path, else search static/assets dirs
    for anything matching 'cover' / 'logo'."""
    if hint and os.path.exists(hint):
        return hint
    root = _app_root()
    search_dirs = [os.path.join(root, d)
                   for d in ("static", "assets", "resources", "")]
    patterns = ([Path(hint).stem] if hint else []) + ["cover", "logo"]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for pat in patterns:
            for ext in _IMG_EXT:
                p = os.path.join(d, f"{pat}{ext}")
                if os.path.exists(p):
                    return p
    return None


# =========================================================================== #
# [8] WORKER THREAD — runs the wrapped function off the UI thread
# =========================================================================== #

class _Worker(QThread):
    progressed = pyqtSignal(int)
    logged = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable, kwargs: dict[str, Any]) -> None:
        super().__init__()
        self.fn = fn
        self.kwargs = kwargs
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            sig = inspect.signature(self.fn)
            kwargs = dict(self.kwargs)
            if "progress_callback" in sig.parameters:
                kwargs["progress_callback"] = self.progressed.emit
            if "log_callback" in sig.parameters:
                kwargs["log_callback"] = self.logged.emit
            if "cancel_check" in sig.parameters:
                kwargs["cancel_check"] = lambda: self._cancelled
            self.finished_ok.emit(self.fn(**kwargs))
        except Exception:
            self.failed.emit(traceback.format_exc())


# =========================================================================== #
# [9] PARAM WIDGETS — one config entry -> one widget
# =========================================================================== #

class _ParamRow:
    """One config entry -> a single widget."""

    def __init__(self, spec: dict, theme: Theme, parent: QWidget) -> None:
        self.spec = spec
        self.name: str = spec["name"]
        self.type: str = spec["type"]
        self.required: bool = bool(spec.get("required", False))
        self.label_text: str = spec.get(
            "label", self.name.replace("_", " ").title())

        builder = getattr(self, f"_build_{self.type}", None)
        if builder is None:
            raise ValueError(
                f"unknown param type '{self.type}' for '{self.name}'")
        self.widget: QWidget = builder(spec, theme, parent)
        if spec.get("tooltip"):
            self.widget.setToolTip(spec["tooltip"])

    # ---- builders --------------------------------------------------------- #

    def _build_select(self, spec, theme, parent) -> QWidget:
        container = QWidget(parent)
        container.setObjectName("SelectPill")
        container.setStyleSheet(theme.select_pill())
        container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        container.setFixedHeight(BTN_HEIGHT)

        row = QHBoxLayout(container)
        row.setContentsMargins(FIELD_PADDING_H, 0, 16, 0)
        row.setSpacing(10)

        caption = QLabel(self.label_text, container)
        caption.setObjectName("SelectCaption")
        row.addWidget(caption)

        # popup anchored to the pill -> same width, opens right beneath it
        combo = _GlassComboBox(container, anchor=container,
                               popup_qss=theme.popup())
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        combo.setCursor(Qt.PointingHandCursor)
        self._values = list(spec.get("options", []))
        combo.addItems([str(o) for o in self._values])
        if "default" in spec and spec["default"] in self._values:
            combo.setCurrentIndex(self._values.index(spec["default"]))

        row.addWidget(combo, 1)

        caret = QLabel(CARET_GLYPH, container)
        caret.setObjectName("SelectCaret")
        caret.setAlignment(Qt.AlignCenter)
        caret.setFixedWidth(14)
        row.addWidget(caret)

        self.get = lambda: (self._values[combo.currentIndex()]
                            if self._values else None)
        self._signal = combo.currentIndexChanged
        return container

    def _build_toggle(self, spec, theme, parent) -> QWidget:
        w = QCheckBox(self.label_text, parent)
        w.setChecked(bool(spec.get("default", False)))
        w.setCursor(Qt.PointingHandCursor)
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        w.setFixedHeight(BTN_HEIGHT)
        weight = "bold" if PILL_FONT_BOLD else "normal"
        w.setStyleSheet(f"""
            QCheckBox {{
                font-size: {PILL_FONT_SIZE}px;
                font-weight: {weight};
                color: {TEXT_PRIMARY};
                background-color: {GLASS_FILL};
                border: 1px solid {GLASS_BORDER};
                border-radius: {FIELD_RADIUS}px;
                padding: 0px {FIELD_PADDING_H}px;
                margin: {BTN_MARGIN}px;
                spacing: 12px;
            }}
            QCheckBox:hover {{
                background-color: {GLASS_FILL_HOVER};
            }}
            QCheckBox::indicator {{
                width: {TOGGLE_W}px;
                height: {TOGGLE_H}px;
                border-radius: {TOGGLE_H // 2}px;
                background-color: {TOGGLE_TRACK_OFF};
                border: 1px solid {GLASS_BORDER};
            }}
            QCheckBox::indicator:checked {{
                background-color: {theme.ink};
                border: 1px solid {theme.ink};
            }}
        """)
        self.get = w.isChecked
        self._signal = w.stateChanged
        return w

    def _build_input(self, spec, theme, parent) -> QWidget:
        w = QLineEdit(parent)
        w.setFixedHeight(BTN_HEIGHT)
        w.setPlaceholderText(spec.get("placeholder", self.label_text))
        w.setText(str(spec.get("default", "")))
        self.get = w.text
        self._signal = w.textChanged
        return w

    def _build_password(self, spec, theme, parent) -> QWidget:
        w = self._build_input(spec, theme, parent)
        w.setEchoMode(QLineEdit.Password)
        return w

    def _build_int(self, spec, theme, parent) -> QWidget:
        w = self._build_input(spec, theme, parent)
        self.get = lambda: int(w.text() or 0)
        return w

    def _build_float(self, spec, theme, parent) -> QWidget:
        w = self._build_input(spec, theme, parent)
        self.get = lambda: float(w.text() or 0.0)
        return w

    def _build_folder(self, spec, theme, parent) -> QWidget:
        return self._picker(spec, theme, parent, mode="folder")

    def _build_file(self, spec, theme, parent) -> QWidget:
        return self._picker(spec, theme, parent, mode="file")

    def _build_save_file(self, spec, theme, parent) -> QWidget:
        return self._picker(spec, theme, parent, mode="save")

    def _picker(self, spec, theme, parent, mode: str) -> QWidget:
        btn = QPushButton(self.label_text, parent)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(BTN_HEIGHT)
        self._path = str(spec.get("default", ""))
        if self._path:
            btn.setText(Path(self._path).name)

        def pick() -> None:
            if mode == "folder":
                p = QFileDialog.getExistingDirectory(
                    parent, "Select Folder", self._path or str(Path.home()))
            elif mode == "file":
                p, _ = QFileDialog.getOpenFileName(
                    parent, "Select File", self._path or str(Path.home()),
                    spec.get("filter", "All files (*.*)"))
            else:
                p, _ = QFileDialog.getSaveFileName(
                    parent, "Save As", self._path or str(Path.home()),
                    spec.get("filter", "All files (*.*)"))
            if p:
                self._path = p
                btn.setText(f"{self.label_text}  ·  {Path(p).name}")
                if spec.get("auto_git_init"):
                    self._auto_git_init(p)

        btn.clicked.connect(pick)
        self.get = lambda: self._path
        self._signal = None
        return btn

    def _auto_git_init(self, path: str) -> None:
        """Run git init + .gitignore on the freshly-selected folder."""
        try:
            from src.assets.github_update import auto_prepare_git
            info = auto_prepare_git(path)
            action = (
                "Initialized new git repo"
                if info.get("initialized")
                else "Git repo already present"
            )
            if info.get("gitignore_created_or_updated"):
                action += " (updated .gitignore)"
            logger.info("[auto_git_init] %s — %s", action, info.get("folder"))
        except Exception as e:
            logger.warning("[auto_git_init] skipped: %s", e)
            QMessageBox.warning(
                self.widget, "Git",
                f"Could not auto-initialize git:\n{e}",
            )

    # ---- validation / conditional visibility ------------------------------ #

    def validate(self) -> str | None:
        if self.required:
            v = self.get()
            if v is None or (isinstance(v, str) and not v.strip()):
                return (f"Please select {self.label_text.lower()} "
                        f"before starting the process.")
        return None

    def apply_visibility(self, values: dict[str, Any]) -> None:
        cond = self.spec.get("visible_when")
        if not cond:
            return
        self.widget.setVisible(values.get(cond.get("name"))
                               == cond.get("equals"))


# =========================================================================== #
# [10] MAIN SHELL — window layout + run flow + frameless drag
# =========================================================================== #

class FunctionShell(QWidget):
    """Config-driven app window wrapping a single function."""

    def __init__(self, fn: Callable,
                 config: dict | str | os.PathLike) -> None:
        super().__init__()
        self.fn = fn
        self.config = load_config(config)
        app_cfg = self.config.get("app", {})
        self.theme = Theme(
            accent=app_cfg.get("accent", ACCENT),
            accent_hover=app_cfg.get("accent_hover", ACCENT_HOVER))
        self.rows: list[_ParamRow] = []
        self.worker: _Worker | None = None
        self.oldPos = self.pos()
        self._dragging = False
        self._build_ui()
        self.setMouseTracking(True)
        # grab the desktop while we are still invisible -> clean frosted glass
        _Backdrop.capture(self, hide_window=False)

    # ---- backdrop --------------------------------------------------------- #
    def refresh_backdrop(self) -> None:
        """Re-grab + re-blur the desktop behind the window."""
        _Backdrop.capture(self, hide_window=True)
        self.card.update()

    def _build_ui(self) -> None:
        app_cfg = self.config.get("app", {})
        run_cfg = self.config.get("run", {})

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # mono frosted-glass card (real blur + tint + blooms)
        self.card = _GlassCard(self)
        self.card.setObjectName("App")
        self.card.setStyleSheet(self.theme.root() + self.theme.select_pill())
        outer.addWidget(self.card)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(*CARD_MARGIN)
        layout.setSpacing(ROW_GAP)

        # -- title bar: close button only -- #
        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(
            CloseButton(
                self, self.theme,
                size=int(app_cfg.get("close_size", CLOSE_BTN_SIZE)),
                font_size=int(app_cfg.get("close_font_size",
                                          CLOSE_FONT_SIZE))),
            alignment=Qt.AlignRight)
        layout.addLayout(bar)

        # -- header: title (+ optional subtitle) + hairline divider -- #
        layout.addWidget(self._header(app_cfg))

        # -- params: strictly in config order -- #
        for spec in self.config.get("params", []):
            row = _ParamRow(spec, self.theme, self)
            self.rows.append(row)
            layout.addWidget(row.widget)

        layout.addSpacing(4)

        # -- progress bar (hidden until run) -- #
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(self.theme.progress())
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        # -- run button: solid black primary -- #
        run_btn = QPushButton(run_cfg.get("label", "Start Cooking"), self)
        run_btn.setObjectName("RunButton")
        run_btn.setStyleSheet(self.theme.cooking())
        run_btn.setFixedHeight(RUN_HEIGHT)
        run_btn.setCursor(Qt.PointingHandCursor)
        run_btn.clicked.connect(self._on_run)
        self.run_btn = run_btn
        layout.addWidget(run_btn)

        # ★ width from config, height fully automatic and UNCAPPED — the
        #   dropdown is a separate window, so nothing needs extra room here
        size_cfg = app_cfg.get("size", [540])
        w = size_cfg[0] if isinstance(size_cfg, (list, tuple)) else size_cfg
        self.setFixedWidth(int(w))
        self.setMinimumHeight(0)
        self.setMaximumHeight(QWIDGETSIZE_MAX)
        self.adjustSize()

        self._refresh_visibility()
        for row in self.rows:
            if getattr(row, "_signal", None) is not None:
                row._signal.connect(self._refresh_visibility)

    def _header(self, app_cfg: dict) -> QWidget:
        """Left-aligned text header — large bold title + subtitle,
        with a thin dark divider line underneath."""
        box = QWidget(self)
        box.setObjectName("Header")
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        box.setStyleSheet(f"""
            QWidget#Header {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {DIVIDER_INK};
            }}
        """)
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 12)   # top = 0 -> title moved up
        col.setSpacing(2)

        title = QLabel(app_cfg.get("title", "App"), box)
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title.setStyleSheet(f"""
            QLabel {{
                background: transparent;
                border: none;
                color: {TEXT_PRIMARY};
                font-size: {TITLE_FONT_SIZE}px;
                font-weight: {TITLE_FONT_WEIGHT};
            }}""")
        col.addWidget(title)

        subtitle_text = app_cfg.get("subtitle")
        if subtitle_text:
            sub = QLabel(str(subtitle_text), box)
            sub.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            sub.setStyleSheet(f"""
                QLabel {{
                    background: transparent;
                    border: none;
                    color: {TEXT_SUBTITLE};
                    font-size: {SUBTITLE_FONT_SIZE}px;
                    font-weight: {SUBTITLE_FONT_WEIGHT};
                }}""")
            col.addWidget(sub)

        return box

    # ---- value collection / validation ------------------------------- #

    def values(self) -> dict[str, Any]:
        return {row.name: row.get() for row in self.rows
                if row.widget.isVisible() or not self.isVisible()}

    def _refresh_visibility(self, *_a) -> None:
        vals = {row.name: row.get() for row in self.rows}
        for row in self.rows:
            row.apply_visibility(vals)

    def _validate(self) -> bool:
        for row in self.rows:
            if not row.widget.isVisible() and self.isVisible():
                continue
            err = row.validate()
            if err:
                self._message(QMessageBox.Warning, "Input Error", err)
                return False
        return True

    # ---- run flow ------------------------------------------------------ #

    def _on_run(self) -> None:
        if self.worker is not None:
            return
        if not self._validate():
            return
        kwargs = self.values()
        logger.info("running %s with %s", self.fn.__name__, kwargs)

        self.run_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.show()

        self.worker = _Worker(self.fn, kwargs)
        self.worker.progressed.connect(self.progress_bar.setValue)
        self.worker.logged.connect(lambda s: logger.info("%s", s))
        self.worker.finished_ok.connect(self._on_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_done(self, result: Any) -> None:
        self.progress_bar.hide()
        self.run_btn.setEnabled(True)
        self.worker = None
        text = "The workflow has been completed successfully."
        if isinstance(result, str) and result:
            text += f"\n\n{result}"
        self._message(QMessageBox.Information, "Success", text)

    def _on_fail(self, tb: str) -> None:
        logger.error("worker failed:\n%s", tb)
        self.progress_bar.hide()
        self.run_btn.setEnabled(True)
        self.worker = None
        self._message(QMessageBox.Warning, "Error",
                      tb.strip().splitlines()[-1])

    # ---- minimal glass dialog ----------------------------------------- #

    def _message(self, icon, title: str, text: str) -> None:
        kind = {QMessageBox.Information: "info",
                QMessageBox.Warning: "warning",
                QMessageBox.Critical: "error"}.get(icon, "info")

        dlg = QDialog(self)
        dlg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        dlg.setAttribute(Qt.WA_TranslucentBackground)
        dlg.setMinimumWidth(DIALOG_MIN_W)

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget(dlg)
        card.setObjectName("Dialog")
        card.setStyleSheet(self.theme.message_box())
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(32, 32, 32, 28)
        lay.setSpacing(18)

        symbol = QLabel(DIALOG_SYMBOLS.get(kind, "●"), card)
        symbol.setAlignment(Qt.AlignCenter)
        symbol.setStyleSheet(
            f"font-size: {DIALOG_SYMBOL_SIZE}px;"
            f"color: {TEXT_PRIMARY};")
        lay.addWidget(symbol)

        body = QLabel(text, card)
        body.setAlignment(Qt.AlignCenter)
        body.setWordWrap(True)
        body.setStyleSheet(
            f"font-size: {DIALOG_FONT_SIZE}px;"
            f"color: {TEXT_PRIMARY};")
        lay.addWidget(body)

        ok = QPushButton("OK", card)
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok, alignment=Qt.AlignCenter)

        dlg.exec_()

    # ---- frameless drag -------------------------------------------------- #

    _INTERACTIVE_TYPES = (QAbstractButton, QComboBox, QLineEdit,
                          QAbstractSlider, QAbstractItemView, QProgressBar)

    def _is_interactive(self, widget) -> bool:
        w = widget
        while w is not None and w is not self:
            if isinstance(w, self._INTERACTIVE_TYPES):
                return True
            w = w.parentWidget()
        return False

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            child = self.childAt(event.pos())
            if not self._is_interactive(child):
                self._dragging = True
                self.oldPos = event.globalPos()
            else:
                self._dragging = False

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and event.buttons() == Qt.LeftButton:
            delta = QPoint(event.globalPos() - self.oldPos)
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.oldPos = event.globalPos()
            self.card.update()          # blur slice follows the window

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            was_dragging = self._dragging
            self._dragging = False
            if was_dragging and BACKDROP_ON_MOVE:
                QTimer.singleShot(0, self.refresh_backdrop)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if hasattr(self, "card"):
            self.card.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "card"):
            self.card.update()

    def closeEvent(self, event) -> None:
        if self.worker:
            self.worker.cancel()
            self.worker.wait(2000)
        event.accept()


# =========================================================================== #
# [11] HELPERS — load_config / run_shell
# =========================================================================== #

def load_config(config: dict | str | os.PathLike) -> dict:
    if isinstance(config, dict):
        cfg = config
    else:
        text = str(config)
        if text.lstrip().startswith("{"):
            cfg = json.loads(text)
        else:
            cfg = json.loads(Path(text).read_text(encoding="utf-8"))
    if "params" not in cfg:
        raise ValueError("config must contain a 'params' list")
    seen: set[str] = set()
    for p in cfg["params"]:
        if "name" not in p or "type" not in p:
            raise ValueError(f"param missing name/type: {p}")
        if p["name"] in seen:
            raise ValueError(f"duplicate param name: {p['name']}")
        seen.add(p["name"])
    return cfg


def run_shell(fn: Callable, config: dict | str | os.PathLike) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    shell = FunctionShell(fn, config)
    shell.show()
    return app.exec_()


# =========================================================================== #
# [12] SELF-DEMO — python ui_style_shell.py
# =========================================================================== #

if __name__ == "__main__":

    def demo_task(folder: str, out_root: str, size, fmt: str,
                  progress_callback=None) -> str:
        for i in range(1, 11):
            time.sleep(0.08)
            if progress_callback:
                progress_callback(i * 10)
        return f"{folder or '-'} -> {out_root or '(in place)'} "\
               f"@ {size} KB -> {fmt}"

    DEMO_CONFIG = {
        "app": {
            "title": "Tiny Image",
            "subtitle": "Smart Image Compression",
            "size": [520],            # height is automatic now
        },
        "params": [
            {"name": "folder", "type": "folder",
             "label": "Select image folder...", "required": True},
            {"name": "out_root", "type": "folder",
             "label": "Output root folder (optional)..."},
            {"name": "size", "type": "select", "label": "Target size (KB)",
             "options": [100, 200, 300, 400, 500, 600, 700, 800, 900,
                         1000, 1200, 1500, 2000],
             "default": 700},
            {"name": "fmt", "type": "select", "label": "Output format",
             "options": ["PNG", "JPG", "WEBP", "AVIF", "TIFF", "BMP"],
             "default": "PNG"},
        ],
        "run": {"label": "START COMPRESSION"},
    }

    sys.exit(run_shell(demo_task, DEMO_CONFIG))