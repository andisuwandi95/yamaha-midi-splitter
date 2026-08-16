from __future__ import annotations

import csv
import json
import os
from typing import Dict, List

from patch_resolver import PSRVoiceDatabase
from splitter import SplitResult


CSV_FIELDS = [
    "input_file",
    "channel",
    "region_start_tick",
    "region_end_tick",
    "start_bar_beat",
    "end_bar_beat",
    "msb",
    "lsb",
    "pc",
    "voice_name",
    "known",
    "category",
    "original_channel",
    "voice_key",
    "event_count_total",
    "note_count_total",
]


class TimeGrid:
    """
    Approximate bar:beat display grid.

    This is for analysis only. It does not modify MIDI timing.
    """

    def __init__(self, ppq: int, time_sig_events):
        self.ppq = int(ppq)
        self.sigs = []

        for ev in time_sig_events:
            msg = ev.msg
            numerator = int(getattr(msg, "numerator", 4))
            denominator = int(getattr(msg, "denominator", 4))

            if denominator <= 0:
                denominator = 4

            self.sigs.append((ev.tick, numerator, denominator))

        self.sigs.sort(key=lambda x: x[0])

    def bar_beat(self, tick: int) -> str:
        tick = max(0, int(tick))

        bar = 0
        current_tick = 0
        numerator = 4
        denominator = 4

        sigs = self.sigs + [(10**18, 4, 4)]

        for sig_tick, num, den in sigs:
            if tick < sig_tick:
                ticks_per_bar = max(
                    1,
                    int(round(self.ppq * 4 * numerator / denominator)),
                )

                delta = tick - current_tick
                bar += delta // ticks_per_bar
                beat = (delta % ticks_per_bar) // max(1, self.ppq)

                return f"{bar}:{beat:02d}"

            if sig_tick > current_tick:
                ticks_per_bar = max(
                    1,
                    int(round(self.ppq * 4 * numerator / denominator)),
                )
                delta = sig_tick - current_tick
                bar += delta // ticks_per_bar
                current_tick = sig_tick

            numerator = num
            denominator = den

        return f"{bar}:00"


def build_report_rows(
    result: SplitResult,
    db: PSRVoiceDatabase,
    input_path: str,
) -> List[Dict]:
    rows = []
    grid = TimeGrid(result.ppq, result.time_sig_events)

    for ch in sorted(result.channels.keys()):
        ch_split = result.channels[ch]
        channel_display = ch + 1

        for region in ch_split.regions:
            v = region.voice
            resolved = db.resolve(v.msb, v.lsb, v.pc, ch)

            end_tick = region.end_tick - 1
            if end_tick < region.start_tick:
                end_tick = region.start_tick

            rows.append(
                {
                    "input_file": input_path,
                    "channel": channel_display,
                    "region_start_tick": region.start_tick,
                    "region_end_tick": end_tick,
                    "start_bar_beat": grid.bar_beat(region.start_tick),
                    "end_bar_beat": grid.bar_beat(end_tick),
                    "msb": v.msb,
                    "lsb": v.lsb,
                    "pc": v.pc,
                    "voice_name": resolved.name,
                    "known": resolved.known,
                    "category": resolved.category,
                    "original_channel": channel_display,
                    "voice_key": f"MSB{v.msb:02d}_LSB{v.lsb:02d}_PC{v.pc:02d}",
                    "event_count_total": ch_split.event_count.get(v, 0),
                    "note_count_total": ch_split.note_count.get(v, 0),
                }
            )

    return rows


def result_to_analysis_text(
    file_path: str,
    result: SplitResult,
    db: PSRVoiceDatabase,
) -> str:
    lines = []
    lines.append(f"Style: {os.path.basename(file_path)}")
    lines.append("")

    grid = TimeGrid(result.ppq, result.time_sig_events)

    for ch in sorted(result.channels.keys()):
        ch_split = result.channels[ch]
        lines.append(f"CH{ch + 1:02d}")

        for region in ch_split.regions:
            v = region.voice
            resolved = db.resolve(v.msb, v.lsb, v.pc, ch)
            bb = grid.bar_beat(region.start_tick)

            lines.append(
                f"    {bb}  MSB={v.msb} LSB={v.lsb} PC={v.pc}  "
                f"(tick {region.start_tick})"
            )
            lines.append(f"           {resolved.name}")

        lines.append("")

    return "\n".join(lines)


def write_csv_report(path: str, rows: List[Dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json_report(path: str, rows: List[Dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False, default=str)


def write_txt_report(path: str, rows: List[Dict]) -> None:
    grouped = {}
    order = []

    for row in rows:
        f = row["input_file"]
        if f not in grouped:
            grouped[f] = {}
            order.append(f)

        ch = row["channel"]
        if ch not in grouped[f]:
            grouped[f][ch] = []

        grouped[f][ch].append(row)

    with open(path, "w", encoding="utf-8") as fh:
        for f in order:
            fh.write(f"Style: {os.path.basename(f)}\n\n")

            for ch in sorted(grouped[f].keys()):
                fh.write(f"CH{ch:02d}\n")

                region_rows = sorted(
                    grouped[f][ch],
                    key=lambda r: r["region_start_tick"],
                )

                for row in region_rows:
                    fh.write(
                        f"    {row['start_bar_beat']}  "
                        f"MSB={row['msb']} LSB={row['lsb']} PC={row['pc']}  "
                        f"(tick {row['region_start_tick']})\n"
                    )
                    fh.write(f"           {row['voice_name']}\n")

                fh.write("\n")
