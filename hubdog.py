# -*- coding: utf-8 -*-
# =============================================================================
#  TVBox Python 爬虫源
#  ---------------------------------------------------------------------------
#  站点名称 : SeedHub（页面标题：SeedHub | 影视&动漫分享）
#  主域名   : https://hubdog.cc/
#  官方镜像 : https://sidhub.cc/（canonical 指向该域名，结构完全一致，作容灾备用）
#  站点性质 : 影视 / 动漫 / 剧集 的「磁力 + 网盘」资源索引站（提供 torrent 磁力链
#             与 百度 / 夸克 / 迅雷 / UC / 阿里 网盘分享链接），站点本身无在线播放器。
#
#  接口规范 : TVBox / CatVod Python 源（base.spider.Spider 子类）
#             homeContent / homeVideoContent / categoryContent /
#             detailContent / searchContent / playerContent
#
#  依赖     : requests（缺失时自动降级为 urllib）
#
#  说明     : 因源站无 m3u8 在线流，本源的“播放”是把资源条目解析成磁力链 / 网盘
#             直链；playerContent 同时内置了通用 m3u8 / mp4 提取逻辑（页面直链、
#             JS 转义、URL 编码、base64 内嵌均可识别），若条目本身或日后站点提供
#             流媒体地址，会直接返回可播地址。
#
#  播放列表结构（两层，避免一点就播、看着像漏了选集）：
#     第一层 vod_play_from = 资源类型：磁力下载(N) / 夸克网盘(N) / 百度网盘(N) …
#     第二层 vod_play_url  = 该类型下的具体资源文件：
#                            资源01｜4K蓝光｜17.36G｜09-10｜更18集
#     点条目才进 playerContent 解析磁力 / 网盘直链；条目名里的“更N集/全N集/
#     第A-B集”来自资源标题，方便按集数范围挑选，而不是被误认成“第N集”。
# =============================================================================

import base64
import gzip
import html as html_lib
import json
import re
import ssl
import time
from urllib.parse import quote, unquote, urljoin, urlparse

try:  # TVBox 运行时提供 base.spider
    from base.spider import Spider as BaseSpider
except Exception:  # 本地自测 / 其他宿主兜底
    class BaseSpider(object):
        def init(self, extend=""):
            pass

try:
    import requests
except Exception:
    requests = None

import urllib.error
import urllib.request


