"""界面皮肤：配色令牌 + 全局 CSS。

设计说明见 docs/design/README.md（静态稿）。
白天 / 夜晚两套配色由侧边栏底部的「🌙 夜晚 / ☀️ 白天」开关切换 —— 纯 CSS 联动，
主题选择挂在 .stApp:has(侧边栏 checkbox input:checked) 上，因此不弹窗、不闪白；
选择同时写入 session_state["skin"]，脚本重跑后按钮与配色保持一致。

模块只含常量与纯函数，无 Streamlit 调用，可离线单测。
"""
from __future__ import annotations

DARK = {
    "bg": "#0A101E", "card": "#111A2E", "card-2": "#16213B",
    "line": "rgba(120,200,255,.12)", "line-2": "rgba(120,200,255,.22)",
    "txt": "#E8ECF6", "txt-2": "#C3CCE0", "txt-3": "#8C99B4",
    "accent": "#22D3EE", "accent-ink": "#04121A", "accent-soft": "rgba(34,211,238,.14)",
    "up": "#FF4D6D", "down": "#00C896", "violet": "#7C5CFF", "amber": "#FFB020",
    "danger": "#FF6B7D", "danger-soft": "rgba(255,107,125,.12)",
    "amber-ink": "#F3D9A8", "rail": "#080D18", "rail-line": "rgba(120,200,255,.10)",
    "grid": "rgba(120,200,255,.055)", "glow": "rgba(34,211,238,.10)",
    "glow2": "rgba(124,92,255,.10)", "shadow": "0 8px 26px rgba(0,0,0,.28)",
}

LIGHT = {
    "bg": "#F4F7FC", "card": "#FFFFFF", "card-2": "#EEF3FA",
    "line": "rgba(23,52,94,.10)", "line-2": "rgba(23,52,94,.20)",
    "txt": "#16233D", "txt-2": "#3D4D6B", "txt-3": "#6B7A96",
    "accent": "#0E7490", "accent-ink": "#FFFFFF", "accent-soft": "rgba(14,116,144,.10)",
    "up": "#D92D48", "down": "#0F9D63", "violet": "#5B4BE0", "amber": "#C27803",
    "danger": "#C62A3C", "danger-soft": "rgba(198,42,60,.09)",
    "amber-ink": "#7A4E02", "rail": "#FFFFFF", "rail-line": "rgba(23,52,94,.09)",
    "grid": "rgba(23,52,94,.05)", "glow": "rgba(14,116,144,.07)",
    "glow2": "rgba(91,75,224,.06)", "shadow": "0 6px 20px rgba(23,52,94,.08)",
}

CHART = {
    "dark": {"up": "#FF4D6D", "down": "#00C896", "bi": "#93A3C2", "zs": "#7C5CFF",
             "buy": "#22D3EE", "sell": "#FFB020", "grid": "rgba(120,200,255,.075)",
             "axis": "#8492AE", "line": "rgba(120,200,255,.16)", "ring": "#0A101E"},
    "light": {"up": "#D92D48", "down": "#0F9D63", "bi": "#8593AB", "zs": "#5B4BE0",
              "buy": "#0E7490", "sell": "#C27803", "grid": "rgba(23,52,94,.10)",
              "axis": "#6B7A96", "line": "rgba(23,52,94,.12)", "ring": "#FFFFFF"},
}

MONO = '"Source Code Pro",ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace'
SANS = '"Microsoft YaHei","Noto Sans SC","Source Sans",system-ui,-apple-system,sans-serif'

TOGGLE = '[data-testid="stSidebar"] .st-key-chx_skin [data-testid="stCheckbox"] input'



