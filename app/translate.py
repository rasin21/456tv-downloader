"""키워드 번역(Google 번역 공개 엔드포인트, 키 불필요)."""
import json
import urllib.parse
import urllib.request

LANGS = {"zh": "zh-CN", "ja": "ja", "en": "en"}


def translate(text: str, target: str) -> str:
    q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text})
    req = urllib.request.Request("https://translate.googleapis.com/translate_a/single?" + q,
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()


def translate_all(text: str) -> dict:
    out = {"ko": text}
    for k, tl in LANGS.items():
        try:
            out[k] = translate(text, tl)
        except Exception:
            out[k] = ""
    return out


SEARCH = {
    "tiktok": "https://www.tiktok.com/search/video?q={q}",
    "douyin": "https://www.douyin.com/search/{q}?type=video",
    "xiaohongshu": "https://www.xiaohongshu.com/search_result?keyword={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "youtube_shorts": "https://www.youtube.com/results?search_query={q}+%23shorts",
    "instagram": "https://www.instagram.com/explore/tags/{tag}/",
}


def search_url(site: str, keyword: str) -> str:
    tpl = SEARCH[site]
    return tpl.format(q=urllib.parse.quote(keyword), tag=urllib.parse.quote(keyword.replace(" ", "")))
