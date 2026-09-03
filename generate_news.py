# -*- coding: utf-8 -*-
"""
每日新闻长图生成器
流程：抓取 RSS/网页新闻 -> 获取苏州天气 -> 在模版上擦除指定区域并重绘 -> 输出图片 + JSON
用法：
  python generate_news.py                 # 生成今日图片
  python generate_news.py --no-push      # 只生成不推送
  python generate_news.py --date 2026-09-03
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

FONT_REGULAR = BASE_DIR / "fonts" / "SourceHanSerifCN-Regular.otf"
FONT_BOLD = BASE_DIR / "fonts" / "SourceHanSerifCN-Bold.otf"
TEMPLATE = BASE_DIR / "template.png"
OUT_DIR = BASE_DIR / "output"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


# ---------------------------------------------------------------- 新闻抓取
def fetch_rss(url, timeout=25):
    """抓取单个 RSS 源，返回 [{title, link, published}]"""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        items = []
        for e in feed.entries[:30]:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            published = None
            for key in ("published_parsed", "updated_parsed"):
                if e.get(key):
                    published = dt.datetime(*e[key][:6])
                    break
            items.append({"title": title, "link": e.get("link", ""), "published": published})
        return items
    except Exception as exc:
        print(f"[warn] RSS 抓取失败 {url}: {exc}")
        return []


def fetch_html_titles(url, title_min, title_max, timeout=25):
    """通用网页列表抓取：提取所有 <a> 的文本标题"""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": UA}, verify=False)
        resp.raise_for_status()
        # 编码修复：优先 utf-8，失败再按探测结果
        try:
            text = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            text = resp.content.decode(resp.apparent_encoding or "gbk", "ignore")
        soup = BeautifulSoup(text, "html.parser")
        items = []
        seen = set()
        for a in soup.find_all("a"):
            text = re.sub(r"\s+", "", a.get_text() or "")
            href = a.get("href") or ""
            if not (title_min <= len(text) <= title_max):
                continue
            # 过滤导航类链接
            if any(k in text for k in ("首页", "更多", "登录", "搜索", "注册")):
                continue
            if text in seen:
                continue
            seen.add(text)
            items.append({"title": text, "link": href, "published": None})
            if len(items) >= 20:
                break
        return items
    except Exception as exc:
        print(f"[warn] 网页抓取失败 {url}: {exc}")
        return []


def collect_news(now):
    """按板块抓取并挑选新闻，返回有序列表 [{title, category}]"""
    cfg = CONFIG["news"]
    block_kw = cfg["block_keywords"]
    max_age = dt.timedelta(days=cfg["max_age_days"])
    selected = []
    for cat in cfg["categories"]:
        items = []
        for src in cat["sources"]:
            if src["type"] == "rss":
                items.extend(fetch_rss(src["url"]))
            else:
                items.extend(fetch_html_titles(src["url"], src.get("title_min", 10), src.get("title_max", 60)))
        # 过滤：娱乐关键词 + 板块必备关键词 + 标题去重 + 时效
        require_kw = cat.get("require_keywords") or []
        uniq, seen = [], set()
        for it in items:
            t = it["title"]
            if any(k in t for k in block_kw):
                continue
            if require_kw and not any(k in t for k in require_kw):
                continue
            key = re.sub(r"[\s\W]+", "", t)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        # 优先取时效内的，不足则放宽取最新 N 条
        fresh = [i for i in uniq if i["published"] and now - i["published"] <= max_age]
        if not fresh:
            fresh = [i for i in uniq if i["published"] is None] or uniq
        fresh = fresh[: cfg["fallback_top_n"]]
        for it in fresh[: cat["quota"]]:
            selected.append({"title": it["title"], "category": cat["name"]})
        print(f"[info] 板块[{cat['name']}] 抓到 {len(uniq)} 条，选用 {min(len(fresh), cat['quota'])} 条")
    # 截断标题
    max_len = cfg["max_title_len"]
    for s in selected:
        if len(s["title"]) > max_len:
            s["title"] = s["title"][: max_len - 1] + "…"
    return selected


# ---------------------------------------------------------------- 天气
WEATHER_TEXT = {
    0: "晴", 1: "多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾",
    51: "小雨", 53: "小雨", 55: "中雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "小雪",
    80: "阵雨", 81: "阵雨", 82: "暴雨", 85: "阵雪", 86: "阵雪",
    95: "雷雨", 96: "雷雨", 99: "雷雨",
}


def get_weather():
    w = CONFIG["weather"]
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={w['latitude']}&longitude={w['longitude']}"
        "&daily=temperature_2m_max,temperature_2m_min,weather_code"
        f"&timezone={requests.utils.quote(w['timezone'])}&forecast_days=1"
    )
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    d = resp.json()["daily"]
    code = int(d["weather_code"][0])
    tmax = round(d["temperature_2m_max"][0])
    tmin = round(d["temperature_2m_min"][0])
    text = WEATHER_TEXT.get(code, "多云")
    return {"code": code, "text": text, "tmin": tmin, "tmax": tmax,
            "display": f"{text} {tmin}~{tmax}℃"}


# ---------------------------------------------------------------- 绘制
def load_font(path, size):
    return ImageFont.truetype(str(path), size)


def erase_region(img, box):
    """擦除区域：采样区域外圈像素的主色填充，兼容纯色/近似纯色背景"""
    x1, y1, x2, y2 = box
    draw = ImageDraw.Draw(img)
    # 采样外圈（区域四周 2px 外扩）
    samples = []
    px = img.load()
    W, H = img.size
    for x in range(max(0, x1 - 3), min(W, x2 + 3), max(1, (x2 - x1) // 20 or 1)):
        for dy in (-3, 3):
            yy = y1 + dy if y1 + dy < y1 else y2 + dy
            if 0 <= yy < H:
                samples.append(px[x, yy])
    for y in range(max(0, y1 - 3), min(H, y2 + 3), max(1, (y2 - y1) // 20 or 1)):
        for dx in (-3, 3):
            xx = x1 + dx if x1 + dx < x1 else x2 + dx
            if 0 <= xx < W:
                samples.append(px[xx, y])
    if not samples:
        samples = [(255, 255, 255)]
    # 众数颜色
    freq = {}
    for c in samples:
        freq[c] = freq.get(c, 0) + 1
    fill = max(freq, key=freq.get)[:3]
    draw.rectangle(box, fill=fill)


def fit_single_line(text, font_path, box, max_size=44, min_size=14):
    """单行文字自适应字号：返回 (font, size)"""
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    for size in range(max_size, min_size - 1, -1):
        font = load_font(font_path, size)
        bbox = font.getbbox(text)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= bw and h <= bh:
            return font, size
    return load_font(font_path, min_size), min_size


def wrap_title(title, font, max_w):
    """按像素宽度换行"""
    lines, cur = [], ""
    for ch in title:
        if font.getlength(cur + ch) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def layout_news(news, box, max_size=40, min_size=18):
    """新闻排版：搜索最大可行字号，返回 (size, layout) layout=[{num, lines}]"""
    x1, y1, x2, y2 = box
    bw = x2 - x1
    for size in range(max_size, min_size - 1, -1):
        font_t = load_font(FONT_REGULAR, size)
        font_n = load_font(FONT_BOLD, size)
        line_h = int(size * 1.5)
        gap = int(size * 0.55)
        layout = []
        total = 0
        num_w = max(font_n.getlength(f"{i}. ") for i in range(1, len(news) + 1)) if news else 0
        for idx, item in enumerate(news, 1):
            lines = wrap_title(item["title"], font_t, bw - num_w)
            h = len(lines) * line_h
            layout.append({"num": idx, "lines": lines})
            total += h + (gap if idx > 1 else 0)
        if total <= (y2 - y1):
            return size, font_t, font_n, line_h, gap, num_w, layout
    return min_size, *(
        load_font(FONT_REGULAR, min_size), load_font(FONT_BOLD, min_size),
        int(min_size * 1.5), int(min_size * 0.55), 0, []
    )


def draw_weather_icon(img, box, code):
    """在 box 内绘制白色简约天气图标"""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    pad = int(min(w, h) * 0.12)
    icon = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(icon)
    W = w - pad * 2
    H = h - pad * 2
    cx, cy = w // 2, h // 2
    white = (255, 255, 255, 255)
    lw = max(3, W // 30)

    def cloud(cx_, cy_, s):
        r = int(s * 0.22)
        for dx, dy in ((-0.28, 0.08), (0, -0.12), (0.3, 0.08)):
            d.ellipse([cx_ + dx * s - r * 1.15, cy_ + dy * s - r, cx_ + dx * s + r * 1.15, cy_ + dy * s + r * 1.25], fill=white)
        d.rounded_rectangle([cx_ - 0.38 * s, cy_, cx_ + 0.38 * s, cy_ + 0.2 * s], radius=int(r * 0.8), fill=white)

    if code == 0:  # 晴
        r = int(W * 0.22)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=white, width=lw)
        import math
        for i in range(8):
            a = i * math.pi / 4
            x3, y3 = cx + (r + lw * 2.2) * math.cos(a), cy + (r + lw * 2.2) * math.sin(a)
            x4, y4 = cx + (r + lw * 4.6) * math.cos(a), cy + (r + lw * 4.6) * math.sin(a)
            d.line([x3, y3, x4, y4], fill=white, width=lw)
    elif code in (1, 2):  # 多云：小太阳+云
        import math
        r = int(W * 0.16)
        sx, sy = cx + W * 0.12, cy - H * 0.16
        d.ellipse([sx - r, sy - r, sx + r, sy + r], outline=white, width=lw)
        for i in range(8):
            a = i * math.pi / 4
            d.line([sx + (r + lw * 1.4) * math.cos(a), sy + (r + lw * 1.4) * math.sin(a),
                    sx + (r + lw * 2.8) * math.cos(a), sy + (r + lw * 2.8) * math.sin(a)], fill=white, width=max(2, lw - 1))
        cloud(cx - W * 0.08, cy + H * 0.16, W * 0.62)
    elif code == 3:  # 阴
        cloud(cx, cy, W * 0.78)
    elif code in (45, 48):  # 雾
        for i, yy in enumerate((-0.18, 0, 0.18)):
            d.line([cx - W * 0.34, cy + H * yy, cx + W * 0.34, cy + H * yy], fill=white, width=lw)
    else:  # 降水类：云 + 雨滴/雪/闪电
        cloud(cx, cy - H * 0.1, W * 0.72)
        if code in (71, 73, 75, 77, 85, 86):  # 雪
            sr = max(3, int(W * 0.035))
            for dx in (-0.2, 0, 0.2):
                d.ellipse([cx + W * dx - sr, cy + H * 0.22 - sr, cx + W * dx + sr, cy + H * 0.22 + sr], fill=white)
        elif code in (95, 96, 99):  # 雷雨
            pts = [(cx - W * 0.02, cy + H * 0.1), (cx + W * 0.1, cy + H * 0.1),
                   (cx + W * 0.01, cy + H * 0.24), (cx + W * 0.12, cy + H * 0.24),
                   (cx - W * 0.06, cy + H * 0.46), (cx - W * 0.0, cy + H * 0.28),
                   (cx - W * 0.12, cy + H * 0.28)]
            d.polygon(pts, fill=white)
        else:  # 雨
            for dx in (-0.2, 0, 0.2):
                d.line([cx + W * dx, cy + H * 0.16, cx + W * dx - W * 0.06, cy + H * 0.34],
                       fill=white, width=max(2, lw - 1))
    img.paste(icon, (x1 + pad, y1 + pad), icon)


def render(date_str, weekday_str, weather, news):
    img = Image.open(TEMPLATE).convert("RGB")
    if img.size != tuple(CONFIG["layout"]["image_size"]):
        print(f"[warn] 模版尺寸 {img.size} 与配置 {CONFIG['layout']['image_size']} 不一致，以模版为准")
    L = CONFIG["layout"]

    # 擦除四个区域
    for key in ("date_box", "weather_text_box", "weather_icon_box", "news_box"):
        erase_region(img, L[key])

    draw = ImageDraw.Draw(img)

    # 日期：白色加粗宋体，单行自适应
    date_text = f"{date_str}  {weekday_str}"
    font_d, _ = fit_single_line(date_text, FONT_BOLD, L["date_box"], max_size=44)
    bbox = font_d.getbbox(date_text)
    x1, y1, x2, y2 = L["date_box"]
    draw.text((x1, (y1 + y2 - bbox[1] - bbox[3]) / 2), date_text, font=font_d, fill=(255, 255, 255))

    # 天气文字：深蓝加粗宋体
    wtxt = weather["display"]
    font_w, _ = fit_single_line(wtxt, FONT_BOLD, L["weather_text_box"], max_size=30)
    bbox = font_w.getbbox(wtxt)
    x1, y1, x2, y2 = L["weather_text_box"]
    draw.text((x1, (y1 + y2 - bbox[1] - bbox[3]) / 2), wtxt, font=font_w, fill=(22, 51, 122))

    # 天气图标
    draw_weather_icon(img, L["weather_icon_box"], weather["code"])

    # 新闻列表：黑色宋体，全文连续编号，序号加粗
    x1, y1, x2, y2 = L["news_box"]
    size, font_t, font_n, line_h, gap, num_w, layout = layout_news(news, L["news_box"])
    print(f"[info] 新闻排版：{len(news)} 条，字号 {size}px")
    cy = y1
    for ent in layout:
        # 序号
        draw.text((x1, cy), f"{ent['num']}. ", font=font_n, fill=(0, 0, 0))
        for li, line in enumerate(ent["lines"]):
            draw.text((x1 + num_w, cy + li * line_h), line, font=font_t, fill=(0, 0, 0))
        cy += len(ent["lines"]) * line_h + gap
    return img, size


# ---------------------------------------------------------------- 推送
def push_pushplus(title, content, token):
    if not token:
        print("[warn] 未配置 PUSHPLUS_TOKEN，跳过推送")
        return
    resp = requests.post(
        "http://www.pushplus.plus/send",
        json={"token": token, "title": title, "content": content, "template": "html"},
        timeout=30,
    )
    print(f"[info] pushplus 推送结果: {resp.text[:200]}")


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now()
    date = dt.date.fromisoformat(args.date) if args.date else now.date()

    print(f"[info] 开始生成 {date} 每日新闻图")
    news = collect_news(now)
    if not news:
        print("[error] 未抓到任何新闻，中止")
        sys.exit(1)
    try:
        weather = get_weather()
        print(f"[info] 苏州天气: {weather['display']} (code={weather['code']})")
    except Exception as exc:
        print(f"[warn] 天气获取失败: {exc}，使用占位")
        weather = {"code": 3, "display": "—", "tmin": 0, "tmax": 0, "text": "—"}

    weekday = "周" + "一二三四五六日"[date.weekday()]
    date_str = f"{date.month}月{date.day}日"

    img, font_size = render(date_str, weekday, weather, news)

    OUT_DIR.mkdir(exist_ok=True)
    out_png = OUT_DIR / f"{date.isoformat()}.png"
    img.save(out_png, "PNG")
    print(f"[info] 图片已保存: {out_png}")

    # latest.json 供 H5 读取
    site_url = os.environ.get("SITE_URL", "")
    payload = {
        "date": date.isoformat(),
        "weekday": weekday,
        "weather": weather["display"],
        "weather_code": weather["code"],
        "news": news,
        "font_size": font_size,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "image": f"output/{date.isoformat()}.png",
    }
    (OUT_DIR / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[info] latest.json 已更新")

    if not args.no_push:
        token = os.environ.get("PUSHPLUS_TOKEN", "")
        img_url = f"{site_url}/output/{date.isoformat()}.png".replace("//output", "/output") if site_url else ""
        html = f'<h3>每日新闻 {date_str} {weekday}</h3><p>{weather["display"]}</p>'
        if img_url:
            html += f'<img src="{img_url}" style="max-width:100%"/><p><a href="{site_url}">查看H5版</a></p>'
        else:
            html += "<p>图片已生成，请到 H5 页面查看</p>"
        push_pushplus(f"每日新闻 {date_str} {weekday} {weather['display']}", html, token)


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    main()
