"""Just enough of the Windows PE format to inspect a game's .exe.

Reads the machine type, whether it is a .NET assembly, the DLLs it imports,
its version strings, and its main icon. Nothing is executed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

RT_ICON = 3
RT_GROUP_ICON = 14
RT_VERSION = 16


class PEError(Exception):
    pass


@dataclass
class Section:
    va: int
    vsize: int
    raw: int
    rsize: int


@dataclass
class PEInfo:
    is_64bit: bool = False
    is_dotnet: bool = False
    imports: list[str] = field(default_factory=list)
    version: dict[str, str] = field(default_factory=dict)


class PEFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        d = self.data
        if d[:2] != b"MZ":
            raise PEError("not a Windows executable")
        (pe_off,) = struct.unpack_from("<I", d, 0x3C)
        if d[pe_off:pe_off + 4] != b"PE\0\0":
            raise PEError("missing PE header")
        coff = pe_off + 4
        self.machine, nsect = struct.unpack_from("<HH", d, coff)
        (opt_size,) = struct.unpack_from("<H", d, coff + 16)
        opt = coff + 20
        (magic,) = struct.unpack_from("<H", d, opt)
        self.is_64bit = magic == 0x20B
        dd = opt + (112 if self.is_64bit else 96)
        (ndirs,) = struct.unpack_from("<I", d, dd - 4)
        self.dirs = [struct.unpack_from("<II", d, dd + 8 * i) for i in range(min(ndirs, 16))]
        self.sections = []
        s = opt + opt_size
        for i in range(nsect):
            vsize, va, rsize, raw = struct.unpack_from("<IIII", d, s + 40 * i + 8)
            self.sections.append(Section(va, vsize, raw, rsize))

    def _dir(self, index: int) -> tuple[int, int]:
        return self.dirs[index] if index < len(self.dirs) else (0, 0)

    def off(self, rva: int) -> int:
        for s in self.sections:
            if s.va <= rva < s.va + max(s.vsize, s.rsize):
                return rva - s.va + s.raw
        raise PEError(f"RVA {rva:#x} outside every section")

    def cstr(self, off: int) -> str:
        end = self.data.index(b"\0", off)
        return self.data[off:end].decode("latin-1")

    # ------------------------------------------------------------------ imports

    def imports(self) -> list[str]:
        rva, size = self._dir(1)
        if not rva:
            return []
        out = []
        try:
            pos = self.off(rva)
            while True:
                fields = struct.unpack_from("<IIIII", self.data, pos)
                if not any(fields):
                    break
                out.append(self.cstr(self.off(fields[3])))
                pos += 20
        except (PEError, struct.error, ValueError):
            pass
        return out

    # ---------------------------------------------------------------- resources

    def _res_root(self) -> int | None:
        rva, _ = self._dir(2)
        if not rva:
            return None
        try:
            return self.off(rva)
        except PEError:
            return None

    def _res_entries(self, base: int, at: int):
        """Yield (id_or_name, is_dir, offset) for one resource directory."""
        named, ids = struct.unpack_from("<HH", self.data, at + 12)
        for i in range(named + ids):
            name, target = struct.unpack_from("<II", self.data, at + 16 + 8 * i)
            key: int | str
            if name & 0x80000000:
                n = base + (name & 0x7FFFFFFF)
                (length,) = struct.unpack_from("<H", self.data, n)
                key = self.data[n + 2:n + 2 + 2 * length].decode("utf-16-le", "replace")
            else:
                key = name
            yield key, bool(target & 0x80000000), base + (target & 0x7FFFFFFF)

    def _res_leaf(self, base: int, at: int) -> bytes | None:
        """Follow the first entry down to the data."""
        for _ in range(4):
            entries = list(self._res_entries(base, at))
            if not entries:
                return None
            _, is_dir, nxt = entries[0]
            if not is_dir:
                rva, size = struct.unpack_from("<II", self.data, nxt)
                o = self.off(rva)
                return self.data[o:o + size]
            at = nxt
        return None

    def resources(self, rtype: int) -> dict:
        """{name_or_id: bytes} for one resource type (first language only)."""
        base = self._res_root()
        if base is None:
            return {}
        out = {}
        try:
            for key, is_dir, at in self._res_entries(base, base):
                if key != rtype or not is_dir:
                    continue
                for name, sub_dir, sub in self._res_entries(base, at):
                    blob = self._res_leaf(base, sub) if sub_dir else None
                    if blob is not None:
                        out[name] = blob
        except (PEError, struct.error, ValueError):
            pass
        return out

    def icon_ico(self) -> bytes | None:
        """The main icon as a single-image .ico file (largest size), or None."""
        groups = self.resources(RT_GROUP_ICON)
        if not groups:
            return None
        icons = self.resources(RT_ICON)
        group = groups[sorted(groups, key=lambda k: (isinstance(k, str), k))[0]]
        _, _, count = struct.unpack_from("<HHH", group, 0)
        best = None
        for i in range(count):
            w, h, colors, _, planes, bits, size, nid = struct.unpack_from("<BBBBHHIH", group, 6 + 14 * i)
            data = icons.get(nid)
            if not data:
                continue
            score = ((w or 256) * (h or 256), bits)
            if best is None or score > best[0]:
                best = (score, w, h, colors, planes, bits, data)
        if best is None:
            return None
        _, w, h, colors, planes, bits, data = best
        header = struct.pack("<HHH", 0, 1, 1)
        entry = struct.pack("<BBBBHHII", w, h, colors, 0, planes, bits, len(data), 6 + 16)
        return header + entry + data

    def version_strings(self) -> dict[str, str]:
        """ProductName, CompanyName, FileDescription... from VS_VERSION_INFO."""
        blobs = self.resources(RT_VERSION)
        if not blobs:
            return {}
        blob = next(iter(blobs.values()))
        out = {}
        # The string table is UTF-16 "Key\0...Value\0"; scan for the keys we want.
        text = blob.decode("utf-16-le", "replace")
        for key in ("ProductName", "CompanyName", "FileDescription", "ProductVersion"):
            i = text.find(key + "\0")
            if i < 0:
                continue
            j = i + len(key) + 1
            while j < len(text) and text[j] == "\0":
                j += 1
            end = text.find("\0", j)
            value = text[j:end].strip()
            if value:
                out[key] = value
        return out

    def info(self) -> PEInfo:
        clr_rva, clr_size = self._dir(14)
        return PEInfo(
            is_64bit=self.is_64bit,
            is_dotnet=bool(clr_rva and clr_size),
            imports=self.imports(),
            version=self.version_strings(),
        )
