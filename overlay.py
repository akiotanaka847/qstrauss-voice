"""
overlay.py — Floating recording overlay for QStrauss Voice (macOS)
Uses NSPanel + NSNonactivatingPanelMask so it floats without activating the app.
"""
import math
import objc
import AppKit
from Foundation import NSMakeRect, NSTimer


# NSPanel-specific style mask: shows panel without activating owning app
NSNonactivatingPanelMask = 1 << 7


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

        bg = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, 16.0, 16.0
        )
        AppKit.NSColor.colorWithRed_green_blue_alpha_(
            0.11, 0.18, 0.11, 0.95
        ).setFill()
        bg.fill()

        style = AppKit.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AppKit.NSTextAlignmentCenter)

        titleAttrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(20.0, 0.5),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(),
            AppKit.NSParagraphStyleAttributeName: style,
        }
        title = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "QStrauss", titleAttrs
        )
        title.drawInRect_(NSMakeRect(0, h - 48, w, 30))

        if self._status == "listening":
            self._drawDots(w, h)
            self._drawHint(w, "Press hotkey again to stop")
        else:
            self._drawLabel(w, h, "Transcribiendo...")
            self._drawHint(w, "Un momento...")

    @objc.python_method
    def _drawDots(self, w, h):
        AppKit.NSColor.colorWithRed_green_blue_alpha_(0.40, 0.85, 0.40, 1.0).setFill()
        n, r = 30, 2.5
        totalW = w - 60
        gap = totalW / (n - 1)
        y0 = h / 2 - 2
        for i in range(n):
            dy = math.sin(self._phase * 0.12 + i * 0.35) * 4.0
            cx = 30 + i * gap
            cy = y0 + dy
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - r, cy - r, r * 2, r * 2)
            ).fill()

    @objc.python_method
    def _drawLabel(self, w, h, text):
        style = AppKit.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AppKit.NSTextAlignmentCenter)
        attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(14.0),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithRed_green_blue_alpha_(0.6, 0.85, 0.6, 1.0),
            AppKit.NSParagraphStyleAttributeName: style,
        }
        s = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        s.drawInRect_(NSMakeRect(0, h / 2 - 10, w, 25))

    @objc.python_method
    def _drawHint(self, w, text):
        style = AppKit.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AppKit.NSTextAlignmentCenter)
        attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11.0),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithRed_green_blue_alpha_(0.5, 0.65, 0.5, 0.7),
            AppKit.NSParagraphStyleAttributeName: style,
        }
        s = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        s.drawInRect_(NSMakeRect(0, 12, w, 20))

    @objc.python_method
    def setStatus(self, status):
        self._status = status
        self.setNeedsDisplay_(True)

    def tick_(self, timer):
        self._phase += 1
        self.setNeedsDisplay_(True)


class RecordingOverlay:
    def __init__(self):
        W, H = 360, 140
        screen = AppKit.NSScreen.mainScreen().frame()
        x = (screen.size.width - W) / 2
        y = screen.size.height * 0.65

        # NSPanel + NonactivatingPanelMask = floats without activating the app
        self._panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H),
            AppKit.NSWindowStyleMaskBorderless | NSNonactivatingPanelMask,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._panel.setLevel_(25)  # CGPopUpMenuWindowLevel — above all normal windows
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
        # orderFront WITHOUT activating — panel-specific behavior
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
