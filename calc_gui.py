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
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# 结果格式化（中文报告）；缺失时降级为原始输出
try:
    from result_format import format_result as _format_result
except ImportError:
    _format_result = None

# 多角色批量计算
try:
    import multi_role as _mr
except ImportError:
    _mr = None

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

        # 关窗时先杀计算子进程，否则它会变成孤儿继续占满 CPU，
        # 表现是「关掉窗口后用中文输入法打字会卡一下」。
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

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

        # ── 栏1：参数（可滚动）──
        # 实测：显示角色勾选面板 + 「额外命令」时，左侧内容高于窗口，
        # 底部按钮被压缩到 2px 完全看不见。所以套一层 Canvas 滚动。
        # 栏1 用一个纵向容器，上=滚动区，下=固定按钮区。
        # 直接往 PanedWindow 里放两个控件会被当成两栏，按钮被压成 1px（实测）。
        col1 = ttk.Frame(mid, padding=(0, 0, 4, 0))
        mid.add(col1, weight=0)

        left_outer = ttk.Frame(col1)
        left_outer.pack(fill='both', expand=True)

        _canvas = tk.Canvas(left_outer, highlightthickness=0, width=270)
        _vsb = ttk.Scrollbar(left_outer, orient='vertical', command=_canvas.yview)
        _canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side='right', fill='y')
        _canvas.pack(side='left', fill='both', expand=True)

        left = ttk.Frame(_canvas, padding=(0, 0, 4, 0))
        _win = _canvas.create_window((0, 0), window=left, anchor='nw')

        def _on_left_configure(_evt=None):
            _canvas.configure(scrollregion=_canvas.bbox('all'))

        def _on_canvas_configure(evt):
            _canvas.itemconfigure(_win, width=evt.width)

        def _on_wheel(evt):
            # Windows: delta 是 120 的倍数
            _canvas.yview_scroll(int(-1 * (evt.delta / 120)), 'units')

        left.bind('<Configure>', _on_left_configure)
        _canvas.bind('<Configure>', _on_canvas_configure)
        # 鼠标进入左栏时接管滚轮
        def _bind_wheel(_e):
            _canvas.bind_all('<MouseWheel>', _on_wheel)
        def _unbind_wheel(_e):
            _canvas.unbind_all('<MouseWheel>')
        left_outer.bind('<Enter>', _bind_wheel)
        left_outer.bind('<Leave>', _unbind_wheel)
        self._left_canvas = _canvas

        # 主按钮固定在左栏底部，不进滚动区
        bf = ttk.Frame(col1)
        bf.pack(side='bottom', fill='x', pady=(4, 0))
        self.run_btn = ttk.Button(bf, text='▶ 开始计算', style='Run.TButton', command=self.start)
        self.run_btn.pack(fill='x', pady=(0, 4))
        self.stop_btn = ttk.Button(bf, text='■ 停止', command=self.stop, state='disabled')
        self.stop_btn.pack(fill='x', pady=(0, 4))
        ttk.Button(bf, text='? 自检配置', command=self.selftest).pack(fill='x')
        self._btn_frame = bf

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

        # ── 多角色批量计算（换成 apc 命令时才显示）──
        # opt_frame 是「可选参数」的固定容器，永远 pack 在「额外命令」之前。
        # 两个面板只在容器内 pack/unpack，避免 before=<未 packed 控件> 的 TclError。
        self.opt_frame = ttk.Frame(left)
        self.opt_frame.pack(fill='x')
        self.role_frame = ttk.LabelFrame(self.opt_frame, text='角色（勾选多个则批量计算）', padding=6)

        rb1 = ttk.Frame(self.role_frame)
        rb1.pack(fill='x', pady=(0, 3))
        ttk.Button(rb1, text='全选', width=6,
                   command=lambda: self._select_roles(True)).pack(side='left', padx=(0, 3))
        ttk.Button(rb1, text='全不选', width=7,
                   command=lambda: self._select_roles(False)).pack(side='left', padx=(0, 3))
        ttk.Button(rb1, text='反选', width=6,
                   command=self._invert_roles).pack(side='left')

        self.role_vars = {}
        grid = ttk.Frame(self.role_frame)
        grid.pack(fill='x', pady=(2, 0))
        ROLE_ORDER = ['MO', 'LIN', 'AI', 'MENG', 'WEI', 'YI', 'MING',
                      'MIN', 'WU', 'XI', 'XIA', 'YA', 'QI']
        for idx, role in enumerate(ROLE_ORDER):
            rr, cc = divmod(idx, 3)
            v = tk.BooleanVar(value=(role == 'MENG'))
            self.role_vars[role] = v
            label = _mr.cn(role) if _mr else role
            ttk.Checkbutton(grid, text='%s' % label, variable=v,
                            width=5).grid(row=rr, column=cc, sticky='w', padx=(0, 2))

        self.reset_attr_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.role_frame,
                        text='搜前重置加点为 1',
                        variable=self.reset_attr_var).pack(anchor='w', pady=(4, 0))

        self.attr_hint = ttk.Label(self.role_frame, text='', foreground='#a60',
                                   font=('Microsoft YaHei UI', 8),
                                   wraplength=210, justify='left')
        self.attr_hint.pack(anchor='w')

        ttk.Label(self.role_frame,
                  text='只换第一行角色名，装备/光环/许愿不变。\n'
                       '勾 1 个 = 普通计算；勾多个 = 依次跑并汇总。\n'
                       '注意 apc/bpc 需要配置里有 PC 段。',
                  foreground='#777', font=('Microsoft YaHei UI', 8),
                  wraplength=210, justify='left').pack(anchor='w', pady=(3, 0))

        # ── al 专用参数面板（选中 al 时才显示）──
        self.al_frame = ttk.LabelFrame(self.opt_frame, text='al 参数', padding=6)

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

        # 折叠控制条（固定布局，不随角色数变化）
        self.fold_bar = ttk.Frame(rf)
        self.fold_bar.pack(fill='x', pady=(0, 4))
        ttk.Label(self.fold_bar, text='明细：').pack(side='left', padx=(0, 4))
        ttk.Button(self.fold_bar, text='展开选中', width=9,
                   command=lambda: self._fold_selected(True)).pack(side='left', padx=(0, 3))
        ttk.Button(self.fold_bar, text='收起选中', width=9,
                   command=lambda: self._fold_selected(False)).pack(side='left', padx=(0, 3))
        ttk.Button(self.fold_bar, text='全展开', width=7,
                   command=lambda: self._toggle_fold_all(True)).pack(side='left', padx=(0, 3))
        ttk.Button(self.fold_bar, text='全收起', width=7,
                   command=lambda: self._toggle_fold_all(False)).pack(side='left', padx=(0, 8))
        self.fold_pick = tk.StringVar(value='')
        self.fold_combo = ttk.Combobox(self.fold_bar, textvariable=self.fold_pick,
                                       width=14, state='readonly', values=[])
        self.fold_combo.pack(side='left')
        self.fold_combo.bind('<<ComboboxSelected>>',
                             lambda e: self._fold_selected(True))

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

    # ─────────────────────────────────────────────────────
    # 多角色
    # ─────────────────────────────────────────────────────
    def _select_roles(self, on):
        for v in self.role_vars.values():
            v.set(on)

    def _invert_roles(self):
        for v in self.role_vars.values():
            v.set(not v.get())

    def _checked_roles(self):
        return [r for r, v in self.role_vars.items() if v.get()]

    def _update_attr_hint(self):
        """在角色面板里提示加点是否已满（满了搜索会原样返回）。"""
        if _mr is None or not hasattr(self, 'attr_hint'):
            return
        try:
            cfg = self.config_text.get('1.0', 'end')
            cur = _mr.current_attr_sum(cfg)
            tot = _mr.total_attr_points(cfg)
            if cur is None or tot is None:
                self.attr_hint.configure(text='')
                return
            if cur >= tot:
                self.attr_hint.configure(
                    text='⚠ 当前加点 %d / 上限 %d，已满，搜索会原样返回' % (cur, tot))
            else:
                self.attr_hint.configure(
                    text='加点 %d / 上限 %d，可分配 %d' % (cur, tot, tot - cur))
        except Exception:
            pass

    def _sync_role_panel(self):
        """搜索类命令（anpc/apc/bnpc/bpc）可多选角色批量跑。

        注意：apc/bpc 需要配置里有 PC 段（玩家作为对手）；
        只有 anpc/bnpc 能配合纯 NPC 配置。选中 apc 但没有 PC 段时，
        计算器会报 No PC data in config file。
        """
        try:
            ok_cmds = ('anpc', 'apc', 'bnpc', 'bpc')
            if self.cmd_var.get() in ok_cmds and _mr is not None:
                self.role_frame.pack(fill='x', pady=(0, 6), before=self.al_frame)
            else:
                self.role_frame.pack_forget()
        except Exception:
            pass

    def _sync_al_panel(self):
        """命令切到 al 时显示参数面板，其它命令隐藏。"""
        if self.cmd_var.get() == 'al':
            self.al_frame.pack(fill='x', pady=(0, 6))
        else:
            self.al_frame.pack_forget()
        self._sync_role_panel()

    def _sync_role_panel(self):
        """搜索类命令（anpc/apc/bnpc/bpc）可多选角色批量跑。

        注意：apc/bpc 需要配置里有 PC 段（玩家作为对手）；
        只有 anpc/bnpc 能配合纯 NPC 配置。
        """
        if _mr is None:
            return
        if self.cmd_var.get() in ('anpc', 'apc', 'bnpc', 'bpc'):
            self.role_frame.pack(fill='x', pady=(0, 6))
            self._update_attr_hint()
        else:
            self.role_frame.pack_forget()

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

        # 组装命令（al 会带上 6 个基础等级）
        try:
            cmds = self._build_cmds()
        except ValueError as e:
            messagebox.showerror('参数错误', str(e))
            return
        cmds.append('q')

        # 决定要跑哪几份配置
        # 勾选 >1 个角色 → 每个角色一份配置，依次跑
        jobs = []       # [(显示名, 配置文本)]
        _roles = self._checked_roles() if (_mr is not None and
                                           self.cmd_var.get() in ('anpc', 'apc', 'bnpc', 'bpc')) else []
        if len(_roles) > 1:
            roles = _roles
            if not roles:
                messagebox.showerror('错误', '请至少勾选一个角色')
                return
            try:
                cur = _mr.parse_player(cfg)
            except Exception:
                cur = None
            if cur is None:
                messagebox.showerror(
                    '配置格式不对',
                    '找不到玩家行。\n\n'
                    '格式：ROLE [G=成长] [M=模式] 等级 强化 技能位 品质\n'
                    '例如：MENG 801 206 5 9')
                return
            # 搜索前是否重置加点。
            # 实测：anpc/apc 是增量搜索，从配置现有加点出发分配剩余点数
            # （attrLeft = totalAttr - sum(myattr)）。加点填满时搜索空间≈0，
            # 会原样返回配置值，表现为「所有角色加点都一样」。
            _reset = self.reset_attr_var.get()
            for r in roles:
                try:
                    one = _mr.swap_role(cfg, r)
                    if _reset:
                        one, _ = _mr.reset_attrs(one, 1)
                    jobs.append((_mr.cn(r), one))
                except Exception as e:
                    messagebox.showerror('生成配置失败', '%s: %s' % (r, e))
                    return
        else:
            one = cfg
            if self.reset_attr_var.get() and _mr is not None \
                    and self.cmd_var.get() in ('anpc', 'apc'):
                try:
                    one, _ = _mr.reset_attrs(one, 1)
                except Exception:
                    pass
            jobs.append((None, one))

        workdir = os.path.dirname(os.path.abspath(exe))

        # 同步一份到 newkf.in（单角色时）
        if len(jobs) == 1:
            try:
                main_cfg = self._default_cfg_path()
                open(main_cfg, 'wb').write((jobs[0][1] + '\n').encode('utf-8'))
                self.cfg_path_var.set(main_cfg)
            except Exception:
                pass

        self.clear_output()
        self._raw_output = ''
        self._multi_header = []      # [(角色名, 原始输出)]
        self.progress['value'] = 0
        self.status_var.set('启动中…')
        self.run_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.stop_flag = False
        self._total_jobs = len(jobs)

        def run_one(cfg_text, tmp_cfg):
            """跑一份配置，把输出推进队列。返回退出码。"""
            open(tmp_cfg, 'wb').write((cfg_text + '\n').encode('utf-8'))
            self.proc = subprocess.Popen(
                [exe, tmp_cfg], cwd=workdir,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
            payload = ('\n'.join(cmds) + '\n').encode('utf-8')
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
            self.proc.stdin.close()

            # 进度行用 \r 覆盖；普通行是 \r\n。
            # 必须逐段判断，否则普通行会被当成进度丢掉（实测过）。
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
            self.proc.wait()
            return self.proc.returncode

        def worker():
            try:
                # 每个角色用独立文件。
                # 坑（实测）：多角色共用同一个 _gui_run.in 时，
                # 前一个进程还没读完文件就被下一个覆盖，会看到
                # "Error reading THREADS setting" 之类的假错误。
                total = len(jobs)
                rc_all = []
                for idx, (role_cn, cfg_text) in enumerate(jobs):
                    if self.stop_flag:
                        break
                    if total > 1:
                        self.msgq.put(('job_start', (idx, total, role_cn)))
                    tmp_cfg = os.path.join(workdir, '_gui_run_%d.in' % idx)
                    rc = run_one(cfg_text, tmp_cfg)
                    rc_all.append(rc)
                    if total > 1:
                        self.msgq.put(('job_done', (idx, total, role_cn, rc)))
                if self.stop_flag:
                    self.msgq.put(('status', '已停止'))
                self.msgq.put(('done', 0 if all(r == 0 for r in rc_all) else 1))
            except Exception as e:
                self.msgq.put(('error', str(e)))

        self.reader_thread = threading.Thread(target=worker, daemon=True)
        self.reader_thread.start()

    def _kill_proc_tree(self):
        """杀掉计算子进程（含可能的子进程树）。"""
        p = getattr(self, 'proc', None)
        if p is None:
            return
        try:
            if p.poll() is not None:
                return
        except Exception:
            pass
        try:
            # Windows 上用 taskkill /T 连子进程一起杀；其他平台退化为 kill
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)],
                               capture_output=True, timeout=5)
            else:
                p.kill()
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            p.wait(timeout=3)
        except Exception:
            pass

    def _on_close(self):
        """关窗：先停计算，再退出。"""
        self.stop_flag = True
        self._kill_proc_tree()
        try:
            self.root.destroy()
        except Exception:
            pass

    def stop(self):
        self.stop_flag = True
        self.status_var.set('正在停止…')
        self._kill_proc_tree()

    # ─────────────────────────────────────────────────────
    # 消息循环
    # ─────────────────────────────────────────────────────
    def _append_out(self, text):
        """追加一行。攒批写入，避免逐行重绘拖慢 UI。"""
        buf = getattr(self, '_out_buf', None)
        if buf is None:
            buf = self._out_buf = []
        buf.append(text)
        # 攒到 40 行或距上次刷新超过 200ms 再真正写入
        now = time.time()
        last = getattr(self, '_out_flush_t', 0)
        if len(buf) >= 40 or (now - last) > 0.2:
            self._flush_out()

    def _flush_out(self):
        buf = getattr(self, '_out_buf', None)
        if not buf:
            return
        self._out_buf = []
        self._out_flush_t = time.time()
        self.out_text.configure(state='normal')
        self.out_text.insert('end', '\n'.join(buf) + '\n')
        self.out_text.see('end')
        self.out_text.configure(state='disabled')

    def _show_text(self, text):
        """整体替换结果框内容。"""
        self._out_buf = []
        self.out_text.configure(state='normal')
        self.out_text.delete('1.0', 'end')
        self.out_text.insert('1.0', text)
        self.out_text.see('end')
        self.out_text.configure(state='disabled')

    def _render_multi(self, multi):
        """多角色结果：汇总常显，各角色明细可折叠。

        折叠用 Text 的 elide 标签实现：
          · 每个角色的明细打上 'det_<i>' 标签
          · 标题行（━ 角色 ━）默认加 elide → 明细不可见
          · 点标题行切换该标签的 elide 状态
        比销毁/重建控件轻得多，长文本也不会卡。
        """
        if self.view_mode.get() == 'raw':
            parts = []
            for role_cn, raw, rc in multi:
                parts.append('=' * 56)
                parts.append('  %s' % role_cn)
                parts.append('=' * 56)
                parts.append(raw.rstrip())
                parts.append('')
            self._show_text('\n'.join(parts))
            self._fold_tags = {}
            return

        if _format_result is None:
            self._show_text('\n'.join(raw for _, raw, _ in multi))
            self._fold_tags = {}
            return

        rows = []
        for role_cn, raw, rc in multi:
            text, ok = _format_result(raw)
            rows.append({
                'role': role_cn, 'raw': raw, 'fmt': text if ok else '',
                'ok': ok, 'rc': rc,
                'attr': self._extract_attrs(raw),
                'avg': self._extract_avg(raw),
            })

        # ── 汇总部分（常显，不折叠）──
        head = []
        head.append('=' * 60)
        head.append('多角色批量计算')
        head.append('=' * 60)
        head.append('')
        head.append('【汇总】%-8s %-10s %-22s %s'
                    % ('角色', '平均胜率', '加点（力敏智体精意）', '合计'))
        head.append('-' * 60)
        for r in rows:
            at = r['attr']
            tot = sum(at) if at else 0
            attr_s = ' '.join(str(x) for x in at) if at else '（未出结果）'
            avg = r['avg'] or '—'
            head.append('  %-8s %-10s %-22s %s'
                        % (r['role'], avg, attr_s, tot if tot else ''))
        head.append('')

        def wkey(r):
            try:
                return -float(r['avg'].rstrip('%')) if r['avg'] else 1e9
            except (ValueError, AttributeError):
                return 1e9

        ranked = sorted(rows, key=wkey)
        if len(ranked) > 1 and any(r['avg'] for r in ranked):
            head.append('【排名】（按平均胜率）')
            for i, r in enumerate(ranked, 1):
                if r['avg']:
                    mark = ' ← 最优' if i == 1 else ''
                    head.append('  %d. %-6s %s%s' % (i, r['role'], r['avg'], mark))
            head.append('')

        head.append('=' * 60)
        head.append('各角色明细（下方按钮控制展开/收起）')
        head.append('=' * 60)
        head.append('')

        # 写入汇总
        self._show_text('\n'.join(head))
        txt = self.out_text
        txt.configure(state='normal')

        # ── 明细部分（可折叠）──
        # 用「下拉框 + 按钮」而不是每个角色一个按钮：
        # 实测 13 个角色时按钮总宽超过栏宽，后面几个被压成 1px，点不动。
        self._fold_tags = {}
        self._fold_roles = {}
        self._fold_tags_order = []

        for idx, r in enumerate(rows):
            tag = 'det_%d' % idx
            self._fold_tags[tag] = False
            self._fold_roles[tag] = r['role']
            self._fold_tags_order.append(tag)

            txt.insert('end', '━' * 56 + '\n')
            line_start = txt.index('end-2l linestart')
            txt.insert('end', '  ▶ %s   %s\n' % (r['role'], r['avg'] or '—'))
            line_end = txt.index('end-1c')
            txt.tag_add('foldhdr_%d' % idx, line_start, line_end)
            txt.tag_configure('foldhdr_%d' % idx, background='#eef4ff',
                              font=('Consolas', 9, 'bold'))

            body = r['fmt'] if r['fmt'] else r['raw'].rstrip()
            txt.insert('end', body + '\n\n', (tag,))
            txt.tag_configure(tag, elide=True)

        # 下拉框选项：角色名
        names = [r['role'] for r in rows]
        try:
            self.fold_combo.configure(values=names)
            self.fold_pick.set(names[0] if names else '')
        except Exception:
            pass

        txt.configure(state='disabled')

    def _fold_selected(self, expand):
        """展开/收起下拉框选中的那个角色。"""
        if not hasattr(self, '_fold_roles'):
            return
        role = self.fold_pick.get()
        for tag, rn in self._fold_roles.items():
            if rn == role:
                if self._fold_tags[tag] != expand:
                    self._toggle_fold(tag)
                # 展开后滚动到该角色
                if expand:
                    try:
                        self.out_text.see('foldhdr_%s' % tag.replace('det_', ''))
                    except Exception:
                        pass
                break

    def _toggle_fold(self, tag):
        """切换单个角色明细的折叠状态，并同步标题行箭头。"""
        if not hasattr(self, '_fold_tags') or tag not in self._fold_tags:
            return
        show = not self._fold_tags[tag]
        self._fold_tags[tag] = show
        try:
            self.out_text.tag_configure(tag, elide=not show)
        except Exception:
            return
        self._set_header_text(tag, self._fold_roles.get(tag, ''), show)

    def _set_header_text(self, tag, role, expanded):
        """把标题行的 ▶/▼ 和提示文字改掉。"""
        txt = self.out_text
        idx = tag.replace('det_', '')
        hdr = 'foldhdr_%s' % idx
        try:
            txt.configure(state='normal')
            ranges = txt.tag_ranges(hdr)
            if not ranges:
                return
            start, end = ranges[0], ranges[1]
            arrow = '▼' if expanded else '▶'
            hint = '（点这里收起）' if expanded else '（点这里展开）'
            line = txt.get(start, end)
            # 替换箭头和提示
            newline = re.sub(r'[▶▼]', arrow, line, count=1)
            newline = re.sub(r'（点这里[^）]*）', hint, newline)
            txt.delete(start, end)
            txt.insert(start, newline, (hdr,))
            txt.tag_bind(hdr, '<Button-1>', lambda e, t=tag: self._toggle_fold(t))
            txt.tag_configure(hdr, background='#eef4ff')
        except Exception:
            pass
        finally:
            txt.configure(state='disabled')

    def _toggle_fold_all(self, expand):
        """全部展开 / 全部收起。"""
        if not hasattr(self, '_fold_tags'):
            return
        for tag in list(self._fold_tags):
            if self._fold_tags[tag] != expand:
                self._toggle_fold(tag)

    @staticmethod
    def _extract_attrs(raw):
        """从原始输出里抓六维（Attribute Result 后第 6 行）。"""
        lines = raw.replace('\r', '\n').split('\n')
        try:
            i = next(k for k, l in enumerate(lines) if 'Attribute Result' in l)
        except StopIteration:
            return None
        for l in lines[i + 1:i + 15]:
            s = l.strip()
            if re.match(r'^(\d+\s+){5}\d+$', s):
                return [int(x) for x in s.split()[:6]]
        return None

    @staticmethod
    def _extract_avg(raw):
        m = re.search(r'Average Win Rate\s*:\s*([\d.]+)%', raw)
        if not m:
            return None
        try:
            return ('%.2f' % float(m.group(1))).rstrip('0').rstrip('.') + '%'
        except ValueError:
            return m.group(1) + '%'

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
                elif kind == 'job_start':
                    idx, total, role_cn = payload
                    # 换角色时清掉上一个的累积，避免混在一起
                    self._raw_output = ''
                    self._append_out('')
                    self._append_out('━' * 46)
                    self._append_out('  [%d/%d] %s' % (idx + 1, total, role_cn))
                    self._append_out('━' * 46)
                    self.status_var.set('(%d/%d) 正在计算 %s' % (idx + 1, total, role_cn))
                elif kind == 'job_done':
                    idx, total, role_cn, rc = payload
                    raw = getattr(self, '_raw_output', '')
                    if not hasattr(self, '_multi_header'):
                        self._multi_header = []
                    self._multi_header.append((role_cn, raw, rc))
                    self._append_out('  → %s 完成（退出码 %s）'
                                     % (role_cn, '成功' if rc == 0 else rc))
                elif kind == 'done':
                    self._flush_out()
                    self.run_btn.configure(state='normal')
                    self.stop_btn.configure(state='disabled')
                    self.progress['value'] = 100 if payload == 0 else self.progress['value']
                    self.progress['mode'] = 'determinate'

                    multi = getattr(self, '_multi_header', []) or []
                    if len(multi) > 1:
                        # 多角色：渲染汇总对比表
                        self._render_multi(multi)
                        self.status_var.set('完成 %d 个角色' % len(multi))
                    else:
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
        self.root.after(120, self._poll_queue)


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
