cmake_minimum_required(VERSION 3.28.0...3.28.0 FATAL_ERROR)

project(SpineBrush)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/usman2806/SlicerSpineBrush")
set(EXTENSION_CATEGORY "Segmentation")
set(EXTENSION_CONTRIBUTORS "Usman Haider")
set(EXTENSION_DESCRIPTION "Brush and eraser in one Segment Editor effect, for tracing bone on CT. You set one bone threshold: the brush paints only above it, the eraser removes only below it, and the editable intensity range flips by itself when you switch. Made for vertebra segmentation.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/usman2806/SlicerSpineBrush/main/SpineBrush.png")
set(EXTENSION_SCREENSHOTURLS "https://raw.githubusercontent.com/usman2806/SlicerSpineBrush/main/Screenshots/1.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(SegmentEditorSpineBrush)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
