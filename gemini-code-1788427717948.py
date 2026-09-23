import os
import sys
import json
import asyncio
import websockets
import logging
import threading
from http.server import SimpleHTTPRequestHandler
import socketserver
import time
import re
import hmac
from urllib.parse import urlparse

from cloud_state_store import CloudStateStore

logging.basicConfig(level=logging.INFO, format='%(asctime)s - UI_APP - %(levelname)s - %(message)s')
logger = logging.getLogger('UI_APP')
# 同时输出到文件，便于排查 MCP 原始消息
_fh = logging.FileHandler('mcp_debug.log', encoding='utf-8')
_fh.setLevel(logging.INFO)
_fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(_fh)

# 全局多态状态机：支持 emotion (表情), timer (定时器), recipe_list (菜谱列表), timer_done (定时完成提醒)
app_state = {
    "card_type": "emotion",       # 默认态：表情
    "emotion": "breathing",       # 表情状态: breathing, listening, thinking, eyes, happy, hungry
    "title": "",
    "value": "",
    "items": [],                  # 菜谱列表数据 / 多灶具定时器数据
    "target_timestamp": 0,        # 单灶具定时器目标时间戳（兼容旧字段）
    "timers": [],                 # 持久化的多灶具定时器列表：[{name, target_timestamp, duration_minutes, status}]
    "fan_speed": 1,               # 油烟机风速档位 1-5，开机默认 1 档
    "wifi_on": True,              # WiFi 开关状态
    "light_brightness": 80,       # 照明亮度 0-100，开机默认 80%
    "pm25": 29,                  # 厨房 PM2.5 实时值；未接入传感器时显示安全基准值
    "burner_temperatures": {},    # 固定油温：{"left": 120, "right": 180}，仅已启用的灶具写入
    "step_index": 0,              # 菜谱步骤当前索引（0-based）
    "step_total": 0,              # 菜谱步骤总数
    "revision": 0,                # 单调递增的状态版本，供前端丢弃过期推送
    "ui_ack_revision": 0,         # 浏览器已完成渲染确认的最高版本
    "user_name": ""               # 当前说话人名（由 AI 声纹识别后传入），空字符串=未识别
    ,"magic_knob_binding": None    # 魔术旋钮一次只绑定一个可控制功能
}

# 重启开发服务时保留正在执行的菜谱与定时任务；时间戳继续按真实时间计算。
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_state.json")
MCP_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_config.json")

def load_mcp_endpoint():
    """优先读取运行环境；首次配置后从本机私有配置文件读取。"""
    endpoint = os.environ.get("MCP_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    try:
        with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as config_file:
            endpoint = str(json.load(config_file).get("endpoint", "")).strip()
    except (OSError, ValueError, TypeError, AttributeError):
        endpoint = ""
    return endpoint

def save_mcp_endpoint(endpoint):
    """保存到本机；令牌不会返回到前端或写入运行状态。"""
    if not isinstance(endpoint, str) or not endpoint.startswith("wss://"):
        raise ValueError("MCP 地址必须以 wss:// 开头")
    temp_path = MCP_CONFIG_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as config_file:
        json.dump({"endpoint": endpoint.strip()}, config_file, ensure_ascii=False)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, MCP_CONFIG_FILE)

app_state["mcp_configured"] = bool(load_mcp_endpoint())
try:
    with open(STATE_FILE, "r", encoding="utf-8") as state_file:
        saved_state = json.load(state_file)
    if isinstance(saved_state, dict):
        app_state.update(saved_state)
        logger.info("Restored runtime kitchen state.")
except (OSError, ValueError, TypeError):
    pass

# 用户个性化待命映射：user_name → (emotion, title, value)
# emotion 对应前端表情状态机的 key；未识别用户走默认文案
USER_GREETINGS = {
    "张老师": ("eyes", "张老师好", "今天想做什么菜？小美随时待命"),
    "钧哥":   ("eyes", "钧哥好", "想做点啥？小美给您推荐"),
    # 可继续添加更多用户
}
DEFAULT_GREETING = ("breathing", "", "")
GENERIC_GREETING = ("eyes", "你好", "小美随时待命")
greeted_users = set()
anonymous_greeted = False

def resolve_standby_greeting(user_name):
    """识别到具体用户时每次打招呼都显示眼睛表情+名称+问候；匿名首次显示通用问候。"""
    global anonymous_greeted
    if user_name and user_name in USER_GREETINGS:
        greeted_users.add(user_name)
        return USER_GREETINGS[user_name]
    if not user_name and not anonymous_greeted:
        anonymous_greeted = True
        return GENERIC_GREETING
    return DEFAULT_GREETING

# 内置菜谱库：当 AI 调用 recipe_list 但漏传 items 时用作兜底填充，保证 UI 一定能显示菜品
# 每道含 name(菜名)、time(时长)、tag(标签)、steps(3-8 步操作步骤数组)
DEFAULT_RECIPE_LIBRARY = [
    {
        "name": "番茄炒蛋",
        "time": "10分钟",
        "tag": "快手",
        "steps": ["鸡蛋打散加少许盐搅匀", "番茄切块", "热锅冷油倒入蛋液炒至凝固盛出", "底油炒番茄至出汁", "倒回鸡蛋翻炒均匀出锅"]
    },
    {
        "name": "红烧肉",
        "time": "50分钟",
        "tag": "硬菜",
        "steps": ["五花肉切块焯水去腥", "锅中放少许油加冰糖炒糖色", "下五花肉翻炒上色", "加入葱姜蒜、八角、桂皮炒香", "倒入生抽、老抽、料酒调味", "加开水没过肉块大火烧开转小火炖40分钟", "大火收汁即可"]
    },
    {
        "name": "清炒时蔬",
        "time": "8分钟",
        "tag": "清淡",
        "steps": ["时蔬洗净沥干", "蒜切末", "热锅冷油爆香蒜末", "下时蔬大火快炒", "加盐调味出锅"]
    },
    {
        "name": "麻婆豆腐",
        "time": "15分钟",
        "tag": "下饭",
        "steps": ["豆腐切块焯水去豆腥", "牛肉末加料酒煸炒出油", "下豆瓣酱炒出红油", "加豆腐和适量热水", "加生抽、糖调味烧2分钟", "水淀粉勾芡撒花椒粉葱花"]
    },
    {
        "name": "酸辣土豆丝",
        "time": "10分钟",
        "tag": "下饭",
        "steps": ["土豆去皮切细丝泡水去淀粉", "青红椒切丝", "热锅冷油下花椒爆香", "下土豆丝大火快炒", "加白醋、盐、生抽调味", "撒青红椒丝出锅"]
    },
    {
        "name": "糖醋里脊",
        "time": "25分钟",
        "tag": "硬菜",
        "steps": ["里脊肉切条加盐料酒抓匀", "加淀粉和水抓成糊状挂浆", "油温六成下肉条炸至定型捞出", "复炸至金黄捞出", "锅留底油倒番茄酱、糖、醋、生抽烧开", "下肉条快速翻炒裹汁出锅"]
    },
    {
        "name": "蒜蓉西兰花",
        "time": "8分钟",
        "tag": "清淡",
        "steps": ["西兰花掰小朵焯水", "蒜切末", "热锅冷油爆香蒜末", "下西兰花翻炒", "加盐、鸡精调味出锅"]
    },
    {
        "name": "鱼香肉丝",
        "time": "20分钟",
        "tag": "下饭",
        "steps": ["里脊肉切丝加料酒淀粉抓匀", "木耳、胡萝卜、青椒切丝", "调鱼香汁：糖醋生抽淀粉水", "热锅冷油滑炒肉丝盛出", "底油下豆瓣酱炒出红油", "下蔬菜丝翻炒再倒回肉丝", "淋入鱼香汁炒匀出锅"]
    }
]

# 保护 app_state 的线程锁（HTTP 线程读 / MCP 线程写）。
# MCP 工具处理在持锁状态下会触发 broadcast_sse()，后者也需要读取状态；
# 使用可重入锁避免该合法的同线程嵌套读取造成死锁。
state_lock = threading.RLock()

# CloudBase 部署时将状态写入 MySQL；本地未配置数据库时自动回退到 runtime_state.json。
# 安卓客户端只读取 /api/state，不会接触数据库连接信息。
cloud_state_store = CloudStateStore(logger)
with state_lock:
    cloud_saved_state = cloud_state_store.load()
    if isinstance(cloud_saved_state, dict):
        app_state.update(cloud_saved_state)
        logger.info("Restored runtime kitchen state from cloud database.")
    # MCP 地址只来自环境变量或本机私有配置，永远不从状态库读取。
    app_state["mcp_configured"] = bool(load_mcp_endpoint())

# 此令牌用于 Android 读取状态接口。生产部署必须设置，避免公开设备状态。
SYNC_API_TOKEN = os.environ.get("SYNC_API_TOKEN", "").strip()

def api_state_snapshot():
    """生成给 Android 的安全状态副本，不泄露页面内部确认状态。"""
    with state_lock:
        snapshot = dict(app_state)
    snapshot.pop("ui_ack_revision", None)
    snapshot.pop("mcp_configured", None)
    return snapshot

def has_api_access(headers):
    """只接受 Bearer 令牌；未设置令牌的服务不会开放同步接口。"""
    if not SYNC_API_TOKEN:
        return False
    supplied = headers.get("Authorization", "")
    expected = "Bearer " + SYNC_API_TOKEN
    return hmac.compare_digest(supplied, expected)

# SSE 客户端管理：状态变化时推送给所有连接的浏览器
sse_clients = set()  # 持有每个客户端的 wfile（BytesIO 缓冲区）

def broadcast_sse():
    """状态变化时把 app_state 推送给所有 SSE 客户端。"""
    with state_lock:
        app_state["revision"] = int(app_state.get("revision", 0)) + 1
        payload = json.dumps(app_state, ensure_ascii=False)
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as state_file:
                state_file.write(payload)
        except OSError as exc:
            logger.warning(f"Could not persist runtime state: {exc}")
        # 云端持久化失败不影响 MCP 响应或本地预览；下次状态改变时会重试。
        cloud_state_store.save(app_state)
    if not sse_clients:
        return app_state["revision"]
    data = f"data: {payload}\n\n".encode("utf-8")
    # 复制一份避免遍历时修改
    for client in list(sse_clients):
        try:
            client.write(data)
            client.flush()
        except Exception:
            try:
                sse_clients.discard(client)
            except Exception:
                pass
    return app_state["revision"]

