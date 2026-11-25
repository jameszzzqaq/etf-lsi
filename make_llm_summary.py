# -*- coding: utf-8 -*-
"""
从看板 HTML（etf_lsi_dashboard.html）内嵌的 const D 生成「给 AI 读」的文档。

产出（默认写到本脚本所在目录，可用 --out 指定）：
  llms.txt   —— 全站数据的人话快照，AI 抓取入口（llms.txt 约定名）
  data.json  —— const D 原样导出，机器可读全量数据

设计边界（改之前先读 README 附录「AI 可读文档」）：
  - 单一真相源 = 看板 HTML 里的 const D。不重新取数、不联网、不新增依赖、
    不读任何密钥、不改任何口径/阈值/单位，AI 读到的与页面看到的完全一致。
  - 降级不阻断：源缺失 / 解析失败只打印 ::warning:: 并以 0 退出，**绝不写空文件**，
    也绝不让主报告失败（daily_run.py 里登记为 fatal=False）。
  - 产物不提交 main：只由 daily.yml 复制进 public/ 发布到 gh-pages / Cloudflare Pages，
    避免在 main 留下一份永不更新的过期副本。
  - 新增/改名字段时同步改本文件的 META_CN 与各段生成器；缺字段一律显示「—」，不抛异常。

用法：
  python make_llm_summary.py                    # 生成 llms.txt / data.json
  python make_llm_summary.py --check            # 只打印 llms.txt，不写文件
  python make_llm_summary.py --out /tmp/xxx     # 写到指定目录
  python make_llm_summary.py --src /path/x.html # 指定源 HTML（本地验证用）
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CF = 'https://etf-lsi.pages.dev'
GH = 'https://wj624.github.io/etf-lsi'

# LSI core 字段中文名（仅用于展示，不参与计算；缺的键原样显示）
CORE_CN = {
    'lsi': 'LSI 内部压力', 'sofr': 'SOFR 担保隔夜融资利率',
    'rrp_rate': 'RRP 隔夜逆回购奖励利率', 'effr': 'EFFR 有效联邦基金利率',
    'iorb': 'IORB 准备金余额利率', 'us02y': '2 年期美债收益率',
    'us10y': '10 年期美债收益率', 'yield_spread': '10Y-2Y 期限利差',
    't10yie': '10Y 盈亏平衡通胀预期', 'dfii10': '10Y 实际利率 (TIPS)',
    'credit': '高收益债信用利差 (OAS)', 'credit_roc': '信用利差变化率',
    # v3 新增：CCC 级利差、CCC 相对溢价倍数与近 3 年 p10/p90 分位参考
    'ccc': 'CCC 级及以下信用利差 (bp)', 'rotten': 'CCC 相对溢价倍数 (CCC÷HY)',
    'credit_p10': '信用利差 3年 p10 (bp)', 'credit_p90': '信用利差 3年 p90 (bp)',
    'ccc_p10': 'CCC 利差 3年 p10 (bp)', 'ccc_p90': 'CCC 利差 3年 p90 (bp)',
    'rotten_p10': '相对溢价倍数 3年 p10', 'rotten_p90': '相对溢价倍数 3年 p90',
    'dfii10_p10': '实际利率 3年 p10 (bp)', 'dfii10_p90': '实际利率 3年 p90 (bp)',
    # v4 新增：4 张宏观卡的「近5年分位」小字读数（当前值在 1250 交易日中的 percentile rank）
    'credit_rank5y': '信用利差近5年分位 (%)', 'ccc_rank5y': 'CCC利差可用历史分位 (%)',
    'rotten_rank5y': 'CCC相对溢价倍数可用历史分位 (%)', 'dfii10_rank5y': '实际利率近5年分位 (%)',
    'lsi_rank5y': 'LSI压力指数近5年分位 (%)', 'gli_rank5y': '净流动性GLI近5年分位 (%)',
    'wrb_T_rank5y': '银行准备金近5年分位 (%)',
    # BAMLH0A3HYC 仅 2023-11-21 起约 3 年，CCC / rotten 用「可用历史分位」而非「近5年」。
    'cpi_yoy': '美国 CPI 同比', 'gli': '全球流动性 GLI',
    'walcl_T': '美联储总资产 WALCL', 'wtregen_T': 'TGA 财政部一般账户',
    'rrp_T': 'RRP 逆回购余额', 'rrp_pct': 'RRP 占峰值比例',
    'wresbal_T': '商业银行准备金', 'wrb_T': '准备金（另一口径）',
    'reserve_gdp': '准备金 / GDP', 'reserve_assets': '准备金 / 银行资产',
    'tlc': '总流动性周期环比', 'impulse': '流动性冲量',
    'dxy': '广义美元指数 DTWEXBGS', 'dxy_ma': '美元指数 50 日均线',
    'gli_high_th': 'GLI 高位阈值', 'gli_low_th': 'GLI 低位阈值',
}

RATIO_ORDER = ['style_cyb', 'style_star', 'bond']


def num(x, nd=2, dash='—'):
    if x is None:
        return dash
    try:
        return format(float(x), ',.%df' % nd)
    except (TypeError, ValueError):
        return str(x)


def pctv(x, nd=2, sign=False, dash='—'):
    """x 已经是百分数数值（如 92.31 表示 92.31%），只做格式化，不再乘 100。"""
    if x is None:
        return dash
    try:
        v = float(x)
        return (('%+.' + str(nd) + 'f%%') if sign else ('%.' + str(nd) + 'f%%')) % v
    except (TypeError, ValueError):
        return str(x)


def s(x, dash='—'):
    if x is None or x == '':
        return dash
    return str(x)


def yi(x, nd=2):
    """原始「份」/「元」转「亿份」/「亿元」。None 透传。"""
    if x is None:
        return None
    try:
        return float(x) / 1e8
    except (TypeError, ValueError):
        return None


def bps(x, sign=True):
    """bp 展示：sign=True 带正负号（变化量），False 不带（水平值）。"""
    if x is None:
        return '—'
    return (('%+.1f bp') if sign else ('%.1f bp')) % x


def kv_table(rows, head=('项目', '数值')):
    out = ['| %s | %s |' % head, '| :--- | :--- |']
    for a, b in rows:
        out.append('| %s | %s |' % (str(a).replace('|', '\\|'), str(b).replace('|', '\\|')))
    return out


def cn_liq_center(liq):
    """与看板 renderCnLiquidity() 内 winStat() 完全同算法：
       末 WIN=20 行内跳过 null，n>=3 才返回 (均值, min, max, n)。
       纯本地计算，不联网、不改口径 —— ⑩ 面板的 20 日中枢 / 波动区间是浏览器现算的，
       不落 const D，所以这里按同一算法自算一份，让 AI 也能读到这个派生值。
    """
    rows = [r for r in (liq.get('data') or []) if isinstance(r, dict) and r.get('date')]
    if not rows:
        return None
    WIN = 20
    vals = []
    for r in rows:
        v = r.get('dr001')
        vals.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
    i = len(rows) - 1
    ssum, n = 0.0, 0
    lo, hi = float('inf'), float('-inf')
    for j in range(max(0, i - WIN + 1), i + 1):
        x = vals[j]
        if x is None:
            continue
        ssum += x
        n += 1
        if x < lo:
            lo = x
        if x > hi:
            hi = x
    return (ssum / n, lo, hi, n) if n >= 3 else None


# ── ⑬ 国债曲线「自动结论」──────────────────────────────────
# 与看板 renderRates() 内 ratesConclusion() 同一套算法：四象限（牛熊 × 陡平）
# ＋ 扭转档（两端一上一下）。两边算法必须同步，否则页面上写「牛平」、
# llms.txt 里写「熊陡」，AI 抓到的就是两套结论。
# 要害：「走阔」只表示变陡、不带方向 —— 牛陡＝松、熊陡＝紧；
# 而 30Y−10Y 是期限溢价，两个方向都不等于宽松或收紧，故不并进结论。
SHAPE_STEPS = ((0, '倒挂'), (20, '极度平坦'), (50, '偏平'), (120, '正常陡峭'), (None, '陡峭'))


def rate_shape(v):
    if v is None:
        return '—'
    for th, name in SHAPE_STEPS:
        if th is None or v < th:
            return name
    return '陡峭'


# ⑬ 总评表：与看板 ratesConclusion() 内的 RATES_VERD **逐字一致**（9 档）。
# 刻意不给「利好 / 利空」单边结论 —— 曲线同时定价分母（无风险利率）与分子（增长通胀预期），
# 牛平正是两者反向的组合，压成一个词必然丢一半。理由与映射表见 README 4.6.1。
# 格式：象限 -> (短语, 怎么读, 什么会推翻它)
RATES_VERD = {
    '牛陡': ('折现率下行 · 钱松',
             '短端先跌、10Y 也跟着跌：降息 / 投放把资金价格压低，10Y 国债收益率（估值折现率）同步下行；钱更松、折现率更低，成长和长久期资产最直接受益。',
             '2Y 重新上行、利差转平，说明只是短期宽松脉冲。'),
    '牛平': ('折现率下行 · 盈利待确认',
             '长端跌得比短端快：10Y 国债收益率（估值折现率）被往下压，债券和高股息这类「类债资产」受益；但曲线变平更多来自增长和通胀预期下修，企业盈利还没确认，成长股不能只因为利率低就买。',
             '2Y 也补跌、曲线转成牛陡，才更像真正流动性转松；10Y 反弹而 2Y 不动，说明只是避险脉冲。'),
    '熊陡': ('折现率上行 · 靠盈利',
             '长端涨得更快：10Y 国债收益率（估值折现率）抬升，股票估值被压；如果企业盈利没有同步变好，就只是「利率贵了」的杀估值。',
             '长端回落、利差收窄，说明更像供给冲击而不是增长变强。'),
    '熊平': ('资金面收紧',
             '短端涨得更快：资金价格和政策利率预期在上移，钱比长端更紧；对高估值、长久期板块最不友好。',
             '长端接力上行转成熊陡，说明久期风险继续放大。'),
    '熊平扭转': ('钱紧 + 预期转弱',
               '短端上、长端下：一边钱更紧，一边增长 / 通胀预期更弱，是当前最差的组合。',
               '2Y 回落，钱紧缓解。'),
    '牛陡扭转': ('钱松 · 长端偏贵',
               '短端下、长端上：资金面确实松了，但长端在定价供给或通胀，长债并不便宜。',
               '长端继续上行，陡峭化就从「松」变成「贵」。'),
    '倒挂加深': ('不降息 + 衰退定价',
               '短端上行、长端下行：短端在定价「不降息」，长端在定价「衰退」，两边打架，方向最不明。',
               '短端开始下行，转向倒挂修复。'),
    '倒挂修复': ('降息预期回升',
               '短端下、长端上：短端开始计入降息，长端风险偏好修复，曲线往正常态回归。',
               '长端再度下行，回到倒挂加深。'),
    '基本走平': ('无方向信息',
               '两端 20 日变化都在噪声内，没有方向信息，这一项不该参与判断。',
               ''),
}


def rates_verdict(rates, m=20):
    """返回 ⑬ 面板的自动结论（dict）；数据不足返回 None，绝不抛异常。"""
    rows = [r for r in (rates.get('data') or []) if isinstance(r, dict) and r.get('date')]
    n = len(rows)
    lt = rates.get('latest') or {}
    if n <= m or not lt:
        return None

    def f(k, row):
        v = row.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v)

    def chg(k):
        a, b = f(k, rows[n - 1 - m]), f(k, rows[-1])
        return None if (a is None or b is None) else (b - a) * 100.0

    def rank(k, cur):
        if cur is None:
            return None
        vs = [v for v in (f(k, r) for r in rows) if v is not None]
        if len(vs) < 30:
            return None
        return sum(1 for v in vs if v <= cur) / float(len(vs) - 1) * 100.0

    sp = f('cn_10y_2y', rows[-1])
    s30 = f('cn_30y_10y', rows[-1])
    sp = None if sp is None else sp * 100.0
    s30 = None if s30 is None else s30 * 100.0
    d2, d10 = chg('cn_2y'), chg('cn_10y')
    d30 = chg('cn_30y_10y')
    if sp is None and d2 is None and d10 is None:
        return None
    inv = sp is not None and sp < 0

    quad, desc, lead = '—', '', ''
    if d2 is not None and d10 is not None:
        if abs(d2) < 1.5 and abs(d10) < 1.5:
            quad = '基本走平'
            desc = '两端 20 日变化都在 2bp 量级以内，形态在噪声范围内，不构成方向性信号。'
        elif d2 > 0 and d10 < 0:
            quad = '倒挂加深' if inv else '熊平扭转'
            desc = ('短端上行、长端下行，倒挂进一步加深 → 短端在定价「不降息」、长端在定价「衰退」，'
                    '市场内部矛盾加剧，是最难做的组合。') if inv else (
                   '短端上行、长端下行 → 资金面边际收紧叠加增长 / 通胀预期下修，'
                   '曲线向平坦甚至倒挂演变，对权益最不友好。')
        elif d2 < 0 and d10 > 0:
            quad = '倒挂修复' if inv else '牛陡扭转'
            desc = ('短端下行、长端上行，倒挂收窄 → 短端开始计入降息、长端风险偏好修复，'
                    '是压力缓解的方向。') if inv else (
                   '短端下行、长端上行 → 短端受益于流动性投放，长端在定价供给 / 通胀预期；'
                   '宽松是有的，但不便宜长债。')
        else:
            bull, steep = (d2 + d10) / 2 < 0, (d10 - d2) > 0
            quad = ('牛' if bull else '熊') + ('陡' if steep else '平')
            lead = ('，' + ('长端' if abs(d10) >= abs(d2) else '短端')
                    + '领' + ('跌' if (d10 < 0 or d2 < 0) else '涨'))
            if bull and steep:
                desc = '短端下行更快 → 降息 / 流动性投放直接驱动，是最「干净」的宽松信号，对成长与长久期资产最友好。'
            elif bull:
                desc = ('长端下行更快 → 宽松预期确实在升温，但更可能来自增长与通胀预期下修（避险 / 资产荒）：'
                        '钱是松的，盈利预期却在往下走。')
            elif steep:
                desc = '长端上行更快 → 增长或通胀预期回暖，也可能是超长债供给冲击；名义利率抬升压制估值。'
            else:
                desc = '短端上行更快 → 资金面边际收紧 / 政策边际转鹰，短端锚在上移，对高估值板块最不友好。'

    shape = rate_shape(sp)
    head = quad if (shape == '倒挂' and quad.startswith('倒挂')) \
        else ' · '.join([x for x in ('' if shape == '—' else shape, '' if quad == '—' else quad) if x])
    tv = RATES_VERD.get(quad, ('', '', ''))
    return {
        'verdict': tv[0], 'read': tv[1], 'watch': tv[2],
        'head': head, 'shape': shape, 'quad': quad, 'desc': desc, 'lead': lead,
        'sp': sp, 's30': s30, 'd2': d2, 'd10': d10, 'd30': d30, 'inv': inv,
        'p_sp': rank('cn_10y_2y', f('cn_10y_2y', rows[-1])),
        'p_10y': rank('cn_10y', f('cn_10y', rows[-1])),
        'n': n, 'y2': f('cn_2y', rows[-1]), 'y10': f('cn_10y', rows[-1]),
    }


def load_data(src):
    raw = io.open(src, encoding='utf-8').read()
    key = 'const D = '
    i = raw.find(key)
    if i < 0:
        raise ValueError('未找到 const D = 数据块')
    d, _ = json.JSONDecoder().raw_decode(raw[i + len(key):].lstrip())
    if not isinstance(d, dict) or 'tracks' not in d:
        raise ValueError('const D 结构异常（缺 tracks）')
    return d


def build(d):
    L = []
    A = L.append
    asof = s(d.get('asof'))
    gen = s(d.get('generated_at'))
    tracks = d.get('tracks') or {}
    order = d.get('order') or list(tracks.keys())
    lsi = d.get('lsi') or {}
    ratios = d.get('ratios') or {}
    sig = d.get('signals') or {}

    A('# 三合一全能监测系统（沪深300 / 创业板 / 红利低波 ETF + 宏观 LSI）· 数据快照')
    A('')
    A('> 看板数据生成时间：**%s**（北京时间）　最新行情日：**%s**' % (gen, asof))
    A('> 在线看板：%s/　｜　备用：%s/' % (CF, GH))
    A('> 机器可读全量数据：%s/data.json　｜　备用：%s/data.json' % (CF, GH))
    A('> 本文件：%s/llms.txt　｜　备用：%s/llms.txt' % (CF, GH))
    A('')
    A('## 这是什么')
    A('')
    A('一个仓库、一次流水线同时跑三个模块：**① 沪深300 / 创业板 / 红利低波 ETF 持仓背离监测（A股三大主力）**、'
      '**② 三大风格 / 股债比价**、**③ 美联储宏观流动性压力指数（LSI）与 20 项危机报警**；'
      '另附市场环境六件套、跨资产比价等增强卡片。')
    A('每工作日北京时间 21:17 自动跑一次，本页面是**快照**：打开网址不取数、不联网，'
      '所有数值都在生成时烤进 HTML，断网也能看。白天看到的是前一晚的快照，不是实时行情。')
    A('')
    A('## 如果你是接下这段文字的大模型')
    A('')
    A('1. **各模块数据日不同**：A 股三条主线、宏观 LSI、比价、市场环境各有自己的 `asof` / '
      '`date`，不要假设全部是同一天；本页表格里都标了日期。')
    A('2. **表里的数值就是准的**：不要自己重算，不要凭记忆补任何数字，不要拿网上历史文章的'
      '具体数字直接对比（口径与阈值是本项目单独设定的）。需要原始序列请读 `data.json`。')
    A('3. **单位**：LSI 相关多为 **bps**（1bp = 0.01 个百分点）；份额为**亿份**；'
      '市值为**亿元**；分位有的是 0~1 小数、有的是百分数，表头已注明；'
      'GLI / 准备金类为**万亿美元**。拿不准时以 `data.json` 的字段原值和 README 第四章 4.1 换算表为准。')
    A('4. **A 股配色是红涨绿跌**（净申购/正 Spread = 红，净赎回/负 = 绿），与美股相反。')
    A('5. **「—」表示本轮该字段缺失或模块降级隐藏**，不等于「数值为 0」或「没有这个指标」。'
      '任一取数模块失败只隐藏对应区块，不会拖垮整份报告。')
    A('6. 下结论时**同时引用证据与其数据日**，明确写出「不构成投资建议」；'
      '单一方向证据不足时请直接标注「证据不足」，不要下买卖结论。')
    A('7. **⑥-4 潜在危机判定**（看板新增图）的口径：|EFFR−T10YIE| 与 |US02Y−T10YIE| 均 < 50bp '
      '**且** 信用利差(OAS) > 400bp 时判为「潜在危机」，对应 `data.json` 的 `lsi.hist.crisis_signal` '
      '逐日布尔序列，与⑦报警表「潜在危机判定」项是**同一公式同一来源**，不要另算。'
      '该图 6 条线（信用利差 OAS / EFFR / US02Y / T10YIE / DFII10 实际利率 / 通胀 YoY）单位统一为 bp，'
      'CPI 同比也 ×100 换成 bp（6.9% → 690bp），通胀线数值高于利率线属正常口径、不是数据错误。'
      '窗口固定近 5 年（约 1300 交易日）；当前数据下该判定历史 0 天触发，背景带不出现是数据事实而非渲染故障。')
    A('')
    A('---')
    A('')
    A('## 一、模块一：A股三大主力 ETF 持仓背离')
    A('')
    A('核心逻辑：把头部 ETF 的市值 / 对应指数市值视作「持仓占比」，'
      '与指数点位做双轴对比。**指数跌而持仓占比升 = 资金逆势加仓（潜在护盘/吸筹）**；'
      '反之是资金撤离。`Spread` 是两条归一化曲线的差值，「底背离 / 顶背离」由此判定。')
    A('分母口径：沪深300 / 创业板用 westock 的**普通流通市值**；红利低波用中证指数官网的'
      '**指数自由流通市值**（`/api/csindex-feature` 的 `calMkvSum`）。两者不可混用，'
      '也不可拿不同口径的持仓占比直接横向比大小。')
    A('红利低波指数点位口径：中证官网日K（Worker `/api/csindex`，覆盖全 5 年）为权威源；'
      'westock `csH30269` 历史只到 2024-05-08，更早的 quote 会返回空，绝不可用沪深300点位兜底。'
      '本快照的数据已经权威源纠正，2024-05-08 的三倍跳变已消除。'
      '**以本快照（生产口径）为准**：本地预览脚本若用估算分母 37952.36 亿，其 Spread 读数与本快照不同，'
      '属临时口径差异，不代表生产数据。')
    A('')
    for k in order:
        t = tracks.get(k) or {}
        sm = t.get('summary') or {}
        _name = s(sm.get('name'), k)
        _title = s(sm.get('title'), '')
        # title 通常已含 name（如「沪深300（大盘蓝筹国家队）」），避免拼成重复括号
        _head = _title if (_title and (_name in _title or _title in _name)) else (
            '%s（%s）' % (_name, _title) if _title else _name)
        A('### %s' % _head)
        A('')
        A('- **判定**：%s（level=`%s`）' % (s(sm.get('status')), s(sm.get('level'))))
        A('- **依据**：%s' % s(sm.get('reason')))
        A('- **数据日**：%s（份额披露 asof %s，尾部未披露截断天数 %s）'
          % (s(sm.get('date')), s(sm.get('shares_asof')), s(sm.get('stale_tail'))))
        A('')
        rows = [
            ('持仓占比 `hold_pct`', pctv(sm.get('hold_pct'), 4)),
            ('指数收盘 `idx_close`', num(sm.get('idx_close'))),
            ('近 20 日指数涨跌 `idx_ret20`', pctv(sm.get('idx_ret20'), 2, True)),
            ('近 20 日持仓占比变化 `hold_ret20`', pctv(sm.get('hold_ret20'), 2, True)),
            ('近 5 日份额净申购 `net5`', num(sm.get('net5')) + ' 亿份'),
            ('近 1 日份额净申购 `net1`', num(sm.get('net1')) + ' 亿份'),
            ('Spread', num(sm.get('spread'))),
            ('ETF 总市值 `etf_mv`', num(yi(sm.get('etf_mv'))) + ' 亿元'),
            ('指数流通市值 `idx_mv`', num(yi(sm.get('idx_mv'))) + ' 亿元'),
            ('持仓占比历史分位 `hold_pct_pos`', pctv(sm.get('hold_pct_pos'))),
            ('持仓占比近5年分位 `hold_rank5y`', pctv(sm.get('hold_rank5y'))),
            ('指数点位历史分位 `idx_pct_pos`', pctv(sm.get('idx_pct_pos'))),
            ('持仓占比斜率 `hold_slope`', num(sm.get('hold_slope'), 4)),
            ('指数斜率 `idx_slope`', num(sm.get('idx_slope'), 4)),
            ('样本天数 / 区间', '%s 日，%s ~ %s' % (
                s(sm.get('n')), s((sm.get('span') or ['—', '—'])[0]),
                s((sm.get('span') or ['—', '—'])[-1]))),
        ]
        L.extend(kv_table(rows, ('指标', '当前读数')))
        A('')
        share_rows = sm.get('share_rows') or []
        names = t.get('names') or {}
        if share_rows:
            codes = list((share_rows[-1].get('shares') or {}).keys())
            A('**近 5 日 ETF 份额明细**（单位：亿份；括号内为当日净申购）')
            A('')
            A('| 日期 | ' + ' | '.join('%s %s' % (c, s(names.get(c), '')) for c in codes) + ' | 合计 |')
            A('| :--- | ' + ' | '.join(['---:'] * (len(codes) + 1)) + ' |')
            for r in share_rows:
                sh = r.get('shares') or {}
                ch = r.get('chg') or {}
                cells = []
                for c in codes:
                    v = num(yi(sh.get(c)))
                    cv = yi(ch.get(c))
                    cells.append('%s (%s)' % (v, ('+' + num(cv)) if (cv or 0) > 0 else num(cv)))
                A('| %s | %s | %s |' % (s(r.get('date')), ' | '.join(cells), num(yi(r.get('total_shares')))))
            A('')
        A('')

    A('---')
    A('')
    A('## 二、模块三：宏观 LSI 压力指数与 20 项报警')
    A('')
    if not lsi:
        A('> 本轮宏观模块降级，`lsi` 数据缺失，以下各段不适用。')
        A('')
    else:
        A('- **当前状态**：%s（level=`%s`）' % (s(lsi.get('state')), s(lsi.get('level'))))
        A('- **报警**：%s / %s 项触发' % (s(lsi.get('alert_count')), s(lsi.get('total'))))
        A('- **数据日**：%s' % s(lsi.get('asof')))
        A('- **核心公式**：`LSI (bps) = (SOFR − RRP利率) + (EFFR − IORB)`；'
          '阈值严格照搬移植源脚本，未做本地调整。')
        A('')
        core = lsi.get('core') or {}
        A('### 核心指标（`lsi.core` 全量）')
        A('')
        A('> 以下为引擎内部原始值，单位换算见 README 第四章 4.1：'
          '利率/利差/通胀类为 **bps**（FRED 原始 %，×100 换算，如 392 = 3.92%）；'
          'GLI / 美联储资产 / 准备金类为**万亿美元**；'
          '`rrp_pct`、`reserve_gdp`、`reserve_assets`、`credit_roc`、`tlc`、`impulse` 为百分数；'
          '`credit_rank5y` / `ccc_rank5y` / `rotten_rank5y` / `dfii10_rank5y` / `lsi_rank5y` / `gli_rank5y` '
          '是 percentile rank（0~100 百分数，不是 bps）：信用利差 / DFII10 / LSI / GLI / 准备金为近5年样本，'
          'CCC 与相对溢价倍数因 BAMLH0A3HYC 仅 2023-11-21 起改称「可用历史分位」；'
          '看板小字用的就是这些值，判色仍按固定阈值；'
          '`dxy` / `dxy_ma` 为指数点位。')
        A('')
        L.extend(kv_table([('%s `%s`' % (CORE_CN.get(k, k), k), num(v, 4) if isinstance(v, float) else s(v))
                           for k, v in core.items()], ('指标', '原始值')))
        A('')
        alerts = lsi.get('alerts') or []
        if alerts:
            A('### 20 项报警全表')
            A('')
            A('| # | 类别 | 报警项 | 公式 | 结果 | 阈值 | 触发 |')
            A('| ---: | :--- | :--- | :--- | :--- | :--- | :---: |')
            for a in alerts:
                A('| %s | %s | %s | %s | %s | %s | %s |' % (
                    s(a.get('no')), s(a.get('cat')), s(a.get('name')), s(a.get('formula')),
                    s(a.get('result')), s(a.get('threshold')),
                    '**是**' if a.get('alert') else '否'))
            A('')
        loose = lsi.get('loose') or []
        if loose:
            A('### 宽松状态判定')
            A('')
            A('| 状态 | 结果 | 阈值 | 触发 |')
            A('| :--- | :--- | :--- | :---: |')
            for a in loose:
                A('| %s | %s | %s | %s |' % (
                    s(a.get('name')), s(a.get('result')), s(a.get('threshold')),
                    '**是**' if a.get('on') else '否'))
            A('')
        flow = lsi.get('flow') or {}
        if flow:
            A('### 三渠道资金流（TGA / 外国逆回购 / 国内逆回购）')
            A('')
            for it in flow.get('items') or []:
                A('- %s：%s B（十亿美元）' % (s(it.get('name')), num(it.get('delta_b'))))
            A('')
            A('- 最大抽水项：%s %s B；最大放水项：%s %s B；三渠道合计净额 **%s B**（判定阈值 %s B）'
              % (s(flow.get('drain_name')), num(flow.get('drain_b')),
                 s(flow.get('inject_name')), num(flow.get('inject_b')),
                 num(flow.get('net_b')), num(flow.get('threshold'))))
            A('- 结论：%s' % s(flow.get('net_desc')))
            A('')
        diag = lsi.get('diag') or {}
        if diag:
            A('### 三层宏观诊断（引擎原文）')
            A('')
            for key, label in (('flow', '资金动态'), ('level1', '一阶 · 宏观周期与债务周期'),
                               ('level2', '二阶 · 流动性矩阵与底层水位'),
                               ('level3', '三阶 · 微观定价与风险溢价')):
                if diag.get(key):
                    A('**%s**：%s' % (label, s(diag.get(key))))
                    A('')
        hist = lsi.get('hist') or {}
        cal = lsi.get('cal') or []
        if hist:
            A('### 历史序列（`lsi.hist`，全量在 data.json）')
            A('')
            A('区间 %s ~ %s，共 %s 个观测点。各序列末端值：'
              % (s(cal[0] if cal else None), s(cal[-1] if cal else None), len(cal)))
            A('')
            L.extend(kv_table([('`%s`' % k, num(v[-1], 4) if isinstance(v, list) and v else '—')
                               for k, v in hist.items()], ('序列', '末端值')))
            A('')
        th = lsi.get('thresholds') or {}
        if th:
            A('### 阈值表（`lsi.thresholds` 全量）')
            A('')
            L.extend(kv_table([('`%s`' % k, num(v, 4) if isinstance(v, float) else s(v))
                               for k, v in th.items()], ('阈值键', '值')))
            A('')
        excl = lsi.get('excluded') or []
        if excl:
            A('### 明确剔除、不参与计算的项')
            A('')
            for e in excl:
                A('- %s' % s(e))
            A('')

    A('---')
    A('')
    A('## 三、风格 / 股债比价（`ratios`）')
    A('')
    items = ratios.get('items') or {}
    if not items:
        A('> 本轮比价模块降级，数据缺失。')
        A('')
    else:
        A('数据日 %s，周期 `%s`。' % (s(ratios.get('asof')), s(ratios.get('period'))))
        A('')
        keys = [k for k in (ratios.get('order') or RATIO_ORDER) if k in items] or list(items.keys())
        unit = '周' if s(ratios.get('period')) == 'week' else '日'
        for k in keys:
            r = items.get(k) or {}
            A('### %s' % s(r.get('title'), k))
            A('')
            A('- 公式：%s' % s(r.get('formula')))
            A('- 当前比值 **%s**，历史分位 **%s**，评级 `%s` / %s'
              % (num(r.get('cur'), 4), pctv(r.get('pct')), s(r.get('level')), s(r.get('word'))))
            A('- 判读：%s；建议：**%s**（%s）'
              % (s(r.get('label')), s(r.get('tilt')), s(r.get('advice'))))
            A('- 区间：min %s / 中位 %s / max %s；q20 %s / q80 %s；样本 %s %s（起 %s）'
              % (num(r.get('min'), 4), num(r.get('median'), 4), num(r.get('max'), 4),
                 num(r.get('q20'), 4), num(r.get('q80'), 4), s(r.get('n')), unit,
                 s(r.get('start'))))
            if r.get('pct_pub') is not None:
                A('- 发布后窗口分位 **%s**（%s 起 %s 周，剔除发布前回算段；主口径仍为全样本）'
                  % (pctv(r.get('pct_pub')), s(r.get('pub_from')), s(r.get('n_pub'))))
            A('- 变动：近 1 月 %s / 近 1 季 %s / 近 1 年 %s'
              % (pctv(r.get('chg_m'), 2, True), pctv(r.get('chg_q'), 2, True),
                 pctv(r.get('chg_y'), 2, True)))
            A('')

    A('---')
    A('')
    A('## 四、市场环境六件套与跨资产比价（`signals`）')
    A('')
    if not sig:
        A('> 本轮市场环境模块降级，`signals` 数据缺失。')
        A('')
    else:
        A('更新时间：%s。六项**各自独立容错**，任一失败只让对应面板消失。' % s(sig.get('updated')))
        A('')
        val = sig.get('valuation') or {}
        if val.get('data'):
            A('### ① 估值')
            A('')
            A('> `pe_pct` 是历史分位（0~1，越小越便宜）；'
              '`pe_over_history` 是其互补值，仅透传备查，**不要混用**。'
              '「分位区间」= 蛋卷 `begin_at` ~ `ts`，即该指数分位样本的真实起止日，'
              '不同指数起点不同（约 6~10 年），不是固定 10 年。')
            A('')
            A('| 指数 | 日期 | 分位区间 | PE(TTM) | PB | PE 分位 | PB 分位 | ROE | 股息率 | PEG | 评级 |')
            A('| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |')
            for v in val['data']:
                span = ('%s ~ %s' % (s(v.get('valuation_from')), s(v.get('date')))
                        if v.get('valuation_from') else s(v.get('date')))
                A('| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
                    s(v.get('name')), s(v.get('date')), span,
                    num(v.get('pe')), num(v.get('pb'), 4),
                    num(v.get('pe_pct'), 4), num(v.get('pb_pct'), 4),
                    pctv((v.get('roe') or 0) * 100), pctv((v.get('dividend_yield') or 0) * 100),
                    num(v.get('peg'), 4), s(v.get('level'))))
            A('')
        liq = sig.get('liquidity') or {}
        if liq.get('latest'):
            A('### ② 境内资金面（DR001 / SHIBOR 隔夜 O/N / FR001，单位 % 年化）')
            A('')
            A('> 派生量为 **bp**（1bp = 0.01 个百分点）。⑩ 面板口径自 **2026-11-10** 起：'
              '主线由 `DR007` **换锚为 `DR001`**（2026-11-02 后央行调控目标），DR007 退居辅助；'
              '灰虚线全名为「**SHIBOR 隔夜 O/N**」，不可简写成「隔夜」。'
              '　（注：本节标题的 `②` 是**本文档**内部的六件套编号，`⑩` 是**看板面板**编号，'
              '两套编号互不相干，不要互相换算。）')
            A('')
            A('> **20 日中枢 / 波动区间全自动**：`CENTER` = `dr001` 末 20 行有值点均值，'
              '`LO~HI` = 同窗口 min~max，随每日数据滚动；**不硬编码政策利率**，'
              '央行调息后看板零改动自动跟随。下表按页面 `winStat()` 同一算法自算，'
              '让 AI 也能读到这个「浏览器现算、不落数据文件」的派生值。')
            A('')
            st = cn_liq_center(liq)
            if st:
                c, lo, hi, n = st
                lt = liq.get('latest') or {}
                cur = lt.get('dr001')
                dev = ((float(cur) - c) * 100
                       if isinstance(cur, (int, float)) and not isinstance(cur, bool) else None)
                L.extend(kv_table([
                    ('20 日中枢 `CENTER`（%）', num(c, 4)),
                    ('同窗口波动区间 `LO~HI`（%）', '%s ~ %s' % (num(lo, 4), num(hi, 4))),
                    ('窗口内有值交易日 `n`', str(n)),
                    ('DR001 距中枢（bp）', ('%+.1f' % dev) if dev is not None else '—'),
                ], ('⑩ 面板派生项（本地自算）', '值')))
                A('')
            A('> **两个 bp 基准不同，禁止混读**：「距中枢」= `DR001 − CENTER`（纵向，今天 vs 近期常态）；'
              '「FR001 分层溢价」= `FR001 − DR001`（横向，全市场 vs 银行间）。'
              '副图蓝虚线 `±round(dm/2)` 是**自适应辅助刻度、不是告警阈值**，越过不触发任何动作；'
              '沪深300 归一紫线用于对照「利率松紧 vs 指数涨跌」，不可删。')
            A('')
            A('> DR001 历史仅约 1 个月（上游中国货币网回购定盘只回最近约 21 个交易日），'
              '图上以「DR001 数据起点」竖虚线标注，**不是数据缺失**。')
            A('')
            L.extend(kv_table([('`%s`' % k, num(v, 4) if isinstance(v, float) else s(v))
                               for k, v in (liq.get('latest') or {}).items()],
                              ('字段', '最新值')))
            A('')
            if liq.get('derived'):
                A('> `derived` 里 `dr007_ma20` / `dr007_vs_ma20_bp` 等仍以 DR007 为主，'
                  '是给 ⑩ 第 4 张 DR007 卡片用的，**不是「没跟着换锚」**。')
                A('')
                L.extend(kv_table([('`%s`' % k, num(v, 4) if isinstance(v, float) else s(v))
                                   for k, v in liq['derived'].items()], ('派生字段', '值 (bp)')))
                A('')
        prem = sig.get('premium') or {}
        if prem.get('data'):
            A('### ③ ETF 折溢价')
            A('')
            A('> 折溢价 = (市价 − 净值) / 净值 × 100；`stale=true` 表示价格与净值日期不同，此时只反映相对差异。')
            A('')
            A('| 代码 | 名称 | 市价 | 净值 | 折溢价 | 价格日 | 净值日 | 错日 |')
            A('| :--- | :--- | ---: | ---: | ---: | :--- | :--- | :---: |')
            for v in prem['data']:
                A('| %s | %s | %s | %s | %s | %s | %s | %s |' % (
                    s(v.get('code')), s(v.get('name')), num(v.get('price'), 4),
                    num(v.get('nav'), 4), pctv(v.get('premium_pct'), 4, True),
                    s(v.get('price_date')), s(v.get('nav_date')),
                    '是' if v.get('stale') else '否'))
            A('')
        br = sig.get('breadth') or {}
        if br.get('today'):
            t = br['today']
            A('### ④ 市场宽度（涨跌家数）')
            A('')
            L.extend(kv_table([
                ('日期', s(t.get('date'))), ('上涨 / 下跌 / 平盘', '%s / %s / %s' % (
                    s(t.get('up')), s(t.get('down')), s(t.get('flat')))),
                ('总数 / 上涨占比', '%s / %s' % (s(t.get('total')), pctv(t.get('up_pct')))),
                ('涨停 / 跌停 / 最高连板', '%s / %s / %s' % (
                    s(t.get('limit_up')), s(t.get('limit_down')), s(t.get('max_streak')))),
                ('连板分布', s(t.get('streak_dist'))),
                ('是否当日实时', '是' if t.get('is_today') else '否（上游忽略 date 参数）'),
            ], ('项目', '数值')))
            A('')
            if br.get('history'):
                A('近几日：' + '；'.join('%s 涨 %s / 跌 %s' % (
                    s(x.get('date')), s(x.get('up')), s(x.get('down')))
                    for x in br['history']))
                A('')
        rates = sig.get('rates') or {}
        if rates.get('latest'):
            A('### ⑤ 国债收益率与期限结构（%）')
            A('')
            A('> 利差字段为百分点（×100 即 bp）；派生量为 bp。')
            A('')
            vd = rates_verdict(rates)
            if vd:
                A('**自动结论：%s**' % vd['head'])
                A('')
                A('> 名词对照：下文「估值折现率」指 10Y 国债收益率（估值分母），'
                  '「盈利预期」指增长 / 通胀对应的企业盈利端（估值分子）。')
                A('')
                if vd['verdict']:
                    A('- **总评** **%s**　%s%s'
                      % (vd['verdict'], vd['read'],
                         ('　**待验证**：' + vd['watch']) if vd['watch'] else ''))
                if vd['sp'] is not None:
                    A('- **形态** 10Y−2Y %s → **%s**%s；2Y %s 贴政策利率，10Y %s%s'
                      % (bps(vd['sp'], False), vd['shape'],
                         '' if vd['p_sp'] is None else
                         '（近 %d 个交易日 %d%% 分位）' % (vd['n'], round(vd['p_sp'])),
                         '%s%%' % num(vd['y2'], 3), '%s%%' % num(vd['y10'], 3),
                         '' if vd['p_10y'] is None else
                         '（近一年 %d%% 分位）' % round(vd['p_10y'])))
                if vd['quad'] != '—':
                    A('- **方向（近 20 日）** 2Y %s、10Y %s → **%s**%s。%s'
                      % (bps(vd['d2']), bps(vd['d10']), vd['quad'], vd['lead'], vd['desc']))
                if vd['s30'] is not None:
                    A('- **超长端** 30Y−10Y %s%s → 这是**期限溢价**，反映超长债供给 / 久期需求 / 长期增长预期，'
                      '**两个方向都不等于宽松或收紧**。'
                      % (bps(vd['s30'], False),
                         '' if vd['d30'] is None else
                         '（近 20 日 %s，%s）' % (
                             bps(vd['d30']),
                             '溢价抬升' if vd['d30'] > 0 else ('溢价压缩' if vd['d30'] < 0 else '基本走平'))))
                if vd['inv']:
                    A('- **倒挂** 10Y < 2Y 已成立 → 市场把**强宽松**定价进了短端，这是「预期」而非「现实」，'
                      '历史上极少见且常与衰退定价同时出现，不能读成「越倒挂越宽松」。')
                A('')
                A('> 口径与看板 ⑬ 面板那段「自动结论」同算法：牛 / 熊看两端 20 日平均变化，陡 / 平看利差变化'
                  '（10Y−2Y 变大＝变陡），四象限互斥；两端变化均在 ±1.5bp 内判「基本走平」；'
                  '两端一上一下判「扭转」（熊平扭转 / 牛陡扭转 / 倒挂加深 / 倒挂修复）。'
                  '**「走阔」只表示变陡、不带方向** —— 牛陡＝松、熊陡＝紧，必须同时看两端谁在动。'
                  '全部由下方 `data` 现算，与页面同源，未新增取数。')
                A('')
            L.extend(kv_table([('`%s`' % k, num(v, 4) if isinstance(v, float) else s(v))
                               for k, v in rates['latest'].items()], ('字段', '最新值')))
            A('')
            if rates.get('derived'):
                L.extend(kv_table([('`%s`' % k, num(v, 4) if isinstance(v, float) else s(v))
                                   for k, v in rates['derived'].items()], ('派生字段', '值')))
                A('')
        mr = sig.get('macro_ratios') or {}
        if mr:
            A('### ⑥ 跨资产比价')
            A('')
            A('数据日 %s；黄金收盘 %s，KOSPI %s；数据源 `%s`。'
              % (s(mr.get('asof')), num(mr.get('gold_close')), num(mr.get('kospi')),
                 s(mr.get('source'))))
            A('')
            A('| 比价 | 当前值 | 近 20 日变动 | 250 日分位 | 250 日 min / max | 数据日 | 样本 |')
            A('| :--- | ---: | ---: | ---: | :--- | :--- | ---: |')
            for k in ('gold_silver', 'gold_oil', 'copper_gold', 'kospi_gold'):
                v = mr.get(k) or {}
                if not v:
                    continue
                A('| %s | %s | %s | %s | %s / %s | %s | %s |' % (
                    k, num(v.get('value'), 4), pctv(v.get('chg20_pct'), 2, True),
                    pctv(v.get('pct250')), num(v.get('min250'), 4), num(v.get('max250'), 4),
                    s(v.get('asof')), s(v.get('n'))))
            A('')
        if sig.get('errors'):
            A('### 本轮降级记录')
            A('')
            A('```')
            A(s(sig.get('errors')))
            A('```')
            A('')

    A('---')
    A('')
    A('## 数据滞后与降级提示')
    A('')
    A('- 各模块发布节奏不同：ETF 份额有 T+1~T+2 披露滞后，'
      '宏观 FRED 序列分日频/周频/月频/季频，比价为周频。'
      '**「—」或整段缺失 = 本轮该模块降级**，不是数值为 0。')
    A('- 打开看板**不会重新计算**，内容每天 21:17 更新一次；'
      '想立刻刷新请到仓库 Actions 手动 Run workflow。')
    A('- 已明确剔除、不参与计算的 TradingView 私有数据项见上文「明确剔除」段。')
    A('')
    A('## 数据来源')
    A('')
    A('| 数据 | 来源 |')
    A('| :--- | :--- |')
    A('| 宏观 LSI 20+ 项序列 | 美联储 FRED 官方 API（JSON 挂自动降级 CSV），需 `FRED_API_KEY` |')
    A('| 上交所 ETF 份额 / 净值 / 交易日历 | 官方披露，经自建 `a-stock-api` Worker `/api/etf-shares`、`/api/fund-nav`、`/api/trade-calendar` |')
    A('| 深交所 ETF 份额 | 深交所官方 xlsx 直连解析 |')
    A('| ETF 单位净值兜底 | 天天基金 pingzhongdata（东财） |')
    A('| 市场环境六件套 / 跨资产比价 | 自建 `a-stock-api` Worker 各端点 |')
    A('| 滞后份额快照 | 腾讯自选股 Node CLI（`westock-data/`） |')
    A('')
    A('> 两条链路完全独立：宏观走 FRED（境外），A 股走自建 Worker（境内），互不影响。')
    A('')
    A('## 名词速查')
    A('')
    A('| 名词 | 含义 |')
    A('| :--- | :--- |')
    A('| LSI | 流动性压力指数，`(SOFR−RRP)+(EFFR−IORB)`，单位 bps，值越大美元体系内部管道越紧 |')
    A('| GLI | 全球流动性指标（美联储净流动性口径），万亿美元 |')
    A('| RRP | 隔夜逆回购余额，常被视作美元体系的「缓冲垫」，枯竭后财政抽水会直接消耗准备金 |')
    A('| SOFR / EFFR / IORB | 担保隔夜融资利率 / 有效联邦基金利率 / 准备金余额利率 |')
    A('| DFII10 / T10YIE | 10 年期实际利率 / 10 年期盈亏平衡通胀预期 |')
    A('| DTWEXBGS | 广义美元指数，上穿 50 日均线视为「强美元潮汐」 |')
    A('| 持仓占比 | 头部 ETF 市值 ÷ 对应指数流通市值，本项目自建口径 |')
    A('| Spread | 持仓占比与指数两条归一化曲线的差值，用于识别背离 |')
    A('| 分位 | 当前值在历史样本中的排名位置；90% 表示只有 10% 的时间更极端 |')
    A('| 折溢价 | (市价−净值)/净值×100，正值溢价、负值折价 |')
    A('')
    A('---')
    A('')
    A('> 本文件由 `make_llm_summary.py` 从看板 HTML 内嵌 `const D` 自动生成，每日随看板一起发布；'
      '**不要手改**。全量原始序列见同目录 `data.json`。内容仅供研究参考，**不构成投资建议**。')
    A('')
    return '\n'.join(L)


def main():
    args = sys.argv[1:]

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default

    check = '--check' in args
    src = opt('--src')
    out = opt('--out', HERE)

    if src is None:
        # 线上由 daily_run.py 以 cwd=BASE 调用，HERE 与 cwd 一致；
        # 这里仍按「HERE 优先、cwd 兜底」找，兼容在别处手动运行脚本的场景。
        for base in (HERE, os.getcwd()):
            for cand in ('etf_lsi_dashboard.html', 'index.html'):
                p = os.path.join(base, cand)
                if os.path.exists(p):
                    src = p
                    break
            if src:
                break
    if not src or not os.path.exists(src):
        print('::warning::[make_llm_summary] 未找到看板 HTML（etf_lsi_dashboard.html / index.html），'
              '跳过 AI 文档生成；不写空文件，不影响主报告。')
        return 0
    try:
        d = load_data(src)
    except Exception as e:
        print('::warning::[make_llm_summary] 解析 const D 失败（%s: %s），'
              '跳过 AI 文档生成；不写空文件，不影响主报告。' % (type(e).__name__, e))
        return 0

    try:
        md = build(d)
    except Exception as e:
        print('::warning::[make_llm_summary] 生成 llms.txt 失败（%s: %s），'
              '跳过 AI 文档生成；不写空文件，不影响主报告。' % (type(e).__name__, e))
        return 0

    if check:
        print(md)
        print('\n[--check] llms.txt %d bytes, data.json 将含 %d 个顶层键；未写文件。'
              % (len(md.encode('utf-8')), len(d)))
        return 0

    try:
        if not os.path.isdir(out):
            os.makedirs(out)
        with io.open(os.path.join(out, 'llms.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(md)
        with io.open(os.path.join(out, 'data.json'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(d, ensure_ascii=False, separators=(',', ':')))
    except Exception as e:
        print('::warning::[make_llm_summary] 写文件失败（%s: %s）；不影响主报告。'
              % (type(e).__name__, e))
        return 0

    print('llms.txt   %8d bytes' % len(md.encode('utf-8')))
    print('data.json  %8d bytes (顶层键: %s)' % (
        os.path.getsize(os.path.join(out, 'data.json')), ', '.join(d.keys())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
