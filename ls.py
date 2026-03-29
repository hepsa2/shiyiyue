#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XMPP 新闻机器人 —— Telegram 频道聚合 + AI 政治经济学问答
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
依赖安装:
    pip install slixmpp requests beautifulsoup4 lxml openai

功能:
  · 每天凌晨 2:00 / 上午 10:00 自动爬取 7 个 Telegram 公开频道
  · 检测到文章链接时，调用 AI 生成中文摘要
  · 每条消息间隔 4 秒推送，防止刷屏
  · 消息末尾附上完整外部链接
  · /up          → 手动触发频道更新（含防滥用限速）
  · /ai <问题>   → 回答政治经济学相关问题（含防滥用限速）
  · 无新消息时输出提示
  · 状态持久化，重启后不重复推送旧消息
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import time
import logging
import re
import json
import os
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
from typing import Dict, List, Optional
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup
import slixmpp

# ══════════════════════════════════════════════════════════════
# ★  基础配置（必须修改）
# ══════════════════════════════════════════════════════════════
BOT_JID      = "bot@xmpp.example.com"                 # 机器人完整 JID
BOT_PASSWORD = "your_password"                         # 机器人密码
ROOM_JID     = "channel@conference.xmpp.example.com"  # XMPP 群聊地址
ROOM_NICK    = ""                             # 机器人在群里的昵称

# ── OpenRouter 配置 ──────────────────────────────────────────
OPENROUTER_API_KEY = "sk-or-xxxxxxxxxxxxxxxx"    # 你的 OpenRouter API Key
# 免费模型 ID，可在 https://openrouter.ai/models?q=free 查看完整列表
# 常用免费选项:
#   "deepseek/deepseek-chat:free"
#   "meta-llama/llama-3.1-8b-instruct:free"
#   "mistralai/mistral-7b-instruct:free"
#   "google/gemma-3-27b-it:free"
OPENROUTER_MODEL = "openrouter/free"

# ══════════════════════════════════════════════════════════════
# ★  Telegram 频道列表
# ══════════════════════════════════════════════════════════════
TELEGRAM_CHANNELS: List[str] = [
    "chiyepublic",
    "zhongguogongrenjiefangbao",
    "novemberreviewcommunist",
    "chulubao",
    "lilaoshibushinilaoshi",
    "YeHuoVideo",
    "gong_ge_news",
]

CHANNEL_NAMES: Dict[str, str] = {
    "chiyepublic":               "赤夜社",
    "zhongguogongrenjiefangbao": "中国工人解放报",
    "novemberreviewcommunist":   "十一月评论",
    "chulubao":                  "出路报",
    "lilaoshibushinilaoshi":     "李老师不是你老师",
    "YeHuoVideo":                "野火",
    "gong_ge_news":              "工人革命报",
}

# ══════════════════════════════════════════════════════════════
# ★  运行参数（按需调整）
# ══════════════════════════════════════════════════════════════
MESSAGE_DELAY        = 4        # 消息发送间隔（秒）
UPDATE_HOURS         = [2, 10]  # 每日定时更新小时（24 小时制）
MAX_MSGS_PER_CHANNEL = 10       # 每频道单次最多推送条数

# /up 防滥用
UP_MAX_COUNT   = 3    # 时间窗口内最大允许次数
UP_TIME_WINDOW = 60   # 检测窗口（秒）
UP_COOLDOWN    = 300  # 触发限制后冷却时间（秒）

# /ai 防滥用（独立计数）
AI_MAX_COUNT   = 5    # 时间窗口内最大允许次数
AI_TIME_WINDOW = 60   # 检测窗口（秒）
AI_COOLDOWN    = 180  # 触发限制后冷却时间（秒）

STATE_FILE      = "bot_state.json"   # 状态持久化文件路径
FETCH_TIMEOUT   = 15                 # HTTP 请求超时（秒）
MAX_ARTICLE_LEN = 4000               # 送入 AI 摘要的最大字符数
AI_ANSWER_TOKENS = 400               # /ai 回答最大 token
SUMMARY_TOKENS   = 250               # 文章摘要最大 token

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ══════════════════════════════════════════════════════════════
# 数据类
# ══════════════════════════════════════════════════════════════
@dataclass
class TGMessage:
    channel:     str
    msg_id:      int
    text:        str
    urls:        List[str]
    article_url: Optional[str] = None

