import re
import base64
import io
import time
import urllib.parse
import html
import random
from collections import Counter
from typing import Optional, Literal, Any


import httpx
import os
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import Response
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from requests_oauthlib import OAuth1Session


app = FastAPI(title="Pulse Yay - Live Buzz → Meme Seed")
analyzer = SentimentIntensityAnalyzer()


# Live data sources (no credentials required for Reddit read endpoints)
REDDIT_NEW = "https://www.reddit.com/r/{subreddit}/new.json"
REDDIT_SEARCH = "https://www.reddit.com/r/{subreddit}/search.json"


# Live enrichment sources (Wikipedia/Wikidata; no auth required)
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"


# NOTE: Bluesky's public CDN endpoint was returning 403 in this environment.
# Keeping this app focused on a reliable no-auth live source for the demo.


EVENT_PATTERNS = [
   (re.compile(r"\btouchdown\b", re.I), "TOUCHDOWN"),
   (re.compile(r"\bfumble\b", re.I), "FUMBLE"),
   (re.compile(r"\binterception\b", re.I), "INTERCEPTION"),
   (re.compile(r"\bhalftime\b", re.I), "HALFTIME"),
   (re.compile(r"\bcommercial\b|\bad\b", re.I), "COMMERCIAL"),
]




def detect_event(text: str) -> Optional[str]:
   """
   Lightweight keyword event detector.


   Purpose:
   - Converts raw social text into a coarse "moment" label (e.g., TOUCHDOWN/FUMBLE).
   - Used to drive meme framing ("TOUCHDOWN ALERT") and reduce LLM usage for latency.
   """
   for pat, name in EVENT_PATTERNS:
       if pat.search(text):
           return name
   return None




def compound_sentiment(text: str) -> float:
   """
   Fast sentiment score in range [-1, 1] using VADER.


   Purpose:
   - Gives a cheap "vibe" signal for meme tone (positive / negative / neutral).
   """
   # -1..1
   return float(analyzer.polarity_scores(text)["compound"])


_STOP_PHRASES = {
   "Super Bowl",
   "NFL",
   "SportsCenter",
   "Washington Times",
   "New England Patriots",
   "Seattle Seahawks",
   "AFC",
   "NFC",
}




def extract_name_candidates(text: str) -> list[str]:
   """
   Heuristic "celebrity-ish" extractor: capitalized word sequences (2-4 words).
   We then dedupe + filter out common sports/org phrases.
   """
   # Keep it cheap + fast for hackathon latency.
   pat = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b")
   cands = [m.group(1).strip() for m in pat.finditer(text)]
   out: list[str] = []
   for c in cands:
       if c.startswith(("Team ", "Report ", "Highlight ", "Game Thread")):
           continue
       if "Franchise Tag" in c or "Thread" in c:
           continue
       if c in _STOP_PHRASES:
           continue
       if any(tok in {"NFL", "SB", "Super", "Bowl"} for tok in c.split()):
           continue
       # Filter very generic pairs
       if c.lower() in {"new england", "seattle seahawks", "new england patriots"}:
           continue
       out.append(c)
   return out




# Simple in-memory cache for Wikipedia lookups to keep latency low and avoid rate limits.
_wiki_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_WIKI_TTL_S = 300




async def wiki_lookup_person(name: str) -> Optional[dict[str, Any]]:
   """
   Resolve a name using Wikipedia live APIs, then verify "human" via Wikidata entity data (P31=Q5).
   Returns: {name, title, description, extract, thumbnail, wikidata_qid, url}
   """
   now = time.time()
   cached = _wiki_cache.get(name)
   if cached and (now - cached[0]) < _WIKI_TTL_S:
       return cached[1] or None


   async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "pulse_yay/0.1 (Pulse NYC SB Hackathon)"}) as client:
       # 1) Wikipedia search
       r = await client.get(
           WIKI_API,
           params={
               "action": "query",
               "list": "search",
               "srsearch": name,
               "srlimit": 1,
               "format": "json",
           },
       )
       if r.status_code != 200:
           _wiki_cache[name] = (now, {})
           return None
       j = r.json()
       hits = (j.get("query") or {}).get("search") or []
       if not hits:
           _wiki_cache[name] = (now, {})
           return None
       title = hits[0].get("title")
       if not title:
           _wiki_cache[name] = (now, {})
           return None


       # Simple alignment: require at least one token overlap between requested name and resolved title.
       req_tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", name)}
       title_tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", title)}
       if len(req_tokens & title_tokens) == 0:
           _wiki_cache[name] = (now, {})
           return None


       # 2) Summary (includes thumbnail + wikibase_item sometimes)
       summary_url = WIKI_SUMMARY.format(title=urllib.parse.quote(title, safe=""))
       rs = await client.get(summary_url)
       if rs.status_code != 200:
           _wiki_cache[name] = (now, {})
           return None
       s = rs.json()


       qid = s.get("wikibase_item")
       # 3) Verify human using Wikidata entity data (no SPARQL)
       is_human = False
       if qid:
           rd = await client.get(WIKIDATA_ENTITY.format(qid=qid))
           if rd.status_code == 200:
               wd = rd.json()
               ent = (wd.get("entities") or {}).get(qid) or {}
               claims = ent.get("claims") or {}
               p31 = claims.get("P31") or []  # instance of
               for stmt in p31:
                   try:
                       val = stmt["mainsnak"]["datavalue"]["value"]["id"]
                       if val == "Q5":  # human
                           is_human = True
                           break
                   except Exception:
                       continue


       # Fallback heuristic if no qid (still useful for demo)
       if not is_human:
           desc = (s.get("description") or "").lower()
           if any(k in desc for k in ["actor", "actress", "singer", "rapper", "musician", "comedian", "american football", "quarterback", "athlete", "player"]):
               is_human = True


       if not is_human:
           _wiki_cache[name] = (now, {})
           return None


       result = {
           "name": name,
           "title": title,
           "description": s.get("description"),
           "extract": s.get("extract"),
           "thumbnail": (s.get("thumbnail") or {}).get("source"),
           "wikidata_qid": qid,
           "url": (s.get("content_urls") or {}).get("desktop", {}).get("page"),
       }
       _wiki_cache[name] = (now, result)
       return result




# Simple in-memory cache for celeb/player thumbnails (bytes) so repeated meme calls are fast.
_img_cache: dict[str, tuple[float, bytes]] = {}
_IMG_TTL_S = 600




