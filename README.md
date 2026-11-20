# Tabs at Intersection – A Rayforge Addon

**Tabs at Intersection** is an addon for [Rayforge](https://rayforge.org/) that automatically adds holding tabs to your laser-cut designs. Instead of placing tabs manually, this addon generates them precisely where a dedicated "Tab Layer" intersects the cut contours of your part. This keeps small or intricate parts secured to the stock material during cutting, preventing them from shifting or falling through the bed.

## Features

- **Automatic Tab Placement** – Tabs are created exactly at the intersection points between geometry (Lines) in your **Tab Layer** and the **Outside contour** of your design.
- **Optional Inside Contours** – You can choose to also add tabs where the Tab Layer intersects **Inside contours** (holes, cutouts, etc.).
- **Stock-Aware Defaults** – The addon detects the stock material (e.g., balsa, plywood) and suggests a sensible default tab width and power accordingly.
- **No External Dependencies** – The addon is self-contained and does **not** depend on other addons like `layer_rules`.

## Installation

1. Download the latest release from the [Releases page](https://github.com/PhilipsCodeHub/Rayforge_tabs_at_intersection/releases).
2. In Rayforge, open the **Addon Manager** (usually found in the application menu or preferences).
3. Click **“Install from file”** (or the equivalent option) and select the downloaded archive.
4. Restart Rayforge if prompted.

> **Compatibility:** This addon requires Rayforge **API version 16** or later. Check your Rayforge version in the *About* dialog if unsure.

## Usage

1. **Prepare your design** in Rayforge (or import an SVG/DXF file).
2. **Create a Tab Layer:** Add a new layer to your document and name it (e.g., `Tabs`). Draw the shapes (Lines) that represent where you want tabs to be placed. These shapes must overlap the contours you intend to cut.
3. **Open the Tab Tool:** Locate the **“Add Tabs at Intersection”** action in the menu (typically under *Tools* or *Extensions*, depending on your Rayforge version).
4. **Configure Options:**
   - **Tab width** – The physical width of each tab.
   - **Tab power** – The laser power percentage used for the tab area (so the tab remains uncut or partially cut).
   - **Include inside contours** – Check this if you also want tabs on internal holes.
5. **Generate Tabs:** The addon will analyse the intersections and create the tab features on the appropriate contours. You can then preview the result in the standard Rayforge preview.

### Tips for Best Results

- Keep the geometry in your Tab Layer simple (small rectangles or circles work well).
- Ensure the Tab Layer is **above** the layer containing the main cut contours so that the intersection logic can detect the overlap.
- Use the stock-aware defaults as a starting point, then fine-tune the width and power based on your material and laser.

## Configuration

The addon exposes the following settings through its dialog (no manual configuration file editing is required):

| Setting | Description | Default |
| :--- | :--- | :--- |
| **Tab Width** | Width of each tab (in your document’s units). | `0.8` |
| **Tab Power** | Laser power percentage for the tab region (plywood default shown). | `40 %` (plywood) |
| **Curve Steps** | Quality of curve approximation when processing geometry. | `few` (8 steps) |

The stock material is detected automatically from the document. If no stock is assigned, the addon falls back to balsa/plywood-style defaults.

## Development

### Project Structure

```text
Rayforge_tabs_at_intersection/
├── rayforge-addon.yaml      # Addon manifest
├── tabs_at_intersection/
│   ├── __init__.py
│   ├── backend.py           # Core logic (geometry processing, tab creation)
│   └── frontend.py          # UI hook (present for standard layout)
└── README.md

### Building / Testing

1. Clone the repository:
   ```bash
   git clone https://github.com/PhilipsCodeHub/Rayforge_tabs_at_intersection.git
   ```
2. Install the addon in development mode by copying the `tabs_at_intersection` folder into Rayforge’s addons directory (refer to the [Rayforge addon development docs](https://rayforge.org/docs/developer/addon-overview/) for the exact path on your OS).
3. Launch Rayforge and verify that the addon appears in the Addon Manager.

## License

This addon is released under the **MIT License**. See the [LICENSE](LICENSE) file for details.

## Author

**Philip** – [GitHub Profile](https://github.com/PhilipsCodeHub)

## Acknowledgements

- The [Rayforge](https://rayforge.org/) project for providing a powerful, open-source laser cutting platform.
- The official [rayforge-addon-template](https://github.com/barebaric/rayforge-addon-template) for guidance on addon structure.

---
