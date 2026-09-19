# Silkroad Direct Importer (Terrain + Objects) for Blender

This is an advanced Blender addon (compatible with Blender 5.0+) designed to import map terrain and 3D objects directly from the original **Silkroad Online (JMXV)** binary files, without requiring intermediate JSON conversions.4

# Silkroad Online Direct Importer for Blender

[![Watch Video](https://img.youtube.com/vi/hebBSLOfcrs/0.jpg)](https://youtu.be/hebBSLOfcrs?si=BT19wpauxfv-QO0_)

Watch the video demonstration: [https://youtu.be/hebBSLOfcrs?si=BT19wpauxfv-QO0_](https://youtu.be/hebBSLOfcrs?si=BT19wpauxfv-QO0_)

## 🚀 Key Features

* **Direct Binary Processing (JMXV):** Native support for `.m`, `.o`, `.o2`, `.t`, `.bsr`, `.bms`, `.bmt`, and `.ddj` formats.

* **Automatic Texture Extraction:** Embedded DDS textures in `.ddj` containers and `.t` lightmap files are extracted automatically to temporary directories on the fly.

* **Region-Based Organization:** Terrain meshes and 3D objects are grouped automatically into collections named after their region coordinates (`R_Z_X`).

* **Complete Terrain Material Engine:**

  * Multi-texture blending using vertex attributes (replicating DirectX 9 rendering pipelines).

  * Vertex brightness and lighting application.

  * Global lighting and region shadow mapping (`.t` files).

  * Automated water surface mesh generation according to height values in `.m` blocks.

* **Area Catalog System (`worlds.json`) — *Crucial Component*:**

  * **Mandatory Area Mapping:** The `worlds.json` file is required to map raw region coordinates to human-readable area names (such as *Jangan*, *Donwhang*, or *Hotan*) and load multi-region zones seamlessly.

  * **Automated Generator:** Includes an internal scanner that analyzes the extracted client files, indexes object placements, and generates the `worlds.json` region catalog automatically if one is not present.

* **Built-in Diagnostic Tools:** Verification panels to inspect binary configuration files (`tile2d.ifo` and `object.ifo`) and troubleshoot missing texture/model paths.

## 🛠️ Supported File Formats

| Extension | Signature / Format | Description | 
 | ----- | ----- | ----- | 
| **`.m`** | `JMXVMAPM1000` | Terrain map data (6x6 blocks, 17x17 vertex heightmaps, texture IDs, water planes). | 
| **`.o` / `.o2`** | `JMXVMAPO1001` | Object placement data (resource IDs, world positions, yaw rotation, LOD data). | 
| **`.t`** | `JMXVMAPT1001` | Region shadow/lightmap data (contains embedded DDS textures). | 
| **`.bsr`** | `JMXVRESOURCE` | 3D Resource wrapper (links `.bms` meshes, `.bmt` materials, skeletons, collision data). | 
| **`.bms`** | `JMXVBMS` | 3D Mesh Geometry (vertices, normals, UV mapping coordinates, face indices). | 
| **`.bmt`** | `JMXVBMT` | Material properties, shader flags, transparency settings, and texture file paths. | 
| **`.ddj`** | `JMXVDDJ 1000` | DirectDraw Surface (DDS) image wrapper. | 

## 📂 Installation Guide

1. Launch Blender.

2. Go to **Edit > Preferences > Add-ons**.

3. Click the top-right gear icon (or **Install...** button) and select the addon script (`silkroad_direct_importer_1_4.py`).

4. Enable the addon by checking the box next to **Silkroad Direct Importer (Terrain + Objects)**.

## 📖 Usage Instructions

The addon can be operated using the **N-Panel** sidebar in the 3D Viewport or via standard file import menus.

### 1. Using the N-Panel Sidebar (*Recommended*)

1. In the 3D Viewport, press **`N`** to open the sidebar.

2. Navigate to the **Silkroad_3dtools** tab.

3. Configure the main paths:

   * **Game Root:** Directory containing extracted client folder structure (`Data/`, `Map/`, `res/`, etc.).

   * **Map Path:** Folder where map subdirectories are located (e.g., `Map/0`, `Map/1`).

   * **worlds.json:** Select your existing `worlds.json` file or click **Generate Catalog** to build the region index automatically from your game data.

4. Click **Reload Areas** to populate the area selector dropdown menu.

5. Select the target area (e.g., *Jangan*) and set the **Neighbor Radius** to automatically include surrounding adjacent regions.

6. Click **Import Map** to build terrain geometry or **Import Objects** to spawn 3D objects.

### 2. Using Blender Standard Import Menu

* Go to **File > Import > Silkroad Terrain (.m)** to load individual terrain files manually.

* Go to **File > Import > Silkroad Objects (.o2/.o)** to load object placement files manually.

## ⚙️ Configuration & Options

### Terrain Options (`Terrain Options`)

* **Textures:** Enable or disable terrain texture loading via `tile2d.ifo`.

* **Texture Repeat:** Controls texture tiling frequency over terrain grids.

* **Vertex Brightness:** Toggles vertex color lighting intensity.

* **Region Shadow (.t):** Blends ambient baked lightmaps onto terrain.

* **Water:** Generates water planes when water flags are detected in `.m` data.

* **Shade Smooth:** Applies smooth shading to terrain geometry.

### Object Options (`Object Options`)

* **Unique Mesh per Instance:** When unchecked (default), instances reuse mesh data (*linked duplicate data*), reducing memory usage drastically.

* **Use File Normals:** Preserves custom vertex normals stored in `.bms` files.

* **Yaw Direction:** Allows flipping Z-axis rotation if imported models face backwards.

* **Group by Region / Model:** Categorizes Blender collections either by region codes or model resource IDs.

* **Skip LOD Groups:** Skips low-detail distance models to focus on high-poly main geometry.

## 🔍 Troubleshooting & Diagnostics

If textures or models fail to render, use the diagnostic buttons in the lower panel:

* **Diagnose Textures:** Validates `tile2d.ifo` parsing and checks if target images exist on disk.

* **Diagnose Objects:** Inspects `object.ifo`, verifies `.bsr` resource links, and outputs diagnostic reports.

* **Clear Cache:** Resets the addon's internal index cache, forcing a complete fresh rescan of client folders.

> **Note:** To inspect detailed logs, open the Blender system console via **Window > Toggle System Console**.
