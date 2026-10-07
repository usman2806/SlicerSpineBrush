"""
Registers the "Spine brush" effect in the Segment Editor toolbar.

This module is hidden on purpose - it has no panel of its own. All it does is
tell Slicer about the effect in SegmentEditorSpineBrushLib/SegmentEditorEffect.py,
which then shows up as an icon next to Paint, Erase, Threshold and the rest.

Put this file, and the SegmentEditorSpineBrushLib folder beside it, in a folder
listed under Application Settings -> Modules -> Additional module paths.
"""

import os

import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModule


class SegmentEditorSpineBrush(ScriptedLoadableModule):

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "Spine Brush effect"
        self.parent.categories = ["Segmentation"]
        self.parent.dependencies = ["Segmentations"]
        self.parent.contributors = ["Usman Haider"]
        self.parent.hidden = True          # no panel, just the effect
        self.parent.helpText = (
            "Registers the Spine brush effect in the Segment Editor."
        )
        self.parent.acknowledgementText = ""

        # At normal startup the effect registry is not ready yet, so wait for it.
        # If this module is loaded by hand later, register straight away.
        try:
            alreadyStarted = slicer.app.startupCompleted()
        except Exception:
            alreadyStarted = False

        if alreadyStarted:
            self.registerEditorEffect()
        else:
            slicer.app.connect("startupCompleted()", self.registerEditorEffect)

    def registerEditorEffect(self):
        import qSlicerSegmentationsEditorEffectsPythonQt as effects

        instance = effects.qSlicerSegmentEditorScriptedPaintEffect(None)
        effectFilename = os.path.join(
            os.path.dirname(__file__),
            self.__class__.__name__ + "Lib/SegmentEditorEffect.py")
        instance.setPythonSource(effectFilename.replace("\\", "/"))
        instance.self().register()
