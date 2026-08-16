from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class VoiceKey:
    msb: int
    lsb: int
    pc: int

    def as_tuple(self) -> Tuple[int, int, int]:
        return self.msb, self.lsb, self.pc


@dataclass
class ResolvedVoice:
    msb: int
    lsb: int
    pc: int
    name: str
    category: str
    known: bool

    @property
    def key(self) -> VoiceKey:
        return VoiceKey(self.msb, self.lsb, self.pc)


class PSRVoiceDatabase:
    """
    PSR-S750 voice resolver.

    Important design rules:
    - MSB 62 and MSB 63 are never treated as normal PSR-S750 voices.
    - Unknown voices are preserved and named Unknown_MSBxx_LSBxx_PCxx_CHxx.
    - No General MIDI fallback naming is used.
    """

    EXCLUDED_MSB = {62, 63}

    def __init__(self, path: Optional[str] = None):
        self.entries: Dict[Tuple[int, int, int], ResolvedVoice] = {}
        self.source: str = ""
        self.instrument: str = "Yamaha PSR-S750"
        self.loaded_path: Optional[str] = None

        if path:
            self.load(path)

    def load(self, path: str) -> None:
        path = os.path.abspath(path)
        self.loaded_path = path

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.source = str(data.get("source", ""))
        self.instrument = str(data.get("instrument", "Yamaha PSR-S750"))
        self.entries.clear()

        for item in data.get("voices", []):
            try:
                msb = int(item.get("msb", item.get("bank_msb", -1)))
                lsb = int(item.get("lsb", item.get("bank_lsb", -1)))
                pc = int(
                    item.get(
                        "pc",
                        item.get("program", item.get("program_change", -1)),
                    )
                )
                name = str(item.get("name", "")).strip()
                category = str(item.get("category", item.get("type", ""))).strip()
            except Exception:
                continue

            if msb < 0 or lsb < 0 or pc < 0:
                continue
            if msb > 127 or lsb > 127 or pc > 127:
                continue
            if not name:
                continue

            # Expansion/sampling banks are intentionally not part of the
            # normal PSR-S750 voice-name database.
            if msb in self.EXCLUDED_MSB:
                continue

            key = (msb, lsb, pc)
            if key not in self.entries:
                self.entries[key] = ResolvedVoice(
                    msb=msb,
                    lsb=lsb,
                    pc=pc,
                    name=name,
                    category=category,
                    known=True,
                )

    def resolve(self, msb: int, lsb: int, pc: int, channel: int) -> ResolvedVoice:
        """
        Resolve a Yamaha voice identity.

        channel is zero-based internally: 0-15.
        Display/channel suffix uses 1-16.
        """
        msb = int(msb) & 0x7F
        lsb = int(lsb) & 0x7F
        pc = int(pc) & 0x7F
        ch = int(channel) & 0x0F

        if msb not in self.EXCLUDED_MSB:
            entry = self.entries.get((msb, lsb, pc))
            if entry is not None:
                return entry

        name = f"Unknown_MSB{msb:02d}_LSB{lsb:02d}_PC{pc:02d}_CH{ch + 1:02d}"
        return ResolvedVoice(
            msb=msb,
            lsb=lsb,
            pc=pc,
            name=name,
            category="Unknown",
            known=False,
        )