class Spider(BaseSpider):

    # ------------------------------------------------------------------ 配置
    site_name = "SeedHub"

    # 主域名 + 镜像，前一个不可用时自动切换
    base_hosts = ("https://hubdog.cc", "https://sidhub.cc")

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    HEADERS = {
        "User-Agent": UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    # 排序方式（对应站点 ?order= 参数）
    ORDERS = (
        ("update", "最近更新"),
        ("view", "近期热门"),
        ("date", "上映时间"),
        ("score", "豆瓣评分"),
    )

    # 网盘域名 -> 展示名
    PAN_NAMES = {
        "pan.baidu.com": "百度网盘",
        "pan.quark.cn": "夸克网盘",
        "drive.uc.cn": "UC网盘",
        "pan.xunlei.com": "迅雷网盘",
        "alipan.com": "阿里云盘",
        "aliyundrive.com": "阿里云盘",
        "www.aliyundrive.com": "阿里云盘",
        "drive.115.com": "115网盘",
        "115.com": "115网盘",
        "caiyun.139.com": "移动云盘",
        "cloud.189.cn": "天翼云盘",
        "pan.123pan.com": "123网盘",
    }

    MEDIA_RE = re.compile(
        r"\.(?:m3u8|mp4|flv|mkv|avi|ts|mov|webm|m4v)(?:$|[?#])", re.I)

    # 单个分类页最多抓取的列表项（站点每页固定 20 条）
    PAGE_SIZE = 20
    # detail 里磁力条目上限，避免剧集类几十上百条把播放列表撑爆
    MAX_SEEDS = 100
    # detail 里网盘条目上限
    MAX_PANS = 120

    # ------------------------------------------------------------------ 生命周期
    def __init__(self):
        self.base = self.base_hosts[0]
        self._sess = None
        self._home_cache = ("", 0.0)
        self._sidebar_cache = (None, 0.0)

    def getName(self):
        return self.site_name

    def init(self, extend=""):
        """extend 可传自定义域名（如 https://sidhub.cc）或 JSON：{"site": "..."}"""
        self.extend = extend
        try:
            if extend:
                host = ""
                ext = extend.strip()
                if ext.startswith("{"):
                    try:
                        host = str(json.loads(ext).get("site") or "").strip()
                    except Exception:
                        host = ""
                elif ext.startswith("http"):
                    host = ext
                if host:
                    self.base = host.rstrip("/")
                    if self.base not in self.base_hosts:
                        self.base_hosts = (self.base,) + self.base_hosts
        except Exception:
            pass
        self._sess = None
        return self

    def destroy(self):
        try:
            if self._sess is not None:
                self._sess.close()
        except Exception:
            pass
        self._sess = None

    # ------------------------------------------------------------------ HTTP
    def _session(self):
        if self._sess is None and requests is not None:
            s = requests.Session()
            s.headers.update(self.HEADERS)
            try:
                from requests.adapters import HTTPAdapter
                s.mount("https://", HTTPAdapter(max_retries=1))
                s.mount("http://", HTTPAdapter(max_retries=1))
            except Exception:
                pass
            self._sess = s
        return self._sess

    def _decode(self, raw, content_type=""):
        """按响应头 / meta charset / utf-8 / gb18030 顺序尽力解码"""
        if not raw:
            return ""
        enc = ""
        m = re.search(r"charset=[\"']?([\w\-]+)", content_type or "", re.I)
        if m:
            enc = m.group(1)
        if not enc:
            m = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", raw[:2048], re.I)
            if m:
                enc = m.group(1).decode("ascii", "ignore")
        for e in (enc, "utf-8", "gb18030"):
            if not e:
                continue
            try:
                return raw.decode(e)
            except Exception:
                continue
        return raw.decode("utf-8", "ignore")

    def _http(self, url, referer=None, timeout=12, retries=1):
        """单次 HTTP 抓取，失败返空串（不抛异常，保证 TVBox 侧不闪退）"""
        headers = dict(self.HEADERS)
        if referer:
            headers["Referer"] = referer
        last = None
        for _ in range(retries + 1):
            try:
                if requests is not None:
                    sess = self._session()
                    try:
                        r = sess.get(url, headers=headers, timeout=timeout,
                                     allow_redirects=True, verify=True)
                    except Exception as e:
                        # 证书问题降级再试一次
                        if "SSL" in type(e).__name__.upper() or "SSL" in str(e).upper():
                            try:
                                import urllib3
                                urllib3.disable_warnings()
                            except Exception:
                                pass
                            r = sess.get(url, headers=headers, timeout=timeout,
                                         allow_redirects=True, verify=False)
                        else:
                            raise
                    if r.status_code >= 400:
                        last = "HTTP %s" % r.status_code
                        # 403/404 不必重试
                        if r.status_code in (403, 404, 410):
                            return ""
                        continue
                    return self._decode(r.content,
                                        r.headers.get("Content-Type", ""))
                # ---- urllib 兜底 ----
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
            except Exception as e:
                last = str(e)
                time.sleep(0.15)
        return ""

    def _abs(self, path):
        if not path:
            return self.base + "/"
        if path.startswith("//"):
            return "https:" + path
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    def _fetch(self, path, referer=None, timeout=12):
        """抓取站内页面；主域名失败自动切镜像"""
        url = self._abs(path)
        host = ""
        try:
            host = "{0.scheme}://{0.netloc}".format(urlparse(url))
        except Exception:
            host = self.base

        text = self._http(url, referer or (self.base + "/"), timeout=timeout)
        if text:
            return text

        if host in self.base_hosts:
            u = urlparse(url)
            tail = u.path + (("?" + u.query) if u.query else "")
            for alt_host in self.base_hosts:
                if alt_host == host:
                    continue
                t = self._http(alt_host + tail, referer or (alt_host + "/"),
                               timeout=timeout)
                if t:
                    self.base = alt_host
                    return t
        return ""

    # ------------------------------------------------------------------ HTML 工具
    def _text(self, frag):
        """去标签取纯文本"""
        if not frag:
            return ""
        s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", frag)
        s = re.sub(r"(?is)<br\s*/?>", "\n", s)
        s = re.sub(r"(?is)</(p|li|div|h\d)>", "\n", s)
        s = re.sub(r"(?s)<[^>]+>", "", s)
        s = html_lib.unescape(s)
        s = s.replace("\u00a0", " ").replace("\u3000", " ")
        s = re.sub(r"[ \t\r\f\v]+", " ", s)
        s = re.sub(r"\n\s*\n+", "\n", s)
        return s.strip()

    def _attr(self, tag, name):
        m = re.search(r'%s\s*=\s*"([^"]*)"' % re.escape(name), tag or "", re.I)
        if not m:
            m = re.search(r"%s\s*=\s*'([^']*)'" % re.escape(name), tag or "", re.I)
        return html_lib.unescape(m.group(1)) if m else ""

    def _esc(self, s):
        """TVBox 播放列表分隔符转义：$ 分隔标题与地址，# 分隔集数"""
        return (s or "").replace("$", "＄").replace("#", "＃").replace("\n", " ").strip()

    # ------------------------------------------------------------------ 列表解析
    def _parse_covers(self, page):
        """解析列表页 .cover 卡片（首页 / 分类页 / 搜索页通用）"""
        out = []
        if not page:
            return out
        # 只取主内容区，避免侧栏 / 推荐区干扰
        start = page.find('<div class="cover-container">')
        if start > 0:
            page = page[start:]
        blocks = page.split('<div class="cover">')[1:]
        seen = set()
        for blk in blocks:
            blk = blk.split("</ul>")[0]
            m = re.search(r'href="(/movies/(\d+)/)"', blk)
            if not m:
                continue
            vid = m.group(2)
            if vid in seen:
                continue
            seen.add(vid)

            name = ""
            h2 = re.search(r"</a>\s*([^<]+?)\s*</h2>", blk, re.S)
            if h2:
                name = html_lib.unescape(h2.group(1)).strip()
            if not name:
                img_tag = re.search(r"<img[^>]*>", blk)
                if img_tag:
                    name = self._attr(img_tag.group(0), "alt")
            if not name:
                continue

            pic = ""
            img_tag = re.search(r"<img[^>]*>", blk)
            if img_tag:
                pic = self._attr(img_tag.group(0), "src")
            if pic.startswith("//"):
                pic = "https:" + pic

            year = area = lang = actor = ""
            meta_line = re.search(r"</h2></li>\s*<li>(.*?)</li>", blk, re.S)
            if meta_line:
                raw = self._text(meta_line.group(1))
                parts = [p.strip() for p in raw.split("/")]
                if len(parts) >= 4:
                    year, area, lang = parts[0], parts[2], parts[3]
                    actor = " / ".join(parts[4:]).strip(" /")
                elif len(parts) == 3:
                    year, area, lang = parts[0], parts[1], parts[2]
                elif parts:
                    year = parts[0]

            types = []
            tm = re.search(r"类型:\s*(.*?)</li>", blk, re.S)
            if tm:
                types = [html_lib.unescape(t) for t in
                         re.findall(r'title="([^"]+)"', tm.group(1))]
                if not types:
                    types = [self._text(tm.group(1))]

            score = ""
            sm = re.search(r"豆瓣评分:\s*<a[^>]*>(.*?)</a>", blk, re.S)
            if sm:
                score = self._text(sm.group(1))

            remarks = []
            if score and score not in ("0", "0.0"):
                remarks.append("豆瓣%s" % score)
            if types:
                remarks.append(types[0])

            out.append({
                "vod_id": vid,
                "vod_name": name,
                "vod_pic": pic,
                "vod_year": year,
                "vod_area": area,
                "vod_lang": lang,
                "vod_actor": actor,
                "vod_remarks": " · ".join(remarks),
            })
        return out

    def _parse_pagecount(self, page, pg):
        try:
            nums = [int(x) for x in re.findall(r'\?page=(\d+)', page or "")]
        except Exception:
            nums = []
        return max(nums) if nums else max(1, int(pg))

    # ------------------------------------------------------------------ 首页
    def _home_html(self):
        html, ts = self._home_cache
        if html and time.time() - ts < 600:
            return html
        html = self._fetch("/")
        if html:
            self._home_cache = (html, time.time())
        return html

    def _sidebar(self):
        """从首页侧栏解析 分类 + 各分类下的类型（作 filters 用），带缓存"""
        cats, types = self._sidebar_cache[0] or ([], {})
        if cats:
            return cats, types
        page = self._home_html()
        if not page:
            return [], {}
        start = page.find('<ul class="sidebar-links">')
        if start < 0:
            return [], {}
        end = page.find('<div class="page"', start)
        seg = page[start:end if end > 0 else len(page)]
        chunks = re.split(r'<p class="sidebar-heading', seg)[1:]
        for ch in chunks:
            m = re.search(r'href="/categories/(\d+)/movies/"[^>]*>([^<]+)</a>', ch)
            if not m:
                continue
            cid = m.group(1)
            cname = html_lib.unescape(m.group(2)).strip()
            tlist = [(t, html_lib.unescape(n).strip()) for t, n in
                     re.findall(r'href="/categories/\d+/types/(\d+)/movies/"[^>]*>([^<]+)</a>', ch)]
            if cid not in [c[0] for c in cats]:
                cats.append((cid, cname))
            if tlist:
                types[cid] = tlist
        if cats:
            self._sidebar_cache = ((cats, types), time.time())
        return cats, types

    def homeContent(self, filter=True):
        try:
            cats, types = self._sidebar()
            if not cats:
                cats = [("1", "电影"), ("2", "动漫"), ("3", "剧集")]
            classes = [{"type_id": cid, "type_name": cname} for cid, cname in cats]

            order_values = [{"n": n, "v": v} for v, n in self.ORDERS]
            filters = {}
            for cid, _ in cats:
                value = [{"n": "全部", "v": ""}]
                value += [{"n": n, "v": t} for t, n in types.get(cid, [])]
                filters[cid] = [
                    {"key": "类型", "name": "类型", "value": value},
                    {"key": "排序", "name": "排序", "value": order_values},
                ]
            return {"class": classes, "filters": filters}
        except Exception:
            return {"class": [{"type_id": "1", "type_name": "电影"},
                              {"type_id": "2", "type_name": "动漫"},
                              {"type_id": "3", "type_name": "剧集"}],
                    "filters": {}}

    def homeVideoContent(self):
        try:
            return {"list": self._parse_covers(self._home_html())}
        except Exception:
            return {"list": []}

    # ------------------------------------------------------------------ 分类
    def categoryContent(self, tid, pg, filter=True, extend=None):
        tid = str(tid or "").strip()
        try:
            page_no = max(1, int(str(pg or "1").strip() or 1))
        except Exception:
            page_no = 1

        ext = {}
        if isinstance(extend, dict):
            ext = extend
        elif isinstance(extend, str) and extend.strip().startswith("{"):
            try:
                ext = json.loads(extend)
            except Exception:
                ext = {}

        type_id = str(ext.get("类型", "") or "").strip()
        order = str(ext.get("排序", "update") or "update").strip()
        if order not in [v for v, _ in self.ORDERS]:
            order = "update"
        if not type_id.isdigit():
            type_id = ""

        if type_id:
            path = "/categories/%s/types/%s/movies/?page=%d&order=%s" % (
                tid, type_id, page_no, order)
        else:
            path = "/categories/%s/movies/?page=%d&order=%s" % (tid, page_no, order)

        page = self._fetch(path, referer=self.base + "/")
        if not page:
            return {"list": [], "page": page_no, "pagecount": page_no,
                    "limit": self.PAGE_SIZE, "total": 0}

        items = self._parse_covers(page)
        pagecount = self._parse_pagecount(page, page_no)
        if pagecount < page_no:
            pagecount = page_no
        # 已到最后一页但解析出 0 条，抬高 total 便于某些壳子判定结束
        total = pagecount * self.PAGE_SIZE if items else 0
        return {
            "list": items,
            "page": page_no,
            "pagecount": pagecount,
            "limit": self.PAGE_SIZE,
            "total": total,
        }

    # ------------------------------------------------------------------ 详情
    def _quality_tag(self, title):
        """从种子文件名推断画质，站点没标特征标签时兜底"""
        t = title or ""
        tags = []
        if re.search(r"2160p|\b4k\b|uhd", t, re.I):
            tags.append("4K")
        elif re.search(r"1080p", t, re.I):
            tags.append("1080P")
        elif re.search(r"720p", t, re.I):
            tags.append("720P")
        if re.search(r"remux", t, re.I):
            tags.append("Remux")
        elif re.search(r"blu-?ray|bdrip|蓝光", t, re.I):
            tags.append("蓝光")
        elif re.search(r"web-?dl|webrip", t, re.I):
            tags.append("WEB")
        return "·".join(tags[:2])

    def _ep_hint(self, title):
        """从资源标题里提取集数范围，便于用户在第二层判断这份资源能覆盖到第几集"""
        t = (title or "").strip()
        if not t:
            return ""
        # 更新至18集 / 更18集 / 全集29集 / 29集全
        m = re.search(r"(更新至|更新到|更新|更至|更|全集|全)\s*(\d{1,4})\s*集", t)
        if m:
            n = int(m.group(2))
            if n > 1:
                return ("全%d集" % n) if m.group(1).startswith("全") else ("更%d集" % n)
        m = re.search(r"(\d{1,4})\s*集全", t)
        if m:
            n = int(m.group(1))
            if n > 1:
                return "全%d集" % n
        # 第01-18集 / 1~18集
        m = re.search(r"第?\s*(\d{1,3})\s*[-~至]\s*(\d{1,3})\s*集", t)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if b > a >= 0:
                return "第%d-%d集" % (a, b)
        # S01E05
        m = re.search(r"S\d{1,2}E(\d{1,3})", t, re.I)
        if m:
            return "EP%s" % m.group(1)
        return ""

    def _clean_pan_title(self, title, limit=42):
        """清理网盘资源标题：去掉更新时间尾巴、压缩空白"""
        t = (title or "").strip()
        t = re.sub(r"\s*更新于.*$", "", t)
        t = re.sub(r"[\s　]*(刚刚|今天|昨天|前天|\d+\s*(?:秒|分钟|小时|天|周|月)前)\s*$",
                   "", t)
        t = re.sub(r"\s*提取码[:：].*$", "", t)
        t = re.sub(r"[ \t\u3000]{2,}", " ", t).strip(" -_|·")
        if len(t) > limit:
            t = t[:limit].rstrip() + "…"
        return t or "资源"

    def _parse_seeds(self, page):
        """磁力条目：/link_start/?seed_id=N → 播放时再换真磁力链"""
        out = []
        m = re.search(r'<ul class="seeds"[^>]*>(.*?)</ul>', page or "", re.S)
        if not m:
            return out
        for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S):
            hm = re.search(r'href="[^"]*seed_id=(\d+)', li)
            if not hm:
                continue
            seed_id = hm.group(1)
            title_tag = re.search(r"<a[^>]*>", li)
            title = self._attr(title_tag.group(0), "title") if title_tag else ""
            size = ""
            sm = re.search(r'class="size"[^>]*>([^<]*)<', li)
            if sm:
                size = html_lib.unescape(sm.group(1)).strip()
            feats = [html_lib.unescape(f).strip() for f in
                     re.findall(r'class="seed-feature"[^>]*>([^<]*)<', li)]
            ctime = ""
            cm = re.search(r'class="create-time"[^>]*>([^<]*)<', li)
            if cm:
                ctime = cm.group(1).strip()
            out.append({
                "seed_id": seed_id,
                "title": title,
                "size": size,
                "features": [f for f in feats if f],
                "time": ctime,
                "url": self._abs("/link_start/?seed_id=%s" % seed_id),
            })
            if len(out) >= self.MAX_SEEDS:
                break
        return out

    def _parse_pans(self, page):
        """网盘条目：/link_start/?redirect_to=pan_id_N → 播放时再换网盘直链"""
        out = []
        m = re.search(r'<ul class="pan-links"[^>]*>(.*?)</ul>', page or "", re.S)
        if not m:
            return out
        for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S):
            tag_m = re.search(r"<a[^>]*>", li)
            if not tag_m:
                continue
            tag = tag_m.group(0)
            href = self._attr(tag, "href")
            pid = ""
            pm = re.search(r"redirect_to=(pan_id_\d+)", href)
            if pm:
                pid = pm.group(1)
            if not pid:
                continue
            host = self._attr(tag, "data-link").lower()
            title = self._attr(tag, "title") or self._text(li)
            title = re.sub(r"\s*更新于.*$", "", title).strip()
            out.append({
                "pan_id": pid,
                "host": host,
                "title": title,
                "url": self._abs("/link_start/?redirect_to=%s" % pid),
            })
            if len(out) >= self.MAX_PANS:
                break
        return out

    def _parse_detail(self, page, vid):
        d = {
            "vod_id": vid, "name": "", "pic": "", "meta": {}, "desc": "",
            "seeds": [], "pans": [],
        }
        if not page:
            return d

        m = re.search(r'<h1 id="cover">(.*?)</h1>', page, re.S)
        if m:
            d["name"] = self._text(m.group(1)).lstrip("#").strip()

        m = re.search(r'<div class="cover-container">\s*<img[^>]*>', page)
        if m:
            d["pic"] = self._attr(m.group(0), "src")
            if d["pic"].startswith("//"):
                d["pic"] = "https:" + d["pic"]

        # 元信息（导演/编剧/主演/类型/制片国家/语言/首播/集数/又名/豆瓣评分…）
        start = page.find('<div class="cover-container">')
        if start > 0:
            end = page.find("</ul>", start)
            block = page[start:end if end > 0 else start + 6000]
            for li in re.findall(r"<li>(.*?)</li>", block, re.S):
                txt = self._text(li)
                if not txt:
                    continue
                parts = re.split(r"[:：]", txt, 1)
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip()
                    if k and k not in d["meta"]:
                        d["meta"][k] = v

        m = re.search(r'<h2 id="description">.*?</h2>\s*<p>(.*?)</p>', page, re.S)
        if m:
            d["desc"] = self._text(m.group(1))

        d["seeds"] = self._parse_seeds(page)
        d["pans"] = self._parse_pans(page)
        return d

    def detailContent(self, ids):
        try:
            raw = ids[0] if isinstance(ids, (list, tuple)) and ids else str(ids)
            m = re.search(r"(\d+)", str(raw))
            if not m:
                return {"list": []}
            vid = m.group(1)
            page = self._fetch("/movies/%s/" % vid, referer=self.base + "/")
            if not page:
                return {"list": []}

            d = self._parse_detail(page, vid)
            meta = d["meta"]

            name = d["name"] or ("影片 %s" % vid)
            year = ""
            for key in ("首播", "上映日期", "上映时间", "年份"):
                if meta.get(key):
                    ym = re.search(r"(19|20)\d{2}", meta[key])
                    if ym:
                        year = ym.group(0)
                        break

            remarks = []
            ep_num = 0
            if meta.get("集数"):
                ep = re.search(r"\d+", meta["集数"])
                if ep:
                    ep_num = int(ep.group(0))
            if ep_num > 1:
                remarks.append("全%d集" % ep_num)
            elif meta.get("单集片长"):
                remarks.append(meta["单集片长"])
            if meta.get("豆瓣评分") and meta["豆瓣评分"] not in ("0", "0.0"):
                remarks.append("豆瓣%s" % meta["豆瓣评分"])
            if not remarks and meta.get("类型"):
                remarks.append(meta["类型"])

            # 播放列表：两层结构
            #   第一层 vod_play_from = 资源类型（磁力下载 / 夸克 / 百度 / 迅雷 / UC）
            #   第二层 vod_play_url  = 该类型下的具体资源文件（每个都是整包，不是单集）
            froms, urls = [], []

            if d["seeds"]:
                eps = []
                for i, s in enumerate(d["seeds"]):
                    feats = "·".join(s["features"][:3]) or self._quality_tag(s["title"])
                    parts = [feats, s["size"]]
                    dm = re.search(r"-(\d{2})-(\d{2})", s["time"] or "")
                    if dm:
                        parts.append("%s-%s" % (dm.group(1), dm.group(2)))
                    hint = self._ep_hint(s["title"])
                    if hint:
                        parts.append(hint)
                    parts = [x for x in parts if x]
                    if not parts:
                        parts = [s["title"][:28] or "整包"]
                    label = "资源%02d｜%s" % (i + 1, "｜".join(parts))
                    eps.append("%s$%s" % (self._esc(label), s["url"]))
                froms.append("磁力下载(%d)" % len(d["seeds"]))
                urls.append("#".join(eps))

            if d["pans"]:
                groups, order = {}, []
                for p in d["pans"]:
                    host = p["host"] or ""
                    key = ""
                    for dom, cname in self.PAN_NAMES.items():
                        if host.endswith(dom):
                            key = cname
                            break
                    if not key:
                        key = host or "网盘"
                    if key not in groups:
                        groups[key] = []
                        order.append(key)
                    groups[key].append(p)
                for key in order:
                    eps, used = [], {}
                    for i, p in enumerate(groups[key]):
                        raw = p["title"] or "整包"
                        hint = self._ep_hint(raw)  # 从原始标题提，避免被截断后丢失
                        title = self._clean_pan_title(raw, limit=34 if hint else 38)
                        # 集数范围放在序号后面，扫一眼就知道这份资源更新到第几集
                        if hint:
                            title = "资源%02d｜%s｜%s" % (i + 1, hint, title)
                        else:
                            title = "资源%02d｜%s" % (i + 1, title)
                        n = used.get(title, 0) + 1
                        used[title] = n
                        if n > 1:
                            title = "%s (%d)" % (title, n)
                        eps.append("%s$%s" % (self._esc(title), p["url"]))
                    froms.append("%s(%d)" % (key, len(groups[key])))
                    urls.append("#".join(eps))

            # 无磁力也无网盘：留空，让壳子显示“暂无内容”，不塞空条目

            info_lines = []
            for k in ("资源更新于", "导演", "编剧", "主演", "类型",
                      "制片国家/地区", "语言", "首播", "集数", "单集片长",
                      "又名", "豆瓣评分", "常用标签"):
                if meta.get(k):
                    info_lines.append("%s: %s" % (k, meta[k]))

            content = d["desc"].strip()
            if info_lines:
                content = (content + "\n\n" if content else "") + "\n".join(info_lines)
            # 使用顺序放在最前面，第一眼就能看到“这是两层选择”
            guide = ("【使用顺序】\n"
                     "① 先选资源类型：磁力下载 / 夸克 / 百度 / 迅雷 / UC 等（播放器上方的线路）\n"
                     "② 再选具体资源文件：条目名形如「资源01｜4K蓝光｜17.36G｜更18集」，"
                     "每条都是一个整包，不是单集\n"
                     "③ 点进去后才进入播放 / 取链接环节\n"
                     "注：本站只提供整包磁力与网盘分享，没有单集直链，"
                     "因此第二层选的是“哪一份资源”，不是“第几集”。")
            content = guide + ("\n\n" + content if content else "")
            content += ("\n\n【提示】磁力 / 网盘需要配套工具：磁力用支持 BT 的播放器或下载器，"
                        "网盘需先转存到自己的网盘再播放。")

            vod = {
                "vod_id": vid,
                "vod_name": name,
                "vod_pic": d["pic"],
                "vod_year": year,
                "vod_area": meta.get("制片国家/地区", ""),
                "vod_lang": meta.get("语言", ""),
                "vod_actor": meta.get("主演", "").strip(" /"),
                "vod_director": meta.get("导演", ""),
                "vod_remarks": " · ".join(remarks),
                "vod_content": content.strip(),
                "vod_play_from": "$$$".join(froms),
                "vod_play_url": "$$$".join(urls),
            }
            return {"list": [vod]}
        except Exception:
            return {"list": []}

    # ------------------------------------------------------------------ 搜索
    def searchContent(self, key, quick=False, pg="1"):
        try:
            kw = (key or "").strip()
            if not kw:
                return {"list": []}
            kw = re.sub(r"[/\\?#]+", " ", kw)
            kw = quote(kw, safe="")
            try:
                page_no = max(1, int(str(pg or "1")))
            except Exception:
                page_no = 1
            path = "/s/%s/" % kw
            if page_no > 1:
                path += "?page=%d" % page_no
            page = self._fetch(path, referer=self.base + "/")
            if not page:
                return {"list": []}
            return {"list": self._parse_covers(page),
                    "page": page_no,
                    "pagecount": self._parse_pagecount(page, page_no)}
        except Exception:
            return {"list": []}

    # ------------------------------------------------------------------ 播放解析
    def _magnet_from_page(self, page):
        """从中转页取磁力链：支持 base64 内嵌（本站主用）、atob、明文"""
        if not page:
            return ""
        # 1) const data = "bWFnbmV0Oi..."
        for m in re.finditer(r'(?:const|let|var)\s+\w+\s*=\s*"([A-Za-z0-9+/=]{24,})"', page):
            try:
                dec = base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore")
            except Exception:
                continue
            if dec.lower().startswith("magnet:"):
                return dec.strip()
        # 2) atob("...")
        for m in re.finditer(r'atob\(\s*["\']([A-Za-z0-9+/=]{24,})["\']\s*\)', page):
            try:
                dec = base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore")
            except Exception:
                continue
            if dec.lower().startswith("magnet:"):
                return dec.strip()
        # 3) 明文磁力
        m = re.search(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{16,}[^\"'<>\s\\]*", page)
        if m:
            return html_lib.unescape(m.group(0))
        return ""

    def _pan_from_page(self, page):
        """从中转页取网盘直链（含提取码）"""
        if not page:
            return ""
        m = re.search(r'class="direct-pan"[^>]*>', page)
        if m:
            href = self._attr(m.group(0), "href")
            if href:
                return html_lib.unescape(href)
        m = re.search(r'<a[^>]+href="(https?://[^"]*(?:pan\.baidu\.com|pan\.quark\.cn|'
                      r'drive\.uc\.cn|pan\.xunlei\.com|alipan\.com|aliyundrive\.com)[^"]*)"',
                      page)
        return html_lib.unescape(m.group(1)) if m else ""

    def _is_media(self, url):
        return bool(url) and bool(self.MEDIA_RE.search(url))

    def _extract_media(self, page, base_url=""):
        """从页面 HTML / JS 中提取可播地址（m3u8 优先，其次 mp4 等）"""
        if not page:
            return ""
        text = page.replace("\\/", "/")
        text = text.replace("&amp;", "&")
        text = unquote(text)

        found = []

        def _push(u, kind):
            u = (u or "").strip().rstrip("),;]}>'\"、，。")
            if not u:
                return
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("http"):
                pass
            elif u.startswith("/"):
                u = urljoin(base_url or self.base, u)
            else:
                return
            if not self._is_media(u):
                return
            found.append((0 if ".m3u8" in u.lower() else 1, u))

        # 明文直链（含 JSON 中的 url 字段）
        for m in re.finditer(r'https?://[^\s"\'<>\\\u4e00-\u9fa5]+', text):
            _push(m.group(0), "raw")
        # 相对路径直链
        for m in re.finditer(r'["\'](/[^\s"\'<>]*?\.(?:m3u8|mp4)(?:\?[^"\']*)?)["\']',
                             text, re.I):
            _push(m.group(1), "rel")
        # base64 内嵌播放地址
        for m in re.finditer(r'(?:atob\(|base64,)\s*["\']?([A-Za-z0-9+/=]{24,})', text):
            try:
                dec = base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore")
            except Exception:
                continue
            for mm in re.finditer(r'https?://[^\s"\'<>\\]+', dec):
                _push(mm.group(0), "b64")

        if not found:
            return ""
        found.sort(key=lambda x: x[0])
        # 去重
        seen = set()
        for _, u in found:
            key = u.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            return u
        return ""

    def playerContent(self, flag, id, vipFlags=None):
        url = (id or "").strip()
        header = {
            "User-Agent": self.UA,
            "Referer": self.base + "/",
            "Accept": "*/*",
        }
        if not url:
            return {"parse": 0, "playUrl": "", "url": "", "header": header}

        low = url.lower()
        # 1) 已是磁力链
        if low.startswith("magnet:"):
            return {"parse": 0, "playUrl": "", "url": url, "header": {}}
        # 2) 已是可播直链
        if self._is_media(url):
            return {"parse": 0, "playUrl": "", "url": url, "header": header}

        page_url = self._abs(url)
        page = self._fetch(page_url, referer=self.base + "/")

        # 3) /link_start/ 中转页：优先磁力，其次网盘
        if "/link_start/" in page_url:
            magnet = self._magnet_from_page(page)
            if magnet:
                return {"parse": 0, "playUrl": "", "url": magnet, "header": {}}
            pan = self._pan_from_page(page)
            if pan:
                return {"parse": 0, "playUrl": "", "url": pan, "header": header}

        # 4) 通用：页面里找 m3u8 / mp4
        media = self._extract_media(page, page_url)
        if media:
            return {"parse": 0, "playUrl": "", "url": media, "header": header}

        # 5) 兜底：原样返回，交给壳子处理
        return {"parse": 0, "playUrl": "", "url": page_url, "header": header}

    # ------------------------------------------------------------------ 其他
    def isVideoFormat(self, url):
        return self._is_media(url)

    def manualVideoCheck(self):
        return False
