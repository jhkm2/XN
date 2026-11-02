# coding=utf-8

import base64
import gzip
import html as html_lib
import json
import re
import ssl
import threading
import time
import urllib.request
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

try:  # TVBox 运行时提供 base.spider
    from base.spider import Spider as BaseSpider
except Exception:  # 本地自测 / 其他宿主兜底
    class BaseSpider(object):
        def init(self, extend=""):
            pass

# 插件配置声明（alist-tvbox 网页端「订阅源 → 配置」自动渲染可视化表单；
# 用户填写的值经 ext → data → init(extend) 原样传回，见 docs/plugin_config_schema.md）
PLUGIN_CONFIG_SCHEMA = {
    "description": "SeedHub（hubdog.cc）磁力/网盘源配置",
    "allowAdditional": False,
    "fields": [
        {"key": "sites", "label": "站点地址", "type": "string", "required": False,
         "aliases": ["site", "host", "url"],
         "defaultValue": "https://hubdog.cc",
         "placeholder": "https://hubdog.cc，多个用逗号分隔，留空用内置镜像清单"},
        {"key": "pic_proxy", "label": "封面图代理前缀", "type": "string", "required": False,
         "description": "封面图床（sh1.pcie.pppoe.top）走 Cloudflare，客户端网络打不开该域名时，"
                        "填一个自己可达的图片代理前缀；留空=直连图床",
         "placeholder": "https://images.weserv.nl/?url={url}（{url} 替换为原图地址）"},
    ],
}

PLUGIN_ID = "seedhub-hu60d0g"

SITE_NAME = "SeedHub"

# 站点域名清单，主域失败自动切镜像
# 注：sidhub/seeduck 同为 Cloudflare；104.243.25.80 为裸 IP 源站（HTTP），
#     SNI 阻断只杀域名，裸 IP 直连在某些网络可通，作最终兜底
DEFAULT_HOSTS = [
    "https://hubdog.cc",
    "https://sidhub.cc",
    "https://seeduck.cc",
    "http://104.243.25.80",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

FIXED_CLASSES = [
    {"type_id": "rec", "type_name": "推荐"},
    {"type_id": "mv", "type_name": "电影"},
    {"type_id": "tv", "type_name": "电视剧"},
    {"type_id": "ac", "type_name": "动漫"},
]

# type_id -> 站点分类目录（None 表示首页）
CLASS_PATHS = {"mv": "1", "tv": "3", "ac": "2", "rec": None}

SORT_PAIRS = [
    ("最近更新", "update"),
    ("近期热门", "view"),
    ("上映时间", "date"),
    ("豆瓣评分", "score"),
]


def _opts(pairs):
    return [{"n": n, "v": v} for n, v in pairs]


FIXED_FILTERS = {
    "mv": [{"key": "sort", "name": "排序", "value": _opts(SORT_PAIRS)}],
    "tv": [{"key": "sort", "name": "排序", "value": _opts(SORT_PAIRS)}],
    "ac": [{"key": "sort", "name": "排序", "value": _opts(SORT_PAIRS)}],
}

# 网盘识别：域名片段 -> 内部键
DISK_PATTERNS = [
    ("quark", ("pan.quark.cn",)),
    ("baidu", ("pan.baidu.com",)),
    ("xunlei", ("pan.xunlei.com",)),
    ("uc", ("drive.uc.cn", "uc.cn")),
    ("aliyun", ("alipan.com", "aliyundrive.com", "pan.aliyun.com")),
    ("123", ("123pan.com", "123pan.cn", "123684.com")),
    ("115", ("115.com", "anxia.com")),
    ("tianyi", ("cloud.189.cn", "189.cn")),
    ("mobile", ("yun.139.com", "caiyun.139.com", "139.com")),
]

PAN_TITLES = {
    "quark": "夸克", "baidu": "百度", "xunlei": "迅雷", "uc": "UC",
    "aliyun": "阿里", "123": "123", "115": "115", "tianyi": "天翼",
    "mobile": "移动", "others": "其他",
}
PAN_ORDER = ["quark", "baidu", "xunlei", "uc", "aliyun", "123", "115",
             "tianyi", "mobile", "others"]

MAGNET_TITLE = "磁力"

# Atvp 延迟解析前缀（与 Atvp.PUSH_PREFIX 对齐）：
# 带 prefix 的链接在二级目录/播放上下文里被剥壳，点开时回调 playerContent 换真链
DEFERRED_PREFIX = "push://"

# 站点对并发很敏感：detail 里批量解析中转页会触发限流（实测 40 并发后详情页直接取不到），
# 所以 detail 只产出中转链接，真正的解析放到 playerContent 点播时按需做（带缓存）。
PAGE_SIZE = 20
MAX_SEEDS = 120
MAX_PANS = 160

# 列表页（首页/分类/搜索）TTL 缓存：设备到站点（Cloudflare）链路偏慢时，
# 重进分类/翻回已看过的页直接秒出
PAGE_CACHE_TTL = 300
PAGE_CACHE_MAX = 64

# Cloudflare 人机验证页特征（盾页会被当成“成功抓到”导致解析为空，必须识别后走容灾）
CHALLENGE_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform",
                     "cf_chl_", "_cf_chl", "attention required", "checking your browser",
                     "enable javascript and cookies")
