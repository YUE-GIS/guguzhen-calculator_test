#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多角色批量计算：把一份配置套到多个角色上，逐个跑。

用法（在 calc_gui 里）：勾选角色 → 点「▶ 开始计算」→ 依次计算并汇总。

设计要点：
  · 计算器一次只认一个玩家行，所以「多角色」= 生成 N 份配置，逐个跑
  · 角色行格式：<名> [G=成长] [M=模式] <等级> <强化> <技能位> <品质>
  · 只有 WU / XI / XIA 需要 G=，只有 YA 需要 M=
  · 换角色时其余部分（装备/光环/许愿）不变，只替换第一行玩家行
"""
import re

# 角色英文名 → 中文名
ROLE_NAMES = {
    'MO': '默', 'LIN': '琳', 'AI': '艾', 'MENG': '梦', 'WEI': '薇',
    'YI': '伊', 'MING': '冥', 'MIN': '命', 'WU': '舞', 'XI': '希',
    'XIA': '霞', 'YA': '雅', 'QI': '绮',
}

# 需要 G= 成长值的角色
NEED_GROWTH = {'WU', 'XI', 'XIA'}
# 需要 M= 模式的角色
NEED_MODE = {'YA'}

# 玩家行匹配：ROLE [G=n] [M=n] lvl kf skl quality
PLAYER_RE = re.compile(
    r'^(?P<role>[A-Z]{2,6})'
    r'(?:\s+G=(?P<growth>\d+))?'
    r'(?:\s+M=(?P<mode>\d+))?'
    r'\s+(?P<lvl>\d+)\s+(?P<kf>\d+)\s+(?P<skl>\d+)\s+(?P<q>\d+)\s*$'
)


def split_config(text):
    """把配置切成 (头部注释, 玩家行索引, 玩家行内容, 剩余部分)

    返回 None 表示找不到玩家行。
    """
    lines = text.split('\n')
    # 前两行是 auraMax / rankLevel，之后可能有空行
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith('//'):
            continue
        m = PLAYER_RE.match(s)
        if m:
            return {'lines': lines, 'idx': i, 'match': m, 'raw': s}
    return None


def parse_player(text):
    """解析当前配置里的玩家行，返回 dict 或 None。"""
    info = split_config(text)
    if not info:
        return None
    m = info['match']
    return {
        'role': m.group('role'),
        'growth': int(m.group('growth')) if m.group('growth') else 0,
        'mode': int(m.group('mode')) if m.group('mode') else 0,
        'lvl': int(m.group('lvl')),
        'kf': int(m.group('kf')),
        'skl': int(m.group('skl')),
        'q': int(m.group('q')),
    }


def build_player_line(role, lvl, kf, skl, q, growth=0, mode=0):
    """按角色需求拼出玩家行。"""
    parts = [role]
    if role in NEED_GROWTH and growth > 0:
        parts.append('G=%d' % growth)
    if role in NEED_MODE:
        parts.append('M=%d' % mode)
    parts.append('%d %d %d %d' % (lvl, kf, skl, q))
    return ' '.join(parts)


def swap_role(text, role, template=None):
    """把配置里的玩家行换成指定角色，其余不动。

    template: dict，含 lvl/kf/skl/q/growth/mode。None 则沿用原配置的值。
    """
    info = split_config(text)
    if not info:
        raise ValueError('配置里找不到玩家行（格式：ROLE [G=n] [M=n] lvl kf skl quality）')

    cur = parse_player(text)
    t = template or cur
    new_line = build_player_line(
        role,
        t.get('lvl', cur['lvl']),
        t.get('kf', cur['kf']),
        t.get('skl', cur['skl']),
        t.get('q', cur['q']),
        t.get('growth', cur['growth']),
        t.get('mode', cur['mode']),
    )
    lines = list(info['lines'])
    # 保留原行缩进
    old = lines[info['idx']]
    indent = old[:len(old) - len(old.lstrip())]
    lines[info['idx']] = indent + new_line
    return '\n'.join(lines)


def cn(role):
    """角色英文名 → 中文名。"""
    return ROLE_NAMES.get(role, role)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    text = open(sys.argv[1], encoding='utf-8').read()
    p = parse_player(text)
    print('当前角色:', p)
    print()
    for r in ROLE_NAMES:
        try:
            out = swap_role(text, r)
            line = [l for l in out.split('\n') if PLAYER_RE.match(l.strip())][0]
            print('  %-5s %-3s → %s' % (r, cn(r), line.strip()))
        except Exception as e:
            print('  %-5s ❌ %s' % (r, e))


# ── 加点重置（anpc/apc 搜索前必须做）──
# 实测：`anpc` 是「增量搜索」，从配置里的现有加点出发分配剩余点数
# （源码：attrLeft = totalAttr - sum(myself.attr)）。
# 如果配置里的加点已经填满（甚至超过 totalAttr），搜索空间几乎为 0，
# 会原样返回配置里的值 —— 表现为「所有角色加点都一样」。
# 所以要先把加点重置成起始值，让搜索有空间。

ATTR_LINE_RE = re.compile(r'^(\d+\s+){5}\d+\s*$')


def find_attr_line(text):
    """找六维那一行，返回 (行号, 原始行)。只在玩家行之后、装备行之前找。"""
    lines = text.split('\n')
    info = split_config(text)
    if not info:
        return None, None
    start = info['idx'] + 1
    for i in range(start, min(start + 10, len(lines))):
        s = lines[i].strip()
        if ATTR_LINE_RE.match(s):
            return i, lines[i]
    return None, None


def reset_attrs(text, value=1):
    """把六维重置成同一个起始值（默认全 1）。

    计算器会在此基础上分配剩余点数（totalAttr - 6）。
    返回 (新文本, 是否改过)。
    """
    idx, raw = find_attr_line(text)
    if idx is None:
        return text, False
    lines = text.split('\n')
    old = lines[idx]
    indent = old[:len(old) - len(old.lstrip())]
    vals = [str(value)] * 6
    lines[idx] = indent + ' '.join(vals)
    return '\n'.join(lines), True


def total_attr_points(text):
    """按计算器公式算可用总点数：(lvl + 2) * 3 * (100 + quality) / 100"""
    p = parse_player(text)
    if not p:
        return None
    return (p['lvl'] + 2) * 3 * (100 + p['q']) // 100


def current_attr_sum(text):
    """当前六维合计。"""
    idx, raw = find_attr_line(text)
    if idx is None:
        return None
    return sum(int(x) for x in raw.split()[:6])
