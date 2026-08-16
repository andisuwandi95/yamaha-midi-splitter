from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Set

from midi_parser import MidiEvent, ParsedMidi
from patch_resolver import VoiceKey


@dataclass
class Options:
    scan_all_channels: bool = True
    identify_bank_program: bool = True
    split_on_voice_changes: bool = True
    use_yamaha_names: bool = True
    preserve_channel_info: bool = True
    preserve_unknown: bool = True
    export_global_meta: bool = True
    group_same_voice: bool = True


@dataclass
class AssignedEvent:
    tick: int
    seq: int
    msg: object
    channel: int
    voice: VoiceKey


@dataclass
class VoiceRegion:
    channel: int
    voice: VoiceKey
    start_tick: int
    end_tick: int  # exclusive


@dataclass
class ChannelSplit:
    channel: int
    initial_voice: VoiceKey
    regions: List[VoiceRegion] = field(default_factory=list)
    voice_events: Dict[VoiceKey, List[AssignedEvent]] = field(default_factory=dict)
    first_event_tick: Dict[VoiceKey, int] = field(default_factory=dict)
    event_count: Dict[VoiceKey, int] = field(default_factory=dict)
    note_count: Dict[VoiceKey, int] = field(default_factory=dict)


@dataclass
class SplitResult:
    path: str
    ppq: int
    file_type: int
    max_tick: int
    global_events: List[MidiEvent]
    time_sig_events: List[MidiEvent]
    channels: Dict[int, ChannelSplit]


def _is_patch_event(msg: object) -> bool:
    msg_type = getattr(msg, "type", None)

    if msg_type == "program_change":
        return True

    if msg_type == "control_change":
        control = getattr(msg, "control", -1)
        if control in (0, 32):
            return True

    return False


def _initial_state_for_channel(events: List[MidiEvent]):
    """
    Establish the effective initial patch state for a channel.

    Yamaha style MIDI files often contain multiple initialization events
    at or near tick 0. These must not become separate musical tracks.

    Policy:
    - If the channel has at least one Note On with velocity > 0, all
      Bank Select / Program Change events up to and including the tick
      of the first real Note On are treated as initialization.
    - If the channel has no Note On events, only tick 0 patch events
      are treated as initialization.

    This avoids creating meaningless tracks from:
        PC 16
        PC 18
        PC 16
        PC 72
    at initialization time.
    """
    msb = 0
    lsb = 0
    pc = 0

    first_note_tick = None
    for ev in events:
        msg = ev.msg
        if getattr(msg, "type", None) == "note_on" and getattr(msg, "velocity", 0) > 0:
            first_note_tick = ev.tick
            break

    init_seqs: Set[int] = set()

    for ev in events:
        if not _is_patch_event(ev.msg):
            continue

        if first_note_tick is None:
            if ev.tick != 0:
                continue
        else:
            if ev.tick > first_note_tick:
                continue

        msg = ev.msg

        if getattr(msg, "type", None) == "control_change":
            control = getattr(msg, "control", -1)
            value = int(getattr(msg, "value", 0)) & 0x7F

            if control == 0:
                msb = value
            elif control == 32:
                lsb = value

        elif getattr(msg, "type", None) == "program_change":
            pc = int(getattr(msg, "program", 0)) & 0x7F

        init_seqs.add(ev.seq)

    return VoiceKey(msb, lsb, pc), init_seqs, first_note_tick