CHALLENGE_HEAD = 6000

# curl_cffi（可选依赖）：以真实 Chrome TLS 指纹发请求。
# 服务端(T4)执行时，容器的 Python/OpenSSL 指纹会被 Cloudflare 盯上（人机验证页），
# 换代理节点无效；装上 curl_cffi 后按 Chrome 指纹直连即可过盾：
#   docker exec alist-tvbox sh -c "apk add --no-cache py3-pip \
#     && python3 -m pip install --break-system-packages curl-cffi"
# 未安装时自动退回普通 requests（客户端执行不受影响）。
try:
    from curl_cffi import requests as _cffi_requests
except Exception:
    _cffi_requests = None

# FlareSolverr 解盾结果缓存（解一次 5~20s，必须复用）
FS_CACHE_TTL = 600
FS_CACHE_MAX = 64
FS_MAX_TIMEOUT = 25000  # T4 单次调用死线 30s，解盾超时留余量


class Spider(BaseSpider):
    def __init__(self):
        self.name = "%s[盘]" % SITE_NAME
        self.hosts = [self._normalize_host(site)[0] for site in DEFAULT_HOSTS]
        self.host = ""
        self.referer = ""
        # 关键：告诉宿主“网盘/磁力链接由你解析”，插件只诚实回链
        self.backend_parse = True
        self.headers = {"User-Agent": UA, "Accept": ACCEPT}
        self.pic_proxy = ""
        self._lock = threading.RLock()
        self._item_cache = {}
        self._link_cache = {}
        self._detail_cache = {}
        self._page_cache = {}
        self._home_cache = ("", 0.0)
        self._activate_host(self.hosts[0])

    def init(self, extend=""):
        cfg = self._parse_extend(extend)
        sites = self._parse_sites(cfg.get("site") or cfg.get("sites") or cfg.get("url"))
        self.hosts = sites or [self._normalize_host(site)[0] for site in DEFAULT_HOSTS]
        self._activate_host(self.hosts[0])
        self.pic_proxy = str(cfg.get("pic_proxy") or "").strip()
        with self._lock:
            self._item_cache.clear()
            self._link_cache.clear()
            self._detail_cache.clear()
            self._page_cache.clear()
            self._home_cache = ("", 0.0)
        return None

    def getName(self):
        return self.name

    def danmaku(self):
        # 站点不提供弹幕
        return False

    # ---- config / host utils ----
    def _parse_extend(self, extend):
        """兼容四种形态：dict / JSON 字符串 / base64(JSON) / 裸站点 URL。

        Atvp 形态 1（.py 插件经包装层加载）传 {"token", "local_proxy_config",
        <插件自定义字段原样合并在顶层>}；形态 2（旧宿主直载）可能带 api/token。
        本插件只消费 sites/site/host/url，其余字段忽略即可。
        """
        if isinstance(extend, dict):
            return extend
        text = str(extend or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except Exception:
            # 兼容 base64(JSON) 形态的 ext
            try:
                padded = text + "=" * (-len(text) % 4)
                data = json.loads(base64.b64decode(padded).decode("utf-8", "ignore"))
            except Exception:
                data = None
            if isinstance(data, dict):
                return data
            # 兼容直接传站点的写法：init("https://sidhub.cc")
            if re.match(r"(?i)^https?://", text):
                return {"site": text}
            return {}
        return data if isinstance(data, dict) else {}

    def _normalize_host(self, value):
        host = str(value or "").strip().rstrip("/")
        if not host:
            host = DEFAULT_HOSTS[0]
        if not re.match(r"(?i)^https?://", host):
            host = "https://" + host
        parsed = urlsplit(host)
        hostname = parsed.hostname or ""
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except Exception:
            ascii_host = hostname
        netloc = ascii_host
        if parsed.port:
            netloc += ":%d" % parsed.port
        normalized = urlunsplit((parsed.scheme or "https", netloc,
                                 parsed.path.rstrip("/"), "", ""))
        referer = urlunsplit((parsed.scheme or "https", netloc, "", "", ""))
        return normalized, referer

    def _parse_sites(self, value):
        if isinstance(value, (list, tuple)):
            raw_sites = value
        else:
            raw_sites = re.split(r"[\s,，|]+", str(value or "").strip())
        sites = []
        for raw in raw_sites:
            text = str(raw or "").strip()
            if not text:
                continue
            normalized, _ = self._normalize_host(text)
            if normalized and normalized not in sites:
                sites.append(normalized)
        return sites

    def _activate_host(self, host):
        normalized, referer = self._normalize_host(host)
        self.host = normalized
        self.referer = referer or normalized
        self.headers["Referer"] = self.referer + "/"

    def _ordered_hosts(self):
        return [self.host] + [h for h in self.hosts if h != self.host]

    def _cover_url(self, pic):
        """封面改写：配置了 pic_proxy 时经图代中转（图床 sh1.pcie.pppoe.top 走
        Cloudflare，部分客户端网络 DNS/SNI 层打不开该域名，直连会全灰）"""
        pic = str(pic or "").strip()
        if not pic or not self.pic_proxy:
            return pic
        proxy = self.pic_proxy
        if "{url}" in proxy:
            return proxy.replace("{url}", quote(pic, safe=""))
        return proxy + quote(pic, safe="")

    def _cached_page(self, path, fetcher, timeout=12):
        """列表页 TTL 缓存：只缓存成功结果，重进分类/翻页不重复抓站"""
        with self._lock:
            hit = self._page_cache.get(path)
        if hit and time.time() - hit[1] < PAGE_CACHE_TTL:
            return hit[0]
        html = fetcher(path, timeout)
        if html:
            with self._lock:
                if len(self._page_cache) >= PAGE_CACHE_MAX and path not in self._page_cache:
                    self._page_cache.pop(next(iter(self._page_cache)))
                self._page_cache[path] = (html, time.time())
        return html

    # ---- http ----
    def _build_headers(self, extra=None):
        headers = dict(self.headers)
        if extra:
            headers.update(extra)
        return headers

    def _request_text(self, url, extra_headers=None, timeout=12):
        """抓取入口：盾页一律按失败处理（返回空 → 触发镜像容灾），不把验证页当内容"""
        text = self._request_text_raw(url, extra_headers, timeout)
        if text and self._looks_like_challenge(text):
            return ""
        return text

    def _looks_like_challenge(self, html):
        if not html:
            return False
        head = html[:CHALLENGE_HEAD].lower()
        return any(marker in head for marker in CHALLENGE_MARKERS)

    def _request_text_raw(self, url, extra_headers=None, timeout=12):
        """优先用宿主给的 fetch（与观影.py 一致），缺失时退回 curl_cffi / requests / urllib"""
        headers = self._build_headers(extra_headers)
        fetcher = getattr(self, "fetch", None)
        if callable(fetcher):
            try:
                resp = fetcher(url, headers=headers, timeout=timeout,
                               verify=False, allow_redirects=True)
                status = int(getattr(resp, "status_code", 200) or 200)
                if status < 400:
                    return str(getattr(resp, "text", "") or "")
                if status in (403, 404, 410):
                    return ""
            except Exception:
                pass

        # curl_cffi：真实 Chrome TLS 指纹（服务端执行被 CF 盾时的解法，可选依赖）
        if _cffi_requests is not None:
            try:
                r = _cffi_requests.get(url, headers=headers, timeout=timeout,
                                       impersonate="chrome", verify=False)
                status = int(getattr(r, "status_code", 0) or 0)
                if status < 400:
                    return self._decode(r.content, "")
                if status in (403, 404, 410):
                    return ""
            except Exception:
                pass

        try:
            import requests
            r = requests.get(url, headers=headers, timeout=timeout, verify=False)
            if r.status_code < 400:
                return self._decode(r.content, r.headers.get("Content-Type", ""))
            return ""
        except Exception:
            pass

        try:
            req = urllib.request.Request(url, headers=headers)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                ce = (resp.headers.get("Content-Encoding") or "").lower()
                try:
                    if "gzip" in ce:
                        raw = gzip.decompress(raw)
                    elif "deflate" in ce:
                        import zlib
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                except Exception:
                    pass
                return self._decode(raw, resp.headers.get("Content-Type", ""))
        except Exception:
            return ""

    def _decode(self, raw, content_type=""):
        if not raw:
            return ""
        enc = ""
        match = re.search(r"charset=[\"']?([\w\-]+)", content_type or "", re.I)
        if match:
            enc = match.group(1)
        if not enc:
            match = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", raw[:2048], re.I)
            if match:
                enc = match.group(1).decode("ascii", "ignore")
        for item in (enc, "utf-8", "gb18030"):
            if not item:
                continue
            try:
                return raw.decode(item)
            except Exception:
                continue
        return raw.decode("utf-8", "ignore")

    def _absolute(self, path):
        if not path:
            return self.host + "/"
        if path.startswith("//"):
            return "https:" + path
        if re.match(r"(?i)^https?://", path):
            return path
        return urljoin(self.host + "/", str(path).lstrip("/"))

    def _fetch(self, path, referer=None, timeout=12):
        """站内页面抓取：主域失败按 _ordered_hosts 依次切镜像"""
        url = self._absolute(path)
        text = self._request_text(url, {"Referer": referer or (self.referer + "/")}, timeout)
        if text:
            return text
        parsed = urlsplit(url)
        origin = "%s://%s" % (parsed.scheme, parsed.netloc)
        if origin not in self.hosts:
            return ""
        tail = parsed.path + (("?" + parsed.query) if parsed.query else "")
        for host in self._ordered_hosts():
            if host == origin:
                continue
            alt = host + tail
            text = self._request_text(alt, {"Referer": host + "/"}, timeout)
            if text:
                self._activate_host(host)
                return text
        return ""

    # ---- html utils ----
    def _text(self, frag):
        if not frag:
            return ""
        value = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", frag)
        value = re.sub(r"(?is)<br\s*/?>", "\n", value)
        value = re.sub(r"(?s)<[^>]+>", "", value)
        value = html_lib.unescape(value).replace("\u00a0", " ").replace("\u3000", " ")
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        return re.sub(r"\n\s*\n+", "\n", value).strip()

    def _attr(self, tag, name):
        match = re.search(r'%s\s*=\s*"([^"]*)"' % re.escape(name), tag or "", re.I)
        return html_lib.unescape(match.group(1)) if match else ""

    # ---- item cache ----
    def _remember_items(self, items):
        with self._lock:
            for item in items:
                vod_id = str(item.get("vod_id") or "").strip()
                title = str(item.get("vod_name") or "").strip()
                if not vod_id or not title or title == vod_id:
                    continue
                if len(self._item_cache) >= 512 and vod_id not in self._item_cache:
                    self._item_cache.pop(next(iter(self._item_cache)))
                self._item_cache[vod_id] = {
                    "vod_name": title,
                    "vod_pic": str(item.get("vod_pic") or "").strip(),
                    "vod_year": str(item.get("vod_year") or "").strip(),
                    "vod_remarks": str(item.get("vod_remarks") or "").strip(),
                }
        return items

    def _cached_item(self, vod_id):
        with self._lock:
            return dict(self._item_cache.get(vod_id) or {})

    # ---- list parsers ----
    def _parse_covers(self, html):
        items = []
        page = str(html or "")
        if not page:
            return items
        start = page.find('<div class="cover-container">')
        if start > 0:
            page = page[start:]
        seen = set()
        for block in page.split('<div class="cover">')[1:]:
            block = block.split("</ul>")[0]
            match = re.search(r'href="(/movies/(\d+)/)"', block)
            if not match:
                continue
            rid = match.group(2)
            if rid in seen:
                continue
            seen.add(rid)

            title = ""
            heading = re.search(r"</a>\s*([^<]+?)\s*</h2>", block, re.S)
            if heading:
                title = html_lib.unescape(heading.group(1)).strip()
            img_tag = re.search(r"<img[^>]*>", block)
            if not title and img_tag:
                title = self._attr(img_tag.group(0), "alt")
            if not title:
                continue

            cover = self._attr(img_tag.group(0), "src") if img_tag else ""
            if cover.startswith("//"):
                cover = "https:" + cover

            year = area = actor = score = ""
            meta = re.search(r"</h2></li>\s*<li>(.*?)</li>", block, re.S)
            if meta:
                parts = [p.strip() for p in self._text(meta.group(1)).split("/")]
                if len(parts) >= 4:
                    year, area = parts[0], parts[2]
                    actor = " / ".join(parts[4:]).strip(" /")
                elif parts:
                    year = parts[0]
            score_match = re.search(r"豆瓣评分:\s*<a[^>]*>(.*?)</a>", block, re.S)
            if score_match:
                score = self._text(score_match.group(1))

            items.append({
                "vod_id": "sd_%s" % rid,
                "vod_name": title,
                "vod_pic": self._cover_url(cover),
                "vod_year": year,
                "vod_area": area,
                "vod_actor": actor,
                "vod_remarks": "%s / 豆瓣 %s" % (year or "—", score or "0"),
            })
        return self._remember_items(items)

    def _parse_detail_page(self, html):
        detail = {"name": "", "pic": "", "meta": {}, "desc": "",
                  "seeds": [], "pans": []}
        page = str(html or "")
        if not page:
            return detail

        match = re.search(r'<h1 id="cover">(.*?)</h1>', page, re.S)
        if match:
            detail["name"] = self._text(match.group(1)).lstrip("#").strip()
        match = re.search(r'<div class="cover-container">\s*<img[^>]*>', page)
        if match:
            detail["pic"] = self._cover_url(self._attr(match.group(0), "src"))
            if detail["pic"].startswith("//"):
                detail["pic"] = "https:" + detail["pic"]

        start = page.find('<div class="cover-container">')
        if start > 0:
            end = page.find("</ul>", start)
            block = page[start:end if end > 0 else start + 6000]
            for row in re.findall(r"<li>(.*?)</li>", block, re.S):
                text = self._text(row)
                parts = re.split(r"[:：]", text, 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    if key and key not in detail["meta"]:
                        detail["meta"][key] = parts[1].strip()

        match = re.search(r'<h2 id="description">.*?</h2>\s*<p>(.*?)</p>', page, re.S)
        if match:
            detail["desc"] = self._text(match.group(1))

        match = re.search(r'<ul class="seeds"[^>]*>(.*?)</ul>', page, re.S)
        if match:
            for row in re.findall(r"<li>(.*?)</li>", match.group(1), re.S)[:MAX_SEEDS]:
                seed = re.search(r'seed_id=(\d+)', row)
                if not seed:
                    continue
                tag = re.search(r"<a[^>]*>", row)
                title = self._attr(tag.group(0), "title") if tag else ""
                size = ""
                size_match = re.search(r'class="size"[^>]*>([^<]*)<', row)
                if size_match:
                    size = size_match.group(1).strip()
                features = [f.strip() for f in
                            re.findall(r'class="seed-feature"[^>]*>([^<]*)<', row)
                            if f.strip()]
                detail["seeds"].append({"seed_id": seed.group(1), "title": title,
                                        "size": size, "features": features})

        match = re.search(r'<ul class="pan-links"[^>]*>(.*?)</ul>', page, re.S)
        if match:
            for row in re.findall(r"<li>(.*?)</li>", match.group(1), re.S)[:MAX_PANS]:
                tag_match = re.search(r"<a[^>]*>", row)
                if not tag_match:
                    continue
                tag = tag_match.group(0)
                href = self._attr(tag, "href")
                pan_id = re.search(r"redirect_to=(pan_id_\d+)", href)
                if not pan_id:
                    continue
                raw_url = "https://" + self._attr(tag, "data-link").lower()
                detail["pans"].append({
                    "pan_id": pan_id.group(1),
                    "disk": self._identify_disk(raw_url) or "others",
                    "title": self._clean_name(self._attr(tag, "title") or self._text(row)),
                })
        return detail

    # ---- disk utils ----
    def _identify_disk(self, url):
        text = str(url or "").strip().lower()
        for disk, patterns in DISK_PATTERNS:
            for pattern in patterns:
                if pattern in text:
                    return disk
        if text.startswith(("http://", "https://")):
            return "others"
        return ""

    def _pan_title(self, disk):
        return PAN_TITLES.get(disk or "", PAN_TITLES["others"])

    def _encode_play_url(self, url):
        return str(url or "").replace("#", "%23")

    def _clean_name(self, name, limit=48):
        text = self._text(name)
        text = re.sub(r"\s*更新于.*$", "", text)
        text = re.sub(r"[\s　]*(刚刚|今天|昨天|前天|\d+\s*(?:秒|分钟|小时|天|周|月)前)\s*$", "", text)
        text = re.sub(r"\s*提取码[:：].*$", "", text)
        text = re.sub(r"^[^\u4e00-\u9fa5A-Za-z0-9【\[(（]+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > limit:
            text = text[:limit].rstrip()
        return text or "资源"

    def _seed_name(self, seed, index):
        head = []
        if seed["features"]:
            head.append("·".join(seed["features"][:2]))
        if seed["size"]:
            head.append(seed["size"])
        prefix = (" ".join(head) + " | ") if head else ""
        title = self._clean_name(seed["title"], 44)
        return "[%02d] %s%s" % (index, prefix, title)

    # ---- link_start 解析 ----
    def _cached_link(self, query):
        with self._lock:
            hit = self._link_cache.get(query)
        if hit and time.time() - hit[1] < 900:
            return hit[0]
        return ""

    def _remember_link(self, query, url):
        if not url:
            return ""
        with self._lock:
            if len(self._link_cache) >= 512:
                self._link_cache.pop(next(iter(self._link_cache)))
            self._link_cache[query] = (url, time.time())
        return url

    def _extract_link(self, page):
        """中转页 -> 真实链接：磁力(base64 内嵌/明文) 或 网盘直达链接"""
        if not page:
            return ""
        url = ""
        for match in re.finditer(r'(?:const|let|var)\s+\w+\s*=\s*"([A-Za-z0-9+/=]{24,})"', page):
            try:
                decoded = base64.b64decode(match.group(1) + "===").decode("utf-8", "ignore")
            except Exception:
                continue
            if decoded.lower().startswith("magnet:"):
                url = decoded.strip()
                break
        if not url:
            match = re.search(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{16,}[^\"'<>\s\\]*", page)
            if match:
                url = html_lib.unescape(match.group(0))
        if not url:
            match = re.search(r'class="direct-pan"[^>]*>', page)
            if match:
                url = self._attr(match.group(0), "href")
            if not url:
                match = re.search(r'href="(https?://[^"]*(?:pan\.baidu\.com|pan\.quark\.cn|'
                                  r'drive\.uc\.cn|pan\.xunlei\.com|alipan\.com|'
                                  r'aliyundrive\.com)[^"]*)"', page)
                if match:
                    url = html_lib.unescape(match.group(1))
        return url

    def _link_start(self, query):
        """解析一条中转链接（网盘 -> 分享链接；磁力 -> magnet），带缓存"""
        cached = self._cached_link(query)
        if cached:
            return cached
        page = self._fetch("/link_start/?" + query, self.referer + "/", timeout=8)
        url = self._extract_link(page)
        if not url and page:
            # 偶发截断/限流：再试一次
            time.sleep(0.15)
            url = self._extract_link(self._fetch("/link_start/?" + query,
                                                 self.referer + "/", timeout=8))
        return self._remember_link(query, url)

    # ---- play fields ----
    def _detail(self, rid):
        with self._lock:
            hit = self._detail_cache.get(rid)
        if hit and time.time() - hit[1] < 900:
            return hit[0]
        detail = self._parse_detail_page(self._fetch("/movies/%s/" % rid, self.referer + "/"))
        if not detail.get("name"):
            # 偶发限流/网络抖动：重试一次，避免详情空返回
            time.sleep(0.2)
            detail = self._parse_detail_page(self._fetch("/movies/%s/" % rid, self.referer + "/"))
        if detail.get("name"):
            with self._lock:
                if len(self._detail_cache) >= 128:
                    self._detail_cache.pop(next(iter(self._detail_cache)))
                self._detail_cache[rid] = (detail, time.time())
        return detail

    def _build_play_fields(self, detail):
        """组装 play_from / play_url / group，与观影.py 同构

        这里只产出站内中转链接（解析一条就要一次请求，详情页有 ~2s 死线，
        批量解析会触发站点限流）。链接统一带 push:// 前缀走 Atvp 延迟解析：
        用户点中资源后，Atvp 回调本插件 playerContent 现场换成真实分享链接，
        再交给 POST /parse 挂载——/parse 不认站内中转页，不能直接透传。
        """
        buckets = {}
        for pan in detail.get("pans") or []:
            buckets.setdefault(pan["disk"], []).append(pan)
        seeds = detail.get("seeds") or []

        play_from = []
        play_url = []
        group = []

        for disk in PAN_ORDER:
            items = buckets.get(disk) or []
            if not items:
                continue
            media = []
            for pan in items:
                url = self._absolute("/link_start/?redirect_to=" + pan["pan_id"])
                media.append({"name": self._clean_name(pan.get("title")),
                              "url": DEFERRED_PREFIX + self._encode_play_url(url)})
            title = self._pan_title(disk)
            for index, item in enumerate(media, 1):
                play_from.append("%s%d" % (title, index) if len(media) > 1 else title)
                play_url.append("%s$%s" % (item["name"], item["url"]))
            group.append({"name": title, "media": media})

        if seeds:
            media = []
            for index, seed in enumerate(seeds, 1):
                url = self._absolute("/link_start/?seed_id=" + seed["seed_id"])
                media.append({"name": self._seed_name(seed, index),
                              "url": DEFERRED_PREFIX + self._encode_play_url(url)})
            for index, item in enumerate(media, 1):
                play_from.append("%s%d" % (MAGNET_TITLE, index) if len(media) > 1 else MAGNET_TITLE)
                play_url.append("%s$%s" % (item["name"], item["url"]))
            group.append({"name": MAGNET_TITLE, "media": media})

        return "$$$".join(play_from), "$$$".join(play_url), group

    # ---- handlers ----
    def _normalize_vod_id(self, raw_id):
        """详情 id 归一化：历史记录里可能出现各种形态
        （sd_123 / 123 / atvp_detail:sd_123 / 详情页 URL），统一解析回站点数字 id"""
        text = str(raw_id or "").strip()
        if text.startswith(DEFERRED_PREFIX):
            text = text[len(DEFERRED_PREFIX):]
        if text.startswith("atvp_detail:"):
            text = text[len("atvp_detail:"):]
        if text.startswith("sd_"):
            text = text[len("sd_"):]
        match = re.search(r"(\d+)", text)
        return match.group(1) if match else ""

    def homeContent(self, filter):
        return {"class": FIXED_CLASSES, "filters": FIXED_FILTERS}

    def homeVideoContent(self):
        try:
            html, stamp = self._home_cache
            if not html or time.time() - stamp > 600:
                html = self._cached_page("/", lambda p, t=12: self._fetch("/", self.referer + "/", t))
                if html:
                    self._home_cache = (html, time.time())
            return {"list": self._parse_covers(html)}
        except Exception:
            return {"list": []}

    def categoryContent(self, tid, pg, filter, extend):
        page = int(pg or 1)
        tid = str(tid or "").strip()
        if tid not in CLASS_PATHS:
            return {"page": page, "pagecount": page, "limit": 0, "total": 0, "list": []}
        try:
            extend = extend if isinstance(extend, dict) else self._parse_extend(extend)
            sort = str(extend.get("sort") or "update").strip()
            if sort not in [v for _, v in SORT_PAIRS]:
                sort = "update"

            directory = CLASS_PATHS.get(tid)
            if not directory:
                if page > 1:
                    return {"page": page, "pagecount": 1, "limit": 0, "total": 0, "list": []}
                html = self._cached_page("/", lambda p, t=12: self._fetch("/", self.referer + "/", t))
            else:
                path = "/categories/%s/movies/?page=%d&order=%s" % (directory, page, sort)
                html = self._cached_page(
                    path,
                    lambda p, t=12, _path=path: self._fetch(_path, self.referer + "/", t))

            items = self._parse_covers(html)
            pages = [int(x) for x in re.findall(r"\?page=(\d+)", html or "")] or [1]
            pagecount = max(pages) if directory else 1
            return {"page": page, "pagecount": pagecount, "limit": PAGE_SIZE,
                    "total": pagecount * PAGE_SIZE if items else 0, "list": items}
        except Exception:
            return {"page": page, "pagecount": 0, "limit": 0, "total": 0, "list": []}

    def searchContent(self, key, quick, pg="1"):
        page = int(pg or 1)
        keyword = str(key or "").strip()
        if not keyword:
            return {"page": page, "pagecount": page, "limit": 0, "total": 0, "list": []}
        try:
            path = "/s/%s/" % quote(re.sub(r"[/\\?#]+", " ", keyword), safe="")
            if page > 1:
                path += "?page=%d" % page
            html = self._cached_page(
                path, lambda p, t=12, _path=path: self._fetch(_path, self.referer + "/", t))
            items = self._parse_covers(html)
            pages = [int(x) for x in re.findall(r"\?page=(\d+)", html or "")] or [1]
            return {"page": page, "pagecount": max(pages), "limit": PAGE_SIZE,
                    "total": len(items), "list": items}
        except Exception:
            return {"page": page, "pagecount": 0, "limit": 0, "total": 0, "list": []}

    def detailContent(self, ids):
        result = {"list": []}
        for raw_id in ids:
            rid = self._normalize_vod_id(raw_id)
            if not rid:
                continue
            vod_id = "sd_%s" % rid
            cached = self._cached_item(vod_id)
            detail = self._detail(rid)
            if not detail.get("name") and not cached.get("vod_name"):
                continue

            play_from, play_url, group = self._build_play_fields(detail)
            meta = detail.get("meta") or {}
            title = detail.get("name") or cached.get("vod_name") or ("影片 %s" % rid)
            detail_lines = []
            for key in ("导演", "编剧", "主演", "类型", "制片国家/地区", "语言",
                        "首播", "集数", "单集片长", "又名", "豆瓣评分"):
                if meta.get(key):
                    detail_lines.append("%s: %s" % (key, meta[key]))
            content = detail.get("desc") or ""
            if detail_lines:
                content = (content + "\n\n" if content else "") + "\n".join(detail_lines)
            content = (content + "\n\n" if content else "") + (
                "【提示】本站为磁力 / 网盘资源索引，选中分享后由客户端网盘模块解析播放。")

            pan_count = len(detail.get("pans") or [])
            seed_count = len(detail.get("seeds") or [])
            remark_parts = []
            if pan_count:
                remark_parts.append("%d条网盘" % pan_count)
            if seed_count:
                remark_parts.append("%d条磁力" % seed_count)
            result["list"].append({
                "vod_id": vod_id,
                "vod_name": title,
                "vod_pic": detail.get("pic") or cached.get("vod_pic") or "",
                "vod_year": cached.get("vod_year") or "",
                "vod_area": meta.get("制片国家/地区", ""),
                "vod_lang": meta.get("语言", ""),
                "vod_actor": meta.get("主演", "").strip(" /"),
                "vod_director": meta.get("导演", ""),
                "vod_content": content.strip(),
                "vod_remarks": (" · ".join(remark_parts) if play_url else "无资源"),
                "vod_play_from": play_from,
                "vod_play_url": play_url,
                "group": group,
            })
            self._remember_items(result["list"][-1:])
        return result

    # ---- play ----
    def playerContent(self, flag, id, vipFlags):
        raw = str(id or "").strip()
        if raw.startswith(DEFERRED_PREFIX):
            raw = raw[len(DEFERRED_PREFIX):]
        # 站内中转页（未在 detail 里解析到的分享）当场换成真实链接
        if "/link_start/" in raw:
            query = raw.split("/link_start/?", 1)[-1]
            resolved = self._link_start(query)
            if resolved:
                raw = resolved
        known = self._identify_disk(raw) or raw.startswith(
            ("http://", "https://", "magnet:", "ed2k://", "thunder://"))
        return {
            "parse": 0,
            "playUrl": "",
            "url": raw if known else "",
            "header": {"User-Agent": UA},
        }