async def fetch_image_bytes(url: str) -> Optional[bytes]:
   """
   Download image bytes from a URL (with a small in-memory TTL cache).


   Purpose:
   - Enables "meme with celeb/player photo background" using Wikipedia thumbnails.
   """
   now = time.time()
   cached = _img_cache.get(url)
   if cached and (now - cached[0]) < _IMG_TTL_S:
       return cached[1]


   try:
       async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "pulse_yay/0.1 (Pulse NYC SB Hackathon)"}) as client:
           r = await client.get(url)
           if r.status_code != 200:
               return None
           b = r.content
           _img_cache[url] = (now, b)
           return b
   except Exception:
       return None




def _cover_resize(img: Image.Image, width: int, height: int) -> Image.Image:
   """
   Resize an image to completely fill (cover) the target size, cropping excess.


   Purpose:
   - Makes arbitrary thumbnails fit a fixed meme canvas cleanly.
   """
   iw, ih = img.size
   if iw == 0 or ih == 0:
       return img.resize((width, height))
   scale = max(width / iw, height / ih)
   nw, nh = int(iw * scale), int(ih * scale)
   r = img.resize((nw, nh))
   left = max((nw - width) // 2, 0)
   top = max((nh - height) // 2, 0)
   return r.crop((left, top, left + width, top + height))




def _pick_font(size: int) -> ImageFont.ImageFont:
   """
   Best-effort font chooser for meme text (Impact/Arial/DejaVu/etc).


   Purpose:
   - Keeps the meme readable across macOS + Linux deployments.
   """
   # Best-effort: common font paths across macOS/Linux; fallback to default.
   candidates = [
       "/System/Library/Fonts/Supplemental/Impact.ttf",
       "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
       "/Library/Fonts/Impact.ttf",
       "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
       "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
   ]
   for p in candidates:
       try:
           return ImageFont.truetype(p, size=size)
       except Exception:
           pass
   return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
   """
   Simple word-wrap that fits text into `max_width` based on rendered pixel width.


   Purpose:
   - Prevents overflow for long business names / offers / punchlines.
   """
   words = text.split()
   lines: list[str] = []
   cur: list[str] = []
   for w in words:
       trial = (" ".join(cur + [w])).strip()
       if not trial:
           continue
       bbox = draw.textbbox((0, 0), trial, font=font)
       if (bbox[2] - bbox[0]) <= max_width:
           cur.append(w)
       else:
           if cur:
               lines.append(" ".join(cur))
               cur = [w]
           else:
               # single very long token; hard cut
               lines.append(w[:40])
               cur = []
   if cur:
       lines.append(" ".join(cur))
   return lines


_STOPWORDS = {
   "the","a","an","and","or","but","to","of","in","on","for","with","at","by","from","as","is","are","was","were",
   "be","been","being","it","its","this","that","these","those","you","your","we","our","they","their","i","me","my",
   "rt","vs","game","thread","highlight","report","per","new","today","team","teams","season","super","bowl","nfl",
   "http","https","www","com","amp",
}




def extract_keywords_after_sentiment(posts: list[dict[str, Any]], vibe: str, top_k: int = 6) -> list[str]:
   """
   Workflow requirement:
     1) sentiment/vibe computed first
     2) THEN extract keywords from the live posts most aligned with that vibe
   """
   # Select posts aligned with the vibe so keywords reflect "how the audience feels right now"
   aligned: list[str] = []
   for p in posts:
       s = float(p.get("sentiment", 0.0))
       if vibe.startswith("positive") and s < 0.10:
           continue
       if vibe.startswith("negative") and s > -0.10:
           continue
       aligned.append(p.get("title") or p.get("text") or "")


   corpus = aligned or [(p.get("title") or p.get("text") or "") for p in posts]
   counter: Counter[str] = Counter()
   for t in corpus:
       tokens = re.findall(r"[A-Za-z]{3,}", t.lower())
       for w in tokens:
           if w in _STOPWORDS:
               continue
           counter[w] += 1


   return [w for (w, _c) in counter.most_common(top_k)]




def vibe_word(vibe: str) -> str:
   if vibe.startswith("positive"):
       return "HYPE"
   if vibe.startswith("negative"):
       return "SALTY"
   return "NEUTRAL"




# Demo "focus" pools (hackathon-friendly, no paid APIs).
# NOTE: These are not authoritative 2026 rosters—just a small, editable set to steer meme outputs.
FOCUS_BAD_BUNNY = ["Bad Bunny"]
FOCUS_SEAHAWKS_PLAYERS = [
   "Geno Smith",
   "DK Metcalf",
   "Tyler Lockett",
   "Kenneth Walker",
   "Devon Witherspoon",
]
FOCUS_PATRIOTS_PLAYERS = [
   "Drake Maye",
   "Rhamondre Stevenson",
   "Christian Gonzalez",
   "Jabrill Peppers",
   "Kyle Dugger",
]




def focus_terms_bad_bunny_seahawks_patriots() -> list[str]:
   """
   Returns a short list of "focus terms" to bias the meme toward:
   - Bad Bunny
   - Seattle Seahawks players
   - New England Patriots players
   """
   terms: list[str] = []
   terms.append(random.choice(FOCUS_BAD_BUNNY))
   terms.append(random.choice(FOCUS_SEAHAWKS_PLAYERS))
   terms.append(random.choice(FOCUS_PATRIOTS_PLAYERS))
   # Add team anchors too (helps matching Reddit post text)
   terms += ["Seattle Seahawks", "New England Patriots"]
   # Dedupe while preserving order
   out: list[str] = []
   seen = set()
   for t in terms:
       k = t.lower().strip()
       if not k or k in seen:
           continue
       seen.add(k)
       out.append(t)
   return out




def _post_text(p: dict[str, Any]) -> str:
   return f"{p.get('title') or ''}\n{p.get('text') or ''}".lower()




def bias_keywords_with_focus(keywords: list[str], focus_terms: list[str], top_k: int = 6) -> list[str]:
   """
   Prepend focus terms (as short labels) so the meme text stays on-theme.
   """
   base = [k for k in (keywords or []) if k]
   focus_labels = []
   for t in focus_terms:
       # Keep as-is for names; for team anchors shorten
       tl = t.lower()
       if "seattle seahawks" in tl:
           focus_labels.append("Seahawks")
       elif "new england patriots" in tl:
           focus_labels.append("Patriots")
       else:
           focus_labels.append(t)


   merged: list[str] = []
   seen = set()
   for k in focus_labels + base:
       kk = k.lower().strip()
       if not kk or kk in seen:
           continue
       seen.add(kk)
       merged.append(k)
       if len(merged) >= top_k:
           break
   return merged




def build_classic_meme_copy(
   mood: str,
   keywords: list[str],
   q: str,
   business: str,
   offer: str,
   event: Optional[str] = None,
) -> tuple[str, str]:
   """
   Generate creative classic meme TOP/BOTTOM text.
   - Uses live mood + keywords (and optional detected event) to sound "of the moment"
   - Randomly picks from a small template set each request
   """
   kws = [k for k in (keywords or []) if k]
   k1 = (kws[0] if len(kws) > 0 else q).upper()
   k2 = (kws[1] if len(kws) > 1 else "VIBES").upper()
   k3 = (kws[2] if len(kws) > 2 else "CHAOS").upper()
   ev = (event or "").strip().upper()
   q_up = (q or "THE GAME").strip().upper()


   mood = (mood or "NEUTRAL").upper()


   common: list[tuple[str, str]] = [
       ("LIVE REACTION CHECK", f"{mood}: {k1} • {k2} • {k3}"),
       ("EVERYONE RN", f"{k1} JUST HIT • {mood} MODE ACTIVATED"),
       ("POV:", f"YOU HEAR '{k1}' AND SUDDENLY IT'S {mood}"),
       ("THE GROUP CHAT:", f"{k1} {k2} {k3} (VOLUME: MAX)"),
       ("THIS IS FINE", f"({mood}) {k1} {k2} {k3}"),
   ]


   hype: list[tuple[str, str]] = [
       ("WE ARE SO BACK", f"{ev or q_up} GOT ME LIKE {k1}"),
       ("ENERGY LEVEL:", f"{k1} • {k2} • {k3}"),
       ("I'M UP", f"AND IT'S BECAUSE OF {k1}"),
       ("SAY IT WITH ME", f"{k1} = {mood}"),
   ]


   salty: list[tuple[str, str]] = [
       ("WHO WROTE THIS SCRIPT", f"{k1} AGAIN?? I'M {mood}"),
       ("I CAN'T BELIEVE", f"{ev or q_up} DID THAT • {k1}"),
       ("ME TRYING TO BE CHILL", f"BUT {k1} HAS OTHER PLANS"),
       ("THE VIBES ARE OFF", f"{k1} • {k2} • {mood}"),
   ]


   neutral: list[tuple[str, str]] = [
       ("CURRENT STATUS:", f"{k1} • {k2} • {k3}"),
       ("OBSERVING", f"{q_up} LIKE: {k1}"),
       ("NO THOUGHTS", f"JUST {k1}"),
       ("REAL-TIME MOODBOARD", f"{k1} • {k2} • {k3}"),
   ]


   pool = list(common)
   if mood == "HYPE":
       pool += hype
   elif mood == "SALTY":
       pool += salty
   else:
       pool += neutral


   top, bottom = random.choice(pool)


   # Occasionally weave in the business output as a wink (CTA tag remains at bottom)
   if random.random() < 0.25:
       bottom = f"{bottom} • {offer.upper()}"
   if random.random() < 0.10:
       top = f"{top} @ {business}".upper()


   return top, bottom




def _draw_text_box(
   draw: ImageDraw.ImageDraw,
   x0: int,
   y0: int,
   x1: int,
   y1: int,
   text: str,
   font: ImageFont.ImageFont,
   text_fill=(255, 255, 255),
   box_fill=(0, 0, 0),
   alpha: float = 0.55,
):
   # Simple translucent box: approximate by drawing a solid box (good enough for demo).
   draw.rectangle([x0, y0, x1, y1], fill=box_fill)
   lines = _wrap_text(draw, text, font, max_width=(x1 - x0 - 24))
   y = y0 + 12
   for line in lines[:3]:
       # Stroke makes text readable on any photo/background.
       try:
           draw.text(
               (x0 + 12, y),
               line,
               font=font,
               fill=text_fill,
               stroke_width=max(2, int(getattr(font, "size", 18) / 18)),
               stroke_fill=(0, 0, 0),
           )
       except TypeError:
           # Older Pillow/font fallback: no stroke support.
           draw.text((x0 + 12, y), line, font=font, fill=text_fill)
       y += int(getattr(font, "size", 18) * 1.15)




def _fit_font_and_wrap(
   draw: ImageDraw.ImageDraw,
   text: str,
   max_width: int,
   max_height: int,
   start_size: int,
   min_size: int = 18,
) -> tuple[ImageFont.ImageFont, list[str]]:
   """
   Pick a font size that allows wrapped text to fit in a box.
   Returns (font, wrapped_lines).
   """
   text = (text or "").strip()
   if not text:
       return _pick_font(min_size), []


   for size in range(start_size, min_size - 1, -4):
       font = _pick_font(size)
       lines = _wrap_text(draw, text, font, max_width=max_width)
       if not lines:
           continue
       line_h = int(getattr(font, "size", size) * 1.10)
       needed_h = line_h * len(lines)
       if needed_h <= max_height:
           return font, lines


   font = _pick_font(min_size)
   return font, _wrap_text(draw, text, font, max_width=max_width)




def _draw_centered_lines(
   draw: ImageDraw.ImageDraw,
   lines: list[str],
   y0: int,
   width: int,
   font: ImageFont.ImageFont,
   fill=(255, 255, 255),
   stroke_fill=(0, 0, 0),
):
   """
   Draw wrapped lines centered horizontally with meme-like stroke.
   """
   if not lines:
       return
   stroke_w = max(2, int(getattr(font, "size", 18) / 14))
   line_h = int(getattr(font, "size", 18) * 1.10)
   y = y0
   for line in lines:
       bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_w)
       tw = bbox[2] - bbox[0]
       x = max((width - tw) // 2, 10)
       try:
           draw.text((x, y), line, font=font, fill=fill, stroke_width=stroke_w, stroke_fill=stroke_fill)
       except TypeError:
           draw.text((x, y), line, font=font, fill=fill)
       y += line_h




def render_classic_meme_png(
   images: list[Image.Image],
   top_text: str,
   bottom_text: str,
   business: str,
   offer: str,
   width: int = 1024,
   height: int = 1024,
   show_cta: bool = False,
) -> bytes:
   """
   Classic meme style (like your examples):
   - 1 photo OR 2-photo split background
   - Big centered TOP text + BOTTOM text (all caps, white w/ black outline)
   - (Optional) CTA tag for business output (disabled by default)
   """
   def _contain_on_blur(img: Image.Image, w: int, h: int) -> Image.Image:
       """
       Avoid "chopped" backgrounds:
       - Fill with a blurred cover version
       - Then paste a contained (fully visible) version on top
       """
       src = img.convert("RGB")
       bg = _cover_resize(src, w, h).filter(ImageFilter.GaussianBlur(18))
       # Slight darken to make text pop
       bg = Image.blend(bg, Image.new("RGB", (w, h), (0, 0, 0)), alpha=0.18)
       fg = src.copy()
       fg.thumbnail((w - 40, h - 40))
       x = (w - fg.size[0]) // 2
       y = (h - fg.size[1]) // 2
       bg.paste(fg, (x, y))
       return bg

   if not images:
       base = Image.new("RGB", (width, height), (30, 35, 60))
   elif len(images) == 1:
       base = _contain_on_blur(images[0], width, height)
   else:
       # 2-photo split background (related images should be supplied by caller)
       base = Image.new("RGB", (width, height), (30, 35, 60))
       w1 = width // 2
       w2 = width - w1
       left = _contain_on_blur(images[0], w1, height)
       right = _contain_on_blur(images[1], w2, height)
       base.paste(left, (0, 0))
       base.paste(right, (w1, 0))


   draw = ImageDraw.Draw(base)
   margin = 24
   max_w = width - 2 * margin


   top_text = (top_text or "").strip().upper()
   bottom_text = (bottom_text or "").strip().upper()


   # No bottom CTA banner by default (user requested to remove the yellow section).
   cta_h = 84 if show_cta else 0
   top_box_h = int(height * 0.28)
   bottom_box_h = int(height * 0.28)


   font_top, top_lines = _fit_font_and_wrap(draw, top_text, max_width=max_w, max_height=top_box_h, start_size=96)
   _draw_centered_lines(draw, top_lines, y0=margin, width=width, font=font_top)


   font_bot, bot_lines = _fit_font_and_wrap(draw, bottom_text, max_width=max_w, max_height=bottom_box_h, start_size=92)
   line_h = int(getattr(font_bot, "size", 36) * 1.10)
   bot_total_h = line_h * len(bot_lines)
   bot_y0 = max(height - cta_h - margin - bot_total_h, margin + top_box_h)
   _draw_centered_lines(draw, bot_lines, y0=bot_y0, width=width, font=font_bot)


   # CTA tag (business output) — optional
   if show_cta:
       font_cta = _pick_font(42)
       tag = f"{offer} @ {business}".strip()
       _draw_text_box(
           draw,
           0,
           height - cta_h,
           width,
           height,
           tag,
           font_cta,
           text_fill=(15, 20, 30),
           box_fill=(255, 213, 79),
       )


   buf = io.BytesIO()
   base.save(buf, format="PNG", optimize=True)
   return buf.getvalue()




def render_grid_meme_png(
   images: list[Image.Image],
   mood: str,
   keywords: list[str],
   business: str,
   offer: str,
   tiles: int,
   width: int = 1024,
   height: int = 1024,
) -> bytes:
   """
   Render a 1/2/4-photo grid meme with overlay words.
   """
   img = Image.new("RGB", (width, height), (10, 15, 30))
   draw = ImageDraw.Draw(img)


   # Layout
   if tiles == 1:
       grid = [(0, 0, width, height)]
   elif tiles == 2:
       grid = [(0, 0, width // 2, height), (width // 2, 0, width, height)]
   else:
       half = width // 2
       grid = [
           (0, 0, half, half),
           (half, 0, width, half),
           (0, half, half, height),
           (half, half, width, height),
       ]


   # Paste images (cover-fill)
   for i, box in enumerate(grid):
       x0, y0, x1, y1 = box
       if i < len(images):
           tile = _cover_resize(images[i].convert("RGB"), x1 - x0, y1 - y0)
       else:
           tile = Image.new("RGB", (x1 - x0, y1 - y0), (30, 35, 60))
       img.paste(tile, (x0, y0))


   # If we couldn't load any live images, make it explicit on the canvas (so it never looks "blank").
   if len(images) == 0:
       font_big = _pick_font(80)
       _draw_text_box(draw, 0, 200, width, 360, "NO LIVE IMAGES FOUND", font_big)
       font_hint = _pick_font(40)
       _draw_text_box(draw, 0, 370, width, 450, "Try: subreddit=pics or a different q=...", font_hint)


   # Top overlay: mood + keywords
   font_top = _pick_font(72)
   top_text = f"{mood} • " + " • ".join(k.upper() for k in keywords[:4])
   _draw_text_box(draw, 0, 0, width, 120, top_text, font_top)


   # Bottom CTA overlay
   font_cta = _pick_font(60)
   cta = f"{offer} — {business}"
   _draw_text_box(draw, 0, height - 140, width, height, cta, font_cta, text_fill=(15, 20, 30), box_fill=(255, 213, 79))


   # Per-tile keyword overlay
   font_tile = _pick_font(54)
   for i, box in enumerate(grid):
       if i >= len(keywords):
           break
       x0, y0, x1, y1 = box
       # Put labels near the bottom of each tile (not hidden under the top bar).
       pad = 10
       label_h = 100
       ly1 = min(y1 - pad, height - 150) if tiles in (2, 4) else (y1 - pad)
       ly0 = max(y0 + pad, ly1 - label_h)
       _draw_text_box(draw, x0 + pad, ly0, x1 - pad, ly1, keywords[i].upper(), font_tile)


   buf = io.BytesIO()
   img.save(buf, format="PNG", optimize=True)
   return buf.getvalue()




def extract_reddit_image_urls(d: dict[str, Any]) -> list[str]:
   """
   Best-effort extraction of image URLs from a Reddit post payload.
   Supports:
   - galleries (media_metadata)
   - crossposts
   - preview images
   - direct image links (i.redd.it / preview.redd.it)
   """
   urls: list[str] = []

   def _add(u: Optional[str]):
       if not u or not isinstance(u, str):
           return
       u = html.unescape(u)
       if u not in urls:
           urls.append(u)

   # 0) Galleries (collect multiple)
   try:
       if d.get("is_gallery") and isinstance(d.get("media_metadata"), dict):
           mm: dict[str, Any] = d["media_metadata"]
           for _k, v in mm.items():
               u = (((v or {}).get("s") or {}).get("u")) or None
               _add(u)
   except Exception:
       pass

   # 1) Crossposts can contain preview/media even if parent does not
   try:
       xposts = d.get("crosspost_parent_list") or []
       if isinstance(xposts, list) and xposts:
           for u in extract_reddit_image_urls(xposts[0]):
               _add(u)
   except Exception:
       pass

   # 2) Preview images (may contain multiple resolutions; take a few sources)
   try:
       prev = (d.get("preview") or {}).get("images") or []
       for img in prev[:4]:
           u = (((img or {}).get("source") or {}).get("url")) or None
           _add(u)
   except Exception:
       pass

   # 3) Direct URL
   u2 = d.get("url_overridden_by_dest") or d.get("url")
   if isinstance(u2, str):
       u2 = html.unescape(u2)
       ul = u2.lower()
       if any(ul.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]) or ("i.redd.it/" in ul) or ("preview.redd.it/" in ul):
           _add(u2)

   # Keep only likely image URLs
   cleaned: list[str] = []
   for u in urls:
       ul = u.lower()
       if any(ul.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]) or ("i.redd.it/" in ul) or ("preview.redd.it/" in ul):
           cleaned.append(u)
   return cleaned


def extract_reddit_image_url(d: dict[str, Any]) -> Optional[str]:
   """
   Back-compat helper: return the first image URL (if any).
   """
   urls = extract_reddit_image_urls(d)
   return urls[0] if urls else None


def render_meme_png(
   headline: str,
   punchline: str,
   cta: str,
   footer: str,
   width: int = 1024,
   height: int = 1024,
   background: Optional[Image.Image] = None,
) -> bytes:
   """
   Renders a meme image to PNG bytes.
   If `background` is provided, it is used as a cover-fill background with a dark overlay for legibility.
   """
   if background is None:
       img = Image.new("RGB", (width, height), (8, 18, 36))
       draw = ImageDraw.Draw(img)
       # Gradient-ish background
       for y in range(height):
           t = y / max(height - 1, 1)
           r = int(10 + 30 * t)
           g = int(20 + 40 * t)
           b = int(50 + 80 * t)
           draw.line([(0, y), (width, y)], fill=(r, g, b))
   else:
       img = _cover_resize(background.convert("RGB"), width, height)
       # Dark overlay for text legibility
       overlay = Image.new("RGB", (width, height), (0, 0, 0))
       img = Image.blend(img, overlay, alpha=0.35)
       draw = ImageDraw.Draw(img)


   pad = 64
   max_w = width - pad * 2


   font_h = _pick_font(84)
   font_p = _pick_font(44)
   font_cta = _pick_font(64)
   font_f = _pick_font(28)


   def shadow_text(x: int, y: int, text: str, font: ImageFont.ImageFont, fill=(255, 255, 255)):
       for dx, dy in [(3, 3), (2, 2)]:
           draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
       draw.text((x, y), text, font=font, fill=fill)


   y = pad
   for line in _wrap_text(draw, headline.upper(), font_h, max_w):
       shadow_text(pad, y, line, font_h)
       y += int(font_h.size * 1.05)


   y += 16
   for line in _wrap_text(draw, punchline, font_p, max_w):
       shadow_text(pad, y, line, font_p, fill=(230, 240, 255))
       y += int(font_p.size * 1.25)


   banner_h = 160
   banner_y = height - pad - banner_h
   draw.rounded_rectangle([pad, banner_y, width - pad, banner_y + banner_h], radius=28, fill=(255, 213, 79))
   cta_lines = _wrap_text(draw, cta.upper(), font_cta, max_w - 40)
   cta_y = banner_y + 24
   for line in cta_lines[:2]:
       draw.text((pad + 24, cta_y), line, font=font_cta, fill=(15, 20, 30))
       cta_y += int(font_cta.size * 1.05)


   draw.text((pad, height - pad + 12), footer, font=font_f, fill=(200, 210, 230))


   buf = io.BytesIO()
   img.save(buf, format="PNG", optimize=True)
   return buf.getvalue()




def build_food_bev_copy(vibe: str, event: Optional[str], business: str, offer: str, celeb_title: Optional[str]) -> tuple[str, str, str]:
   """
   Build meme headline + punchline + CTA focused on food/beverage conversion.


   Purpose:
   - Deterministic copy (no LLM), good for <45s latency demo.
   - Injects celeb/player name to connect meme to the trending figure.
   """
   # Keep deterministic + punchy (demo-friendly).
   ev = event or "SUPER BOWL"
   if vibe.startswith("positive"):
       punch = "HYPE MODE ON."
   elif vibe.startswith("negative"):
       punch = "SALTY CHAT. NEED A RESET."
   else:
       punch = "LIVE REACTIONS INCOMING."


   celeb = f"Feat: {celeb_title}." if celeb_title else ""
   headline = f"{ev} REACTION"
   punchline = f"{punch} {celeb} {business}: fuel up now."
   cta = f"{offer}"
   return headline, punchline.strip(), cta




@app.get("/trend")
async def trend(
   subreddit: str = Query(default="nfl"),
   q: str = Query(default="super bowl"),
):
   """
   One call for the live demo dashboard:
     Reddit live -> sentiment -> top celeb/player (Wikipedia) -> meme-ready seed
   """
   b = await buzz(source="reddit", subreddit=subreddit, q=q, limit=25)
   c = await celebs(subreddit=subreddit, q=q, limit=25, top_n=1)
   top = c["celebs"][0] if c["celebs"] else None
   return {"buzz": b, "topCeleb": top}




def _x_env():
   """
   Read X/Twitter credentials from environment variables.
   """
   return {
       "api_key": os.getenv("X_API_KEY"),
       "api_secret": os.getenv("X_API_SECRET"),
       "access_token": os.getenv("X_ACCESS_TOKEN"),
       "access_token_secret": os.getenv("X_ACCESS_TOKEN_SECRET"),
   }




def _x_ready(env: dict) -> bool:
   """
   True if all required X credentials are present.
   """
   return all(env.values())




def x_post_image_and_text(image_png: bytes, text: str) -> dict[str, Any]:
   """
   Posts an image + text to X/Twitter using OAuth1.0a user context.
   Requires env vars: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
   """
   env = _x_env()
   if not _x_ready(env):
       return {
           "ok": False,
           "error": "missing_x_credentials",
           "required_env": ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"],
       }


   oauth = OAuth1Session(
       env["api_key"],
       client_secret=env["api_secret"],
       resource_owner_key=env["access_token"],
       resource_owner_secret=env["access_token_secret"],
   )


   # 1) Upload media (v1.1 endpoint)
   up = oauth.post("https://upload.twitter.com/1.1/media/upload.json", files={"media": image_png})
   if up.status_code >= 300:
       return {"ok": False, "error": "media_upload_failed", "status": up.status_code, "body": up.text[:500]}
   media_id = up.json().get("media_id_string")
   if not media_id:
       return {"ok": False, "error": "media_id_missing", "body": up.text[:500]}


   # 2) Create post (v2)
   payload = {"text": text, "media": {"media_ids": [media_id]}}
   tw = oauth.post("https://api.twitter.com/2/tweets", json=payload)
   if tw.status_code >= 300:
       return {"ok": False, "error": "tweet_create_failed", "status": tw.status_code, "body": tw.text[:500]}
   return {"ok": True, "response": tw.json()}


def _reddit_headers() -> dict:
   """
   Reddit requires a User-Agent; otherwise requests can be blocked/rate-limited.
   """
   # Reddit blocks/ratelimits aggressively without a User-Agent.
   return {"User-Agent": "pulse_yay/0.1 (Pulse NYC SB Hackathon)"}


async def fetch_from_reddit(subreddit: str, q: str, limit: int):
   """
   Pull live posts from Reddit (either newest feed or subreddit search).


   Returns:
   - list of {id, title, text, createdAt, url}
   """
   # Reddit can be slow; don't let timeouts crash the whole API.
   timeout = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
   try:
       async with httpx.AsyncClient(timeout=timeout, headers=_reddit_headers(), follow_redirects=True) as client:
           # Use search whenever a query is provided (better chance of getting relevant + image posts).
           if q.strip():
               r = await client.get(
                   REDDIT_SEARCH.format(subreddit=subreddit),
                   params={"q": q, "sort": "new", "restrict_sr": 1, "limit": limit},
               )
           else:
               r = await client.get(REDDIT_NEW.format(subreddit=subreddit), params={"limit": limit})
   except httpx.TimeoutException:
       raise HTTPException(status_code=502, detail={"upstream": "reddit", "error": "timeout"})
   except httpx.RequestError as e:
       raise HTTPException(status_code=502, detail={"upstream": "reddit", "error": "request_error", "message": str(e)[:200]})


   if r.status_code != 200:
       raise HTTPException(status_code=502, detail={"upstream": "reddit", "status": r.status_code, "body": r.text[:500]})


   data = r.json()


   posts = []
   for child in (data.get("data") or {}).get("children") or []:
       d = child.get("data") or {}
       title = d.get("title") or ""
       selftext = d.get("selftext") or ""
       text = (title + "\n" + selftext).strip()
       if len(text) < 6:
           continue
       posts.append(
           {
               "id": d.get("name") or d.get("id"),
               "title": title,
               "text": text[:2000],
               "createdAt": d.get("created_utc"),
               "url": ("https://www.reddit.com" + d.get("permalink")) if d.get("permalink") else d.get("url"),
               "imageUrl": extract_reddit_image_url(d),
               "imageUrls": extract_reddit_image_urls(d),
           }
       )
   return posts




@app.get("/health")
async def health():
   """
   Basic health endpoint for deployment checks.
   """
   return {"ok": True}




@app.get("/buzz")
async def buzz(
   source: Literal["reddit"] = Query(default="reddit", description="Live data source (no-cred)"),
   subreddit: str = Query(default="nfl", description="Subreddit to pull from when using reddit"),
   q: str = Query(default="super bowl", description="Search query for live social posts"),
   limit: int = Query(default=25, ge=5, le=50),
):
   """
   Live Reddit -> sentiment -> event/vibe -> memeSeed.


   This is the core "Real Data → AI-ish Processing" step for judging.
   """
   if source != "reddit":
       raise HTTPException(status_code=400, detail="Unsupported source")


   raw_posts = await fetch_from_reddit(subreddit=subreddit, q=q, limit=limit)
   posts = [
       {
           **p,
           "sentiment": compound_sentiment(p["text"]),
           "event": detect_event(p["text"]),
       }
       for p in raw_posts
   ]


   avg = sum(x["sentiment"] for x in posts) / max(len(posts), 1)
   top_event = next((x["event"] for x in posts if x["event"]), None)


   vibe = "neutral"
   if avg > 0.2:
       vibe = "positive/hype"
   elif avg < -0.2:
       vibe = "negative/salty"


   return {
       "query": q,
       "count": len(posts),
       "avgCompoundSentiment": avg,
       "topEvent": top_event,
       "posts": posts[:20],
       "memeSeed": {
           "vibe": vibe,
           "event": top_event,
           "topLines": [x["text"] for x in posts[:5]],
       },
   }




@app.get("/celebs")
async def celebs(
   subreddit: str = Query(default="nfl"),
   q: str = Query(default="super bowl"),
   limit: int = Query(default=25, ge=5, le=50),
   top_n: int = Query(default=5, ge=1, le=10),
):
   """
   Live flow:
     Reddit (live) -> extract name candidates -> Wikipedia (live) -> return likely celebrities.
   """
   raw_posts = await fetch_from_reddit(subreddit=subreddit, q=q, limit=limit)


   counter: Counter[str] = Counter()
   evidence: dict[str, list[dict[str, Any]]] = {}
   for p in raw_posts:
       for name in extract_name_candidates(p.get("title") or p["text"]):
           counter[name] += 1
           evidence.setdefault(name, [])
           if len(evidence[name]) < 3:
               evidence[name].append({"id": p["id"], "title": p.get("title"), "url": p.get("url")})


   # Resolve top candidates via Wikipedia/Wikidata
   results: list[dict[str, Any]] = []
   for name, mentions in counter.most_common(30):
       info = await wiki_lookup_person(name)
       if not info:
           continue
       results.append({**info, "mentions": mentions, "evidence": evidence.get(name, [])})
       if len(results) >= top_n:
           break


   return {"query": q, "subreddit": subreddit, "count": len(results), "celebs": results}




@app.get("/meme_suggestion")
async def meme_suggestion(
   business: str = Query(default="local pizza shop", description="Business type"),
   offer: str = Query(default="15% OFF", description="Promo offer text"),
   q: str = Query(default="super bowl", description="Buzz query"),
):
   """
   Returns text outputs only (caption + image prompt).


   Purpose:
   - Useful if you want to plug into an external image generator later.
   """
   b = await buzz(q=q, limit=25)
   seed = b["memeSeed"]


   caption = f"{seed['event'] or 'Super Bowl'} vibes: {seed['vibe']}. {offer} at {business} tonight."
   image_prompt = (
       f"Create a bold, funny Super Bowl reaction meme for a {business}. "
       f"Tone: {seed['vibe']}. Event: {seed['event'] or 'game moment'}. "
       f"Include big readable text: '{offer} TONIGHT'."
   )


   return {
       "buzz": {k: b[k] for k in ["avgCompoundSentiment", "topEvent", "count", "query"]},
       "caption": caption,
       "imagePrompt": image_prompt,
   }




@app.get("/meme")
async def meme(
   business: str = Query(default="local pizza shop", description="Business type"),
   offer: str = Query(default="15% OFF", description="Promo offer text"),
   q: str = Query(default="super bowl", description="Buzz query"),
   tiles: int = Query(default=4, ge=1, le=4, description="How many photos in grid (1,2,4)"),
   subreddit: str = Query(default="nfl", description="Reddit subreddit"),
   style: Literal["grid", "classic"] = Query(default="grid", description="Meme style: grid (1/2/4) or classic (top/bottom)"),
   focus: bool = Query(default=False, description="Focus on Bad Bunny + Seahawks + Patriots (bias keywords + image selection)"),
   bg_random_1_or_2: bool = Query(default=True, description="(classic) Randomly use 1 or 2 background photos"),
):
   """
   NEW workflow (grid meme):
     1) Pull live Reddit posts (with images)
     2) Compute sentiment/vibe
     3) Extract top keywords AFTER sentiment selection
     4) Render a 1/2/4-photo grid meme with mood + keywords + food/bev promo
   """
   raw_posts = await fetch_from_reddit(subreddit=subreddit, q=q, limit=25)
   posts = [
       {**p, "sentiment": compound_sentiment(p["text"]), "event": detect_event(p["text"])}
       for p in raw_posts
   ]
   avg = sum(p["sentiment"] for p in posts) / max(len(posts), 1)
   vibe = "neutral"
   if avg > 0.2:
       vibe = "positive/hype"
   elif avg < -0.2:
       vibe = "negative/salty"


   keywords = extract_keywords_after_sentiment(posts, vibe=vibe, top_k=6)
   mood = vibe_word(vibe)


   focus_terms = focus_terms_bad_bunny_seahawks_patriots() if focus else []
   if focus:
       keywords = bias_keywords_with_focus(keywords, focus_terms, top_k=6)


   # Choose posts with actual images
   img_posts = [p for p in posts if p.get("imageUrl")]
   if tiles not in (1, 2, 4):
       tiles = 4
   want = tiles if tiles != 3 else 4
   image_urls_selected: list[str] = []

   if style == "classic":
       # IMPORTANT: Only use 2 photos when they're related (same Reddit post/gallery).
       candidates = [p for p in posts if (p.get("imageUrls") or p.get("imageUrl"))]
       if focus and focus_terms:
           focus_hits = [p for p in candidates if any(t.lower() in _post_text(p) for t in focus_terms)]
           if focus_hits:
               candidates = focus_hits

       src = random.choice(candidates) if candidates else None
       urls = (src.get("imageUrls") if src else None) or []
       if (not urls) and src and src.get("imageUrl"):
           urls = [src["imageUrl"]]

       want_images = 1
       if bg_random_1_or_2 and len(urls) >= 2 and random.random() < 0.5:
           want_images = 2
       image_urls_selected = random.sample(urls, k=min(want_images, len(urls))) if urls else []
   else:
       # Grid style can mix posts; pick N image posts randomly
       want_images = want
       if focus and focus_terms:
           focus_hits = [p for p in img_posts if any(t.lower() in _post_text(p) for t in focus_terms)]
           if len(focus_hits) >= 1:
               img_posts = focus_hits

       if len(img_posts) > want_images:
           img_posts = random.sample(img_posts, k=want_images)
       else:
           img_posts = img_posts[:want_images]
       image_urls_selected = [p["imageUrl"] for p in img_posts if p.get("imageUrl")]


   images: list[Image.Image] = []
   for u in image_urls_selected:
       bimg = await fetch_image_bytes(u)
       if not bimg:
           continue
       try:
           images.append(Image.open(io.BytesIO(bimg)))
       except Exception:
           continue


   # If no images are available, fall back to a 1-tile placeholder (still shows keywords/mood)
   tiles_used = tiles
   if not images:
       tiles_used = 1


   classic_top = None
   classic_bottom = None
   if style == "classic":
       # Creative classic memes (random template each request) driven by live mood/keywords (+event).
       top_text, bottom_text = build_classic_meme_copy(
           mood=mood,
           keywords=keywords,
           q=q,
           business=business,
           offer=offer,
           event=posts[0].get("event") if posts else None,
       )
       classic_top, classic_bottom = top_text, bottom_text


       png = render_classic_meme_png(
           images=images[:2],
           top_text=top_text,
           bottom_text=bottom_text,
           business=business,
           offer=offer,
           show_cta=False,
       )
       tiles_used = 1
   else:
       png = render_grid_meme_png(
           images=images,
           mood=mood,
           keywords=keywords,
           business=business,
           offer=offer,
           tiles=tiles_used,
       )
   data_url = "data:image/png;base64," + base64.b64encode(png).decode("utf-8")


   caption = f"Mood: {mood}. Keywords: {', '.join(keywords[:4])}. {offer} at {business} tonight."
   if style == "classic" and classic_top and classic_bottom:
       caption = f"{classic_top} — {classic_bottom}. {offer} at {business}."


   return {
       "caption": caption,
       "imageDataUrl": data_url,
       "mood": mood,
       "keywords": keywords,
       "avgCompoundSentiment": avg,
       "subreddit": subreddit,
       "query": q,
       "style": style,
       "classicTopText": classic_top,
       "classicBottomText": classic_bottom,
       "focus": focus,
       "focusTerms": focus_terms,
       "bgImagesUsed": len(images[:2]) if style == "classic" else None,
       "tilesRequested": tiles,
       "tilesUsed": tiles_used,
       "imagesFound": len([p for p in posts if (p.get('imageUrls') or p.get('imageUrl'))]),
       "imagesUsed": len(images),
       "imageUrlsUsed": image_urls_selected,
   }




@app.get("/meme.png")
async def meme_png(
   business: str = Query(default="local pizza shop", description="Business type"),
   offer: str = Query(default="15% OFF", description="Promo offer text"),
   q: str = Query(default="super bowl", description="Buzz query"),
   tiles: int = Query(default=4, ge=1, le=4, description="How many photos in grid (1,2,4)"),
   subreddit: str = Query(default="nfl", description="Reddit subreddit"),
   style: Literal["grid", "classic"] = Query(default="grid", description="Meme style: grid (1/2/4) or classic (top/bottom)"),
   focus: bool = Query(default=False, description="Focus on Bad Bunny + Seahawks + Patriots (bias keywords + image selection)"),
   bg_random_1_or_2: bool = Query(default=True, description="(classic) Randomly use 1 or 2 background photos"),
):
   """
   Grid meme PNG (1/2/4 photos) using live Reddit images + sentiment-derived keywords.
   """
   raw_posts = await fetch_from_reddit(subreddit=subreddit, q=q, limit=25)
   posts = [
       {**p, "sentiment": compound_sentiment(p["text"]), "event": detect_event(p["text"])}
       for p in raw_posts
   ]
   avg = sum(p["sentiment"] for p in posts) / max(len(posts), 1)
   vibe = "neutral"
   if avg > 0.2:
       vibe = "positive/hype"
   elif avg < -0.2:
       vibe = "negative/salty"


   keywords = extract_keywords_after_sentiment(posts, vibe=vibe, top_k=6)
   mood = vibe_word(vibe)


   focus_terms = focus_terms_bad_bunny_seahawks_patriots() if focus else []
   if focus:
       keywords = bias_keywords_with_focus(keywords, focus_terms, top_k=6)


   img_posts = [p for p in posts if p.get("imageUrl")]
   if tiles not in (1, 2, 4):
       tiles = 4
   want = tiles if tiles != 3 else 4
   image_urls_selected: list[str] = []

   if style == "classic":
       candidates = [p for p in posts if (p.get("imageUrls") or p.get("imageUrl"))]
       if focus and focus_terms:
           focus_hits = [p for p in candidates if any(t.lower() in _post_text(p) for t in focus_terms)]
           if focus_hits:
               candidates = focus_hits

       src = random.choice(candidates) if candidates else None
       urls = (src.get("imageUrls") if src else None) or []
       if (not urls) and src and src.get("imageUrl"):
           urls = [src["imageUrl"]]

       want_images = 1
       if bg_random_1_or_2 and len(urls) >= 2 and random.random() < 0.5:
           want_images = 2
       image_urls_selected = random.sample(urls, k=min(want_images, len(urls))) if urls else []
   else:
       want_images = want
       if focus and focus_terms:
           focus_hits = [p for p in img_posts if any(t.lower() in _post_text(p) for t in focus_terms)]
           if len(focus_hits) >= 1:
               img_posts = focus_hits

       if len(img_posts) > want_images:
           img_posts = random.sample(img_posts, k=want_images)
       else:
           img_posts = img_posts[:want_images]
       image_urls_selected = [p["imageUrl"] for p in img_posts if p.get("imageUrl")]


   images: list[Image.Image] = []
   for u in image_urls_selected:
       bimg = await fetch_image_bytes(u)
       if not bimg:
           continue
       try:
           images.append(Image.open(io.BytesIO(bimg)))
       except Exception:
           continue


   tiles_used = tiles
   if not images:
       tiles_used = 1


   if style == "classic":
       top_text, bottom_text = build_classic_meme_copy(
           mood=mood,
           keywords=keywords,
           q=q,
           business=business,
           offer=offer,
           event=posts[0].get("event") if posts else None,
       )
       png = render_classic_meme_png(
           images=images[:2],
           top_text=top_text,
           bottom_text=bottom_text,
           business=business,
           offer=offer,
           show_cta=False,
       )
   else:
       png = render_grid_meme_png(
           images=images,
           mood=mood,
           keywords=keywords,
           business=business,
           offer=offer,
           tiles=tiles_used,
       )
   return Response(content=png, media_type="image/png")




@app.get("/meme_card.png")
async def meme_card_png(
   business: str = Query(default="local pizza shop"),
   offer: str = Query(default="15% OFF"),
   q: str = Query(default="super bowl"),
):
   """
   Legacy promo-card meme (kept so you still have the older style available).
   """
   b = await buzz(source="reddit", subreddit="nfl", q=q, limit=25)
   seed = b["memeSeed"]
   event = seed["event"]
   vibe = seed["vibe"]
   headline, punchline, cta = build_food_bev_copy(vibe=vibe, event=event, business=business, offer=offer, celeb_title=None)
   footer = f"Live signal: r/nfl • query='{q}' • sentiment={b['avgCompoundSentiment']:.2f}"
   png = render_meme_png(headline=headline, punchline=punchline, cta=cta, footer=footer, background=None)
   return Response(content=png, media_type="image/png")




@app.post("/x/post_latest")
async def x_post_latest(
   business: str = Query(default="local pizza shop"),
   offer: str = Query(default="15% OFF"),
   q: str = Query(default="super bowl"),
   with_celeb: bool = Query(default=True),
):
   """
   Generates a meme (PNG) and attempts to post to X/Twitter.
   If credentials are missing, returns a payload you can manually post.
   """
   # Post the new grid meme by default (still returns a "ready-to-post" payload if creds missing).
   b = await meme(business=business, offer=offer, q=q, tiles=4, subreddit="nfl")
   # Decode data_url -> bytes
   data_url: str = b["imageDataUrl"]
   b64 = data_url.split(",", 1)[1]
   png = base64.b64decode(b64.encode("utf-8"))
   text = b["caption"]
   res = x_post_image_and_text(png, text)
   return {"caption": text, "mood": b.get("mood"), "keywords": b.get("keywords"), "x": res}