def split_parsed(parsed: ParsedMidi, options: Options) -> SplitResult:
    """
    Scan all 16 MIDI channels independently.

    Core rule:
        CHANNEL != INSTRUMENT

    Voice identity is determined only by:
        Bank Select MSB
        Bank Select LSB
        Program Change
    """
    channels: Dict[int, ChannelSplit] = {}

    time_sig_events = [
        ev
        for ev in parsed.global_events
        if getattr(ev.msg, "type", None) == "time_signature"
    ]

    for ch in range(16):
        events = parsed.channel_events.get(ch, [])
        if not events:
            continue

        initial_voice, init_seqs, _first_note_tick = _initial_state_for_channel(events)

        current_voice = initial_voice

        bank_msb = initial_voice.msb
        bank_lsb = initial_voice.lsb
        bank_pc = initial_voice.pc

        pending_bank_events: List[MidiEvent] = []

        regions: List[VoiceRegion] = []
        region_voice = current_voice
        region_start = events[0].tick

        voice_events: Dict[VoiceKey, List[AssignedEvent]] = defaultdict(list)
        first_event_tick: Dict[VoiceKey, int] = {}
        event_count: Dict[VoiceKey, int] = defaultdict(int)
        note_count: Dict[VoiceKey, int] = defaultdict(int)

        # For deterministic handling of crossing notes.
        # Key: MIDI note number, value: FIFO queue of voices that started notes.
        active_note_queues: Dict[int, deque] = defaultdict(deque)

        def assign(ev: MidiEvent, voice: VoiceKey) -> None:
            voice_events[voice].append(
                AssignedEvent(
                    tick=ev.tick,
                    seq=ev.seq,
                    msg=ev.msg,
                    channel=ch,
                    voice=voice,
                )
            )
            event_count[voice] += 1

            old = first_event_tick.get(voice)
            if old is None or ev.tick < old:
                first_event_tick[voice] = ev.tick

        for ev in events:
            msg = ev.msg
            msg_type = getattr(msg, "type", None)

            # Initialization patch events are assigned to the final initial
            # voice and do not create separate musical regions.
            if ev.seq in init_seqs:
                assign(ev, initial_voice)
                continue

            # Bank Select MSB / LSB.
            #
            # Bank messages do not immediately change the effective voice.
            # They are latched until Program Change.
            if msg_type == "control_change" and getattr(msg, "control", -1) in (0, 32):
                control = getattr(msg, "control", -1)
                value = int(getattr(msg, "value", 0)) & 0x7F

                if control == 0:
                    bank_msb = value
                elif control == 32:
                    bank_lsb = value

                pending_bank_events.append(ev)
                continue

            # Program Change.
            #
            # This is where the effective Yamaha voice identity changes.
            if msg_type == "program_change":
                bank_pc = int(getattr(msg, "program", 0)) & 0x7F
                new_voice = VoiceKey(bank_msb, bank_lsb, bank_pc)

                if new_voice != current_voice:
                    regions.append(
                        VoiceRegion(
                            channel=ch,
                            voice=region_voice,
                            start_tick=region_start,
                            end_tick=ev.tick,
                        )
                    )

                    region_voice = new_voice
                    region_start = ev.tick
                    current_voice = new_voice

                # Bank Select messages immediately before this Program Change
                # belong to the newly selected voice.
                for pending_ev in pending_bank_events:
                    assign(pending_ev, current_voice)
                pending_bank_events.clear()

                assign(ev, current_voice)
                continue

            # Any other channel event.
            #
            # Flush pending Bank Select messages to the currently effective
            # voice before assigning the musical event.
            if pending_bank_events:
                for pending_ev in pending_bank_events:
                    assign(pending_ev, current_voice)
                pending_bank_events.clear()

            if msg_type == "note_on" and getattr(msg, "velocity", 0) > 0:
                note = int(getattr(msg, "note", 0)) & 0x7F
                active_note_queues[note].append(current_voice)
                assign(ev, current_voice)
                note_count[current_voice] += 1

            elif msg_type == "note_off" or (
                msg_type == "note_on" and getattr(msg, "velocity", 0) == 0
            ):
                note = int(getattr(msg, "note", 0)) & 0x7F
                q = active_note_queues.get(note)

                if q:
                    voice = q.popleft()
                else:
                    # Orphan Note Off.
                    # Preserve it deterministically in the current voice.
                    voice = current_voice

                assign(ev, voice)

            else:
                assign(ev, current_voice)

        # Flush remaining pending Bank Select messages.
        if pending_bank_events:
            for pending_ev in pending_bank_events:
                assign(pending_ev, current_voice)
            pending_bank_events.clear()

        # Close final region.
        regions.append(
            VoiceRegion(
                channel=ch,
                voice=region_voice,
                start_tick=region_start,
                end_tick=parsed.max_tick + 1,
            )
        )

        channels[ch] = ChannelSplit(
            channel=ch,
            initial_voice=initial_voice,
            regions=regions,
            voice_events=dict(voice_events),
            first_event_tick=dict(first_event_tick),
            event_count=dict(event_count),
            note_count=dict(note_count),
        )

    return SplitResult(
        path=parsed.path,
        ppq=parsed.ppq,
        file_type=parsed.file_type,
        max_tick=parsed.max_tick,
        global_events=parsed.global_events,
        time_sig_events=time_sig_events,
        channels=channels,
    )