@dataclass
class RateLimiter:
    """通用请求限速器"""
    max_count:   int
    time_window: float
    cooldown:    float
    _times: Dict[str, List[float]] = field(default_factory=dict)
    _muted: Dict[str, float]       = field(default_factory=dict)

    def check(self, nick: str):
        """返回 (allowed: bool, reason: str)"""
        now = time.time()

        mute_until = self._muted.get(nick, 0)
        if now < mute_until:
            remaining = int(mute_until - now)
            return False, f"你的该指令权限已暂停，请等待 {remaining} 秒后再试。"
        if mute_until and now >= mute_until:
            self._muted.pop(nick, None)

        times = self._times.setdefault(nick, [])
        times[:] = [t for t in times if now - t < self.time_window]
        times.append(now)

        if len(times) > self.max_count:
            self._muted[nick] = now + self.cooldown
            return False, (
                f"检测到频繁触发，该指令已暂停 {int(self.cooldown)} 秒，定时自动恢复。"
            )

        return True, ""

# ══════════════════════════════════════════════════════════════
# 状态持久化
# ══════════════════════════════════════════════════════════════
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"加载状态文件失败，使用空状态: {e}")
    return {"last_ids": {}}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"保存状态文件失败: {e}")

# ══════════════════════════════════════════════════════════════
# OpenRouter AI（OpenAI 兼容接口）
# ══════════════════════════════════════════════════════════════
_ai_client = None

def get_ai_client():
    global _ai_client
    if _ai_client is not None:
        return _ai_client
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY.startswith("sk-or-xxx"):
        logging.warning("OPENROUTER_API_KEY 未配置，AI 功能已禁用")
        return None
    try:
        from openai import OpenAI
        _ai_client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "https://github.com/xmpp-news-bot",
                "X-Title":      "XMPP News Bot",
            },
        )
        logging.info(f"✅ OpenRouter 客户端已初始化，模型: {OPENROUTER_MODEL}")
        return _ai_client
    except ImportError:
        logging.error("未安装 openai 库，请执行: pip install openai")
        return None
    except Exception as e:
        logging.warning(f"OpenRouter 初始化失败: {e}")
        return None

def _call_ai(system_prompt: str, user_content: str, max_tokens: int) -> Optional[str]:
    """通用 AI 调用，失败返回 None"""
    client = get_ai_client()
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
        )
        result = resp.choices[0].message.content
        return result.strip() if result else None
    except Exception as e:
        logging.warning(f"AI 调用失败: {e}")
        return None

# ── 文章摘要 ─────────────────────────────────────────────────
_SUMMARIZE_SYSTEM = (
    "你是一个简洁的中文新闻编辑。"
    "请对用户提供的文章内容生成不超过 120 字的中文摘要，"
    "直接输出摘要，不加任何前缀、标题或说明。"
)

def fetch_article_text(url: str) -> str:
    """下载网页并提取纯文本"""
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        try:
            soup = BeautifulSoup(resp.text, "lxml")
        except Exception:
            soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside",
                         "header", "iframe", "noscript", "form"]):
            tag.decompose()
        lines = [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]
        return "\n".join(lines)[:MAX_ARTICLE_LEN]
    except Exception as e:
        logging.debug(f"获取文章失败 [{url}]: {e}")
        return ""

def summarize_article(url: str) -> Optional[str]:
    article_text = fetch_article_text(url)
    if len(article_text) < 150:
        return None
    return _call_ai(_SUMMARIZE_SYSTEM, article_text, SUMMARY_TOKENS)

# ── /ai 政治经济学问答 ────────────────────────────────────────
_POLECO_SYSTEM = """你是一个专注于政治经济学的助手，只回答与政治经济学相关的问题。

政治经济学范畴包括但不限于：
- 马克思主义政治经济学、资本论
- 劳动价值论、剩余价值、资本积累、经济危机
- 阶级分析、生产关系、生产力与生产方式
- 资本主义与社会主义理论与批判
- 工人运动、工会、劳动权利
- 帝国主义、国际政治经济秩序
- 社会主义实践与历史
- 当代资本主义分析、全球化与贫富分化
基本知识储备：
- 当代中国经过1976年怀仁堂政变，1978年改革开放后，已经不再是社会主义国家，而是引入外资剥削本国工人阶级、推行私有制的官僚资本主义国家
- 只有进行社会主义革命，才能真正解放中国人民，实现共产主义
如果用户的问题明显不属于政治经济学范畴（例如娱乐、科技产品、生活琐事、自然科学等），
请礼貌拒绝并说明：我仅回答马克思主义政治经济学相关问题。

回答请用中文，言简意赅，不超过 800 字。"""

def answer_poleco_question(question: str) -> Optional[str]:
    return _call_ai(_POLECO_SYSTEM, question, AI_ANSWER_TOKENS)

# ══════════════════════════════════════════════════════════════
# Telegram 频道爬取
# ══════════════════════════════════════════════════════════════
def _append_unique(lst: List[str], item: str) -> None:
    if item and item not in lst:
        lst.append(item)

