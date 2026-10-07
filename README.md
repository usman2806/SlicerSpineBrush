# Spine Brush

**A brush and eraser for tracing bone in 3D Slicer.**

GitHub: <https://github.com/usman2806/SlicerSpineBrush>

Spine Brush is one tool in the Segment Editor. It is a brush and an eraser in one. You set a single number, the bone threshold (about 200 HU). The brush then paints only on bone, and the eraser removes only soft tissue. It was made for tracing vertebrae on spine CT.

---

## 1. What it does

| Tool | Where it works | Result |
|---|---|---|
| Brush | from the threshold up to the brightest voxel in the scan | paint lands only on bone |
| Eraser | from the darkest voxel in the scan up to the threshold | only soft tissue is wiped, traced bone stays safe |

You never type these ranges. The tool writes Slicer's **Editable intensity range** for you, and flips it when you switch between brush and eraser.

Main features:

- Brush and eraser in one tool, with one bone threshold.
- Press `e` to swap brush and eraser. Press `r` to turn the threshold on or off.
- The toolbar icon, panel, slice-view text and mouse cursor all change with the mode.
- **Scroll while painting**: hold the left mouse button and scroll the wheel. The same stroke is applied to every slice you pass.
- **Live stroke preview**: the stroke is drawn on the slice while you paint, blue for the brush and red for the eraser, and it stays visible when you scroll.
- **Slice tools**: step back and forward one slice (`a` / `d`), copy the previous slice (`c`), and fill a closed outline (`f`).

---

## 2. Installation

You need **3D Slicer 5.0 or newer**. It works with both the older `MasterVolume` and the newer `SourceVolume` names, so no edits are needed.

### 2.1 From the Extensions Manager

Once the extension is published, open **View > Extensions Manager**, search for **Spine Brush**, install it, and restart Slicer.

### 2.2 From source

1. Clone or download this repository.
2. In Slicer, go to **Edit > Application Settings > Modules**.
3. Under **Additional module paths**, click **Add** and choose this folder:

   ```
   <your copy>/SegmentEditorSpineBrush
   ```

4. Click **OK**, then **close Slicer completely and open it again**.
5. Open the **Segment Editor** module. **Spine brush** now appears in the effects grid, next to Paint and Erase.

> Slicer reads the tool only when it starts. After any change to the files, always restart Slicer fully.

### 2.3 Check that it works

1. Open **Segment Editor**, choose a segmentation, a source volume, and add a segment.
2. Look in the effects grid for **Spine brush**.
3. Click it. The panel shows a large **BRUSH** bar and the bone threshold.

If the icon is not there, see section 6 (Troubleshooting).

### 2.4 Updating

Replace the files in the same folder, then restart Slicer fully. The module path stays as it is.

---

## 3. Quick start

1. Open **Segment Editor**. Pick the segmentation, the source volume (your CT), and a segment such as L3.
2. Click **Spine brush** in the effects grid.
3. Keep the threshold at **200 HU**. This suits most spine CT.
4. Paint over the vertebra with the left mouse button. Paint goes only on bone.
5. If paint spills into soft tissue, press `e` and wipe over it. Traced bone is not touched.
6. Press `e` again to go back to the brush.

Tip: in the Segment Editor, set **Masking > Overwrite other segments** to **None** when you trace several vertebrae. This stops one vertebra from cutting into its neighbour across the disc.

---

## 4. Keys

All keys work only while Spine brush is the active tool. They are ignored while you type in a text box. You can change every key in the panel: type the new key and press Enter.

| Key | What it does |
|---|---|
| `e` | swap brush and eraser |
| `r` | bone threshold on / off |
| `a` | go back one slice |
| `d` | go forward one slice |
| `c` | copy the previous slice onto this one |
| `f` | fill the closed outline under the pointer |

Keys already used by Slicer, so avoid them: `0-9`, `Esc`, `Space`, `z`, `y`, `q`, `w`, `i`, and `/ * , . < >`. Avoid `Tab` too, because Qt uses it to move between controls.

---

## 5. Features in detail

### 5.1 Brush and eraser

The big bar at the top of the panel says **BRUSH** (blue) or **ERASER** (red). Under it, a line shows the exact range in use, for example `painting range 200 to 3071 HU`.

The mode shows in four places, so you always know which tool is on:

