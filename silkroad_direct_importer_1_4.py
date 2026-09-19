bl_info = {
    "name": "Silkroad Direct Importer (Terrain + Objects)",
    "author": "arq2k",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "File > Import > Silkroad Terrain (.m) / Silkroad Objects (.o2)",
    "description": "Reads Silkroad JMXV binaries directly (.m/.o/.o2/.t/.bsr/.bms/.bmt/.ddj). No JSON step.",
    "category": "Import-Export",
}

# ---------------------------------------------------------------------------
# Silkroad Direct Importer
#
# Reads the original game binaries directly. No master JSON needed.
#
#   .m   JMXVMAPM1000  terrain (6x6 blocks, 17x17 vertices per block)
#   .o   JMXVMAPO1001  objects (no region id)
#   .o2  JMXVMAPO1001  objects (with region id)
#   .t   JMXVMAPT1001  96x96 lightmap + region shadow DDS
#   .bsr JMXVRESOURCE  resource: list of .bms, .bmt, skeleton, collision
#   .bms JMXVBMS       mesh
#   .bmt JMXVBMT       material -> .ddj
#   .ddj JMXVDDJ 1000  container with a raw DDS inside
#
# Axis conversion (single place in the whole addon):
#   Blender = (sro.x, sro.z, sro.y)
#   face winding flipped (the Y/Z swap has determinant -1)
#   UV V flipped (DirectX has its origin at the top)
#   yaw applied positive around +Z
# ---------------------------------------------------------------------------

import math
import os
import struct
import tempfile
import time
import traceback
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy_extras.io_utils import ImportHelper


# ===========================================================================
# Format constants
# ===========================================================================

BLOCKS = 6
TILES = 16
VERTS = 17
TILE_SIZE = 20.0
GRID = BLOCKS * TILES + 1          # 97 vertices por lado de regiao
REGION_SIZE = BLOCKS * TILES * TILE_SIZE   # 1920.0

DDS_CACHE = Path(tempfile.gettempdir()) / "sro_direct_dds_cache"

DEFAULT_GAME_ROOT = r"D:\silkroad-project\extracted"


# ===========================================================================
# Binary reader
# ===========================================================================

class Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos

    def seek(self, pos):
        self.pos = max(0, min(int(pos), len(self.data)))

    def skip(self, n):
        self.pos += int(n)

    def remaining(self):
        return max(0, len(self.data) - self.pos)

    def u8(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def i16(self):
        v = struct.unpack_from("<h", self.data, self.pos)[0]
        self.pos += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return v

    def raw(self, n):
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v

    def text(self, n):
        raw = self.raw(n)
        for enc in ("cp949", "cp1252", "utf-8"):
            try:
                return raw.decode(enc).rstrip("\0")
            except Exception:
                continue
        return raw.decode("latin-1").rstrip("\0")

    def lstring(self, limit=8192):
        n = self.u32()
        if n > limit or n > self.remaining():
            raise ValueError("string invalida len=%d" % n)
        return self.text(n)


# ===========================================================================
# Game root file index (case-insensitive)
# ===========================================================================

_ROOT_INDEX = {}


class GameRoot:
    """Indexes the extracted folder once and resolves game paths."""

    def __init__(self, root):
        self.root = Path(root) if root else None
        self.by_rel = {}
        self.by_name = {}
        self.ok = False

    def build(self, report=None):
        if not self.root or not self.root.is_dir():
            return False
        t0 = time.time()
        for base, _dirs, files in os.walk(self.root):
            base_path = Path(base)
            try:
                rel_dir = base_path.relative_to(self.root).as_posix().lower()
            except ValueError:
                continue
            prefix = "" if rel_dir == "." else rel_dir + "/"
            for fname in files:
                low = fname.lower()
                full = str(base_path / fname)
                self.by_rel[prefix + low] = full
                self.by_name.setdefault(low, full)
        self.ok = True
        if report:
            report("Indexed %d files in %.1fs" % (len(self.by_rel), time.time() - t0))
        return True

    def resolve(self, raw):
        """Resolves a game path (e.g. 'res\\bldg\\x.bsr') to disk."""
        if not raw:
            return None
        value = str(raw).strip().strip('"').replace("\\", "/").lstrip("./")
        while "//" in value:
            value = value.replace("//", "/")
        low = value.lower().lstrip("/")
        if not low:
            return None

        direct = Path(value)
        if direct.is_absolute() and direct.exists():
            return str(direct)
        if not self.ok:
            return None

        hit = self.by_rel.get(low)
        if hit:
            return hit
        for prefix in ("data/", "map/", "data/map/", "media/", "particles/",
                       "map/tile2d/", "data/map/tile2d/", "tile2d/",
                       "data/tile2d/"):
            hit = self.by_rel.get(prefix + low)
            if hit:
                return hit

        # o caminho gravado no .ifo pode ter um prefixo que nao existe na sua
        # extracao (ex.: um disco/pasta raiz tipo "3ddata\..."). Tenta
        # descartar segmentos da esquerda antes de cair no scan completo.
        segments = low.split("/")
        for cut in range(1, min(len(segments) - 1, 4) + 1):
            candidate = "/".join(segments[cut:])
            hit = self.by_rel.get(candidate)
            if hit:
                return hit

        # sufixo: 'map/tile2d/x.ddj' pode estar em 'data/map/tile2d/x.ddj'
        suffix = "/" + low
        for key, val in self.by_rel.items():
            if key.endswith(suffix):
                return val
        return self.by_name.get(os.path.basename(low))

    def find_any(self, names):
        for name in names:
            hit = self.resolve(name)
            if hit:
                return hit
        return None


def get_game_root(path, report=None):
    key = os.path.normcase(os.path.abspath(str(path))) if path else ""
    cached = _ROOT_INDEX.get(key)
    if cached is not None:
        return cached
    root = GameRoot(path)
    root.build(report)
    _ROOT_INDEX[key] = root
    return root


# ===========================================================================
# Textures: .ddj / .t -> cached .dds
# ===========================================================================

_IMAGE_CACHE = {}


def safe_name(value):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value)) or "tex"


def ddj_to_dds(path):
    """Extracts the DDS from inside a .ddj. Returns the cached .dds path."""
    if not path:
        return None
    src = Path(path)
    if not src.exists():
        return None
    if src.suffix.lower() in (".dds", ".png", ".tga", ".jpg"):
        return str(src)
    data = src.read_bytes()
    if len(data) < 24:
        return None
    if data[:7] != b"JMXVDDJ":
        idx = data.find(b"DDS ")
        if idx < 0:
            return None
        payload = data[idx:]
    else:
        size = struct.unpack_from("<I", data, 12)[0]
        payload = data[20:20 + size] if size else data[20:]
        if not payload.startswith(b"DDS "):
            idx = data.find(b"DDS ")
            if idx < 0:
                return None
            payload = data[idx:]
    DDS_CACHE.mkdir(parents=True, exist_ok=True)
    out = DDS_CACHE / ("%s_%08x.dds" % (safe_name(src.stem), len(payload) & 0xFFFFFFFF))
    if not out.exists() or out.stat().st_size != len(payload):
        out.write_bytes(payload)
    return str(out)


def map_t_to_dds(path):
    """Extracts the shadow/lightmap DDS from a region .t file."""
    if not path:
        return None
    src = Path(path)
    if not src.exists():
        return None
    data = src.read_bytes()
    idx = data.find(b"DDS ")
    if idx < 0:
        return None
    payload = data[idx:]
    DDS_CACHE.mkdir(parents=True, exist_ok=True)
    out = DDS_CACHE / ("mapt_%s_%08x.dds" % (safe_name(src.stem), len(payload) & 0xFFFFFFFF))
    if not out.exists() or out.stat().st_size != len(payload):
        out.write_bytes(payload)
    return str(out)


def _image_alive(img):
    """True if the datablock still exists in the .blend (not freed)."""
    if img is None:
        return False
    try:
        img.name  # acessar qualquer atributo dispara ReferenceError se morreu
        return True
    except (ReferenceError, RuntimeError):
        return False


def load_image(dds_path):
    if not dds_path:
        return None
    key = os.path.normcase(str(dds_path))
    cached = _IMAGE_CACHE.get(key)
    if cached is not None and _image_alive(cached):
        return cached
    try:
        img = bpy.data.images.load(str(dds_path), check_existing=True)
        img.alpha_mode = "CHANNEL_PACKED"
    except Exception as exc:
        print("SRO: failed to load image %s: %s" % (dds_path, exc))
        img = None
    _IMAGE_CACHE[key] = img
    return img


def game_texture_image(root, raw_path):
    """Game path -> bpy.types.Image."""
    resolved = root.resolve(raw_path)
    if not resolved:
        return None, None
    return load_image(ddj_to_dds(resolved)), resolved


# ===========================================================================
# tile2d.ifo  and  object.ifo
# ===========================================================================

_IFO_CACHE = {}

_IMAGE_EXT = (".ddj", ".dds", ".png", ".jpg", ".bmp", ".tga")


def _parse_index(token):
    """Converts the first column of an .ifo line (record id).

    It is almost always zero-padded decimal (e.g. '00001'), which
    Python's int(x, 0) rejects (treated as a malformed old-style octal).
    Tries decimal first, only falling back to base 0 (accepts 0x..) if
    decimal fails, covering the few files that use hex in that column.
    """
    try:
        return int(token, 10)
    except ValueError:
        return int(token, 0)


def _quoted(line):
    out = []
    parts = line.split('"')
    for i in range(1, len(parts), 2):
        out.append(parts[i])
    return out