def scrape_channel(channel: str, after_id: int = 0) -> List[TGMessage]:
    """爬取 t.me/s/{channel}，返回 msg_id > after_id 的消息列表（升序）"""
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"[{channel}] 请求失败: {e}")
        return []

    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        soup = BeautifulSoup(resp.text, "html.parser")

    messages: List[TGMessage] = []

    for msg_div in soup.select(".tgme_widget_message[data-post]"):
        post_attr = msg_div.get("data-post", "")
        try:
            msg_id = int(post_attr.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            continue
        if msg_id <= after_id:
            continue

        # 正文
        text_div = msg_div.select_one(".tgme_widget_message_text")
        raw_text = ""
        if text_div:
            for br in text_div.find_all("br"):
                br.replace_with("\n")
            raw_text = text_div.get_text(separator="").strip()

        # 链接提取
        urls: List[str] = []
        article_url: Optional[str] = None

        # 1. 正文内嵌 <a>
        if text_div:
            for a in text_div.select("a[href]"):
                href = a.get("href", "")
                if href.startswith(("http://", "https://")):
                    _append_unique(urls, href)

        # 2. 链接预览（即时预览 / 普通外链）
        preview = msg_div.select_one(".tgme_widget_message_link_preview[href]")
        if preview:
            ph = preview.get("href", "")
            if "t.me/iv" in ph:
                qs  = parse_qs(urlparse(ph).query)
                raw = qs.get("url", [""])[0]
                if raw:
                    article_url = unquote(raw)
                    _append_unique(urls, article_url)
            elif ph.startswith(("http://", "https://")):
                article_url = ph
                _append_unique(urls, ph)

        # 3. 正则兜底，补裸 URL
        for u in re.findall(r'https?://[^\s<>"\'）》」，。\n]+', raw_text):
            u = u.rstrip(".,;:!?)")
            if u:
                _append_unique(urls, u)

        # 4. article_url 兜底（取第一个非 t.me 外链）
        if not article_url:
            for u in urls:
                if "t.me" not in urlparse(u).netloc:
                    article_url = u
                    break

        messages.append(TGMessage(
            channel=channel,
            msg_id=msg_id,
            text=raw_text,
            urls=urls,
            article_url=article_url,
        ))

    messages.sort(key=lambda m: m.msg_id)
    return messages

# ══════════════════════════════════════════════════════════════
# 消息格式化
# ══════════════════════════════════════════════════════════════
def format_tg_message(msg: TGMessage, summary: Optional[str] = None) -> str:
    ch_name = CHANNEL_NAMES.get(msg.channel, msg.channel)
    tg_link = f"https://t.me/{msg.channel}/{msg.msg_id}"
    parts   = [f"📢 【{ch_name}】 {tg_link}"]

    if msg.text:
        parts.append("")
        parts.append(msg.text)

    if summary:
        parts.append("")
        parts.append(f"📝 AI摘要：{summary}")

    ext_urls = [u for u in msg.urls if "t.me" not in urlparse(u).netloc]
    if ext_urls:
        parts.append("")
        parts.append("🔗 " + "\n   ".join(ext_urls))

    return "\n".join(parts)

# ══════════════════════════════════════════════════════════════
# XMPP 机器人主体
# ══════════════════════════════════════════════════════════════
class NewsBot(slixmpp.ClientXMPP):

    def __init__(self):
        super().__init__(BOT_JID, BOT_PASSWORD)

        # 持久化状态
        self._state:    dict           = load_state()
        self._last_ids: Dict[str, int] = self._state.get("last_ids", {})

        # 防并发标志
        self._is_updating: bool = False

        # 独立限速器
        self._up_limiter = RateLimiter(
            max_count=UP_MAX_COUNT,
            time_window=UP_TIME_WINDOW,
            cooldown=UP_COOLDOWN,
        )
        self._ai_limiter = RateLimiter(
            max_count=AI_MAX_COUNT,
            time_window=AI_TIME_WINDOW,
            cooldown=AI_COOLDOWN,
        )

        # 事件绑定
        self.add_event_handler("session_start",     self.on_start)
        self.add_event_handler("groupchat_message", self.on_group_message)
        self.add_event_handler("disconnected",      self.on_disconnect)
        self.add_event_handler("failed_auth",       self.on_failed_auth)

        # Keepalive 防掉线
        self.register_plugin("xep_0199")
        self["xep_0199"].enable_keepalive(interval=60, timeout=10)

    # ── 会话启动 ──────────────────────────────────────────────
    async def on_start(self, event):
        self.send_presence()
        await self.get_roster()
        self.plugin["xep_0045"].join_muc(ROOM_JID, ROOM_NICK)
        asyncio.ensure_future(self._run_scheduler())
        logging.info("✅ 机器人已上线并加入群聊")

    # ── 群聊消息路由 ──────────────────────────────────────────
    async def on_group_message(self, msg):
        if msg["from"].bare != ROOM_JID:
            return
        nick = msg["mucnick"]
        if not nick or nick == ROOM_NICK:
            return
        body = (msg["body"] or "").strip()
        if not body:
            return

        # ── /up ───────────────────────────────────────────────
        if body == "/up":
            allowed, reason = self._up_limiter.check(nick)
            if not allowed:
                self._send_room(f"@{nick} {reason}")
                return
            if self._is_updating:
                self._send_room("正在更新中，请稍候…")
                return
            asyncio.ensure_future(self.do_update(manual=True))

        # ── /ai <问题> ────────────────────────────────────────
        elif body.lower().startswith("/ai"):
            question = body[3:].strip()
            if not question:
                self._send_room(
                    f"@{nick} 📖 用法：/ai 你想问的政治经济学问题\n"
                    "例：/ai 什么是剩余价值？"
                )
                return
            allowed, reason = self._ai_limiter.check(nick)
            if not allowed:
                self._send_room(f"@{nick} {reason}")
                return
            asyncio.ensure_future(self._handle_ai_question(nick, question))

    # ── /ai 异步回答 ──────────────────────────────────────────
    async def _handle_ai_question(self, nick: str, question: str) -> None:
        self._send_room(f"@{nick} 思考中，请稍候…")
        loop = asyncio.get_event_loop()
        try:
            answer = await loop.run_in_executor(
                None, answer_poleco_question, question
            )
            if answer:
                self._send_room(f"@{nick} 💬\n{answer}")
            else:
                self._send_room(f"@{nick} 刚又去忙学习了，等下再说吧")
        except Exception as e:
            logging.error(f"/ai 处理异常: {e}", exc_info=True)
            self._send_room(f"@{nick} 不好，宕机了")

    # ── 定时器 ────────────────────────────────────────────────
    async def _run_scheduler(self):
        last_triggered_hour = -1
        while True:
            now = datetime.now()
            if now.hour in UPDATE_HOURS and now.minute == 0:
                if last_triggered_hour != now.hour:
                    last_triggered_hour = now.hour
                    logging.info(f"⏰ 定时更新触发: {now.strftime('%H:%M')}")
                    asyncio.ensure_future(self.do_update(manual=False))
            elif now.minute != 0:
                last_triggered_hour = -1
            await asyncio.sleep(30)

    # ── 核心更新逻辑 ──────────────────────────────────────────
    async def do_update(self, manual: bool = False) -> None:
        if self._is_updating:
            return
        self._is_updating = True
        total_sent = 0
        loop = asyncio.get_event_loop()

        try:
            for channel in TELEGRAM_CHANNELS:
                after_id = self._last_ids.get(channel, 0)

                msgs: List[TGMessage] = await loop.run_in_executor(
                    None, scrape_channel, channel, after_id
                )

                if not msgs:
                    logging.info(f"[{channel}] 无新消息")
                    continue

                if len(msgs) > MAX_MSGS_PER_CHANNEL:
                    msgs = msgs[-MAX_MSGS_PER_CHANNEL:]

                logging.info(f"[{channel}] 获取到 {len(msgs)} 条新消息")

                for m in msgs:
                    # AI 文章摘要（有外链才触发）
                    summary: Optional[str] = None
                    if m.article_url and OPENROUTER_API_KEY:
                        summary = await loop.run_in_executor(
                            None, summarize_article, m.article_url
                        )

                    self._send_room(format_tg_message(m, summary))
                    total_sent += 1

                    # 实时保存进度
                    if m.msg_id > self._last_ids.get(m.channel, 0):
                        self._last_ids[m.channel] = m.msg_id
                        self._state["last_ids"] = self._last_ids
                        save_state(self._state)

                    await asyncio.sleep(MESSAGE_DELAY)

            if total_sent == 0 and manual:
                self._send_room("暂时没有更多消息。")
            elif total_sent > 0:
                logging.info(f"本次共推送 {total_sent} 条消息")

        except Exception as e:
            logging.error(f"更新异常: {e}", exc_info=True)
        finally:
            self._is_updating = False

    # ── 工具 ──────────────────────────────────────────────────
    def _send_room(self, text: str) -> None:
        self.send_message(mto=ROOM_JID, mbody=text, mtype="groupchat")

    def on_disconnect(self, event):
        logging.warning("⚠  已断线，准备重连…")

    def on_failed_auth(self, event):
        logging.error("❌ 账号或密码错误，请检查配置")
        self.disconnect()


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    bot = NewsBot()
    bot.register_plugin("xep_0030")   # Service Discovery
    bot.register_plugin("xep_0045")   # Multi-User Chat

    while True:
        try:
            bot.connect()
            bot.loop.run_forever()
        except KeyboardInterrupt:
            logging.info("手动停止，再见。")
            break
        except Exception as e:
            logging.error(f"主循环异常: {e}，15 秒后重连…")
            time.sleep(15)