- the effect icon in the Segment Editor toolbar (red tint for the eraser),
- the panel bar,
- text in the corner of each slice view, such as `BRUSH > L3` or `ERASER > L3`, in the colour of the selected segment,
- the small icon that follows the mouse pointer.

### 5.2 Bone threshold

- **Use bone threshold**: untick it for plain paint and erase with no limit. The `r` key does the same.
- **Threshold slider**: where bone starts. Try 150 for more spongy (trabecular) bone, or 300 for hard outer (cortical) bone only. The slider adjusts itself to the value range of your scan.

The eraser takes everything darker than the threshold. Slicer's intensity limit is one continuous range, so this is the closest match for bone on CT, and it is what you want here.

### 5.3 Scroll while painting

1. Start a stroke: hold the left mouse button.
2. Scroll the mouse wheel while still holding the button.
3. Release the button.

The stroke you drew is applied to every slice you passed. It works for the brush and the eraser, and the bone threshold is still applied on each slice. It needs a straight (not tilted) slice view. You can turn it off with the checkbox **Scroll while painting: copy the stroke to each slice**.

While you hold the button, the stroke is drawn as a coloured shape on the slice: **blue for the brush, red for the eraser**. It stays on screen when you scroll, so you can see what will be applied. It disappears when you release, and the real paint stays.

### 5.4 Slice tools

**Back / Forward (`a` / `d`)** move the slice view one slice at a time. They act on the slice view under your mouse pointer. The panel has buttons for the same thing.

**Copy previous slice (`c`)** copies the selected segment from the slice you just left onto the slice you are on. The bone threshold still applies, so only bone is kept. It works in both brush and eraser mode. Use it right after `d` to start the next slice from the last one, then correct the edges.

**Fill (`f`)**:

1. Draw a closed outline around the area with the brush.
2. Put the mouse pointer inside the outline.
3. Press `f`.

The empty area inside the outline is filled, and only bone (above the threshold) is kept. Soft marrow inside a vertebra can stay empty. To fill everything, press `r` to turn the threshold off first.

Fill works in brush mode only. Copy and fill touch only the selected segment, never the others, and they ignore key auto-repeat (holding the key down runs it once). If the outline has a gap, nothing is filled and a message tells you to close the gap. If the pointer is on a spot that is already painted, it asks you to point inside the empty area.

All slice tools support Undo (Ctrl+Z).

---

## 6. Troubleshooting

| Problem | What to try |
|---|---|
| Spine brush is not in the effects grid | Check that the `SpineBrush` folder is listed under **Additional module paths**, then restart Slicer fully. Look in **View > Error Log** for the word SpineBrush. |
| Changes do not show after an update | Close Slicer completely and open it again. Python files are read only at start. |
| Keys do nothing | Make sure Spine brush is the active tool, and that you are not typing in a text box. Check that the key is not used by Slicer (section 4). |
| Scroll while painting does nothing | Use a straight slice view (not tilted) and keep the left button held while scrolling. If it still fails, do a stroke, then open **View > Python Interactor** (Ctrl+3) and run the report command below, and send the output. |
| Paint goes everywhere | The threshold is off. Press `r`, or tick **Use bone threshold**. |
| Paint does not land on some bone | The threshold is too high for that bone. Lower the slider, for example to 150. |
| Fill says the outline is not closed | There is a gap in the outline. Close it with the brush and press `f` again. |
| Copy or fill says it could not read the segment | Select a segment first. Make sure the segmentation uses the same source volume you are viewing. |
| Copy or fill gives a wrong result | Run the tools report command below right after it happens and send the output. |

Report command for scroll-while-painting problems (paste as one line):

```
print(slicer.modules.segmenteditor.widgetRepresentation().self().editor.effectByName("Spine brush").self().carryReport())
```

Report command for copy and fill problems (paste as one line):

```
print(slicer.modules.segmenteditor.widgetRepresentation().self().editor.effectByName("Spine brush").self().toolsReport())
```


---

## 7. Good to know

- The mouse wheel without Shift scrolls slices as usual. **Shift + wheel** changes the brush size, as in Slicer's own Paint tool.
- Brush size, sphere brush and the other standard brush options are the same as in Slicer's Paint effect.
- The tool does not change your data except through normal Segment Editor edits, so Undo works.

---

## 8. License and credits

BSD 3-Clause. See [LICENSE](LICENSE). Made by Usman Haider.

Source code and updates: <https://github.com/usman2806/SlicerSpineBrush>
