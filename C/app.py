"""Windows entry point for schematic-to-PCB generation."""

import os
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from tkinter import filedialog, messagebox, ttk

from auto_pcb import DEMO, DEMO_STRATEGY, ROOT, generate
from verify import find_kicad_cli


class DesignWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KiCad 自动 PCB 设计助手")
        self.geometry("870x590")
        self.minsize(670, 480)
        self.schematic = tk.StringVar()
        self.output = tk.StringVar(value=str(ROOT / "results"))
        self.kicad = tk.StringVar(value=str(find_kicad_cli() or ""))
        self.model = tk.BooleanVar()
        self.width = tk.StringVar()
        self.height = tk.StringVar()
        self.status = tk.StringVar(value="选择原理图，或载入 Boost 练习原理图。")
        self.result = None
        self.events = Queue()
        self.running = False
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        panel = ttk.Frame(body)
        panel.pack(fill="x")
        ttk.Label(panel, text="KiCad 自动 PCB 设计", font=("Microsoft YaHei UI", 19, "bold")).pack(side="left")
        ttk.Button(panel, text="载入 Boost 练习原理图", command=lambda: self.schematic.set(str(DEMO))).pack(side="right")

        for label, value, browse in (
            ("KiCad 原理图", self.schematic, self.pick_input),
            ("结果保存位置", self.output, self.pick_output),
            ("KiCad 命令行工具", self.kicad, self.pick_kicad),
        ):
            row = ttk.Frame(body)
            row.pack(fill="x", pady=(12, 0))
            ttk.Label(row, text=label, width=17).pack(side="left")
            ttk.Entry(row, textvariable=value).pack(side="left", expand=True, fill="x", padx=(0, 8))
            ttk.Button(row, text="选择…", command=browse).pack(side="right")
        ttk.Checkbutton(body, text="请 Codex 提出布局优先级（会提交器件和网络数据）",
                        variable=self.model).pack(anchor="w", pady=(12, 6))
        size = ttk.Frame(body)
        size.pack(fill="x", pady=(4, 0))
        ttk.Label(size, text="指定板框（mm）", width=17).pack(side="left")
        ttk.Entry(size, textvariable=self.width, width=7).pack(side="left")
        ttk.Label(size, text=" × ").pack(side="left")
        ttk.Entry(size, textvariable=self.height, width=7).pack(side="left")
        ttk.Label(size, text="留空自动估算").pack(side="left", padx=8)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=7)
        self.run_button = ttk.Button(actions, text="自动布局布线", command=self.submit)
        self.run_button.pack(side="left")
        self.open_board = ttk.Button(actions, text="打开 PCB", state="disabled",
                                     command=lambda: self.open_result("board"))
        self.open_board.pack(side="right")
        self.open_folder = ttk.Button(actions, text="打开结果目录", state="disabled",
                                      command=lambda: self.open_result("directory"))
        self.open_folder.pack(side="right", padx=8)
        ttk.Label(body, textvariable=self.status, wraplength=730).pack(anchor="w", pady=(0, 8))
        self.log = tk.Text(body, state="disabled", height=12, wrap="word", font=("Microsoft YaHei UI", 10))
        self.log.pack(fill="both", expand=True)
        self.after(120, self.poll)

    def pick_input(self):
        chosen = filedialog.askopenfilename(parent=self, title="选择 KiCad 原理图或网表",
                                            filetypes=[("KiCad 设计", "*.kicad_sch *.sch *.net *.xml")])
        if chosen:
            self.schematic.set(chosen)

    def pick_output(self):
        directory = filedialog.askdirectory(parent=self, title="选择生成结果的位置")
        if directory:
            self.output.set(directory)

    def pick_kicad(self):
        chosen = filedialog.askopenfilename(parent=self, title="选择 kicad-cli.exe",
                                            filetypes=[("KiCad 命令行程序", "kicad-cli.exe")])
        if chosen:
            self.kicad.set(chosen)

    def submit(self):
        path, target = self.schematic.get().strip(), self.output.get().strip()
        if not path or not target:
            messagebox.showerror("缺少工程信息", "请选择原理图和结果保存目录。", parent=self)
            return
        dimensions = None
        width, height = self.width.get().strip(), self.height.get().strip()
        if width or height:
            try:
                dimensions = float(width), float(height)
            except ValueError:
                messagebox.showerror("板框尺寸", "请同时填写有效的宽和高。", parent=self)
                return
        self.running = True
        self.result = None
        for button in (self.run_button, self.open_board, self.open_folder):
            button.state(["disabled"])
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.status.set("正在生成 PCB…")
        Thread(target=self._worker, args=(path, target, self.kicad.get().strip(), self.model.get(), dimensions),
               daemon=True).start()

    def _worker(self, path, target, cli, model, dimensions):
        try:
            strategy = DEMO_STRATEGY if not model and Path(path).resolve() == DEMO else None
            result = generate(path, target, cli_path=cli or None, use_model=model, dimensions=dimensions,
                              strategy_file=strategy,
                              progress=lambda message: self.events.put(("progress", message)))
        except (RuntimeError, ValueError, OSError) as error:
            self.events.put(("error", str(error)))
        except Exception as error:
            self.events.put(("error", f"{type(error).__name__}: {error}"))
        else:
            self.events.put(("result", result))

    def poll(self):
        while True:
            try:
                event, value = self.events.get_nowait()
            except Empty:
                break
            if event == "progress":
                self.status.set(value)
                self.append(value)
                continue
            self.running = False
            self.run_button.state(["!disabled"])
            if event == "error":
                self.status.set("未能生成 PCB。")
                self.append(value)
            else:
                self.result = value
                drc = value["drc"]
                self.open_board.state(["!disabled"])
                self.open_folder.state(["!disabled"])
                self.status.set("PCB 已生成，可在 KiCad 中打开。")
                if drc["status"] == "failed":
                    self.append("KiCad DRC 未完成，详情见设计报告。")
                else:
                    self.append(f"DRC 未连接 {drc['unconnected']} 项，错误 {drc['errors']}，警告 {drc['warnings']}。")
                self.append(str(value["report"]))
        self.after(120, self.poll)

    def append(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_result(self, item):
        if self.result:
            try:
                os.startfile(self.result[item])
            except OSError as error:
                messagebox.showerror("无法打开", str(error), parent=self)

    def close(self):
        if self.running:
            self.status.set("PCB 正在生成，完成后可关闭窗口。")
        else:
            self.destroy()


if __name__ == "__main__":
    DesignWindow().mainloop()
