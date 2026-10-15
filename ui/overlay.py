"""Holographic HUD overlay.

A frameless, transparent, always-on-top, click-through window painted with QPainter at
~60 fps. It knows nothing about audio or AI: it is driven only through its slots
(set_state / set_level / set_transcript / set_reply), which the voice worker reaches via
Qt signals, so the animation never waits on the voice loop.

Layers, centre outwards: breathing core orb, inner ring, segmented ring (clockwise),
tick ring (counter-clockwise), outer arcs, orbiting data dots, then captions below.
"""
from __future__ import annotations

import ctypes
import math
import random
import time
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QObject, QPointF, QRect, QRectF, Qt, QTimer, Slot
from PySide6.QtGui import (QColor, QConicalGradient, QFont, QFontDatabase, QFontMetricsF, QGuiApplication,
                           QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient)
from PySide6.QtWidgets import QWidget

FONT_DIR = Path(__file__).resolve().parent / "fonts"

# Visual targets per state; current values glide towards these every frame.
STATE_TARGETS = {
    #            ring speed, red-orange accent, radar sweep, waveform, segment chase, strong pulse
    "idle":      dict(speed=0.6, alert=0.0, sweep=0.0, wave=0.0, chase=0.0, pulse=0.0),
    "listening": dict(speed=1.0, alert=0.0, sweep=0.0, wave=0.0, chase=0.0, pulse=0.0),
    "thinking":  dict(speed=3.2, alert=0.0, sweep=1.0, wave=0.0, chase=1.0, pulse=0.0),
    "speaking":  dict(speed=1.4, alert=0.0, sweep=0.0, wave=1.0, chase=0.0, pulse=0.0),
    "confirm":   dict(speed=0.5, alert=1.0, sweep=0.0, wave=0.0, chase=0.0, pulse=1.0),
    "error":     dict(speed=0.8, alert=1.0, sweep=0.0, wave=0.0, chase=0.0, pulse=0.0),
}
LABELS = {"listening": "LISTENING", "thinking": "PROCESSING", "speaking": "RESPONDING",
          "confirm": "CONFIRM", "error": "ERROR", "idle": "STANDBY"}

SEGMENTS = 32
TICKS = 72
TYPE_RATE = 55  # characters per second for the typewriter caption


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _mix(c1: QColor, c2: QColor, t: float, alpha: float = 255) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(int(_lerp(c1.red(), c2.red(), t)), int(_lerp(c1.green(), c2.green(), t)),
                  int(_lerp(c1.blue(), c2.blue(), t)), int(max(0, min(255, alpha))))


def _load_font(family: str, fallback: str = "Segoe UI") -> str:
    """Register bundled fonts once; return the family to use."""
    if not getattr(_load_font, "_loaded", False):
        for ttf in FONT_DIR.glob("*.ttf"):
            QFontDatabase.addApplicationFont(str(ttf))
        _load_font._loaded = True
    return family if family in QFontDatabase.families() else fallback