def _ifo_lines(path):
    raw = Path(path).read_bytes()
    for enc in ("cp949", "cp1252", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    else:
        text = raw.decode("latin-1")
    return [ln.strip() for ln in text.replace("\r", "").split("\n")]


def _tile2d_entries(path):
    """Extracts (file_id, texture_path) from each usable line."""
    entries = []
    for line in _ifo_lines(path)[1:]:
        if not line or line.startswith("//"):
            continue
        head = line.split()
        if not head:
            continue
        try:
            file_id = _parse_index(head[0])
        except ValueError:
            continue

        texture = None
        for q in _quoted(line):
            q = q.strip()
            if not q:
                continue
            if q.lower().endswith(_IMAGE_EXT):
                texture = q
                break
            # alguns dumps guardam o caminho sem extensao
            if ("\\" in q or "/" in q) and texture is None:
                texture = q + ".ddj"
        if texture is None:
            for token in head[1:]:
                low = token.lower()
                if low.endswith(_IMAGE_EXT):
                    texture = token
                    break
        if texture:
            entries.append((file_id, texture))
    return entries


def read_tile2d(root, force=False):
    """terrain texture id -> .ddj path

    The id stored in the .m file indexes tile2d.ifo. Depending on the dump,
    the useful id is either the first number on the line or the line's
    position in the list. We store both and the lookup tries the first,
    falling back to the second.
    """
    if not root.ok:
        return {}
    key = ("tile2d", id(root))
    if key in _IFO_CACHE and not force:
        return _IFO_CACHE[key]

    path = root.find_any(["tile2d.ifo", "Map/tile2d.ifo", "Data/tile2d.ifo",
                          "Data/Map/tile2d.ifo"])
    table = {}
    if path:
        entries = _tile2d_entries(path)
        by_id = {}
        for file_id, texture in entries:
            by_id.setdefault(file_id, texture)
        by_order = {index: texture for index, (_fid, texture) in enumerate(entries)}
        # o dicionario final usa o id da linha; a ordem entra so onde faltar
        table = dict(by_order)
        table.update(by_id)
        table["__source__"] = path
        table["__count__"] = len(entries)
    _IFO_CACHE[key] = table
    return table


def read_object_ifo(root):
    """object id -> .bsr path"""
    if not root.ok:
        return {}
    key = ("object", id(root))
    if key in _IFO_CACHE:
        return _IFO_CACHE[key]
    path = root.find_any(["object.ifo", "Map/object.ifo", "Data/object.ifo",
                          "Data/Map/object.ifo"])
    table = {}
    if path:
        for line in _ifo_lines(path)[1:]:
            if not line:
                continue
            head = line.split()
            if not head:
                continue
            try:
                index = _parse_index(head[0])
            except ValueError:
                continue
            chosen = None
            for q in _quoted(line):
                if "." in q:
                    chosen = q
                    if q.lower().endswith(".bsr"):
                        break
            if chosen:
                table[index] = chosen
    _IFO_CACHE[key] = table
    return table


# ===========================================================================
# Terrain: .m
# ===========================================================================

class TerrainRegion:
    __slots__ = ("rx", "rz", "heights", "tex_ids", "brightness", "water", "path")

    def __init__(self, rx, rz, path):
        self.rx = rx
        self.rz = rz
        self.path = path
        self.heights = [0.0] * (GRID * GRID)
        self.tex_ids = [0] * (GRID * GRID)
        self.brightness = [255] * (GRID * GRID)
        self.water = []


def read_terrain(path, rx, rz):
    data = Path(path).read_bytes()
    if data[:8] != b"JMXVMAPM":
        raise ValueError("assinatura invalida em %s" % path)
    reader = Reader(data, 12)
    region = TerrainRegion(rx, rz, str(path))
    heights = region.heights
    tex_ids = region.tex_ids
    brightness = region.brightness

    for bz in range(BLOCKS):
        for bx in range(BLOCKS):
            reader.skip(6)                      # block id (u32 + u16)
            base_x = bx * TILES
            base_z = bz * TILES
            chunk = reader.raw(VERTS * VERTS * 7)
            offset = 0
            for z in range(VERTS):
                row = (base_z + z) * GRID
                for x in range(VERTS):
                    height, info, bright = struct.unpack_from("<fHB", chunk, offset)
                    offset += 7
                    idx = row + base_x + x
                    heights[idx] = height
                    tex_ids[idx] = info & 0x03FF
                    brightness[idx] = bright

            water_type = reader.u8()
            reader.u8()                          # wave type
            water_height = reader.f32()
            reader.skip(TILES * TILES * 2)       # per-tile min/max
            reader.skip(8)                       # height max / min
            reader.skip(20)                      # reserved

            if water_type in (0, 1):
                region.water.append((bx, bz, water_height))
    return region


# ===========================================================================
# Objects: .o / .o2
# ===========================================================================

class WorldObject:
    __slots__ = ("oid", "x", "y", "z", "yaw", "region_x", "region_z", "lod")

    def __init__(self, oid, x, y, z, yaw, region_x, region_z, lod):
        self.oid = oid
        self.x = x
        self.y = y
        self.z = z
        self.yaw = yaw
        self.region_x = region_x
        self.region_z = region_z
        self.lod = lod


def read_objects(path, rx, rz):
    data = Path(path).read_bytes()
    if data[:8] != b"JMXVMAPO":
        raise ValueError("assinatura invalida em %s" % path)
    has_region = str(path).lower().endswith(".o2")
    reader = Reader(data, 12)
    out = []
    seen = set()
    for _bz in range(BLOCKS):
        for _bx in range(BLOCKS):
            for lod in range(4):
                count = reader.u16()
                for _i in range(count):
                    oid = reader.u32()
                    x = reader.f32()
                    y = reader.f32()
                    z = reader.f32()
                    reader.i16()
                    yaw = reader.f32()
                    reader.i16()
                    reader.i16()
                    reader.u8()
                    reader.u8()
                    if has_region:
                        packed = reader.u16()
                        orx = packed & 0xFF
                        orz = (packed >> 8) & 0x7F
                    else:
                        orx, orz = rx, rz
                    key = (oid, round(x, 2), round(y, 2), round(z, 2), orx, orz)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(WorldObject(oid, x, y, z, yaw, orx, orz,
                                           lod if has_region else None))
    return out


# ===========================================================================
# BSR / BMS / BMT
# ===========================================================================

class ResourceRefs:
    __slots__ = ("materials", "meshes", "skeleton", "attach_bone", "collision")

    def __init__(self):
        self.materials = []
        self.meshes = []
        self.skeleton = None
        self.attach_bone = None
        self.collision = None


def _indexed_paths(data, offset):
    if offset <= 0 or offset >= len(data):
        return []
    reader = Reader(data, offset)
    try:
        count = reader.i32()
        out = []
        for _ in range(max(0, min(count, 4096))):
            reader.i32()
            out.append(reader.lstring())
        return out
    except Exception:
        return []


def _mesh_paths(data, offset, flags):
    if offset <= 0 or offset >= len(data):
        return []
    reader = Reader(data, offset)
    try:
        count = reader.i32()
        out = []
        for _ in range(max(0, min(count, 4096))):
            out.append(reader.lstring())
            if flags & 1:
                reader.i32()
        return out
    except Exception:
        return []


def _skeleton_info(data, offset):
    if offset <= 0 or offset >= len(data):
        return None, None
    reader = Reader(data, offset)
    try:
        if reader.u32() == 0:
            return None, None
        skeleton = reader.lstring()
        bone = reader.lstring()
        return skeleton or None, bone or None
    except Exception:
        return None, None


def _collision_path(data, offset):
    if offset <= 0 or offset + 4 > len(data):
        return None
    try:
        length = struct.unpack_from("<I", data, offset)[0]
        if length == 0 or offset + 4 + length > len(data):
            return None
        value = Reader(data, offset).lstring()
        return value if "." in value else None
    except Exception:
        return None


def read_bsr(path):
    data = Path(path).read_bytes()
    reader = Reader(data, 12)
    offsets = [reader.u32() for _ in range(8)]
    flags = [reader.i32() for _ in range(5)]
    refs = ResourceRefs()
    refs.materials = _indexed_paths(data, offsets[0])
    refs.meshes = _mesh_paths(data, offsets[1], flags[0])
    refs.skeleton, refs.attach_bone = _skeleton_info(data, offsets[2])
    refs.collision = _collision_path(data, offsets[7])
    return refs


class MeshData:
    __slots__ = ("name", "material_name", "positions", "normals", "uvs", "indices")

    def __init__(self):
        self.name = ""
        self.material_name = ""
        self.positions = []
        self.normals = []
        self.uvs = []
        self.indices = []


def read_bms(path):
    data = Path(path).read_bytes()
    if data[:7] != b"JMXVBMS":
        return None
    reader = Reader(data, 12)
    offsets = [reader.u32() for _ in range(10)]
    vert_offset, _skin_offset, face_offset = offsets[0], offsets[1], offsets[2]
    if vert_offset <= 0 or face_offset <= 0:
        return None

    reader.seek(12 + 40 + 12)
    vertex_flag = reader.u32()
    reader.u32()
    mesh = MeshData()
    try:
        mesh.name = reader.lstring()
        mesh.material_name = reader.lstring()
    except Exception:
        mesh.name = Path(path).stem

    # vertices
    vr = Reader(data, vert_offset)
    count = vr.u32()
    if count > 2_000_000:
        return None
    stride_extra = 12
    if vertex_flag & 0x400:
        stride_extra += 8
    if vertex_flag & 0x800:
        stride_extra += 36
    positions = mesh.positions
    normals = mesh.normals
    uvs = mesh.uvs
    base = vr.pos
    record = 32 + stride_extra
    for i in range(count):
        px, py, pz, nx, ny, nz, u, v = struct.unpack_from("<8f", data, base + i * record)
        positions.append((px, pz, py))          # SRO -> Blender
        normals.append((nx, nz, ny))
        uvs.append((u, 1.0 - v))                # DirectX V invertido

    # faces (winding invertido por causa da troca Y/Z)
    fr = Reader(data, face_offset)
    face_count = fr.u32()
    if face_count > 2_000_000:
        return None
    tri = struct.unpack_from("<%dH" % (face_count * 3), data, fr.pos)
    indices = mesh.indices
    for i in range(face_count):
        a = tri[i * 3]
        b = tri[i * 3 + 1]
        c = tri[i * 3 + 2]
        if a < count and b < count and c < count:
            indices.append((c, b, a))
    return mesh


class MaterialEntry:
    __slots__ = ("name", "texture", "diffuse", "flags", "emissive")

    def __init__(self, name, texture, diffuse, flags, emissive):
        self.name = name
        self.texture = texture
        self.diffuse = diffuse
        self.flags = flags
        self.emissive = emissive


def read_bmt(path):
    data = Path(path).read_bytes()
    out = []
    try:
        reader = Reader(data, 12)
        count = reader.i32()
        if count < 0 or count > 4096:
            raise ValueError("count invalido")
        for _ in range(count):
            name = reader.lstring()
            diffuse = [reader.f32() for _ in range(4)]
            [reader.f32() for _ in range(4)]          # ambient
            [reader.f32() for _ in range(4)]          # specular
            emissive = [reader.f32() for _ in range(4)]
            reader.f32()                              # specular power
            flags = reader.u32()
            texture = reader.lstring()
            reader.f32()                              # diffuse map weight
            reader.u8()
            reader.u8()
            reader.u8()
            if flags & 0x2000:
                reader.lstring()
                reader.u32()
            out.append(MaterialEntry(name, texture, diffuse, flags, emissive))
        if out:
            return out
    except Exception:
        pass
    # fallback: varre strings de textura
    text = data.decode("latin-1")
    found = []
    token = ""
    for ch in text:
        if ch.isalnum() or ch in "_./\\ -":
            token += ch
        else:
            low = token.lower()
            if low.endswith(_IMAGE_EXT) and len(token) > 5:
                found.append(token.strip())
            token = ""
    seen = set()
    for item in found:
        if item.lower() in seen:
            continue
        seen.add(item.lower())
        out.append(MaterialEntry("", item, [1, 1, 1, 1], 1, [0, 0, 0, 1]))
    return out


def material_candidates(resource_materials, mesh_path, material_name):
    """Search order for .bmt files, matching sro-archive-explorer's logic."""
    out = []

    def add(value):
        if not value:
            return
        norm = str(value).replace("\\", "/")
        if norm.lower() not in [item.lower() for item in out]:
            out.append(norm)

    for item in resource_materials:
        add(item)

    mesh_base = str(mesh_path).replace("\\", "/")
    mesh_dir = os.path.dirname(mesh_base)
    dirs = []
    for candidate in (
        mesh_dir.replace("/mesh", "/mtrl").replace("/Mesh", "/mtrl").replace("/ani", "/mtrl"),
        mesh_dir,
    ):
        if candidate not in dirs:
            dirs.append(candidate)
    mesh_name = os.path.splitext(os.path.basename(mesh_base))[0]
    for directory in dirs:
        add(directory + "/" + mesh_name + ".bmt")

    if material_name:
        mat_base = str(material_name).replace("\\", "/")
        if os.path.splitext(mat_base)[1]:
            add(mat_base)
            for directory in dirs:
                add(directory + "/" + os.path.basename(mat_base))
        else:
            for directory in dirs:
                add(directory + "/" + mat_base + ".bmt")
    return out


# ===========================================================================
# Blender: collections and utilities
# ===========================================================================

def get_collection(name, parent=None):
    parent = parent or bpy.context.scene.collection
    existing = None
    for child in parent.children:
        if child.name == name:
            existing = child
            break
    if existing:
        return existing
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


def link(obj, collection):
    if obj.name not in collection.objects:
        collection.objects.link(obj)


def node_at(nodes, kind, x, y):
    node = nodes.new(kind)
    node.location = (x, y)
    return node


# ===========================================================================
# Terrain material: weighted sum over vertex attributes
# ===========================================================================

def terrain_material(name, slots, root, tile2d, shadow_dds,
                     use_textures, brightness_strength, shadow_strength,
                     tiles_per_cell):
    """
    slots: lista de texture ids na ordem em que foram empacotados nos
    atributos SRO_W0..SRO_Wn (4 canais por atributo).

    cor = sum(peso_k * textura_k)  -- os pesos somam 1 em cada vertice,
    e o Blender interpola por vertice, o mesmo que o DX9 faz com vertex alpha.
    """
    existing = bpy.data.materials.get(name)
    if existing:
        return existing

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    nodes = tree.nodes
    links = tree.links
    nodes.clear()

    output = node_at(nodes, "ShaderNodeOutputMaterial", 1600, 0)
    bsdf = node_at(nodes, "ShaderNodeBsdfPrincipled", 1300, 0)
    bsdf.inputs["Roughness"].default_value = 0.92
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.1
    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    uv_tile = node_at(nodes, "ShaderNodeUVMap", -1400, 300)
    uv_tile.uv_map = "SRO_Tile"
    mapping = node_at(nodes, "ShaderNodeMapping", -1200, 300)
    mapping.inputs["Scale"].default_value = (tiles_per_cell, tiles_per_cell, 1.0)
    links.new(uv_tile.outputs["UV"], mapping.inputs["Vector"])

    accum = None
    y = 0
    for slot_index, texture_id in enumerate(slots):
        attr_index = slot_index // 4
        channel = slot_index % 4

        attr = node_at(nodes, "ShaderNodeAttribute", -700, y)
        attr.attribute_type = "GEOMETRY"
        attr.attribute_name = "SRO_W%d" % attr_index

        if channel == 3:
            weight_socket = attr.outputs["Alpha"]
        else:
            sep = node_at(nodes, "ShaderNodeSeparateColor", -520, y)
            links.new(attr.outputs["Color"], sep.inputs["Color"])
            weight_socket = sep.outputs[channel]

        color_socket = None
        if use_textures:
            raw = tile2d.get(int(texture_id))
            if not isinstance(raw, str):
                raw = None
            image, _ = game_texture_image(root, raw) if raw else (None, None)
            if image:
                tex = node_at(nodes, "ShaderNodeTexImage", -340, y)
                tex.image = image
                tex.extension = "REPEAT"
                tex.interpolation = "Linear"
                links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
                color_socket = tex.outputs["Color"]
        if color_socket is None:
            rgb = node_at(nodes, "ShaderNodeRGB", -340, y)
            shade = ((int(texture_id) * 37) % 48) / 255.0
            warm = ((int(texture_id) * 17) % 32) / 255.0
            rgb.outputs["Color"].default_value = (0.42 + warm, 0.55 + shade,
                                                  0.38 + shade * 0.5, 1.0)
            color_socket = rgb.outputs["Color"]

        weighted = node_at(nodes, "ShaderNodeVectorMath", 40, y)
        weighted.operation = "SCALE"
        links.new(color_socket, weighted.inputs[0])
        links.new(weight_socket, weighted.inputs["Scale"])

        if accum is None:
            accum = weighted.outputs["Vector"]
        else:
            add = node_at(nodes, "ShaderNodeVectorMath", 240, y)
            add.operation = "ADD"
            links.new(accum, add.inputs[0])
            links.new(weighted.outputs["Vector"], add.inputs[1])
            accum = add.outputs["Vector"]
        y -= 300

    if accum is None:
        accum = node_at(nodes, "ShaderNodeRGB", 240, 0).outputs["Color"]

    # brilho por vertice
    if brightness_strength > 0.0:
        battr = node_at(nodes, "ShaderNodeAttribute", 500, 400)
        battr.attribute_type = "GEOMETRY"
        battr.attribute_name = "SRO_Brightness"
        lift = node_at(nodes, "ShaderNodeMix", 680, 400)
        lift.data_type = "RGBA"
        lift.blend_type = "MIX"
        lift.inputs["Factor"].default_value = float(brightness_strength)
        lift.inputs[6].default_value = (1.0, 1.0, 1.0, 1.0)
        links.new(battr.outputs["Color"], lift.inputs[7])
        mul = node_at(nodes, "ShaderNodeMix", 860, 300)
        mul.data_type = "RGBA"
        mul.blend_type = "MULTIPLY"
        mul.inputs["Factor"].default_value = 1.0
        links.new(accum, mul.inputs[6])
        links.new(lift.outputs[2], mul.inputs[7])
        accum = mul.outputs[2]

    # sombra da regiao (.t)
    if shadow_strength > 0.0 and shadow_dds:
        image = load_image(shadow_dds)
        if image:
            uv_region = node_at(nodes, "ShaderNodeUVMap", 500, -400)
            uv_region.uv_map = "SRO_Region"
            shadow = node_at(nodes, "ShaderNodeTexImage", 680, -400)
            shadow.image = image
            shadow.extension = "EXTEND"
            links.new(uv_region.outputs["UV"], shadow.inputs["Vector"])
            smul = node_at(nodes, "ShaderNodeMix", 1040, -200)
            smul.data_type = "RGBA"
            smul.blend_type = "MULTIPLY"
            smul.inputs["Factor"].default_value = float(shadow_strength)
            links.new(accum, smul.inputs[6])
            links.new(shadow.outputs["Color"], smul.inputs[7])
            accum = smul.outputs[2]

    links.new(accum, bsdf.inputs["Base Color"])
    return mat


def water_material():
    mat = bpy.data.materials.get("SRO_Water")
    if mat:
        return mat
    mat = bpy.data.materials.new("SRO_Water")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.11, 0.34, 0.52, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.08
        if "Transmission Weight" in bsdf.inputs:
            bsdf.inputs["Transmission Weight"].default_value = 0.8
        bsdf.inputs["Alpha"].default_value = 0.55
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED"
    return mat


# ===========================================================================
# Terrain construction
# ===========================================================================

def build_terrain_object(region, collection, root, tile2d, options):
    ox = region.rx * REGION_SIZE
    oy = region.rz * REGION_SIZE
    scale = options["scale"]

    verts = []
    heights = region.heights
    for gz in range(GRID):
        wy = (oy + gz * TILE_SIZE) * scale
        for gx in range(GRID):
            verts.append(((ox + gx * TILE_SIZE) * scale,
                          wy,
                          heights[gz * GRID + gx] * scale))

    faces = []
    for gz in range(GRID - 1):
        row = gz * GRID
        for gx in range(GRID - 1):
            a = row + gx
            faces.append((a, a + 1, a + GRID + 1, a + GRID))

    name = "SRO_Terrain_%d_%d" % (region.rz, region.rx)
    mesh = bpy.data.meshes.new(name + "_Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update(calc_edges=True)

    obj = bpy.data.objects.new(name, mesh)
    obj["sro_region_x"] = region.rx
    obj["sro_region_z"] = region.rz
    obj["sro_source"] = region.path
    link(obj, collection)

    # ---- UVs
    uv_tile = mesh.uv_layers.new(name="SRO_Tile")
    uv_region = mesh.uv_layers.new(name="SRO_Region")
    span = float(GRID - 1)
    tile_data = uv_tile.data
    region_data = uv_region.data
    for loop_index, loop in enumerate(mesh.loops):
        vertex = loop.vertex_index
        gx = vertex % GRID
        gz = vertex // GRID
        tile_data[loop_index].uv = (gx, gz)
        region_data[loop_index].uv = (gx / span, gz / span)

    # ---- pesos por textura
    order = []
    counts = {}
    for value in region.tex_ids:
        counts[value] = counts.get(value, 0) + 1
    order = [item[0] for item in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    if options["max_textures"] and len(order) > options["max_textures"]:
        order = order[:options["max_textures"]]
    slot_of = {value: index for index, value in enumerate(order)}
    groups = (len(order) + 3) // 4

    weights = [[0.0] * (GRID * GRID * 4) for _ in range(max(1, groups))]
    for vertex_index, value in enumerate(region.tex_ids):
        slot = slot_of.get(value)
        if slot is None:
            slot = 0
        weights[slot // 4][vertex_index * 4 + (slot % 4)] = 1.0

    for group_index in range(max(1, groups)):
        attr = mesh.color_attributes.new(name="SRO_W%d" % group_index,
                                         type="FLOAT_COLOR", domain="POINT")
        attr.data.foreach_set("color", weights[group_index])

    # ---- brilho por vertice
    bright = mesh.color_attributes.new(name="SRO_Brightness",
                                       type="FLOAT_COLOR", domain="POINT")
    flat = []
    for value in region.brightness:
        v = value / 255.0
        flat.extend((v, v, v, 1.0))
    bright.data.foreach_set("color", flat)

    # ---- material
    shadow_dds = None
    if options["shadow_strength"] > 0.0:
        t_path = str(Path(region.path).with_suffix(".t"))
        if Path(t_path).exists():
            shadow_dds = map_t_to_dds(t_path)

    mat_name = "SRO_Terrain_%d_%d_Mtrl" % (region.rz, region.rx)
    mat = terrain_material(mat_name, order, root, tile2d, shadow_dds,
                           options["use_textures"],
                           options["brightness_strength"],
                           options["shadow_strength"],
                           options["tiles_per_cell"])
    mesh.materials.append(mat)

    if options["shade_smooth"]:
        for poly in mesh.polygons:
            poly.use_smooth = True
    mesh.update()
    return obj


def build_water(region, collection, options):
    if not region.water:
        return []
    ox = region.rx * REGION_SIZE
    oy = region.rz * REGION_SIZE
    scale = options["scale"]
    size = TILES * TILE_SIZE
    verts = []
    faces = []
    for bx, bz, height in region.water:
        x0 = (ox + bx * size) * scale
        y0 = (oy + bz * size) * scale
        x1 = (ox + (bx + 1) * size) * scale
        y1 = (oy + (bz + 1) * size) * scale
        z = height * scale
        base = len(verts)
        verts.extend([(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)])
        faces.append((base, base + 1, base + 2, base + 3))
    mesh = bpy.data.meshes.new("SRO_Water_%d_%d_Mesh" % (region.rz, region.rx))
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh.materials.append(water_material())
    obj = bpy.data.objects.new("SRO_Water_%d_%d" % (region.rz, region.rx), mesh)
    link(obj, collection)
    return [obj]


# ===========================================================================
# Object construction (BSR)
# ===========================================================================

class ModelCache:
    """Builds one Blender mesh per .bsr, with all parts merged."""

    def __init__(self, root, options, library):
        self.root = root
        self.options = options
        self.library = library
        self.meshes = {}
        self.materials = {}
        self.failed = set()
        self.fail_reasons = {}
        self.fail_samples = []

    def _fail(self, key, reason, detail=""):
        self.failed.add(key)
        self.fail_reasons[reason] = self.fail_reasons.get(reason, 0) + 1
        if len(self.fail_samples) < 20:
            self.fail_samples.append((reason, key, str(detail)))

    def material_for(self, entry, bmt_path):
        key = (os.path.normcase(str(bmt_path)), entry.name.lower(),
               str(entry.texture).lower())
        cached = self.materials.get(key)
        if cached:
            return cached

        base = entry.name or os.path.splitext(os.path.basename(str(entry.texture)))[0]
        name = "SRO_%s" % safe_name(base or "mtrl")
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        tree = mat.node_tree
        bsdf = tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Roughness"].default_value = 0.85
            if "Specular IOR Level" in bsdf.inputs:
                bsdf.inputs["Specular IOR Level"].default_value = 0.15
            bsdf.inputs["Base Color"].default_value = (
                entry.diffuse[0], entry.diffuse[1], entry.diffuse[2], 1.0)

        image = None
        if self.options["use_textures"] and entry.texture:
            image, resolved = game_texture_image(self.root, entry.texture)
            if image is None:
                # tenta relativo ao .bmt
                rel = os.path.join(os.path.dirname(str(bmt_path)),
                                   os.path.basename(str(entry.texture).replace("\\", "/")))
                if os.path.exists(rel):
                    image = load_image(ddj_to_dds(rel))

        if image and bsdf:
            tex = node_at(tree.nodes, "ShaderNodeTexImage", -400, 200)
            tex.image = image
            tex.extension = "REPEAT"
            tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            if entry.flags & 0x200:
                tree.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
                if hasattr(mat, "surface_render_method"):
                    mat.surface_render_method = "DITHERED"
                mat.use_backface_culling = False

        mat["sro_material_name"] = entry.name
        mat["sro_texture"] = str(entry.texture or "")
        mat["sro_flags"] = int(entry.flags)
        self.materials[key] = mat
        return mat

    def resolve_materials(self, refs, mesh_path, mesh):
        for candidate in material_candidates(refs.materials, mesh_path, mesh.material_name):
            resolved = self.root.resolve(candidate)
            if not resolved or not resolved.lower().endswith(".bmt"):
                continue
            entries = read_bmt(resolved)
            if not entries:
                continue
            wanted = (mesh.material_name or "").strip().lower()
            for entry in entries:
                if entry.name.strip().lower() == wanted:
                    return self.material_for(entry, resolved)
            return self.material_for(entries[0], resolved)
        return None

    def get(self, bsr_game_path):
        key = str(bsr_game_path).replace("\\", "/").lower()
        if key in self.meshes:
            return self.meshes[key]
        if key in self.failed:
            return None

        resolved = self.root.resolve(bsr_game_path)
        if not resolved:
            self._fail(key, "bsr_nao_encontrado", bsr_game_path)
            return None
        try:
            refs = read_bsr(resolved)
        except Exception as exc:
            print("SRO: error reading bsr %s: %s" % (resolved, exc))
            self._fail(key, "bsr_parse_falhou", "%s (%s)" % (resolved, exc))
            return None

        if not refs.meshes:
            self._fail(key, "bsr_sem_referencia_de_mesh", resolved)
            return None

        verts = []
        faces = []
        uvs = []
        normals = []
        mat_index = []
        materials = []
        mesh_fail_reason = None

        for raw_mesh_path in refs.meshes:
            mesh_file = self.root.resolve(raw_mesh_path)
            if not mesh_file:
                mesh_fail_reason = mesh_fail_reason or ("bms_nao_encontrado", raw_mesh_path)
                continue
            try:
                part = read_bms(mesh_file)
            except Exception as exc:
                print("SRO: error reading bms %s: %s" % (mesh_file, exc))
                mesh_fail_reason = mesh_fail_reason or ("bms_parse_falhou", "%s (%s)" % (mesh_file, exc))
                continue
            if not part or not part.positions or not part.indices:
                mesh_fail_reason = mesh_fail_reason or ("bms_sem_geometria", mesh_file)
                continue

            mat = self.resolve_materials(refs, raw_mesh_path, part)
            if mat is None:
                mat = bpy.data.materials.get("SRO_Untextured")
                if mat is None:
                    mat = bpy.data.materials.new("SRO_Untextured")
                    mat.use_nodes = True
            if mat in materials:
                slot = materials.index(mat)
            else:
                materials.append(mat)
                slot = len(materials) - 1

            base = len(verts)
            verts.extend(part.positions)
            normals.extend(part.normals)
            for tri in part.indices:
                faces.append((tri[0] + base, tri[1] + base, tri[2] + base))
                uvs.append((part.uvs[tri[0]], part.uvs[tri[1]], part.uvs[tri[2]]))
                mat_index.append(slot)

        if not verts or not faces:
            reason, detail = mesh_fail_reason or ("bsr_sem_geometria_final", resolved)
            self._fail(key, reason, detail)
            return None

        name = safe_name(Path(resolved).stem)
        mesh = bpy.data.meshes.new("SRO_M_%s" % name)
        mesh.from_pydata(verts, [], faces)
        mesh.update(calc_edges=True)

        for mat in materials:
            mesh.materials.append(mat)

        uv_layer = mesh.uv_layers.new(name="UVMap")
        data = uv_layer.data
        for poly_index, poly in enumerate(mesh.polygons):
            if poly_index < len(mat_index):
                poly.material_index = mat_index[poly_index]
            poly.use_smooth = True
            triangle_uv = uvs[poly_index] if poly_index < len(uvs) else ((0, 0),) * 3
            for corner, loop_index in enumerate(poly.loop_indices):
                if corner < 3:
                    data[loop_index].uv = triangle_uv[corner]

        if self.options["custom_normals"] and len(normals) == len(verts):
            try:
                mesh.normals_split_custom_set_from_vertices(normals)
            except Exception:
                pass
        mesh.update()

        self.meshes[key] = mesh
        return mesh


def place_object(mesh, world_object, collection, options, index, resource_path):
    scale = options["scale"]
    wx = (world_object.region_x * REGION_SIZE + world_object.x) * scale
    wy = (world_object.region_z * REGION_SIZE + world_object.z) * scale
    wz = world_object.y * scale

    name = "%s_%05d" % (safe_name(Path(str(resource_path)).stem), index)
    if options["unique_mesh"]:
        obj = bpy.data.objects.new(name, mesh.copy())
    else:
        obj = bpy.data.objects.new(name, mesh)

    obj.location = (wx, wy, wz)
    obj.scale = (scale, scale, scale)
    obj.rotation_euler = (0.0, 0.0, world_object.yaw * options["yaw_sign"])
    obj["sro_resource"] = str(resource_path)
    obj["sro_object_id"] = int(world_object.oid)
    obj["sro_region"] = "%d_%d" % (world_object.region_z, world_object.region_x)
    if world_object.lod is not None:
        obj["sro_lod"] = int(world_object.lod)
    link(obj, collection)
    return obj


# ===========================================================================
# Region selection
# ===========================================================================

def region_from_path(path):
    """.../Map/<z>/<x>.m  ->  (x, z)"""
    p = Path(path)
    try:
        return int(p.stem), int(p.parent.name)
    except ValueError:
        return None


def expand_regions(paths, radius, extension):
    """Expands the selection to neighbours within the radius."""
    selected = {}
    for path in paths:
        coord = region_from_path(path)
        if coord:
            selected[coord] = str(path)

    if radius <= 0:
        return selected

    for (rx, rz), path in list(selected.items()):
        map_root = Path(path).parent.parent
        for dz in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                key = (rx + dx, rz + dz)
                if key in selected:
                    continue
                candidate = map_root / str(rz + dz) / ("%d%s" % (rx + dx, extension))
                if candidate.exists():
                    selected[key] = str(candidate)
    return selected



def regions_from_ids(map_folder, region_ids, extension):
    """worlds.json ids -> {(x, z): file path}"""
    folder = Path(map_folder)
    out = {}
    for region_id in region_ids:
        rx = int(region_id) & 0xFF
        rz = (int(region_id) >> 8) & 0x7F
        candidate = folder / str(rz) / ("%d%s" % (rx, extension))
        if candidate.exists():
            out[(rx, rz)] = str(candidate)
    return out


# ===========================================================================
# Area catalog (worlds.json)
# ===========================================================================

_WORLDS = {"path": None, "areas": [], "by_key": {}}
_AREA_ITEMS = [("NONE", "(carregue o worlds.json)", "")]


def load_worlds(path, report=None):
    global _AREA_ITEMS
    _WORLDS["path"] = None
    _WORLDS["areas"] = []
    _WORLDS["by_key"] = {}
    _AREA_ITEMS = [("NONE", "(carregue o worlds.json)", "")]

    if not path or not Path(path).is_file():
        return 0
    import json
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        if report:
            report("worlds.json invalido: %s" % exc)
        return 0

    areas = data.get("areas") or []
    areas = sorted(areas, key=lambda a: (str(a.get("kind") or ""), str(a.get("name") or "")))
    _WORLDS["path"] = str(path)
    _WORLDS["areas"] = areas
    _WORLDS["by_key"] = {str(a.get("key")): a for a in areas}
    return len(areas)


def area_items(self, context):
    """Dropdown items, filtered by the search box. Kept in a global list."""
    global _AREA_ITEMS
    areas = _WORLDS["areas"]
    if not areas:
        _AREA_ITEMS = [("NONE", "(carregue o worlds.json)", "")]
        return _AREA_ITEMS

    needle = ""
    try:
        needle = (context.scene.sro_map.search or "").strip().lower()
    except Exception:
        needle = ""

    items = []
    for area in areas:
        key = str(area.get("key") or "")
        name = str(area.get("name") or key)
        kind = str(area.get("kind") or "")
        count = len(area.get("regions") or [])
        haystack = (key + " " + name + " " + kind).lower()
        if needle and needle not in haystack:
            continue
        label = "%s | %s | %d regioes" % (name, kind, count)
        items.append((key, label, key))
        if len(items) >= 800:
            break
    if not items:
        items = [("NONE", "(nenhuma area corresponde a busca)", "")]
    _AREA_ITEMS = items
    return _AREA_ITEMS


def selected_area(context):
    settings = context.scene.sro_map
    return _WORLDS["by_key"].get(settings.area)


# ===========================================================================
# Catalog generator built from the extracted client
#
# sro-archive-explorer's worlds.json comes from the server database (_RefRegion).
# Here we build the equivalent by reading only the client:
#   - varre Map/<z>/<x>.m para saber quais regioes existem
#   - liga as regioes vizinhas (flood fill) para formar continentes
#   - le os .o/.o2 e resolve os ids no object.ifo; o caminho dos .bsr de
#     construcao (res/bldg/<continente>/<cidade>/...) da o nome da area
# ===========================================================================

_GENERIC_DIRS = {"res", "resource", "object", "objects", "obj", "mapobj",
                 "data", "map", "bldg", "building", "nature", "etc", "dungeon"}


def scan_map_regions(map_folder):
    """{(x, z): .m path} by scanning Map/<z>/<x>.m"""
    folder = Path(map_folder)
    found = {}
    if not folder.is_dir():
        return found
    for entry in folder.iterdir():
        if not entry.is_dir():
            continue
        try:
            rz = int(entry.name)
        except ValueError:
            continue
        for item in entry.iterdir():
            if item.suffix.lower() != ".m":
                continue
            try:
                rx = int(item.stem)
            except ValueError:
                continue
            found[(rx, rz)] = str(item)
    return found


def connected_components(coords):
    """Groups neighbouring regions (4 directions) into connected blocks."""
    remaining = set(coords)
    groups = []
    while remaining:
        start = remaining.pop()
        group = [start]
        queue = [start]
        while queue:
            cx, cz = queue.pop()
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbour = (cx + dx, cz + dz)
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    group.append(neighbour)
                    queue.append(neighbour)
        groups.append(sorted(group))
    groups.sort(key=len, reverse=True)
    return groups


def _resource_tokens(resource_path):
    """res/bldg/china/jangan/x.bsr -> ('china', 'jangan', is_building)"""
    parts = [p for p in str(resource_path).replace("\\", "/").lower().split("/") if p]
    if not parts:
        return None, None, False
    building = "bldg" in parts or "building" in parts
    meaningful = [p for p in parts[:-1] if p not in _GENERIC_DIRS]
    if not meaningful:
        return None, None, building
    continent = meaningful[0]
    place = meaningful[1] if len(meaningful) > 1 else None
    return continent, place, building


def region_labels(map_folder, regions, objects_ifo, progress=None):
    """{(x, z): (continent, city)} inferred from each region's objects."""
    labels = {}
    total = len(regions)
    for index, ((rx, rz), mesh_path) in enumerate(sorted(regions.items())):
        if progress and index % 200 == 0:
            progress("catalogo: %d/%d regioes" % (index, total))
        base = Path(mesh_path)
        object_path = None
        for extension in (".o2", ".o"):
            candidate = base.with_suffix(extension)
            if candidate.exists():
                object_path = candidate
                break
        if not object_path:
            continue
        try:
            entries = read_objects(str(object_path), rx, rz)
        except Exception:
            continue

        continents = {}
        places = {}
        for entry in entries:
            resource = objects_ifo.get(entry.oid)
            if not resource:
                continue
            continent, place, building = _resource_tokens(resource)
            weight = 3 if building else 1
            if continent:
                continents[continent] = continents.get(continent, 0) + weight
            if place and building:
                places[place] = places.get(place, 0) + 1

        continent = max(continents, key=continents.get) if continents else None
        place = None
        if places:
            best = max(places, key=places.get)
            # so aceita o nome da cidade se ele realmente domina a regiao
            if places[best] >= 3 and places[best] >= 0.4 * sum(places.values()):
                place = best
        labels[(rx, rz)] = (continent, place)
    return labels


def _bounds(coords):
    xs = [c[0] for c in coords]
    zs = [c[1] for c in coords]
    return {"minX": min(xs), "maxX": max(xs), "minZ": min(zs), "maxZ": max(zs)}


def _region_id(coord):
    return (int(coord[1]) << 8) | int(coord[0])


def build_catalog(map_folder, game_root, merge_path=None, progress=None):
    """Builds the catalog and returns the dict ready to save."""
    regions = scan_map_regions(map_folder)
    if not regions:
        raise ValueError("Nenhum arquivo .m encontrado em %s" % map_folder)
    if progress:
        progress("encontradas %d regioes" % len(regions))

    root = get_game_root(game_root, progress)
    objects_ifo = read_object_ifo(root) if root.ok else {}
    labels = region_labels(map_folder, regions, objects_ifo, progress) if objects_ifo else {}

    areas = []

    # continentes = blocos de regioes conectadas
    for group in connected_components(regions.keys()):
        names = {}
        for coord in group:
            continent = labels.get(coord, (None, None))[0]
            if continent:
                names[continent] = names.get(continent, 0) + 1
        if names:
            name = max(names, key=names.get)
        else:
            name = "regiao_%d_%d" % (group[0][1], group[0][0])
        key = "continent:%s" % name
        suffix = 2
        existing = {area["key"] for area in areas}
        while key in existing:
            key = "continent:%s_%d" % (name, suffix)
            suffix += 1
        areas.append({
            "key": key, "continent": name, "name": name, "kind": "continent",
            "regions": [_region_id(c) for c in group], "bounds": _bounds(group),
        })

    # areas = regioes que compartilham a mesma cidade dominante
    cities = {}
    for coord, (continent, place) in labels.items():
        if not place:
            continue
        cities.setdefault((continent or "?", place), []).append(coord)
    for (continent, place), coords in sorted(cities.items()):
        coords.sort()
        areas.append({
            "key": "%s:%s" % (continent, place), "continent": continent,
            "name": place, "kind": "area",
            "regions": [_region_id(c) for c in coords], "bounds": _bounds(coords),
        })

    # opcional: aproveita um worlds.json existente, trocando nomes ilegiveis
    merged = 0
    renamed = 0
    if merge_path and Path(merge_path).is_file():
        import json as _json
        try:
            source = _json.loads(Path(merge_path).read_text(encoding="utf-8"))
        except Exception:
            source = None
        if source:
            existing_keys = {area["key"] for area in areas}
            for area in source.get("areas") or []:
                key = str(area.get("key") or "")
                if not key or key in existing_keys:
                    continue
                ids = [i for i in (area.get("regions") or [])
                       if (int(i) & 0xFF, (int(i) >> 8) & 0x7F) in regions]
                if not ids:
                    continue
                coords = [(int(i) & 0xFF, (int(i) >> 8) & 0x7F) for i in ids]
                name = str(area.get("name") or "")
                if not name.strip() or set(name.strip()) <= set("? "):
                    votes = {}
                    for coord in coords:
                        continent, place = labels.get(coord, (None, None))
                        pick = place or continent
                        if pick:
                            votes[pick] = votes.get(pick, 0) + 1
                    if votes:
                        name = max(votes, key=votes.get)
                        renamed += 1
                    else:
                        name = "%s %d.%d" % (area.get("continent") or "area",
                                             min(c[0] for c in coords),
                                             min(c[1] for c in coords))
                        renamed += 1
                areas.append({
                    "key": key, "continent": str(area.get("continent") or ""),
                    "name": name, "kind": str(area.get("kind") or "area"),
                    "regions": ids, "bounds": _bounds(coords),
                })
                merged += 1

    return {
        "version": 1,
        "source": "cliente extraido",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "regionSize": REGION_SIZE,
        "regions": [{"id": _region_id(c), "x": c[0], "z": c[1],
                     "continent": (labels.get(c, (None, None))[0] or ""),
                     "area": (labels.get(c, (None, None))[1] or "")}
                    for c in sorted(regions.keys())],
        "areas": areas,
        "stats": {"regions": len(regions), "areas": len(areas),
                  "mergedFromJson": merged, "renamed": renamed},
    }


# ===========================================================================
# Core import functions (used by the panel and by File > Import)
# ===========================================================================

def do_import_terrain(regions, settings, report):
    root = get_game_root(settings["game_root"], lambda msg: print("SRO:", msg))
    if not root.ok and settings["use_textures"]:
        report({"WARNING"}, "Game Root invalido: importando sem texturas")
    tile2d = read_tile2d(root)
    textures_found = int(tile2d.get("__count__", 0))

    collection = get_collection(settings["collection_name"])
    options = {
        "scale": settings["scale"],
        "use_textures": settings["use_textures"] and root.ok,
        "tiles_per_cell": settings["tiles_per_cell"],
        "brightness_strength": settings["brightness_strength"],
        "shadow_strength": settings["shadow_strength"],
        "max_textures": settings["max_textures"],
        "shade_smooth": settings["shade_smooth"],
    }

    made = 0
    failed = 0
    for (rx, rz), path in sorted(regions.items()):
        try:
            region = read_terrain(path, rx, rz)
            target = get_collection("R_%d_%d" % (rz, rx), collection)
            build_terrain_object(region, target, root, tile2d, options)
            if settings["import_water"]:
                build_water(region, target, options)
            made += 1
        except Exception as exc:
            failed += 1
            print("SRO: terrain failure %s: %s" % (path, exc))
            traceback.print_exc()

    report({"INFO"}, "Terreno: %d regioes, %d falhas, %d texturas no tile2d.ifo"
           % (made, failed, textures_found))
    return made


def do_import_objects(regions, settings, report):
    root = get_game_root(settings["game_root"], lambda msg: print("SRO:", msg))
    if not root.ok:
        report({"ERROR"}, "Game Root invalido. Aponte para a pasta extraida.")
        return 0

    objects_ifo = read_object_ifo(root)
    if not objects_ifo:
        report({"ERROR"}, "object.ifo nao encontrado no Game Root.")
        return 0

    collection = get_collection(settings["objects_collection"])
    options = {
        "scale": settings["scale"],
        "use_textures": settings["use_textures"],
        "unique_mesh": settings["unique_mesh"],
        "custom_normals": settings["custom_normals"],
        "yaw_sign": settings["yaw_sign"],
    }
    cache = ModelCache(root, options, None)

    placed = 0
    missing = 0
    unknown = set()
    index = 0

    for (rx, rz), path in sorted(regions.items()):
        try:
            entries = read_objects(path, rx, rz)
        except Exception as exc:
            print("SRO: failed reading %s: %s" % (path, exc))
            continue

        if settings["skip_lod"]:
            entries = [e for e in entries if e.lod in (None, 0)]

        region_collection = collection
        if settings["group_by_region"]:
            region_collection = get_collection("R_%d_%d" % (rz, rx), collection)

        for entry in entries:
            if placed >= settings["max_objects"]:
                break
            resource = objects_ifo.get(entry.oid)
            if not resource:
                unknown.add(entry.oid)
                missing += 1
                continue
            mesh = cache.get(resource)
            if mesh is None:
                missing += 1
                continue
            target = region_collection
            if settings["group_by_resource"]:
                target = get_collection(safe_name(Path(resource).stem), region_collection)
            place_object(mesh, entry, target, options, index, resource)
            placed += 1
            index += 1
        if placed >= settings["max_objects"]:
            break

    if cache.fail_reasons:
        breakdown = ", ".join("%s=%d" % (k, v) for k, v in
                              sorted(cache.fail_reasons.items(), key=lambda kv: -kv[1]))
        print("SRO: object failure reasons ->", breakdown)
        for reason, key, detail in cache.fail_samples:
            print("  [%s] %s -> %s" % (reason, key, detail))

    report({"INFO"}, "Objetos: %d colocados, %d sem modelo (%d ids desconhecidos no object.ifo), %d BSR unicos carregados, %d BSR com falha"
           % (placed, missing, len(unknown), len(cache.meshes), len(cache.failed)))
    if cache.fail_reasons:
        report({"INFO"}, "Detalhe das falhas: %s (veja o console do sistema para exemplos)"
               % ", ".join("%s=%d" % (k, v) for k, v in
                           sorted(cache.fail_reasons.items(), key=lambda kv: -kv[1])))
    return placed


def settings_dict(source):
    """Accepts the panel's PropertyGroup or a File > Import operator."""
    def pick(name, default):
        return getattr(source, name, default)

    return {
        "game_root": pick("game_root", ""),
        "scale": pick("scale", 0.01),
        "use_textures": pick("use_textures", True),
        "tiles_per_cell": pick("tiles_per_cell", 1.0),
        "brightness_strength": pick("brightness_strength", 0.35),
        "shadow_strength": pick("shadow_strength", 0.35),
        "max_textures": pick("max_textures", 32),
        "import_water": pick("import_water", True),
        "shade_smooth": pick("shade_smooth", True),
        "collection_name": pick("collection_name", "SRO_TERRAIN"),
        "objects_collection": pick("objects_collection", "SRO_OBJECTS"),
        "unique_mesh": pick("unique_mesh", False),
        "custom_normals": pick("custom_normals", True),
        "group_by_region": pick("group_by_region", True),
        "group_by_resource": pick("group_by_resource", False),
        "skip_lod": pick("skip_lod", False),
        "max_objects": pick("max_objects", 20000),
        "yaw_sign": -1.0 if pick("yaw_direction", "NORMAL") == "INVERTED" else 1.0,
    }


# ===========================================================================
# Panel properties
# ===========================================================================

class SRO_MapSettings(bpy.types.PropertyGroup):
    game_root: StringProperty(
        name="Game Root", subtype="DIR_PATH", default=DEFAULT_GAME_ROOT,
        description="Root with Data/, Map/, res/, Media/")
    map_folder: StringProperty(
        name="Map", subtype="DIR_PATH", default="",
        description="Map folder with the subfolders numbered by Z")
    worlds_json: StringProperty(
        name="worlds.json", subtype="FILE_PATH", default="",
        description="Area catalog from sro-archive-explorer (src/assets/worlds.json)")

    catalog_output: StringProperty(
        name="Save Catalog To", subtype="FILE_PATH", default="",
        description="Where to write the generated catalog. Empty = <Game Root>/sro_worlds.json")
    merge_worlds: BoolProperty(
        name="Merge worlds.json", default=True,
        description="Also bring in areas from the worlds.json above, "
                    "replacing unreadable names (???) with names found in the client")

    search: StringProperty(name="Search Area", default="",
                           description="Filters the area list")
    area: EnumProperty(name="Area", items=area_items)

    neighbor_radius: IntProperty(name="Neighbor Radius", default=0, min=0, max=6,
                                 description="Also load the regions surrounding the area")
    max_regions: IntProperty(name="Max Regions", default=64, min=1, max=2500,
                             description="Caps huge areas; use 2500 to allow everything")

    scale: FloatProperty(name="Scale", default=0.01, min=0.000001)
    use_textures: BoolProperty(name="Textures", default=True)
    tiles_per_cell: FloatProperty(name="Texture Repeat", default=1.0, min=0.01, max=16.0)
    brightness_strength: FloatProperty(name="Vertex Brightness", default=0.35, min=0.0, max=1.0)
    shadow_strength: FloatProperty(name="Region Shadow (.t)", default=0.35, min=0.0, max=1.0)
    max_textures: IntProperty(name="Max Textures/Region", default=32, min=4, max=64)
    import_water: BoolProperty(name="Water", default=True)
    shade_smooth: BoolProperty(name="Shade Smooth", default=True)
    collection_name: StringProperty(name="Terrain Collection", default="SRO_TERRAIN")

    objects_collection: StringProperty(name="Objects Collection", default="SRO_OBJECTS")
    unique_mesh: BoolProperty(name="Unique Mesh per Instance", default=False)
    custom_normals: BoolProperty(name="Use File Normals", default=True)
    group_by_region: BoolProperty(name="Group by Region", default=True)
    group_by_resource: BoolProperty(name="Group by Model", default=False)
    skip_lod: BoolProperty(name="Skip LOD Groups", default=False)
    max_objects: IntProperty(name="Object Limit", default=20000, min=1, max=500000)
    yaw_direction: EnumProperty(
        name="Yaw",
        items=[("NORMAL", "Normal", "Yaw as stored in the file"),
               ("INVERTED", "Inverted", "Use if buildings end up rotated backwards")],
        default="NORMAL")

    show_terrain_options: BoolProperty(name="Terrain Options", default=False)
    show_object_options: BoolProperty(name="Object Options", default=False)


# ===========================================================================
# Panel operators
# ===========================================================================

def resolved_map_folder(settings):
    if settings.map_folder and Path(bpy.path.abspath(settings.map_folder)).is_dir():
        return bpy.path.abspath(settings.map_folder)
    root = bpy.path.abspath(settings.game_root) if settings.game_root else ""
    if root:
        for name in ("Map", "map", "Data/Map", "data/map"):
            candidate = Path(root) / name
            if candidate.is_dir():
                return str(candidate)
    return ""


def area_region_files(context, extension, report):
    settings = context.scene.sro_map
    area = selected_area(context)
    if not area:
        report({"ERROR"}, "Choose an area (load worlds.json first).")
        return None

    map_folder = resolved_map_folder(settings)
    if not map_folder:
        report({"ERROR"}, "Map folder not found. Fill in the Map field.")
        return None

    ids = list(area.get("regions") or [])
    regions = regions_from_ids(map_folder, ids, extension)
    if not regions and extension == ".o2":
        regions = regions_from_ids(map_folder, ids, ".o")
    if not regions:
        report({"ERROR"}, "No %s file found in %s for that area."
               % (extension, map_folder))
        return None

    if settings.neighbor_radius > 0:
        regions = expand_regions(list(regions.values()), settings.neighbor_radius,
                                 os.path.splitext(next(iter(regions.values())))[1])

    if len(regions) > settings.max_regions:
        center_x = sum(k[0] for k in regions) / len(regions)
        center_z = sum(k[1] for k in regions) / len(regions)
        ordered = sorted(regions.items(),
                         key=lambda kv: math.hypot(kv[0][0] - center_x, kv[0][1] - center_z))
        regions = dict(ordered[:settings.max_regions])
        report({"WARNING"}, "Area capped to %d regions (closest to the center)."
               % settings.max_regions)
    return regions


class SRO_OT_reload_areas(bpy.types.Operator):
    bl_idname = "sro.reload_areas"
    bl_label = "Reload Areas"
    bl_description = "Reads worlds.json and fills the area list"

    def execute(self, context):
        settings = context.scene.sro_map
        path = bpy.path.abspath(settings.worlds_json) if settings.worlds_json else ""
        if not path:
            root = bpy.path.abspath(settings.game_root) if settings.game_root else ""
            guess = Path(root).parent / "sro-archive-explorer-main" / "src" / "assets" / "worlds.json"
            if guess.is_file():
                path = str(guess)
                settings.worlds_json = path
        count = load_worlds(path, lambda msg: self.report({"WARNING"}, msg))
        if count == 0:
            self.report({"ERROR"}, "No areas loaded. Check the worlds.json path.")
            return {"CANCELLED"}
        self.report({"INFO"}, "Areas loaded: %d" % count)
        return {"FINISHED"}


class SRO_OT_build_catalog(bpy.types.Operator):
    bl_idname = "sro.build_catalog"
    bl_label = "Generate Client Catalog"
    bl_description = ("Scans the Map folder and builds the area catalog by reading only "
                      "the extracted client, without depending on the server's worlds.json")

    def execute(self, context):
        import json
        settings = context.scene.sro_map
        map_folder = resolved_map_folder(settings)
        if not map_folder:
            self.report({"ERROR"}, "Map folder not found. Fill in the Map field.")
            return {"CANCELLED"}

        game_root = bpy.path.abspath(settings.game_root) if settings.game_root else ""
        merge = bpy.path.abspath(settings.worlds_json) if (settings.merge_worlds and settings.worlds_json) else None

        output = bpy.path.abspath(settings.catalog_output) if settings.catalog_output else ""
        if not output:
            base = Path(game_root) if game_root else Path(map_folder).parent
            output = str(base / "sro_worlds.json")

        try:
            catalog = build_catalog(map_folder, game_root, merge,
                                    lambda msg: print("SRO:", msg))
        except Exception as exc:
            self.report({"ERROR"}, "Failed to generate catalog: %s" % exc)
            traceback.print_exc()
            return {"CANCELLED"}

        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(json.dumps(catalog, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        except Exception as exc:
            self.report({"ERROR"}, "Could not write %s: %s" % (output, exc))
            return {"CANCELLED"}

        settings.catalog_output = output
        settings.worlds_json = output
        load_worlds(output)
        stats = catalog["stats"]
        self.report({"INFO"},
                    "Catalog: %d regions, %d areas (%d merged from worlds.json, %d renamed) -> %s"
                    % (stats["regions"], stats["areas"], stats["mergedFromJson"],
                       stats["renamed"], output))
        return {"FINISHED"}


class SRO_OT_panel_import_terrain(bpy.types.Operator):
    bl_idname = "sro.panel_import_terrain"
    bl_label = "Import Map"
    bl_description = "Imports the terrain for every region in the chosen area"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        regions = area_region_files(context, ".m", self.report)
        if regions is None:
            return {"CANCELLED"}
        settings = settings_dict(context.scene.sro_map)
        settings["game_root"] = bpy.path.abspath(context.scene.sro_map.game_root)
        do_import_terrain(regions, settings, self.report)
        return {"FINISHED"}


class SRO_OT_panel_import_objects(bpy.types.Operator):
    bl_idname = "sro.panel_import_objects"
    bl_label = "Import Objects"
    bl_description = "Imports the objects for every region in the chosen area"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        regions = area_region_files(context, ".o2", self.report)
        if regions is None:
            return {"CANCELLED"}
        settings = settings_dict(context.scene.sro_map)
        settings["game_root"] = bpy.path.abspath(context.scene.sro_map.game_root)
        do_import_objects(regions, settings, self.report)
        return {"FINISHED"}


class SRO_OT_diagnose(bpy.types.Operator):
    bl_idname = "sro.diagnose"
    bl_label = "Diagnose Textures"
    bl_description = ("Prints to the console what was found in tile2d.ifo and "
                      "object.ifo, and whether the textures resolve on disk")

    def execute(self, context):
        settings = context.scene.sro_map
        root = get_game_root(bpy.path.abspath(settings.game_root),
                             lambda msg: print("SRO:", msg))
        print("=" * 70)
        print("SRO DIAGNOSTIC")
        print("Game root:", settings.game_root, "| indexed:", root.ok,
              "|", len(root.by_rel), "files")
        if not root.ok:
            self.report({"ERROR"}, "Invalid Game Root.")
            return {"CANCELLED"}

        tile_path = root.find_any(["tile2d.ifo", "Map/tile2d.ifo", "Data/tile2d.ifo",
                                   "Data/Map/tile2d.ifo"])
        print("tile2d.ifo:", tile_path)
        if tile_path:
            raw_lines = _ifo_lines(tile_path)
            print("  lines:", len(raw_lines))
            print("  --- first 12 raw lines ---")
            for line in raw_lines[:12]:
                print("   |", line[:160])
            entries = _tile2d_entries(tile_path)
            print("  entries recognized:", len(entries))
            for item in entries[:8]:
                print("   ->", item)
            ok = 0
            miss = []
            for _fid, texture in entries[:400]:
                if root.resolve(texture):
                    ok += 1
                elif len(miss) < 8:
                    miss.append(texture)
            print("  resolved on disk (sample of 400): %d" % ok)
            for item in miss:
                print("   NOT FOUND:", item)
            found = [k for k in root.by_rel if "/tile2d/" in k][:8]
            print("  example files under tile2d/ on disk:")
            for item in found:
                print("   *", item)
        else:
            print("  NOT FOUND. Locate where tile2d.ifo is in your folder.")

        obj_path = root.find_any(["object.ifo", "Map/object.ifo", "Data/object.ifo",
                                  "Data/Map/object.ifo"])
        print("object.ifo:", obj_path, "| entries:", len(read_object_ifo(root)))
        print("=" * 70)
        self.report({"INFO"}, "Diagnostic printed to the system console (Window > Toggle System Console).")
        return {"FINISHED"}


class SRO_OT_diagnose_objects(bpy.types.Operator):
    bl_idname = "sro.diagnose_objects"
    bl_label = "Diagnose Objects"
    bl_description = ("Takes a sample of objects from one region and shows, step by "
                      "step, where the .bsr/.bms reading is failing")

    def execute(self, context):
        settings = context.scene.sro_map
        root = get_game_root(bpy.path.abspath(settings.game_root),
                             lambda msg: print("SRO:", msg))
        print("=" * 70)
        print("SRO OBJECT DIAGNOSTIC")
        print("Game root:", settings.game_root, "| indexed:", root.ok,
              "|", len(root.by_rel), "files")
        if not root.ok:
            self.report({"ERROR"}, "Invalid Game Root.")
            return {"CANCELLED"}

        obj_path = root.find_any(["object.ifo", "Map/object.ifo", "Data/object.ifo",
                                  "Data/Map/object.ifo"])
        print("object.ifo:", obj_path)
        if not obj_path:
            print("  NOT FOUND.")
            self.report({"ERROR"}, "object.ifo not found.")
            return {"CANCELLED"}

        raw_lines = _ifo_lines(obj_path)
        print("  lines:", len(raw_lines))
        print("  --- first 10 raw lines ---")
        for line in raw_lines[:10]:
            print("   |", line[:160])

        objects_ifo = read_object_ifo(root)
        print("  entries recognized:", len(objects_ifo))
        sample_resources = list(dict.fromkeys(objects_ifo.values()))[:6]
        print("  --- sample of resources pointed to by object.ifo ---")
        for resource in sample_resources:
            print("   *", resource)

        map_folder = resolved_map_folder(settings)
        region_file = None
        if map_folder:
            regions = scan_map_regions(map_folder)
            for (rx, rz), path in sorted(regions.items()):
                for ext in (".o2", ".o"):
                    candidate = Path(path).with_suffix(ext)
                    if candidate.exists():
                        region_file = str(candidate)
                        break
                if region_file:
                    break
        print("Test region:", region_file)

        oids = []
        if region_file:
            rx = int(Path(region_file).stem)
            rz = int(Path(region_file).parent.name)
            try:
                entries = read_objects(region_file, rx, rz)
                oids = list(dict.fromkeys(e.oid for e in entries))[:6]
            except Exception as exc:
                print("  error reading the test region:", exc)

        if not oids:
            oids = list(objects_ifo.keys())[:6]

        print("  --- tracing %d sample objects ---" % len(oids))
        example = ["res/bldg/china/jangan/ja_house_01.bsr",
                   "bldg\\china\\jangan\\ja_house_01.bsr"]
        found_bsr_sample = [k for k in root.by_rel if k.endswith(".bsr")][:6]
        print("  .bsr files found on disk (sample):")
        for item in found_bsr_sample:
            print("   *", item)
        if not found_bsr_sample:
            print("   NO .bsr file was indexed in this Game Root!")

        for oid in oids:
            resource = objects_ifo.get(oid)
            print("  oid=%s -> object.ifo says: %r" % (oid, resource))
            if not resource:
                print("     (id is not in object.ifo)")
                continue
            resolved = root.resolve(resource)
            print("     resolve() ->", resolved)
            if not resolved:
                print("     FAILED: file not found on disk with that path/name.")
                continue
            try:
                data = Path(resolved).read_bytes()
                print("     signature:", data[:12])
                refs = read_bsr(resolved)
                print("     materials:", len(refs.materials), "| meshes:", len(refs.meshes),
                      "| skeleton:", refs.skeleton, "| collision:", refs.collision)
                if refs.meshes:
                    first = refs.meshes[0]
                    mesh_resolved = root.resolve(first)
                    print("     first mesh:", first, "-> resolve() ->", mesh_resolved)
                    if mesh_resolved:
                        part = read_bms(mesh_resolved)
                        if part:
                            print("     bms ok: %d vertices, %d faces, material_name=%r"
                                  % (len(part.positions), len(part.indices), part.material_name))
                        else:
                            print("     bms: JMXVBMS signature not found / invalid offsets")
                else:
                    print("     BSR has no mesh reference at all (offsets[1] may be wrong, "
                          "or the file isn't a regular object .bsr)")
            except Exception as exc:
                print("     ERROR:", exc)
                traceback.print_exc()

        print("=" * 70)
        self.report({"INFO"}, "Diagnostic printed to the system console (Window > Toggle System Console).")
        return {"FINISHED"}


class SRO_OT_clear_cache(bpy.types.Operator):
    bl_idname = "sro.clear_cache"
    bl_label = "Clear Cache"
    bl_description = "Forces re-indexing the Game Root and re-reading tile2d.ifo / object.ifo"

    def execute(self, context):
        _ROOT_INDEX.clear()
        _IFO_CACHE.clear()
        _IMAGE_CACHE.clear()
        self.report({"INFO"}, "Cache cleared.")
        return {"FINISHED"}


# ===========================================================================
# N-panel
# ===========================================================================

class SRO_PT_map(bpy.types.Panel):
    bl_label = "Silkroad Map"
    bl_idname = "SRO_PT_map"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Silkroad_3dtools"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sro_map

        box = layout.box()
        box.label(text="Files", icon="FILE_FOLDER")
        box.prop(settings, "game_root")
        box.prop(settings, "map_folder")
        box.prop(settings, "worlds_json")
        row = box.row()
        row.operator(SRO_OT_reload_areas.bl_idname, icon="FILE_REFRESH")
        row.operator(SRO_OT_clear_cache.bl_idname, icon="TRASH")
        box.separator()
        box.prop(settings, "catalog_output")
        box.prop(settings, "merge_worlds")
        box.operator(SRO_OT_build_catalog.bl_idname, icon="FILE_NEW")

        box = layout.box()
        box.label(text="Area", icon="WORLD")
        box.prop(settings, "search", icon="VIEWZOOM")
        box.prop(settings, "area", text="")
        area = selected_area(context)
        if area:
            bounds = area.get("bounds") or {}
            box.label(text="%d regions | X %s-%s  Z %s-%s"
                      % (len(area.get("regions") or []),
                         bounds.get("minX", "?"), bounds.get("maxX", "?"),
                         bounds.get("minZ", "?"), bounds.get("maxZ", "?")))
        else:
            box.label(text="No area loaded", icon="INFO")
        row = box.row()
        row.prop(settings, "neighbor_radius")
        row.prop(settings, "max_regions")

        layout.prop(settings, "scale")

        column = layout.column(align=True)
        column.scale_y = 1.6
        column.operator(SRO_OT_panel_import_terrain.bl_idname, icon="MESH_GRID")
        column.operator(SRO_OT_panel_import_objects.bl_idname, icon="OUTLINER_OB_GROUP_INSTANCE")

        box = layout.box()
        box.prop(settings, "show_terrain_options",
                 icon="TRIA_DOWN" if settings.show_terrain_options else "TRIA_RIGHT",
                 emboss=False)
        if settings.show_terrain_options:
            box.prop(settings, "use_textures")
            box.prop(settings, "tiles_per_cell")
            box.prop(settings, "brightness_strength")
            box.prop(settings, "shadow_strength")
            box.prop(settings, "max_textures")
            box.prop(settings, "import_water")
            box.prop(settings, "shade_smooth")
            box.prop(settings, "collection_name")

        box = layout.box()
        box.prop(settings, "show_object_options",
                 icon="TRIA_DOWN" if settings.show_object_options else "TRIA_RIGHT",
                 emboss=False)
        if settings.show_object_options:
            box.prop(settings, "unique_mesh")
            box.prop(settings, "custom_normals")
            box.prop(settings, "yaw_direction")
            box.prop(settings, "group_by_region")
            box.prop(settings, "group_by_resource")
            box.prop(settings, "skip_lod")
            box.prop(settings, "max_objects")
            box.prop(settings, "objects_collection")

        layout.operator(SRO_OT_diagnose.bl_idname, icon="CONSOLE")
        layout.operator(SRO_OT_diagnose_objects.bl_idname, icon="CONSOLE")


# ===========================================================================
# File > Import (manual chunk selection)
# ===========================================================================

class SRO_OT_import_terrain(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.sro_terrain_direct"
    bl_label = "Import Silkroad Terrain (.m)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".m"
    filter_glob: StringProperty(default="*.m", options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})

    game_root: StringProperty(name="Game Root", subtype="DIR_PATH", default=DEFAULT_GAME_ROOT)
    neighbor_radius: IntProperty(name="Neighbor Radius", default=0, min=0, max=6)
    collection_name: StringProperty(name="Collection", default="SRO_TERRAIN")
    scale: FloatProperty(name="Scale", default=0.01, min=0.000001)
    use_textures: BoolProperty(name="Textures", default=True)
    tiles_per_cell: FloatProperty(name="Texture Repeat", default=1.0, min=0.01, max=16.0)
    brightness_strength: FloatProperty(name="Vertex Brightness", default=0.35, min=0.0, max=1.0)
    shadow_strength: FloatProperty(name="Region Shadow (.t)", default=0.35, min=0.0, max=1.0)
    max_textures: IntProperty(name="Max Textures/Region", default=32, min=4, max=64)
    import_water: BoolProperty(name="Water", default=True)
    shade_smooth: BoolProperty(name="Shade Smooth", default=True)

    def execute(self, context):
        paths = [Path(self.directory) / f.name for f in self.files] if self.files else [Path(self.filepath)]
        paths = [p for p in paths if p.suffix.lower() == ".m"]
        if not paths:
            self.report({"ERROR"}, "Select at least one .m file")
            return {"CANCELLED"}
        regions = expand_regions(paths, self.neighbor_radius, ".m")
        do_import_terrain(regions, settings_dict(self), self.report)
        return {"FINISHED"}


class SRO_OT_import_objects(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.sro_objects_direct"
    bl_label = "Import Silkroad Objects (.o2/.o)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".o2"
    filter_glob: StringProperty(default="*.o2;*.o", options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})

    game_root: StringProperty(name="Game Root", subtype="DIR_PATH", default=DEFAULT_GAME_ROOT)
    neighbor_radius: IntProperty(name="Neighbor Radius", default=0, min=0, max=6)
    objects_collection: StringProperty(name="Collection", default="SRO_OBJECTS")
    scale: FloatProperty(name="Scale", default=0.01, min=0.000001)
    use_textures: BoolProperty(name="Textures", default=True)
    unique_mesh: BoolProperty(name="Unique Mesh per Instance", default=False)
    custom_normals: BoolProperty(name="Use File Normals", default=True)
    group_by_region: BoolProperty(name="Group by Region", default=True)
    group_by_resource: BoolProperty(name="Group by Model", default=False)
    skip_lod: BoolProperty(name="Skip LOD Groups", default=False)
    max_objects: IntProperty(name="Object Limit", default=20000, min=1, max=500000)
    yaw_direction: EnumProperty(
        name="Yaw",
        items=[("NORMAL", "Normal", ""), ("INVERTED", "Inverted", "")],
        default="NORMAL")

    def execute(self, context):
        paths = [Path(self.directory) / f.name for f in self.files] if self.files else [Path(self.filepath)]
        paths = [p for p in paths if p.suffix.lower() in (".o", ".o2")]
        if not paths:
            self.report({"ERROR"}, "Select at least one .o2 or .o file")
            return {"CANCELLED"}
        regions = expand_regions(paths, self.neighbor_radius, paths[0].suffix.lower())
        do_import_objects(regions, settings_dict(self), self.report)
        return {"FINISHED"}


# ===========================================================================
# Registration
# ===========================================================================

def menu_import(self, context):
    self.layout.operator(SRO_OT_import_terrain.bl_idname, text="Silkroad Terrain (.m)")
    self.layout.operator(SRO_OT_import_objects.bl_idname, text="Silkroad Objects (.o2/.o)")


CLASSES = (
    SRO_MapSettings,
    SRO_OT_reload_areas,
    SRO_OT_build_catalog,
    SRO_OT_panel_import_terrain,
    SRO_OT_panel_import_objects,
    SRO_OT_diagnose,
    SRO_OT_diagnose_objects,
    SRO_OT_clear_cache,
    SRO_PT_map,
    SRO_OT_import_terrain,
    SRO_OT_import_objects,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sro_map = bpy.props.PointerProperty(type=SRO_MapSettings)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    del bpy.types.Scene.sro_map
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
