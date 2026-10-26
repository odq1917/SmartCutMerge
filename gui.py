import base64
import os
import queue
import threading
from fractions import Fraction
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import engine

VIDEO_TYPES = [("영상 파일", "*.mp4 *.mkv *.mov *.ts *.flv"), ("모든 파일", "*.*")]


class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root, padding=10)
        self.pack(fill="both", expand=True)
        self.files = []  # {"path", "info", "cuts": [(a, b|None)]}
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.pos = 0.0
        self.seq = 0
        self._after = None
        self.photo = None
        self._build()
        self.after(100, self._poll)

    # ---------- layout ----------
    def _build(self):
        run = ttk.Frame(self)
        run.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_start = ttk.Button(run, text="출력하기 (잘라서 합치기)", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(run, text="취소", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.bar = ttk.Progressbar(run, maximum=1000)
        self.bar.pack(side="left", fill="x", expand=True, padx=6)
        self.v_pct = tk.StringVar(value="0%")
        ttk.Label(run, textvariable=self.v_pct, width=6).pack(side="left")

        self.log = tk.Text(self, height=5, state="disabled", wrap="word")
        self.log.pack(side="bottom", fill="x", pady=(8, 0))

        top = ttk.LabelFrame(self, text="1. 입력 파일 (위에서 아래 순서로 이어 붙임)", padding=6)
        top.pack(fill="x")
        self.file_list = tk.Listbox(top, height=4, exportselection=False)
        self.file_list.pack(side="left", fill="both", expand=True)
        self.file_list.bind("<<ListboxSelect>>", lambda e: self._on_file_select())
        bt = ttk.Frame(top)
        bt.pack(side="left", padx=(6, 0))
        for text, cmd in (("추가", self.add_files), ("▲", lambda: self.move(-1)),
                          ("▼", lambda: self.move(1)), ("삭제", self.remove_file)):
            ttk.Button(bt, text=text, width=6, command=cmd).pack(pady=1)

        pv = ttk.LabelFrame(self, text="미리보기 (슬라이더를 끌어 장면을 찾은 뒤 시작/끝으로 지정)", padding=6)
        pv.pack(fill="x", pady=(8, 0))
        box = tk.Frame(pv, height=232, background="#111")
        box.pack(fill="x")
        box.pack_propagate(False)
        self.img = tk.Label(box, background="#111")
        self.img.pack(expand=True)
        self.tl = tk.Canvas(pv, height=26, bg="#2b2b2b", highlightthickness=0, cursor="hand2")
        self.tl.pack(fill="x", pady=(4, 2))
        self.tl.bind("<Configure>", lambda e: self._draw_tl())
        self.tl.bind("<Button-1>", lambda e: self._tl_seek(e.x, False))
        self.tl.bind("<B1-Motion>", lambda e: self._tl_seek(e.x, False))
        self.tl.bind("<ButtonRelease-1>", lambda e: self._tl_seek(e.x, True))
        nav = ttk.Frame(pv)
        nav.pack(fill="x")
        for text, d in (("-30초", -30), ("-5초", -5), ("-1초", -1), ("◀ 1프레임", "-f"),
                        ("1프레임 ▶", "+f"), ("+1초", 1), ("+5초", 5), ("+30초", 30)):
            ttk.Button(nav, text=text, width=9,
                       command=lambda d=d: self._step(d)).pack(side="left", padx=1)
        self.v_pos = tk.StringVar(value="0:00:00.000")
        ttk.Label(nav, textvariable=self.v_pos, width=12, anchor="e").pack(side="right")
        mk = ttk.Frame(pv)
        mk.pack(fill="x", pady=(4, 0))
        ttk.Button(mk, text="현재 위치를 시작으로", command=lambda: self.v_start.set(engine.fmt_time(self.pos))).pack(side="left")
        ttk.Button(mk, text="현재 위치를 끝으로", command=lambda: self.v_end.set(engine.fmt_time(self.pos))).pack(side="left", padx=4)
        ttk.Label(mk, text="(빨간 영역 = 잘라낼 구간)").pack(side="left", padx=8)

        mid = ttk.LabelFrame(self, text="2. 잘라낼 구간 (선택한 파일 기준, 영상 플레이어에 보이는 시간)", padding=6)
        mid.pack(fill="both", expand=True, pady=8)
        self.cut_list = tk.Listbox(mid, height=4, exportselection=False)
        self.cut_list.pack(side="left", fill="both", expand=True)
        self.cut_list.bind("<<ListboxSelect>>", lambda e: self._jump_cut())
        form = ttk.Frame(mid)
        form.pack(side="left", padx=(6, 0))
        self.v_start, self.v_end = tk.StringVar(), tk.StringVar()
        ttk.Label(form, text="시작  (예: 1:02:10.5)").pack(anchor="w")
        ttk.Entry(form, textvariable=self.v_start, width=16).pack()
        ttk.Label(form, text="끝  (비우면 영상 끝까지)").pack(anchor="w")
        ttk.Entry(form, textvariable=self.v_end, width=16).pack()
        ttk.Button(form, text="구간 추가", command=self.add_cut).pack(fill="x", pady=(4, 1))
        ttk.Button(form, text="구간 삭제", command=self.remove_cut).pack(fill="x")

        out = ttk.LabelFrame(self, text="3. 저장할 파일", padding=6)
        out.pack(fill="x")
        self.v_out = tk.StringVar()
        ttk.Entry(out, textvariable=self.v_out).pack(side="left", fill="x", expand=True)
        ttk.Button(out, text="찾기", command=self.pick_output).pack(side="left", padx=(6, 0))


    # ---------- files ----------
    def add_files(self):
        for p in filedialog.askopenfilenames(filetypes=VIDEO_TYPES):
            try:
                info = engine.probe(p)
            except engine.CutError as e:
                messagebox.showerror("오류", str(e))
                return
            self.files.append({"path": p, "info": info, "cuts": []})
        if self.files and not self.v_out.get():
            stem = os.path.splitext(self.files[0]["path"])[0]
            self.v_out.set(stem + "_합본.mp4")
        self._refresh_files()

    def _refresh_files(self, select=None):
        self.file_list.delete(0, "end")
        for f in self.files:
            self.file_list.insert("end", f"{os.path.basename(f['path'])}   [{engine.fmt_time(f['info']['duration'])}]")
        if select is None and self.files:
            select = 0
        if select is not None and self.files:
            self.file_list.selection_set(select)
        self._refresh_cuts()
        self._request(True)

    def _sel(self):
        s = self.file_list.curselection()
        return s[0] if s else None

    def move(self, d):
        i = self._sel()
        if i is None or not 0 <= i + d < len(self.files):
            return
        self.files[i], self.files[i + d] = self.files[i + d], self.files[i]
        self._refresh_files(i + d)

    def remove_file(self):
        i = self._sel()
        if i is not None:
            del self.files[i]
            self._refresh_files(min(i, len(self.files) - 1) if self.files else None)

    def pick_output(self):
        p = filedialog.asksaveasfilename(defaultextension=".mp4",
                                         filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv")])
        if p:
            self.v_out.set(p)

    # ---------- preview ----------
    def _cur(self):
        i = self._sel()
        return self.files[i] if i is not None else None

    def _on_file_select(self):
        self.pos = 0.0
        self._refresh_cuts()
        self._request(True)

    def _draw_tl(self):
        c = self.tl
        c.delete("all")
        f = self._cur()
        if not f:
            return
        w, dur = c.winfo_width(), f["info"]["duration"]
        for a, b in f["cuts"]:
            c.create_rectangle(a / dur * w, 0, (b if b is not None else dur) / dur * w, 26,
                               fill="#c0392b", outline="")
        x = self.pos / dur * w
        c.create_line(x, 0, x, 26, fill="#f1c40f", width=2)

    def _tl_seek(self, x, final):
        f = self._cur()
        if f:
            w = max(self.tl.winfo_width(), 1)
            self._seek(min(max(x / w, 0), 1) * f["info"]["duration"], final)

    def _step(self, d):
        f = self._cur()
        if not f:
            return
        if d in ("-f", "+f"):
            fr = float(Fraction(f["info"]["fps"]) or 30)
            d = (-1 if d == "-f" else 1) / fr
        self._seek(self.pos + d, True)

    def _seek(self, t, final):
        f = self._cur()
        if not f:
            return
        self.pos = min(max(t, 0.0), max(f["info"]["duration"] - 0.05, 0.0))
        self.v_pos.set(engine.fmt_time(self.pos))
        self._draw_tl()
        if self._after:
            self.after_cancel(self._after)
            self._after = None
        if final:
            self._request(True)
        else:
            self._after = self.after(80, lambda: self._request(False))

    def _request(self, accurate):
        f = self._cur()
        if not f:
            return
        self.seq += 1
        seq, path, t = self.seq, f["path"], self.pos
        self.v_pos.set(engine.fmt_time(t))

        def job():
            try:
                self.q.put(("img", (seq, engine.grab_frame(path, t, 400, accurate))))
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=job, daemon=True).start()

    # ---------- cuts ----------
    def _jump_cut(self):
        i, c = self._sel(), self.cut_list.curselection()
        if i is not None and c:
            self._seek(self.files[i]["cuts"][c[0]][0], True)

    def _refresh_cuts(self):
        self.cut_list.delete(0, "end")
        self._draw_tl()
        i = self._sel()
        if i is None:
            return
        for a, b in self.files[i]["cuts"]:
            self.cut_list.insert("end", f"{engine.fmt_time(a)}  →  {engine.fmt_time(b) if b is not None else '끝'}")

    def add_cut(self):
        i = self._sel()
        if i is None:
            messagebox.showinfo("안내", "먼저 위 목록에서 파일을 선택하세요.")
            return
        try:
            a = engine.parse_time(self.v_start.get())
            b = engine.parse_time(self.v_end.get())
            if a is None:
                raise ValueError
        except ValueError:
            messagebox.showerror("오류", "시간 형식이 잘못되었습니다. 예: 1:02:10.5 / 62:10 / 3730")
            return
        dur = self.files[i]["info"]["duration"]
        if a >= dur or (b is not None and b <= a):
            messagebox.showerror("오류", f"구간이 잘못되었습니다. (영상 길이 {engine.fmt_time(dur)})")
            return
        self.files[i]["cuts"].append((a, b))
        self.files[i]["cuts"].sort()
        self.v_start.set("")
        self.v_end.set("")
        self._refresh_cuts()

    def remove_cut(self):
        i, c = self._sel(), self.cut_list.curselection()
        if i is not None and c:
            del self.files[i]["cuts"][c[0]]
            self._refresh_cuts()

    # ---------- run ----------
    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        if not self.files:
            messagebox.showinfo("안내", "입력 파일을 추가하세요.")
            return
        out = self.v_out.get().strip()
        if not out:
            messagebox.showinfo("안내", "저장할 파일 경로를 지정하세요.")
            return
        if os.path.exists(out) and not messagebox.askyesno("확인", "이미 있는 파일입니다. 덮어쓸까요?"):
            return
        jobs = [{"path": f["path"], "cuts": list(f["cuts"])} for f in self.files]
        self.cancel.clear()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.bar["value"] = 0
        self.worker = threading.Thread(target=self._work, args=(jobs, out), daemon=True)
        self.worker.start()

    def _work(self, jobs, out):
        try:
            engine.merge(jobs, out, on_progress=lambda f: self.q.put(("p", f)),
                         on_log=lambda m: self.q.put(("l", m)), cancel=self.cancel)
            self.q.put(("done", out))
        except engine.Cancelled:
            self.q.put(("l", "취소되었습니다."))
            self.q.put(("done", None))
        except Exception as e:  # noqa: BLE001
            self.q.put(("err", str(e)))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "p":
                    self.bar["value"] = val * 1000
                    self.v_pct.set(f"{val * 100:.0f}%")
                elif kind == "img":
                    seq, data = val
                    if seq == self.seq and data:
                        self.photo = tk.PhotoImage(data=base64.b64encode(data))
                        self.img.configure(image=self.photo)
                elif kind == "l":
                    self._log(val)
                elif kind == "err":
                    self._log(val)
                    messagebox.showerror("오류", val)
                    self._idle()
                elif kind == "done":
                    self._idle()
                    if val and messagebox.askyesno("완료", f"저장했습니다.\n{val}\n\n폴더를 열까요?"):
                        os.startfile(os.path.dirname(os.path.abspath(val)))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def close(self):
        self.cancel.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(5)
        self.winfo_toplevel().destroy()

    def _idle(self):
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")


def run():
    root = tk.Tk()
    root.title("SmartCutMerge - 무손실에 가까운 컷 편집 + 합치기")
    root.minsize(680, 560)
    try:
        engine.tools()
    except engine.CutError as e:
        messagebox.showerror("ffmpeg 필요", str(e))
        return
    app = App(root)
    root.update_idletasks()
    w = max(root.winfo_reqwidth() + 20, 720)
    h = min(root.winfo_reqheight() + 30, root.winfo_screenheight() - 90)
    x = (root.winfo_screenwidth() - w) // 2
    y = max((root.winfo_screenheight() - h) // 2 - 20, 0)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
