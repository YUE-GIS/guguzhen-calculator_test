#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""咕咕镇计算器 GUI

左侧是参数，右侧是可粘贴配置的文本框，下面是计算结果和进度。

设计要点：
  · 计算器是交互式控制台程序，靠 stdin 喂命令、stdout 读结果
  · 进度是 "\\rProgress: 12.34%" 形式反复覆盖同一行，必须按 \\r 切分后取最后一段
  · 计算过程可能很久（anpc 几十秒到几分钟），所以放在子线程里跑，避免界面卡死
"""
import os
import re
import sys
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# 结果格式化（中文报告）；缺失时降级为原始输出
try:
    from result_format import format_result as _format_result
except ImportError:
    _format_result = None

APP_TITLE = '咕咕镇计算器'
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = 'newkf.in'

# 计算器候选路径（相对本脚本所在目录）
# 兼容两种布局：
#   1) 脚本和 exe 同目录（仓库扁平结构）
#   2) 脚本在子目录、exe 在上一级（本地开发结构）
EXE_CANDIDATES = [
    'newkf_64.exe', 'newkf.exe',
    os.path.join('..', 'newkf_64.exe'),
    os.path.join('..', 'newkf.exe'),
    os.path.join('..', 'build', 'newkf_64.exe'),
]

# 常用命令
COMMANDS = [
    ('anpc', '为配置里的所有 NPC 搜索最优属性'),
    ('apc',  '为配置里的所有 PC 搜索最优属性'),
    ('bnpc', '计算对配置里所有 NPC 的胜率'),
    ('bpc',  '计算对配置里所有 PC 的胜率'),
    ('al',   '按段位算最优收益（需要参数）'),
    ('rank', '计算所有 PC 的战力排名'),
]


def probe_exe(path, cfg_text, timeout=20):
    """跑一次极小的 bnpc，判断这个 exe 能不能读配置。

    返回 (ok, 说明)。ok=False 时说明里带具体错误。
    """
    workdir = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(workdir, '_gui_probe.in')
    try:
        open(tmp, 'wb').write((cfg_text.strip() + '\n').encode('utf-8'))
    except OSError as e:
        return False, '写临时文件失败: %s' % e
    try:
        r = subprocess.run([path, tmp], cwd=workdir,
                           input=b'tests 1\nbnpc\nq\n',
                           capture_output=True, timeout=timeout)
        out = r.stdout.decode('utf-8', 'replace')
        for tag in ('Error reading NPC', 'Error reading PC',
                    'Error reading user player', 'Error reading aura',
                    'Error reading gear', 'Cannot open config'):
            if tag in out:
                line = next((l for l in out.splitlines() if l.startswith('Error')), tag)
                return False, line.strip()
        if r.returncode != 0:
            return False, '退出码 %s' % r.returncode
        if 'Win Rate' not in out and 'Role' not in out:
            return False, '没有正常输出'
        return True, 'OK'
    except subprocess.TimeoutExpired:
        return False, '超时'
    except Exception as e:
        return False, str(e)[:80]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def find_exe(cfg_text=None):
    """找计算器可执行文件。

    如果有配置文本，会逐个试跑，优先返回「能读这份配置」的那一个。
    实测：CI 编的 newkf_64.exe 可能是旧版（不认 SLIME 这个 NPC 名），
    选中它只会看到 'Error reading NPC enemy data' 且零输出，
    所以能读的优先于更新的。
    """
    cands = []
    for rel in EXE_CANDIDATES:
        p = os.path.normpath(os.path.join(HERE, rel))
        if os.path.isfile(p):
            try:
                st = os.stat(p)
                cands.append((st.st_mtime, st.st_size, p))
            except OSError:
                pass
    if not cands:
        return None
    cands.sort(reverse=True)   # 新的优先

    if not cfg_text or not cfg_text.strip():
        return cands[0][2]

    # 逐个探测，第一个能读的就用
    for _, _, p in cands:
        ok, _msg = probe_exe(p, cfg_text)
        if ok:
            return p
    return cands[0][2]


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.reader_thread = None
        self.msgq = queue.Queue()
        self.stop_flag = False

        root.title(APP_TITLE)
        root.geometry('1400x820')
        root.minsize(1100, 640)

        self._build_style()
        self._build_layout()
        self._sync_al_panel()
        self._poll_queue()

    # ─────────────────────────────────────────────────────
    # 样式
    # ─────────────────────────────────────────────────────
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TLabel', font=('Microsoft YaHei UI', 9))
        style.configure('TButton', font=('Microsoft YaHei UI', 9))
        style.configure('TLabelframe.Label', font=('Microsoft YaHei UI', 9, 'bold'))
        style.configure('Run.TButton', font=('Microsoft YaHei UI', 10, 'bold'))

    # ─────────────────────────────────────────────────────
    # 布局
    # ─────────────────────────────────────────────────────
    def _build_layout(self):
        # 顶部：可执行文件路径
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill='x')
        ttk.Label(top, text='计算器：').pack(side='left')
        # 先尝试读默认配置，用它来挑一个「真能读」的 exe
        _cfg0 = ''
        for _p in (os.path.join(HERE, CONFIG_NAME),
                   os.path.normpath(os.path.join(HERE, '..', CONFIG_NAME))):
            if os.path.isfile(_p):
                try:
                    _cfg0 = open(_p, 'rb').read().decode('utf-8', 'replace')
                    break
                except OSError:
                    pass
        self.exe_var = tk.StringVar(value=find_exe(_cfg0) or '')
        self.exe_entry = ttk.Entry(top, textvariable=self.exe_var)
        self.exe_entry.pack(side='left', fill='x', expand=True, padx=(0, 6))
        ttk.Button(top, text='浏览…', command=self.pick_exe, width=8).pack(side='left')

        # ── 三栏布局：参数 | 配置 | 结果 ──
        # 用 PanedWindow 让用户能自己拖分隔条。
        # 结果区放右侧而不是下方，因为下方的固定高度装不下中文报告。
        mid = ttk.PanedWindow(self.root, orient='horizontal')
        mid.pack(fill='both', expand=True, padx=10, pady=(0, 8))

        # ── 栏1：参数 ──
        left = ttk.Frame(mid, padding=(0, 0, 4, 0))
        mid.add(left, weight=0)

        pf = ttk.LabelFrame(left, text='计算参数', padding=6)
        pf.pack(fill='x', pady=(0, 6))

        self.param_vars = {}
        rows = [
            ('threads', '线程数', '0 = 自动', '0'),
            ('tests',   '单组测试', '每组的模拟场次', '2000'),
            ('seedmax', '最大种子', '搜索空间大小', '30000'),
            ('citest',  'CI 阈值', '0 = 用固定场次', '0'),
            ('verbose', '输出进度', '1 / 0', '1'),
        ]
        for key, label, hint, default in rows:
            r = ttk.Frame(pf)
            r.pack(fill='x', pady=2)
            ttk.Label(r, text=label, width=9).pack(side='left')
            v = tk.StringVar(value=default)
            self.param_vars[key] = v
            ttk.Entry(r, textvariable=v, width=9).pack(side='left')
            ttk.Label(r, text=hint, foreground='#777').pack(side='left', padx=(5, 0))

        cf = ttk.LabelFrame(left, text='命令', padding=6)
        cf.pack(fill='x', pady=(0, 6))
        self.cmd_var = tk.StringVar(value='anpc')
        for name, desc in COMMANDS:
            r = ttk.Frame(cf)
            r.pack(fill='x', pady=1)
            ttk.Radiobutton(r, text=name, value=name, variable=self.cmd_var,
                            width=7).pack(side='left')
            ttk.Label(r, text=desc, foreground='#777', wraplength=170,
                      justify='left').pack(side='left', padx=(3, 0))

        # ── al 专用参数面板（选中 al 时才显示）──
        self.al_frame = ttk.LabelFrame(left, text='al 参数', padding=6)

        r0 = ttk.Frame(self.al_frame)
        r0.pack(fill='x', pady=(0, 4))
        ttk.Label(r0, text='段位', width=6).pack(side='left')
        self.al_rank = tk.StringVar(value='BBB')
        ttk.Combobox(r0, textvariable=self.al_rank, width=8, state='readonly',
                     values=['C', 'CC', 'CCC', 'B', 'BB', 'BBB',
                             'A', 'AA', 'AAA', 'S', 'SS', 'SSS']).pack(side='left')

        ttk.Label(self.al_frame, text='6 种野怪的基础等级（1-4000）',
                  foreground='#777', font=('Microsoft YaHei UI', 8)).pack(anchor='w', pady=(2, 2))

        self.al_bases = {}
        NPC_OLD = [('MU2', '蘑菇Ⅱ'), ('ZHU2', '竹Ⅱ'), ('DENG2', '灯Ⅱ'),
                   ('SHOU2', '兽Ⅱ'), ('YU2', '鱼Ⅱ'), ('HAO2', '蚝Ⅱ')]
        grid = ttk.Frame(self.al_frame)
        grid.pack(fill='x')
        for idx, (key, cn) in enumerate(NPC_OLD):
            rr, cc = divmod(idx, 2)
            cell = ttk.Frame(grid)
            cell.grid(row=rr, column=cc, sticky='w', padx=(0, 6), pady=1)
            ttk.Label(cell, text=cn, width=5).pack(side='left')
            v = tk.StringVar(value='400')
            self.al_bases[key] = v
            ttk.Entry(cell, textvariable=v, width=6).pack(side='left')

        ttk.Label(self.al_frame, text='提示：从 startlevel 起测，胜率<20% 停止',
                  foreground='#777', font=('Microsoft YaHei UI', 8),
                  wraplength=210, justify='left').pack(anchor='w', pady=(4, 0))

        ef = ttk.LabelFrame(left, text='额外命令（每行一条）', padding=6)
        ef.pack(fill='x', pady=(0, 6))
        self.extra_cmd = tk.Text(ef, height=3, width=26, font=('Consolas', 9))
        self.extra_cmd.pack(fill='x')

        # 按命令切换 al 面板的显示
        self.cmd_var.trace_add('write', lambda *a: self._sync_al_panel())

        bf = ttk.Frame(left)
        bf.pack(fill='x', pady=(4, 0))
        self.run_btn = ttk.Button(bf, text='▶ 开始计算', style='Run.TButton', command=self.start)
        self.run_btn.pack(fill='x', pady=(0, 4))
        self.stop_btn = ttk.Button(bf, text='■ 停止', command=self.stop, state='disabled')
        self.stop_btn.pack(fill='x', pady=(0, 4))
        ttk.Button(bf, text='? 自检配置', command=self.selftest).pack(fill='x')

        # ── 栏2：配置 ──
        # 宽度按工具条按钮的宽度定，不需要占太多
        right = ttk.Frame(mid, padding=(4, 0, 4, 0))
        mid.add(right, weight=0)

        cf2 = ttk.LabelFrame(right, text='配置内容', padding=6)
        cf2.pack(fill='both', expand=True)

        bar = ttk.Frame(cf2)
        bar.pack(fill='x', pady=(0, 4))
        ttk.Button(bar, text='📋 粘贴', width=7, command=self.paste_clipboard).pack(side='left', padx=(0, 3))
        ttk.Button(bar, text='💾 写入', width=7, command=self.write_config).pack(side='left', padx=(0, 3))
        ttk.Button(bar, text='↻ 读回', width=7, command=self.load_default).pack(side='left', padx=(0, 3))
        ttk.Button(bar, text='清空', width=6,
                   command=lambda: self.config_text.delete('1.0', 'end')).pack(side='left')

        bar2 = ttk.Frame(cf2)
        bar2.pack(fill='x', pady=(0, 4))
        ttk.Button(bar2, text='导入文件…', width=11, command=self.load_file).pack(side='left', padx=(0, 3))
        ttk.Button(bar2, text='另存为…', width=11, command=self.save_file).pack(side='left')

        self.cfg_path_var = tk.StringVar(value=self._default_cfg_path())
        ttk.Label(cf2, textvariable=self.cfg_path_var, foreground='#666',
                  font=('Consolas', 7), wraplength=260, justify='left').pack(fill='x', pady=(0, 2))

        self.config_text = scrolledtext.ScrolledText(
            cf2, wrap='none', font=('Consolas', 9), undo=True, width=34)
        self.config_text.pack(fill='both', expand=True)

        # ── 栏3：结果 ──
        res = ttk.Frame(mid, padding=(4, 0, 0, 0))
        mid.add(res, weight=1)

        rf = ttk.LabelFrame(res, text='计算结果', padding=6)
        rf.pack(fill='both', expand=True)

        pbar = ttk.Frame(rf)
        pbar.pack(fill='x', pady=(0, 4))
        self.progress = ttk.Progressbar(pbar, mode='determinate', maximum=100)
        self.progress.pack(side='left', fill='x', expand=True)
        self.status_var = tk.StringVar(value='就绪')
        ttk.Label(pbar, textvariable=self.status_var, anchor='w').pack(
            side='left', padx=(8, 0), fill='x', expand=True)

        # 按钮栏先占底部，保证永远可见
        obar = ttk.Frame(rf)
        obar.pack(side='bottom', fill='x', pady=(4, 0))
        ttk.Button(obar, text='复制结果', width=9, command=self.copy_output).pack(side='left', padx=(0, 3))
        ttk.Button(obar, text='保存…', width=7, command=self.save_output).pack(side='left', padx=(0, 3))
        ttk.Button(obar, text='清空', width=6, command=self.clear_output).pack(side='left', padx=(0, 10))
        self.view_mode = tk.StringVar(value='cn')
        ttk.Radiobutton(obar, text='中文', value='cn', variable=self.view_mode,
                        command=self._rerender_output).pack(side='left')
        ttk.Radiobutton(obar, text='原始', value='raw', variable=self.view_mode,
                        command=self._rerender_output).pack(side='left', padx=(6, 0))

        self.out_text = scrolledtext.ScrolledText(
            rf, wrap='word', font=('Consolas', 9), state='disabled')
        self.out_text.pack(fill='both', expand=True, side='top')

    def _sync_al_panel(self):
        """命令切到 al 时显示参数面板，其它命令隐藏。"""
        try:
            if self.cmd_var.get() == 'al':
                self.al_frame.pack(fill='x', pady=(0, 6), before=self.extra_cmd.master)
            else:
                self.al_frame.pack_forget()
        except Exception:
            pass

    def _build_cmds(self):
        """组装要发给计算器的命令序列。"""
        cmds = []
        for key in ('threads', 'tests', 'seedmax', 'citest', 'verbose'):
            v = self.param_vars[key].get().strip()
            if v:
                cmds.append('%s %s' % (key, v))

        cmd = self.cmd_var.get()
        if cmd == 'al':
            rank = self.al_rank.get().strip()
            bases = []
            for key in ('MU2', 'ZHU2', 'DENG2', 'SHOU2', 'YU2', 'HAO2'):
                v = self.al_bases[key].get().strip()
                bases.append(v)
            bad = [b for b in bases if not b.isdigit() or not (1 <= int(b) <= 4000)]
            if bad:
                raise ValueError('al 的 6 个基础等级必须都是 1-4000 的整数\n\n当前有问题的是：%s'
                                 % '、'.join(bad))
            cmds.append('al %s %s' % (rank, ' '.join(bases)))
        else:
            cmds.append(cmd)

        extra = self.extra_cmd.get('1.0', 'end').strip()
        for line in extra.splitlines():
            line = line.strip()
            if line:
                cmds.append(line)
        return cmds

    # ─────────────────────────────────────────────────────
    # 配置文件路径与写入
    # ─────────────────────────────────────────────────────
    def _default_cfg_path(self):
        """newkf.in 的默认位置：优先和 exe 同目录，其次工作区。"""
        exe = ''
        try:
            exe = self.exe_var.get().strip()
        except Exception:
            pass
        if exe and os.path.isfile(exe):
            return os.path.join(os.path.dirname(os.path.abspath(exe)), CONFIG_NAME)
        for p in (os.path.join(HERE, CONFIG_NAME),
                  os.path.normpath(os.path.join(HERE, '..', CONFIG_NAME))):
            if os.path.isfile(p):
                return p
        return os.path.normpath(os.path.join(HERE, '..', CONFIG_NAME))

    def paste_clipboard(self):
        """把剪贴板内容粘到文本框（不弹文件框）。"""
        try:
            data = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo('剪贴板为空', '剪贴板里没有文本。\n\n'
                                '也可以直接在右边文本框里按 Ctrl+V 粘贴。')
            return
        data = data.replace('\r\n', '\n').replace('\r', '\n')
        self.config_text.delete('1.0', 'end')
        self.config_text.insert('1.0', data)
        n = data.count('\n') + 1
        self.status_var.set('已粘贴 %d 行（还没写入文件）' % n)

    def write_config(self):
        """把文本框内容写入 newkf.in。"""
        text = self.config_text.get('1.0', 'end').strip()
        if not text:
            messagebox.showerror('错误', '文本框是空的')
            return
        path = self._default_cfg_path()
        # 写入前备份
        if os.path.isfile(path):
            bak = path + '.bak'
            try:
                shutil.copyfile(path, bak)
            except OSError:
                pass
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 计算器按 ANSI/本地编码读更稳，但实测 UTF-8 也能读（纯 ASCII 内容）
            # 这里统一写 UTF-8 无 BOM
            with open(path, 'wb') as f:
                f.write((text + '\n').encode('utf-8'))
        except Exception as e:
            messagebox.showerror('写入失败', '%s\n\n%s' % (path, e))
            return
        self.cfg_path_var.set(path)
        self.status_var.set('已写入 %s' % os.path.basename(path))

    # ─────────────────────────────────────────────────────
    # 文件操作
    # ─────────────────────────────────────────────────────
    def pick_exe(self):
        p = filedialog.askopenfilename(
            title='选择计算器可执行文件',
            filetypes=[('可执行文件', '*.exe'), ('所有文件', '*.*')])
        if p:
            self.exe_var.set(p)

    def load_file(self):
        p = filedialog.askopenfilename(
            title='导入配置',
            filetypes=[('配置文件', '*.in *.txt'), ('所有文件', '*.*')])
        if not p:
            return
        try:
            data = open(p, 'rb').read().decode('utf-8', 'replace')
        except Exception as e:
            messagebox.showerror('读取失败', str(e))
            return
        self.config_text.delete('1.0', 'end')
        self.config_text.insert('1.0', data)
        self.status_var.set('已导入 %s' % os.path.basename(p))

    def load_default(self):
        """把 newkf.in 的内容读回文本框。"""
        p = self._default_cfg_path()
        if not os.path.isfile(p):
            # 再退回几个可能的位置
            for cand in (os.path.join(HERE, CONFIG_NAME),
                         os.path.normpath(os.path.join(HERE, '..', CONFIG_NAME))):
                if os.path.isfile(cand):
                    p = cand
                    break
            else:
                messagebox.showwarning('没找到', '找不到 %s\n\n可以点「📋 粘贴」从剪贴板导入，\n'
                                       '或「导入文件…」选一个。' % CONFIG_NAME)
                return
        data = open(p, 'rb').read().decode('utf-8', 'replace')
        self.config_text.delete('1.0', 'end')
        self.config_text.insert('1.0', data)
        self.cfg_path_var.set(p)
        self.status_var.set('已读回 %s' % os.path.basename(p))

    def save_file(self):
        p = filedialog.asksaveasfilename(
            title='保存配置', defaultextension='.in',
            initialfile=CONFIG_NAME,
            filetypes=[('配置文件', '*.in'), ('所有文件', '*.*')])
        if not p:
            return
        data = self.config_text.get('1.0', 'end')
        open(p, 'wb').write(data.encode('utf-8'))
        self.status_var.set('已保存 %s' % os.path.basename(p))

    def copy_output(self):
        txt = self.out_text.get('1.0', 'end').strip()
        if not txt:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self.status_var.set('结果已复制')

    def save_output(self):
        txt = self.out_text.get('1.0', 'end').strip()
        if not txt:
            return
        p = filedialog.asksaveasfilename(
            title='保存结果', defaultextension='.txt',
            filetypes=[('文本', '*.txt'), ('所有文件', '*.*')])
        if p:
            open(p, 'wb').write(txt.encode('utf-8'))
            self.status_var.set('结果已保存')

    def clear_output(self):
        self.out_text.configure(state='normal')
        self.out_text.delete('1.0', 'end')
        self.out_text.configure(state='disabled')
        self._raw_output = ''

    # ─────────────────────────────────────────────────────
    # 自检：确认 exe 能读当前配置
    # ─────────────────────────────────────────────────────
    def selftest(self):
        """快速判断 exe 能不能读当前配置。"""
        exe = self.exe_var.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror('错误', '计算器路径无效')
            return
        cfg = self.config_text.get('1.0', 'end').strip()
        if not cfg:
            messagebox.showerror('错误', '配置内容为空')
            return

        self.status_var.set('自检中…')
        self.root.update_idletasks()
        ok, msg = probe_exe(exe, cfg, timeout=30)
        if ok:
            self.status_var.set('自检通过')
            messagebox.showinfo('自检通过',
                                '这个计算器能正常读取当前配置。\n\n%s'
                                % os.path.basename(exe))
        else:
            self.status_var.set('配置读不了')
            messagebox.showerror(
                '配置读不了',
                '这个计算器版本无法解析当前配置：\n\n%s\n\n'
                '常见原因：\n'
                '  · 该版本不认 SLIME 这个 NPC 名（旧版只有 SHI）\n'
                '  · 配置里有该版本不支持的装备/光环/角色\n\n'
                '请点上面的「浏览…」换一个更新的 newkf_64.exe。' % msg)

    # ─────────────────────────────────────────────────────
    # 运行
    # ─────────────────────────────────────────────────────
    def start(self):
        exe = self.exe_var.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror('错误', '计算器路径无效')
            return

        cfg = self.config_text.get('1.0', 'end').strip()
        if not cfg:
            messagebox.showerror('错误', '配置内容为空')
            return

        # 写临时配置文件（用和 exe 同目录，避免路径问题）
        workdir = os.path.dirname(os.path.abspath(exe))
        tmp_cfg = os.path.join(workdir, '_gui_run.in')
        try:
            open(tmp_cfg, 'wb').write((cfg + '\n').encode('utf-8'))
        except Exception as e:
            messagebox.showerror('写入失败', str(e))
            return

        # 同步一份到 newkf.in，保证「文本框内容 == 计算器默认配置」
        try:
            main_cfg = self._default_cfg_path()
            open(main_cfg, 'wb').write((cfg + '\n').encode('utf-8'))
            self.cfg_path_var.set(main_cfg)
        except Exception:
            pass   # 写不了主配置不影响本次计算（用的是 tmp_cfg）

        # 组装命令（al 会带上 6 个基础等级）
        try:
            cmds = self._build_cmds()
        except ValueError as e:
            messagebox.showerror('参数错误', str(e))
            return
        cmds.append('q')

        self.clear_output()
        self._raw_output = ''
        self.progress['value'] = 0
        self.status_var.set('启动中…')
        self.run_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.stop_flag = False

        def worker():
            try:
                # 注意：不能用 bufsize=0 + read(1)。实测在 Windows 管道上
                # 那样读会直接拿不到任何输出（进程 rc=0 但 0 行）。
                # 用默认缓冲 + readline 迭代是可靠的，且能实时拿到进度。
                self.proc = subprocess.Popen(
                    [exe, tmp_cfg],
                    cwd=workdir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                payload = ('\n'.join(cmds) + '\n').encode('utf-8')
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
                self.proc.stdin.close()

                # 进度行用 \r 覆盖同一行，readline() 会把多段合并成一条。
                #
                # 坑（实测）：普通输出行是 "...\r\n"，按 \r 切会得到 [内容, '\n']。
                # 如果把 parts[:-1] 一律当进度处理，所有普通行都会被丢掉，
                # 界面表现为「状态栏显示完成、结果框却是空的」。
                # 正确做法：先按 \r 切，但每一段都要判断它到底是不是进度行。
                for raw in iter(self.proc.stdout.readline, b''):
                    if self.stop_flag:
                        break
                    s = raw.decode('utf-8', 'replace')
                    for seg in s.split('\r'):
                        seg = seg.replace('\n', '').strip()
                        if not seg:
                            continue
                        if 'Progress:' in seg or 'Best result' in seg:
                            self.msgq.put(('progress_line', seg))
                        else:
                            self.msgq.put(('line', seg))

                if self.stop_flag:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                    self.msgq.put(('status', '已停止'))
                self.proc.wait()
                self.msgq.put(('done', self.proc.returncode))
            except Exception as e:
                self.msgq.put(('error', str(e)))

        self.reader_thread = threading.Thread(target=worker, daemon=True)
        self.reader_thread.start()

    def stop(self):
        self.stop_flag = True
        self.status_var.set('正在停止…')
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # ─────────────────────────────────────────────────────
    # 消息循环
    # ─────────────────────────────────────────────────────
    def _append_out(self, text):
        self.out_text.configure(state='normal')
        self.out_text.insert('end', text + '\n')
        self.out_text.see('end')
        self.out_text.configure(state='disabled')

    def _show_text(self, text):
        """整体替换结果框内容。"""
        self.out_text.configure(state='normal')
        self.out_text.delete('1.0', 'end')
        self.out_text.insert('1.0', text)
        self.out_text.see('end')
        self.out_text.configure(state='disabled')

    def _rerender_output(self):
        """按当前显示模式重绘结果区。"""
        raw = getattr(self, '_raw_output', '')
        if not raw:
            return
        if self.view_mode.get() == 'raw' or _format_result is None:
            self._show_text(raw)
            return
        formatted, ok = _format_result(raw)
        if ok and formatted:
            self._show_text(formatted)
        else:
            self._show_text(raw + '\n\n（未识别到 Attribute Result，显示原始输出）')

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == 'line':
                    self._raw_output = getattr(self, '_raw_output', '') + payload + '\n'
                    # 中文模式下不逐行刷，等跑完一次性格式化；
                    # 原始模式保持实时追加，方便看进度细节
                    if self.view_mode.get() == 'raw':
                        self._append_out(payload)
                elif kind == 'progress_line':
                    m = re.search(r'Progress:\s*([\d.]+)%', payload)
                    if m:
                        try:
                            pct = float(m.group(1))
                            self.progress['value'] = max(0, min(100, pct))
                        except ValueError:
                            pct = None
                        best = re.search(r'Best result:\s*([\d.]+)%', payload)
                        eta = re.search(r'Estimated time:\s*([\d.]+)s', payload)
                        bits = []
                        if pct is not None:
                            bits.append('%.1f%%' % pct)
                        if best:
                            bits.append('最佳 %s%%' % best.group(1))
                        if eta:
                            bits.append('剩余 %ss' % eta.group(1))
                        self.status_var.set(' | '.join(bits) if bits else payload[:40])
                    elif payload.strip():
                        # 非进度行（比如 "Attribute seed count"）
                        self.status_var.set(payload.strip()[:40])
                elif kind == 'done':
                    self.run_btn.configure(state='normal')
                    self.stop_btn.configure(state='disabled')
                    self.progress['value'] = 100 if payload == 0 else self.progress['value']
                    # 跑完统一渲染
                    self._rerender_output()
                    raw = getattr(self, '_raw_output', '')
                    if self.view_mode.get() == 'cn' and _format_result is not None:
                        formatted, ok = _format_result(raw)
                        self.status_var.set(
                            '完成（退出码 %s）%s' % (payload, '' if ok else '，未识别结果'))
                    else:
                        self.status_var.set('完成（退出码 %s）' % payload)
                elif kind == 'error':
                    self._append_out('[错误] ' + payload)
                    self.run_btn.configure(state='normal')
                    self.stop_btn.configure(state='disabled')
                    self.status_var.set('出错')
                elif kind == 'status':
                    self.status_var.set(payload)
        except queue.Empty:
            pass
        self.root.after(60, self._poll_queue)


def main():
    root = tk.Tk()
    try:
        root.iconbitmap(default='')
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
