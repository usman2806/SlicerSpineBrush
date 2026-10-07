#-----------------------------------------------------------------------------
set(MODULE_NAME SegmentEditorSpineBrush)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  ${MODULE_NAME}Lib/__init__.py
  ${MODULE_NAME}Lib/SegmentEditorEffect.py
  )

set(MODULE_PYTHON_RESOURCES
  ${MODULE_NAME}Lib/SegmentEditorEffect.png
  ${MODULE_NAME}Lib/SegmentEditorEffect-paint.png
  ${MODULE_NAME}Lib/SegmentEditorEffect-erase.png
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )
