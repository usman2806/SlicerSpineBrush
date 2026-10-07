"""
"Spine brush" - a Segment Editor effect for bone tracing on CT.

One effect that is both a brush and an eraser. You set one number, the bone
threshold (about 200 HU). The effect writes the Editable intensity range for
you, and flips it when you switch:

    Brush  ->  200 .. top of the scan     paint only lands on bone
    Eraser ->  bottom of the scan .. 200  only soft tissue gets wiped

While the effect is active:
    e  swaps brush and eraser
    r  turns the bone threshold on and off
"""

import contextlib
import logging
import math
import os

import ctk
import qt
import slicer
import vtk

from SegmentEditorEffects import *


EFFECT_NAME = "Spine brush"

PARAM_THRESHOLD = "SpineBrushThreshold"
PARAM_ERASE = "SpineBrushErase"
PARAM_USE_THRESHOLD = "SpineBrushUseThreshold"
PARAM_CARRY = "SpineBrushCarryStroke"

DEFAULT_THRESHOLD = 200.0
# Single key, and deliberately "e" for eraser. Slicer already uses q and w for
# previous/next segment, so q-w-e sit together under the left hand. None of
# Slicer's own Segment Editor shortcuts use "e" (they take 0-9, Esc, Space,
# z, y, q, w, / * , . < > and i), so there is no clash.
# Avoid Tab here: Qt uses it to move focus between controls, so grabbing it
# breaks keyboard navigation and it often never reaches a shortcut at all.
TOGGLE_SHORTCUT = "e"        # brush <-> eraser
MASK_SHORTCUT = "r"          # bone threshold on / off
PREV_SHORTCUT = "a"          # go one slice back
NEXT_SHORTCUT = "d"          # go one slice forward
COPY_SHORTCUT = "c"          # copy the previous slice onto this one
FILL_SHORTCUT = "f"          # fill the closed outline under the pointer
KEY_DEFAULTS = {"swap": TOGGLE_SHORTCUT, "mask": MASK_SHORTCUT, "prev": PREV_SHORTCUT,
                "next": NEXT_SHORTCUT, "copy": COPY_SHORTCUT, "fill": FILL_SHORTCUT}

# keys typed into these should never trigger the swap
TEXT_ENTRY_CLASSES = ("QLineEdit", "QAbstractSpinBox", "QTextEdit", "QPlainTextEdit")
FALLBACK_RANGE = (-1024.0, 3071.0)
CARRY_IDLE_MS = 3000          # a carried stroke with no activity this long is dropped
PREVIEW_MS = 30               # how often the live stroke preview is refreshed
PREVIEW_MAX_POINTS = 20000
PREVIEW_OPACITY = 0.10
PREVIEW_BRUSH_COLOR = (0.10, 0.65, 1.00)    # blue   = painting
PREVIEW_ERASER_COLOR = (1.00, 0.25, 0.20)   # red    = erasing


# --- Slicer renamed "master volume" to "source volume" in 5.2 ---------------

def _sourceVolumeNode(node):
    if hasattr(node, "GetSourceVolumeNode"):
        return node.GetSourceVolumeNode()
    return node.GetMasterVolumeNode()


def _setMaskEnabled(node, enabled):
    if hasattr(node, "SetSourceVolumeIntensityMask"):
        node.SetSourceVolumeIntensityMask(enabled)
    else:
        node.SetMasterVolumeIntensityMask(enabled)


def _setMaskRange(node, low, high):
    if hasattr(node, "SetSourceVolumeIntensityMaskRange"):
        node.SetSourceVolumeIntensityMaskRange(low, high)
    else:
        node.SetMasterVolumeIntensityMaskRange(low, high)



# --- carrying a stroke across slices: plain numpy helpers --------------------
#
# A labelmap array has shape (nk, nj, ni): array axis 2-a belongs to image axis
# a (0 = i, 1 = j, 2 = k). "ext" is the VTK extent (i0,i1,j0,j1,k0,k1).

def _other_axes(axis):
    p, q = [a for a in (0, 1, 2) if a != axis]
    return p, q                      # p < q


def _slab(ext, ranges):
    """Index into the labelmap array. ranges = {imageAxis: (lo, hi)}, inclusive."""
    index = [slice(None)] * 3
    for a, (lo, hi) in ranges.items():
        index[2 - a] = slice(lo - ext[2 * a], hi - ext[2 * a] + 1)
    return tuple(index)


def _batch_plane(arr, ext, box, axis):
    """Footprint of one paint batch squashed onto its slice plane.

    Returns a bool array shaped (q range, p range).
    """
    import numpy as np
    ranges = {a: (box[2 * a], box[2 * a + 1]) for a in range(3)}
    sub = arr[_slab(ext, ranges)]
    return np.asarray(sub > 0).any(axis=2 - axis)


def _stamp_plane(arr, ext, axis, sliceIndex, plane, pqBox, fill):
    """Write a plane footprint into one slice of the labelmap array."""
    import numpy as np
    p, q = _other_axes(axis)
    p0, p1, q0, q1 = pqBox
    view = arr[_slab(ext, {axis: (sliceIndex, sliceIndex), q: (q0, q1), p: (p0, p1)})]
    flat = np.expand_dims(plane, 2 - axis)
    view[...] = np.maximum(view, flat.astype(arr.dtype) * arr.dtype.type(fill))


# -- live stroke preview helpers (plain python/numpy, easy to test) ----------

def _interp_points(last, new, spacing):
    """Points from `last` (not included) to `new` (included), at most `spacing` apart."""
    d = [new[i] - last[i] for i in range(3)]
    dist = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
    n = max(1, int(math.ceil(dist / spacing))) if spacing > 0 else 1
    return [tuple(last[i] + d[i] * k / float(n) for i in range(3)) for k in range(1, n + 1)]


def _project_points(points, rasToXY):
    """RAS points (N x 3) -> view pixel coordinates (N x 2), rasToXY is a 4x4 numpy array."""
    import numpy as np
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    homogeneous = np.hstack([pts, np.ones((pts.shape[0], 1))])
    return (homogeneous @ np.asarray(rasToXY, dtype=float).T)[:, :2]


class _Preview(object):
    """The coloured live stroke drawn over one slice view while the mouse is down."""

    def __init__(self, viewName, sliceWidget):
        self.viewName = viewName
        self.sliceWidget = sliceWidget
        self.points = []             # RAS points of the stroke so far
        self.signature = None
        self.actor = None
        self.polyData = None
        self.circle = None


# -- slice tools helpers (plain numpy) ---------------------------------------

def _flood_region(painted, seed):
    """Empty area around `seed` that is closed in by painted voxels.

    painted: 2D bool array. seed: (row, col). Returns a bool array of the
    enclosed area, or None if the area is open, or the seed is painted.
    A closed area always lies inside the box around the painted voxels, so only
    that box is searched - an open area is rejected at once.
    """
    import numpy as np
    painted = np.asarray(painted, dtype=bool)
    if not painted.any() or painted[seed]:
        return None
    rows = np.nonzero(painted.any(axis=1))[0]
    cols = np.nonzero(painted.any(axis=0))[0]
    r0, r1, c0, c1 = rows[0], rows[-1], cols[0], cols[-1]
    if not (r0 < seed[0] < r1 and c0 < seed[1] < c1):
        return None                        # outside the outline's box: open
    # crop with a one-voxel margin; touching that margin means the area leaks
    r0, c0 = max(r0 - 1, 0), max(c0 - 1, 0)
    r1, c1 = min(r1 + 1, painted.shape[0] - 1), min(c1 + 1, painted.shape[1] - 1)
    crop = painted[r0:r1 + 1, c0:c1 + 1]
    empty = ~crop
    local = (seed[0] - r0, seed[1] - c0)
    try:
        from scipy import ndimage
        labels, _count = ndimage.label(empty)
        region = labels == labels[local]
        leaks = (region[0, :].any() or region[-1, :].any()
                 or region[:, 0].any() or region[:, -1].any())
    except ImportError:
        region = np.zeros_like(empty)
        region[local] = True
        count, leaks = 1, False
        while True:
            grown = region.copy()
            grown[1:, :] |= region[:-1, :]
            grown[:-1, :] |= region[1:, :]
            grown[:, 1:] |= region[:, :-1]
            grown[:, :-1] |= region[:, 1:]
            grown &= empty
            if (grown[0, :].any() or grown[-1, :].any()
                    or grown[:, 0].any() or grown[:, -1].any()):
                leaks = True
                break
            newCount = int(grown.sum())
            region = grown
            if newCount == count:
                break
            count = newCount
    if leaks:
        return None
    out = np.zeros(painted.shape, dtype=bool)
    out[r0:r1 + 1, c0:c1 + 1] = region
    return out


