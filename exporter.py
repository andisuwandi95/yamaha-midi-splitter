from __future__ import annotations

import mido
from pathlib import Path
from typing import Dict, List, Set

from patch_resolver import PSRVoiceDatabase, ResolvedVoice
from splitter import AssignedEvent, Options, SplitResult, VoiceKey


def sanitize_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, "_")

    name = "".join(ch for ch in name if ord(ch) >= 32)
    name = name.strip(". ")

    if len(name) > 150:
        name = name[:150]

    return name or "Unknown"


def _ascii_text(s: object) -> str:
    return "".join(ch if ord(ch) < 128 else "?" for ch in str(s))


def _base_name(
    resolved: ResolvedVoice,
    channel: int,
    preserve_channel_info: bool,
) -> str:
    suffix = f"_CH{channel + 1:02d}"

    if resolved.known:
        base = resolved.name
        if preserve_channel_info:
            base += suffix
    else:
        # Unknown fallback names already contain the channel suffix.
        base = resolved.name
        if preserve_channel_info and not base.endswith(suffix):
            base += suffix

    return sanitize_filename(base)


def _has_musical_content(events: List[AssignedEvent]) -> bool:
    """
    Avoid exporting tracks that contain only Bank Select / Program Change
    initialization data and no musical channel events.
    """
    for ae in events:
        msg = ae.msg
        msg_type = getattr(msg, "type", None)

        if msg_type in ("note_on", "note_off", "polytouch", "aftertouch", "pitchwheel"):
            return True

        if msg_type == "control_change":
            control = getattr(msg, "control", -1)
            if control not in (0, 32):
                return True

    return False


def _build_global_track(global_events, include_meta: bool = True) -> mido.MidiTrack:
    items = []
    has_tempo = False

    for ev in global_events:
        msg = ev.msg
        msg_type = getattr(msg, "type", None)

        if msg_type == "end_of_track":
            continue

        if not include_meta:
            # Even when global meta export is disabled, keep the minimum
            # timing skeleton required for sensible playback.
            if msg_type not in ("set_tempo", "time_signature", "key_signature"):
                continue

        if msg_type == "set_tempo":
            has_tempo = True

        items.append((ev.tick, ev.seq, msg))

    if not has_tempo:
        items.append(
            (
                0,
                -10,
                mido.MetaMessage("set_tempo", tempo=500000, time=0),
            )
        )

    items.sort(key=lambda x: (x[0], x[1]))

    track = mido.MidiTrack()
    last_tick = 0

    for tick, _seq, msg in items:
        delta = tick - last_tick
        if delta < 0:
            delta = 0

        track.append(msg.copy(time=delta))
        last_tick = tick

    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def _build_voice_track(
    events: List[AssignedEvent],
    voice: VoiceKey,
    channel: int,
    resolved: ResolvedVoice,
    track_name: str,
    first_tick: int,
) -> mido.MidiTrack:
    items = []

    # Track identity and original-channel metadata.
    items.append(
        (
            0,
            -120,
            mido.MetaMessage("track_name", text=_ascii_text(track_name), time=0),
        )
    )

    meta_lines = [
        f"Voice Name: {resolved.name}",
        f"Original Channel: {channel + 1}",
        f"MSB: {voice.msb}",
        f"LSB: {voice.lsb}",
        f"Program Change: {voice.pc}",
    ]

    if resolved.category:
        meta_lines.append(f"Category: {resolved.category}")

    for i, line in enumerate(meta_lines):
        items.append(
            (
                0,
                -110 + i,
                mido.MetaMessage("text", text=_ascii_text(line), time=0),
            )
        )

    # Inject the patch state at the first event tick for this voice.
    #
    # This guarantees that each exported logical voice track can be played
    # correctly, even if the original Bank Select messages were located in
    # another logical region or were ambiguous.
    #
    # Original events are still preserved; these injected messages are
    # deterministic setup messages.
    items.append(
        (
            first_tick,
            -20,
            mido.Message(
                "control_change",
                channel=channel,
                control=0,
                value=voice.msb & 0x7F,
                time=0,
            ),
        )
    )

    items.append(
        (
            first_tick,
            -19,
            mido.Message(
                "control_change",
                channel=channel,
                control=32,
                value=voice.lsb & 0x7F,
                time=0,
            ),
        )
    )

    items.append(
        (
            first_tick,
            -18,
            mido.Message(
                "program_change",
                channel=channel,
                program=voice.pc & 0x7F,
                time=0,
            ),
        )
    )

    for ae in events:
        items.append((ae.tick, ae.seq, ae.msg))

    items.sort(key=lambda x: (x[0], x[1]))

    track = mido.MidiTrack()
    last_tick = 0

    for tick, _seq, msg in items:
        delta = tick - last_tick
        if delta < 0:
            delta = 0

        track.append(msg.copy(time=delta))
        last_tick = tick

    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def export_result(
    result: SplitResult,
    db: PSRVoiceDatabase,
    out_dir: str,
    options: Options,
) -> List[str]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    exported: List[str] = []
    used_names: Set[str] = set()

    for ch in sorted(result.channels.keys()):
        ch_split = result.channels[ch]

        for voice, events in sorted(
            ch_split.voice_events.items(),
            key=lambda item: (item[0].msb, item[0].lsb, item[0].pc),
        ):
            if not events:
                continue

            if not _has_musical_content(events):
                continue

            resolved = db.resolve(voice.msb, voice.lsb, voice.pc, ch)

            if not resolved.known and not options.preserve_unknown:
                continue

            base = _base_name(
                resolved=resolved,
                channel=ch,
                preserve_channel_info=options.preserve_channel_info,
            )

            candidate = base

            # If two different patches resolve to the same display name on
            # the same channel, make the filename unambiguous.
            if candidate.lower() in used_names:
                candidate = (
                    f"{base}_MSB{voice.msb:02d}"
                    f"_LSB{voice.lsb:02d}"
                    f"_PC{voice.pc:02d}"
                )

            i = 2
            while candidate.lower() in used_names:
                candidate = f"{base}_{i}"
                i += 1

            used_names.add(candidate.lower())

            out_file = out_path / f"{candidate}.mid"

            first_tick = ch_split.first_event_tick.get(voice)
            if first_tick is None:
                first_tick = min(ae.tick for ae in events)

            voice_track = _build_voice_track(
                events=events,
                voice=voice,
                channel=ch,
                resolved=resolved,
                track_name=candidate,
                first_tick=first_tick,
            )

            global_track = _build_global_track(
                result.global_events,
                include_meta=options.export_global_meta,
            )

            mf = mido.MidiFile(type=1, ticks_per_beat=result.ppq)
            mf.tracks.append(global_track)
            mf.tracks.append(voice_track)
            mf.save(str(out_file))

            exported.append(str(out_file))

    return exported