LIGHT_EXTRA = '''
.stApp:has(.st-key-chx_skin [data-testid="stCheckbox"] input:checked) [data-testid="stAppViewContainer"]{color-scheme:light}
.stApp:has(.st-key-chx_skin [data-testid="stCheckbox"] input:checked) [data-testid="stMainMenuPopover"],
.stApp:has(.st-key-chx_skin [data-testid="stCheckbox"] input:checked) [data-baseweb="popover"] > div,
.stApp:has(.st-key-chx_skin [data-testid="stCheckbox"] input:checked) [role="tooltip"]{
  background:#FFFFFF!important;color:#16233D!important;border:1px solid rgba(23,52,94,.16)!important;border-radius:10px!important}
.stApp:has(.st-key-chx_skin [data-testid="stCheckbox"] input:checked) [data-baseweb="menu"] li,
.stApp:has(.st-key-chx_skin [data-testid="stCheckbox"] input:checked) [role="menuitem"]{
  color:#16233D!important;background:transparent!important}
.stApp:has(.st-key-chx_skin [data-testid="stCheckbox"] input:checked) [role="menuitem"]:hover{
  background:rgba(14,116,144,.10)!important}
[data-testid="stExpander"] details{background:transparent!important}
'''


def chart_colors(night: bool) -> dict:
    """图表用色，随皮肤切换。"""
    return CHART["dark" if night else "light"]


def _tokens(t: dict) -> str:
    body = "".join(f"--{k}:{v};" for k, v in t.items())
    return body + f"--mono:{MONO};--sans:{SANS}"


def _base() -> str:
    return """
*{box-sizing:border-box}
html,body,[data-testid="stAppViewContainer"]{background:var(--bg);color:var(--txt);font-family:var(--sans)}
[data-testid="stMainBlockContainer"]{padding-top:1.4rem;max-width:1500px}
[data-testid="stHeader"]{background:transparent}
[data-testid="stHeader"] [data-testid="stToolbar"]{color:var(--txt-3)}
[data-testid="stDecoration"],footer,[data-testid="stStatusWidget"]{display:none!important}
h1,h2,h3,h4{color:var(--txt);letter-spacing:.01em}
a{color:var(--accent)}
hr{border-color:var(--line);background:var(--line)}
code,kbd,pre,[data-testid="stCode"]{font-family:var(--mono)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:6px}
"""


def _sidebar() -> str:
    return """
[data-testid="stSidebar"]{background:var(--rail);border-right:1px solid var(--rail-line)}
[data-testid="stSidebar"] [data-testid="stSidebarContent"]{position:relative}
[data-testid="stSidebar"] [data-testid="stSidebarContent"]:before{content:"";position:absolute;left:0;top:0;bottom:0;
  width:2px;background:linear-gradient(180deg,var(--accent),transparent 46%);opacity:.85}
[data-testid="stSidebar"] :is(h1,h2,h3,strong,b){color:var(--txt)}
[data-testid="stSidebar"] p,[data-testid="stSidebar"] li{color:var(--txt-2)}
[data-testid="stSidebar"] :is([data-testid="stCaptionContainer"],.chx-note,.chx-foot){color:var(--txt-3)}
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p{color:var(--txt-3);font-size:12px}
[data-testid="stSidebar"] hr{background:var(--line);border-color:var(--line)}
[data-testid="stSidebarCollapseButton"] :is(svg,span){color:var(--txt-3)}
"""


def _brand() -> str:
    return """
.chx-brand{display:flex;align-items:center;gap:11px;margin:2px 0 6px}
.chx-brand .logo{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;flex:0 0 34px;
  font-family:var(--mono);font-size:15px;color:var(--accent-ink);
  background:linear-gradient(140deg,var(--accent),color-mix(in srgb,var(--accent) 62%,#000));
  box-shadow:0 0 18px color-mix(in srgb,var(--accent) 40%,transparent)}
.chx-brand b{display:block;font-size:14px;color:var(--txt);letter-spacing:.01em}
.chx-brand span{display:block;font-family:var(--mono);font-size:9.5px;color:var(--txt-3);letter-spacing:.18em;margin-top:2px}
.chx-sec{font-family:var(--mono);font-size:10px;letter-spacing:.2em;color:var(--txt-3);
  text-transform:uppercase;margin:4px 0 -4px}
.chx-note{font-size:11px;color:var(--txt-3);line-height:1.75}
.chx-note b{color:var(--txt-2);font-family:var(--mono);font-weight:500}
.chx-foot{font-size:10.5px;color:var(--txt-3);line-height:1.7;font-family:var(--mono);opacity:.9}
"""