def _embed_plane(sub, subExt, ext, axis):
    """Put a 2D slice taken from an array with extent `subExt` into a full
    (q rows, p columns) plane of a labelmap with extent `ext`."""
    import numpy as np
    p, q = _other_axes(axis)
    full = np.zeros((ext[2 * q + 1] - ext[2 * q] + 1,
                     ext[2 * p + 1] - ext[2 * p] + 1), dtype=bool)
    q0, p0 = subExt[2 * q] - ext[2 * q], subExt[2 * p] - ext[2 * p]
    # clip to the overlap
    qa, pa = max(q0, 0), max(p0, 0)
    qb = min(q0 + sub.shape[0], full.shape[0])
    pb = min(p0 + sub.shape[1], full.shape[1])
    if qb > qa and pb > pa:
        full[qa:qb, pa:pb] = sub[qa - q0:qb - q0, pa - p0:pb - p0] > 0
    return full


def _plane_of(arr, ext, axis, sliceIndex):
    """One slice of a labelmap-shaped array as a 2D array (q rows, p columns)."""
    return arr[_slab(ext, {axis: (sliceIndex, sliceIndex)})].squeeze(axis=2 - axis)


class _CarryState(object):
    """Everything about the stroke that is being carried across slices."""

    def __init__(self, view, axis, ext):
        self.view = view
        self.axis = axis
        self.p, self.q = _other_axes(axis)
        self.mask = None             # bool (nq, np): union of the whole stroke
        self.shape = (ext[2 * self.q + 1] - ext[2 * self.q] + 1,
                      ext[2 * self.p + 1] - ext[2 * self.p] + 1)
        self.box = None              # [p0, p1, q0, q1] of what is in mask
        self.visited = set()
        self.lastMS = 0
        self.ext = tuple(ext)
        self.worldToImage = None
        self.nodeID = None