async def wait_for_ui_ack(revision, timeout=0.7):
    """在语音工具返回前，短暂等待浏览器确认已接收指定状态版本。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with state_lock:
            if int(app_state.get("ui_ack_revision", 0)) >= revision:
                return True
        await asyncio.sleep(0.03)
    return False

def set_voice_lifecycle(action):
    """更新默认待机页的语音生命周期，不覆盖菜谱、定时和设备控制画面。"""
    action = str(action or "").strip().lower()
    with state_lock:
        if app_state.get("card_type") != "emotion":
            return False, app_state.copy()
        if action == "listening":
            emotion, title, value = "listening", "", ""
        elif action == "thinking":
            emotion, title, value = "thinking", "", ""
        elif action == "idle":
            emotion, title, value = resolve_standby_greeting(app_state.get("user_name", ""))
        else:
            return False, app_state.copy()
        app_state["emotion"] = emotion
        app_state["title"] = title
        app_state["value"] = value
        snapshot = app_state.copy()
    broadcast_sse()
    return True, snapshot

def normalize_burner_name(raw):
    """将各种灶具称呼归一化为标准名：左灶 / 右灶。
    只支持左灶和右灶，不支持"灶台"默认值。
    无法识别时返回 None，调用方应拒绝创建定时器。"""
    if not raw:
        return None
    s = str(raw).strip()
    # 左灶识别：左灶/左边灶/左侧灶/左炉/左火/左头/左 等
    if any(k in s for k in ("左灶", "左边", "左侧", "左炉", "左火", "左头", "左")):
        return "左灶"
    # 右灶识别
    if any(k in s for k in ("右灶", "右边", "右侧", "右炉", "右火", "右头", "右")):
        return "右灶"
    return None

def upsert_timer(burner_raw, target_ts, minutes, timer_mode="shutoff"):
    """灶具定时器写入：先清除该灶具所有旧定时器（含已完成），再追加新定时器。
    确保每个灶具同时只存在一个定时时间段，以最后设定为准。
    burner_raw 无法识别为左灶/右灶时返回 None，不写入。"""
    # 提醒计时不绑定灶具，也绝不触发关火；每次仅保留当前菜谱的一项提醒。
    if timer_mode == "reminder":
        name = "计时提醒"
    else:
        name = normalize_burner_name(burner_raw)
    if name is None:
        return None
    # 清除该灶具的所有旧定时器（running 或 done），保证唯一性
    app_state["timers"] = [t for t in app_state["timers"]
                           if not (t.get("name") == name and t.get("mode", "shutoff") == timer_mode)]
    app_state["timers"].append({
        "name": name,
        "target_timestamp": target_ts,
        "duration_minutes": minutes,
        "mode": timer_mode,
        "status": "running",
        "done_notified": False
    })
    return name

def remove_timer(burner_raw):
    """删除指定灶具的定时器。返回被删除的灶具名，未找到返回 None。"""
    name = normalize_burner_name(burner_raw)
    if name is None:
        return None
    before = len(app_state["timers"])
    app_state["timers"] = [t for t in app_state["timers"] if t.get("name") != name]
    return name if len(app_state["timers"]) < before else None

HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Smart Kitchen Generative UI - PC Dev</title>
    <style>
        /* 横屏布局：固定 960×220 厨房显示屏设计稿 */
        /* 预览自适应：仅按当前屏幕宽度等比缩放，内部布局始终维持 960:220 */
        html, body { height: 100%; overflow: hidden; }
        /* 窗口外（灰色）：超出设计稿尺寸的浏览器区域 */
        body { font-family: -apple-system, sans-serif; background: #fff; color: #f8fafc; margin: 0; padding: 0; display: flex; flex-direction: row; align-items: center; justify-content: center; min-height: 100vh; box-sizing: border-box; }
        /* 设计稿（纯黑底色）：固定 960x220 */
        .container { width: 960px; height: 220px; display: flex; flex-direction: row; gap: 20px; align-items: stretch; transform-origin: center center; background: #000; border-radius: 0; position: relative; }

        /* 通用卡片容器 */
        .card { background: #1e293b; padding: 18px 28px; border-radius: 20px; box-shadow: 0 12px 30px rgba(0,0,0,0.6); text-align: center; border: 1px solid rgba(255,255,255,0.1); position: relative; overflow: hidden; transition: all 0.4s ease; flex: 1; display: flex; flex-direction: column; justify-content: center; }
        .card::before { content: ''; position: absolute; top: 0; left: 0; bottom: 0; width: 6px; }

        /* 状态变体 */
        .card.emotion::before { background: linear-gradient(180deg, #3b82f6, #6366f1); }
        .card.timer::before { background: linear-gradient(180deg, #a855f7, #ec4899); }
        .card.recipe_list::before { background: linear-gradient(180deg, #22c55e, #10b981); }
        .card.recipe_steps::before { background: linear-gradient(180deg, #3b82f6, #8b5cf6); }

        .badge { display: inline-block; padding: 4px 14px; border-radius: 20px; font-size: 12px; text-transform: uppercase; letter-spacing: 1.5px; font-weight: bold; margin-bottom: 6px; }
        .card.emotion .badge { background: rgba(59, 130, 246, 0.15); color: #60a5fa; }
        .card.timer .badge { background: rgba(168, 85, 247, 0.15); color: #e879f9; }
        .card.recipe_list .badge { background: rgba(34, 197, 94, 0.15); color: #4ade80; }
        .card.recipe_steps .badge { background: rgba(59, 130, 246, 0.15); color: #60a5fa; }

        /* 表情动效区 */
        .avatar-box { width: 72px; height: 72px; margin: 4px auto 8px; background: rgba(255,255,255,0.05); border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 36px; border: 2px solid rgba(255,255,255,0.1); animation: pulse 2s infinite; }
        @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }

        .title { font-size: 22px; font-weight: 600; margin-bottom: 4px; color: #cbd5e1; }
        .value { font-size: 42px; font-weight: 800; margin: 4px 0; color: #f8fafc; font-variant-numeric: tabular-nums; line-height: 1.1; }
        .desc { font-size: 15px; color: #94a3b8; line-height: 1.4; margin-top: 4px; }
        /* 定时器数字位：每位独立、固定宽度，避免整体重渲染抖动 */
        .digit { display: inline-block; min-width: 0.62em; text-align: center; }
        .digit-colon { display: inline-block; min-width: 0.32em; text-align: center; }

        /* 多灶具定时器列表 */
        .timer-list { display: flex; flex-direction: column; gap: 14px; margin-top: 10px; }
        .timer-item { background: rgba(255,255,255,0.04); padding: 14px 18px; border-radius: 16px; border: 2px solid rgba(255,255,255,0.06); transition: all 0.25s ease; position: relative; }
        .timer-item.active { border-color: #a855f7; background: rgba(168,85,247,0.12); box-shadow: 0 6px 20px rgba(168,85,247,0.25); }
        .timer-item.done { border-color: #22c55e; background: rgba(34,197,94,0.10); }
        .timer-item.done .value { color: #4ade80; }
        .burner-name { font-size: 13px; font-weight: 700; color: #cbd5e1; margin-bottom: 4px; letter-spacing: 0.5px; }
        .timer-status { font-size: 12px; color: #94a3b8; margin-top: 4px; }
        .timer-item.done .timer-status { color: #4ade80; font-weight: 600; }

        /* 油烟机风速卡片：半圆形挡位可视化 */
        .fan-wrap { display: flex; flex-direction: column; align-items: center; padding: 8px 0; }
        .fan-svg { width: 260px; height: 150px; }
        .fan-arc { fill: none; stroke: rgba(255,255,255,0.08); stroke-width: 16; stroke-linecap: round; transition: stroke 0.3s ease, filter 0.3s ease; }
        .fan-arc.active { stroke: url(#fanGrad); filter: drop-shadow(0 0 6px rgba(251,146,60,0.6)); }
        .fan-center { font-size: 48px; font-weight: 800; fill: #fff; text-anchor: middle; }
        .fan-label { font-size: 12px; fill: #94a3b8; text-anchor: middle; }
        .fan-tick { font-size: 10px; fill: #64748b; text-anchor: middle; }
        .fan-tick.on { fill: #fb923c; font-weight: 700; }
        .fan-speed-value { margin-top: 6px; font-size: 15px; color: #fbbf24; font-weight: 700; font-variant-numeric: tabular-nums; }
        .fan-hint { font-size: 11px; color: #64748b; margin-top: 4px; }

        /* 完成提示横幅 */
        /* 隐藏提示不可占用或露出有效显示区；仅在有完成/提醒信息时出现。 */
        .done-banner { position: absolute; top: 20px; left: 50%; transform: translateX(-50%) translateY(-150%); background: linear-gradient(90deg, #22c55e, #10b981); color: #fff; padding: 14px 28px; border-radius: 30px; font-size: 16px; font-weight: 700; box-shadow: 0 10px 30px rgba(34,197,94,0.45); z-index: 9999; opacity: 0; visibility: hidden; pointer-events: none; transition: transform 0.4s cubic-bezier(0.34, 1.56, 0.64, 1), opacity .15s ease, visibility 0s linear .4s; }
        .done-banner.show { transform: translateX(-50%) translateY(0); opacity: 1; visibility: visible; transition-delay: 0s; animation: bannerPulse 1.2s ease-in-out 2; }
        @keyframes bannerPulse { 0%, 100% { box-shadow: 0 10px 30px rgba(34,197,94,0.45); } 50% { box-shadow: 0 10px 40px rgba(34,197,94,0.85); } }

        /* 即将完成定时器 overlay：最后 60 秒自动置顶显示 */
        #timer-overlay { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%) scale(calc(0.9 * var(--preview-scale, 1))); background: linear-gradient(135deg, #1e293b 0%, #312e81 100%); padding: 28px 32px; border-radius: 28px; box-shadow: 0 25px 60px rgba(168,85,247,0.5), 0 0 0 2px rgba(168,85,247,0.4); z-index: 10000; min-width: 320px; max-width: none; opacity: 0; pointer-events: none; transition: all 0.35s cubic-bezier(0.34, 1.56, 0.64, 1); }
        #timer-overlay.show { opacity: 1; transform: translate(-50%, -50%) scale(calc(1 * var(--preview-scale, 1))); pointer-events: auto; animation: overlayPulse 1.5s ease-in-out infinite; }
        @keyframes overlayPulse { 0%, 100% { box-shadow: 0 25px 60px rgba(168,85,247,0.5), 0 0 0 2px rgba(168,85,247,0.4); } 50% { box-shadow: 0 25px 70px rgba(236,72,153,0.65), 0 0 0 2px rgba(236,72,153,0.5); } }
        #timer-overlay .overlay-badge { display: inline-block; padding: 4px 12px; border-radius: 16px; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; background: rgba(236,72,153,0.2); color: #f9a8d4; margin-bottom: 12px; text-transform: uppercase; }
        #timer-overlay .overlay-burner { font-size: 14px; color: #cbd5e1; font-weight: 600; margin-bottom: 4px; }
        #timer-overlay .overlay-value { font-size: 56px; font-weight: 800; color: #fff; margin: 6px 0; font-variant-numeric: tabular-nums; text-shadow: 0 0 20px rgba(236,72,153,0.6); }
        #timer-overlay .overlay-hint { font-size: 12px; color: #94a3b8; margin-top: 8px; }
        #timer-overlay .overlay-close { position: absolute; top: 10px; right: 14px; background: none; border: none; color: #94a3b8; font-size: 22px; cursor: pointer; line-height: 1; padding: 4px; }
        #timer-overlay .overlay-close:hover { color: #fff; }
        #timer-overlay .overlay-list { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; max-height: 50vh; overflow-y: auto; }
        #timer-overlay .overlay-list .overlay-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: rgba(255,255,255,0.06); border-radius: 10px; }
        #timer-overlay .overlay-list .overlay-row .row-name { font-size: 13px; color: #cbd5e1; }
        #timer-overlay .overlay-list .overlay-row .row-time { font-size: 18px; font-weight: 700; color: #f9a8d4; font-variant-numeric: tabular-nums; }

        /* 菜谱列表流式布局 */
        .recipe-scroll { display: flex; overflow-x: auto; gap: 12px; padding: 10px 0; margin-top: 15px; scrollbar-width: none; scroll-behavior: smooth; }
        .recipe-scroll::-webkit-scrollbar { display: none; }
        .recipe-item { min-width: 130px; background: rgba(255,255,255,0.04); padding: 12px; border-radius: 14px; text-align: left; border: 1px solid rgba(255,255,255,0.06); cursor: pointer; transition: transform 0.2s ease, border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease; flex: 0 0 auto; }
        .recipe-item.selected { border-color: #4ade80; background: rgba(74,222,128,0.12); transform: scale(1.06); box-shadow: 0 8px 22px rgba(74,222,128,0.25); }
        .recipe-name { font-size: 13px; font-weight: bold; color: #fff; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .recipe-tag { font-size: 10px; color: #4ade80; background: rgba(74,222,128,0.1); padding: 2px 6px; border-radius: 6px; display: inline-block; }
        .kbd-hint { font-size: 11px; color: #64748b; margin-top: 10px; text-align: center; letter-spacing: 0.5px; }

        /* 快捷功能菜单（回车键弹出） */
        #quick-menu { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%) scale(calc(0.85 * var(--preview-scale, 1))); background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); padding: 24px 20px; border-radius: 24px; box-shadow: 0 20px 60px rgba(59,130,246,0.35), 0 0 0 2px rgba(59,130,246,0.3); z-index: 10001; min-width: 340px; max-width: 92vw; opacity: 0; pointer-events: none; transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1); }
        #quick-menu.show { opacity: 1; transform: translate(-50%, -50%) scale(calc(1 * var(--preview-scale, 1))); pointer-events: auto; }
        #quick-menu .qm-title { font-size: 13px; color: #93c5fd; font-weight: 700; text-align: center; margin-bottom: 16px; letter-spacing: 1px; text-transform: uppercase; }
        #quick-menu .qm-grid { display: flex; justify-content: space-between; gap: 10px; }
        #quick-menu .qm-item { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 14px 6px; border-radius: 14px; background: rgba(255,255,255,0.04); border: 1.5px solid rgba(255,255,255,0.06); cursor: pointer; transition: all 0.2s ease; }
        #quick-menu .qm-item.selected { border-color: #60a5fa; background: rgba(96,165,250,0.18); transform: translateY(-4px); box-shadow: 0 10px 24px rgba(96,165,250,0.3); }
        #quick-menu .qm-icon { font-size: 26px; line-height: 1; }
        #quick-menu .qm-label { font-size: 11px; color: #cbd5e1; font-weight: 600; }
        #quick-menu .qm-item.selected .qm-label { color: #fff; }
        #quick-menu .qm-hint { font-size: 11px; color: #64748b; text-align: center; margin-top: 14px; letter-spacing: 0.5px; }
        #quick-menu .qm-close { position: absolute; top: 8px; right: 12px; background: none; border: none; color: #64748b; font-size: 20px; cursor: pointer; line-height: 1; padding: 4px; }
        #quick-menu .qm-close:hover { color: #fff; }
        /* Figma D01: 魔术旋钮是完整的设备选择画面，而非小型弹窗。 */
        #quick-menu { box-sizing: border-box; width: 960px; min-width: 960px; max-width: 960px; height: 220px; padding: 14px 24px; color: #f5f2e8; background: #0e1110; border: 0; border-radius: 0; box-shadow: none; }
        #quick-menu .qm-header { display: flex; align-items: center; justify-content: space-between; min-height: 26px; }
        #quick-menu .qm-title { margin: 0; color: #f5f2e8; font-size: 22px; line-height: 26px; text-align: left; text-transform: none; letter-spacing: 0; }
        #quick-menu .qm-status { color: #8e9a90; font-size: 12px; letter-spacing: .02em; }
        #quick-menu .qm-grid { gap: 12px; margin-top: 14px; }
        #quick-menu .qm-item { box-sizing: border-box; height: 112px; padding: 16px 18px; align-items: flex-start; gap: 10px; color: #a3b2a6; background: #1d221f; border: 1px solid #364038; border-radius: 14px; transform: none; box-shadow: none; }
        #quick-menu .qm-item.selected { color: #213321; background: #c7dbad; border-color: #e5f5d1; transform: none; box-shadow: none; }
        #quick-menu .qm-icon { font-size: 12px; }
        #quick-menu .qm-label { color: inherit; font-size: 24px; line-height: 1; }
        #quick-menu .qm-item.selected .qm-label { color: inherit; }
        #quick-menu .qm-detail { color: inherit; font-size: 12px; }
        #quick-menu .qm-hint { margin-top: 9px; color: #adb8ad; font-size: 13px; }
        #quick-menu .qm-close { display: none; }

        /* 子卡片通用提示 */
        .sub-hint { font-size: 11px; color: #64748b; text-align: center; margin-top: 14px; letter-spacing: 0.5px; }
        .sub-hint kbd { display: inline-block; padding: 1px 6px; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15); border-radius: 4px; font-family: monospace; color: #cbd5e1; margin: 0 2px; }

        /* WiFi 子卡片 */
        .wifi-wrap { display: flex; flex-direction: column; align-items: center; padding: 10px 0; }
        .wifi-icon { font-size: 56px; margin-bottom: 10px; }
        .wifi-status { font-size: 22px; font-weight: 800; margin-bottom: 16px; }
        .wifi-status.on { color: #4ade80; }
        .wifi-status.off { color: #94a3b8; }
        .wifi-toggle { display: flex; gap: 8px; }
        .wifi-toggle .toggle-opt { padding: 8px 24px; border-radius: 12px; font-size: 14px; font-weight: 700; background: rgba(255,255,255,0.05); color: #64748b; border: 1.5px solid rgba(255,255,255,0.08); }
        .wifi-toggle .toggle-opt.active { background: rgba(74,222,128,0.18); color: #4ade80; border-color: #4ade80; }

        /* 照明子卡片 */
        .light-wrap { display: flex; flex-direction: column; align-items: center; padding: 10px 0; }
        .light-icon { font-size: 56px; margin-bottom: 14px; transition: opacity 0.2s ease; }
        .light-bar-bg { width: 80%; height: 10px; background: rgba(255,255,255,0.08); border-radius: 6px; overflow: hidden; margin-bottom: 10px; }
        .light-bar-fill { height: 100%; background: linear-gradient(90deg, #fbbf24, #f59e0b); border-radius: 6px; transition: width 0.2s ease; }
        .light-value { font-size: 28px; font-weight: 800; color: #fbbf24; }

        /* 定时设置子卡片 */
        .timer-setup-wrap { display: flex; flex-direction: column; align-items: center; padding: 10px 0; }
        .timer-setup-icon { font-size: 48px; margin-bottom: 10px; }
        .timer-setup-value { font-size: 52px; font-weight: 800; color: #fff; margin-bottom: 14px; }
        .timer-setup-value span { font-size: 16px; color: #94a3b8; margin-left: 6px; font-weight: 600; }
        .timer-setup-bar { width: 80%; height: 8px; background: rgba(255,255,255,0.08); border-radius: 6px; overflow: hidden; }
        .timer-setup-fill { height: 100%; background: linear-gradient(90deg, #60a5fa, #3b82f6); border-radius: 6px; transition: width 0.2s ease; }
        .kbd-hint kbd { display: inline-block; padding: 1px 6px; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15); border-radius: 4px; font-family: monospace; color: #cbd5e1; margin: 0 2px; }

        /* 菜谱步骤卡片 */
        .recipe-steps-wrap { display: flex; flex-direction: column; align-items: center; padding: 8px 0; }
        .step-progress { font-size: 14px; color: #94a3b8; margin-bottom: 12px; }
        .step-progress .step-cur { font-size: 22px; font-weight: 800; color: #60a5fa; }
        .step-card-long {
            width: 100%;
            background: linear-gradient(135deg, rgba(59,130,246,0.12), rgba(147,51,234,0.08));
            border: 1.5px solid rgba(96,165,250,0.3);
            border-radius: 14px;
            padding: 18px 16px;
            display: flex;
            align-items: flex-start;
            gap: 14px;
            margin-bottom: 14px;
            min-height: 80px;
            box-shadow: 0 4px 20px rgba(59,130,246,0.1);
        }
        .step-num {
            flex-shrink: 0;
            width: 36px; height: 36px;
            border-radius: 50%;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            color: #fff;
            font-size: 18px; font-weight: 800;
            display: flex; align-items: center; justify-content: center;
            box-shadow: 0 2px 8px rgba(59,130,246,0.4);
        }
        .step-text { font-size: 15px; line-height: 1.6; color: #e2e8f0; flex: 1; padding-top: 4px; }
        .step-dots { display: flex; gap: 6px; margin-bottom: 8px; }
        .step-dot { width: 8px; height: 8px; border-radius: 50%; background: rgba(255,255,255,0.15); transition: all 0.2s; }
        .step-dot.active { background: #60a5fa; width: 20px; border-radius: 4px; }

        /* 查看步骤按钮 */
        .view-steps-btn {
            display: inline-block;
            margin-top: 8px;
            padding: 4px 12px;
            font-size: 11px;
            background: rgba(96,165,250,0.15);
            color: #60a5fa;
            border: 1px solid rgba(96,165,250,0.3);
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.15s;
        }
        .view-steps-btn:hover { background: rgba(96,165,250,0.3); }

        /* Apple-inspired visual system: quiet materials, a single blue accent, and clear hierarchy. */
        :root {
            --apple-blue: #0a84ff;
            --apple-blue-soft: rgba(10,132,255,.12);
            --apple-green: #30d158;
            --apple-orange: #ff9f0a;
            --ink: #f5f5f7;
            --secondary: rgba(235,235,245,.60);
            --tertiary: rgba(235,235,245,.38);
            --surface: rgba(44,44,46,.72);
            --hairline: rgba(255,255,255,.12);
        }
        html { background: #fff; }
        body {
            color: var(--ink);
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", "Helvetica Neue", sans-serif;
            letter-spacing: -.01em;
            background: #fff;
        }
        .container {
            box-sizing: border-box;
            padding: 0;
            gap: 0;
            background: #000;
            border: 0;
            box-shadow: none;
        }
        .card {
            box-sizing: border-box;
            padding: 34px 56px;
            border-radius: 30px;
            background: linear-gradient(145deg, rgba(62,62,66,.84), rgba(28,28,30,.86));
            border: 1px solid var(--hairline);
            box-shadow: 0 22px 60px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.08);
            backdrop-filter: blur(28px) saturate(125%);
            -webkit-backdrop-filter: blur(28px) saturate(125%);
        }
        .card::before {
            left: 50%; top: 0; right: auto; bottom: auto;
            width: 72px; height: 4px;
            border-radius: 0 0 8px 8px;
            transform: translateX(-50%);
            background: var(--apple-blue) !important;
        }
        .badge {
            align-self: center;
            padding: 5px 11px;
            margin-bottom: 12px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: .02em;
            text-transform: none;
            color: #9acbff !important;
            background: var(--apple-blue-soft) !important;
        }
        .title { color: var(--secondary); font-size: 20px; font-weight: 500; margin-bottom: 8px; }
        .value { color: var(--ink); font-size: 52px; font-weight: 700; letter-spacing: -.045em; }
        .desc { color: var(--secondary); font-size: 16px; }
        .avatar-box {
            width: 80px; height: 80px; margin: 2px auto 14px;
            background: linear-gradient(145deg, rgba(255,255,255,.14), rgba(255,255,255,.05));
            border: 1px solid rgba(255,255,255,.16);
            box-shadow: inset 0 1px 0 rgba(255,255,255,.12);
            animation: none;
        }
        .timer-list { gap: 10px; margin-top: 8px; }
        .timer-item, .recipe-item, #quick-menu .qm-item {
            background: rgba(255,255,255,.055);
            border: 1px solid rgba(255,255,255,.10);
            border-radius: 16px;
            box-shadow: none;
        }
        .timer-item { padding: 12px 16px; }
        .timer-item.active, .recipe-item.selected, #quick-menu .qm-item.selected {
            border-color: rgba(10,132,255,.72);
            background: rgba(10,132,255,.14);
            box-shadow: 0 0 0 3px rgba(10,132,255,.14);
        }
        .timer-item.done { border-color: rgba(48,209,88,.62); background: rgba(48,209,88,.11); }
        .timer-item.done .value, .timer-item.done .timer-status { color: var(--apple-green); }
        .burner-name, .recipe-name { color: var(--ink); }
        .timer-status, .fan-hint, .kbd-hint, .sub-hint, .step-progress { color: var(--tertiary); }
        .recipe-tag { color: #89e7a1; background: rgba(48,209,88,.12); }
        .recipe-item.selected { transform: none; }
        /* Recipe rail follows the supplied Figma reference: equal, light cards with three clear text levels. */
        .recipe-scroll {
            justify-content: flex-start;
            gap: 28px;
            padding: 14px 2px 16px;
            margin-top: 6px;
        }
        .recipe-item {
            box-sizing: border-box;
            min-width: 300px;
            min-height: 168px;
            padding: 17px 22px 16px;
            color: #1d1d1f;
            background: #e5e5e7;
            border: 1px solid rgba(255,255,255,.58);
            border-radius: 26px;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.7);
            display: flex;
            flex: 0 0 300px;
            flex-direction: column;
            text-align: left;
        }
        .recipe-item.selected {
            background: #f5f5f7;
            border-color: var(--apple-blue);
            box-shadow: 0 0 0 3px rgba(10,132,255,.28), inset 0 1px 0 rgba(255,255,255,.9);
        }
        .recipe-item .recipe-tag {
            align-self: center;
            padding: 0;
            color: #3a3a3c;
            background: transparent;
            font-size: 15px;
            font-weight: 500;
            line-height: 20px;
        }
        .recipe-name {
            margin: auto 0 12px;
            color: #1d1d1f;
            font-size: 27px;
            line-height: 1.15;
            font-weight: 600;
            letter-spacing: -.045em;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .recipe-meta {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            color: #3a3a3c;
            font-size: 16px;
            line-height: 20px;
            font-weight: 500;
        }
        .recipe-item .view-steps-btn {
            margin: 0;
            padding: 0;
            color: #1d1d1f;
            background: transparent;
            border: 0;
            border-radius: 0;
            font: inherit;
            white-space: nowrap;
        }
        .recipe-item .view-steps-btn:hover { color: var(--apple-blue); background: transparent; }
        /* When cooking guidance and a running burner coexist, use the reference's left-timer / centered-step framework. */
        .recipe-steps-layout {
            width: 100%;
            display: grid;
            grid-template-columns: 264px minmax(0, 1fr);
            align-items: center;
            gap: 46px;
        }
        .recipe-steps-layout.no-timers { grid-template-columns: 1fr; }
        .steps-timer-rail { display: flex; justify-content: center; }
        .steps-timer-card {
            box-sizing: border-box;
            width: 232px;
            min-height: 152px;
            padding: 18px;
            color: var(--ink);
            background: linear-gradient(145deg, rgba(10,132,255,.20), rgba(44,44,46,.88));
            border: 1px solid rgba(255,255,255,.16);
            border-radius: 25px;
            box-shadow: 0 0 0 3px rgba(10,132,255,.10), inset 0 1px 0 rgba(255,255,255,.10);
            text-align: center;
        }
        .steps-timer-label { color: var(--secondary); font-size: 14px; font-weight: 500; }
        .steps-timer-value { margin: 12px 0 9px; color: var(--ink); font-size: 39px; line-height: 1; font-weight: 700; letter-spacing: -.05em; font-variant-numeric: tabular-nums; }
        .steps-timer-finish { color: var(--secondary); font-size: 13px; }
        .recipe-steps-layout .recipe-steps-wrap { width: auto; justify-self: center; max-width: 700px; }
        .recipe-steps-layout.no-timers .recipe-steps-wrap { grid-column: 1; }
        .recipe-steps-layout .step-card-long { min-height: 0; margin: 12px 0 16px; padding: 0; justify-content: center; align-items: center; background: transparent; border: 0; box-shadow: none; }
        .recipe-steps-layout .step-num { width: 42px; height: 42px; font-size: 18px; }
        .recipe-steps-layout .step-text { max-width: 620px; padding: 0; color: var(--ink); font-size: 34px; line-height: 1.3; font-weight: 600; letter-spacing: -.045em; }
        /* Transparent presentation: retain hierarchy through type, borders, and selection—not filled panels. */
        .card, .timer-item, .recipe-item, .steps-timer-card, .step-card-long {
            background: transparent !important;
            backdrop-filter: none;
            -webkit-backdrop-filter: none;
        }
        .card { box-shadow: none; }
        .timer-item.active, .recipe-item.selected { background: transparent !important; }
        /* Recipe view is content-led: remove diagnostic state and its outer framing. */
        #badge { display: none !important; }
        #main-card.recipe_list { border: 0; border-radius: 0; box-shadow: none; }
        #main-card.recipe_list::before { display: none; }
        /* Recipe recommendations: sage selection, compact information hierarchy, and a three-card rail. */
        .recipe-list-context, .recipe-list-footer { display: flex; align-items: center; justify-content: space-between; padding: 0 2px; font-size: 14px; line-height: 1.25; }
        .recipe-list-context { margin: 2px 0 8px; color: #9cad9e; }
        .recipe-list-counter { color: #bac9b2; font-variant-numeric: tabular-nums; }
        .recipe-scroll.recipe-recommendations { gap: 12px; margin: 0; padding: 0; }
        .recipe-scroll.recipe-recommendations .recipe-item { box-sizing: border-box; width: 296px; min-width: 296px; height: 128px; min-height: 128px; padding: 12px 18px; gap: 5px; color: #f5f2e8; background: #1d221f !important; border: 1px solid #364038; border-radius: 14px; box-shadow: none; transform: none; transition: border-color .2s ease, background .2s ease, transform .2s ease; }
        .recipe-scroll.recipe-recommendations .recipe-item:hover { transform: translateY(-2px); border-color: #627161; }
        .recipe-scroll.recipe-recommendations .recipe-item.selected { color: #213321; background: #c7dbad !important; border-color: #e5f5d1; box-shadow: none; }
        .recipe-scroll.recipe-recommendations .recipe-card-index { color: #a3b2a6; font-size: 11px; font-weight: 500; line-height: 1.2; }
        .recipe-scroll.recipe-recommendations .selected .recipe-card-index { color: #405441; }
        .recipe-scroll.recipe-recommendations .recipe-name { margin: 0; color: #f5f2e8; font-size: 24px; line-height: 1.15; font-weight: 650; letter-spacing: -.04em; }
        .recipe-scroll.recipe-recommendations .selected .recipe-name { color: #213321; }
        .recipe-card-description { overflow: hidden; color: #a3b2a6; font-size: 12px; line-height: 1.25; white-space: nowrap; text-overflow: ellipsis; }
        .recipe-scroll.recipe-recommendations .selected .recipe-card-description { color: #405441; }
        .recipe-scroll.recipe-recommendations .recipe-meta { margin-top: auto; color: #a3b2a6; font-size: 12px; line-height: 1.2; font-weight: 400; }
        .recipe-scroll.recipe-recommendations .selected .recipe-meta { color: #405441; }
        .recipe-scroll.recipe-recommendations .recipe-item .view-steps-btn { color: inherit; font-size: 12px; font-weight: 500; }
        .recipe-list-footer { margin-top: 12px; color: #adb8ad; }
        .recipe-list-footer .recipe-return { color: #dbe3d6; }
        /* Shared card framework from the reference: timer cards use the same compact, horizontal rhythm. */
        .timer-list {
            flex-direction: row;
            justify-content: center;
            gap: 24px;
            margin: 10px 0 4px;
        }
        .timer-item {
            box-sizing: border-box;
            min-width: 264px;
            min-height: 166px;
            padding: 18px 22px;
            background: rgba(44,44,46,.78);
            border: 1px solid rgba(255,255,255,.14);
            border-radius: 26px;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.10);
            display: flex;
            flex: 0 0 264px;
            flex-direction: column;
            align-items: center;
        }
        .timer-item.active {
            background: linear-gradient(145deg, rgba(10,132,255,.22), rgba(44,44,46,.88));
            border-color: rgba(10,132,255,.75);
            box-shadow: 0 0 0 3px rgba(10,132,255,.16), inset 0 1px 0 rgba(255,255,255,.10);
        }
        .timer-item.done { background: rgba(48,209,88,.12); }
        .burner-name {
            color: var(--secondary);
            font-size: 15px;
            line-height: 20px;
            font-weight: 500;
            letter-spacing: 0;
        }
        .timer-item .value {
            margin: auto 0 10px;
            color: var(--ink);
            font-size: 42px;
            font-weight: 700;
            letter-spacing: -.04em;
        }
        .timer-status { color: var(--secondary); font-size: 14px; line-height: 18px; }
        .fan-arc.active { filter: drop-shadow(0 0 5px rgba(10,132,255,.55)); }
        .fan-speed-value, .light-value { color: #ffd60a; }
        .light-bar-fill { background: #ffd60a; }
        .timer-setup-fill { background: var(--apple-blue); }
        .step-card-long {
            background: rgba(10,132,255,.10);
            border: 1px solid rgba(10,132,255,.26);
            border-radius: 18px;
            box-shadow: none;
        }
        .step-num { background: var(--apple-blue); box-shadow: none; }
        .step-text { color: var(--ink); }
        .step-dot.active { background: var(--apple-blue); }
        .view-steps-btn { color: #9acbff; background: var(--apple-blue-soft); border-color: rgba(10,132,255,.30); border-radius: 999px; }
        .view-steps-btn:hover { background: rgba(10,132,255,.24); }
        #quick-menu, #timer-overlay {
            background: rgba(44,44,46,.92);
            border: 1px solid rgba(255,255,255,.14);
            border-radius: 26px;
            box-shadow: 0 28px 70px rgba(0,0,0,.52);
            backdrop-filter: blur(34px) saturate(135%);
            -webkit-backdrop-filter: blur(34px) saturate(135%);
        }
        #quick-menu .qm-title, #timer-overlay .overlay-badge { color: #9acbff; }
        #timer-overlay .overlay-value { text-shadow: none; }
        #timer-overlay.show { animation: none; }
        .done-banner { background: rgba(48,209,88,.94); box-shadow: 0 14px 34px rgba(0,0,0,.30); }
        .sub-hint kbd, .kbd-hint kbd {
            padding: 2px 6px;
            border-radius: 5px;
            color: var(--secondary);
            background: rgba(255,255,255,.08);
            border-color: rgba(255,255,255,.12);
        }
        /* Figma B02: one focused step panel, with an optional circular timing aid. */
        #main-card.recipe_steps { justify-content: flex-start; padding: 24px; background: transparent !important; border: 0; border-radius: 0; }
        #main-card.recipe_steps::before { display: none; }
        #main-card.recipe_steps .title { margin: 0 0 18px; color: #f5f2e8; font-size: 22px; font-weight: 600; line-height: 1; text-align: left; }
        .step-guide { position: relative; width: 912px; max-width: 100%; }
        .step-page-progress { position: absolute; top: -37px; right: 0; color: #bac9b2; font-size: 13px; font-variant-numeric: tabular-nums; }
        .step-panel { box-sizing: border-box; display: flex; align-items: stretch; gap: 24px; width: 100%; min-height: 128px; padding: 12px 18px; color: #f5f2e8; background: #1d221f !important; border: 1px solid #364038; border-radius: 14px; }
        .step-copy { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 9px; text-align: left; }
        .step-index-label { color: #a3b2a6; font-size: 11px; font-weight: 500; line-height: 1.25; }
        .step-panel .step-text { max-width: none; color: #f5f2e8; font-size: 24px; line-height: 1.25; font-weight: 600; letter-spacing: -.03em; }
        .step-navigation { display: flex; justify-content: space-between; margin-top: auto; color: #c7dbad; font-size: 13px; }
        .step-timer-gauge { box-sizing: border-box; display: flex; width: 104px; height: 104px; flex: 0 0 104px; flex-direction: column; align-items: center; justify-content: center; align-self: center; color: #f5f2e8; background: radial-gradient(circle at center, #1d221f 58%, transparent 59%), conic-gradient(#c7dbad 0 72%, #596657 72% 100%); border-radius: 50%; }
        .step-timer-gauge span { color: #a3b2a6; font-size: 11px; }
        .step-timer-gauge strong { margin-top: 3px; font-size: 17px; font-variant-numeric: tabular-nums; }
        .step-shutoff-card { box-sizing: border-box; display: flex; width: 128px; min-width: 128px; min-height: 88px; flex-direction: column; justify-content: space-between; padding: 11px 12px; color: #f5f2e8; background: #1d221f; border: 1px solid #364038; border-radius: 14px; text-align: left; }
        .step-shutoff-card .burner { color: #a3b2a6; font-size: 12px; }
        .step-shutoff-card strong { font-size: 18px; font-variant-numeric: tabular-nums; letter-spacing: -.03em; white-space: nowrap; }
        .step-shutoff-card small { color: #a3b2a6; font-size: 12px; }
        .step-shutoff-card.done { border-color: #c7dbad; }
        .step-shutoff-card.done small { color: #c7dbad; }
        .step-guide-footer { margin-top: 13px; color: #adb8ad; font-size: 13px; text-align: center; }
        /* 960×220 compact canvas: keep every screen's primary content inside the display window. */
        #main-card.recipe_steps { padding: 14px 24px; }
        #main-card.recipe_steps .title { margin-bottom: 12px; }
        .step-panel { min-height: 112px; }
        .step-timer-gauge { width: 88px; height: 88px; flex-basis: 88px; }
        .step-task-layout { display: flex; align-items: stretch; gap: 12px; width: 100%; }
        .step-task-layout .step-panel { width: auto; flex: 1 1 auto; }
        .step-task-layout .step-shutoff-card { width: 152px; min-width: 152px; min-height: 112px; }
        .step-guide-footer { margin-top: 8px; font-size: 12px; }
        #main-card.recipe_list { justify-content: flex-start; padding: 14px 24px; }
        #main-card.recipe_list { box-sizing:border-box; height:220px; padding:12px 24px 8px; overflow:hidden; }
        #main-card.recipe_list .title { margin: 0 0 5px; font-size:20px; line-height:24px; }
        .recipe-list-context { margin: 0 0 4px; font-size:12px; }
        .recipe-scroll.recipe-recommendations .recipe-item { height:104px; min-height:104px; padding:9px 14px; gap:3px; }
        .recipe-scroll.recipe-recommendations .recipe-name { font-size:21px; line-height:1.1; }
        .recipe-scroll.recipe-recommendations .recipe-card-description { font-size:11px; }
        .recipe-list-footer { margin-top:5px; font-size:11px; }
        #main-card.timer { padding: 16px 24px; }
        .timer-item { min-height: 132px; padding: 14px 18px; }
        /* Keep D01 on the same 960×220 scale as the display canvas. */
        body #quick-menu { transform: translate(-50%, -50%) scale(var(--preview-scale, 1)); background: #0e1110; border: 0; border-radius: 0; box-shadow: none; backdrop-filter: none; -webkit-backdrop-filter: none; }
        body #quick-menu.show { transform: translate(-50%, -50%) scale(var(--preview-scale, 1)); }
        body #quick-menu .qm-item { background: #1d221f; border-color: #364038; }
        body #quick-menu .qm-item.selected { color: #213321; background: #c7dbad; border-color: #e5f5d1; box-shadow: none; }
        body #quick-menu .qm-title { color: #f5f2e8; }
        /* Figma D02–D04: compact device task screens on the 960×220 canvas. */
        #main-card.timer-sub, #main-card.magic-sub, #main-card.fan-sub, #main-card.light-sub, #main-card.temp-sub, #main-card.steam-sub, #main-card.wifi-sub { padding: 0; background: #0e1110; border: 0; border-radius: 0; box-shadow: none; }
        #main-card.timer-sub::before, #main-card.magic-sub::before, #main-card.fan-sub::before, #main-card.light-sub::before, #main-card.temp-sub::before, #main-card.steam-sub::before, #main-card.wifi-sub::before { display: none; }
        #main-card.timer-sub .title, #main-card.magic-sub .title, #main-card.light-sub .title, #main-card.fan-sub .title, #main-card.wifi-sub .title, #main-card.steam-sub .title, #main-card.temp-sub .title { display: none; }
        #main-card.fan-sub #title { display: none !important; }
        .device-task-screen { position: relative; box-sizing: border-box; width: 100%; height: 220px; padding: 18px 86px 0; color: #f5f2e8; text-align: center; }
        .device-task-title { font-size: 22px; font-weight: 600; line-height: 1.2; }
        .device-task-value { margin-top: 22px; font-size: 48px; font-weight: 650; line-height: 1; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
        .device-task-value span { margin-left: 8px; font-size: 28px; letter-spacing: 0; }
        .device-task-track { height: 4px; margin-top: 22px; overflow: hidden; background: #4c5950; }
        .device-task-track i { display: block; height: 100%; background: #c7dbad; }
        .device-task-meta { margin-top: 12px; color: #adb8ad; font-size: 15px; }
        .device-task-hint { position: absolute; right: 0; bottom: 11px; left: 0; color: #adb8ad; font-size: 13px; }
        .hood-choice-screen { box-sizing:border-box; width:100%; height:220px; padding:14px 24px; color:#f5f2e8; }.hood-choice-header{display:flex;justify-content:space-between;font-size:13px;color:#9cad9e}.hood-choice-header strong{color:#f5f2e8;font-size:22px}.hood-choice-grid{display:flex;gap:12px;margin-top:14px}.hood-choice{box-sizing:border-box;flex:1;height:112px;padding:16px 18px;border:1px solid #364038;border-radius:14px;background:#1d221f;color:#a3b2a6;text-align:left}.hood-choice.selected{background:#c7dbad;border-color:#e5f5d1;color:#213321}.hood-choice b{display:block;margin:9px 0;font-size:22px;color:inherit}.hood-choice small{font-size:12px}.hood-segments{display:flex;justify-content:center;gap:12px;margin-top:22px}.hood-segments i{width:52px;height:4px;background:#344038}.hood-segments i.on{background:#c7dbad}
        .steam-steps { display:flex; justify-content:center; gap:28px; margin-top:34px; }.steam-steps span{width:175px;padding:38px 0;border:1px solid #404a42;border-radius:14px;color:#adb8ad;font-size:22px}.steam-steps span.active{background:#c7dbad;color:#213321}
        /* 待机页：左侧问候，居中播放表情动效。 */
        #main-card.emotion { position: relative; box-sizing: border-box; width: 960px; height: 220px; padding: 0; overflow: hidden; background: #000; border: 0; border-radius: 0; box-shadow: none; }
        #main-card.emotion #badge { display: none; }
        #main-card.emotion #title { position: absolute; z-index: 1; top: 78px; left: 32px; margin: 0; color: #f5f2e8; font-size: 26px; font-weight: 650; line-height: 1.15; text-align: left; }
        #main-card.emotion #dynamic-content { width: 100%; height: 100%; }
        #main-card.emotion .avatar-box { position: absolute; top: 50%; left: 50%; width: 191px; height: 190px; margin: 0; padding: 0; overflow: hidden; background: transparent; border: 0; border-radius: 0; box-shadow: none; transform: translate(-50%, -50%); animation: none; }
        #main-card.emotion .expression-video { display: block; width: 100%; height: 100%; object-fit: contain; }
        #main-card.emotion .desc { position: absolute; z-index: 1; top: 114px; left: 32px; max-width: 260px; margin: 0; color: #adb8ad; font-size: 14px; line-height: 1.45; text-align: left; }
        /* 默认页设备状态：参照待机表情两侧的灶具信息布局。 */
        #main-card.emotion.has-appliance-status #title,
        #main-card.emotion.has-appliance-status .desc { display: none; }
        #main-card.emotion:not(.is-greeting) #title,
        #main-card.emotion:not(.is-greeting) .desc { display: none; }
        #main-card.emotion.is-greeting.has-appliance-status #title,
        #main-card.emotion.is-greeting.has-appliance-status .desc { display: block; }
        #main-card.emotion.is-greeting .standby-appliance-cards { display: none; }
        .standby-ambient-info { position:absolute; inset:0; color:#a3b2a6; pointer-events:none; }
        .standby-date-time { position:absolute; top:81px; left:162px; }
        .standby-date-time .date { font-size:13px; letter-spacing:.04em; }
        .standby-date-time .time { margin-top:8px; color:#f5f2e8; font-size:34px; font-weight:600; line-height:1; font-variant-numeric:tabular-nums; }
        .clock-colon { animation:clockColonBlink 1s steps(1, end) infinite; }
        @keyframes clockColonBlink { 50% { opacity:.22; } }
        .standby-pm { position:absolute; top:81px; right:162px; min-width:118px; text-align:right; }
        .standby-pm .label { color:#65c061; font-size:12px; font-weight:600; }
        .standby-pm strong { display:block; margin-top:8px; color:#65c061; font-size:42px; font-weight:600; line-height:1; font-variant-numeric:tabular-nums; }
        #main-card.emotion.has-appliance-status .standby-ambient-info,
        #main-card.emotion.is-greeting .standby-ambient-info { display:none; }
        .standby-appliance-cards { position:absolute; inset:0; pointer-events:none; }
        .standby-appliance-group { position:absolute; top:54px; display:flex; gap:12px; }
        .standby-appliance-group.left { left:24px; }
        .standby-appliance-group.right { right:18px; }
        .standby-appliance-card { box-sizing:border-box; display:flex; flex-direction:column; gap:13px; width:173px; height:128px; padding:16px 18px; border:1px solid #364038; border-radius:14px; background:#1d221f; color:#a3b2a6; }
        .standby-appliance-card .burner { display:block; font-size:12px; line-height:1.25; }
        .standby-appliance-card strong { display:block; margin:0; color:#65c061; font-size:22px; font-weight:600; line-height:1.25; letter-spacing:.035em; font-variant-numeric:tabular-nums; }
        .standby-appliance-card small { display:block; margin:0; color:#a3b2a6; font-size:12px; line-height:1.25; }
        .standby-appliance-card.temperature strong { letter-spacing:0; }
        .standby-appliance-card.temperature strong { color:#65c061; font-size:22px; }
        .standby-appliance-card.temperature small { color:#a3b2a6; }
        .standby-burner-idle { margin-top:43px; color:#7d8b7f; font-size:13px; line-height:1.3; white-space:nowrap; }
        .standby-burner-idle small { display:block; margin-top:6px; color:#5f6b62; font-size:11px; }
        #mcp-setup { display:none; position:absolute; inset:0; z-index:120; box-sizing:border-box; padding:32px 72px; background:#000; color:#f5f2e8; }
        #mcp-setup.show { display:block; }
        #mcp-setup h1 { margin:0 0 8px; font-size:25px; font-weight:650; }
        #mcp-setup p { margin:0 0 18px; color:#a3b2a6; font-size:13px; }
        #mcp-endpoint { box-sizing:border-box; width:100%; height:40px; padding:0 13px; border:1px solid #3b473e; border-radius:10px; outline:none; background:#171b18; color:#eff5eb; font-size:13px; }
        #mcp-endpoint:focus { border-color:#72c66d; box-shadow:0 0 0 2px rgba(101,192,97,.18); }
        #mcp-save { float:right; margin-top:12px; padding:7px 18px; border:0; border-radius:15px; background:#65c061; color:#0b160c; font-size:13px; font-weight:650; cursor:pointer; }
        #mcp-status { display:inline-block; margin-top:17px; color:#a3b2a6; font-size:12px; }
        /* 锅与火形适配：遵循设计稿的中央助手 + 左右匹配卡片布局。 */
        #pot-fire-match { display:none; position:absolute; inset:0; z-index:80; color:#f5f2e8; pointer-events:none; }
        #pot-fire-match.show { display:block; }
        #pot-fire-match.show ~ #main-card .standby-ambient-info,
        #pot-fire-match.show ~ #main-card #title,
        #pot-fire-match.show ~ #main-card .desc { display:none !important; }
        .pot-fire-card { position:absolute; top:54px; box-sizing:border-box; width:173px; height:128px; padding:16px 18px; overflow:hidden; border:1px solid #364038; border-radius:14px; background:#1d221f; }
        .pot-fire-card.left { left:137px; }
        .pot-fire-card.right { right:137px; }
        #pot-fire-match.left-active .pot-fire-card.right,
        #pot-fire-match.right-active .pot-fire-card.left { display:none; }
        .pot-fire-card-label { color:#a3b2a6; font-size:12px; line-height:1.25; }
        .pot-fire-card-label strong { color:#dcebd4; font-weight:600; }
        .pot-fire-image { display:block; width:137px; height:66px; margin-top:9px; object-fit:cover; border-radius:7px; }
        .pot-fire-dots { position:absolute; right:16px; top:17px; display:flex; gap:4px; }
        .pot-fire-dot { width:4px; height:4px; border-radius:50%; background:#526056; }
        .pot-fire-dot.active { background:#72c66d; box-shadow:0 0 5px rgba(114,198,109,.65); }
    </style>
    <script>
        let countdownTimer = null;
        let stepTimerTicker = null;
        let lastTargetTimestamp = 0;
        let lastDigits = "----";
        let selectedRecipeIndex = 0;   // 当前选中的菜谱索引
        let currentRecipeCount = 0;    // 当前菜谱总数
        let lastItemsJson = "";        // 上次菜谱数据快照，用于变更检测
        let lastRecipeValue = "";      // 上次菜谱卡片描述文字
        let renderedTimersKey = "";    // 已渲染的定时器列表快照，用于变更检测
        let notifiedDoneSet = new Set(); // 已通知过的完成定时器名集合
        let activeBurnerIndex = 0;     // 当前高亮查看的灶具索引（左右键切换）
        let overlayTicker = null;      // overlay 倒计时定时器
        let overlayUserDismissed = new Set(); // 用户手动关闭过的灶具名（同一灶具不再自动弹出，直到完成）
        const OVERLAY_THRESHOLD_SEC = 60; // 最后 60 秒触发 overlay
        let lastCardType = null;           // 上一次的卡片类型，用于检测语音指令变化
        let lastEmotion = "";              // 上一次的 emotion 值，用于检测表情变化触发重渲染
        let lastEmotionValue = "";         // 待机文案变化也必须重绘
        let forceEmotionRender = false;     // 旋钮调节页退出时强制恢复默认页 DOM
        let lastStandbyApplianceKey = "";  // 默认页灶具状态变化触发重绘
        let standbyStatusTicker = null;     // 默认页关火倒计时刷新器
        let settingAutoCloseTimer = null;   // 语音参数页 5 秒后自动回到待机
        let lastAppliedRevision = -1;       // 丢弃晚到的旧状态，避免语音与屏幕回退
        let overlayPriorityUntil = 0;      // 主卡片优先期截止时间戳（ms），期间 overlay 不自动弹出
        let timerAutoCloseScheduled = false; // 定时卡片自动关闭调度标志（避免重复 setTimeout）
        let lastStepKey = "";              // 上次步骤卡片快照（标题+索引+总数），用于变更检测
        const RECIPE_FINAL_STEP_IDLE_TIMEOUT_MS = 15000;
        let recipeAutoCloseScheduled = false; // 菜谱步骤最后一步无操作自动关闭调度标志
        let backendStepIndex = -1;         // 后端推送的菜谱步骤当前索引（用于旋钮按压判断最后一步）
        let backendStepTotal = 0;          // 后端推送的菜谱步骤总数

        // 仅更新发生变化的数字位，避免整体重渲染闪烁
        function setDigit(id, ch) {
            let el = document.getElementById(id);
            if (el && el.innerText !== ch) el.innerText = ch;
        }

        // 格式化秒数为 MM:SS
        function formatMmSs(totalSeconds) {
            let m = Math.floor(totalSeconds / 60);
            let s = totalSeconds % 60;
            return (m < 10 ? "0" + m : m) + ":" + (s < 10 ? "0" + s : s);
        }

        // 隐藏 overlay（用户主动关闭）
        function hideTimerOverlay() {
            let overlay = document.getElementById('timer-overlay');
            if (overlay) overlay.classList.remove('show');
            // 记录当前进入倒计时的灶具为已关闭，避免立刻再次弹出
            let timers = window.__currentTimers || [];
            let now = Date.now() / 1000;
            timers.forEach(t => {
                let remaining = Math.max(0, Math.floor(t.target_timestamp - now));
                if (remaining > 0 && remaining <= OVERLAY_THRESHOLD_SEC) {
                    overlayUserDismissed.add(t.name);
                }
            });
        }

        // 更新 overlay 内容（每秒调用）
        function updateTimerOverlay() {
            let timers = window.__currentTimers || [];
            let now = Date.now() / 1000;
            // 清理已完成灶具的 dismissed 状态，下次重新定时仍可弹出
            // 同时检测从 running → done 的过渡，触发完成横幅（无论主卡片是什么类型）
            timers.forEach(t => {
                let remaining = Math.max(0, Math.floor(t.target_timestamp - now));
                // 步骤画面左侧的紧凑计时器，与全局倒计时共用同一个时钟。
                document.querySelectorAll('[data-step-timer="' + t.name + '"] .steps-timer-value').forEach(el => {
                    let value = formatMmSs(remaining);
                    if (el.innerText !== value) el.innerText = value;
                });
                if (remaining === 0) {
                    if (overlayUserDismissed.has(t.name)) overlayUserDismissed.delete(t.name);
                    // 完成检测：尚未通知过则弹横幅
                    if (!notifiedDoneSet.has(t.name) && !t.done_notified) {
                        notifiedDoneSet.add(t.name);
                        showDoneBanner(t.name, t.mode);
                        markTimerDoneNotified(t.name);
                    }
                } else {
                    // 灶具仍在运行：如果之前通知过完成，说明重新定了时，清除通知记录
                    if (notifiedDoneSet.has(t.name)) notifiedDoneSet.delete(t.name);
                }
            });

            // 自动关闭定时卡片：所有定时器都已完成（remaining=0）且当前主卡片是 timer
            let allDone = timers.length > 0 && timers.every(t => Math.max(0, Math.floor(t.target_timestamp - now)) === 0);
            if (allDone) {
                let mainCard = document.getElementById('main-card');
                if (mainCard && mainCard.classList.contains('timer') && !timerAutoCloseScheduled) {
                    timerAutoCloseScheduled = true;
                    // 延迟 3 秒关闭（让完成横幅展示一会儿）
                    setTimeout(() => {
                        // 再次确认所有定时器仍处于完成状态（期间没有重新定时）
                        let cur = window.__currentTimers || [];
                        let stillAllDone = cur.length > 0 && cur.every(t => Math.max(0, Math.floor(t.target_timestamp - now)) === 0);
                        if (stillAllDone) {
                            // 切回 emotion 卡片（自动关闭定时卡片）
                            fetch('/quick_card?card_type=emotion&title=' + encodeURIComponent('小美随时待命') + '&value=' + encodeURIComponent('定时已完成，祝您用餐愉快！'));
                        }
                        timerAutoCloseScheduled = false;
                    }, 3000);
                }
            }
            // 找出所有 running 且剩余 <= 60 秒、未被用户关闭的定时器
            let imminent = timers
                .map(t => ({ name: t.name, remaining: Math.max(0, Math.floor(t.target_timestamp - now)) }))
                .filter(x => x.remaining > 0 && x.remaining <= OVERLAY_THRESHOLD_SEC && !overlayUserDismissed.has(x.name))
                .sort((a, b) => a.remaining - b.remaining);

            let overlay = document.getElementById('timer-overlay');
            let content = document.getElementById('overlay-content');
            if (!overlay || !content) return;

            // 主卡片优先期：语音指令刚变化时，让用户先看清新卡片，overlay 不自动弹出
            if (Date.now() < overlayPriorityUntil) {
                overlay.classList.remove('show');
                return;
            }

            if (imminent.length === 0) {
                overlay.classList.remove('show');
                return;
            }

            // 渲染内容：单个时大显示，多个时列表显示
            let html = '';
            if (imminent.length === 1) {
                let item = imminent[0];
                html = `
                    <div class="overlay-burner">${item.name}</div>
                    <div class="overlay-value">${formatMmSs(item.remaining)}</div>
                `;
            } else {
                html = '<div class="overlay-list">';
                imminent.forEach(item => {
                    html += `
                        <div class="overlay-row">
                            <span class="row-name">${item.name}</span>
                            <span class="row-time">${formatMmSs(item.remaining)}</span>
                        </div>
                    `;
                });
                html += '</div>';
            }
            content.innerHTML = html;
            overlay.classList.add('show');

            // 振动提醒（移动端）
            if (navigator.vibrate && imminent.length > 0 && window.__lastOverlayVibrate !== Math.floor(Date.now() / 1000)) {
                window.__lastOverlayVibrate = Math.floor(Date.now() / 1000);
                navigator.vibrate(50);
            }
        }

        // 启动 overlay ticker（独立于主卡片 ticker，长期运行）
        function startOverlayTicker() {
            if (overlayTicker) return;
            overlayTicker = setInterval(updateTimerOverlay, 1000);
            updateTimerOverlay();
        }

        // 渲染单个定时器项（含按位刷新逻辑）
        function renderTimerItem(t, idx, isActive) {
            let now = Date.now() / 1000;
            let totalSeconds = Math.max(0, Math.floor(t.target_timestamp - now));
            let m = Math.floor(totalSeconds / 60);
            let s = totalSeconds % 60;
            let digits =
                (m < 10 ? "0" + m : "" + m) +
                (s < 10 ? "0" + s : "" + s);
            let isDone = totalSeconds <= 0;
            let isReminder = t.mode === 'reminder';
            let taskLabel = isReminder ? '提醒计时' : '定时关火';
            let statusLabel = isDone ? (isReminder ? '✓ 提醒完成' : '✓ 定时完成、已关火') : (isReminder ? '到时提醒' : '后关火');
            let stateClass = isDone ? "timer-item done" : "timer-item";
            if (isActive) stateClass += " active";
            let mPrefix = 'tm' + idx;
            let sPrefix = 'ts' + idx;
            let finishAt = new Date(t.target_timestamp * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
            return `
                <div class="${stateClass}" data-index="${idx}" data-burner="${t.name}">
                    <div class="burner-name">${t.name} · ${taskLabel}</div>
                    <div class="value" id="val-${idx}">
                        <span class="digit" id="${mPrefix}t">${digits[0]}</span><span class="digit" id="${mPrefix}o">${digits[1]}</span><span class="digit-colon">:</span><span class="digit" id="${sPrefix}t">${digits[2]}</span><span class="digit" id="${sPrefix}o">${digits[3]}</span>
                    </div>
                    <div class="timer-status">${isDone ? statusLabel : statusLabel + ' · ' + finishAt}</div>
                </div>
            `;
        }

        function renderStepsTimerRail(timers) {
            let activeTimers = (timers || []).filter(t => Math.floor(t.target_timestamp - Date.now() / 1000) > 0);
            if (!activeTimers.length) return { className: 'no-timers', html: '' };
            let t = activeTimers[0];
            let remaining = Math.max(0, Math.floor(t.target_timestamp - Date.now() / 1000));
            let finishAt = new Date(t.target_timestamp * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
            return {
                className: 'has-timers',
                html: `<aside class="steps-timer-rail"><div class="steps-timer-card" data-step-timer="${t.name}"><div class="steps-timer-label">${t.name} · 定时器</div><div class="steps-timer-value">${formatMmSs(remaining)}</div><div class="steps-timer-finish">完成于 ${finishAt}</div></div></aside>`
            };
        }

        function renderStepGuide(index, total, text, phase, timers) {
            let reminderTimer = (timers || []).find(t => (t.mode === 'reminder') && Math.floor(t.target_timestamp - Date.now() / 1000) > 0);
            // 步骤页只显示仍在执行的左、右灶“定时关火”。
            // 已完成、普通提醒计时，或未指定灶具的任务都不占用灶具卡片位置。
            let shutoffTimers = (timers || []).filter(t =>
                (t.mode || 'shutoff') === 'shutoff'
                && (t.name === '左灶' || t.name === '右灶')
                && Math.floor(t.target_timestamp - Date.now() / 1000) > 0
            ).slice(0, 2);
            let gauge = '';
            if (reminderTimer) {
                let seconds = Math.max(0, Math.floor(reminderTimer.target_timestamp - Date.now() / 1000));
                gauge = `<aside class="step-timer-gauge" aria-label="提醒计时"><span>计时</span><strong>${formatMmSs(seconds)}</strong></aside>`;
            }
            let shutoffCards = shutoffTimers.map(t => {
                let remaining = Math.max(0, Math.floor(t.target_timestamp - Date.now() / 1000));
                return `<aside class="step-shutoff-card" aria-label="${t.name}定时关火"><span class="burner">${t.name}</span><strong>${formatMmSs(remaining)}</strong><small>后关火</small></aside>`;
            }).join('');
            let stepNo = String(index + 1).padStart(2, '0');
            return `<div class="step-guide"><div class="step-page-progress">步骤 ${stepNo} / ${String(total).padStart(2, '0')}</div><div class="step-task-layout"><section class="step-panel"><div class="step-copy"><div class="step-index-label">${stepNo}　/　${phase || '烹饪步骤'}</div><div class="step-text">${text}</div><div class="step-navigation"><span>上一步</span><span>下一步</span></div></div>${gauge}</section>${shutoffCards}</div><div class="step-guide-footer">${magicKnobBinding ? `旋钮控制：${boundKnobLabel()}<br>说“下一步”继续 · 说“恢复页面操作”使用旋钮翻页` : '旋钮：左右上一步、下一步 / 按下退出'}</div></div>`;
        }

        // 步骤页内的计时环独立刷新，避免为更新秒数重复渲染整张步骤画面。
        function startStepTimerTicker() {
            if (stepTimerTicker) clearInterval(stepTimerTicker);
            stepTimerTicker = setInterval(() => {
                let card = document.getElementById('main-card');
                let gauge = document.querySelector('.step-timer-gauge');
                if (!card || !card.classList.contains('recipe_steps') || !gauge) {
                    clearInterval(stepTimerTicker);
                    stepTimerTicker = null;
                    return;
                }
                let activeTimer = (window.__currentTimers || []).find(t => t.mode === 'reminder' && Math.floor(t.target_timestamp - Date.now() / 1000) > 0);
                if (!activeTimer) {
                    gauge.remove();
                    return;
                }
                let value = gauge.querySelector('strong');
                if (value) value.textContent = formatMmSs(Math.max(0, Math.floor(activeTimer.target_timestamp - Date.now() / 1000)));
            }, 1000);
        }

        // 油烟机风速：半圆形 5 档可视化
        // 风速值映射（m/s），6=爆炒档
        const FAN_SPEED_MAP = { 1: 2.5, 2: 4.0, 3: 5.5, 4: 7.0, 5: 8.5, 6: 10.0 };
        // 半圆 5 段弧路径（圆心 130,130，半径 100，下半圆从 180°→0°）
        const FAN_ARCS = [
            "M30,130 A100,100 0 0 1 49.1,188.8",
            "M49.1,188.8 A100,100 0 0 1 99.1,225.1",
            "M99.1,225.1 A100,100 0 0 1 160.9,225.1",
            "M160.9,225.1 A100,100 0 0 1 210.9,188.8",
            "M210.9,188.8 A100,100 0 0 1 230,130"
        ];
        // 档位数字标签位置（各段中点）
        const FAN_TICKS = [
            { x: 15.9, y: 167, label: "1" },
            { x: 59.5, y: 227, label: "2" },
            { x: 130, y: 250, label: "3" },
            { x: 200.5, y: 227, label: "4" },
            { x: 244.1, y: 167, label: "5" }
        ];

        function renderFanCard(speed) {
            speed = Math.max(1, Math.min(6, speed | 0));
            let mps = FAN_SPEED_MAP[speed];
            let isBaochao = speed >= 6;
            // 生成 5 段弧，前 speed 段激活（6=爆炒档时全部激活）
            let arcsHtml = FAN_ARCS.map((d, i) =>
                `<path d="${d}" class="fan-arc${i < speed ? ' active' : ''}"/>`
            ).join('');
            // 档位数字标签
            let ticksHtml = FAN_TICKS.map((t, i) =>
                `<text x="${t.x}" y="${t.y}" class="fan-tick${i < speed ? ' on' : ''}">${t.label}</text>`
            ).join('');
            return `
                <div class="fan-wrap">
                    <svg class="fan-svg" viewBox="0 0 260 270" xmlns="http://www.w3.org/2000/svg">
                        <defs>
                            <linearGradient id="fanGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                                <stop offset="0%" stop-color="#fbbf24"/>
                                <stop offset="50%" stop-color="#fb923c"/>
                                <stop offset="100%" stop-color="#ef4444"/>
                            </linearGradient>
                        </defs>
                        ${arcsHtml}
                        ${ticksHtml}
                        <text x="130" y="150" class="fan-center">${isBaochao ? '爆炒' : speed}</text>
                        <text x="130" y="175" class="fan-label">${isBaochao ? '' : '档'}</text>
                    </svg>
                    <div class="fan-speed-value">风速 ${mps.toFixed(1)} m/s</div>
                    <div class="fan-hint">${isBaochao ? '爆炒 · 强力排烟' : speed === 5 ? '已最高档' : '共 6 档 · 当前 ' + speed + ' 档'}</div>
                </div>
            `;
        }

        // 定时器列表的全局倒计时（每秒更新所有项的数字位）
        function startTimerListTicker() {
            if (countdownTimer) clearInterval(countdownTimer);
            countdownTimer = setInterval(function() {
                let card = document.getElementById('main-card');
                if (!card || !card.classList.contains('timer')) {
                    clearInterval(countdownTimer);
                    countdownTimer = null;
                    return;
                }
                // 从全局 state 取最新 timers（最近一次 pollState 写入 window.__currentTimers）
                let timers = window.__currentTimers || [];
                let now = Date.now() / 1000;
                timers.forEach((t, idx) => {
                    let totalSeconds = Math.max(0, Math.floor(t.target_timestamp - now));
                    let m = Math.floor(totalSeconds / 60);
                    let s = totalSeconds % 60;
                    let digits =
                        (m < 10 ? "0" + m : "" + m) +
                        (s < 10 ? "0" + s : "" + s);
                    let mPrefix = 'tm' + idx;
                    let sPrefix = 'ts' + idx;
                    setDigit(mPrefix + 't', digits[0]);
                    setDigit(mPrefix + 'o', digits[1]);
                    setDigit(sPrefix + 't', digits[2]);
                    setDigit(sPrefix + 'o', digits[3]);
                    // 更新状态文字
                    let statusEl = document.querySelector('.timer-item[data-index="' + idx + '"] .timer-status');
                    if (statusEl) {
                        let isReminder = t.mode === 'reminder';
                        let newText = totalSeconds > 0 ? (isReminder ? '到时提醒' : '后关火') : (isReminder ? '✓ 提醒完成' : '✓ 定时完成、已关火');
                        if (statusEl.innerText !== newText) statusEl.innerText = newText;
                    }
                    // 完成/未完成样式
                    let itemEl = document.querySelector('.timer-item[data-index="' + idx + '"]');
                    if (itemEl) {
                        let shouldBeDone = totalSeconds <= 0;
                        if (shouldBeDone && !itemEl.classList.contains('done')) {
                            itemEl.classList.add('done');
                            // 检测新完成，触发完成提示
                            if (!notifiedDoneSet.has(t.name) && !t.done_notified) {
                                notifiedDoneSet.add(t.name);
                                showDoneBanner(t.name, t.mode);
                                markTimerDoneNotified(t.name);
                            }
                        }
                    }
                });
            }, 1000);
        }

        // 显示完成提示横幅（不切换 card_type，避免覆盖 AI 后续更新）
        function showDoneBanner(burnerName, mode) {
            let banner = document.getElementById('done-banner');
            if (!banner) return;
            banner.innerText = '🔔 ' + burnerName + (mode === 'reminder' ? ' 计时提醒！' : ' 定时完成，已关火！');
            banner.classList.add('show');
            // 振动反馈（移动端）
            if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
            setTimeout(function() {
                banner.classList.remove('show');
            }, 3200);
        }

        function markTimerDoneNotified(burnerName) {
            fetch('/timer_done?burner=' + encodeURIComponent(burnerName)).catch(() => {});
        }

        // 切换当前高亮的灶具
        function selectBurner(idx) {
            let items = document.querySelectorAll('.timer-item');
            if (items.length === 0) return;
            if (idx < 0) idx = items.length - 1;
            if (idx >= items.length) idx = 0;
            activeBurnerIndex = idx;
            items.forEach((el, i) => el.classList.toggle('active', i === idx));
            let target = items[idx];
            if (target) target.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
        }

        const expressionAssets = {
            breathing: { src: 'assets/expressions/呼吸.mp4', loop: true },
            listening: { src: 'assets/expressions/聆听.mp4', loop: true },
            thinking: { src: 'assets/expressions/思考.mp4', loop: true },
            greeting: { src: 'assets/expressions/打招呼.mp4', loop: false },
            success: { src: 'assets/expressions/OK.mp4', loop: false },
            playful: { src: 'assets/expressions/俏皮抖动.mp4', loop: false }
        };
        const emotionExpressions = { breathing: 'breathing', listening: 'listening', thinking: 'thinking', eyes: 'greeting', happy: 'success', hungry: 'playful', cooking: 'breathing' };
        let playfulTimer = null;
        function schedulePlayfulMoment() {
            if (playfulTimer) clearTimeout(playfulTimer);
            playfulTimer = setTimeout(() => {
                const card = document.getElementById('main-card');
                const video = card && card.querySelector('.expression-video');
                if (!video || !card.classList.contains('emotion') || subCardMode || quickMenuOpen) { schedulePlayfulMoment(); return; }
                video.loop = false;
                video.src = expressionAssets.playful.src;
                video.play().catch(() => {});
                video.addEventListener('ended', () => {
                    video.src = expressionAssets.breathing.src;
                    video.loop = true;
                    video.play().catch(() => {});
                    schedulePlayfulMoment();
                }, { once: true });
            }, 90000 + Math.floor(Math.random() * 60000));
        }

        // 高亮指定索引的菜谱，并平滑滚动到视图中央
        function selectRecipe(index) {
            if (currentRecipeCount === 0) return;
            // 边界循环：左越界到末尾，右越界到开头
            if (index < 0) index = currentRecipeCount - 1;
            if (index >= currentRecipeCount) index = 0;
            selectedRecipeIndex = index;
            document.querySelectorAll('.recipe-item').forEach((el, i) => {
                el.classList.toggle('selected', i === index);
                let tag = el.dataset.tag || '为你推荐';
                let indexLabel = String(i + 1).padStart(2, '0') + ' / ' + (i === 0 ? '今日首选' : tag);
                let label = el.querySelector('.recipe-card-index');
                let action = el.querySelector('.view-steps-btn');
                if (label) label.textContent = indexLabel;
                if (action) {
                    action.textContent = i === index ? '查看步骤 →' : '选择菜谱';
                    action.onclick = function(event) {
                        event.stopPropagation();
                        if (i === index && el.dataset.hasSteps === 'true') viewRecipeSteps(i);
                        else selectRecipe(i);
                    };
                }
            });
            let context = document.querySelector('.recipe-list-context span:first-child');
            let counter = document.querySelector('.recipe-list-counter');
            let selected = document.querySelector('.recipe-item[data-index="' + index + '"]');
            if (context && selected) context.textContent = '为你挑选 · ' + (selected.dataset.tag || '今日推荐');
            if (counter) counter.textContent = String(index + 1).padStart(2, '0') + ' / ' + String(currentRecipeCount).padStart(2, '0');
            let target = document.querySelector('.recipe-item[data-index="' + index + '"]');
            if (target) {
                target.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
            }
        }

        // 查看菜谱步骤：如果 items 中有 steps 就本地渲染，否则请求 AI 生成
        function viewRecipeSteps(index) {
            fetch('/state').then(r=>r.json()).then(d=>{
                let items = d.items || [];
                if (index < 0 || index >= items.length) return;
                let item = items[index];
                if (item.steps && item.steps.length > 0) {
                    // 本地有步骤数据：先本地渲染（即时反馈），同时通知后端设置 recipe_steps 状态
                    // 这样后端 card_type=recipe_steps，pollState 不会退出 localStepsMode，
                    // 且语音 navigate_steps 能正确同步步骤
                    renderRecipeStepsLocal(item.name, item.steps);
                    fetch('/recipe_steps?dish=' + encodeURIComponent(item.name));
                } else {
                    // 无步骤数据，请求后端/AI 生成
                    fetch('/recipe_steps?dish=' + encodeURIComponent(item.name)).then(()=>{});
                }
            });
        }

        // 本地渲染菜谱步骤卡片（不依赖后端 card_type=recipe_steps）
        let localStepsMode = false;
        let localSteps = [];
        let localStepIndex = 0;
        let localStepsDish = '';
        let localFinalStepExitTimer = null;
        function renderRecipeStepsLocal(dishName, steps) {
            localStepsMode = true;
            localSteps = steps;
            localStepIndex = 0;
            localStepsDish = dishName;
            renderLocalStepCard();
        }

        function renderLocalStepCard() {
            if (!localStepsMode || localSteps.length === 0) return;
            let card = document.getElementById('main-card');
            let dynamicContent = document.getElementById('dynamic-content');
            let badge = document.getElementById('badge');
            let title = document.getElementById('title');
            if (!card || !dynamicContent) return;
            card.className = "card recipe_steps";
            badge.innerText = "菜谱步骤";
            title.innerText = localStepsDish;
            let total = localSteps.length;
            let idx = localStepIndex;
            let step = localSteps[idx];
            dynamicContent.innerHTML = renderStepGuide(idx, total, step, '烹饪步骤', window.__currentTimers || []);
            startStepTimerTicker();
            if (localFinalStepExitTimer) { clearTimeout(localFinalStepExitTimer); localFinalStepExitTimer = null; }
            if (idx >= total - 1) {
                localFinalStepExitTimer = setTimeout(() => {
                    if (localStepsMode && localStepIndex >= localSteps.length - 1) exitLocalSteps();
                }, RECIPE_FINAL_STEP_IDLE_TIMEOUT_MS);
            }
        }

        function exitLocalSteps() {
            localStepsMode = false;
            localSteps = [];
            localStepIndex = 0;
            if (localFinalStepExitTimer) { clearTimeout(localFinalStepExitTimer); localFinalStepExitTimer = null; }
            // 恢复到菜谱列表（重新拉取状态）
            fetch('/quick_card?card_type=recipe_list&title=' + encodeURIComponent('菜谱推荐') + '&value=' + encodeURIComponent('返回菜谱列表'));
        }

        // 最后一步按压旋钮 → 退出到默认页面（emotion 待机页）
        function exitLocalStepsToStandby() {
            localStepsMode = false;
            localSteps = [];
            localStepIndex = 0;
            if (localFinalStepExitTimer) { clearTimeout(localFinalStepExitTimer); localFinalStepExitTimer = null; }
            fetch('/quick_card?card_type=emotion&title=' + encodeURIComponent('小美随时待命') + '&value=' + encodeURIComponent('做菜完成，祝您用餐愉快！'));
        }

        // 后端推送的菜谱步骤最后一步按压旋钮 → 退出到默认页面（emotion 待机页）
        function exitRecipeStepsToStandby() {
            recipeAutoCloseScheduled = false; // 取消自动退出调度，避免重复请求
            fetch('/quick_card?card_type=emotion&title=' + encodeURIComponent('小美随时待命') + '&value=' + encodeURIComponent('做菜完成，祝您用餐愉快！'));
        }

        // 全局键盘控制：菜谱/定时器/fan 左右键；回车键弹出快捷菜单
        // 快捷菜单：选择功能后进入对应子卡片，子卡片内左右键调节，回车关闭/确认
        let quickMenuOpen = false;       // 快捷菜单打开中
        let quickMenuIndex = 0;          // 当前选中的菜单项索引
        let magicKnobBinding = null;     // 已绑定的唯一控制功能
        let knobBindingBeforeConfig = null;
        let subCardMode = null;          // 当前子卡片模式
        let subCardState = { wifi: true, light: 80, fan: 1, hoodChoice: 0, leftBurnerChoice: 0, rightBurnerChoice: 0, leftTimer: 10, rightTimer: 10, leftTemp: 180, rightTemp: 180, dishwasherDelay: 80 };
        const QUICK_MENU_ITEMS = [
            { fn: 'hood',        icon: '烟机', label: '日常功能', detail: '风速 / 照明 · 进入后选一项' },
            { fn: 'left_timer',  icon: '左灶', label: '定时关火、定温', detail: '仅控制左侧灶具' },
            { fn: 'right_timer', icon: '右灶', label: '定时关火、定温', detail: '仅控制右侧灶具' },
            { fn: 'dishwasher',  icon: '洗碗机', label: '预约启动', detail: '设定开始时间与程序' }
        ];

        function showQuickMenu() {
            let menu = document.getElementById('quick-menu');
            if (!menu) return;
            let mainCard = document.getElementById('main-card');
            if (mainCard) mainCard.style.visibility = 'hidden';
            let boundIndex = QUICK_MENU_ITEMS.findIndex(item => item.fn === magicKnobBinding);
            knobBindingBeforeConfig = magicKnobBinding;
            quickMenuIndex = boundIndex >= 0 ? boundIndex : 0;
            let title = menu.querySelector('.qm-title');
            let grid = menu.querySelector('.qm-grid');
            if (title) title.textContent = '设置魔术旋钮';
            if (grid) grid.innerHTML = QUICK_MENU_ITEMS.map((item, index) =>
                `<div class="qm-item${index === quickMenuIndex ? ' selected' : ''}" onclick="selectQuickItem(${index})"><div class="qm-icon">${item.icon}</div><div class="qm-label">${item.label}</div><div class="qm-detail">${item.detail}</div></div>`
            ).join('');
            updateQuickMenuSel();
            menu.classList.add('show');
            quickMenuOpen = true;
        }

        function hideQuickMenu() {
            let menu = document.getElementById('quick-menu');
            if (menu) menu.classList.remove('show');
            let mainCard = document.getElementById('main-card');
            if (mainCard) mainCard.style.visibility = '';
            quickMenuOpen = false;
        }

        function boundKnobLabel() { return ({ hood: '烟机风速', left_timer: '左灶定时关火', right_timer: '右灶定时关火', dishwasher: '洗碗机预约' })[magicKnobBinding] || '页面操作'; }
        function recipeKnobHint() { return magicKnobBinding ? `旋钮控制：${boundKnobLabel()}<br>说“下一步”继续 · 说“恢复页面操作”使用旋钮翻页` : '旋钮：页面操作　·　左右选择 / 按下查看步骤'; }
        // 魔术旋钮绑定功能已改为"点击旋钮呼出控制卡片"形式：
        // 绑定后按压旋钮 → openBoundMagicControl → 在主显示区渲染对应子卡片（renderSubCard）。
        // 旧的顶部信息窗口（showKnobFeedback + .done-banner 横幅）已取消。
        function handleCurrentPageNavigation(action) {
            if (localStepsMode) { if (action === 'left' && localStepIndex > 0) { localStepIndex--; renderLocalStepCard(); } else if (action === 'right' && localStepIndex < localSteps.length - 1) { localStepIndex++; renderLocalStepCard(); } else if (action === 'confirm') { if (localStepIndex < localSteps.length - 1) { localStepIndex++; renderLocalStepCard(); } else exitLocalStepsToStandby(); } return; }
            if (subCardMode) { if (action === 'left') { if (isOptionMenu()) openSelectedMagicOption(); else handleSubCardArrow(-1); } else if (action === 'right') { if (isOptionMenu()) openSelectedMagicOption(); else handleSubCardArrow(1); } else if (action === 'confirm') { if (isOptionMenu()) cycleMagicOption(); else exitSubCard(subCardMode === 'left_timer' || subCardMode === 'right_timer'); } return; }
            let card = document.getElementById('main-card'); if (!card) return;
            if (action === 'confirm') {
                if (card.classList.contains('recipe_list')) viewRecipeSteps(selectedRecipeIndex);
                else if (card.classList.contains('recipe_steps') && backendStepTotal > 0 && backendStepIndex >= backendStepTotal - 1) { exitRecipeStepsToStandby(); }
                else openBoundMagicControl();
            }
            else if (card.classList.contains('recipe_list')) { if (action === 'left') selectRecipe(selectedRecipeIndex - 1); else if (action === 'right') selectRecipe(selectedRecipeIndex + 1); }
            else if (card.classList.contains('timer')) { if (action === 'left') selectBurner(activeBurnerIndex - 1); else if (action === 'right') selectBurner(activeBurnerIndex + 1); }
        }
        function handleKnob(action) {
            if (quickMenuOpen) { if (action === 'left') { quickMenuIndex = (quickMenuIndex - 1 + QUICK_MENU_ITEMS.length) % QUICK_MENU_ITEMS.length; updateQuickMenuSel(); } else if (action === 'right') { quickMenuIndex = (quickMenuIndex + 1) % QUICK_MENU_ITEMS.length; updateQuickMenuSel(); } else if (action === 'confirm') enterSubCard(); return; }
            // 已绑定功能时，按压旋钮（confirm）由 handleCurrentPageNavigation → openBoundMagicControl 呼出对应控制卡片；
            // 子卡片内左右/按下由 subCardMode 分支处理。不再走顶部横幅反馈。
            handleCurrentPageNavigation(action);
        }

        function updateQuickMenuSel() {
            document.querySelectorAll('#quick-menu .qm-item').forEach((el, i) => {
                el.classList.toggle('selected', i === quickMenuIndex);
            });
        }

        function selectQuickItem(idx) {
            quickMenuIndex = idx;
            updateQuickMenuSel();
            enterSubCard();
        }

        // 进入子卡片模式
        function enterSubCard() {
            let item = QUICK_MENU_ITEMS[quickMenuIndex];
            if (!item) return;
            // D01 的按压只保存绑定并关闭；之后按旋钮才进入相应调节页。
            magicKnobBinding = item.fn;
            hideQuickMenu();
            fetch('/magic_knob_binding?function=' + encodeURIComponent(item.fn));
        }

        function openBoundMagicControl() {
            if (!magicKnobBinding) { showQuickMenu(); return; }
            subCardMode = magicKnobBinding === 'left_timer' ? 'left_burner_menu' : magicKnobBinding === 'right_timer' ? 'right_burner_menu' : magicKnobBinding;
            // 进入已绑定功能前从后端同步当前状态
            fetch('/state').then(r=>r.json()).then(d=>{
                if (subCardMode === 'fan') subCardState.fan = d.fan_speed || 1;
                if (subCardMode === 'wifi') subCardState.wifi = !!d.wifi_on;
                if (subCardMode === 'light') subCardState.light = d.light_brightness || 0;
                renderSubCard();
            }).catch(()=>renderSubCard());
        }

        // 渲染子卡片到主卡片区域
        function renderSubCard() {
            let card = document.getElementById('main-card');
            let dynamicContent = document.getElementById('dynamic-content');
            let badge = document.getElementById('badge');
            let title = document.getElementById('title');
            if (!card || !dynamicContent) return;

            if (subCardMode === 'hood') {
                card.className = 'card magic-sub'; badge.innerText = '魔术旋钮 · 烟机'; title.innerText = '烟机日常功能';
                let choices = [['照明','灯光控制','开关照明'],['风速','调节风速','选择风速档位'],['蒸汽洗','清洁养护','进入准备流程'],['Wi-Fi','设备连接','查看连接状态'],['随温感','温感联动','查看模式设置']];
                dynamicContent.innerHTML = `<div class="hood-choice-screen"><div class="hood-choice-header"><strong>吸油烟机控制</strong><span>选择要控制的功能</span></div><div class="hood-choice-grid">${choices.map((x,i)=>`<div class="hood-choice ${i===subCardState.hoodChoice?'selected':''}"><small>${String(i+1).padStart(2,'0')} / ${x[0]}</small><b>${x[1]}</b><small>${x[2]}</small></div>`).join('')}</div><div class="device-task-hint">旋钮：参数调节 / 按下：功能选择</div></div>`;
            } else if (subCardMode === 'dishwasher') {
                card.className = 'card magic-sub'; badge.innerText = '魔术旋钮 · 洗碗机'; title.innerText = '预约洗涤时间';
                let startAt = new Date(Date.now() + subCardState.dishwasherDelay * 60000);
                let timeText = startAt.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
                let hours = Math.floor(subCardState.dishwasherDelay / 60), mins = subCardState.dishwasherDelay % 60;
                let relative = (hours ? hours + '小时' : '') + (mins ? mins + '分钟' : '');
                dynamicContent.innerHTML = `<div class="device-task-screen"><div class="device-task-title">预约洗涤时间</div><div class="device-task-value">${timeText}</div><div class="device-task-meta">今天 · ${relative}后开始 · 标准洗</div><div class="device-task-hint">旋钮：左右调节时长 / 按下确认</div></div>`;
            } else if (subCardMode === 'wifi') {
                card.className = "card wifi-sub";
                badge.innerText = "快捷设置 · WiFi";
                title.innerText = "WiFi 开关";
                let on = subCardState.wifi;
                dynamicContent.innerHTML = `
                    <div class="wifi-wrap">
                        <div class="wifi-icon">${on ? '📶' : '📵'}</div>
                        <div class="wifi-status ${on ? 'on' : 'off'}">${on ? '已开启' : '已关闭'}</div>
                        <div class="wifi-toggle">
                            <span class="toggle-opt ${!on ? 'active' : ''}">关</span>
                            <span class="toggle-opt ${on ? 'active' : ''}">开</span>
                        </div>
                        <div class="sub-hint"><kbd>←</kbd> 关 <kbd>→</kbd> 开 &nbsp;·&nbsp; <kbd>Enter</kbd> 关闭</div>
                    </div>
                `;
                fetch('/quick_state?wifi=' + (on ? 1 : 0));
            } else if (subCardMode === 'light') {
                card.className = "card light-sub";
                badge.innerText = "快捷设置 · 照明";
                title.innerText = "照明亮度";
                let b = subCardState.light;
                let levels=[0,30,60,100], selected=levels.reduce((best,v,i)=>Math.abs(v-b)<Math.abs(levels[best]-b)?i:best,0);
                let levelLabel = levels[selected] === 0 ? '关闭' : levels[selected] + '<span>%</span>';
                dynamicContent.innerHTML = `<div class="device-task-screen"><div class="device-task-title">照明</div><div class="device-task-value">${levelLabel}</div><div class="hood-segments">${levels.map((v,i)=>`<i class="${i===selected?'on':''}"></i>`).join('')}</div><div class="device-task-hint">旋钮：左右调光 / 按下退出 · 0%=关闭</div></div>`;
                fetch('/quick_state?light=' + b);
            } else if (subCardMode === 'fan') {
                card.className = "card fan-sub";
                badge.innerText = "快捷设置 · 风速";
                title.innerText = "油烟机风速";
                let s = subCardState.fan;
                let isBaochao = s >= 6;
                dynamicContent.innerHTML = `<div class="device-task-screen"><div class="device-task-title">风速</div><div class="device-task-value">${isBaochao ? '爆炒' : s + '<span>档</span>'}</div><div class="hood-segments">${[1,2,3,4,5,6].map(i=>`<i class="${i<=s?'on':''}"></i>`).join('')}</div><div class="device-task-hint">旋钮：左右调档 / 按下保存并退出 · 6档=爆炒</div></div>`;
                fetch('/fan_speed?speed=' + s);
            } else if (subCardMode === 'steam') {
                card.className = 'card steam-sub'; badge.innerText = '烟机 · 蒸汽洗'; title.innerText = '自动蒸汽洗';
                dynamicContent.innerHTML = `<div class="device-task-screen"><div class="device-task-title">自动蒸汽洗</div><div class="steam-steps"><span class="active">01　准备</span><span>02　清洁</span><span>03　完成</span></div><div class="device-task-hint">旋钮：按下退出</div></div>`;
            } else if (subCardMode === 'left_burner_menu' || subCardMode === 'right_burner_menu') {
                let isLeft = subCardMode === 'left_burner_menu';
                let burner = isLeft ? '左灶' : '右灶';
                let choice = isLeft ? subCardState.leftBurnerChoice : subCardState.rightBurnerChoice;
                let choices = [['定时关火', '设定时长，到时自动关火'], ['固定油温', '设定油温，辅助稳定烹饪']];
                card.className = 'card magic-sub'; badge.innerText = '魔术旋钮 · ' + burner; title.innerText = burner + '控制';
                dynamicContent.innerHTML = `<div class="hood-choice-screen"><div class="hood-choice-header"><strong>${burner}控制</strong><span>选择要控制的功能</span></div><div class="hood-choice-grid">${choices.map((x,i)=>`<div class="hood-choice ${i===choice?'selected':''}"><small>${String(i+1).padStart(2,'0')} / ${burner}</small><b>${x[0]}</b><small>${x[1]}</small></div>`).join('')}</div><div class="device-task-hint">旋钮：参数调节 / 按下：功能选择</div></div>`;
            } else if (subCardMode === 'left_timer' || subCardMode === 'right_timer') {
                let isLeft = subCardMode === 'left_timer';
                let burner = isLeft ? '左灶' : '右灶';
                let mins = isLeft ? subCardState.leftTimer : subCardState.rightTimer;
                card.className = "card timer-sub";
                badge.innerText = "快捷设置 · " + burner + "定时";
                title.innerText = burner + "定时关火";
                if (mins <= 0) {
                    dynamicContent.innerHTML = `<div class="device-task-screen"><div class="device-task-title">${burner}定时关火</div><div class="device-task-value">关闭</div><div class="device-task-track"><i style="width:0%"></i></div><div class="device-task-hint">旋钮：右转设置时长 / 按下确认关闭</div></div>`;
                } else {
                    dynamicContent.innerHTML = `<div class="device-task-screen"><div class="device-task-title">${burner}定时关火</div><div class="device-task-value">${mins}<span>分钟</span></div><div class="device-task-track"><i style="width:${Math.min(100, mins / 60 * 100)}%"></i></div><div class="device-task-hint">旋钮：左右调节时长 / 按下确认 · 左转到0关闭</div></div>`;
                }
            } else if (subCardMode === 'left_temp' || subCardMode === 'right_temp') {
                let isLeft = subCardMode === 'left_temp';
                let burner = isLeft ? '左灶' : '右灶';
                let temp = isLeft ? subCardState.leftTemp : subCardState.rightTemp;
                card.className = 'card temp-sub'; badge.innerText = '魔术旋钮 · ' + burner; title.innerText = burner + '固定油温';
                dynamicContent.innerHTML = `<div class="device-task-screen"><div class="device-task-title">${burner}固定油温</div><div class="device-task-value">${temp}<span>°C</span></div><div class="device-task-track"><i style="width:${Math.min(100, (temp - 120) / 100 * 100)}%"></i></div><div class="device-task-hint">旋钮：左右调节温度 / 按下确认</div></div>`;
            }
        }

        function isOptionMenu() {
            return subCardMode === 'hood' || subCardMode === 'left_burner_menu' || subCardMode === 'right_burner_menu';
        }

        // 在功能选项页按压旋钮：切换当前高亮选项。
        function cycleMagicOption() {
            if (subCardMode === 'hood') subCardState.hoodChoice = (subCardState.hoodChoice + 1) % 5;
            else if (subCardMode === 'left_burner_menu') subCardState.leftBurnerChoice = (subCardState.leftBurnerChoice + 1) % 2;
            else if (subCardMode === 'right_burner_menu') subCardState.rightBurnerChoice = (subCardState.rightBurnerChoice + 1) % 2;
            renderSubCard();
        }

        // 在功能选项页旋转旋钮：进入当前选中的调节页面。
        function openSelectedMagicOption() {
            if (subCardMode === 'hood') subCardMode = ['light', 'fan', 'steam', 'wifi', 'temp'][subCardState.hoodChoice];
            else if (subCardMode === 'left_burner_menu') subCardMode = subCardState.leftBurnerChoice === 0 ? 'left_timer' : 'left_temp';
            else if (subCardMode === 'right_burner_menu') subCardMode = subCardState.rightBurnerChoice === 0 ? 'right_timer' : 'right_temp';
            renderSubCard();
        }

        // 退出子卡片模式
        function closeAdjustmentPage(title, value) {
            subCardMode = null;
            quickMenuOpen = false;
            forceEmotionRender = true;
            const url = '/quick_card?card_type=emotion&title=' + encodeURIComponent(title || ' ') + '&value=' + encodeURIComponent(value) + '&emotion=happy';
            // 直接使用接口返回的状态，不能只等待 SSE，避免调节页因推送延迟而停留。
            fetch(url).then(r => r.json()).then(data => applyState(data)).catch(() => pollState());
        }

        function exitSubCard(startTimer) {
            let mode = subCardMode;
            subCardMode = null;
            if (mode === 'left_timer' || mode === 'right_timer') {
                // 定时器：确认启动定时（mins=0 代表关闭定时）
                let isLeft = mode === 'left_timer';
                let burner = isLeft ? '左灶' : '右灶';
                let mins = isLeft ? subCardState.leftTimer : subCardState.rightTimer;
                if (startTimer) {
                    fetch('/quick_timer?burner=' + encodeURIComponent(burner) + '&minutes=' + mins).then(()=>{
                        closeAdjustmentPage('', mins <= 0 ? burner + '已关闭定时' : burner + '已设定时 ' + mins + ' 分钟');
                    });
                } else {
                    closeAdjustmentPage('', '已取消定时设置');
                }
            } else if (mode === 'fan') {
                closeAdjustmentPage('', subCardState.fan >= 6 ? '风速已保存 · 爆炒' : '风速已保存 · ' + subCardState.fan + ' 档');
            } else if (mode === 'dishwasher') {
                closeAdjustmentPage('', '洗碗机已预约 · ' + subCardState.dishwasherDelay + ' 分钟后开始标准洗');
            } else if (mode === 'left_temp' || mode === 'right_temp') {
                let isLeft = mode === 'left_temp';
                let burner = isLeft ? '左灶' : '右灶';
                let temp = isLeft ? subCardState.leftTemp : subCardState.rightTemp;
                fetch('/quick_temperature?burner=' + encodeURIComponent(burner) + '&temperature=' + temp).then(()=>{
                    closeAdjustmentPage('', burner + '固定油温 ' + temp + '°C');
                }).catch(()=>closeAdjustmentPage('', burner + '固定油温 ' + temp + '°C'));
            } else {
                // E02/E03/E04 等烟机功能按下后直接退出，恢复默认待机页。
                closeAdjustmentPage('', mode === 'light' ? (subCardState.light <= 0 ? '照明已关闭' : '照明已保存 · ' + subCardState.light + '%') : '设置已保存');
            }
        }

        function knobActionFromKey(e) {
            // Android 硬件键：音量减/加对应旋钮左/右，静音键对应按压确认。
            if (e.key === 'VolumeDown' || e.key === 'AudioVolumeDown' || e.code === 'AudioVolumeDown' || e.keyCode === 25) return 'left';
            if (e.key === 'VolumeUp' || e.key === 'AudioVolumeUp' || e.code === 'AudioVolumeUp' || e.keyCode === 24) return 'right';
            if (e.key === 'AudioVolumeMute' || e.code === 'AudioVolumeMute' || e.keyCode === 164) return 'confirm';
            // 桌面预览与开发调试兼容。
            if (e.key === 'ArrowLeft') return 'left';
            if (e.key === 'ArrowRight') return 'right';
            if (e.key === 'Enter') return 'confirm';
            return null;
        }

        const potFireSearchImage = 'assets/pot-fire-match/right-fire.png';
        const potFireResultImages = [
            'assets/pot-fire-match/figure-a.png',
            'assets/pot-fire-match/figure-b.png',
            'assets/pot-fire-match/figure-c.png'
        ];
        const potFireStates = ['正在检索锅具', '火形匹配中', '火形匹配成功'];
        let potFireLeftResultIndex = -1;
        let potFireRightResultIndex = -1;
        let potFireAutoCloseTimer = null;
        let potFireSearchTimers = [];
        let potFireSearchVersion = 0;

        function potFireDots(index) {
            return [0, 1, 2].map(i => `<i class="pot-fire-dot${i === index ? ' active' : ''}"></i>`).join('');
        }

        function renderPotFireMatch(side, stateIndex, imageSrc) {
            const screen = document.getElementById('pot-fire-match');
            if (!screen) return;
            const label = screen.querySelector(`[data-pot="${side}-label"]`);
            const image = screen.querySelector(`[data-pot="${side}-image"]`);
            const dots = screen.querySelector(`[data-pot="${side}-dots"]`);
            label.innerHTML = `${side === 'left' ? '左灶' : '右灶'} / <strong>${potFireStates[stateIndex]}</strong>`;
            image.src = imageSrc;
            dots.innerHTML = potFireDots(stateIndex);
        }

        function clearPotFireSearchTimers() {
            potFireSearchTimers.forEach(clearTimeout);
            potFireSearchTimers = [];
        }

        function closePotFireMatch() {
            const screen = document.getElementById('pot-fire-match');
            if (screen) screen.classList.remove('show');
            if (potFireAutoCloseTimer) clearTimeout(potFireAutoCloseTimer);
            clearPotFireSearchTimers();
            potFireAutoCloseTimer = null;
        }

        function showPotFireMatch(side) {
            const screen = document.getElementById('pot-fire-match');
            if (!screen) return;
            clearPotFireSearchTimers();
            if (potFireAutoCloseTimer) clearTimeout(potFireAutoCloseTimer);
            const version = ++potFireSearchVersion;
            if (side === 'left') potFireLeftResultIndex = (potFireLeftResultIndex + 1) % potFireResultImages.length;
            else potFireRightResultIndex = (potFireRightResultIndex + 1) % potFireResultImages.length;
            const resultImage = potFireResultImages[side === 'left' ? potFireLeftResultIndex : potFireRightResultIndex];
            screen.className = side === 'left' ? 'show left-active' : 'show right-active';
            // 模拟设备先检索、再给出匹配结果的三个状态。
            renderPotFireMatch(side, 0, potFireSearchImage);
            [1, 2].forEach((stateIndex, index) => {
                potFireSearchTimers.push(setTimeout(() => {
                    if (version === potFireSearchVersion) renderPotFireMatch(side, stateIndex, potFireSearchImage);
                }, (index + 1) * 420));
            });
            potFireSearchTimers.push(setTimeout(() => {
                if (version !== potFireSearchVersion) return;
                renderPotFireMatch(side, 2, resultImage);
                potFireAutoCloseTimer = setTimeout(closePotFireMatch, 3000);
            }, 1260));
        }

        document.addEventListener('keydown', function(e) {
            // 锅与火形适配仅在默认待机页呼出，不影响菜谱或设备控制操作。
            const isStandby = document.getElementById('main-card')?.classList.contains('emotion');
            if (isStandby && e.key.toLowerCase() === 'q') { e.preventDefault(); showPotFireMatch('left'); return; }
            if (isStandby && e.key.toLowerCase() === 'p') { e.preventDefault(); showPotFireMatch('right'); return; }
            // 唯一旋钮入口：绑定功能优先，页面不能越过它直接处理左右/按下。
            const knobAction = knobActionFromKey(e);
            if (knobAction) { e.preventDefault(); handleKnob(knobAction); return; }
            if (e.key === 'Escape' && quickMenuOpen) { e.preventDefault(); magicKnobBinding = knobBindingBeforeConfig; hideQuickMenu(); return; }
            // ===== 菜谱步骤本地模式 =====
            if (localStepsMode) {
                if (e.key === 'ArrowLeft') {
                    e.preventDefault();
                    if (localStepIndex > 0) { localStepIndex--; renderLocalStepCard(); }
                } else if (e.key === 'ArrowRight') {
                    e.preventDefault();
                    if (localStepIndex < localSteps.length - 1) { localStepIndex++; renderLocalStepCard(); }
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    if (magicKnobBinding) openBoundMagicControl();
                    else if (localStepIndex < localSteps.length - 1) { localStepIndex++; renderLocalStepCard(); }
                    else { exitLocalSteps(); }
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    exitLocalSteps();
                }
                return;
            }

            // ===== 快捷菜单打开时 =====
            if (quickMenuOpen) {
                if (e.key === 'ArrowLeft') {
                    e.preventDefault();
                    quickMenuIndex = (quickMenuIndex - 1 + QUICK_MENU_ITEMS.length) % QUICK_MENU_ITEMS.length;
                    updateQuickMenuSel();
                } else if (e.key === 'ArrowRight') {
                    e.preventDefault();
                    quickMenuIndex = (quickMenuIndex + 1) % QUICK_MENU_ITEMS.length;
                    updateQuickMenuSel();
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    enterSubCard();
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    hideQuickMenu();
                }
                return;
            }

            // ===== 子卡片模式 =====
            if (subCardMode) {
                if (e.key === 'ArrowLeft') {
                    e.preventDefault();
                    if (isOptionMenu()) openSelectedMagicOption();
                    else handleSubCardArrow(-1);
                } else if (e.key === 'ArrowRight') {
                    e.preventDefault();
                    if (isOptionMenu()) openSelectedMagicOption();
                    else handleSubCardArrow(1);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    if (isOptionMenu()) cycleMagicOption();
                    else exitSubCard(subCardMode === 'left_timer' || subCardMode === 'right_timer');
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    exitSubCard(false);
                }
                return;
            }

            // ===== 正常模式 =====
            // 菜谱列表卡片：回车进入选中菜谱的步骤
            let mainCard = document.getElementById('main-card');
            if (e.key === 'Enter' && mainCard && mainCard.classList.contains('recipe_list')) {
                e.preventDefault();
                viewRecipeSteps(selectedRecipeIndex);
                return;
            }

            // 其他卡片：回车弹出快捷菜单
            if (e.key === 'Enter') {
                e.preventDefault();
                openBoundMagicControl();
                return;
            }

            // ===== 各主卡片的左右键 =====
            let card = document.getElementById('main-card');
            if (!card) return;
            if (card.classList.contains('recipe_list')) {
                if (e.key === 'ArrowLeft') { e.preventDefault(); selectRecipe(selectedRecipeIndex - 1); }
                else if (e.key === 'ArrowRight') { e.preventDefault(); selectRecipe(selectedRecipeIndex + 1); }
            } else if (card.classList.contains('timer')) {
                if (e.key === 'ArrowLeft') { e.preventDefault(); selectBurner(activeBurnerIndex - 1); }
                else if (e.key === 'ArrowRight') { e.preventDefault(); selectBurner(activeBurnerIndex + 1); }
            } else if (card.classList.contains('fan')) {
                if (e.key === 'ArrowLeft') {
                    e.preventDefault();
                    fetch('/state').then(r=>r.json()).then(d=>{ fetch('/fan_speed?speed=' + Math.max(1, (d.fan_speed||1) - 1)); });
                } else if (e.key === 'ArrowRight') {
                    e.preventDefault();
                    fetch('/state').then(r=>r.json()).then(d=>{ fetch('/fan_speed?speed=' + Math.min(6, (d.fan_speed||1) + 1)); });
                }
            }
        });

        // 子卡片左右键处理
        function handleSubCardArrow(dir) {
            if (subCardMode === 'dishwasher') {
                // 洗碗机预约：左右调节启动延迟时长（10 分钟步进，10~720 分钟）
                subCardState.dishwasherDelay = Math.max(10, Math.min(720, subCardState.dishwasherDelay + dir * 10));
                renderSubCard();
                return;
            }
            if (subCardMode === 'wifi') {
                subCardState.wifi = (dir > 0);  // 右=开，左=关
                renderSubCard();
            } else if (subCardMode === 'light') {
                const levels = [0, 30, 60, 100];
                const current = levels.reduce((best, value, index) => Math.abs(value - subCardState.light) < Math.abs(levels[best] - subCardState.light) ? index : best, 0);
                subCardState.light = levels[Math.max(0, Math.min(levels.length - 1, current + dir))];
                renderSubCard();
            } else if (subCardMode === 'fan') {
                subCardState.fan = Math.max(1, Math.min(6, subCardState.fan + dir));
                renderSubCard();
            } else if (subCardMode === 'left_timer') {
                subCardState.leftTimer = Math.max(0, Math.min(120, subCardState.leftTimer + dir));
                renderSubCard();
            } else if (subCardMode === 'right_timer') {
                subCardState.rightTimer = Math.max(0, Math.min(120, subCardState.rightTimer + dir));
                renderSubCard();
            } else if (subCardMode === 'left_temp') {
                subCardState.leftTemp = Math.max(120, Math.min(220, subCardState.leftTemp + dir * 10));
                renderSubCard();
            } else if (subCardMode === 'right_temp') {
                subCardState.rightTemp = Math.max(120, Math.min(220, subCardState.rightTemp + dir * 10));
                renderSubCard();
            }
        }

        // pollState 改为接收 SSE 推送的数据（或 fallback 轮询）
        async function pollState() {
            try {
                let res = await fetch('/state');
                let data = await res.json();
                applyState(data);
            } catch(e) {}
        }

        function acknowledgeRenderedRevision(revision) {
            if (!revision) return;
            fetch('/ui_ack?revision=' + encodeURIComponent(revision), { cache: 'no-store', keepalive: true }).catch(() => {});
        }

        // 应用状态到 UI（从 pollState 抽取，供 SSE 和 fallback 轮询共用）
        function getStandbyApplianceCards(data) {
            const now = Date.now() / 1000;
            const activeTimers = (data.timers || []).filter(t =>
                (t.mode || 'shutoff') === 'shutoff' && Math.floor(t.target_timestamp - now) > 0 && (t.name === '左灶' || t.name === '右灶')
            );
            const temperatures = data.burner_temperatures || {};
            const makeTimer = t => ({ side: t.name === '左灶' ? 'left' : 'right', type: 'timer', burner: t.name, value: formatMmSs(Math.max(0, Math.floor(t.target_timestamp - now))), label: '后自动关火', target: t.target_timestamp });
            const makeTemp = side => {
                const source = temperatures[side];
                const target = Number(typeof source === 'object' ? source.target : source);
                const current = Number(typeof source === 'object' ? source.current : Math.max(30, target - 30));
                const phase = typeof source === 'object' && source.state ? source.state : (current < target ? '升温中' : '自动控温中');
                return { side, type: 'temperature', burner: side === 'left' ? '左灶' : '右灶', current, target, phase };
            };
            const cards = [];
            activeTimers.forEach(t => cards.push(makeTimer(t)));
            ['left', 'right'].forEach(side => {
                const source = temperatures[side];
                const target = Number(typeof source === 'object' ? source.target : source);
                if (Number.isFinite(target) && target > 0 && !(typeof source === 'object' && source.active === false)) cards.push(makeTemp(side));
            });
            return cards;
        }

        function renderStandbyApplianceCards(data) {
            const cards = getStandbyApplianceCards(data);
            const card = document.getElementById('main-card');
            const dynamicContent = document.getElementById('dynamic-content');
            if (!card || !dynamicContent) return;
            card.classList.toggle('has-appliance-status', cards.length > 0);
            const old = dynamicContent.querySelector('.standby-appliance-cards');
            if (old) old.remove();
            if (!cards.length) return;
            const left = cards.filter(item => item.side === 'left');
            const right = cards.filter(item => item.side === 'right');
            const render = item => item.type === 'temperature'
                ? `<section class="standby-appliance-card temperature"><span class="burner">${item.burner} · 定温</span><strong>当前 ${item.current}°C</strong><small>目标 ${item.target}°C · ${item.phase}</small></section>`
                : `<section class="standby-appliance-card timer" data-standby-timer="${item.target}"><span class="burner">${item.burner} · 定时</span><strong>${item.value}</strong><small>后自动关火</small></section>`;
            const idle = side => `<div class="standby-burner-idle">${side === 'left' ? '左灶' : '右灶'} · 未开火<small>未设定时 / 定温</small></div>`;
            dynamicContent.insertAdjacentHTML('beforeend', `<div class="standby-appliance-cards"><div class="standby-appliance-group left">${left.length ? left.map(render).join('') : idle('left')}</div><div class="standby-appliance-group right">${right.length ? right.map(render).join('') : idle('right')}</div></div>`);
            if (standbyStatusTicker) clearInterval(standbyStatusTicker);
            standbyStatusTicker = setInterval(() => {
                document.querySelectorAll('[data-standby-timer]').forEach(el => {
                    const target = Number(el.dataset.standbyTimer);
                    if (!target) return;
                    const value = el.querySelector('strong');
                    if (value) value.textContent = formatMmSs(Math.max(0, Math.floor(target - Date.now() / 1000)));
                });
            }, 1000);
        }

        function renderStandbyAmbientInfo(data) {
            const dynamicContent = document.getElementById('dynamic-content');
            if (!dynamicContent) return;
            const old = dynamicContent.querySelector('.standby-ambient-info');
            if (old) old.remove();
            const pm25 = Math.max(0, Number(data.pm25 == null ? 29 : data.pm25));
            const level = pm25 <= 35 ? '安全' : pm25 <= 75 ? '轻度污染' : '污染偏高';
            dynamicContent.insertAdjacentHTML('beforeend', `<div class="standby-ambient-info"><div class="standby-date-time"><div class="date"></div><div class="time"></div></div><div class="standby-pm"><div class="label">PM2.5 · ${level}</div><strong>${String(pm25).padStart(3, '0')}</strong></div></div>`);
            const updateClock = () => {
                const now = new Date();
                const days = ['日','一','二','三','四','五','六'];
                const date = dynamicContent.querySelector('.standby-date-time .date');
                const clock = dynamicContent.querySelector('.standby-date-time .time');
                if (date) date.textContent = `${now.getMonth() + 1}月${now.getDate()}日 星期${days[now.getDay()]}`;
                if (clock) {
                    const value = now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
                    clock.innerHTML = value.replace(':', '<span class="clock-colon">:</span>');
                }
            };
            updateClock();
            setInterval(updateClock, 1000);
        }

        function scheduleSettingAutoClose(cardType) {
            if (settingAutoCloseTimer) clearTimeout(settingAutoCloseTimer);
            settingAutoCloseTimer = setTimeout(() => {
                // Android 离线包没有 /state 与 /quick_card 接口，由原生 MCP 状态驱动回待机。
                // 这里仅作为原生端未及时推送时的视觉兜底，避免调节页永久卡住。
                if (window.__androidNativeMcp) {
                    const current = window.__lastReceivedState || {};
                    if (current.card_type === cardType) {
                        subCardMode = null;
                        forceEmotionRender = true;
                        applyState(Object.assign({}, current, { card_type: 'emotion', emotion: 'breathing', title: '', value: '', revision: 0 }));
                    }
                    settingAutoCloseTimer = null;
                    return;
                }
                fetch('/state').then(r => r.json()).then(state => {
                    if (state.card_type === cardType) {
                        fetch('/quick_card?card_type=emotion&title=%20&value=&emotion=breathing');
                    }
                }).catch(() => {});
                settingAutoCloseTimer = null;
            }, 5000);
        }

        function applyState(data) {
            try {
                if (data && data.mcp_configured === false) { showMcpSetup(); return; }
                hideMcpSetup();
                window.__lastReceivedState = data || {};
                const revision = Number(data.revision || 0);
                if (revision && revision < lastAppliedRevision) return;
                if (revision) lastAppliedRevision = revision;
                const previousCardType = lastCardType;
                // 当前 applyState 的 DOM 写入是同步的；下一帧确认，确保语音在屏幕更新后继续。
                if (revision) requestAnimationFrame(() => acknowledgeRenderedRevision(revision));
                magicKnobBinding = data.magic_knob_binding || null;

                // 设备调节页只能阻止“同一页”的同步刷新；新的语音状态必须立刻接管页面。
                // 否则风速/照明页会吞掉菜谱、定时等后续指令，表现为页面卡住。
                if (subCardMode && data.card_type !== subCardMode) {
                    subCardMode = null;
                    forceEmotionRender = true;
                }
                if (subCardMode || quickMenuOpen) return;

                // 小美语音触发的魔术旋钮设置页：复用 D01 的四入口选择界面。
                if (data.card_type === 'magic_knob') {
                    showQuickMenu();
                    return;
                }

                // 先更新定时快照，让本地步骤页也能立即接收语音新建的定时器。
                window.__currentTimers = data.timers || [];

                // 本地步骤模式：检查后端 step_index 是否被语音改变
                if (localStepsMode) {
                    let backendIdx = data.step_index || 0;
                    let backendTotal = data.step_total || 0;
                    // 同步语音导航：后端是 recipe_steps 且 step_index 与本地不同 → 刷新
                    if (data.card_type === 'recipe_steps') {
                        if (backendIdx !== localStepIndex && backendIdx < localSteps.length) localStepIndex = backendIdx;
                        // 定时器变更与步骤导航都复用步骤页；重新生成一次计时环即可。
                        renderLocalStepCard();
                    }
                    // 仅当后端切换到完全不同的功能卡片（timer/fan/emotion），
                    // 或推来新的菜谱列表（recipe_list 且 items 是菜谱格式而非步骤格式）时才退出步骤模式
                    // 这样既避免 /recipe_steps 异步请求未完成的竞态，又能让 AI 新推荐菜谱时正常渲染
                    let isFreshRecipeList = (data.card_type === 'recipe_list'
                        && Array.isArray(data.items) && data.items.length > 0
                        && data.items[0] && data.items[0].name);
                    if (data.card_type === 'timer' || data.card_type === 'fan' || data.card_type === 'emotion' || isFreshRecipeList) {
                        localStepsMode = false;
                        localSteps = [];
                        localStepIndex = 0;
                        // 继续正常渲染新卡片
                    } else {
                        return; // 保持显示步骤
                    }
                }

                let card = document.getElementById('main-card');
                
                // 无论主卡片是什么类型，始终更新 timers 快照，让 overlay ticker 和完成检测长期有效
                window.__currentTimers = data.timers || [];
                if (data.card_type !== 'recipe_steps' && stepTimerTicker) {
                    clearInterval(stepTimerTicker);
                    stepTimerTicker = null;
                }

                // 检测语音指令变化：card_type 变化说明用户发了新的语音指令
                // 此时优先显示新的语音卡片，隐藏 overlay 并设置 8 秒优先期
                if (previousCardType !== null && data.card_type !== previousCardType) {
                    let overlay = document.getElementById('timer-overlay');
                    if (overlay) overlay.classList.remove('show');
                    overlayPriorityUntil = Date.now() + 8000; // 8 秒内 overlay 不自动弹出，让用户看清新卡片
                }
                lastCardType = data.card_type;

                // 旋钮调节页由本地状态即时渲染；SSE 仅同步数据，不可覆盖页面结构或重新显示待机标题。
                if (subCardMode) return;

                card.className = "card " + data.card_type;
                document.getElementById('badge').innerText = "AI 状态: " + data.card_type.toUpperCase();
                document.getElementById('title').innerText = data.title;

                let dynamicContent = document.getElementById('dynamic-content');

                if (data.card_type === 'emotion') {
                    if (countdownTimer) clearInterval(countdownTimer);
                    const applianceKey = JSON.stringify({ timers: (data.timers || []).map(t => [t.name, t.target_timestamp, t.mode]), temperatures: data.burner_temperatures || {} });
                    let expressionKey = emotionExpressions[data.emotion] || 'breathing';
                    let expression = expressionAssets[expressionKey];
                    card.classList.toggle('is-greeting', expressionKey === 'greeting');
                    // emotion 变化或卡片切换时强制重写，确保个性化表情及时刷新
                    let needEmotionRender = forceEmotionRender || (lastEmotion !== data.emotion) || (lastEmotionValue !== data.value) || (previousCardType !== 'emotion') || (lastStandbyApplianceKey !== applianceKey);
                    lastEmotion = data.emotion;
                    lastEmotionValue = data.value;
                    lastStandbyApplianceKey = applianceKey;
                    forceEmotionRender = false;
                    if (needEmotionRender) {
                        dynamicContent.innerHTML = `
                            <div class="avatar-box"><video class="expression-video" src="${expression.src}" autoplay muted ${expression.loop ? 'loop' : ''} playsinline></video></div>
                            <div class="desc">${data.value}</div>
                        `;
                        // 单次表情播放结束后回到呼吸待机，不改变当前页面内容。
                        if (!expression.loop) {
                            const video = dynamicContent.querySelector('.expression-video');
                            if (video) video.addEventListener('ended', () => {
                                // Android 端由原生 MCP 在问候可见期结束后切回 breathing。
                                // 不能在视频播放结束时立刻清空姓名与问候，否则短视频会造成“没有显示”。
                                video.src = expressionAssets.breathing.src;
                                video.loop = true;
                                if (!window.__androidNativeMcp) {
                                    document.getElementById('title').textContent = '';
                                    const greetingText = dynamicContent.querySelector('.desc');
                                    if (greetingText) greetingText.textContent = '';
                                    card.classList.remove('is-greeting');
                                }
                                video.play().catch(() => {});
                                schedulePlayfulMoment();
                            }, { once: true });
                        }
                        if (expressionKey === 'breathing') schedulePlayfulMoment();
                        else if (playfulTimer) { clearTimeout(playfulTimer); playfulTimer = null; }
                    }
                    renderStandbyApplianceCards(data);
                    renderStandbyAmbientInfo(data);
                } else if (data.card_type === 'timer') {
                    card.classList.remove('has-appliance-status');
                    if (standbyStatusTicker) { clearInterval(standbyStatusTicker); standbyStatusTicker = null; }
                    // 多灶具定时器列表：仅当 timers 数据变化时才重写 DOM，否则只让 ticker 更新数字位
                    let timersArr = data.timers || [];
                    window.__currentTimers = timersArr;
                    let timersKey = timersArr.map(t => t.name + ':' + t.target_timestamp).join('|');
                    let needRender = timersKey !== renderedTimersKey || !card.classList.contains('timer');
                    if (needRender) {
                        renderedTimersKey = timersKey;
                        // 清除已完成集合中已不存在的灶具
                        let currentNames = new Set(timersArr.map(t => t.name));
                        for (let n of Array.from(notifiedDoneSet)) {
                            if (!currentNames.has(n)) notifiedDoneSet.delete(n);
                        }
                        let itemsHtml = '';
                        if (timersArr.length > 0) {
                            itemsHtml = '<div class="timer-list">';
                            timersArr.forEach((t, i) => {
                                itemsHtml += renderTimerItem(t, i, i === activeBurnerIndex);
                            });
                            itemsHtml += '</div>';
                            if (timersArr.length > 1) {
                                itemsHtml += '<div class="kbd-hint"><kbd>←</kbd><kbd>→</kbd> 切换灶具</div>';
                            }
                        }
                        dynamicContent.innerHTML = `
                            <div class="desc" style="margin-bottom:8px;">${data.value}</div>
                            ${itemsHtml}
                        `;
                        startTimerListTicker();
                    }
                } else if (data.card_type === 'recipe_list') {
                    if (countdownTimer) clearInterval(countdownTimer);
                    // 仅在菜谱数据或描述变化时才重写 DOM，避免每 500ms 轮询丢失滚动位置和选中态
                    let itemsJson = JSON.stringify(data.items || []);
                    let needRender = itemsJson !== lastItemsJson || data.value !== lastRecipeValue || !card.classList.contains('recipe_list');
                    if (needRender) {
                        lastItemsJson = itemsJson;
                        lastRecipeValue = data.value;
                        let itemsHtml = '';
                        if (data.items && data.items.length > 0) {
                            // 菜谱列表变化时重置选中索引
                            if (data.items.length !== currentRecipeCount) {
                                selectedRecipeIndex = 0;
                            }
                            currentRecipeCount = data.items.length;
                            itemsHtml = '<div class="recipe-scroll recipe-recommendations">';
                            data.items.forEach((item, i) => {
                                let hasSteps = item.steps && item.steps.length > 0;
                                let selected = i === selectedRecipeIndex;
                                let recommendationCopy = item.description || item.desc || ({
                                    '清淡': '清淡少油，适合你的日常口味',
                                    '高蛋白': '鲜嫩少油，保留食材原味',
                                    '汤羹': '清爽暖胃，搭配主菜刚刚好',
                                    '快手': '简单快手，轻松完成一餐'
                                }[item.tag] || '为你挑选的今日口味');
                                let indexLabel = String(i + 1).padStart(2, '0') + ' / ' + (i === 0 ? '今日首选' : (item.tag || '为你推荐'));
                                let actionLabel = selected ? '查看步骤 →' : '选择菜谱';
                                let actionClick = selected && hasSteps ? `event.stopPropagation(); viewRecipeSteps(${i})` : `event.stopPropagation(); selectRecipe(${i})`;
                                itemsHtml += `
                                    <div class="recipe-item${selected ? ' selected' : ''}" data-index="${i}" data-tag="${item.tag || '为你推荐'}" data-has-steps="${hasSteps}" onclick="selectRecipe(${i})">
                                        <span class="recipe-card-index">${indexLabel}</span>
                                        <div class="recipe-name">${item.name}</div>
                                        <div class="recipe-card-description">${recommendationCopy}</div>
                                        <div class="recipe-meta">
                                            <span>${item.time || '20分钟'}</span>
                                            <button class="view-steps-btn" onclick="${actionClick}">${actionLabel}</button>
                                        </div>
                                    </div>
                                `;
                            });
                            itemsHtml += '</div>';
                            let selectedRecipe = data.items[selectedRecipeIndex] || data.items[0];
                            let recommendationLabel = selectedRecipe.tag || data.value || '今日推荐';
                            itemsHtml = `<div class="recipe-list-context"><span>为你挑选 · ${recommendationLabel}</span><span class="recipe-list-counter">${String(selectedRecipeIndex + 1).padStart(2, '0')} / ${String(currentRecipeCount).padStart(2, '0')}</span></div>${itemsHtml}<div class="recipe-list-footer"><span>${recipeKnobHint()}</span><span class="recipe-return">返回</span></div>`;
                        } else {
                            currentRecipeCount = 0;
                        }
                        dynamicContent.innerHTML = `
                            ${itemsHtml}
                        `;
                    }  // end: recipe_list needRender
                } else if (data.card_type === 'fan') {
                    // 旧版半圆风速主卡片已停用：所有风速指令统一进入 E03 旋钮调节页。
                    if (countdownTimer) clearInterval(countdownTimer);
                    subCardState.fan = data.fan_speed || 1;
                    subCardMode = 'fan';
                    renderSubCard();
                    scheduleSettingAutoClose('fan');
                } else if (data.card_type === 'light') {
                    if (countdownTimer) clearInterval(countdownTimer);
                    subCardState.light = typeof data.light_brightness === 'number' ? data.light_brightness : 100;
                    subCardMode = 'light';
                    renderSubCard();
                    scheduleSettingAutoClose('light');
                } else if (data.card_type === 'recipe_steps') {
                    // 菜谱步骤卡片（由 show_recipe_steps 工具触发）
                    if (countdownTimer) clearInterval(countdownTimer);
                    let total = data.step_total || (data.items ? data.items.length : 0);
                    let idx = data.step_index || 0;
                    // 去重：仅在步骤索引、总数、标题变化时才重写 DOM，避免每 500ms 闪烁
                    let timerKey = (data.timers || []).map(t => t.name + ':' + t.target_timestamp).join('|');
                    let stepKey = data.title + '|' + idx + '|' + total + '|' + timerKey;
                    let needRender = stepKey !== lastStepKey || !card.classList.contains('recipe_steps');
                    if (needRender && total > 0 && data.items && idx < data.items.length) {
                        lastStepKey = stepKey;
                        backendStepIndex = idx;    // 记录当前步骤索引，供旋钮按压判断最后一步
                        backendStepTotal = total;  // 记录步骤总数
                        // 最后一步保留给用户确认；只有无操作满 15 秒才自动退出。
                        if (idx >= total - 1 && !recipeAutoCloseScheduled) {
                            recipeAutoCloseScheduled = true;
                            setTimeout(() => {
                                fetch('/state').then(r=>r.json()).then(d=>{
                                    if (d.card_type === 'recipe_steps' && (d.step_index || 0) >= (d.step_total || 0) - 1) {
                                        fetch('/quick_card?card_type=emotion&title=' + encodeURIComponent('小美随时待命') + '&value=' + encodeURIComponent('做菜完成，祝您用餐愉快！'));
                                    }
                                    recipeAutoCloseScheduled = false;
                                }).catch(()=>{ recipeAutoCloseScheduled = false; });
                            }, RECIPE_FINAL_STEP_IDLE_TIMEOUT_MS);
                        }
                        let step = data.items[idx];
                        let stepText = step.step || step.text || String(step);
                        document.getElementById('badge').innerText = "菜谱步骤";
                        document.getElementById('title').innerText = data.title || '';
                        let phase = step.phase || step.label || '烹饪步骤';
                        dynamicContent.innerHTML = renderStepGuide(idx, total, stepText, phase, data.timers || []);
                        startStepTimerTicker();
                    }
                }
            } catch(e) { console.warn('状态渲染失败', e); }
        }
        // 优先用 SSE 推送（实时，无轮询延迟，不耗尽 HTTP 连接）
        // fallback：SSE 断开时用 2 秒轮询兜底
        let sseConnected = false;
        let fallbackTimer = null;
        let sse = null;
        function showMcpSetup() {
            const panel = document.getElementById('mcp-setup');
            if (panel) panel.classList.add('show');
        }
        function hideMcpSetup() {
            const panel = document.getElementById('mcp-setup');
            if (panel) panel.classList.remove('show');
        }
        async function saveMcpConfig() {
            const input = document.getElementById('mcp-endpoint');
            const status = document.getElementById('mcp-status');
            const endpoint = (input && input.value || '').trim();
            if (!endpoint.startsWith('wss://')) { status.textContent = '请输入以 wss:// 开头的 MCP 地址'; return; }
            status.textContent = '正在保存并连接…';
            try {
                const response = await fetch('/mcp_config', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({endpoint}) });
                const result = await response.json();
                if (!response.ok || !result.ok) throw new Error(result.error || '保存失败');
                status.textContent = '配置已保存，正在连接小智…';
                hideMcpSetup();
                pollState();
            } catch (error) { status.textContent = error.message || '保存失败，请检查地址'; }
        }
        function startSSE() {
            if (sse) return;
            sse = new EventSource('/sse');
            sse.onmessage = function(ev) {
                try {
                    let data = JSON.parse(ev.data);
                    applyState(data);
                } catch(e) {}
            };
            sse.onopen = function() {
                sseConnected = true;
                if (fallbackTimer) { clearInterval(fallbackTimer); fallbackTimer = null; }
            };
            sse.onerror = function() {
                sseConnected = false;
                if (!fallbackTimer) {
                    fallbackTimer = setInterval(pollState, 2000);
                }
                // EventSource 会自行重连；不要额外创建连接，避免重复 SSE 流覆盖界面。
            };
        }
        startSSE();
        // 启动后立即拉一次初始状态
        pollState();

        // overlay ticker 独立启动，长期运行（不依赖主卡片类型）
        startOverlayTicker();

        // 自动按 960x220 设计稿等比缩放适配预览窗口尺寸
        function autoFit() {
            const designW = 960, designH = 220;
            const winW = window.innerWidth;
            const winH = window.innerHeight;
            // 保持 960:220，并尽可能完整占用当前显示区域。
            const scale = Math.min(winW / designW, winH / designH);
            document.documentElement.style.setProperty('--preview-scale', String(scale));
            const c = document.querySelector('.container');
            if (c) {
                c.style.width = designW + 'px';
                c.style.height = designH + 'px';
                c.style.transform = 'scale(' + scale + ')';
            }
        }
        window.addEventListener('resize', autoFit);
        autoFit();
    </script>
</head>
<body>
    <section id="mcp-setup" aria-label="MCP 设置">
        <h1>连接小智 MCP</h1>
        <p>首次使用请输入小智提供的 MCP WebSocket 地址。配置仅保存于本机。</p>
        <input id="mcp-endpoint" type="text" autocomplete="off" spellcheck="false" placeholder="wss://api.xiaozhi.me/mcp/?token=…">
        <span id="mcp-status">未检测到 MCP 配置</span>
        <button id="mcp-save" type="button" onclick="saveMcpConfig()">保存并连接</button>
    </section>
    <div id="timer-overlay">
        <button class="overlay-close" onclick="hideTimerOverlay()" aria-label="关闭">×</button>
        <div class="overlay-badge">即将完成</div>
        <div id="overlay-content"></div>
        <div class="overlay-hint">最后 1 分钟倒计时 · 即将完成</div>
    </div>
    <div id="quick-menu">
        <button class="qm-close" onclick="hideQuickMenu()" aria-label="关闭">×</button>
        <div class="qm-header">
            <div class="qm-title">设置魔术旋钮</div>
            <div class="qm-status">每次只绑定一个功能</div>
        </div>
        <div class="qm-grid">
            <div class="qm-item selected" data-fn="wifi" onclick="selectQuickItem(0)">
                <div class="qm-icon">📶</div>
                <div class="qm-label">WiFi</div>
            </div>
            <div class="qm-item" data-fn="light" onclick="selectQuickItem(1)">
                <div class="qm-icon">💡</div>
                <div class="qm-label">照明</div>
            </div>
            <div class="qm-item" data-fn="fan" onclick="selectQuickItem(2)">
                <div class="qm-icon">🌀</div>
                <div class="qm-label">风速</div>
            </div>
            <div class="qm-item" data-fn="left_timer" onclick="selectQuickItem(3)">
                <div class="qm-icon">⏱️</div>
                <div class="qm-label">左定时</div>
            </div>
            <div class="qm-item" data-fn="right_timer" onclick="selectQuickItem(4)">
                <div class="qm-icon">⏲️</div>
                <div class="qm-label">右定时</div>
            </div>
        </div>
        <div class="qm-hint">语音：设定魔术旋钮功能</div>
    </div>
    <div class="container">
        <div id="done-banner" class="done-banner"></div>
        <section id="pot-fire-match" aria-label="锅与火形适配">
            <article class="pot-fire-card left">
                <div class="pot-fire-card-label" data-pot="left-label"></div>
                <div class="pot-fire-dots" data-pot="left-dots"></div>
                <img class="pot-fire-image" data-pot="left-image" alt="左灶锅具与火形">
            </article>
            <article class="pot-fire-card right">
                <div class="pot-fire-card-label" data-pot="right-label"></div>
                <div class="pot-fire-dots" data-pot="right-dots"></div>
                <img class="pot-fire-image" data-pot="right-image" alt="右灶锅具与火形">
            </article>
        </section>
        <div id="main-card" class="card emotion">
            <div id="badge" class="badge">AI 状态: EMOTION</div>
            <div id="title" class="title">加载中...</div>
            <div id="dynamic-content"></div>
        </div>
    </div>
</body>
</html>
"""