class Overlay(QWidget):
    def __init__(self, cfg: dict):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        ui = cfg["ui"]
        pal = ui["palette"]
        self.c_primary, self.c_secondary = QColor(pal["primary"]), QColor(pal["secondary"])
        self.c_high, self.c_accent = QColor(pal["highlight"]), QColor(pal["accent"])
        self.fade_in_s = ui["fade_in_ms"] / 1000
        self.fade_out_delay = ui["fade_out_delay_ms"] / 1000
        self.backdrop = float(ui.get("backdrop", 0.35))

        size = int(ui["size"])
        self.s = size / 420  # every radius below is designed for 420 px
        self.resize(size, int(size * 1.12))
        self.cx, self.cy = size // 2, round(size * 0.42)  # whole pixels: cached layers blit 1:1
        self._place(ui["position"], int(ui["margin"]))

        self.label_font = QFont(_load_font(ui.get("font", "Orbitron")), max(7, round(8 * self.s)))
        self.label_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3 * self.s)
        self.label_font.setWeight(QFont.Weight.DemiBold)
        cap_family = _load_font(ui.get("caption_font", "Rajdhani"))
        self.user_font = QFont(cap_family, max(8, round(11 * self.s)))
        self.user_font.setWeight(QFont.Weight.Medium)
        self.reply_font = QFont(cap_family, max(9, round(13 * self.s)))
        self.reply_font.setWeight(QFont.Weight.DemiBold)

        self._build_paths()
        self._cache: dict[tuple, QPixmap] = {}
        self._backdrop_pm: QPixmap | None = None
        ring = int(182 * self.s)
        self._ring_rect = QRect(self.cx - ring, max(0, self.cy - ring), 2 * ring, 2 * ring)
        self._caption_rect = QRect(0, self.cy + int(150 * self.s), self.width(), self.height())
        self._full_redraw = True
        self._wave_th = [2 * math.pi * i / 64 for i in range(65)]
        self._wave_cos = [math.cos(t) for t in self._wave_th]
        self._wave_sin = [math.sin(t) for t in self._wave_th]
        rnd = random.Random(7)
        self.dots = [(rnd.uniform(66, 150) * self.s, rnd.choice([-1, 1]) * rnd.uniform(12, 45),
                      rnd.uniform(0, 360), rnd.uniform(1.3, 2.4) * self.s) for _ in range(8)]

        # animation state
        self.state, self.info = "idle", ""
        self.cur = dict(STATE_TARGETS["idle"])
        self.target = dict(STATE_TARGETS["idle"])
        self.level_in, self.level = 0.0, 0.0
        self.rot_seg = self.rot_tick = self.sweep_angle = 0.0
        self.rot_arcs = [0.0, 120.0, 240.0]
        self.flash = 0.0
        self.t = 0.0
        self.opacity, self.opacity_target = 0.0, 0.0
        self.hide_at: float | None = None
        self.user_text = self.reply_text = ""
        self.reply_t0 = 0.0

        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.interval_ms = max(8, int(1000 / int(ui.get("fps", 60))))

    # ---------------------------------------------------------------- window setup

    def _place(self, position: str, margin: int) -> None:
        area = QGuiApplication.primaryScreen().availableGeometry()
        w, h = self.width(), self.height()
        if position == "bottom-right":
            x, y = area.right() - w - margin, area.bottom() - h - margin
        elif position == "top-right":
            x, y = area.right() - w - margin, area.top() + margin
        else:
            x, y = area.center().x() - w // 2, area.center().y() - h // 2
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        # Belt and braces for click-through: layered + transparent + never activate.
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x80000, 0x20, 0x8000000, 0x80
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:  # noqa: BLE001
            pass

    def _build_paths(self) -> None:
        s = self.s
        # segmented ring: one path for all segments + one per segment for the chase highlight
        r = 88 * s
        rect = QRectF(-r, -r, 2 * r, 2 * r)
        step, gap = 360 / SEGMENTS, 3.2
        self.seg_paths = []
        self.seg_path = QPainterPath()
        for i in range(SEGMENTS):
            p = QPainterPath()
            p.arcMoveTo(rect, i * step)
            p.arcTo(rect, i * step, step - gap)
            self.seg_paths.append(p)
            self.seg_path.addPath(p)
        # tick ring
        self.tick_path, self.tick_major = QPainterPath(), QPainterPath()
        r0 = 108 * s
        for i in range(TICKS):
            a = math.radians(i * 360 / TICKS)
            major = i % 6 == 0
            length = (9 if major else 4) * s
            path = self.tick_major if major else self.tick_path
            path.moveTo(math.cos(a) * r0, math.sin(a) * r0)
            path.lineTo(math.cos(a) * (r0 + length), math.sin(a) * (r0 + length))
        # outer arcs: (radius, span degrees, speed deg/s, colour key, width)
        self.arcs = [(126 * s, 70, 26, "primary", 2.2), (135 * s, 135, -15, "secondary", 1.6),
                     (144 * s, 34, 42, "high", 2.0)]

    # ---------------------------------------------------------------- slots (driven by signals)

    @Slot(str, str)
    def set_state(self, state: str, info: str = "") -> None:
        if state not in STATE_TARGETS:
            return
        self._full_redraw = True
        now = time.monotonic()
        if state == "idle":
            if self.state != "error":  # an error keeps its own, longer, hide timer
                self.state = "idle"
                self.target = dict(STATE_TARGETS["idle"])
                self.hide_at = now + self.fade_out_delay
            return
        if self.opacity_target == 0 or not self.isVisible():  # waking up from hidden
            self.user_text = self.reply_text = ""
        self.hide_at = None
        self.state, self.info = state, info
        self.target = dict(STATE_TARGETS[state])
        if state == "error":
            self.flash = 1.0
            self.reply_text, self.reply_t0 = info or "Something went wrong.", now
            self.hide_at = now + 2.5 + len(self.reply_text) / TYPE_RATE
        if state == "confirm" and info:
            info = info.split(" Should I go ahead?")[0]  # the HUD shows its own "SAY YES" line
            if info != self.reply_text:
                self.reply_text, self.reply_t0 = info, now
        if state == "listening":
            self.level_in = 0.0
        self._appear()

    @Slot(float)
    def set_level(self, level: float) -> None:
        self.level_in = max(0.0, min(1.0, float(level)))

    @Slot(str)
    def set_transcript(self, text: str) -> None:
        self._full_redraw = True
        self.user_text = text
        if text:
            self.reply_text = ""

    @Slot(str)
    def set_reply(self, text: str) -> None:
        self._full_redraw = True
        if self.state == "confirm" and text.startswith(self.reply_text):
            return  # confirm prompt already shown from the state info
        self.reply_text, self.reply_t0 = text, time.monotonic()
        self._appear()

    def _appear(self) -> None:
        self.opacity_target = 1.0
        if not self.isVisible():
            self.show()
        if not self.timer.isActive():
            self.clock.start()
            self.timer.start(self.interval_ms)

    # ---------------------------------------------------------------- animation

    def _tick(self) -> None:
        dt = min(0.05, self.clock.restart() / 1000) or 1 / 60
        self.t += dt
        now = time.monotonic()
        if self.hide_at is not None and now >= self.hide_at:
            self.opacity_target, self.hide_at = 0.0, None

        k = 1 - math.exp(-dt * 5)  # ~200 ms glide between states
        for key, goal in self.target.items():
            self.cur[key] = _lerp(self.cur[key], goal, k)
        rate = 18 if self.level_in > self.level else 6  # fast attack, slow release
        self.level = _lerp(self.level, self.level_in if self.state in ("listening", "speaking") else 0.0,
                           1 - math.exp(-dt * rate))
        self.flash = max(0.0, self.flash - dt * 2.2)

        spd = self.cur["speed"]
        self.rot_seg = (self.rot_seg + 14 * spd * dt) % 360
        self.rot_tick = (self.rot_tick - 9 * spd * dt) % 360
        for i, (_, _, v, _, _) in enumerate(self.arcs):
            self.rot_arcs[i] = (self.rot_arcs[i] + v * spd * dt) % 360
        self.sweep_angle = (self.sweep_angle + 220 * dt) % 360

        if self.opacity_target > self.opacity:
            self.opacity = min(1.0, self.opacity + dt / self.fade_in_s)
        elif self.opacity_target < self.opacity:
            self.opacity = max(0.0, self.opacity - dt / 0.45)
            if self.opacity == 0.0:
                self.timer.stop()
                self.hide()
                return
        # Repaint only what changes: the rings every frame, the captions only while text is
        # typing out / blinking, everything during fades.
        typing = self.reply_text and (now - self.reply_t0) * TYPE_RATE < len(self.reply_text) + 2
        if 0 < self.opacity < 1 or self._full_redraw:
            self._full_redraw = False
            self.update()
        else:
            self.update(self._ring_rect)
            if typing or self.state == "confirm":
                self.update(self._caption_rect)

    def _glow(self, p: QPainter, path: QPainterPath, color: QColor, width: float, glow: float = 4.0,
              glow_alpha: float = 0.28, cap=Qt.PenCapStyle.FlatCap) -> None:
        """Bloom: a wide translucent stroke underneath, the sharp stroke on top."""
        wide = QColor(color)
        wide.setAlphaF(max(0.0, min(1.0, color.alphaF() * glow_alpha)))
        p.setPen(QPen(wide, width * glow, Qt.PenStyle.SolidLine, cap))
        p.drawPath(path)
        p.setPen(QPen(color, width, Qt.PenStyle.SolidLine, cap))
        p.drawPath(path)

    # ---------------------------------------------------------------- cached layers
    # Software painting of wide glowing strokes is the expensive part, so:
    #  * rings that repeat around the circle (segments: every 11.25 deg, minor ticks: every 5 deg)
    #    are pre-rendered as a short strip of rotation frames and then just copied each frame;
    #  * the inner ring is pre-rendered at a few sizes for its pulse;
    #  * the short outer arcs are cropped images rotated live (small area = cheap).
    # Each is built in two colours (normal / alert) and cross-faded, so memory stays small.

    SEG_FRAMES, TICK_FRAMES, INNER_FRAMES, CORE_FRAMES = 24, 10, 12, 20
    CORE_MIN, CORE_MAX = 0.9, 1.75  # core radius range (x 30 px) across breathing / voice / pulse

    def _pixmap(self, w: float, h: float | None = None) -> tuple[QPixmap, QPainter]:
        h = w if h is None else h
        dpr = self.devicePixelRatioF()
        even = lambda v: int(math.ceil(v * dpr / 2)) * 2  # even device size: centre lands on a pixel
        pm = QPixmap(even(w), even(h))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        return pm, painter

    def _colors(self, alert_bin: int) -> tuple[QColor, QColor]:
        return (_mix(self.c_primary, self.c_accent, alert_bin),
                _mix(self.c_secondary, self.c_accent.darker(140), alert_bin))

    def _frames(self, name: str, alert_bin: int) -> list[QPixmap]:
        key = (name, alert_bin)
        if key not in self._cache:
            primary, secondary = self._colors(alert_bin)
            self._cache[key] = getattr(self, f"_build_{name}")(primary, secondary)
        return self._cache[key]

    def _ring_frames(self, radius: float, count: int, draw) -> list[QPixmap]:
        size = 2 * math.ceil(radius)
        frames = []
        for i in range(count):
            pm, p = self._pixmap(size)
            p.translate(size / 2, size / 2)
            draw(p, i)
            p.end()
            frames.append(pm)
        return frames

    def _build_seg(self, primary, secondary):
        step = 360 / SEGMENTS / self.SEG_FRAMES
        col = _mix(secondary, primary, 0.35, 210)

        def draw(p, i):
            p.rotate(i * step)
            self._glow(p, self.seg_path, col, 5 * self.s, 2.0, 0.35)
        return self._ring_frames(100 * self.s, self.SEG_FRAMES, draw)

    def _build_ticks(self, primary, secondary):
        step = 360 / TICKS / self.TICK_FRAMES
        col = _mix(primary, primary, 0, 150)

        def draw(p, i):
            p.rotate(i * step)
            self._glow(p, self.tick_path, col, 1.0, 3, 0.35)
        return self._ring_frames(122 * self.s, self.TICK_FRAMES, draw)

    def _build_inner(self, primary, secondary):
        col = _mix(primary, self.c_high, 0.25, 235)

        def draw(p, i):
            r = 58 * self.s * (1 + 0.12 * i / (self.INNER_FRAMES - 1))
            ring = QPainterPath()
            ring.addEllipse(QPointF(0, 0), r, r)
            self._glow(p, ring, col, 1.5, 5, 0.3)
        return self._ring_frames(58 * 1.12 * self.s + 10 * self.s, self.INNER_FRAMES, draw)

    def _build_arcs(self, primary, secondary):
        """One cropped image per arc, drawn at angle 0; returns (pixmap, top-left) pairs."""
        out = []
        cols = {"primary": primary, "secondary": _mix(secondary, primary, 0.3),
                "high": _mix(self.c_high, primary, 0.4)}
        for r, span, _, key, w in self.arcs:
            rect = QRectF(-r, -r, 2 * r, 2 * r)
            arc = QPainterPath()
            arc.arcMoveTo(rect, 0)
            arc.arcTo(rect, 0, span)
            pad = 8 * self.s
            box = arc.boundingRect().adjusted(-pad, -pad, pad, pad)
            pm, p = self._pixmap(box.width(), box.height())
            p.translate(-box.left(), -box.top())
            col = cols[key]
            self._glow(p, arc, _mix(col, col, 0, 235), w * self.s, 4, 0.3, Qt.PenCapStyle.RoundCap)
            p.end()
            out.append((pm, box.topLeft()))
        return out

    def _build_core(self, primary, secondary):
        """The glowing orb at CORE_FRAMES sizes, so the pulse never needs a live rescale."""
        frames = []
        for i in range(self.CORE_FRAMES):
            k = self.CORE_MIN + (self.CORE_MAX - self.CORE_MIN) * i / (self.CORE_FRAMES - 1)
            r = 30 * self.s * k * 1.9
            pm, p = self._pixmap(2 * r)
            p.translate(pm.width() / pm.devicePixelRatio() / 2, pm.height() / pm.devicePixelRatio() / 2)
            g = QRadialGradient(QPointF(0, 0), r)
            g.setColorAt(0.0, _mix(self.c_high, self.c_high, 0, 255))
            g.setColorAt(0.18, _mix(self.c_high, primary, 0.5, 235))
            g.setColorAt(0.45, _mix(primary, primary, 0, 170))
            g.setColorAt(0.75, _mix(secondary, secondary, 0, 60))
            g.setColorAt(1.0, _mix(secondary, secondary, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(0, 0), r, r)
            p.end()
            frames.append(pm)
        return frames

    def _build_backdrop(self) -> QPixmap:
        """Soft dark glass behind rings and captions so the HUD reads on bright windows."""
        pm, p = self._pixmap(self.width(), self.height())
        p.setPen(Qt.PenStyle.NoPen)
        a, s = self.backdrop, self.s
        g = QRadialGradient(QPointF(self.cx, self.cy), 178 * s)
        g.setColorAt(0.0, QColor(0, 8, 16, int(255 * a)))
        g.setColorAt(0.72, QColor(0, 8, 16, int(170 * a)))
        g.setColorAt(1.0, QColor(0, 8, 16, 0))
        p.setBrush(g)
        p.drawEllipse(QPointF(self.cx, self.cy), 178 * s, 178 * s)
        # caption panel: nested rounded rects give it a feathered edge
        top, bottom, margin = self.cy + 152 * s, self.height() - 4 * s, 10 * s
        for i in range(14):
            inset = i * 1.3 * s
            p.setBrush(QColor(0, 8, 16, int(255 * a * 0.15)))
            p.drawRoundedRect(QRectF(margin + inset, top + inset, self.width() - 2 * (margin + inset),
                                     bottom - top - 2 * inset), 16 * s, 16 * s)
        p.end()
        return pm

    def _blend(self, p: QPainter, name: str, opacity: float, draw_one) -> None:
        """Cross-fade the normal and alert colour versions of a cached layer."""
        alert = self.cur["alert"]
        for alert_bin, weight in ((0, 1 - alert), (1, alert)):
            if weight > 0.02:
                p.setOpacity(self.opacity * max(0.0, min(1.0, opacity)) * weight)
                draw_one(self._frames(name, alert_bin))
        p.setOpacity(self.opacity)

    @staticmethod
    def _blit(p: QPainter, pm: QPixmap) -> None:
        half = pm.width() / pm.devicePixelRatio() / 2
        p.drawPixmap(QPointF(-half, -half), pm)

    # ---------------------------------------------------------------- painting

    def paintEvent(self, event):
        if self.opacity <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s, c = self.s, self.cur
        alert, lvl = c["alert"], self.level
        pulse = c["pulse"] * (0.5 + 0.5 * math.sin(self.t * 2 * math.pi * 0.7))
        primary = _mix(self.c_primary, self.c_accent, alert)
        secondary = _mix(self.c_secondary, self.c_accent.darker(140), alert)
        bright = 0.72 + 0.28 * min(1.0, pulse + lvl)

        if self.backdrop > 0:
            if self._backdrop_pm is None:
                self._backdrop_pm = self._build_backdrop()
            p.setOpacity(self.opacity)
            p.drawPixmap(0, 0, self._backdrop_pm)
        p.setOpacity(self.opacity)
        dpr = self.devicePixelRatioF()  # snap the centre to whole device pixels (e.g. 125% scaling)
        p.translate(round(self.cx * dpr) / dpr, round(self.cy * dpr) / dpr)

        # error flash
        if self.flash > 0:
            g = QRadialGradient(QPointF(0, 0), 160 * s)
            g.setColorAt(0.0, _mix(self.c_accent, self.c_accent, 0, 150 * self.flash))
            g.setColorAt(1.0, _mix(self.c_accent, self.c_accent, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(0, 0), 160 * s, 160 * s)
            p.setBrush(Qt.BrushStyle.NoBrush)

        # 1. core orb — breathing, swells with voice, strong pulse in CONFIRM
        breath = 0.05 * math.sin(self.t * 2 * math.pi * 0.25)
        k = min(self.CORE_MAX, max(self.CORE_MIN, 1 + breath + 0.45 * lvl + 0.22 * pulse))
        core_i = round((k - self.CORE_MIN) / (self.CORE_MAX - self.CORE_MIN) * (self.CORE_FRAMES - 1))
        self._blend(p, "core", 0.85 + 0.15 * bright, lambda f: self._blit(p, f[core_i]))

        # radar sweep (PROCESSING)
        if c["sweep"] > 0.01:
            r = 100 * s
            cg = QConicalGradient(QPointF(0, 0), -self.sweep_angle)
            cg.setColorAt(0.0, _mix(primary, primary, 0, 140 * c["sweep"]))
            cg.setColorAt(0.14, _mix(primary, primary, 0, 0))
            cg.setColorAt(1.0, _mix(primary, primary, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cg)
            p.drawPie(QRectF(-r, -r, 2 * r, 2 * r), int(-self.sweep_angle * 16), 52 * 16)
            p.setBrush(Qt.BrushStyle.NoBrush)
            a = math.radians(-self.sweep_angle)
            line = QPainterPath(QPointF(0, 0))
            line.lineTo(math.cos(a) * r, -math.sin(a) * r)
            self._glow(p, line, _mix(self.c_high, primary, 0.4, 200 * c["sweep"]), 1.2, 4)

        # circular waveform (RESPONDING)
        if c["wave"] > 0.01:
            n = 64
            for layer, (col, phase, gain) in enumerate(((primary, 0.0, 1.0), (secondary, 1.7, 0.7))):
                amp = (2.5 + 24 * lvl) * s * c["wave"] * gain
                base = (70 + layer * 3) * s
                pts = []
                for i in range(n + 1):
                    th = self._wave_th[i]
                    d = amp * (0.55 * math.sin(5 * th + 3.1 * self.t + phase)
                               + 0.3 * math.sin(9 * th - 4.7 * self.t + phase)
                               + 0.15 * math.sin(14 * th + 7.3 * self.t))
                    pts.append(QPointF(self._wave_cos[i] * (base + d), self._wave_sin[i] * (base + d)))
                path = QPainterPath()
                path.addPolygon(QPolygonF(pts))
                color = _mix(col, col, 0, 230 * c["wave"])
                if layer == 0:
                    self._glow(p, path, color, 1.4, 3.5, cap=Qt.PenCapStyle.RoundCap)
                else:
                    p.setPen(QPen(color, 1.0))
                    p.drawPath(path)

        # 2. inner ring — pulses with your voice / the confirm heartbeat
        grow = min(1.0, (0.07 * lvl + 0.04 * pulse) / 0.12)
        self._blend(p, "inner", bright, lambda f: self._blit(p, f[round(grow * (self.INNER_FRAMES - 1))]))

        # 3. segmented ring — clockwise; pre-rendered frames, no live rotation
        seg_step = 360 / SEGMENTS
        seg_i = int((self.rot_seg % seg_step) / seg_step * self.SEG_FRAMES)
        self._blend(p, "seg", bright, lambda f: self._blit(p, f[seg_i]))
        if c["chase"] > 0.01:
            p.save()
            p.rotate(self.rot_seg - (self.rot_seg % seg_step) + seg_i * seg_step / self.SEG_FRAMES)
            head = int(self.t * 22) % SEGMENTS
            for k in range(6):
                col = _mix(self.c_high, primary, k / 6, 255 * c["chase"] * (1 - k / 6))
                self._glow(p, self.seg_paths[(head - k) % SEGMENTS], col, 5 * s, 2.0, 0.5)
            p.restore()

        # 4. tick ring — counter-clockwise (minor ticks pre-rendered, 12 major ticks live)
        tick_step = 360 / TICKS
        tick_i = int((self.rot_tick % tick_step) / tick_step * self.TICK_FRAMES)
        self._blend(p, "ticks", bright, lambda f: self._blit(p, f[tick_i]))
        p.save()
        p.rotate(self.rot_tick - (self.rot_tick % tick_step) + tick_i * tick_step / self.TICK_FRAMES)
        self._glow(p, self.tick_major, _mix(self.c_high, primary, 0.3, 240 * bright), 1.4, 3, 0.4)
        p.restore()

        # 5. outer arcs — different lengths and speeds (small cropped images, rotated live)
        def draw_arcs(arcs):
            for (pm, top_left), rot in zip(arcs, self.rot_arcs):
                p.save()
                p.rotate(-rot)
                p.drawPixmap(top_left, pm)
                p.restore()
        self._blend(p, "arcs", bright, draw_arcs)

        # 6. orbiting data dots
        p.setPen(Qt.PenStyle.NoPen)
        halo, dot = _mix(primary, primary, 0, 60), _mix(self.c_high, primary, 0.3, 240)
        for radius, speed, phase, size in self.dots:
            a = math.radians(phase + speed * self.t * (0.6 + 0.4 * c["speed"]))
            pt = QPointF(math.cos(a) * radius, math.sin(a) * radius)
            p.setBrush(halo)
            p.drawEllipse(pt, size * 2.6, size * 2.6)
            p.setBrush(dot)
            p.drawEllipse(pt, size, size)
        p.setBrush(Qt.BrushStyle.NoBrush)

        p.resetTransform()
        self._paint_captions(p, primary, alert)
        p.end()

    def _paint_captions(self, p: QPainter, primary: QColor, alert: float) -> None:
        s, w = self.s, self.width()
        y = self.cy + 160 * s
        label = LABELS.get(self.state, "")
        if self.state == "idle":
            label = "STANDBY"
        # state label with decorative rules either side
        p.setFont(self.label_font)
        fm = QFontMetricsF(self.label_font)
        lw = fm.horizontalAdvance(label)
        p.setPen(QPen(_mix(primary, self.c_high, 0.2, 230), 1))
        p.drawText(QRectF(0, y, w, fm.height()), Qt.AlignmentFlag.AlignHCenter, label)
        mid = y + fm.height() / 2
        rule = QColor(primary)
        rule.setAlpha(140)
        p.setPen(QPen(rule, 1))
        p.drawLine(QPointF(w / 2 - lw / 2 - 40 * s, mid), QPointF(w / 2 - lw / 2 - 10 * s, mid))
        p.drawLine(QPointF(w / 2 + lw / 2 + 10 * s, mid), QPointF(w / 2 + lw / 2 + 40 * s, mid))
        y += fm.height() + 6 * s

        margin = 24 * s
        box_w = w - 2 * margin
        # what you said (dim, one line)
        if self.user_text:
            p.setFont(self.user_font)
            fmu = QFontMetricsF(self.user_font)
            text = fmu.elidedText(f"“{self.user_text}”", Qt.TextElideMode.ElideRight, box_w)
            p.setPen(_mix(self.c_high, primary, 0.5, 150))
            p.drawText(QRectF(margin, y, box_w, fmu.height()), Qt.AlignmentFlag.AlignHCenter, text)
            y += fmu.height() + 2 * s

        # Jarvis's reply (bright, typewriter)
        if self.reply_text:
            shown = int((time.monotonic() - self.reply_t0) * TYPE_RATE)
            text = self.reply_text[:shown]
            if shown < len(self.reply_text):
                text += "▌"
            p.setFont(self.reply_font)
            fmr = QFontMetricsF(self.reply_font)
            rect = QRectF(margin, y, box_w, fmr.lineSpacing() * 3 + 2)
            flags = Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap
            p.setPen(QColor(0, 0, 0, 150))  # soft shadow for legibility
            p.drawText(rect.translated(0, 1.2 * s), flags, text)
            p.setPen(_mix(self.c_high, primary, 0.08 + 0.4 * alert, 250))
            p.drawText(rect, flags, text)
            y += fmr.lineSpacing() * 3 + 4 * s

        if self.state == "confirm":
            p.setFont(self.label_font)
            blink = 0.55 + 0.45 * math.sin(self.t * 2 * math.pi * 0.9)
            p.setPen(_mix(self.c_accent, self.c_high, 0.15, 255 * blink))
            p.drawText(QRectF(0, min(y, self.height() - fm.height() - 4), w, fm.height()),
                       Qt.AlignmentFlag.AlignHCenter, "SAY YES TO CONFIRM")


# ================================================================ demo mode

class DemoDriver(QObject):
    """Cycles through every state with fake mic levels: python main.py --demo-ui"""

    SCRIPT = [
        ("listening", "", 3.6, "I feel like listening to some music", None),
        ("thinking", "", 2.6, None, None),
        ("speaking", "", 4.2, None, "Right away, sir. Spotify is open, so enjoy the music."),
        ("idle", "", 2.6, None, None),
        ("listening", "", 3.0, "delete the old reports folder on my desktop", None),
        ("thinking", "", 1.8, None, None),
        ("confirm", "Move the folder Desktop\\old reports and everything in it (14 items) to the Recycle Bin.",
         4.5, None, None),
        ("error", "I can't reach OpenAI right now. Check your internet connection.", 3.2, None, None),
        ("idle", "", 3.0, None, None),
    ]

    def __init__(self, overlay: Overlay):
        super().__init__(overlay)
        self.o = overlay
        self.i = -1
        self.step_timer = QTimer(self)
        self.step_timer.setSingleShot(True)
        self.step_timer.timeout.connect(self._next)
        self.level_timer = QTimer(self)
        self.level_timer.timeout.connect(self._level)
        self.t0 = time.monotonic()

    def start(self) -> None:
        self.level_timer.start(40)
        self._next()

    def _next(self) -> None:
        self.i = (self.i + 1) % len(self.SCRIPT)
        state, info, dur, said, reply = self.SCRIPT[self.i]
        print(f"[demo] {state.upper()}", flush=True)
        self.o.set_state(state, info)
        if said:
            QTimer.singleShot(int(dur * 650), lambda: self.o.set_transcript(said))
        if reply:
            self.o.set_reply(reply)
        self.t0 = time.monotonic()
        self.step_timer.start(int(dur * 1000))

    def _level(self) -> None:
        state = self.SCRIPT[self.i][0]
        t = time.monotonic() - self.t0
        if state == "listening":  # syllable-like bursts while "talking", quiet at the end
            talking = 0.4 < t < self.SCRIPT[self.i][2] * 0.65
            lvl = (0.35 + 0.6 * abs(math.sin(t * 7.3)) * random.uniform(0.5, 1.0)) if talking else random.uniform(0, 0.05)
        elif state == "speaking":
            lvl = 0.25 + 0.55 * abs(math.sin(t * 5.1)) * random.uniform(0.6, 1.0)
        else:
            lvl = 0.0
        self.o.set_level(lvl)