def _controls() -> str:
    return """
[data-testid="stTextInput"] input,[data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea,[data-testid="stDateInput"] input,
[data-testid="stTimeInput"] input,[data-testid="stSelectbox"] div[data-baseweb="select"]>div{
  background:var(--card)!important;border:1px solid var(--line-2)!important;border-radius:10px!important;
  color:var(--txt)!important;font-family:var(--mono)!important;font-size:13.5px!important}
[data-testid="stTextArea"] textarea{line-height:2;letter-spacing:.05em}
[data-testid="stTextInput"] input:focus,[data-testid="stTextArea"] textarea:focus,
[data-testid="stTextInput"]>div:focus-within,[data-testid="stTextArea"]>div:focus-within,
[data-testid="stTextAreaRootElement"]:focus-within,[data-testid="stTextInputRootElement"]:focus-within{
  border-color:var(--accent)!important;box-shadow:0 0 0 2px var(--accent-soft)!important}
[data-testid="stTextInputRootElement"],[data-testid="stTextAreaRootElement"],
[data-testid="stNumberInputContainer"],[data-testid="stDateInputField"]{
  background:var(--card)!important;border-color:var(--line-2)!important}
[data-testid="stTextInput"] input::placeholder,[data-testid="stTextArea"] textarea::placeholder{color:var(--txt-3)}
[data-testid="stWidgetLabel"] p{color:var(--txt-3);font-size:12px}
[data-testid="stCaptionContainer"]{color:var(--txt-3)}
div[data-baseweb="popover"] :is(div[role="listbox"],ul){background:var(--card)!important;
  border:1px solid var(--line-2)!important;border-radius:12px!important}
div[data-baseweb="popover"] li{color:var(--txt)!important}
[data-testid="stDateInputCalendar"]{background:var(--card)!important;border:1px solid var(--line-2)!important;
  border-radius:12px!important}
[data-testid="stDateInputCalendar"] *{color:var(--txt)}
.stButton>button,.stDownloadButton>button{
  background:var(--card)!important;border:1px solid var(--line-2)!important;border-radius:10px!important;
  color:var(--txt-2)!important;font-weight:500!important;font-family:var(--sans)!important;min-height:40px}
.stButton>button:hover,.stDownloadButton>button:hover{border-color:var(--accent)!important;color:var(--txt)!important}
.stButton>button:disabled,.stDownloadButton>button:disabled{opacity:.45}
[data-testid="stSidebar"] .stButton>button{background:transparent!important;border-color:var(--line-2)!important}
.st-key-cta_main .stButton>button{
  background:linear-gradient(140deg,var(--accent),color-mix(in srgb,var(--accent) 72%,#000))!important;
  color:var(--accent-ink)!important;border:none!important;font-weight:600!important;letter-spacing:.02em!important;
  min-height:44px;box-shadow:0 6px 22px color-mix(in srgb,var(--accent) 26%,transparent)}
[data-testid="stCheckbox"]{margin-bottom:2px}
[data-testid="stCheckbox"] label>div:first-of-type{background:var(--accent)!important;border-color:var(--accent)!important}
[data-testid="stCheckbox"] label>div:first-of-type svg polyline{stroke:var(--accent-ink)}
[data-testid="stCheckbox"]:not(:has(input:checked)) label>div:first-of-type{background:transparent!important;
  border-color:var(--line-2)!important}
[data-testid="stCheckbox"]:not(:has(input:checked)) label>div:first-of-type svg{opacity:0}
[data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p{color:var(--txt-2);font-size:12.5px}
.st-key-chx_skin [data-testid="stCheckbox"]{margin:0}
.st-key-chx_skin [data-testid="stCheckbox"] label>div:first-of-type{display:none!important}
.st-key-chx_skin [data-testid="stCheckbox"] [data-testid="stWidgetLabel"]{
  display:flex!important;align-items:center;justify-content:center;width:100%!important;height:40px;
  border:1px solid var(--line-2);border-radius:10px;background:var(--card);
  cursor:pointer;transition:border-color .18s}
.st-key-chx_skin [data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p{
  font-size:0!important;margin:0;display:flex;align-items:center;gap:8px}
.st-key-chx_skin [data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p:before{
  content:"🌙 夜晚";font-family:var(--mono);font-size:12.5px;letter-spacing:.04em;color:var(--txt-3);opacity:.6}
.st-key-chx_skin [data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p:after{
  content:"☀️ 白天";font-family:var(--mono);font-size:12.5px;letter-spacing:.04em;color:var(--txt-3);opacity:.6}
.st-key-chx_skin [data-testid="stCheckbox"] [data-testid="stWidgetLabel"]:hover{border-color:var(--accent)}
"""


