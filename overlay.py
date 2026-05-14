"""
overlay.py — Floating recording overlay for QStrauss Voice (macOS)
Uses NSPanel + NSNonactivatingPanelMask so it floats without activating the app.

Design: QStrauss brand — navy #0b1133 + teal #00c896 — minimal pill shape
"""
import math
import objc
import AppKit
from Foundation import NSMakeRect, NSTimer


NSNonactivatingPanelMask = 1 << 7

# Brand colors (NSColor 0-1 range)
_NAVY  = (0.043, 0.067, 0.20, 0.94)   # #0b1133
_TEAL  = (0.0,   0.784, 0.588, 1.0)   # #00c896
_MUTED = (0.227, 0.306, 0.447, 0.6)   # #3a4e72 dim


class _OverlayView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(_OverlayView, self).initWithFrame_(frame)
        if self is not None:
            self._status = "listening"
            self._phase = 0
        return self

    def drawRect_(self, dirtyRect):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height

        # ── Pill-shaped navy background ────────────────────────────────────
        bg = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, h / 2, h / 2
        )
        AppKit.NSColor.colorWithRed_green_blue_alpha_(*_NAVY).setFill()
        bg.fill()

        style = AppKit.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AppKit.NSTextAlignmentCenter)

        if self._status == "listening":
            self._drawWave(w, h)
        else:
            self._drawTranscribingDots(w, h)

        # ── Small label bottom-right ────────────────────────────────────────
        label = "escuchando" if self._status == "listening" else "transcribiendo"
        attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(9.5, 0.3),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithRed_green_blue_alpha_(*_MUTED),
            AppKit.NSParagraphStyleAttributeName: style,
        }
        s = AppKit.NSAttributedString.alloc().initWithString_attributes_(label, attrs)
        s.drawInRect_(NSMakeRect(0, 6, w, 14))

    @objc.python_method
    def _drawWave(self, w, h):
        """Teal sine-wave dots — listening state."""
        n, r = 18, 2.5
        pad = h / 2 + 4
        total_w = w - pad * 2
        gap = total_w / (n - 1)
        cy0 = h / 2 + 3
        for i in range(n):
            dy    = math.sin(self._phase * 0.10 + i * 0.40) * 4.5
            alpha = 0.55 + 0.45 * abs(math.sin(self._phase * 0.08 + i * 0.3))
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.0, 0.784, 0.588, alpha).setFill()
            cx = pad + i * gap
            cy = cy0 + dy
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - r, cy - r, r * 2, r * 2)
            ).fill()

    @objc.python_method
    def _drawTranscribingDots(self, w, h):
        """Three bouncing dots — transcribing state."""
        r = 4.5
        spacing = 20.0
        x0 = (w - spacing * 2) / 2
        cy0 = h / 2 + 3
        for i in range(3):
            phase_off = i * (math.pi * 2 / 3)
            dy    = math.sin(self._phase * 0.14 + phase_off) * 5.5
            alpha = 0.45 + 0.55 * abs(math.sin(self._phase * 0.14 + phase_off))
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.0, 0.784, 0.588, alpha).setFill()
            cx = x0 + i * spacing
            cy = cy0 + dy
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - r, cy - r, r * 2, r * 2)
            ).fill()

    @objc.python_method
    def setStatus(self, status):
        self._status = status
        self.setNeedsDisplay_(True)

    def tick_(self, timer):
        self._phase += 1
        self.setNeedsDisplay_(True)


class RecordingOverlay:
    def __init__(self):
        W, H = 300, 52          # Slim pill — very minimal
        screen = AppKit.NSScreen.mainScreen().frame()
        x = (screen.size.width - W) / 2
        y = screen.size.height * 0.12  # Near bottom of screen

        self._panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H),
            AppKit.NSWindowStyleMaskBorderless | NSNonactivatingPanelMask,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._panel.setLevel_(25)
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        self._panel.setHasShadow_(True)
        self._panel.setMovableByWindowBackground_(True)
        self._panel.setFloatingPanel_(True)
        self._panel.setWorksWhenModal_(True)
        self._panel.setHidesOnDeactivate_(False)
        self._panel.setCanHide_(False)
        self._panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle
        )

        self._view = _OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        self._panel.setContentView_(self._view)
        self._timer = None

    def show(self, status="listening"):
        self._view.setStatus(status)
        screen = AppKit.NSScreen.mainScreen().frame()
        W = self._panel.frame().size.width
        H = self._panel.frame().size.height
        x = (screen.size.width - W) / 2
        y = screen.size.height * 0.12
        self._panel.setFrame_display_(NSMakeRect(x, y, W, H), True)
        self._panel.orderFront_(None)
        if self._timer is None:
            self._timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    0.04, self._view, b"tick:", None, True
                )
            )

    def set_status(self, status):
        self._view.setStatus(status)

    def hide(self):
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        self._panel.orderOut_(None)