class UIHandler(SimpleHTTPRequestHandler):
    # 必须用 HTTP/1.1 才能支持 SSE 持久连接（HTTP/1.0 会关闭连接）
    protocol_version = "HTTP/1.1"
    def do_POST(self):
        self.close_connection = True
        if self.path != "/mcp_config":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("配置内容无效")
            body = self.rfile.read(length)
            endpoint = json.loads(body.decode("utf-8")).get("endpoint", "")
            save_mcp_endpoint(endpoint)
            with state_lock:
                app_state["mcp_configured"] = True
            broadcast_sse()
            payload = json.dumps({"ok": True, "configured": True}, ensure_ascii=False)
            self.send_response(200)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            payload = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
            self.send_response(400)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def do_GET(self):
        # Normal JSON/HTML responses must close so fetch() can complete without a Content-Length header.
        self.close_connection = True
        request_path = urlparse(self.path).path
        if request_path == "/healthz":
            payload = json.dumps({"ok": True, "cloud_state_store": cloud_state_store.enabled}, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif request_path == "/api/state":
            # Android 云端同步入口。数据库永不暴露给 APK，只返回经 MCP 更新后的状态快照。
            if not has_api_access(self.headers):
                payload = json.dumps({"ok": False, "error": "unauthorized"}, ensure_ascii=False)
                self.send_response(401)
            else:
                payload = json.dumps({"ok": True, "state": api_state_snapshot()}, ensure_ascii=False)
                self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path in ("/", ""):
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
        elif self.path.startswith("/assets/expressions/"):
            # 仅暴露待机页所需的本地表情视频，避免把项目目录作为通用静态目录开放。
            from urllib.parse import urlparse, unquote
            filename = os.path.basename(unquote(urlparse(self.path).path))
            asset_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "expressions", filename)
            if filename.endswith(".mp4") and os.path.isfile(asset_path):
                self.send_response(200)
                self.send_header("Content-type", "video/mp4")
                self.send_header("Content-Length", str(os.path.getsize(asset_path)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(asset_path, "rb") as asset_file:
                    self.wfile.write(asset_file.read())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path.startswith("/assets/pot-fire-match/"):
            # 锅与火形适配页仅允许读取该专用素材目录中的 PNG，路径取 basename 防止目录穿越。
            from urllib.parse import urlparse, unquote
            filename = os.path.basename(unquote(urlparse(self.path).path))
            asset_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "pot-fire-match", filename)
            if filename.endswith(".png") and os.path.isfile(asset_path):
                self.send_response(200)
                self.send_header("Content-type", "image/png")
                self.send_header("Content-Length", str(os.path.getsize(asset_path)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(asset_path, "rb") as asset_file:
                    self.wfile.write(asset_file.read())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/state":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with state_lock:
                payload = json.dumps(app_state, ensure_ascii=False)
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/voice_state"):
            # 给语音接入层/本地联调使用：/voice_state?action=listening|thinking|idle
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            action = q.get("action", [""])[0]
            changed, snapshot = set_voice_lifecycle(action)
            payload = json.dumps({"ok": changed, "action": action, "state": snapshot}, ensure_ascii=False)
            self.send_response(200 if changed else 400)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/ui_ack"):
            # 浏览器在完成一帧渲染后确认版本，供 MCP 工具在语音播报前短暂等待。
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                revision = int(q.get("revision", ["0"])[0])
            except (ValueError, TypeError):
                revision = 0
            with state_lock:
                revision = min(revision, int(app_state.get("revision", 0)))
                app_state["ui_ack_revision"] = max(int(app_state.get("ui_ack_revision", 0)), revision)
                payload = json.dumps({"ok": True, "revision": app_state["ui_ack_revision"]}, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path == "/sse":
            # SSE 端点：保持连接，状态变化时推送
            self.close_connection = False
            self.send_response(200)
            self.send_header("Content-type", "text/event-stream")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # 立即推一次当前状态
            with state_lock:
                payload = json.dumps(app_state, ensure_ascii=False)
            try:
                self.wfile.write(f"retry: 3000\n\n".encode("utf-8"))
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                return
            # 注册此客户端，后续由 broadcast_sse 推送
            sse_clients.add(self.wfile)
            # 保持连接直到客户端断开
            try:
                import time as _t
                while True:
                    _t.sleep(30)
                    # 每 30 秒发一个 SSE 注释作为 keepalive（不触发前端 onmessage）
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                sse_clients.discard(self.wfile)
        elif self.path == "/test_recipe":
            # 测试端点：直接设置菜谱列表状态，验证卡片渲染
            with state_lock:
                app_state["card_type"] = "recipe_list"
                app_state["title"] = "今日推荐"
                app_state["value"] = "三道快手家常菜"
                app_state["items"] = [
                    {"name": "番茄炒蛋", "time": "10分钟", "tag": "快手",
                     "steps": ["鸡蛋打散加少许盐搅匀", "热锅冷油倒入蛋液炒至凝固盛出", "锅中加油炒香西红柿", "倒入鸡蛋翻炒均匀", "加盐和少许糖调味"]},
                    {"name": "清炒时蔬", "time": "8分钟", "tag": "清淡",
                     "steps": ["蔬菜洗净切好", "热锅加油", "放入蒜末爆香", "倒入蔬菜大火快炒", "加盐调味即可"]},
                    {"name": "青椒肉丝", "time": "15分钟", "tag": "下饭",
                     "steps": ["猪肉切丝加淀粉腌制", "青椒切丝", "热锅加油炒肉丝至变色盛出", "锅中炒香青椒", "倒入肉丝翻炒加酱油调味"]}
                ]
                payload = json.dumps(app_state, ensure_ascii=False)
            broadcast_sse()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/fan_speed"):
            # 本地风速调节接口（音量键触发）：?speed=1-6（6=爆炒档）
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            keep_page = q.get("keep_page", ["0"])[0] in ("1", "true")
            try:
                s = max(1, min(6, int(q.get("speed", ["1"])[0])))
            except (ValueError, IndexError):
                s = 1
            with state_lock:
                # 魔术旋钮绑定功能在菜谱/步骤页内调节时只更新设备状态，
                # 不得把主画面切换成风速页。
                if not keep_page:
                    app_state["card_type"] = "fan"
                    app_state["title"] = "油烟机风速"
                    app_state["value"] = f"已调至{'爆炒' if s >= 6 else f' {s} 档' + ('强排风' if s >= 4 else '中排风' if s == 3 else '弱排风')}"
                app_state["fan_speed"] = s
                payload = json.dumps(app_state, ensure_ascii=False)
            logger.info(f"--> Local Fan Speed Adjust: {s} 档（音量键触发）")
            broadcast_sse()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/quick_state"):
            # 快捷菜单状态更新：?wifi=0/1 或 ?light=0-100
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            with state_lock:
                if "wifi" in q:
                    try: app_state["wifi_on"] = q["wifi"][0] == "1"
                    except: pass
                    logger.info(f"--> Quick WiFi: {'ON' if app_state['wifi_on'] else 'OFF'}")
                if "light" in q:
                    try: app_state["light_brightness"] = max(0, min(100, int(q["light"][0])))
                    except: pass
                    logger.info(f"--> Quick Light: {app_state['light_brightness']}%")
                payload = json.dumps(app_state, ensure_ascii=False)
            broadcast_sse()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/quick_timer"):
            # 快捷菜单定时器：?burner=左灶&minutes=10
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            keep_page = q.get("keep_page", ["0"])[0] in ("1", "true")
            try:
                burner = q.get("burner", ["灶台"])[0]
                minutes = max(0, min(120, int(q.get("minutes", ["10"])[0])))
            except (ValueError, IndexError):
                burner, minutes = "灶台", 10
            with state_lock:
                if minutes <= 0:
                    # minutes=0 代表关闭定时：移除该灶具已有定时器
                    cancelled = remove_timer(burner)
                    if not keep_page:
                        app_state["card_type"] = "emotion"
                        app_state["emotion"] = "happy"
                        app_state["title"] = ""
                        app_state["value"] = f"{cancelled}已关闭定时" if cancelled else "定时已关闭"
                else:
                    target_ts = time.time() + minutes * 60
                    app_state["target_timestamp"] = target_ts
                    # 归一化灶具名并写入（保证唯一性，以最后设定为准）
                    burner = upsert_timer(burner, target_ts, minutes)
                    # 绑定旋钮从菜谱页确认时，只更新任务本身，不抢占当前页面。
                    if not keep_page:
                        app_state["card_type"] = "emotion"
                        app_state["emotion"] = "happy"
                        app_state["title"] = ""
                        app_state["value"] = f"{burner}已设定时 {minutes} 分钟" if burner else "定时设置失败"
                payload = json.dumps(app_state, ensure_ascii=False)
            logger.info(f"--> Quick Timer: {burner} {minutes} 分钟")
            broadcast_sse()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/timer_done"):
            # 完成提醒只展示一次；前端确认展示后写回，刷新/重连不再重复弹出。
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            burner = normalize_burner_name(q.get("burner", [""])[0])
            with state_lock:
                if burner:
                    for timer in app_state.get("timers", []):
                        if timer.get("name") == burner and timer.get("target_timestamp", 0) <= time.time():
                            timer["done_notified"] = True
                payload = json.dumps({"ok": bool(burner)}, ensure_ascii=False)
            broadcast_sse()
            self.send_response(200 if burner else 400)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/quick_temperature"):
            # 固定油温确认后持久化，待机页据此显示自动控温信息。
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            burner = normalize_burner_name(q.get("burner", [""])[0])
            active = q.get("active", ["1"])[0] not in ("0", "false", "complete", "completed")
            try:
                temperature = max(80, min(260, int(q.get("temperature", ["180"])[0])))
            except (ValueError, IndexError):
                temperature = 180
            with state_lock:
                if burner:
                    temperatures = app_state.setdefault("burner_temperatures", {})
                    side = "left" if burner == "左灶" else "right"
                    if active:
                        temperatures[side] = temperature
                    else:
                        temperatures.pop(side, None)
                payload = json.dumps(app_state, ensure_ascii=False)
            logger.info(f"--> Fixed oil temperature: {burner or 'unknown'} {'active ' + str(temperature) + '°C' if active else 'completed'}")
            broadcast_sse()
            self.send_response(200 if burner else 400)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/recipe_steps"):
            # 前端请求菜谱步骤（无本地 steps 时回退）：?dish=菜名
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            dish = q.get("dish", ["未知菜品"])[0]
            with state_lock:
                # 尝试从当前菜谱列表中查找该菜的 steps
                found = None
                for item in app_state.get("items", []):
                    if item.get("name") == dish and item.get("steps"):
                        found = item["steps"]
                        break
                if found:
                    app_state["card_type"] = "recipe_steps"
                    app_state["title"] = dish
                    app_state["value"] = f"{dish} · 共{len(found)}步"
                    app_state["items"] = [{"step": s, "index": i+1, "total": len(found)} for i, s in enumerate(found)]
                    app_state["step_index"] = 0
                    app_state["step_total"] = len(found)
                    # 保存步骤快照，供 navigate_steps 恢复使用
                    app_state["current_steps_snapshot"] = {
                        "dish_name": dish,
                        "steps": found,
                        "step_total": len(found)
                    }
                payload = json.dumps(app_state, ensure_ascii=False)
            logger.info(f"--> Recipe Steps: {dish} (found={bool(found)})")
            broadcast_sse()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/step_nav"):
            # 前端步骤导航：?action=next|prev|first|last|jump&index=N
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            action = q.get("action", ["next"])[0]
            with state_lock:
                # 如果 card_type 不是 recipe_steps（被 recipe_list 等覆盖），
                # 从保存的步骤快照恢复 recipe_steps 卡片
                snap = app_state.get("current_steps_snapshot")
                if snap and app_state["card_type"] != "recipe_steps":
                    steps = snap["steps"]
                    dish_name = snap["dish_name"]
                    app_state["card_type"] = "recipe_steps"
                    app_state["title"] = dish_name
                    app_state["value"] = f"{dish_name} · 共{len(steps)}步"
                    app_state["items"] = [{"step": s, "index": i+1, "total": len(steps)} for i, s in enumerate(steps)]
                    app_state["step_total"] = len(steps)
                    app_state["step_index"] = 0

                total = app_state.get("step_total", 0)
                cur = app_state.get("step_index", 0)
                if action == "next":
                    new_idx = min(total - 1, cur + 1)
                elif action == "prev":
                    new_idx = max(0, cur - 1)
                elif action == "first":
                    new_idx = 0
                elif action == "last":
                    new_idx = max(0, total - 1)
                elif action == "jump":
                    try: new_idx = max(0, min(total - 1, int(q.get("index", ["1"])[0]) - 1))
                    except: new_idx = cur
                else:
                    new_idx = cur
                app_state["step_index"] = new_idx
                payload = json.dumps(app_state, ensure_ascii=False)
            logger.info(f"--> Step Nav: {action} → {new_idx+1}/{total}")
            broadcast_sse()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/magic_knob_binding"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            binding = q.get("function", [""])[0]
            allowed = {"hood", "left_timer", "right_timer", "dishwasher"}
            with state_lock:
                if binding in allowed:
                    app_state["magic_knob_binding"] = binding
                    app_state["card_type"] = "emotion"
                    app_state["title"] = ""
                    app_state["value"] = "当前控制：" + {"hood":"烟机日常功能", "left_timer":"左灶定时关火", "right_timer":"右灶定时关火", "dishwasher":"洗碗机预约"}[binding]
                payload = json.dumps(app_state, ensure_ascii=False)
            broadcast_sse()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path.startswith("/quick_card"):
            # 快捷菜单触发的卡片切换：?card_type=&title=&value=
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            ct = q.get("card_type", ["emotion"])[0]
            title = q.get("title", ["快捷功能"])[0]
            value = q.get("value", [""])[0]
            requested_emotion = q.get("emotion", [""])[0]
            with state_lock:
                app_state["card_type"] = ct
                if ct == "emotion" and requested_emotion:
                    app_state["emotion"] = requested_emotion
                # 当切回 emotion 待命且未显式传 title 时，按 user_name 应用个性化问候（含专属表情）
                if ct == "emotion" and not requested_emotion and (not title or title == "快捷功能" or title == "小美提示"):
                    uname = app_state.get("user_name", "")
                    emo, title, value = resolve_standby_greeting(uname)
                    app_state["emotion"] = emo
                # 切回 recipe_list 时，清除旧 items（可能是步骤数据），用内置菜谱库填充，避免显示 undefined
                if ct == "recipe_list":
                    app_state["items"] = [dict(dish) for dish in DEFAULT_RECIPE_LIBRARY]
                    app_state["step_index"] = 0
                    app_state["step_total"] = 0
                app_state["title"] = title
                app_state["value"] = value
                payload = json.dumps(app_state, ensure_ascii=False)
            logger.info(f"--> Quick Menu Switch: {ct} / {title} (user={app_state.get('user_name','')})")
            broadcast_sse()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        return

class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def run_web():
    # 本地默认 5050；CloudBase 通过 PORT/HOST 注入 8080/0.0.0.0。
    port = int(os.environ.get("PORT", os.environ.get("UI_PORT", "5050")))
    host = os.environ.get("HOST", "127.0.0.1")
    with ThreadedHTTPServer((host, port), UIHandler) as httpd:
        logger.info(f"Web Server started at http://{host}:{port}")
        httpd.serve_forever()

async def mcp_worker():
    while True:
        endpoint = load_mcp_endpoint()
        if not endpoint:
            # 保持 UI 服务可用，等待首页首次保存配置后再建立连接。
            await asyncio.sleep(1)
            continue
        try:
            # 高频保活：ping_interval=3s 防止服务端在 ping 间隙回收连接导致工具调用丢失；
            # close_timeout=0 让断连快速释放，配合 0.3s 重连把工具调用丢失窗口压到最小。
            async with websockets.connect(endpoint, ping_interval=3, ping_timeout=3, close_timeout=0) as websocket:
                logger.info("Connected to XiaoZhi MCP Cloud.")
                async for message in websocket:
                    msg = json.loads(message)
                    method = msg.get("method")
                    msg_id = msg.get("id")
                    logger.info(f"[MCP recv] method={method} id={msg_id}")

                    if method == "initialize":
                        res = {"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "SmartKitchenGenUI", "version": "3.0.0"}}}
                        await websocket.send(json.dumps(res))
                    elif method == "ping":
                        res = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
                        await websocket.send(json.dumps(res))
                    elif method in ("voice/state", "voice_state"):
                        # 兼容语音接入层直接推送生命周期事件，不要求经由 LLM 工具调用。
                        params = msg.get("params", {})
                        changed, snapshot = set_voice_lifecycle(params.get("action") if isinstance(params, dict) else "")
                        logger.info(f"[MCP voice state] action={params.get('action', '') if isinstance(params, dict) else ''} changed={changed}")
                        if msg_id is not None:
                            res = {"jsonrpc": "2.0", "id": msg_id, "result": {"success": changed, "state": snapshot}}
                            await websocket.send(json.dumps(res, ensure_ascii=False))
                    elif method == "tools/list":
                        res = {
                            "jsonrpc": "2.0", "id": msg_id, 
                            "result": {
                                "tools": [
                                    {
                                        "name": "update_generative_ui",
                                        "description": "厨房生成式 UI 控制器。【声纹识别规则】如果系统已识别出当前说话人身份，每次调用本工具时必须在 arguments 里带 user_name 字段（填识别到的姓名，如'张总'），用于个性化待命页面显示。未识别时省略此字段。【打招呼强制显示规则】当用户打招呼（'你好''早上好''晚上好''小美'等）或系统识别到声纹身份时，必须立即调用本工具更新屏幕：card_type=emotion, emotion=eyes, user_name=识别到的姓名。屏幕会显示眼睛表情+用户名+个性化问候。禁止只语音回复而不更新屏幕，每次打招呼都一定要让屏幕显示出来。\n根据用户意图选择卡片类型：\n- emotion: 待机/表情卡片（默认，闲聊、问候、等待指令、思考时用）\n- timer: 灶台倒计时卡片。当用户表达任何与时间、计时、火候控制相关的需求时必须用此卡片，包括但不限于：'定时X分钟'、'X分钟后提醒/关火/熄火'、'左灶/右灶计时'、'煲汤X分钟'、'煮X分钟'、'蒸X分钟'、'炖X分钟'、'烧X分钟'、'炸X分钟'、'炒X分钟'、'X分钟后关火/熄火/起锅'、'X分钟后叫我'、'X分钟后提醒我'、'火候X分钟'。识别要点：只要用户提到具体时长（X分钟/X秒）+烹饪动作或关火/提醒等意图，就调用此卡片。value 必须填纯时长字符串如'15分钟'，title 必须含灶具名'左灶定时'或'右灶定时'（必须明确指定左灶或右灶，不支持'灶台'默认）。修改定时：用户说'左灶改成5分钟''右灶调到10分钟'时，调用 timer 卡片设置新时间，会自动覆盖旧定时。删除定时：用户说'取消左灶定时''删掉右灶定时''左灶不要定时了'时，调用 cancel_timer 工具。\n- recipe_list: 菜谱推荐列表卡片。【最高优先级强制规则】只要用户提到任何与菜谱、菜品、做菜、吃什么相关的内容（无论当前在什么卡片、无论用户用什么措辞），都必须立即调用此卡片，替换当前显示的其他卡片。触发词包括但不限于：'做什么菜'、'推荐菜谱'、'今天吃什么'、'有什么菜'、'给推荐几道菜'、'有什么好吃的'、'不知道做什么菜'、'晚餐吃什么'、'午饭做什么'、'早餐吃什么'、'周末吃什么'、'朋友来吃饭做什么'、'家常菜'、'快手菜'、'硬菜'、'下饭菜'、'清淡点'、'红烧XX怎么做'、'XX怎么做'、'XX做法'、'XX菜谱'、'怎么做XX菜'、'XX怎么炒'、'给我几个菜谱'、'来几道菜'、'看看菜谱'、'有什么菜谱'、'菜谱'、'菜'。识别要点：只要用户提到'吃什么''做什么''推荐菜''菜谱''怎么做菜''做法'等，就必须调用此卡片，必须在 items 中返回 3-5 道菜（每道含 name 菜名、time 烹饪时长、tag 标签、steps 步骤数组），不允许只口头回答而不显示卡片。调用此卡片会自动替换当前的定时器/风速/表情等卡片，显示菜谱列表。value 填一句推荐语如'三道快手家常菜'。\n- fan: 油烟机风速调节卡片。当用户提到油烟/排风/风速相关需求时必须用此卡片，包括：'烟太大了'、'油烟好大'、'开大排风'、'风速调到X档'、'加大风速'、'减小风速'、'油烟机关小点'、'排烟开大'、'烟好呛'等。识别要点：只要用户提到油烟、烟、排风、风速档位等，就调用此卡片。fan_speed 填 1-6 的数字（'烟太大了'→4或5档；'爆炒'→6；'调到3档'→3；'关小点'→2；'风速大一点'→当前+1不超6）。开机默认 1 档，最高 6 档(爆炒)。value 填一句状态描述如'已调至 4 档强排风'。\n\n重要：用户一句话可能同时包含多个意图（如'煲汤定15分钟关火'必须调 timer；'烟太大了调到5档'必须调 fan；'推荐几道菜'必须调 recipe_list），不能因为提到烹饪就只回答菜谱或闲聊。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "card_type": {"type": "string", "description": "卡片类型：emotion | timer | recipe_list | fan"},
                                                "emotion": {"type": "string", "description": "表情，仅 card_type=emotion 时用：listening=正在接收用户讲话（必须传，循环）；thinking=收音结束等待结果（必须传，循环）；happy=操作成功确认（单次）；hungry=空闲俏皮动效（单次）；breathing=普通待机（循环）；eyes=识别用户打招呼表情（每次打招呼都用）"},
                                                "title": {"type": "string", "description": "卡片标题，如'小美随时待命'、'左灶定时'、'今日推荐'、'油烟机风速'。timer 时必须含灶具名"},
                                                "value": {"type": "string", "description": "卡片描述文字。timer 时必须填纯时长如'15分钟'（用于解析计时分钟数）；recipe_list 时填推荐语；fan 时填状态描述如'已调至 4 档强排风'"},
                                                "timer_mode": {"type": "string", "enum": ["reminder", "shutoff"], "description": "仅 card_type=timer。reminder=普通提醒计时，不关火；菜谱步骤中用户说‘计时/提醒’时必须用此值，title 填‘计时提醒’，不需要灶具名。shutoff=定时关火；仅用户明确说‘左灶/右灶’且要求‘关火/熄火’时使用，title 必须为‘左灶定时’或‘右灶定时’。不得把普通计时当作关火。"},
                                                "fan_speed": {"type": "integer", "description": "油烟机风速档位 1-6，仅 card_type=fan 时用。1=弱(2.5m/s) 2=中弱(4.0) 3=中(5.5) 4=中强(7.0) 5=强(8.5) 6=爆炒档(10.0)。用户未明确档位时：'爆炒'→6，'烟太大了'→4或5，'大一点'→当前+1(不超6)，'小一点'→当前-1(不低于1)"},
                                                "user_name": {"type": "string", "description": "当前说话人姓名（声纹识别后由 AI 填入）。用于个性化待命页面。当系统识别到具体用户时，必须传此字段；emotion 待机卡片会根据此字段显示个性化问候。未识别时省略此字段。示例：'张总'、'李姐'"},
                                                "items": {
                                                    "type": "array",
                                                    "description": "菜谱数组（card_type=recipe_list 时必填，返回 3-5 道菜）。每项含 name(菜名)、time(烹饪时长如'15分钟')、tag(标签如'清淡'/'快手'/'下饭')、steps(操作步骤数组，3-8步，每步一句话描述)",
                                                    "items": {
                                                        "type": "object",
                                                        "properties": {
                                                            "name": {"type": "string", "description": "菜名，如'番茄炒蛋'"},
                                                            "time": {"type": "string", "description": "烹饪时长，如'15分钟'"},
                                                            "tag": {"type": "string", "description": "标签，如'清淡'/'快手'/'下饭'/'硬菜'"},
                                                            "steps": {
                                                                "type": "array",
                                                                "description": "操作步骤数组（3-8步），每步一句话，如'鸡蛋打散加少许盐搅匀'",
                                                                "items": {"type": "string"}
                                                            }
                                                        },
                                                        "required": ["name", "time", "tag", "steps"]
                                                    }
                                                }
                                            },
                                            "required": ["card_type", "title", "value"]
                                        }
                                    },
                                    {
                                        "name": "set_voice_state",
                                        "description": "语音生命周期状态同步工具。仅在默认待机页生效，不会覆盖菜谱、定时或设备控制画面。语音接收刚开始时必须调用 action=listening；用户说完、正在理解或等待设备执行结果时必须调用 action=thinking；回复或操作结束后必须调用 action=idle，恢复待机表情。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "action": {"type": "string", "enum": ["listening", "thinking", "idle"], "description": "listening=正在收音；thinking=识别结束/等待回答或执行；idle=回复完成，恢复待机"}
                                            },
                                            "required": ["action"]
                                        }
                                    },
                                    {
                                        "name": "set_burner_temperature_state",
                                        "description": "同步灶具固定油温执行状态，用于默认待机页信息卡。开始/更新控温时传 action=active、burner=左灶或右灶、temperature=温度；灶具控温任务结束、取消或已完成时必须传 action=completed，页面会自动关闭该灶具的定温卡片。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "burner": {"type": "string", "enum": ["左灶", "右灶"]},
                                                "action": {"type": "string", "enum": ["active", "completed"]},
                                                "temperature": {"type": "integer", "minimum": 80, "maximum": 260}
                                            },
                                            "required": ["burner", "action"]
                                        }
                                    },
                                    {
                                        "name": "show_magic_knob_setup",
                                        "description": "当用户说‘设置魔术旋钮’、‘打开魔术旋钮设置’、‘配置旋钮’、‘我要设置旋钮’或‘魔术旋钮怎么设置’时必须调用。若用户只要求查看或选择功能，不传 function，显示 D01。若用户已明确说出要绑定烟机、左灶、右灶或洗碗机功能，必须传 function：系统将直接完成绑定、语音确认后自动退出设置页，回到默认页面。",
                                        "inputSchema": {"type": "object", "properties": {"function": {"type": "string", "enum": ["hood", "left_timer", "right_timer", "dishwasher"], "description": "用户明确指定的旋钮绑定功能；仅查看设置页时省略"}}}
                                    },
                                    {
                                        "name": "restore_page_navigation",
                                        "description": "当用户说‘恢复页面操作’、‘旋钮恢复翻页’、‘旋钮控制菜谱’或‘退出旋钮专用控制’时必须调用。清除当前魔术旋钮专属绑定；之后旋钮左右键和按下键重新操作当前页面的菜谱选择、步骤切换等功能。",
                                        "inputSchema": {"type": "object", "properties": {}}
                                    },
                                    {
                                        "name": "set_light_control",
                                        "description": "照明语音控制专用工具。用户说‘开灯’、‘关灯’、‘打开照明’、‘照明调亮/调暗’、‘灯光调到百分之 X’、‘亮一点/暗一点’时必须调用。调用后立即显示 E02 照明控制页面。power=false 表示关灯；power=true 表示开灯。明确亮度时传 brightness（0-100）；未明确亮度时，开灯默认 100，关灯默认 0。",
                                        "inputSchema": {"type": "object", "properties": {"power": {"type": "boolean", "description": "true=开灯，false=关灯；只调亮度时可省略"}, "brightness": {"type": "integer", "minimum": 0, "maximum": 100, "description": "目标亮度百分比 0-100"}}}
                                    },
                                    {
                                        "name": "query_fan_speed",
                                        "description": "查询当前油烟机风速档位（1-6，6=爆炒档）。当用户说'风速大一点/小一点'、'加大/减小排风'等需要相对调节时，先调用此工具获取当前档位，再加 1 或减 1 后通过 update_generative_ui 的 fan 卡片设置新档位。也可用于用户问'现在风速几档'、'油烟机开到几档了'等查询。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {},
                                            "required": []
                                        }
                                    },
                                    {
                                        "name": "query_timers",
                                        "description": "查询当前所有灶具定时器状态。用户问'还有多少时间'、'定时到哪了'、'现在有几个灶在烧'、'左灶还剩多久'、'定时器状态'等需要查阅进行中或刚完成的定时器时调用。返回每个定时器的灶具名、剩余秒数、状态（running/done）。完成后定时器仍保留在记忆中，便于用户查阅。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {},
                                            "required": []
                                        }
                                    },
                                    {
                                        "name": "cancel_timer",
                                        "description": "取消/删除指定灶具的定时器。用户说'取消左灶定时'、'删掉右灶定时'、'左灶不要定时了'、'停止左灶计时'、'右灶定时关掉'、'取消所有定时'、'清除定时器'时调用。参数 burner 填'左灶'或'右灶'指定要取消的灶具；burner 留空或填'all'则取消所有定时器。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "burner": {"type": "string", "description": "要取消的灶具名：'左灶' / '右灶' / 'all'(默认，取消全部)"}
                                            },
                                            "required": []
                                        }
                                    },
                                    {
                                        "name": "start_cooking",
                                        "description": "【优先级高于 recipe_list】当用户说‘做菜’、‘开始做菜’、‘开始做’、‘我要做饭’、‘做这个’、‘就做这道’或‘开始吧’时必须调用，直接打开菜谱操作步骤页并只显示/播报第 1 步。dish_name 可选：用户指定菜名时填写；未指定时使用当前推荐列表的第一道菜，若没有推荐则用内置番茄炒蛋。调用后不得只显示菜谱推荐列表。后续用户说‘下一步’必须调用 navigate_steps。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {"dish_name": {"type": "string", "description": "要开始制作的菜名；未指定时省略"}},
                                            "required": []
                                        }
                                    },
                                    {
                                        "name": "show_recipe_steps",
                                        "description": "【仅限查看已推荐菜谱的详细步骤时调用】。此工具不用于菜谱推荐。当菜谱列表卡片（recipe_list）已显示，且用户明确要求查看某道菜的做法时才调用，例如用户说'番茄炒蛋怎么做'（且番茄炒蛋已在菜谱列表中）、'第一道菜的步骤'、'第二个怎么做'、'这道菜怎么做'、'查看步骤'。如果用户只是问'做什么菜''推荐菜谱''今天吃什么'，必须先调用 update_generative_ui 的 recipe_list 卡片返回 3-5 道菜，不要直接调用本工具。参数 dish_name 填菜名（如'番茄炒蛋'），steps 填 3-8 步操作步骤（每步一句话如'鸡蛋打散加少许盐搅匀'）。\n\n【关键同步规则】调用此工具后 UI 只显示第1步。你只能语音播报第1步内容。绝对禁止一次性播报多步！用户说'下一步'时，必须调用 navigate_steps action=next 同步 UI 到下一步，等工具返回后再播报该步内容。每播报一步前都要先调 navigate_steps，保证屏幕显示和语音完全同步。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "dish_name": {"type": "string", "description": "菜名，如'番茄炒蛋'"},
                                                "steps": {
                                                    "type": "array",
                                                    "description": "操作步骤数组（3-8步），每步一句话描述具体操作，如'鸡蛋打散加少许盐搅匀'、'热锅冷油倒入蛋液翻炒至凝固盛出'",
                                                    "items": {"type": "string"}
                                                }
                                            },
                                            "required": ["dish_name", "steps"]
                                        }
                                    },
                                    {
                                        "name": "navigate_steps",
                                        "description": "语音控制菜谱步骤切换。【重要：必须严格区分 next 和 prev！】当用户在查看菜谱步骤时：\n- 用户说'下一步'、'继续'、'接下来'、'下一个步骤'、'然后呢'、'再下一步' → action 必须填 'next'\n- 用户说'上一步'、'返回上一步'、'退一步'、'上一个步骤'、'刚才那步' → action 填 'prev'\n- 用户说'第一步'、'回到开头'、'重新开始' → action 填 'first'\n- 用户说'最后一步'、'最后' → action 填 'last'\n- 用户说'跳到第X步'、'直接看第X步' → action 填 'jump'，step_index 填 X\n- 【仅最后一步】用户说'确认'、'完成'、'退出'、'结束做菜'、'不用了' → action 必须填 'exit'，页面立即退出到默认待机页；不要等待默认超时。\n\n【严禁】用户说'下一步'时调用 prev，这会导致页面没有变化，用户体验极差。\n\n【关键同步规则】此工具的职责是同步 UI 显示和语音播报。调用此工具后 UI 会切换到对应步骤，你必须在工具返回后再播报该步骤的具体操作内容。绝对禁止不调用此工具就直接语音播报下一步——那会导致屏幕和语音不同步。每播报一步前必须先调本工具。",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "action": {"type": "string", "description": "导航动作：next=下一步 / prev=上一步 / first=第一步 / last=最后一步 / jump=跳转 / exit=最后一步确认或退出，立即回待机页"},
                                                "step_index": {"type": "integer", "description": "跳转到的步骤序号（从1开始），仅 action=jump 时用"}
                                            },
                                            "required": ["action"]
                                        }
                                    }
                                ]
                            }
                        }
                        await websocket.send(json.dumps(res))
                    elif method == "tools/call":
                        params = msg.get("params", {})
                        tool_name = params.get("name")
                        args = params.get("arguments", {})
                        # 调试：打印原始 params 结构，排查小美 MCP 是否下发说话人/声纹用户信息
                        logger.info(f"[MCP tools/call] tool={tool_name} user_name={args.get('user_name', '') if isinstance(args, dict) else ''}")
                        
                        if tool_name == "set_burner_temperature_state":
                            burner = normalize_burner_name(args.get("burner") if isinstance(args, dict) else "")
                            action = args.get("action") if isinstance(args, dict) else ""
                            try:
                                temperature = max(80, min(260, int(args.get("temperature", 180))))
                            except (ValueError, TypeError):
                                temperature = 180
                            with state_lock:
                                if burner:
                                    temperatures = app_state.setdefault("burner_temperatures", {})
                                    side = "left" if burner == "左灶" else "right"
                                    if action == "active":
                                        temperatures[side] = temperature
                                    elif action == "completed":
                                        temperatures.pop(side, None)
                                    else:
                                        burner = None
                                snapshot = app_state.copy()
                            if burner:
                                broadcast_sse()
                                res_text = json.dumps({"success": True, "state": snapshot}, ensure_ascii=False)
                            else:
                                res_text = json.dumps({"success": False, "error": "请指定左灶或右灶以及有效状态"}, ensure_ascii=False)
                        elif tool_name == "set_voice_state":
                            changed, snapshot = set_voice_lifecycle(args.get("action") if isinstance(args, dict) else "")
                            if changed:
                                logger.info(f"--> Voice lifecycle: {args.get('action')}")
                                res_text = json.dumps({"success": True, "state": snapshot}, ensure_ascii=False)
                            else:
                                res_text = json.dumps({"success": False, "message": "当前不是默认待机页，已保留现有画面"}, ensure_ascii=False)
                        elif tool_name == "update_generative_ui":
                            with state_lock:
                                # 在菜谱步骤中创建定时器时，定时器是步骤页的附属信息，
                                # 不能用 timer 卡片覆盖步骤、标题和导航进度。
                                preserve_recipe_steps = None
                                if (args.get("card_type") == "timer" and
                                        app_state.get("card_type") == "recipe_steps"):
                                    preserve_recipe_steps = {
                                        key: app_state.get(key)
                                        for key in ("card_type", "emotion", "title", "value", "items",
                                                    "step_index", "step_total", "current_steps_snapshot")
                                    }
                                app_state["card_type"] = args.get("card_type", "emotion")
                                app_state["emotion"] = args.get("emotion", "happy")
                                app_state["title"] = args.get("title", "小美提示")
                                app_state["value"] = args.get("value", "")
                                app_state["items"] = args.get("items", [])
                                # 提取 AI 传来的说话人姓名，用于个性化待命页面
                                # 即使本次不是 emotion 卡片也保存，以便切回待命时显示个性化问候
                                if "user_name" in args and args["user_name"]:
                                    app_state["user_name"] = args["user_name"]
                                    logger.info(f"[MCP] user_name 更新: {args['user_name']}")
                                # 明确传入聆听/思考等交互状态时必须原样保留；
                                # 仅未指定状态或普通待机时才应用首次问候/默认呼吸兜底。
                                # 声纹识别到具体用户时优先显示个性化打招呼（含名称+问候+眼睛表情）。
                                if app_state["card_type"] == "emotion":
                                    uname = app_state.get("user_name", "")
                                    requested_emotion = args.get("emotion")
                                    # listening/thinking 是对话中状态，不打断；其余（含 happy 打招呼）都应用个性化问候
                                    is_explicit_interaction = requested_emotion in ("listening", "thinking", "hungry")
                                    user_identified = uname and uname in USER_GREETINGS
                                    if (user_identified or (not is_explicit_interaction and
                                            (not args.get("title") or args.get("title") in ("小美提示", "小美随时待命", "快捷功能")))):
                                        emo, title, value = resolve_standby_greeting(uname)
                                        app_state["emotion"] = emo
                                        if user_identified or not args.get("title"):
                                            app_state["title"] = title
                                        if user_identified or not args.get("value"):
                                            app_state["value"] = value
                                        logger.info(f"[MCP] standby emotion: user={uname or 'anonymous'} emotion={emo}")
                                # 兼容 AI 把 items 传成 JSON 字符串的情况（如 '\n[{"name":"..."}]\n'）
                                if isinstance(app_state["items"], str):
                                    try:
                                        parsed = json.loads(app_state["items"].strip())
                                        if isinstance(parsed, list):
                                            app_state["items"] = parsed
                                        else:
                                            app_state["items"] = []
                                    except (json.JSONDecodeError, ValueError):
                                        app_state["items"] = []

                                # 契约兜底：recipe_list 必须有 3-5 道菜（每项含 name/time/tag/steps）
                                # AI 偶尔会只传 card_type+title+value 而漏掉 items，导致前端菜谱卡片空白
                                # 此时用内置菜谱库填充，保证 UI 一定能显示菜品
                                if app_state["card_type"] == "recipe_list":
                                    items_list = app_state["items"] if isinstance(app_state["items"], list) else []
                                    valid_items = []
                                    for it in items_list:
                                        if isinstance(it, dict) and it.get("name") and it.get("steps"):
                                            # 确保每项有 time/tag（AI 偶尔漏字段）
                                            it.setdefault("time", "15分钟")
                                            it.setdefault("tag", "家常菜")
                                            valid_items.append(it)
                                    if len(valid_items) < 3:
                                        # AI 漏 items 或菜品不足，用内置菜谱库兜底填充至 4 道
                                        logger.warning(f"--> Recipe list fallback: AI items has {len(valid_items)} valid dishes, filling from built-in library")
                                        # 保留 AI 已传的有效菜品，再从内置库补充
                                        existing_names = {it.get("name") for it in valid_items}
                                        for dish in DEFAULT_RECIPE_LIBRARY:
                                            if len(valid_items) >= 4:
                                                break
                                            if dish["name"] not in existing_names:
                                                valid_items.append(dict(dish))
                                                existing_names.add(dish["name"])
                                        app_state["items"] = valid_items

                                # 如果是油烟机风速：解析档位并持久化（1-5）
                                # 契约兜底：优先取 AI 显式传的 fan_speed 参数；如果没传或非法，
                                # 再从 value 文本（如"已调至 4 档强排风"）里解析出数字档；
                                # 两处都解析不出时才保持当前值。
                                # 目的：防止 AI 话术写了档位却忘传 fan_speed 参数，导致显示与语音不一致。
                                if app_state["card_type"] == "fan":
                                    req_speed = args.get("fan_speed")
                                    param_speed = None
                                    if req_speed is not None and str(req_speed).strip() != "":
                                        try:
                                            param_speed = max(1, min(5, int(req_speed)))
                                        except (ValueError, TypeError):
                                            param_speed = None
                                    # 从 value 文本二次解析：匹配 "X档" "X 档" "X挡" "到X" "调至X" 等模式
                                    text_speed = None
                                    txt = app_state["value"] or ""
                                    m = re.search(r'(\d+)\s*[档位挡级]', txt)
                                    if not m:
                                        m = re.search(r'(?:调至|调到|开到|设为|调到|到|加大|关小)\s*(\d+)', txt)
                                    if m:
                                        try:
                                            text_speed = max(1, min(5, int(m.group(1))))
                                        except (ValueError, TypeError):
                                            text_speed = None
                                    # 决策：参数 > 文本 > 保持
                                    if param_speed is not None:
                                        app_state["fan_speed"] = param_speed
                                        # 如果文本解析与参数不一致，且文本看起来可信（参数默认值1但文本是4档→参数漏传），修正
                                        if text_speed is not None and param_speed == 1 and text_speed != 1:
                                            app_state["fan_speed"] = text_speed
                                            logger.warning(f"Fan param miss: fan_speed=1 but value text says {text_speed}; fixed to {text_speed}")
                                    elif text_speed is not None:
                                        app_state["fan_speed"] = text_speed
                                        logger.warning(f"Fan param missing: parsed fan_speed={text_speed} from value text")

                                # 如果是定时器：解析时长并持久化到 timers 数组（仅左灶/右灶）
                                if app_state["card_type"] == "timer":
                                    match = re.search(r'\d+', app_state["value"])
                                    mins = int(match.group()) if match else 15
                                    target_ts = time.time() + (mins * 60)
                                    app_state["target_timestamp"] = target_ts
                                    timer_mode = args.get("timer_mode", "shutoff")
                                    if timer_mode not in ("reminder", "shutoff"):
                                        timer_mode = "shutoff"
                                    # reminder 是独立的提醒任务；shutoff 才绑定左/右灶并执行关火逻辑。
                                    burner_name = upsert_timer(app_state["title"], target_ts, mins, timer_mode)
                                    if burner_name is None:
                                        # 无法识别灶具（如"灶台定时"），返回错误提示
                                        snapshot = app_state.copy()
                                        res_text = json.dumps({
                                            "success": False,
                                            "error": "不支持的灶具名。只能使用'左灶'或'右灶'，请明确指定灶具后重试。",
                                            "state": snapshot
                                        }, ensure_ascii=False)
                                        logger.warning(f"--> Timer rejected: unknown burner in title '{app_state['title']}'")
                                        # 跳过后续正常响应
                                        skip_normal_response = True

                                    elif timer_mode == "shutoff" and preserve_recipe_steps is None:
                                        # 灶具关火定时是默认页的附属状态，不能再覆盖成旧定时页面。
                                        app_state["card_type"] = "emotion"
                                        app_state["emotion"] = "happy"
                                        app_state["title"] = ""
                                        app_state["value"] = f"{burner_name}已设定时 {mins} 分钟"

                                if preserve_recipe_steps is not None:
                                    app_state.update(preserve_recipe_steps)

                                if not locals().get('skip_normal_response', False):
                                    snapshot = app_state.copy()
                                    logger.info(f"--> UI Generative Updated: {snapshot}")
                                    broadcast_sse()
                                    res_text = json.dumps({"success": True, "state": snapshot}, ensure_ascii=False)
                        elif tool_name == "show_magic_knob_setup":
                            with state_lock:
                                binding = args.get("function")
                                labels = {"hood": "烟机日常功能", "left_timer": "左灶控制", "right_timer": "右灶控制", "dishwasher": "洗碗机预约"}
                                if binding in labels:
                                    # 明确的语音绑定无需停留在 D01；确认后返回默认页面。
                                    app_state["magic_knob_binding"] = binding
                                    app_state["card_type"] = "emotion"
                                    app_state["title"] = ""
                                    app_state["value"] = "魔术旋钮已设为" + labels[binding]
                                else:
                                    app_state["card_type"] = "magic_knob"
                                    app_state["title"] = "设置魔术旋钮"
                                    app_state["value"] = "左右选择功能，按下确认"
                                snapshot = app_state.copy()
                            revision = broadcast_sse()
                            await wait_for_ui_ack(revision)
                            logger.info(f"--> Magic knob voice setup: {binding or 'open D01'}")
                            res_text = json.dumps({"success": True, "state": snapshot}, ensure_ascii=False)
                        elif tool_name == "restore_page_navigation":
                            with state_lock:
                                app_state["magic_knob_binding"] = None
                                snapshot = app_state.copy()
                            broadcast_sse()
                            logger.info("--> Magic knob restored to page navigation")
                            res_text = json.dumps({"success": True, "state": snapshot, "message": "旋钮已恢复页面操作"}, ensure_ascii=False)
                        elif tool_name == "set_light_control":
                            with state_lock:
                                requested = args.get("brightness")
                                if requested is not None:
                                    try:
                                        brightness = max(0, min(100, int(requested)))
                                    except (TypeError, ValueError):
                                        brightness = app_state.get("light_brightness", 100)
                                elif args.get("power") is False:
                                    brightness = 0
                                elif args.get("power") is True:
                                    brightness = 100
                                else:
                                    brightness = app_state.get("light_brightness", 100)
                                app_state["light_brightness"] = brightness
                                app_state["card_type"] = "light"
                                app_state["title"] = "照明"
                                app_state["value"] = "照明已关闭" if brightness == 0 else f"照明已调至 {brightness}%"
                                snapshot = app_state.copy()
                            revision = broadcast_sse()
                            await wait_for_ui_ack(revision)
                            logger.info(f"--> Voice light control: {brightness}%")
                            res_text = json.dumps({"success": True, "state": snapshot}, ensure_ascii=False)
                        elif tool_name == "query_fan_speed":
                            # 查询当前油烟机风速档位，便于 AI 相对调节（大一点/小一点）
                            with state_lock:
                                speed = app_state.get("fan_speed", 1)
                                snapshot = {"fan_speed": speed, "speed_mps": {1:2.5,2:4.0,3:5.5,4:7.0,5:8.5,6:10.0}.get(speed, 2.5), "max": 6}
                            logger.info(f"--> Query Fan Speed: {snapshot}")
                            res_text = json.dumps({"success": True, "result": snapshot}, ensure_ascii=False)
                        elif tool_name == "query_timers":
                            # 查询所有定时器状态（含剩余时间和状态），便于 AI 语音回答
                            with state_lock:
                                now = time.time()
                                timers_view = []
                                for t in app_state["timers"]:
                                    remaining = max(0, int(t["target_timestamp"] - now))
                                    status = "running" if remaining > 0 else "done"
                                    # 同步更新后端 status
                                    t["status"] = status
                                    timers_view.append({
                                        "name": t["name"],
                                        "remaining_seconds": remaining,
                                        "remaining_human": f"{remaining // 60}分{remaining % 60}秒" if remaining > 0 else "已完成",
                                    "duration_minutes": t["duration_minutes"],
                                    "mode": t.get("mode", "shutoff"),
                                        "status": status
                                    })
                                snapshot = {"timers": timers_view, "total": len(timers_view),
                                            "running_count": sum(1 for x in timers_view if x["status"] == "running"),
                                            "done_count": sum(1 for x in timers_view if x["status"] == "done")}

                            logger.info(f"--> Query Timers: {snapshot}")
                            res_text = json.dumps({"success": True, "result": snapshot}, ensure_ascii=False)
                        elif tool_name == "cancel_timer":
                            # 取消/删除指定灶具的定时器
                            burner_arg = args.get("burner", "all") or "all"
                            with state_lock:
                                if burner_arg == "all":
                                    count = len(app_state["timers"])
                                    app_state["timers"] = []
                                    snapshot = {"cancelled": "all", "count": count, "remaining_timers": []}
                                else:
                                    name = remove_timer(burner_arg)
                                    if name:
                                        snapshot = {"cancelled": name, "remaining_timers": app_state["timers"]}
                                    else:
                                        snapshot = {"cancelled": None, "remaining_timers": app_state["timers"], "error": f"未找到 {burner_arg} 的定时器"}

                            logger.info(f"--> Cancel Timer: {snapshot}")
                            broadcast_sse()
                            res_text = json.dumps({"success": True, "result": snapshot}, ensure_ascii=False)
                        elif tool_name == "start_cooking":
                            requested_name = str(args.get("dish_name", "")).strip()
                            with state_lock:
                                candidates = app_state.get("items", []) if isinstance(app_state.get("items"), list) else []
                                selected = None
                                if requested_name:
                                    selected = next((item for item in candidates if isinstance(item, dict) and item.get("name") == requested_name), None)
                                    if selected is None:
                                        selected = next((item for item in DEFAULT_RECIPE_LIBRARY if item["name"] == requested_name), None)
                                if selected is None:
                                    selected = next((item for item in candidates if isinstance(item, dict) and item.get("name") and item.get("steps")), None)
                                if selected is None:
                                    selected = DEFAULT_RECIPE_LIBRARY[0]
                                dish_name = selected.get("name", "番茄炒蛋")
                                steps = selected.get("steps") or DEFAULT_RECIPE_LIBRARY[0]["steps"]
                                app_state["card_type"] = "recipe_steps"
                                app_state["title"] = dish_name
                                app_state["value"] = f"{dish_name} · 共{len(steps)}步"
                                app_state["items"] = [{"step": step, "index": index + 1, "total": len(steps)} for index, step in enumerate(steps)]
                                app_state["step_index"] = 0
                                app_state["step_total"] = len(steps)
                                app_state["current_steps_snapshot"] = {"dish_name": dish_name, "steps": steps, "step_total": len(steps)}
                                snapshot = app_state.copy()
                            revision = broadcast_sse()
                            await wait_for_ui_ack(revision)
                            logger.info(f"--> Start cooking: {dish_name}, step 1/{len(steps)}")
                            res_text = json.dumps({"success": True, "state": snapshot, "first_step": steps[0]}, ensure_ascii=False)
                        elif tool_name == "show_recipe_steps":
                            # 展示某道菜的详细步骤
                            dish_name = args.get("dish_name", "未知菜品")
                            steps = args.get("steps", [])
                            if not isinstance(steps, list) or len(steps) == 0:
                                steps = ["暂无步骤信息"]
                            with state_lock:
                                app_state["card_type"] = "recipe_steps"
                                app_state["title"] = dish_name
                                app_state["value"] = f"{dish_name} · 共{len(steps)}步"
                                app_state["items"] = [{"step": s, "index": i+1, "total": len(steps)} for i, s in enumerate(steps)]
                                # 记录步骤导航状态
                                app_state["step_index"] = 0
                                app_state["step_total"] = len(steps)
                                # 保存当前步骤快照，防止后续 recipe_list 覆盖后 navigate_steps 无法恢复
                                app_state["current_steps_snapshot"] = {
                                    "dish_name": dish_name,
                                    "steps": steps,
                                    "step_total": len(steps)
                                }
                                snapshot = app_state.copy()
                            logger.info(f"--> Show Recipe Steps: {dish_name} ({len(steps)} steps)")
                            broadcast_sse()
                            res_text = json.dumps({"success": True, "state": snapshot}, ensure_ascii=False)
                        elif tool_name == "navigate_steps":
                            # 语音控制步骤切换
                            action = args.get("action", "next")
                            step_index_param = args.get("step_index", 1)
                            with state_lock:
                                # 如果当前 card_type 不是 recipe_steps（被 recipe_list 等覆盖），
                                # 从保存的步骤快照恢复 recipe_steps 卡片，确保前端显示步骤而非菜谱列表
                                snap = app_state.get("current_steps_snapshot")
                                if snap and app_state["card_type"] != "recipe_steps":
                                    steps = snap["steps"]
                                    dish_name = snap["dish_name"]
                                    app_state["card_type"] = "recipe_steps"
                                    app_state["title"] = dish_name
                                    app_state["value"] = f"{dish_name} · 共{len(steps)}步"
                                    app_state["items"] = [{"step": s, "index": i+1, "total": len(steps)} for i, s in enumerate(steps)]
                                    app_state["step_total"] = len(steps)
                                    app_state["step_index"] = 0  # 恢复时从第一步开始

                                total = app_state.get("step_total", 0)
                                cur = app_state.get("step_index", 0)
                                if action == "exit":
                                    # 仅在最后一步接受确认/退出，立刻返回默认待机页。
                                    if total and cur >= total - 1:
                                        app_state["card_type"] = "emotion"
                                        app_state["emotion"] = "happy"
                                        app_state["title"] = ""
                                        app_state["value"] = "做菜完成，祝您用餐愉快！"
                                        app_state["current_steps_snapshot"] = None
                                        snapshot = {"action": "exit", "exited": True, "step_total": total}
                                    else:
                                        snapshot = {"action": "exit", "exited": False, "error": "当前未到最后一步", "step_index": cur, "step_total": total}
                                elif action == "next":
                                    new_idx = min(total - 1, cur + 1)
                                elif action == "prev":
                                    new_idx = max(0, cur - 1)
                                elif action == "first":
                                    new_idx = 0
                                elif action == "last":
                                    new_idx = max(0, total - 1)
                                elif action == "jump":
                                    new_idx = max(0, min(total - 1, int(step_index_param) - 1))
                                else:
                                    new_idx = cur
                                if action != "exit":
                                    app_state["step_index"] = new_idx
                                    snapshot = {"action": action, "step_index": new_idx, "step_total": total, "current_step": app_state["items"][new_idx] if app_state["items"] and new_idx < len(app_state["items"]) else None}
                            logger.info(f"--> Navigate Steps: {action} → {'exit' if action == 'exit' else f'step {new_idx+1}/{total}'}")
                            broadcast_sse()
                            res_text = json.dumps({"success": True, "result": snapshot}, ensure_ascii=False)
                        else:
                            res_text = json.dumps({"error": "Unknown tool"})

                        # 等待浏览器完成对应状态的首帧渲染，再返回工具结果触发语音播报。
                        with state_lock:
                            target_revision = int(app_state.get("revision", 0))
                        await wait_for_ui_ack(target_revision)
                        res = {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": res_text}]}}
                        await websocket.send(json.dumps(res))
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            # 最快重连，减少断连期间工具调用丢失窗口
            await asyncio.sleep(0.3)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(mcp_worker())