def _toggle_rules(day_sel: str, night_sel: str) -> str:
    """按当前皮肤点亮开关的对应半边。"""
    base = '.st-key-chx_skin [data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p'
    return (
        f'{night_sel} {base}:before{{color:var(--accent);opacity:1}}'
        f'{day_sel} {base}:after{{color:var(--accent);opacity:1}}'
    )


def _hero() -> str:
    return """
.chx-hero{position:relative;border:1px solid var(--line);border-radius:16px;padding:22px 24px 18px;
  overflow:hidden;background:var(--card);box-shadow:var(--shadow)}
.chx-hero:before{content:"";position:absolute;inset:0;
  background:linear-gradient(150deg,var(--glow),var(--glow2) 46%,transparent 74%)}
.chx-hero:after{content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(var(--grid) 1px,transparent 1px) 0 0/38px 38px,
             linear-gradient(90deg,var(--grid) 1px,transparent 1px) 0 0/38px 38px}
.chx-hero>*{position:relative;z-index:1}
.chx-hero-top{display:flex;align-items:flex-start;gap:18px;flex-wrap:wrap}
.chx-title{font-size:38px;line-height:1.08;font-weight:700;color:var(--txt);letter-spacing:.01em}
.chx-title small{font-size:19px;font-weight:500;color:var(--txt-2);margin-left:12px;letter-spacing:.02em}
.chx-meta{margin-top:9px;font-family:var(--mono);font-size:11.5px;color:var(--txt-3);letter-spacing:.03em;line-height:1.7}
.chx-price{margin-left:auto;text-align:right}
.chx-price b{font-family:var(--mono);font-size:31px;font-weight:600;letter-spacing:.01em}
.chx-price b small{font-size:14px;margin-left:7px;font-weight:500}
.chx-price span{display:block;margin-top:5px;font-family:var(--mono);font-size:11px;color:var(--txt-3)}
.chx-chip{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--accent);
  border:1px solid color-mix(in srgb,var(--accent) 34%,transparent);background:var(--accent-soft);
  border-radius:7px;padding:5px 10px;white-space:nowrap;align-self:flex-start}
.chx-chip.dim{color:var(--txt-3);border-color:var(--line-2);background:transparent}
.chx-strip{display:flex;flex-wrap:wrap;gap:22px;margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
.chx-strip div{font-size:11.5px;color:var(--txt-3);line-height:1.5}
.chx-strip b{display:block;font-family:var(--mono);font-size:13.5px;color:var(--txt);font-weight:500;margin-top:4px}
"""


def _cards() -> str:
    return """
.chx-metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}
@media (max-width:1200px){.chx-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:760px){.chx-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
.chx-metric{position:relative;border:1px solid var(--line);border-radius:13px;padding:14px 15px 12px;
  background:var(--card);box-shadow:var(--shadow)}
.chx-metric:before{content:"";position:absolute;left:15px;right:15px;top:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--line-2),transparent)}
.chx-metric .k{font-size:11.5px;color:var(--txt-3);letter-spacing:.03em}
.chx-metric .v{font-family:var(--mono);font-size:25px;font-weight:600;margin-top:8px;letter-spacing:.01em;
  display:flex;align-items:baseline;gap:6px;color:var(--txt)}
.chx-metric .v u{text-decoration:none;font-size:12.5px;font-weight:500;opacity:.85}
.chx-metric .s{font-family:var(--mono);font-size:10.5px;color:var(--txt-3);margin-top:6px;line-height:1.5}
.chx-sec{display:flex;align-items:center;gap:13px;margin:30px 0 14px}
.chx-sec .no{font-family:var(--mono);font-size:12.5px;color:var(--accent);letter-spacing:.1em}
.chx-sec h2{font-size:16px;font-weight:600;letter-spacing:.02em;color:var(--txt);margin:0}
.chx-sec .rule{flex:1;height:1px;background:linear-gradient(90deg,var(--line-2),transparent)}
.chx-sec .hint{font-family:var(--mono);font-size:11px;color:var(--txt-3);white-space:nowrap}
.chx-card{border:1px solid var(--line);border-radius:14px;background:var(--card);box-shadow:var(--shadow);
  padding:14px 16px;overflow:hidden}
.chx-card :is(h1,h2,h3){margin-top:.5em}
.chx-ai{border-left:3px solid var(--violet)!important}
"""