class SegmentEditorEffect(AbstractScriptedSegmentEditorPaintEffect):

    def __init__(self, scriptedEffect):
        scriptedEffect.name = EFFECT_NAME
        # The C++ paint effect sets its own title ("Paint"), and title() only
        # falls back to name() when the title is empty - so set it explicitly
        # or the options panel is headed "Paint".
        try:
            scriptedEffect.title = EFFECT_NAME
        except Exception:
            pass
        scriptedEffect.perSegment = True
        scriptedEffect.requireSegments = True
        AbstractScriptedSegmentEditorPaintEffect.__init__(self, scriptedEffect)
        self.shortcuts = []
        self._updatingGUI = False
        self._applyingMask = False
        self.banners = {}                # view name -> (sliceWidget, vtkTextActor)
        self._paramObserverTag = None
        self._observedParamNode = None
        self._carry = None               # stroke being carried across slices
        self._watch = {}                 # slice node id -> {node, tag, last}
        self._strokeOrigins = {}         # slice node id -> [(x,y,z,ms), ...] seen with the button down
        self._carryLog = []              # last few carry decisions, for diagnosis
        self._previewTimer = None
        self._preview = None             # _Preview of the stroke in progress
        self.keyEdits = {}               # 'prev'/'next'/'copy'/'fill' -> QLineEdit
        self._cameFrom = {}              # slice node id -> origin of the slice we left
        self._lastViewName = None        # last slice view the pointer was over
        self._wasDown = False
        self._blocked = False            # button went down outside a slice view

    # -- boilerplate --------------------------------------------------------

    def clone(self):
        import qSlicerSegmentationsEditorEffectsPythonQt as effects
        clonedEffect = effects.qSlicerSegmentEditorScriptedPaintEffect(None)
        clonedEffect.setPythonSource(__file__.replace("\\", "/"))
        return clonedEffect

    def icon(self):
        """The icon in the effects grid.

        Slicer only reads this when it builds the button, so the live swap is
        done in updateEffectButtonIcon. Returning the current mode here keeps
        the two in step if the grid is ever rebuilt.
        """
        try:
            return self.iconFor(self.isErase())
        except Exception:
            return self.iconFor(False)

    def iconFor(self, erase):
        here = os.path.dirname(__file__)
        names = (["SegmentEditorEffect-erase.png"] if erase
                 else ["SegmentEditorEffect-paint.png"])
        names.append("SegmentEditorEffect.png")      # fallback
        for name in names:
            path = os.path.join(here, name)
            if os.path.exists(path):
                icon = qt.QIcon(path)
                if not icon.isNull():
                    return icon
        return qt.QIcon()

    def _collectToolButtons(self, widget, found, depth=0):
        """Plain QObject walk - does not depend on findChildren behaviour."""
        if widget is None or depth > 14:
            return
        try:
            children = widget.children()
        except Exception:
            return
        for child in children:
            try:
                if child.inherits("QToolButton"):
                    found.append(child)
            except Exception:
                pass
            self._collectToolButtons(child, found, depth + 1)

    def effectButtons(self):
        """This effect's button(s) in the Segment Editor grid.

        Three ways to recognise it, most reliable first:
          1. the button carries a pointer to its effect in a Qt property
          2. Slicer sets the button's objectName to the effect's name
          3. the visible label, which is title() and can be translated
        """
        matches = []
        mainWindow = slicer.util.mainWindow()
        if mainWindow is None:
            return matches

        candidates = []
        self._collectToolButtons(mainWindow, candidates)
        if not candidates:
            try:
                candidates = slicer.util.findChildren(
                    mainWindow, className="QToolButton")
            except Exception:
                return matches

        for button in candidates:
            try:
                effect = button.property("Effect")
                if effect is not None and effect.name == EFFECT_NAME:
                    matches.append(button)
                    continue
            except Exception:
                pass
            try:
                if button.objectName == EFFECT_NAME:
                    matches.append(button)
                    continue
            except Exception:
                pass
            try:
                if button.text.strip() == EFFECT_NAME:
                    matches.append(button)
            except Exception:
                continue
        return matches

    def describeButtons(self):
        """Diagnostic: run from the Python Interactor to see what was found."""
        candidates = []
        self._collectToolButtons(slicer.util.mainWindow(), candidates)
        lines = ["tool buttons found: %d" % len(candidates)]
        for button in candidates:
            try:
                effect = button.property("Effect")
                effectName = effect.name if effect is not None else "-"
            except Exception:
                effectName = "?"
            try:
                lines.append("  objectName=%r text=%r effect=%r"
                             % (button.objectName, button.text, effectName))
            except Exception:
                lines.append("  <unreadable>")
        lines.append("matched for %r: %d" % (EFFECT_NAME, len(self.effectButtons())))
        return "\n".join(lines)

    def updateEffectButtonIcon(self, erase=None):
        """Show the active mode on the effect button.

        Two signals, because swapping a QToolButton icon after the button has
        been built does not repaint on every Qt build:
          1. the icon itself (brush / eraser)
          2. a red tint on the button, which stylesheets apply reliably
        Errors are logged, not swallowed, so a failure is visible in the log.
        """
        if erase is None:
            erase = self.isErase()
        icon = self.iconFor(erase)
        buttons = self.effectButtons()
        if not buttons:
            logging.debug("Spine brush: effect button not found, no icon swap")
            return

        for button in buttons:
            try:
                button.setIcon(icon)
                button.setIconSize(icon.availableSizes()[0]
                                   if icon.availableSizes() else qt.QSize(21, 21))
            except Exception as exc:
                logging.warning("Spine brush: setIcon failed (%s)" % exc)
            try:
                if erase:
                    button.setStyleSheet(
                        "QToolButton { background-color: #f4c2c2;"
                        " border: 2px solid #b83a3a; border-radius: 4px; }")
                else:
                    button.setStyleSheet("")
            except Exception as exc:
                logging.warning("Spine brush: tint failed (%s)" % exc)
            try:
                button.update()
                button.repaint()
            except Exception:
                pass

    def iconReport(self):
        """Diagnostic: is the icon actually loading, and does the button take it?"""
        lines = []
        here = os.path.dirname(__file__)
        lines.append("folder: %s" % here)
        for name in ("SegmentEditorEffect-paint.png",
                     "SegmentEditorEffect-erase.png",
                     "SegmentEditorEffect.png"):
            path = os.path.join(here, name)
            exists = os.path.exists(path)
            null = True
            sizes = []
            if exists:
                try:
                    testIcon = qt.QIcon(path)
                    null = testIcon.isNull()
                    sizes = ["%dx%d" % (s.width(), s.height())
                             for s in testIcon.availableSizes()]
                except Exception as exc:
                    sizes = ["error: %s" % exc]
            lines.append("  %-32s exists=%s null=%s sizes=%s"
                         % (name, exists, null, sizes))

        buttons = self.effectButtons()
        lines.append("buttons matched: %d" % len(buttons))
        for button in buttons:
            try:
                lines.append("  before: iconNull=%s" % button.icon.isNull())
                button.setIcon(self.iconFor(True))
                button.setStyleSheet(
                    "QToolButton { background-color: #f4c2c2;"
                    " border: 2px solid #b83a3a; border-radius: 4px; }")
                button.repaint()
                lines.append("  after forcing eraser: iconNull=%s"
                             % button.icon.isNull())
                lines.append("  -> the button should now be tinted red")
            except Exception as exc:
                lines.append("  FAILED: %s" % exc)
        return "\n".join(lines)

    def helpText(self):
        return (
            "<html>Brush and eraser in one, for tracing bone."
            "<br><br>"
            "<b>Brush</b> paints only where the image is brighter than the bone "
            "threshold. <b>Eraser</b> removes only where it is darker, so bone you "
            "have already traced is safe."
            "<br><br>"
            "Press <b>%s</b> to swap between them, and <b>%s</b> to turn the "
            "threshold off for plain painting with no limit."
            "<br><br>"
            "<b>Scroll while painting:</b> hold the left button and scroll the "
            "wheel. The stroke is repeated on every slice you pass."
            "<br><br>"
            "<b>%s / %s</b> go back and forward one slice. <b>%s</b> copies the "
            "slice you just left onto this one. <b>%s</b> fills the closed "
            "outline under the pointer.</html>"
            % (TOGGLE_SHORTCUT, MASK_SHORTCUT, PREV_SHORTCUT, NEXT_SHORTCUT,
               COPY_SHORTCUT, FILL_SHORTCUT)
        )

    # -- options panel ------------------------------------------------------

    def setupOptionsFrame(self):
        # Do NOT call the Python base class here. It does not define
        # setupOptionsFrame, so the call raises AttributeError and silently
        # kills the rest of this method. The C++ side has already built the
        # standard brush controls (diameter, sphere, smudge) before calling us.
        try:
            self.scriptedEffect.setColorSmudgeCheckboxVisible(False)
        except Exception:
            pass

        # big mode read-out
        self.modeLabel = qt.QLabel("BRUSH")
        font = self.modeLabel.font
        font.setBold(True)
        font.setPointSize(font.pointSize() + 3)
        self.modeLabel.setFont(font)
        self.modeLabel.setAlignment(qt.Qt.AlignCenter)
        self.modeLabel.setMinimumHeight(30)
        self.scriptedEffect.addOptionsWidget(self.modeLabel)

        # brush / eraser
        self.paintButton = qt.QPushButton("Brush")
        self.paintButton.setCheckable(True)
        self.paintButton.setMinimumHeight(28)
        self.eraseButton = qt.QPushButton("Eraser")
        self.eraseButton.setCheckable(True)
        self.eraseButton.setMinimumHeight(28)
        self.eraseButton.toolTip = "%s swaps between the two" % TOGGLE_SHORTCUT
        self.paintButton.toolTip = self.eraseButton.toolTip

        modeLayout = qt.QHBoxLayout()
        modeLayout.addWidget(self.paintButton)
        modeLayout.addWidget(self.eraseButton)
        self.scriptedEffect.addOptionsWidget(modeLayout)

        # threshold
        self.useThresholdCheckBox = qt.QCheckBox("Use bone threshold")
        self.useThresholdCheckBox.checked = True
        self.useThresholdCheckBox.toolTip = (
            "Off means plain paint and erase, with no intensity limit."
        )
        self.scriptedEffect.addOptionsWidget(self.useThresholdCheckBox)

        self.carryCheckBox = qt.QCheckBox("Scroll while painting: copy the stroke to each slice")
        self.carryCheckBox.checked = True
        self.carryCheckBox.toolTip = (
            "Hold the left mouse button and scroll the mouse wheel. Whatever you "
            "have painted (or erased) in this stroke is repeated on every slice "
            "you scroll through, so one stroke can cover a stretch of bone.\n\n"
            "The bone threshold still applies on every slice. Works for the "
            "brush and the eraser."
        )
        self.scriptedEffect.addOptionsWidget(self.carryCheckBox)

        self.thresholdSlider = ctk.ctkSliderWidget()
        self.thresholdSlider.minimum = FALLBACK_RANGE[0]
        self.thresholdSlider.maximum = FALLBACK_RANGE[1]
        self.thresholdSlider.decimals = 0
        self.thresholdSlider.singleStep = 5.0
        self.thresholdSlider.pageStep = 50.0
        self.thresholdSlider.value = DEFAULT_THRESHOLD
        self.thresholdSlider.suffix = " HU"
        self.thresholdSlider.toolTip = "Bone starts here."

        thresholdLayout = qt.QHBoxLayout()
        thresholdLayout.addWidget(qt.QLabel("Threshold:"))
        thresholdLayout.addWidget(self.thresholdSlider)
        self.scriptedEffect.addOptionsWidget(thresholdLayout)

        self.rangeLabel = qt.QLabel("")
        self.rangeLabel.setStyleSheet("color: #444;")
        self.rangeLabel.setAlignment(qt.Qt.AlignCenter)
        self.scriptedEffect.addOptionsWidget(self.rangeLabel)

        # which segment am I painting into?
        self.segmentSwatch = qt.QLabel("")
        self.segmentSwatch.setFixedSize(16, 16)
        self.segmentSwatch.setStyleSheet(
            "background-color: #999; border: 1px solid #444; border-radius: 3px;")
        self.segmentLabel = qt.QLabel("no segment selected")
        self.segmentLabel.setStyleSheet("font-weight: bold;")

        segmentLayout = qt.QHBoxLayout()
        segmentLayout.addWidget(qt.QLabel("Working on:"))
        segmentLayout.addWidget(self.segmentSwatch)
        segmentLayout.addWidget(self.segmentLabel)
        segmentLayout.addStretch(1)
        self.scriptedEffect.addOptionsWidget(segmentLayout)

        self.showBannerCheckBox = qt.QCheckBox("Show it in the slice views too")
        self.showBannerCheckBox.checked = True
        self.showBannerCheckBox.toolTip = (
            "Puts the segment name in the corner of each slice view, in that "
            "segment's own colour, so you do not have to look away from the image."
        )
        self.scriptedEffect.addOptionsWidget(self.showBannerCheckBox)

        # slice tools
        sliceTitle = qt.QLabel("Slice tools")
        sliceTitle.setStyleSheet("font-weight: bold; margin-top: 6px;")
        self.scriptedEffect.addOptionsWidget(sliceTitle)

        self.prevSliceButton = qt.QPushButton("< Previous slice")
        self.nextSliceButton = qt.QPushButton("Next slice >")
        self.copySliceButton = qt.QPushButton("Copy previous slice")
        self.copySliceButton.toolTip = (
            "Copies the selected segment from the slice you just came from onto "
            "this slice. The bone threshold still applies, so only bone is kept.")
        sliceButtons = qt.QHBoxLayout()
        sliceButtons.addWidget(self.prevSliceButton)
        sliceButtons.addWidget(self.nextSliceButton)
        sliceButtons.addWidget(self.copySliceButton)
        self.scriptedEffect.addOptionsWidget(sliceButtons)

        self.fillHint = qt.QLabel(
            "Fill: put the pointer inside a closed outline and press the fill key.")
        self.fillHint.setStyleSheet("color: #555;")
        self.fillHint.setWordWrap(True)
        self.scriptedEffect.addOptionsWidget(self.fillHint)

        # the keys, editable
        keyHelp = (
            "Type one key (e, r, x) or a combination (Ctrl+E), then press Enter.\n\n"
            "Already taken by Slicer: 0-9, Esc, Space, z, y, q, w, i, / * , . < >\n"
            "Tab is a bad choice - Qt uses it to move between controls."
        )
        self.shortcutEdit = qt.QLineEdit(TOGGLE_SHORTCUT)
        self.shortcutEdit.setFixedWidth(60)
        self.shortcutEdit.toolTip = "Swaps brush and eraser.\n\n" + keyHelp

        self.maskShortcutEdit = qt.QLineEdit(MASK_SHORTCUT)
        self.maskShortcutEdit.setFixedWidth(60)
        self.maskShortcutEdit.toolTip = (
            "Turns the bone threshold on and off.\n\n" + keyHelp)

        self.shortcutHint = qt.QLabel("")
        self.shortcutHint.setStyleSheet("color: #555;")

        shortcutRow = qt.QHBoxLayout()
        shortcutRow.addWidget(qt.QLabel("Swap:"))
        shortcutRow.addWidget(self.shortcutEdit)
        shortcutRow.addSpacing(10)
        shortcutRow.addWidget(qt.QLabel("Threshold on/off:"))
        shortcutRow.addWidget(self.maskShortcutEdit)
        shortcutRow.addWidget(self.shortcutHint)
        shortcutRow.addStretch(1)
        self.scriptedEffect.addOptionsWidget(shortcutRow)

        # keys for the slice tools
        sliceKeyRow = qt.QHBoxLayout()
        for which, label, tip in (
                ("prev", "Back:", "Go one slice back."),
                ("next", "Forward:", "Go one slice forward."),
                ("copy", "Copy:", "Copy the previous slice onto this one."),
                ("fill", "Fill:", "Fill the closed outline under the pointer.")):
            edit = qt.QLineEdit(KEY_DEFAULTS[which])
            edit.setFixedWidth(40)
            edit.toolTip = tip + "\n\n" + keyHelp
            edit.connect("editingFinished()", self.installShortcuts)
            self.keyEdits[which] = edit
            sliceKeyRow.addWidget(qt.QLabel(label))
            sliceKeyRow.addWidget(edit)
            sliceKeyRow.addSpacing(6)
        sliceKeyRow.addStretch(1)
        self.scriptedEffect.addOptionsWidget(sliceKeyRow)

        self.shortcutEdit.connect("editingFinished()", self.installShortcuts)
        self.maskShortcutEdit.connect("editingFinished()", self.installShortcuts)
        self.prevSliceButton.connect("clicked()", lambda: self.stepSlice(-1))
        self.nextSliceButton.connect("clicked()", lambda: self.stepSlice(+1))
        self.copySliceButton.connect("clicked()", self.copyPreviousSlice)

        # connections
        self.paintButton.connect("clicked()", lambda: self.setEraseMode(False))
        self.eraseButton.connect("clicked()", lambda: self.setEraseMode(True))
        self.useThresholdCheckBox.connect("toggled(bool)", self.onGuiChanged)
        self.carryCheckBox.connect("toggled(bool)", self.onGuiChanged)
        self.thresholdSlider.connect("valueChanged(double)", self.onGuiChanged)
        self.showBannerCheckBox.connect("toggled(bool)", self.onBannerToggled)

    # -- which segment is selected ------------------------------------------

    def selectedSegment(self):
        """Return (name, (r, g, b)) of the selected segment, or (None, None)."""
        try:
            node = self.scriptedEffect.parameterSetNode()
            if node is None:
                return None, None
            segmentationNode = node.GetSegmentationNode()
            segmentID = node.GetSelectedSegmentID()
            if segmentationNode is None or not segmentID:
                return None, None
            segment = segmentationNode.GetSegmentation().GetSegment(segmentID)
            if segment is None:
                return None, None
            return segment.GetName(), segment.GetColor()
        except Exception:
            return None, None

    def updateSegmentFeedback(self):
        """Refresh the panel label and the in-view banners."""
        name, color = self.selectedSegment()

        if name is None:
            self.segmentLabel.text = "no segment selected"
            self.segmentLabel.setStyleSheet("font-weight: bold; color: #a05000;")
            self.segmentSwatch.setStyleSheet(
                "background-color: #999; border: 1px solid #444; border-radius: 3px;")
        else:
            self.segmentLabel.text = name
            self.segmentLabel.setStyleSheet("font-weight: bold; color: #222;")
            self.segmentSwatch.setStyleSheet(
                "background-color: rgb(%d,%d,%d); border: 1px solid #444;"
                " border-radius: 3px;"
                % (color[0] * 255, color[1] * 255, color[2] * 255))

        self.updateBanners(name, color)

    # -- the banner drawn inside the slice views ----------------------------

    def bannerText(self, name):
        mode = "ERASER" if self.isErase() else "BRUSH"
        if name is None:
            return "%s  -  no segment selected" % mode
        return "%s  >  %s" % (mode, name)

    def refreshCursor(self):
        """Make the mouse cursor show the current mode (brush or eraser).

        The Segment Editor builds the cursor (arrow + this effect's icon) once,
        when the effect is activated, so it keeps showing the old icon after a
        switch. Building it again here picks up icon(), which follows the mode.
        """
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        for viewName in layoutManager.sliceViewNames():
            try:
                sliceWidget = layoutManager.sliceWidget(viewName)
                cursor = self.scriptedEffect.createCursor(sliceWidget)
                # Slicer sets both: the cursor in use now, and the one it
                # falls back to after hovering over a markup.
                sliceWidget.sliceView().setViewCursor(cursor)
                sliceWidget.sliceView().setDefaultViewCursor(cursor)
            except Exception as exc:
                logging.warning("Spine brush: cursor not updated in %s (%s)" % (viewName, exc))

    def showBanners(self):
        self.removeBanners()
        if not self.showBannerCheckBox.checked:
            return
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        for viewName in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(viewName)
            if sliceWidget is None:
                continue
            actor = vtk.vtkTextActor()
            actor.SetTextScaleModeToNone()
            textProperty = actor.GetTextProperty()
            textProperty.SetFontSize(15)
            textProperty.SetBold(True)
            textProperty.ShadowOn()
            textProperty.SetJustificationToLeft()

            # anchor: top-left corner of the view; the text hangs down from it.
            anchor = vtk.vtkCoordinate()
            anchor.SetCoordinateSystemToNormalizedViewport()
            anchor.SetValue(0.02, 0.97)
            actor.GetPositionCoordinate().SetCoordinateSystemToDisplay()
            actor.GetPositionCoordinate().SetReferenceCoordinate(anchor)
            actor.SetPosition(0, -20)

            try:
                self.scriptedEffect.addActor2D(sliceWidget, actor)
            except Exception as exc:
                logging.debug("Spine brush: no banner in %s (%s)" % (viewName, exc))
                continue
            self.banners[viewName] = (sliceWidget, actor)
        self.updateSegmentFeedback()

    def removeBanners(self):
        for viewName, (sliceWidget, actor) in list(self.banners.items()):
            try:
                self.scriptedEffect.removeActor2D(sliceWidget, actor)
                self.scriptedEffect.forceRender(sliceWidget)
            except Exception:
                pass
        self.banners = {}

    def updateBanners(self, name=None, color=None):
        if not self.banners:
            return
        if name is None and color is None:
            name, color = self.selectedSegment()
        text = self.bannerText(name)

        if color is None:
            r, g, b = 0.85, 0.85, 0.85
        else:
            # lift dark segment colours so they stay readable over grey CT
            r, g, b = color
            brightest = max(r, g, b, 0.001)
            if brightest < 0.6:
                factor = 0.6 / brightest
                r, g, b = min(r * factor, 1.0), min(g * factor, 1.0), min(b * factor, 1.0)

        for viewName, (sliceWidget, actor) in self.banners.items():
            try:
                actor.SetInput(text)
                actor.GetTextProperty().SetColor(r, g, b)
                self.scriptedEffect.forceRender(sliceWidget)
            except Exception:
                continue

    def onBannerToggled(self, checked):
        if checked:
            self.showBanners()
        else:
            self.removeBanners()

    # -- follow the segment editor node so the feedback stays current -------

    def observeParameterNode(self):
        self.unobserveParameterNode()
        node = self.scriptedEffect.parameterSetNode()
        if node is None:
            return
        try:
            self._paramObserverTag = node.AddObserver(
                vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)
            self._observedParamNode = node
        except Exception as exc:
            logging.debug("Spine brush: cannot watch the editor node (%s)" % exc)

    def unobserveParameterNode(self):
        if self._observedParamNode is not None and self._paramObserverTag is not None:
            try:
                self._observedParamNode.RemoveObserver(self._paramObserverTag)
            except Exception:
                pass
        self._paramObserverTag = None
        self._observedParamNode = None

    def onParameterNodeModified(self, caller=None, event=None):
        # selecting another segment in the list lands here
        try:
            self.updateSegmentFeedback()
        except Exception:
            pass

    # -- parameters ---------------------------------------------------------

    def setMRMLDefaults(self):
        # Same as setupOptionsFrame: no Python base call. The C++ side already
        # applied its own defaults before calling this.
        self.scriptedEffect.setParameterDefault(PARAM_THRESHOLD, DEFAULT_THRESHOLD)
        self.scriptedEffect.setParameterDefault(PARAM_ERASE, 0)
        self.scriptedEffect.setParameterDefault(PARAM_USE_THRESHOLD, 1)
        self.scriptedEffect.setParameterDefault(PARAM_CARRY, 1)

    def isErase(self):
        try:
            return self.scriptedEffect.integerParameter(PARAM_ERASE) != 0
        except Exception:
            return False

    def useThreshold(self):
        try:
            return self.scriptedEffect.integerParameter(PARAM_USE_THRESHOLD) != 0
        except Exception:
            return True

    def carryEnabled(self):
        try:
            return self.scriptedEffect.integerParameter(PARAM_CARRY) != 0
        except Exception:
            return True

    def threshold(self):
        try:
            return self.scriptedEffect.doubleParameter(PARAM_THRESHOLD)
        except Exception:
            return DEFAULT_THRESHOLD

    def volumeRange(self):
        node = self.scriptedEffect.parameterSetNode()
        volume = _sourceVolumeNode(node) if node else None
        if volume and volume.GetImageData():
            low, high = volume.GetImageData().GetScalarRange()
            return float(low), float(high)
        return FALLBACK_RANGE

    def maskRangeFor(self, erase):
        volLow, volHigh = self.volumeRange()
        value = self.threshold()
        if erase:
            return volLow, value
        return value, volHigh

    # -- keeping the Segment Editor's intensity mask in step ----------------

    def applyMask(self):
        node = self.scriptedEffect.parameterSetNode()
        if node is None or self._applyingMask:
            return
        self._applyingMask = True
        try:
            if not self.useThreshold():
                _setMaskEnabled(node, False)
                return
            low, high = self.maskRangeFor(self.isErase())
            _setMaskRange(node, low, high)
            _setMaskEnabled(node, True)
        except Exception as exc:
            logging.warning("Spine brush: could not set the intensity mask (%s)" % exc)
        finally:
            self._applyingMask = False

    def _refreshModeIcons(self, erase):
        self.updateEffectButtonIcon(erase)
        self.refreshCursor()

    def setEraseMode(self, erase):
        self.scriptedEffect.setParameter(PARAM_ERASE, 1 if erase else 0)
        self.applyMask()
        self.updateGUIFromMRML()
        # The Segment Editor refreshes its own buttons when the parameter node
        # changes, which can undo our icon a moment later. Put it back once
        # the dust has settled.
        self.refreshCursor()
        for delay in (0, 150):
            qt.QTimer.singleShot(
                delay, lambda e=bool(erase): self._refreshModeIcons(e))

    def toggleEraseMode(self):
        erase = not self.isErase()
        self.setEraseMode(erase)
        low, high = self.maskRangeFor(erase)
        if self.useThreshold():
            slicer.util.showStatusMessage(
                "Spine brush: %s  (%.0f to %.0f HU)"
                % ("ERASER" if erase else "BRUSH", low, high), 2000)
        else:
            slicer.util.showStatusMessage(
                "Spine brush: %s" % ("ERASER" if erase else "BRUSH"), 2000)

    # -- activate / deactivate ---------------------------------------------

    def activate(self):
        # widen the slider to whatever this scan actually holds
        low, high = self.volumeRange()
        self._updatingGUI = True
        try:
            self.thresholdSlider.minimum = low
            self.thresholdSlider.maximum = high
        finally:
            self._updatingGUI = False

        self.installShortcuts()
        self.observeParameterNode()
        self._startWatching()
        self._startPreviewTimer()
        self.showBanners()
        self.applyMask()
        self.updateGUIFromMRML()
        self.refreshCursor()

    def deactivate(self):
        self._stopPreviewTimer()
        self._stopWatching()
        self._endCarry()
        self.removeShortcuts()
        self.unobserveParameterNode()
        self.removeBanners()
        # put the grid icon back to the brush, so a red eraser icon does not
        # sit there while the effect is not even in use
        self.updateEffectButtonIcon(False)

    def shortcutKeyText(self, which):
        """which is 'swap' or 'mask'."""
        edit = {"swap": getattr(self, "shortcutEdit", None),
                "mask": getattr(self, "maskShortcutEdit", None)}.get(which)
        if edit is None:
            edit = self.keyEdits.get(which)
        fallback = KEY_DEFAULTS[which]
        try:
            text = edit.text.strip()
        except Exception:
            text = ""
        return text or fallback

    def installShortcuts(self):
        self.removeShortcuts()
        bad = []
        for which, action in (("swap", self.toggleEraseMode),
                              ("mask", self.toggleThreshold),
                              ("prev", lambda: self.stepSlice(-1)),
                              ("next", lambda: self.stepSlice(+1)),
                              ("copy", self.copyPreviousSlice),
                              ("fill", self.fillAtPointer)):
            keyText = self.shortcutKeyText(which)
            shortcut = self._makeShortcut(keyText, action)
            if shortcut is None:
                bad.append(keyText)
            else:
                self.shortcuts.append(shortcut)
        self.setShortcutHint(("not a key: " + ", ".join(bad)) if bad else "")

    def _makeShortcut(self, keyText, action):
        try:
            sequence = qt.QKeySequence(keyText)
            if sequence.isEmpty():
                return None
            shortcut = qt.QShortcut(slicer.util.mainWindow())
            shortcut.setKey(sequence)
            shortcut.setContext(qt.Qt.ApplicationShortcut)
            # default args bind the values now, not when the lambda runs
            shortcut.connect(
                "activated()",
                lambda key=keyText, run=action: self._runShortcut(key, run))
            return shortcut
        except Exception as exc:
            logging.warning("Spine brush: shortcut '%s' failed (%s)" % (keyText, exc))
            return None

    def removeShortcuts(self):
        for shortcut in self.shortcuts:
            try:
                shortcut.setParent(None)
                shortcut.deleteLater()
            except Exception:
                pass
        self.shortcuts = []

    def setShortcutHint(self, text):
        try:
            self.shortcutHint.text = text
            self.shortcutHint.setStyleSheet(
                "color: #a05000;" if text else "color: #555;")
        except Exception:
            pass

    def focusIsTextEntry(self):
        """True when the user is typing into a field, so the key is theirs."""
        try:
            focus = qt.QApplication.focusWidget()
            if focus is None:
                return False
            for className in TEXT_ENTRY_CLASSES:
                if focus.inherits(className):
                    return True
        except Exception:
            pass
        return False

    def _runShortcut(self, keyText, action):
        """Run the action, unless the key was meant for a text box."""
        if self.focusIsTextEntry():
            # hand the character back, so renaming a segment to "Vertebra"
            # does not silently lose every e
            try:
                focus = qt.QApplication.focusWidget()
                if len(keyText) == 1:
                    sequence = qt.QKeySequence(keyText)
                    event = qt.QKeyEvent(
                        qt.QEvent.KeyPress, sequence[0], qt.Qt.NoModifier, keyText)
                    qt.QApplication.sendEvent(focus, event)
            except Exception:
                pass
            return
        action()

    def toggleThreshold(self):
        use = not self.useThreshold()
        self.scriptedEffect.setParameter(PARAM_USE_THRESHOLD, 1 if use else 0)
        self.applyMask()
        self.updateGUIFromMRML()
        if use:
            low, high = self.maskRangeFor(self.isErase())
            slicer.util.showStatusMessage(
                "Spine brush: threshold ON  (%.0f to %.0f HU)" % (low, high), 2000)
        else:
            slicer.util.showStatusMessage(
                "Spine brush: threshold OFF - painting everywhere", 2000)

    # -- GUI <-> MRML -------------------------------------------------------

    def updateGUIFromMRML(self):
        if self._updatingGUI:
            return
        self._updatingGUI = True
        try:
            erase = self.isErase()
            useThreshold = self.useThreshold()

            self.paintButton.checked = not erase
            self.eraseButton.checked = erase
            self.useThresholdCheckBox.checked = useThreshold
            self.carryCheckBox.checked = self.carryEnabled()
            self.thresholdSlider.value = self.threshold()
            self.thresholdSlider.enabled = useThreshold

            if erase:
                self.modeLabel.text = "ERASER"
                self.modeLabel.setStyleSheet(
                    "background-color: #ffd9d9; color: #8b0000; border-radius: 5px;")
            else:
                self.modeLabel.text = "BRUSH"
                self.modeLabel.setStyleSheet(
                    "background-color: #d9f0ff; color: #004b7a; border-radius: 5px;")

            if useThreshold:
                low, high = self.maskRangeFor(erase)
                self.rangeLabel.text = "painting range  %.0f to %.0f HU" % (low, high)
            else:
                self.rangeLabel.text = "no intensity limit"

            self.updateEffectButtonIcon(erase)
        finally:
            self._updatingGUI = False

        # outside the guard: this only reads MRML and repaints the views
        self.updateSegmentFeedback()

    def updateMRMLFromGUI(self):
        if self._updatingGUI:
            return
        self.scriptedEffect.setParameter(
            PARAM_THRESHOLD, float(self.thresholdSlider.value))
        self.scriptedEffect.setParameter(
            PARAM_USE_THRESHOLD, 1 if self.useThresholdCheckBox.checked else 0)
        self.scriptedEffect.setParameter(
            PARAM_CARRY, 1 if self.carryCheckBox.checked else 0)

    def onGuiChanged(self, *args):
        if self._updatingGUI:
            return
        self.updateMRMLFromGUI()
        self.applyMask()
        self.updateGUIFromMRML()

    def sourceVolumeNodeChanged(self):
        low, high = self.volumeRange()
        self._updatingGUI = True
        try:
            self.thresholdSlider.minimum = low
            self.thresholdSlider.maximum = high
        finally:
            self._updatingGUI = False
        self.applyMask()
        self.updateGUIFromMRML()

    # -- the actual painting ------------------------------------------------

    def modificationMode(self):
        if self.isErase():
            return slicer.qSlicerSegmentEditorAbstractEffect.ModificationModeRemove
        return slicer.qSlicerSegmentEditorAbstractEffect.ModificationModeAdd

    def paintApply(self, viewWidget):
        """Same brush strokes as Paint, but add or remove depending on mode.

        The intensity limit is not applied here - it lives on the segment
        editor node as the Editable intensity range, and Slicer honours it
        inside modifySelectedSegmentByLabelmap.

        With "scroll while painting" on, the stroke is also stamped onto every
        slice visited since the mouse went down, in the same single update.
        """
        modifierLabelmap = self.scriptedEffect.defaultModifierLabelmap()
        maskExtent = self.scriptedEffect.paintBrushesIntoLabelmap(
            modifierLabelmap, viewWidget)
        self.scriptedEffect.clearBrushes()
        self.scriptedEffect.forceRender(viewWidget)

        if (maskExtent[0] > maskExtent[1]
                or maskExtent[2] > maskExtent[3]
                or maskExtent[4] > maskExtent[5]):
            if not self._leftDown():
                self._endCarry()
            return      # nothing was painted

        updateExtent = None
        if self.carryEnabled():
            try:
                updateExtent = self._carryBatch(viewWidget, modifierLabelmap, maskExtent)
            except Exception as exc:
                logging.warning("Spine brush: stroke carry failed (%s)" % exc)
                self._endCarry()
                updateExtent = None

        self.scriptedEffect.saveStateForUndo()
        if updateExtent is not None:
            self.scriptedEffect.modifySelectedSegmentByLabelmap(
                modifierLabelmap, self.modificationMode(), updateExtent)
        else:
            self.scriptedEffect.modifySelectedSegmentByLabelmap(
                modifierLabelmap, self.modificationMode())

        if not self._leftDown():
            self._endCarry()          # mouse released: the stroke is finished

    # -- carrying a stroke across slices ------------------------------------
    #
    # Slicer's paint effect may hold the whole stroke back and apply it only
    # when the mouse is released ("delayed paint"), so paintApply cannot be the
    # one that notices scrolling. Instead a watcher on every slice view records
    # each slice the view passes through while the left button is held, and
    # paintApply stamps the stroke onto all of them.

    CARRY_STALE_MS = 10000

    def _log(self, text):
        self._carryLog.append(text)
        del self._carryLog[:-40]
        logging.debug("Spine brush carry: " + text)

    def carryReport(self):
        """Diagnostic: run from the Python Interactor after a stroke."""
        lines = ["carry on: %s" % self.carryEnabled(),
                 "watching slice views: %d" % len(self._watch),
                 "left button down now: %s" % self._leftDown(),
                 "recorded origins: %s" % {k: len(v) for k, v in self._strokeOrigins.items()},
                 "--- last decisions ---"]
        return "\n".join(lines + self._carryLog)

    def _leftDown(self):
        try:
            buttons = qt.QApplication.mouseButtons()
            try:
                return bool(int(buttons) & int(qt.Qt.LeftButton))
            except Exception:
                return buttons == qt.Qt.LeftButton
        except Exception:
            return False

    @staticmethod
    def _nowMS():
        return qt.QTime.currentTime().msecsSinceStartOfDay()

    def _startWatching(self):
        self._stopWatching()
        try:
            nodes = slicer.util.getNodesByClass("vtkMRMLSliceNode")
        except Exception:
            nodes = []
        for node in nodes:
            try:
                origin = tuple(node.GetSliceToRAS().GetElement(r, 3) for r in range(3))
                tag = node.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onSliceNodeModified)
                self._watch[node.GetID()] = {"node": node, "tag": tag, "last": origin}
            except Exception as exc:
                logging.debug("Spine brush: cannot watch %s (%s)" % (node, exc))
        self._log("watching %d slice views" % len(self._watch))

    def _stopWatching(self):
        for entry in self._watch.values():
            try:
                entry["node"].RemoveObserver(entry["tag"])
            except Exception:
                pass
        self._watch = {}
        self._strokeOrigins = {}
        self._cameFrom = {}

    def _onSliceNodeModified(self, caller=None, event=None):
        """A slice view moved. If the left button is down, remember where."""
        try:
            nodeID = caller.GetID()
            entry = self._watch.get(nodeID)
            if entry is None:
                return
            m = caller.GetSliceToRAS()
            origin = tuple(m.GetElement(r, 3) for r in range(3))
            last = entry["last"]
            entry["last"] = origin
            moved = max(abs(origin[i] - last[i]) for i in range(3)) > 1e-4
            if not moved:
                return                      # zoom, pan, rotate: not a scroll
            self._cameFrom[nodeID] = last
            if not self.carryEnabled() or not self._leftDown():
                self._strokeOrigins.pop(nodeID, None)
                return
            now = self._nowMS()
            record = self._strokeOrigins.get(nodeID)
            if record and now - record[-1][3] > self.CARRY_STALE_MS:
                record = None
            if not record:
                record = [last + (now,)]    # where the stroke began
            record.append(origin + (now,))
            self._strokeOrigins[nodeID] = record
            self._log("scrolled with button down, %d positions" % len(record))
            slicer.util.showStatusMessage(
                "Spine brush: stroke will cover %d slices" % len(record), 1500)
            self._carryPaintNewSlice(nodeID, origin)
        except Exception as exc:
            logging.warning("Spine brush: slice watcher failed (%s)" % exc)

    def _labelArray(self, image):
        from vtk.util import numpy_support
        dims = image.GetDimensions()
        flat = numpy_support.vtk_to_numpy(image.GetPointData().GetScalars())
        return flat.reshape(dims[2], dims[1], dims[0])

    def _sliceAxisFor(self, viewWidget, image):
        """Which image axis the slice view scrolls along, or None if tilted."""
        sliceToRAS = viewWidget.sliceLogic().GetSliceNode().GetSliceToRAS()
        normal = [sliceToRAS.GetElement(r, 2) for r in range(3)] + [0.0]
        worldToImage = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(worldToImage)
        worldToImage.Invert()
        n = worldToImage.MultiplyPoint(normal)[:3]
        length = sum(c * c for c in n) ** 0.5
        if length == 0:
            return None
        axis = max(range(3), key=lambda a: abs(n[a]))
        if abs(n[axis]) / length < 0.95:
            self._log("slice view is tilted against the labelmap (%.2f)" % (abs(n[axis]) / length))
            return None
        return axis

    def _indexOfOrigin(self, state, origin):
        ijk = state.worldToImage.MultiplyPoint(list(origin[:3]) + [1.0])
        return int(round(ijk[state.axis]))

    def _beginCarry(self, viewWidget, image):
        axis = self._sliceAxisFor(viewWidget, image)
        if axis is None:
            slicer.util.showStatusMessage(
                "Spine brush: scroll-while-painting needs a straight slice view", 5000)
            return None
        state = _CarryState(viewWidget, axis, image.GetExtent())
        state.lastMS = self._nowMS()
        state.worldToImage = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(state.worldToImage)
        state.worldToImage.Invert()
        state.nodeID = viewWidget.sliceLogic().GetSliceNode().GetID()
        self._carry = state
        self._log("stroke started, axis %d" % axis)
        return state

    def _endCarry(self):
        self._carry = None
        self._strokeOrigins = {}

    def _carryBatch(self, viewWidget, image, extent):
        """Add this batch to the carried stroke; return the extent to update.

        Called from paintApply, before the segment is modified. The batch is
        already painted into `image`; here the same footprint is stamped onto
        every other slice visited since the left button went down.
        """
        if not hasattr(viewWidget, "sliceLogic"):
            return None                       # painting in a 3D view: nothing to carry
        ext = image.GetExtent()
        box = [max(extent[0], ext[0]), min(extent[1], ext[1]),
               max(extent[2], ext[2]), min(extent[3], ext[3]),
               max(extent[4], ext[4]), min(extent[5], ext[5])]
        if box[0] > box[1] or box[2] > box[3] or box[4] > box[5]:
            return None

        state = self._carry
        now = self._nowMS()
        nodeID = viewWidget.sliceLogic().GetSliceNode().GetID()
        if state is not None and (state.nodeID != nodeID
                                  or now - state.lastMS > CARRY_IDLE_MS):
            state = None
        if state is None:
            state = self._beginCarry(viewWidget, image)
            if state is None:
                return None
        state.lastMS = now

        axis, p, q = state.axis, state.p, state.q
        arr = self._labelArray(image)
        plane = _batch_plane(arr, ext, box, axis)
        if not plane.any():
            return None

        p0, p1, q0, q1 = box[2 * p], box[2 * p + 1], box[2 * q], box[2 * q + 1]
        if state.mask is None:
            import numpy as np
            state.mask = np.zeros(state.shape, dtype=bool)
            state.box = [p0, p1, q0, q1]
        else:
            state.box = [min(state.box[0], p0), max(state.box[1], p1),
                         min(state.box[2], q0), max(state.box[3], q1)]
        state.mask[q0 - ext[2 * q]:q1 - ext[2 * q] + 1,
                   p0 - ext[2 * p]:p1 - ext[2 * p] + 1] |= plane

        own = range(box[2 * axis], box[2 * axis + 1] + 1)
        scrolledTo = {self._indexOfOrigin(state, origin)
                      for origin in self._strokeOrigins.get(nodeID, [])}
        self._log("batch on slices %s..%s, scrolled through %s"
                  % (own[0], own[-1], sorted(scrolledTo)))
        if not scrolledTo:
            return None                       # never scrolled: plain painting
        state.visited.update(own)
        state.visited.update(scrolledTo)
        lo, hi = ext[2 * axis], ext[2 * axis + 1]
        # Every slice of the stroke gets the WHOLE stroke, including the slices
        # the brush already touched, which only hold the part drawn there.
        others = [s for s in state.visited if lo <= s <= hi]
        if not others:
            return None

        fill = max(1, int(arr[_slab(ext, {a: (box[2 * a], box[2 * a + 1])
                                          for a in range(3)})].max()))
        for s in others:
            _stamp_plane(arr, ext, axis, s, plane, (p0, p1, q0, q1), fill)
        image.GetPointData().GetScalars().Modified()
        image.Modified()

        newExtent = list(box)
        newExtent[2 * axis] = min(min(others), box[2 * axis])
        newExtent[2 * axis + 1] = max(max(others), box[2 * axis + 1])
        slicer.util.showStatusMessage(
            "Spine brush: stroke carried over %d slices" % len(state.visited), 2500)
        return newExtent

    def _carryPaintNewSlice(self, nodeID, origin):
        """Immediate paint mode: show the stroke so far on a slice just reached."""
        state = self._carry
        if state is None or state.nodeID != nodeID or state.mask is None or state.box is None:
            return
        try:
            index = self._indexOfOrigin(state, origin)
            ext = state.ext
            axis = state.axis
            if index in state.visited or not (ext[2 * axis] <= index <= ext[2 * axis + 1]):
                return
            state.visited.add(index)
            state.lastMS = self._nowMS()
            image = self.scriptedEffect.defaultModifierLabelmap()
            if image is None or tuple(image.GetExtent()) != tuple(ext):
                self._endCarry()
                return
            p, q = state.p, state.q
            p0, p1, q0, q1 = state.box
            plane = state.mask[q0 - ext[2 * q]:q1 - ext[2 * q] + 1,
                               p0 - ext[2 * p]:p1 - ext[2 * p] + 1]
            if not plane.any():
                return
            arr = self._labelArray(image)
            _stamp_plane(arr, ext, axis, index, plane, (p0, p1, q0, q1), 1)
            image.GetPointData().GetScalars().Modified()
            image.Modified()

            box = [0] * 6
            box[2 * axis] = box[2 * axis + 1] = index
            box[2 * p], box[2 * p + 1] = p0, p1
            box[2 * q], box[2 * q + 1] = q0, q1
            self.scriptedEffect.saveStateForUndo()
            self.scriptedEffect.modifySelectedSegmentByLabelmap(
                image, self.modificationMode(), box)
            self._log("painted stroke so far on new slice %d" % index)
        except Exception as exc:
            logging.warning("Spine brush: could not carry the stroke (%s)" % exc)
            self._endCarry()


    # -- live stroke preview --------------------------------------------------
    #
    # The scripted paint effect gets no mouse events, so a short timer watches
    # the left button and the pointer. While the button is down it draws the
    # stroke so far over the slice view - blue for the brush, red for the
    # eraser - and keeps drawing it when you scroll to the next slices, so the
    # stroke is visible on every slice it will be applied to.

    def previewColor(self):
        return PREVIEW_ERASER_COLOR if self.isErase() else PREVIEW_BRUSH_COLOR

    def _startPreviewTimer(self):
        self._stopPreviewTimer()
        self._wasDown = False
        self._blocked = False
        timer = qt.QTimer()
        timer.setInterval(PREVIEW_MS)
        timer.connect("timeout()", self._previewTick)
        timer.start()
        self._previewTimer = timer

    def _stopPreviewTimer(self):
        if self._previewTimer is not None:
            try:
                self._previewTimer.stop()
            except Exception:
                pass
            self._previewTimer = None
        self._clearPreview()
        self._wasDown = False
        self._blocked = False

    def _brushRadiusMm(self):
        try:
            diameter = float(self.scriptedEffect.doubleParameter("BrushAbsoluteDiameter"))
            if diameter > 0:
                return diameter / 2.0
        except Exception:
            pass
        return 2.5

    def _viewUnderCursor(self):
        """(viewName, sliceWidget, x, y) of the slice view under the pointer, or None.

        x, y are in the pixel units the slice node uses (origin bottom-left).
        """
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return None
        globalPos = qt.QCursor.pos()
        for viewName in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(viewName)
            if sliceWidget is None:
                continue
            view = sliceWidget.sliceView()
            if not view.isVisible():
                continue
            pos = view.mapFromGlobal(globalPos)
            if not (0 <= pos.x() < view.width and 0 <= pos.y() < view.height):
                continue
            dims = sliceWidget.sliceLogic().GetSliceNode().GetDimensions()
            ratio = float(dims[0]) / max(1, view.width)
            return viewName, sliceWidget, pos.x() * ratio, dims[1] - pos.y() * ratio
        return None

    def _previewTick(self):
        try:
            down = self._leftDown()
            if not down:
                hover = self._viewUnderCursor()
                if hover is not None:
                    self._lastViewName = hover[0]
                self._wasDown = False
                self._blocked = False
                if self._preview is not None:
                    self._clearPreview()
                return
            hit = self._viewUnderCursor()
            if hit is not None:
                self._lastViewName = hit[0]
            if not self._wasDown:
                self._wasDown = True
                self._blocked = hit is None        # pressed outside a slice view
            if self._blocked:
                return
            if self._preview is None:
                if hit is None:
                    return
                self._preview = _Preview(hit[0], hit[1])
            preview = self._preview
            if hit is not None and hit[0] == preview.viewName:
                node = preview.sliceWidget.sliceLogic().GetSliceNode()
                xyToRAS = node.GetXYToRAS()
                ras = tuple(xyToRAS.MultiplyPoint([hit[2], hit[3], 0.0, 1.0])[:3])
                if len(preview.points) < PREVIEW_MAX_POINTS:
                    if preview.points:
                        spacing = max(0.35 * self._brushRadiusMm(), 1e-3)
                        preview.points.extend(_interp_points(preview.points[-1], ras, spacing))
                    else:
                        preview.points.append(ras)
            self._drawPreview(preview)
        except Exception as exc:
            logging.warning("Spine brush: stroke preview failed (%s)" % exc)
            self._clearPreview()

    def _drawPreview(self, preview):
        import numpy as np
        from vtk.util import numpy_support
        node = preview.sliceWidget.sliceLogic().GetSliceNode()
        xyToRAS = node.GetXYToRAS()
        elements = tuple(xyToRAS.GetElement(r, c) for r in range(4) for c in range(4))
        radiusMm = self._brushRadiusMm()
        color = self.previewColor()
        signature = (len(preview.points), elements, radiusMm, color)
        if signature == preview.signature:
            return
        preview.signature = signature

        # millimetres per pixel in this view = length of the first column
        mmPerPixel = math.sqrt(sum(xyToRAS.GetElement(r, 0) ** 2 for r in range(3))) or 1.0
        radiusPx = max(radiusMm / mmPerPixel, 2.0)

        inverse = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Invert(xyToRAS, inverse)
        rasToXY = np.array([[inverse.GetElement(r, c) for c in range(4)] for r in range(4)])
        xy = _project_points(preview.points, rasToXY)

        if preview.actor is None:
            preview.polyData = vtk.vtkPolyData()
            preview.circle = vtk.vtkRegularPolygonSource()
            preview.circle.SetNumberOfSides(24)
            preview.circle.GeneratePolygonOn()
            preview.circle.GeneratePolylineOff()
            preview.circle.SetNormal(0, 0, 1)
            glyph = vtk.vtkGlyph2D()
            glyph.SetInputData(preview.polyData)
            glyph.SetSourceConnection(preview.circle.GetOutputPort())
            glyph.SetScaleModeToDataScalingOff()
            glyph.SetVectorModeToVectorRotationOff()
            glyph.OrientOff()
            glyph.ScalingOff()
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputConnection(glyph.GetOutputPort())
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            self.scriptedEffect.addActor2D(preview.sliceWidget, actor)
            preview.actor = actor

        preview.circle.SetRadius(radiusPx)
        points = vtk.vtkPoints()
        points.SetData(numpy_support.numpy_to_vtk(
            np.hstack([xy, np.zeros((xy.shape[0], 1))]).astype(np.float32), deep=True))
        preview.polyData.SetPoints(points)
        preview.polyData.Modified()
        prop = preview.actor.GetProperty()
        prop.SetColor(*color)
        prop.SetOpacity(PREVIEW_OPACITY)
        self.scriptedEffect.forceRender(preview.sliceWidget)

    def _clearPreview(self):
        preview = self._preview
        self._preview = None
        if preview is None or preview.actor is None:
            return
        try:
            self.scriptedEffect.removeActor2D(preview.sliceWidget, preview.actor)
            self.scriptedEffect.forceRender(preview.sliceWidget)
        except Exception:
            pass


    # -- slice tools: step, copy previous, fill ---------------------------------

    def _say(self, text, ms=3500):
        slicer.util.showStatusMessage("Spine brush: " + text, ms)

    def _targetSliceWidget(self):
        """The slice view the pointer is over, else the last one it was over."""
        hit = self._viewUnderCursor()
        layoutManager = slicer.app.layoutManager()
        if hit is not None:
            return hit[1]
        if layoutManager is None:
            return None
        if self._lastViewName:
            widget = layoutManager.sliceWidget(self._lastViewName)
            if widget is not None:
                return widget
        for viewName in layoutManager.sliceViewNames():
            widget = layoutManager.sliceWidget(viewName)
            if widget is not None and widget.sliceView().isVisible():
                return widget
        return None

    def stepSlice(self, direction):
        """Move the slice view one slice back (-1) or forward (+1)."""
        widget = self._targetSliceWidget()
        if widget is None:
            return
        logic = widget.sliceLogic()
        try:
            step = float(logic.GetLowestVolumeSliceSpacing()[2])
        except Exception:
            step = 0.0
        if step <= 0:
            step = 1.0
        logic.SetSliceOffset(logic.GetSliceOffset() + direction * step)

    def _indexAlong(self, image, axis, world):
        matrix = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(matrix)
        matrix.Invert()
        ijk = matrix.MultiplyPoint(list(world[:3]) + [1.0])
        return int(round(ijk[axis]))

    def _sliceContext(self, widget):
        """Labelmap, axis and slice index for the slice view, or None (with a message)."""
        image = self.scriptedEffect.defaultModifierLabelmap()
        if image is None:
            self._say("no segment to work on")
            return None
        ext = image.GetExtent()
        if ext[0] != 0 or ext[2] != 0 or ext[4] != 0:
            self._say("this volume layout is not supported by the slice tools")
            return None
        axis = self._sliceAxisFor(widget, image)
        if axis is None:
            self._say("the slice tools need a straight slice view")
            return None
        node = widget.sliceLogic().GetSliceNode()
        origin = tuple(node.GetSliceToRAS().GetElement(r, 3) for r in range(3))
        index = self._indexAlong(image, axis, origin)
        if not (ext[2 * axis] <= index <= ext[2 * axis + 1]):
            self._say("this slice is outside the volume")
            return None
        return {"image": image, "ext": tuple(ext), "axis": axis, "index": index,
                "node": node, "origin": origin}

    def _segmentPlane(self, image, ext, axis, index):
        """The selected segment on one slice, as a bool plane (q rows, p columns).

        Uses Slicer's own "selected segment labelmap": 1 where the selected
        segment is, 0 everywhere else (other segments never show up), already
        in the same geometry as the modifier labelmap. Only one slice of it is
        read, so this is quick on a big scan.
        """
        import numpy as np
        from vtk.util import numpy_support
        node = self.scriptedEffect.parameterSetNode()
        if node is None or not node.GetSelectedSegmentID():
            self._say("select a segment first")
            return None
        labelmap = self.scriptedEffect.selectedSegmentLabelmap()
        if labelmap is None:
            self._say("could not read the selected segment")
            return None
        lext = tuple(labelmap.GetExtent())
        p, q = _other_axes(axis)
        if lext[0] > lext[1] or lext[2] > lext[3] or lext[4] > lext[5]:
            self._log("selected segment is empty")
            return np.zeros((ext[2 * q + 1] - ext[2 * q] + 1,
                             ext[2 * p + 1] - ext[2 * p] + 1), dtype=bool)

        mine, theirs = vtk.vtkMatrix4x4(), vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(mine)
        labelmap.GetImageToWorldMatrix(theirs)
        for r in range(4):
            for c in range(4):
                if abs(mine.GetElement(r, c) - theirs.GetElement(r, c)) > 1e-4 * (1 + abs(mine.GetElement(r, c))):
                    self._say("the segment and the image do not line up - slice tools unavailable")
                    self._log("geometry differs at %d,%d" % (r, c))
                    return None

        if not (lext[2 * axis] <= index <= lext[2 * axis + 1]):
            return np.zeros((ext[2 * q + 1] - ext[2 * q] + 1,
                             ext[2 * p + 1] - ext[2 * p] + 1), dtype=bool)
        dims = labelmap.GetDimensions()
        flat = numpy_support.vtk_to_numpy(labelmap.GetPointData().GetScalars())
        arr = flat.reshape(dims[2], dims[1], dims[0])
        sub = _plane_of(arr, lext, axis, index)
        plane = _embed_plane(sub, lext, ext, axis)
        self._log("slice %d: %d segment voxels (values %s)"
                  % (index, int(plane.sum()), sorted(set(int(v) for v in np.unique(sub)))[:5]))
        return plane

    def toolsReport(self):
        """Diagnostic for the slice tools: run from the Python Interactor."""
        return "\n".join(["--- last slice tool decisions ---"] + self._carryLog)

    def _toolEnter(self):
        """False if a slice tool is still running or a key is auto-repeating."""
        import time
        if getattr(self, "_toolBusy", False):
            return False
        if time.monotonic() - getattr(self, "_toolLast", 0.0) < 0.6:
            return False
        self._toolBusy = True
        try:
            qt.QApplication.setOverrideCursor(qt.QCursor(qt.Qt.BusyCursor))
        except Exception:
            pass
        return True

    def _toolExit(self):
        import time
        self._toolBusy = False
        self._toolLast = time.monotonic()
        try:
            qt.QApplication.restoreOverrideCursor()
        except Exception:
            pass

    @contextlib.contextmanager
    def _brushMask(self):
        """Use the brush side of the intensity range for a moment, even in eraser mode."""
        node = self.scriptedEffect.parameterSetNode()
        swap = bool(node is not None and self.isErase() and self.useThreshold())
        if swap:
            low, high = self.maskRangeFor(False)
            _setMaskRange(node, low, high)
        try:
            yield
        finally:
            if swap:
                self.applyMask()

    def _addPlane(self, ctx, plane, what):
        """Add a 2D footprint to the selected segment on ctx's slice."""
        import numpy as np
        image, ext, axis, index = ctx["image"], ctx["ext"], ctx["axis"], ctx["index"]
        p, q = _other_axes(axis)
        rows, cols = np.nonzero(plane)
        if rows.size == 0:
            self._say("nothing to %s" % what)
            return False
        q0, q1, p0, p1 = int(rows.min()), int(rows.max()), int(cols.min()), int(cols.max())
        arr = self._labelArray(image)
        _stamp_plane(arr, ext, axis, index, plane,
                     (ext[2 * p], ext[2 * p + 1], ext[2 * q], ext[2 * q + 1]), 1)
        image.GetPointData().GetScalars().Modified()
        image.Modified()
        box = [0] * 6
        box[2 * axis] = box[2 * axis + 1] = index
        box[2 * p], box[2 * p + 1] = ext[2 * p] + p0, ext[2 * p] + p1
        box[2 * q], box[2 * q + 1] = ext[2 * q] + q0, ext[2 * q] + q1
        self.scriptedEffect.saveStateForUndo()
        with self._brushMask():
            self.scriptedEffect.modifySelectedSegmentByLabelmap(
                image, slicer.qSlicerSegmentEditorAbstractEffect.ModificationModeAdd, box)
        return True

    def copyPreviousSlice(self):
        """Copy the selected segment from the slice we came from onto this one."""
        if not self._toolEnter():
            return
        try:
            widget = self._targetSliceWidget()
            if widget is None:
                return
            ctx = self._sliceContext(widget)
            if ctx is None:
                return
            image, ext, axis, index = ctx["image"], ctx["ext"], ctx["axis"], ctx["index"]
            came = self._cameFrom.get(widget.sliceLogic().GetSliceNode().GetID())
            source = self._indexAlong(image, axis, came) if came else index - 1
            if source == index:
                source = index - 1
            if not (ext[2 * axis] <= source <= ext[2 * axis + 1]):
                self._say("there is no slice before this one")
                return
            plane = self._segmentPlane(image, ext, axis, source)
            if plane is None:
                return
            if self._addPlane(ctx, plane, "copy - the previous slice is empty"):
                self._say("copied slice %d onto slice %d" % (source, index), 2500)
        except Exception as exc:
            logging.warning("Spine brush: copy previous slice failed (%s)" % exc)
            self._say("copy failed: %s" % exc)
        finally:
            self._toolExit()

    def fillAtPointer(self):
        """Fill the closed outline under the pointer (on the bone, by threshold)."""
        if not self._toolEnter():
            return
        try:
            if self.isErase():
                self._say("fill works with the brush - press %s first" % self.shortcutKeyText("swap"))
                return
            hit = self._viewUnderCursor()
            if hit is None:
                self._say("move the pointer over a slice view, inside an outline")
                return
            widget = hit[1]
            ctx = self._sliceContext(widget)
            if ctx is None:
                return
            image, ext, axis, index = ctx["image"], ctx["ext"], ctx["axis"], ctx["index"]
            p, q = _other_axes(axis)
            xyToRAS = ctx["node"].GetXYToRAS()
            world = xyToRAS.MultiplyPoint([hit[2], hit[3], 0.0, 1.0])[:3]
            matrix = vtk.vtkMatrix4x4()
            image.GetImageToWorldMatrix(matrix)
            matrix.Invert()
            ijk = matrix.MultiplyPoint(list(world) + [1.0])
            seed = (int(round(ijk[q])) - ext[2 * q], int(round(ijk[p])) - ext[2 * p])
            painted = self._segmentPlane(image, ext, axis, index)
            if painted is None:
                return
            if not (0 <= seed[0] < painted.shape[0] and 0 <= seed[1] < painted.shape[1]):
                self._say("the pointer is outside the volume")
                return
            if not painted.any():
                self._say("nothing is painted on this slice yet - draw an outline first")
                return
            if painted[seed]:
                self._say("that spot is already painted - point inside the empty area")
                return
            region = _flood_region(painted, seed)
            if region is None:
                self._say("the outline is not closed - close the gap and try again", 5000)
                return
            if self._addPlane(ctx, region, "fill"):
                self._say("filled the outline (bone only)", 2500)
        except Exception as exc:
            logging.warning("Spine brush: fill failed (%s)" % exc)
            self._say("fill failed: %s" % exc)
        finally:
            self._toolExit()
