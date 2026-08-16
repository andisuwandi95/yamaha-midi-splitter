from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import tkinterdnd2
    from tkinterdnd2 import TkinterDnD, DND_FILES

    DND_AVAILABLE = True
    DND_DIRECTORIES = getattr(tkinterdnd2, "DND_DIRECTORIES", None)
except Exception:
    DND_AVAILABLE = False
    DND_FILES = None
    DND_DIRECTORIES = None

from analyzer import (
    build_report_rows,
    result_to_analysis_text,
    write_csv_report,
    write_json_report,
    write_txt_report,
)
from exporter import export_result
from midi_parser import parse_midi_file
from patch_resolver import PSRVoiceDatabase
from splitter import Options, split_parsed


def resource_path(*relative: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    return os.path.join(base, *relative)


def _sanitize_path_part(name: str) -> str:
    name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
    name = name.strip(". ")
    return name or "input"


def collect_midi_files(paths) -> list:
    seen = set()
    out = []

    for p in paths:
        p = os.path.abspath(p)

        if os.path.isfile(p):
            if p.lower().endswith((".mid", ".midi")) and p not in seen:
                seen.add(p)
                out.append(p)

        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    if f.lower().endswith((".mid", ".midi")):
                        fp = os.path.join(root, f)
                        if fp not in seen:
                            seen.add(fp)
                            out.append(fp)

    return out


def make_unique_output_dir(out_root: str, midi_path: str, used: set) -> Path:
    out_root = Path(out_root)
    stem = Path(midi_path).stem
    base = _sanitize_path_part(stem)

    candidate = out_root / base
    key = str(candidate).lower()

    if key not in used:
        used.add(key)
        return candidate

    parent = _sanitize_path_part(Path(midi_path).parent.name)
    candidate = out_root / f"{base}_{parent}"
    key = str(candidate).lower()

    if key not in used:
        used.add(key)
        return candidate

    i = 2
    while True:
        candidate = out_root / f"{base}_{i}"
        key = str(candidate).lower()
        if key not in used:
            used.add(key)
            return candidate
        i += 1


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Yamaha PSR-S750 MIDI Voice Splitter")
        self.root.geometry("1000x740")
        self.root.minsize(860, 640)

        self.queue = queue.Queue()
        self.path_set = set()
        self.report_rows = []
        self.used_output_dirs = set()
        self.running = False

        self.db = self._load_db()

        self._build_ui()
        self._setup_dnd()

        if not self.db.entries:
            self._append_log(
                "WARNING: data/psr_s750_voices.json contains no voices.\n"
                "Populate it from the official Yamaha PSR-S750 Data List.\n"
                "Until then, all patches will be preserved as Unknown_*."
            )

        self.root.after(100, self._poll_queue)

    def _load_db(self) -> PSRVoiceDatabase:
        db_path = resource_path("data", "psr_s750_voices.json")
        db = PSRVoiceDatabase()

        if os.path.exists(db_path):
            try:
                db.load(db_path)
            except Exception as e:
                messagebox.showwarning(
                    "Voice database error",
                    f"Could not load voice database:\n{db_path}\n\n{e}",
                )
        else:
            messagebox.showwarning(
                "Voice database missing",
                "data/psr_s750_voices.json was not found.\n\n"
                "The application will continue, but all voices will be named Unknown_*.",
            )

        return db

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)

        self.drop_label = ttk.Label(
            main,
            text="Drop MIDI files or folders here",
            anchor="center",
            font=("Segoe UI", 12, "bold"),
        )
        self.drop_label.pack(fill="x", padx=10, pady=8)

        input_frame = ttk.LabelFrame(main, text="Input")
        input_frame.pack(fill="both", expand=True, padx=10, pady=4)

        self.listbox = tk.Listbox(input_frame, selectmode=tk.EXTENDED)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)

        sb = ttk.Scrollbar(input_frame, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y", pady=8, padx=(0, 8))
        self.listbox.config(yscrollcommand=sb.set)

        btns = ttk.Frame(input_frame)
        btns.pack(side="left", fill="y", padx=(0, 8), pady=8)

        ttk.Button(btns, text="Add Files...", command=self._add_files).pack(
            fill="x", pady=2
        )
        ttk.Button(btns, text="Add Folder...", command=self._add_folder).pack(
            fill="x", pady=2
        )
        ttk.Button(btns, text="Remove Selected", command=self._remove_selected).pack(
            fill="x", pady=2
        )
        ttk.Button(btns, text="Clear", command=self._clear_inputs).pack(
            fill="x", pady=2
        )

        output_frame = ttk.LabelFrame(main, text="Output")
        output_frame.pack(fill="x", padx=10, pady=4)

        self.output_var = tk.StringVar(value=os.path.abspath("output"))

        ttk.Entry(output_frame, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True, padx=8, pady=8
        )
        ttk.Button(output_frame, text="Browse...", command=self._browse_output).pack(
            side="left", padx=(0, 8), pady=8
        )

        options_frame = ttk.LabelFrame(main, text="Options")
        options_frame.pack(fill="x", padx=10, pady=4)

        self.opt_scan = tk.BooleanVar(value=True)
        self.opt_identify = tk.BooleanVar(value=True)
        self.opt_split = tk.BooleanVar(value=True)
        self.opt_names = tk.BooleanVar(value=True)
        self.opt_channel = tk.BooleanVar(value=True)
        self.opt_unknown = tk.BooleanVar(value=True)
        self.dry_var = tk.BooleanVar(value=False)
        self.verbose_var = tk.BooleanVar(value=False)

        grid = ttk.Frame(options_frame)
        grid.pack(fill="x", padx=8, pady=6)

        ttk.Checkbutton(
            grid,
            text="Scan all 16 MIDI channels",
            variable=self.opt_scan,
        ).grid(row=0, column=0, sticky="w", padx=6, pady=2)

        ttk.Checkbutton(
            grid,
            text="Identify Bank MSB / LSB / Program Change",
            variable=self.opt_identify,
        ).grid(row=0, column=1, sticky="w", padx=6, pady=2)

        ttk.Checkbutton(
            grid,
            text="Split on effective Voice changes",
            variable=self.opt_split,
        ).grid(row=1, column=0, sticky="w", padx=6, pady=2)

        ttk.Checkbutton(
            grid,
            text="Name tracks using Yamaha Voice names",
            variable=self.opt_names,
        ).grid(row=1, column=1, sticky="w", padx=6, pady=2)

        ttk.Checkbutton(
            grid,
            text="Preserve original channel information",
            variable=self.opt_channel,
        ).grid(row=2, column=0, sticky="w", padx=6, pady=2)

        ttk.Checkbutton(
            grid,
            text="Preserve unknown patches",
            variable=self.opt_unknown,
        ).grid(row=2, column=1, sticky="w", padx=6, pady=2)

        ttk.Checkbutton(
            grid,
            text="Dry run / analysis only",
            variable=self.dry_var,
        ).grid(row=3, column=0, sticky="w", padx=6, pady=2)

        ttk.Checkbutton(
            grid,
            text="Verbose channel progress",
            variable=self.verbose_var,
        ).grid(row=3, column=1, sticky="w", padx=6, pady=2)

        action_frame = ttk.Frame(main)
        action_frame.pack(fill="x", padx=10, pady=4)

        self.process_btn = ttk.Button(
            action_frame,
            text="Process",
            command=self._process,
        )
        self.process_btn.pack(side="left", padx=8)

        ttk.Button(
            action_frame,
            text="Export Analysis CSV...",
            command=self._export_csv,
        ).pack(side="left", padx=4)

        ttk.Button(
            action_frame,
            text="Export Analysis TXT...",
            command=self._export_txt,
        ).pack(side="left", padx=4)

        ttk.Button(
            action_frame,
            text="Export Analysis JSON...",
            command=self._export_json,
        ).pack(side="left", padx=4)

        progress_frame = ttk.Frame(main)
        progress_frame.pack(fill="x", padx=10, pady=4)

        self.progress_label = ttk.Label(progress_frame, text="Idle")
        self.progress_label.pack(side="left")

        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=8)

        log_tab = ttk.Frame(self.notebook)
        analysis_tab = ttk.Frame(self.notebook)

        self.notebook.add(log_tab, text="Log")
        self.notebook.add(analysis_tab, text="Analysis")

        self.log_text = scrolledtext.ScrolledText(log_tab, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

        self.analysis_text = scrolledtext.ScrolledText(
            analysis_tab,
            wrap="none",
            state="disabled",
        )
        self.analysis_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _setup_dnd(self):
        if not DND_AVAILABLE:
            self.drop_label.config(
                text="Drag-and-drop not available. Use Add Files / Add Folder."
            )
            return

        try:
            types = []
            if DND_FILES:
                types.append(DND_FILES)
            if DND_DIRECTORIES:
                types.append(DND_DIRECTORIES)

            if not types:
                return

            try:
                self.root.drop_target_register(*types)
            except TypeError:
                self.root.drop_target_register(types[0])

            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as e:
            self.drop_label.config(
                text=f"Drag-and-drop initialization failed: {e}"
            )

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = str(event.data).split()

        for p in paths:
            self._add_path(str(p).strip("{}"))

    def _add_path(self, p):
        p = os.path.abspath(p)

        if not os.path.exists(p):
            return

        if p in self.path_set:
            return

        if os.path.isfile(p) and not p.lower().endswith((".mid", ".midi")):
            return

        self.path_set.add(p)
        self.listbox.insert(tk.END, p)

    def _add_files(self):
        files = filedialog.askopenfilenames(
            title="Add MIDI files",
            filetypes=[("MIDI files", "*.mid *.midi"), ("All files", "*.*")],
        )
        for f in files:
            self._add_path(f)

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Add folder")
        if folder:
            self._add_path(folder)

    def _remove_selected(self):
        selected = list(self.listbox.curselection())
        selected.reverse()

        for idx in selected:
            item = self.listbox.get(idx)
            self.path_set.discard(item)
            self.listbox.delete(idx)

    def _clear_inputs(self):
        self.listbox.delete(0, tk.END)
        self.path_set.clear()

    def _browse_output(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_var.set(os.path.abspath(folder))

    def _append_log(self, text: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _append_analysis(self, text: str):
        self.analysis_text.configure(state=tk.NORMAL)
        self.analysis_text.insert(tk.END, text + "\n\n")
        self.analysis_text.see(tk.END)
        self.analysis_text.configure(state=tk.DISABLED)

    def _clear_texts(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

        self.analysis_text.configure(state=tk.NORMAL)
        self.analysis_text.delete("1.0", tk.END)
        self.analysis_text.configure(state=tk.DISABLED)

    def _process(self):
        if self.running:
            return

        paths = list(self.listbox.get(0, tk.END))
        if not paths:
            messagebox.showinfo("No input", "Add MIDI files or folders first.")
            return

        output_root = self.output_var.get().strip() or "output"
        output_root = os.path.abspath(output_root)

        opts = Options(
            scan_all_channels=self.opt_scan.get(),
            identify_bank_program=self.opt_identify.get(),
            split_on_voice_changes=self.opt_split.get(),
            use_yamaha_names=self.opt_names.get(),
            preserve_channel_info=self.opt_channel.get(),
            preserve_unknown=self.opt_unknown.get(),
            export_global_meta=True,
            group_same_voice=True,
        )

        files = collect_midi_files(paths)
        if not files:
            messagebox.showinfo("No MIDI files", "No .mid or .midi files were found.")
            return

        self.running = True
        self.process_btn.config(state="disabled")
        self.report_rows = []
        self.used_output_dirs = set()

        self._clear_texts()
        self.progress["value"] = 0
        self.progress["maximum"] = len(files)

        dry = self.dry_var.get()
        verbose = self.verbose_var.get()

        threading.Thread(
            target=self._worker,
            args=(files, output_root, opts, dry, verbose),
            daemon=True,
        ).start()

    def _worker(self, files, output_root, opts, dry, verbose):
        success = 0
        errors = 0
        total = len(files)

        for idx, path in enumerate(files, 1):
            name = os.path.basename(path)
            self.queue.put(("progress", idx, total, name))
            self.queue.put(("log", f"\n=== [{idx}/{total}] {name} ==="))

            try:
                parsed = parse_midi_file(path)
                result = split_parsed(parsed, opts)

                rows = build_report_rows(result, self.db, path)
                self.queue.put(("rows", rows))

                if verbose:
                    for ch in range(16):
                        mark = "✓" if ch in result.channels else "-"
                        self.queue.put(("log", f"CH{ch + 1:02d} {mark}"))
                else:
                    marks = []
                    for ch in range(16):
                        mark = "✓" if ch in result.channels else "-"
                        marks.append(f"CH{ch + 1:02d} {mark}")
                    self.queue.put(("log", " ".join(marks)))

                if dry:
                    text = result_to_analysis_text(path, result, self.db)
                    self.queue.put(("analysis", text))
                    self.queue.put(("log", "Dry run complete. No files exported."))
                else:
                    out_dir = make_unique_output_dir(
                        output_root,
                        path,
                        self.used_output_dirs,
                    )
                    exported = export_result(result, self.db, out_dir, opts)
                    self.queue.put(
                        ("log", f"Exported {len(exported)} track file(s) -> {out_dir}")
                    )

                success += 1

            except Exception as e:
                errors += 1
                self.queue.put(("log", f"ERROR processing {path}: {e}"))
                self.queue.put(("analysis", f"ERROR processing {path}\n{traceback.format_exc()}"))

        self.queue.put(("done", success, errors))

    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]

                if kind == "log":
                    self._append_log(msg[1])

                elif kind == "analysis":
                    self._append_analysis(msg[1])

                elif kind == "progress":
                    idx, total, name = msg[1], msg[2], msg[3]
                    self.progress["maximum"] = total
                    self.progress["value"] = idx
                    self.progress_label.config(text=f"Processing {idx}/{total}: {name}")

                elif kind == "rows":
                    self.report_rows.extend(msg[1])

                elif kind == "done":
                    self._finish(msg[1], msg[2])

        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)

    def _finish(self, success, errors):
        self.running = False
        self.process_btn.config(state=tk.NORMAL)
        self.progress_label.config(text=f"Done. Success: {success}, Errors: {errors}")

        if errors:
            messagebox.showwarning(
                "Batch finished with errors",
                f"Success: {success}\nErrors: {errors}\n\nSee log for details.",
            )
        else:
            messagebox.showinfo(
                "Batch complete",
                f"Success: {success}\nErrors: {errors}",
            )

    def _export_csv(self):
        if not self.report_rows:
            messagebox.showinfo("No analysis", "Run processing or dry-run analysis first.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if path:
            write_csv_report(path, self.report_rows)
            messagebox.showinfo("Export complete", f"CSV written to:\n{path}")

    def _export_txt(self):
        if not self.report_rows:
            messagebox.showinfo("No analysis", "Run processing or dry-run analysis first.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt")],
        )
        if path:
            write_txt_report(path, self.report_rows)
            messagebox.showinfo("Export complete", f"TXT written to:\n{path}")

    def _export_json(self):
        if not self.report_rows:
            messagebox.showinfo("No analysis", "Run processing or dry-run analysis first.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
        )
        if path:
            write_json_report(path, self.report_rows)
            messagebox.showinfo("Export complete", f"JSON written to:\n{path}")


def main():
    if DND_AVAILABLE:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