def _alert() -> str:
    return """
[data-testid="stAlert"]{border-radius:11px!important;border:1px solid var(--line)!important;
  border-left:3px solid var(--accent)!important;background:var(--card)!important;box-shadow:var(--shadow)}
[data-testid="stAlertContainer"]{background:transparent!important;color:var(--txt-2)!important}
[data-testid="stAlertContentWarning"]{background:color-mix(in srgb,var(--amber) 8%,transparent)!important;
  border-radius:10px!important;color:var(--amber-ink)!important}
[data-testid="stAlertContentInfo"]{background:var(--accent-soft)!important;border-radius:10px!important}
[data-testid="stAlertContentSuccess"]{background:color-mix(in srgb,var(--down) 9%,transparent)!important;
  border-radius:10px!important}
[data-testid="stAlertContentError"]{background:var(--danger-soft)!important;border-radius:10px!important}
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]){border-left-color:var(--amber)!important}
[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]){border-left-color:var(--down)!important}
[data-testid="stAlert"]:has([data-testid="stAlertContentError"]){border-left-color:var(--danger)!important}
[data-testid="stAlert"] [data-testid="stMarkdownContainer"] p{margin:8px 0;line-height:1.75;font-size:12.5px}
[data-testid="stAlert"] a{color:inherit;text-decoration:underline}
"""


def _table() -> str:
    return """
.chx-table{width:100%;border-collapse:collapse;font-size:12.5px}
.chx-table thead th{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--txt-3);text-align:left;padding:10px 14px;background:color-mix(in srgb,var(--txt) 4%,transparent);
  border-bottom:1px solid var(--line-2);font-weight:500;white-space:nowrap}
.chx-table td{padding:10px 14px;border-bottom:1px solid var(--line);color:var(--txt-2);
  font-family:var(--mono);font-size:12px;vertical-align:middle}
.chx-table tbody tr:hover td{background:var(--accent-soft)}
.chx-table td.txt{font-family:var(--sans)}
.chx-table .tag{display:inline-block;font-family:var(--mono);font-size:11px;padding:3px 8px;border-radius:6px;
  border:1px solid transparent;white-space:nowrap}
.chx-table .tag.buy{color:var(--accent);background:var(--accent-soft);
  border-color:color-mix(in srgb,var(--accent) 32%,transparent)}
.chx-table .tag.sell{color:var(--amber);background:color-mix(in srgb,var(--amber) 13%,transparent);
  border-color:color-mix(in srgb,var(--amber) 32%,transparent)}
.chx-table .tag.ok{color:var(--down);background:color-mix(in srgb,var(--down) 12%,transparent);
  border-color:color-mix(in srgb,var(--down) 30%,transparent)}
.chx-table .tag.no{color:var(--txt-3);background:color-mix(in srgb,var(--txt) 5%,transparent);border-color:var(--line)}
.chx-table .tag.warn{color:var(--amber);background:color-mix(in srgb,var(--amber) 13%,transparent);
  border-color:color-mix(in srgb,var(--amber) 30%,transparent)}
.chx-table .star{color:var(--accent)}
.chx-scroll{overflow-x:auto}
.chx-up{color:var(--up)}.chx-down{color:var(--down)}.chx-cy{color:var(--accent)}
.chx-vi{color:var(--violet)}.chx-am{color:var(--amber)}.chx-dim{color:var(--txt-3)}
[data-testid="stMarkdownContainer"] table{border-collapse:collapse;width:100%;font-size:12.5px;margin:10px 0}
[data-testid="stMarkdownContainer"] thead th{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;
  color:var(--txt-3);text-align:left;padding:9px 12px;border-bottom:1px solid var(--line-2);
  background:color-mix(in srgb,var(--txt) 4%,transparent)}
[data-testid="stMarkdownContainer"] td{padding:9px 12px;border-bottom:1px solid var(--line);color:var(--txt-2)}
[data-testid="stMarkdownContainer"] blockquote{border-left:3px solid var(--amber);
  background:color-mix(in srgb,var(--amber) 7%,transparent);border-radius:9px;padding:10px 14px;color:var(--amber-ink)}
[data-testid="stMarkdownContainer"] code{background:var(--card-2);border:1px solid var(--line);
  border-radius:5px;padding:1px 6px;font-size:12px;color:var(--txt)}
"""


