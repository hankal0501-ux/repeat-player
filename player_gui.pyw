"""
반복 학습 플레이어 GUI (Tkinter).
영상 URL 입력 → 다운로드 + 무음 감지 + HTML 플레이어 생성 → 브라우저 실행.
"""
import sys, os, subprocess, threading, queue, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

BASE = Path(__file__).parent
MAKE = BASE / 'make_player.py'
OUT_DIR = BASE / 'output'

class App:
    def __init__(self, root):
        self.root = root
        root.title('반복 학습 플레이어 메이커')
        root.geometry('780x600')
        root.configure(bg='#1f2937')
        self.queue = queue.Queue()
        self.process = None
        self.build_ui()
        self.refresh_projects()
        self.root.after(100, self.poll)

    def build_ui(self):
        hdr = tk.Frame(self.root, bg='#111827', height=50)
        hdr.pack(fill='x')
        tk.Label(hdr, text='📺 반복 학습 플레이어 메이커', bg='#111827', fg='#fff',
                 font=('맑은 고딕', 14, 'bold')).pack(side='left', padx=14, pady=12)

        inp = tk.LabelFrame(self.root, text='새 프로젝트', bg='#374151', fg='#fff',
                            font=('맑은 고딕', 10, 'bold'))
        inp.pack(fill='x', padx=10, pady=8)

        # URL/file
        row1 = tk.Frame(inp, bg='#374151')
        row1.pack(fill='x', padx=10, pady=6)
        tk.Label(row1, text='YouTube URL 또는 로컬 영상:', bg='#374151', fg='#fff').pack(side='left')
        self.url_var = tk.StringVar()
        tk.Entry(row1, textvariable=self.url_var, width=50).pack(side='left', padx=8, fill='x', expand=True)
        tk.Button(row1, text='📁', command=self.browse, bg='#4b5563', fg='#fff', relief='flat').pack(side='left')

        # Project ID
        row2 = tk.Frame(inp, bg='#374151')
        row2.pack(fill='x', padx=10, pady=6)
        tk.Label(row2, text='프로젝트 ID (영문/숫자):', bg='#374151', fg='#fff').pack(side='left')
        self.pid_var = tk.StringVar()
        tk.Entry(row2, textvariable=self.pid_var, width=20).pack(side='left', padx=8)

        tk.Label(row2, text='무음 임계 dB:', bg='#374151', fg='#fff').pack(side='left', padx=(20,4))
        self.noise_var = tk.StringVar(value='-30')
        ttk.Combobox(row2, textvariable=self.noise_var, values=['-25','-30','-35','-40'],
                     state='readonly', width=6).pack(side='left')

        tk.Label(row2, text='최소 무음 길이(초):', bg='#374151', fg='#fff').pack(side='left', padx=(10,4))
        self.silence_var = tk.StringVar(value='0.4')
        ttk.Combobox(row2, textvariable=self.silence_var, values=['0.2','0.3','0.4','0.5','0.7','1.0'],
                     state='readonly', width=6).pack(side='left')

        # Buttons
        row3 = tk.Frame(inp, bg='#374151')
        row3.pack(fill='x', padx=10, pady=10)
        self.start_btn = tk.Button(row3, text='▶ 처리 시작', command=self.start,
                                    bg='#22c55e', fg='#fff', font=('맑은 고딕', 11, 'bold'),
                                    padx=20, pady=8, relief='flat')
        self.start_btn.pack(side='left', padx=4)
        self.stop_btn = tk.Button(row3, text='■ 중지', command=self.stop,
                                   bg='#ef4444', fg='#fff', state='disabled',
                                   padx=20, pady=8, relief='flat')
        self.stop_btn.pack(side='left', padx=4)
        tk.Button(row3, text='🌐 서버 시작', command=self.start_server,
                  bg='#1e3a8a', fg='#fff', padx=15, pady=8, relief='flat').pack(side='left', padx=4)
        tk.Button(row3, text='🔄 목록 새로고침', command=self.refresh_projects,
                  bg='#6b7280', fg='#fff', padx=15, pady=8, relief='flat').pack(side='left', padx=4)

        # Project list
        proj_frame = tk.LabelFrame(self.root, text='기존 프로젝트', bg='#374151', fg='#fff',
                                    font=('맑은 고딕', 10, 'bold'))
        proj_frame.pack(fill='x', padx=10, pady=4)
        cols = ('id', 'video', 'segments')
        self.tree = ttk.Treeview(proj_frame, columns=cols, show='headings', height=4)
        self.tree.heading('id', text='ID')
        self.tree.heading('video', text='영상')
        self.tree.heading('segments', text='문장수')
        self.tree.column('id', width=120)
        self.tree.column('video', width=350)
        self.tree.column('segments', width=80, anchor='center')
        self.tree.pack(fill='x', padx=8, pady=4)
        self.tree.bind('<Double-1>', lambda e: self.open_project())
        tk.Button(proj_frame, text='더블클릭 → 브라우저에서 열기', bg='#4b5563', fg='#fff',
                  relief='flat').pack(pady=4)

        # Log
        log_frame = tk.LabelFrame(self.root, text='진행 로그', bg='#374151', fg='#fff',
                                   font=('맑은 고딕', 10, 'bold'))
        log_frame.pack(fill='both', expand=True, padx=10, pady=4)
        self.log = scrolledtext.ScrolledText(log_frame, height=12, font=('Consolas', 9),
                                              bg='#0f172a', fg='#e5e7eb')
        self.log.pack(fill='both', expand=True, padx=4, pady=4)

        self.status_var = tk.StringVar(value='대기 중')
        tk.Label(self.root, textvariable=self.status_var, bd=1, relief='sunken',
                 anchor='w', bg='#1f2937', fg='#9ca3af').pack(side='bottom', fill='x')

    def log_msg(self, msg):
        self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.root.update_idletasks()

    def browse(self):
        f = filedialog.askopenfilename(title='영상 파일 선택',
                                        filetypes=[('Video', '*.mp4 *.mkv *.webm *.mov')])
        if f: self.url_var.set(f)

    def refresh_projects(self):
        for r in self.tree.get_children():
            self.tree.delete(r)
        if not OUT_DIR.exists(): return
        import json
        for html in OUT_DIR.glob('*.html'):
            pid = html.stem
            video = OUT_DIR / f'{pid}.mp4'
            seg_file = OUT_DIR / f'{pid}.segments.json'
            n_seg = '?'
            if seg_file.exists():
                try:
                    n_seg = json.loads(seg_file.read_text(encoding='utf-8'))['count']
                except: pass
            self.tree.insert('', 'end', values=(pid, video.name if video.exists() else '(missing)', n_seg))

    def open_project(self):
        sel = self.tree.selection()
        if not sel: return
        pid = self.tree.item(sel[0])['values'][0]
        url = f'http://localhost:5757/{pid}.html'
        # Try server first, fallback to file
        import urllib.request
        try:
            urllib.request.urlopen(url, timeout=1)
            webbrowser.open(url)
        except Exception:
            html = OUT_DIR / f'{pid}.html'
            if html.exists(): webbrowser.open(html.as_uri())

    def start(self):
        url = self.url_var.get().strip()
        pid = self.pid_var.get().strip()
        if not url or not pid:
            messagebox.showerror('오류', 'URL과 ID 모두 입력하세요'); return
        if not pid.replace('_','').replace('-','').isalnum():
            messagebox.showerror('오류', 'ID는 영문/숫자/_/- 만 사용'); return

        self.log.delete('1.0', 'end')
        self.log_msg(f'=== 시작: {pid} ===')
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.status_var.set(f'처리 중... ({pid})')

        cmd = [sys.executable, str(MAKE), url, pid,
               '--noise', self.noise_var.get(),
               '--silence', self.silence_var.get()]
        threading.Thread(target=self.run_subprocess, args=(cmd,), daemon=True).start()

    def run_subprocess(self, cmd):
        try:
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             bufsize=1, text=True, encoding='utf-8', errors='replace',
                                             cwd=str(BASE))
            for line in self.process.stdout:
                self.queue.put(('log', line.rstrip()))
            self.process.wait()
            self.queue.put(('done', self.process.returncode))
        except Exception as e:
            self.queue.put(('error', str(e)))

    def poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == 'log':
                    self.log_msg(payload)
                elif kind == 'done':
                    self.start_btn.config(state='normal')
                    self.stop_btn.config(state='disabled')
                    if payload == 0:
                        self.status_var.set('완료')
                        self.refresh_projects()
                        if messagebox.askyesno('완료', '플레이어 생성 완료. 브라우저에서 열까요?'):
                            self.start_server()
                            import time; time.sleep(1)
                            url = f'http://localhost:5757/{self.pid_var.get().strip()}.html'
                            webbrowser.open(url)
                    else:
                        self.status_var.set(f'실패 (코드 {payload})')
                elif kind == 'error':
                    self.log_msg(f'오류: {payload}')
                    self.start_btn.config(state='normal')
                    self.stop_btn.config(state='disabled')
        except queue.Empty: pass
        self.root.after(100, self.poll)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.log_msg('중지 요청됨')

    def start_server(self):
        # Check if already running
        import urllib.request
        try:
            urllib.request.urlopen('http://127.0.0.1:5757/', timeout=1)
            self.log_msg('서버 이미 실행 중 (5757)')
            return
        except: pass
        # Start no-cache server
        srv_script = OUT_DIR / '_serve.py'
        srv_script.write_text(
            "from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
            "class H(SimpleHTTPRequestHandler):\n"
            "    def end_headers(self):\n"
            "        self.send_header('Cache-Control','no-store')\n"
            "        super().end_headers()\n"
            "ThreadingHTTPServer(('0.0.0.0',5757), H).serve_forever()\n",
            encoding='utf-8')
        # Launch detached
        subprocess.Popen([sys.executable, str(srv_script)],
                         cwd=str(OUT_DIR),
                         creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0)
        self.log_msg('서버 시작됨 (http://localhost:5757)')

if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
