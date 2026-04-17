"""
overlay.py — Floating recording overlay for QStrauss Voice (macOS)
Uses NSPanel + NSNonactivatingPanelMask so it floats without activating the app.

Design: QStrauss brand — navy #0b1133 + teal #00c896
"""
import math
import objc
import AppKit
from Foundation import NSMakeRect, NSTimer


NSNonactivatingPanelMask = 1 << 7

# Brand colors (NSColor 0-1 range)
_NAVY   = (0.043, 0.067, 0.20, 0.96)   # #0b1133
_TEAL   = (0.0,   0.784, 0.588, 1.0)   # #00c896
_WHITE  = (1.0,   1.0,   1.0,   1.0)
_MUTED  = (0.227, 0.306, 0.447, 0.75)  # #3a4e72
_TEAL_DIM = (0.0, 0.784, 0.588, 0.35)  # teal at low opacity for transcribing dots


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

        # ── Background — navy rounded card ─────────────────────────────────
        bg = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, 18.0, 18.0
        )
        AppKit.NSColor.colorWithRed_green_blue_alpha_(*_NAVY).setFill()
        bg.fill()

        # ── Thin teal top border line ───────────────────────────────────────
        teal_line = AppKit.NSBezierPath.bezierPath()
        teal_line.moveToPoint_(AppKit.NSMakePoint(36, h - 1))
        teal_line.lineToPoint_(AppKit.NSMakePoint(w - 36, h - 1))
        teal_line.setLineWidth_(1.5)
        AppKit.NSColor.colorWithRed_green_blue_alpha_(*_TEAL).setStroke()
        teal_line.stroke()

        style = AppKit.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AppKit.NSTextAlignmentCenter)

        # ── Title: "QStrauss" white + "Voice" teal ─────────────────────────
        attrs_q = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(17.0, 0.6),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithRed_green_blue_alpha_(*_WHITE),
            AppKit.NSParagraphStyleAttributeName: style,
        }
        attrs_v = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(17.0, 0.4),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithRed_green_blue_alpha_(*_TEAL),
            AppKit.NSParagraphStyleAttributeName: style,
        }
        # Draw as one attributed string with mixed colors
        full = AppKit.NSMutableAttributedString.alloc().init()
        full.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_("QStrauss", attrs_q)
        )
        full.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(" Voice", attrs_v)
        )
        full.drawInRect_(NSMakeRect(0, h - 44, w, 26))

        # ── Animation area ──────────────────────────────────────────────────
        if self._status == "listening":
            self._drawWave(w, h)
            self._drawHint(w, "Presiona el atajo para detener")
        else:
            self._drawTranscribingDots(w, h)
            self._drawHint(w, "Transcribiendo…")

    @objc.python_method
    def _drawWave(self, w, h):
        """Teal sine-wave dot animation for listening state."""
        AppKit.NSColor.colorWithRed_green_blue_alpha_(*_TEAL).setFill()
        n, r = 22, 3.0
        total_w = w - 72
        gap = total_w / (n - 1)
        y0 = h / 2 - 2
        for i in range(n):
            amp = 5.0 + math.sin(self._phase * 0.05 + i * 0.4) * 1.5
            dy = math.sin(self._phase * 0.10 + i * 0.38) * amp
            # Vary opacity slightly for depth
            alpha = 0.6 + 0.4 * abs(math.sin(self._phase * 0.08 + i * 0.3))
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.0, 0.784, 0.588, alpha).setFill()
            cx = 36 + i * gap
            cy = y0 + dy
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - r, cy - r, r * 2, r * 2)
            ).fill()

    @objc.python_method
    def _drawTranscribingDots(self, w, h):
        """Three bouncing dots for transcribing state."""
        n = 3
        r = 5.0
        spacing = 22.0
        total = (n - 1) * spacing
        x0 = (w - total) / 2
        y0 = h / 2 - r
        for i in range(n):
            phase_offset = i * (math.pi * 2 / 3)
            dy = math.sin(self._phase * 0.14 + phase_offset) * 6.0
            alpha = 0.5 + 0.5 * abs(math.sin(self._phase * 0.14 + phase_offset))
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.0, 0.784, 0.588, alpha).setFill()
            cx = x0 + i * spacing
            cy = y0 + dy
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - r, cy - r, r * 2, r * 2)
            ).fill()

    @objc.python_method
    def _drawHint(self, w, text):
        style = AppKit.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AppKit.NSTextAlignmentCenter)
        attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(10.5),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithRed_green_blue_alpha_(*_MUTED),
            AppKit.NSParagraphStyleAttributeName: style,
        }
        s = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        s.drawInRect_(NSMakeRect(0, 13, w, 18))

    @objc.python_method
    def setStatus(self, status):
        self._status = status
        self.setNeedsDisplay_(True)

    def tick_(self, timer):
        self._phase += 1
        self.setNeedsDisplay_(True)


class RecordingOverlay:
    def __init__(self):
        W, H = 360, 148
        screen = AppKit.NSScreen.mainScreen().frame()
        x = (screen.size.width - W) / 2
        y = screen.size.height * 0.65

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
        y = screen.size.height * 0.65
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
