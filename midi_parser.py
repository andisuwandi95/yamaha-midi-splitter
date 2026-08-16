from __future__ import annotations

import mido
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class MidiEvent:
    tick: int
    seq: int
    msg: object
    track: int
    channel: int  # 0-15 for channel messages, -1 for global/meta/sysx


@dataclass
class ParsedMidi:
    path: str
    ppq: int
    file_type: int
    max_tick: int
    global_events: List[MidiEvent]
    channel_events: Dict[int, List[MidiEvent]]


def parse_midi_file(path: str) -> ParsedMidi:
    """
    Parse a MIDI file into absolute-tick events.

    Channel messages are separated by MIDI channel 0-15.
    Meta events and SysEx are kept as global events.

    Event ordering is preserved by the monotonically increasing seq value.
    """
    mf = mido.MidiFile(path)

    ppq = getattr(mf, "ticks_per_beat", None)
    try:
        ppq = int(ppq)
    except Exception:
        ppq = 480

    if ppq <= 0:
        ppq = 480

    global_events: List[MidiEvent] = []
    channel_events: Dict[int, List[MidiEvent]] = {ch: [] for ch in range(16)}

    seq = 0
    max_tick = 0

    for track_idx, track in enumerate(mf.tracks):
        tick = 0

        for msg in track:
            tick += int(msg.time)
            seq += 1

            if tick > max_tick:
                max_tick = tick

            if msg.is_meta:
                if msg.type != "end_of_track":
                    global_events.append(
                        MidiEvent(
                            tick=tick,
                            seq=seq,
                            msg=msg,
                            track=track_idx,
                            channel=-1,
                        )
                    )
            elif msg.type == "sysex":
                global_events.append(
                    MidiEvent(
                        tick=tick,
                        seq=seq,
                        msg=msg,
                        track=track_idx,
                        channel=-1,
                    )
                )
            else:
                ch = getattr(msg, "channel", None)
                if ch is None:
                    continue

                ch = int(ch) & 0x0F
                channel_events[ch].append(
                    MidiEvent(
                        tick=tick,
                        seq=seq,
                        msg=msg,
                        track=track_idx,
                        channel=ch,
                    )
                )

    global_events.sort(key=lambda e: (e.tick, e.seq))
    for ch in channel_events:
        channel_events[ch].sort(key=lambda e: (e.tick, e.seq))

    return ParsedMidi(
        path=path,
        ppq=ppq,
        file_type=mf.type,
        max_tick=max_tick,
        global_events=global_events,
        channel_events=channel_events,
    )