def _misc() -> str:
    return """
[data-testid="stExpander"]{border:1px solid var(--line)!important;border-radius:12px!important;
  background:var(--card)!important;overflow:hidden}
[data-testid="stExpander"] summary{color:var(--txt-2)!important;font-size:12.5px}
[data-testid="stExpander"] summary:hover{color:var(--accent)!important}
[data-testid="stJson"]{background:transparent!important}
[data-testid="stProgressBarTrack"]{background:var(--card-2)!important;border-radius:8px}
[data-testid="stProgressBarTrack"]>div{background:linear-gradient(90deg,var(--accent),var(--violet))!important}
[data-testid="stSpinner"] *{color:var(--txt-3)!important}
[data-testid="stPlotlyChart"]{border-radius:12px;overflow:hidden}
[data-testid="stDataFrame"]{border:1px solid var(--line);border-radius:12px;overflow:hidden}
[data-testid="stRadio"] [role="radiogroup"]{gap:6px}
[data-testid="stSidebar"] [data-testid="stRadioOption"]{
  border:1px solid var(--line-2)!important;border-radius:9px!important;padding:7px 12px!important;
  background:var(--card)!important;transition:.16s;flex:1;justify-content:center;margin:0!important}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover{border-color:var(--accent)!important}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:has(input:checked){
  background:var(--accent-soft)!important;border-color:var(--accent)!important}
[data-testid="stSidebar"] [data-testid="stRadioOption"] p{color:var(--txt-2)!important;font-size:12.5px}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:has(input:checked) p{
  color:var(--accent)!important;font-weight:600}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"]{gap:6px;flex-direction:row}
.chx-footline{margin-top:22px;padding-top:12px;border-top:1px solid var(--line);
  text-align:center;font-size:11.5px;color:var(--txt-3);font-family:var(--mono);letter-spacing:.04em}
"""


def build_css() -> str:
    """整套样式：两套调色板 + 白天/夜晚联动 + 组件皮肤。

    开关语义 —— 未勾选 = 🌙 夜晚（默认，天然无闪白），勾选 = ☀️ 白天。
    """
    day_sel = f".stApp:has({TOGGLE}:checked)"
    night_sel = f".stApp:not(:has({TOGGLE}:checked))"
    media_sel = f":root:not(:has({TOGGLE}))"
    parts = [
        f":root{{{_tokens(DARK)}}}",
        f"{night_sel}{{{_tokens(DARK)}}}",
        f"{day_sel}{{{_tokens(LIGHT)}}}",
        f"@media (prefers-color-scheme: light){{{media_sel}{{{_tokens(LIGHT)}}}}}",
        _base(), _sidebar(), _brand(), _controls(), _hero(), _cards(), _alert(), _table(),
        _misc(), LIGHT_EXTRA,
        f'{day_sel} [data-testid="stAppViewContainer"]{{color-scheme:light!important}}',
        _toggle_rules(day_sel, night_sel),
    ]
    return "<style>" + "".join(parts) + "</style>"
