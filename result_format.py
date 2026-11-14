#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把计算器原始输出格式化成中文可读报告。

计算器典型输出（Attribute Result 段）：
    Attribute Result:
    MENG 801 206 5 9                 ← 角色 等级 kfLvl 技能位 品质
    WISH 13 28 20 21 25 ...          ← 14 个许愿值
    AMULET AGI 3 INT 3 VIT 4 SPR 3 ENDAMULET
    2 553 625 305 840 300            ← 六维（力 敏 智 体 精 意）
    BLADE 98 50 107 108 79 0         ← 装备 ×4
    ...
    5 SHI XUE WU ZHI JU              ← 光环数量 + 名字
      0: SHI Lv.220 : Win Rate : 55.61122% (...)
    Average Win Rate : 55.61122%
"""

import re

# ── 名字映射 ──
ROLE_CN = {
    'MO': '默', 'LIN': '琳', 'AI': '艾', 'MENG': '梦', 'WEI': '薇',
    'YI': '伊', 'MING': '冥', 'MIN': '命', 'WU': '舞', 'XI': '希',
    'XIA': '霞', 'YA': '雅', 'QI': '绮',
}

NPC_CN = {
    'MU': '蘑菇', 'ZHU': '竹', 'DENG': '灯', 'SHOU': '兽',
    'MU2': '蘑菇Ⅱ', 'ZHU2': '竹Ⅱ', 'DENG2': '灯Ⅱ', 'SHOU2': '兽Ⅱ',
    'YU2': '鱼Ⅱ', 'HAO2': '蚝Ⅱ', 'LIU': '六边形战士', 'SHI': '史莱姆',
}

ATTR_CN = ['力量', '敏捷', '智力', '体魄', '精神', '意志']

GEAR_CN = {
    'SWORD': '探险者之剑', 'BOW': '探险者短弓', 'STAFF': '探险者短杖',
    'BLADE': '狂信者的荣誉之刃', 'ASSBOW': '反叛者的刺杀弓', 'DAGGER': '幽梦匕首',
    'WAND': '光辉法杖', 'SHIELD': '荆棘盾剑', 'CLAYMORE': '陨铁重剑',
    'SPEAR': '饮血魔剑', 'COLORFUL': '彩金长剑', 'LIMPIDWAND': '清澄长杖',
    'GLOVES': '探险者手环', 'BRACELET': '命师的传承手环', 'VULTURE': '秃鹫手环',
    'RING': '海星戒指', 'DEVOUR': '噬魔戒指', 'REFRACT': '折光戒指',
    'PLATE': '探险者铁甲', 'LEATHER': '探险者皮甲', 'CLOTH': '探险者布甲',
    'CLOAK': '旅法师的灵光袍', 'THORN': '战线支撑者的荆棘重甲', 'WOOD': '复苏战衣',
    'CAPE': '挑战斗篷', 'SCARF': '探险者耳环', 'TIARA': '占星师的耳饰',
    'RIBBON': '萌爪耳钉', 'HUNT': '猎魔耳环', 'FIERCE': '凶神耳环',
    'NONE': '（空）',
}

AURA_CN = {
    'SHI': '启程之誓', 'XIN': '启程之心',
    'BI': '破壁之心', 'MO': '破魔之心', 'DUN': '复合护盾',
    'XUE': '鲜血渴望', 'XIAO': '恶意抽奖', 'SHENG': '圣盾祝福', 'E': '恶意抽奖',
    'SHANG': '伤口恶化', 'SHEN': '精神创伤', 'CI': '铁甲尖刺',
    'REN': '忍无可忍', 'RE': '热血战魂', 'DIAN': '点到为止',
    'WU': '午时已到', 'ZHI': '纸薄命硬', 'SHAN': '不动如山',
    'FEI': '沸血之志', 'BO': '波澜不惊', 'JU': '飓风之力',
    'HONG': '红蓝双刺', 'JUE': '荧光护盾', 'HOU': '后发制人',
    'DUNH': '钝化锋芒', 'ZI': '自信回头',
    'ZOU': '致命节奏', 'PIAO': '往返车票', 'PEN': '天降花盆',
    'DI': '绝对底线',
}

WISH_CN = [
    '物理攻击%', '魔法攻击%', '攻击速度%', '生命偷取%', '伤害反弹%',
    '最大生命%', '最大护盾%', '附加物穿', '附加魔穿', '附加生命',
    '附加护盾', '暴击几率', '技能几率', '附加绝对攻击',
]

AMUL_CN = {
    'STR': '力量', 'AGI': '敏捷', 'INT': '智力', 'VIT': '体魄',
    'SPR': '精神', 'MND': '意志',
    'PATK': '物理攻击%', 'MATK': '魔法攻击%', 'SPD': '速度%', 'REC': '回复%',
    'HP': '最大生命%', 'SLD': '最大护盾%', 'LCH': '生命偷取%', 'RFL': '伤害反弹%',
    'CRT': '暴击几率', 'SKL': '技能几率', 'PDEF': '物防', 'MDEF': '魔防',
    'AAA': '附加绝对攻击', 'CRTR': '暴击抵抗%', 'SKLR': '技能抵抗%',
}


# ── 运行参数的中文名 ──
PARAM_CN = [
    (r'Number of threads for calculation:\s*(\S+)', '计算线程数', lambda v: v.replace('(auto)', '（自动）')),
    (r'Number of tests for win rate calculation:\s*(\S+)', '单组测试次数', lambda v: v),
    (r'Start level for al calculation:\s*(\S+)', 'al 起始等级', lambda v: v),
    (r'Reduce rate of skill/critical:\s*(\S+)', '技能/暴击衰减比', lambda v: v),
    (r'PC opponent weight:\s*(\S+)', 'PC 对手权重', lambda v: v),
    (r'Attribute seed max:\s*(\S+)', '最大种子数', lambda v: v),
    (r'Defender mode:\s*(\S+)', '防守方模式',
     lambda v: {'OFF': '关闭', 'ON': '开启'}.get(v, v)),
    (r'Verbose mode:\s*(\S+)', '输出进度',
     lambda v: {'OFF': '关闭（0）', 'ON': '开启（1）'}.get(v, v)),
]

# 角色面板属性（showState 输出）
STATE_CN = [
    ('Role', '角色'), ('Level', '等级'),
    ('Attack Level', '进攻等级'), ('Defence Level', '防御等级'),
    ('Max HP', '最大生命'), ('HP Recover', '生命回复'),
    ('Physical Attack', '物理攻击'), ('Magical Attack', '魔法攻击'),
    ('Absolute Attack', '绝对攻击'), ('Attack Speed', '攻击速度'),
    ('Physical Breach', '物理穿透'), ('Magical Breach', '魔法穿透'),
    ('Skill Rate', '技能释放率'), ('Critical Rate', '暴击率'),
    ('Critical Breach', '暴击穿透'), ('Life Steal', '生命偷取'),
    ('Physical Defence', '物理防御'), ('Magical Defence', '魔法防御'),
    ('Physical Damage Reduce', '物理减伤'), ('Magical Damage Reduce', '魔法减伤'),
    ('Max Shield', '最大护盾'), ('Shield Recover', '护盾回复'),
    ('Damage Reflection', '伤害反弹'), ('Critical Defence', '暴击抵抗'),
    ('Skill Defence', '技能抵抗'),
    ('Aura Skill Set', '已选光环'), ('Aura Filter', '排除光环'),
    ('Wish Points', '许愿点数'), ('Amulet Points', '护符点数'),
    ('Version', '计算器版本'),
]


def format_params(raw_text):
    """把参数回显翻译成中文。只取最后一次出现的值（前面是历史回显）。"""
    text = raw_text.replace('\r', '\n')
    found = {}
    for pat, cn, conv in PARAM_CN:
        for m in re.finditer(pat, text):
            found[cn] = conv(m.group(1))
    if not found:
        return None
    out = ['', '【运行参数】']
    # 按 PARAM_CN 的顺序输出，保持稳定
    for pat, cn, conv in PARAM_CN:
        if cn in found:
            out.append('  %s %s' % (_pad(cn, 16), found[cn]))
    return out


def format_state(raw_text):
    """把角色面板属性（Role/Level/Max HP...）翻译成中文。"""
    text = raw_text.replace('\r', '\n')
    vals = {}
    for en, cn in STATE_CN:
        m = re.search(r'^' + re.escape(en) + r'\s*:\s*(.*)$', text, re.M)
        if m:
            v = m.group(1).strip()
            if v and v != '0' or en in ('Role', 'Level', 'Version'):
                vals[cn] = v
    if not vals:
        return None

    out = ['【角色属性】']
    # 角色名转中文
    if '角色' in vals:
        raw = vals['角色']
        vals['角色'] = ROLE_CN.get(raw, raw)
    order1 = ['角色', '等级', '进攻等级', '防御等级', '计算器版本']
    order2 = ['最大生命', '生命回复', '最大护盾', '护盾回复']
    order3 = ['物理攻击', '魔法攻击', '绝对攻击', '攻击速度']
    order4 = ['物理穿透', '魔法穿透', '暴击率', '暴击穿透',
              '技能释放率', '生命偷取', '伤害反弹']
    order5 = ['物理防御', '魔法防御', '物理减伤', '魔法减伤',
              '暴击抵抗', '技能抵抗']
    for grp in (order1, order2, order3, order4, order5):
        items = [(k, vals[k]) for k in grp if k in vals]
        if not items:
            continue
        out.append('')
        for k, v in items:
            out.append('  %s %s' % (_pad(k, 12), v))
    return out




def _disp_width(s):
    """字符串在等宽字体下的显示宽度（中文/全角算 2）"""
    w = 0
    for ch in s:
        o = ord(ch)
        w += 2 if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or
                   0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or
                   0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60 or
                   0xFFE0 <= o <= 0xFFE6 or 0x20000 <= o <= 0x3FFFD) else 1
    return w


def _pad(s, width):
    """按显示宽度右补空格"""
    n = width - _disp_width(s)
    return s + ' ' * max(0, n)

def _gear_line(s):
    """'BLADE 98 50 107 108 79 0' → 可读描述"""
    parts = s.split()
    if len(parts) < 7:
        return s
    t = parts[0]
    lvl = parts[1]
    pcts = parts[2:6]
    myst = parts[6] == '1'
    cn = GEAR_CN.get(t, t)
    if t == 'NONE':
        return '  · （未装备）'
    line = '  · %s  Lv.%s' % (cn, lvl)
    if myst:
        line += '  [神秘]'
    line += '\n      词条品质：%s%%' % ' / '.join(pcts)
    return line


def format_battle_line(line):
    """'  0: SHI Lv.220 : Win Rate : 55.61% (555/998 D=2(0.2%))' → 中文"""
    m = re.match(r'\s*(\d+):\s*(\S+)\s+Lv\.(\d+)\s*:\s*'
                 r'Win Rate\s*:\s*([\d.]+)%\s*'
                 r'\((?:(\d+)/(\d+))?[^)]*D=(\d+)\(([\d.]+)%\)\)', line)
    if not m:
        return None
    idx, npc, lvl, rate, win, total, draw, drawpct = m.groups()
    name = NPC_CN.get(npc, npc)
    try:
        rv = float(rate)
        rs = ('%.2f' % rv).rstrip('0').rstrip('.')
    except ValueError:
        rs = rate
    out = '  %s Lv.%s    胜率 %s%%' % (name, lvl, rs)
    if win and total:
        out += '  （胜 %s / 共 %s' % (win, total)
        if draw and draw != '0':
            out += '，平 %s' % draw
        out += '）'
    return out


def format_result(raw_text):
    """把整段原始输出转成中文报告。返回 (格式化文本, 是否识别到结果)"""
    text = raw_text.replace('\r', '\n')
    lines = [l.rstrip() for l in text.split('\n')]

    # 找 Attribute Result
    try:
        start = next(i for i, l in enumerate(lines) if 'Attribute Result' in l)
    except StopIteration:
        # 没有搜索结果时（比如 bnpc/bpc），至少把参数和角色属性格式化出来
        parts = []
        st = format_state(raw_text)
        if st:
            parts.extend(st)
        pr = format_params(raw_text)
        if pr:
            parts.extend(pr)
        # 战斗结果也可能有
        bl = [format_battle_line(l) for l in lines]
        bl = [b for b in bl if b]
        if bl:
            parts.append('')
            parts.append('【战斗结果】')
            parts.extend(bl)
            m = re.search(r'Average Win Rate\s*:\s*([\d.]+)%', raw_text)
            if m:
                try:
                    av = ('%.2f' % float(m.group(1))).rstrip('0').rstrip('.')
                except ValueError:
                    av = m.group(1)
                parts.append('')
                parts.append('  平均胜率  %s%%' % av)
        if parts:
            return '\n'.join(parts), True
        return None, False

    out = []
    out.append('=' * 56)
    out.append('属性搜索结果')
    out.append('=' * 56)

    # 解析紧随其后的数据块
    blk = [l.strip() for l in lines[start + 1:start + 20] if l.strip()]
    i = 0
    player = None
    wish = amulet = attrs = None
    gears = []
    auras = []

    while i < len(blk):
        s = blk[i]
        # 玩家行: ROLE lvl kfLvl sklSlot quality
        m = re.match(r'^([A-Z]+)(?:\s+G=(\d+))?(?:\s+M=(\d+))?\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$', s)
        if m and m.group(1) in ROLE_CN and player is None:
            player = {'role': m.group(1), 'lvl': m.group(4),
                      'kf': m.group(5), 'skl': m.group(6), 'q': m.group(7)}
            if m.group(2):
                player['growth'] = m.group(2)
            i += 1
            continue
        if s.startswith('WISH'):
            wish = s[4:].strip()
            i += 1
            continue
        if s.startswith('AMULET'):
            amulet = s.replace('AMULET', '').replace('ENDAMULET', '').strip()
            i += 1
            continue
        # 六维：6 个数字
        if re.match(r'^(\d+\s+){5}\d+$', s) and attrs is None:
            attrs = s.split()
            i += 1
            continue
        # 装备
        if re.match(r'^[A-Z]+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+$', s):
            gears.append(s)
            i += 1
            continue
        # 光环: "5 SHI XUE WU ZHI JU"
        m = re.match(r'^(\d+)((?:\s+[A-Z]+)+)$', s)
        if m:
            auras = m.group(2).split()
            i += 1
            continue
        # 战斗行
        if re.match(r'^\s*\d+:', s):
            break
        i += 1

    # ── 角色 ──
    if player:
        out.append('')
        out.append('【角色】')
        cn = ROLE_CN.get(player['role'], player['role'])
        line = '  %s  Lv.%s' % (cn, player['lvl'])
        line += '   强化等级 %s' % player['kf']
        line += '   技能位 %s' % player['skl']
        line += '   品质 %s%%' % player['q']
        if player.get('growth'):
            line += '   成长值 %s' % player['growth']
        out.append(line)

    # ── 加点 ──
    if attrs and len(attrs) >= 6:
        out.append('')
        out.append('【加点】')
        total = sum(int(x) for x in attrs[:6])
        for k, v in zip(ATTR_CN, attrs[:6]):
            out.append('  %s  %s' % (k, v))
        out.append('  ──────────────')
        out.append('  合计  %d' % total)

    # ── 装备 ──
    if gears:
        out.append('')
        out.append('【装备】')
        for g in gears:
            out.append(_gear_line(g))

    # ── 光环 ──
    if auras:
        out.append('')
        out.append('【光环】%d 个' % len(auras))
        names = [AURA_CN.get(a, a) for a in auras]
        # 每行 3 个
        for j in range(0, len(names), 3):
            out.append('  ' + ' / '.join(names[j:j + 3]))

    # ── 许愿 / 护符 ──
    if wish or amulet:
        out.append('')
        out.append('【其它】')
        if wish:
            vals = wish.split()
            items = []
            for idx, v in enumerate(vals):
                if v and v != '0' and idx < len(WISH_CN):
                    items.append('%s %s' % (WISH_CN[idx], v))
            if items:
                out.append('  许愿：')
                for j in range(0, len(items), 3):
                    out.append('    ' + ' / '.join(items[j:j + 3]))
            else:
                out.append('  许愿：全部为 0')
        if amulet:
            parts = []
            toks = amulet.split()
            k = 0
            while k + 1 < len(toks):
                key, val = toks[k], toks[k + 1]
                parts.append('%s +%s' % (AMUL_CN.get(key, key), val))
                k += 2
            if parts:
                out.append('  护符：' + '，'.join(parts))

    # ── 运行参数 ──
    params = format_params(raw_text)
    if params:
        out.extend(params)

    # ── 战斗结果 ──
    battles = []
    avg = None
    for l in lines[start:]:
        b = format_battle_line(l)
        if b:
            battles.append(b)
        m = re.search(r'Average Win Rate\s*:\s*([\d.]+)%', l)
        if m:
            avg = m.group(1)

    if battles:
        out.append('')
        out.append('【战斗结果】')
        out.extend(battles)
        if avg:
            out.append('')
            try:
                av = ('%.2f' % float(avg)).rstrip('0').rstrip('.')
            except ValueError:
                av = avg
            out.append('  平均胜率  %s%%' % av)

    return '\n'.join(out), True


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        raw = open(sys.argv[1], 'rb').read().decode('utf-8', 'replace')
        s, ok = format_result(raw)
        print(s if ok else '（没找到 Attribute Result）')
    else:
        print(__doc__)
