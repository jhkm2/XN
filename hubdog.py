# -*- coding: utf-8 -*-
# =============================================================================
#  TVBox Python 爬虫源 —— SeedHub 网盘版（三级目录）
#  ---------------------------------------------------------------------------
#  站点名称 : SeedHub（页面标题：SeedHub | 影视&动漫分享）
#  主域名   : https://hubdog.cc/   镜像：https://sidhub.cc/
#
#  目录结构（对应客户端的「网盘」模块）：
#      影片列表（vod_tag=folder）      vod_id = m:<影片ID>
#        └─ 网盘类型（folder）          vod_id = p:<影片ID>:<分组序号>
#             └─ 分享文件（file）       vod_id = s:<影片ID>:<分组序号>:<条目序号>
#                  └─ 详情：线路 = 网盘类型，播放项 = 网盘分享链接
#                       └─ 播放：把分享链接原样交给宿主（客户端解析夸克原画/普画）
#
#  说明：本站只有「整包磁力 + 网盘分享」，没有单集直链。分享里的文件清单由
#       客户端的网盘模块自己展开，本源只负责把“哪个网盘、哪条分享”给对。
# =============================================================================

import base64
import gzip
import html as html_lib
import re
import ssl
import time
import urllib.request
from urllib.parse import quote, urlparse

try:  # TVBox 运行时提供 base.spider
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider(object):
        def init(self, extend=""):
            pass

try:
    import requests
except Exception:
    requests = None


class Spider(BaseSpider):

    # ------------------------------------------------------------------ 配置
    site_name = "SeedHub"

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
    }

    # 首页分类：type_id -> (名称, 列表路径模板)
    CLASSES = (
        ("rec", "推荐", None),
        ("1", "电影", "/categories/1/movies/?page=%d&order=update"),
        ("3", "电视剧", "/categories/3/movies/?page=%d&order=update"),
        ("2", "动漫", "/categories/2/movies/?page=%d&order=update"),
    )

    ORDERS = (("update", "最近更新"), ("view", "近期热门"),
              ("date", "上映时间"), ("score", "豆瓣评分"))

    PAN_NAMES = {
        "pan.quark.cn": "夸克网盘",
        "pan.baidu.com": "百度网盘",
        "pan.xunlei.com": "迅雷网盘",
        "drive.uc.cn": "UC网盘",
        "alipan.com": "阿里云盘",
        "aliyundrive.com": "阿里云盘",
        "www.aliyundrive.com": "阿里云盘",
        "drive.115.com": "115网盘",
        "caiyun.139.com": "移动云盘",
        "cloud.189.cn": "天翼云盘",
        "pan.123pan.com": "123网盘",
    }

    PAN_ORDER = ("夸克网盘", "百度网盘", "迅雷网盘", "UC网盘", "阿里云盘",
                 "115网盘", "移动云盘", "天翼云盘", "123网盘")

    PAGE_SIZE = 20
    MAX_SEEDS = 120
    MAX_PANS = 160

    # ------------------------------------------------------------------ 生命周期
    def __init__(self):
        self.base = self.base_hosts[0]
        self._sess = None
        self._home_cache = ("", 0.0)
        self._movie_cache = {}
        self._link_cache = {}

    def getName(self):
        return self.site_name

    def init(self, extend=""):
        self.extend = extend
        try:
            if extend and str(extend).strip().startswith("http"):
                self.base = str(extend).strip().rstrip("/")
        except Exception:
            pass
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

    def _http(self, url, referer=None, timeout=12):
        headers = dict(self.HEADERS)
        if referer:
            headers["Referer"] = referer
        try:
            if requests is not None:
                s = self._session()
                try:
                    r = s.get(url, headers=headers, timeout=timeout, allow_redirects=True)
                except Exception as e:
                    if "SSL" in type(e).__name__.upper() or "SSL" in str(e).upper():
                        try:
                            import urllib3
                            urllib3.disable_warnings()
                        except Exception:
                            pass
                        r = s.get(url, headers=headers, timeout=timeout,
                                  allow_redirects=True, verify=False)
                    else:
                        raise
                if r.status_code >= 400:
                    return ""
                return self._decode(r.content, r.headers.get("Content-Type", ""))
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
        url = self._abs(path)
        text = self._http(url, referer or (self.base + "/"), timeout)
        if text:
            return text
        try:
            u = urlparse(url)
            host = "%s://%s" % (u.scheme, u.netloc)
        except Exception:
            return ""
        if host in self.base_hosts:
            u = urlparse(url)
            tail = u.path + (("?" + u.query) if u.query else "")
            for alt in self.base_hosts:
                if alt == host:
                    continue
                t = self._http(alt + tail, referer or (alt + "/"), timeout)
                if t:
                    self.base = alt
                    return t
        return ""

    # ------------------------------------------------------------------ HTML 工具
    def _text(self, frag):
        if not frag:
            return ""
        s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", frag)
        s = re.sub(r"(?is)<br\s*/?>", "\n", s)
        s = re.sub(r"(?s)<[^>]+>", "", s)
        s = html_lib.unescape(s).replace("\u00a0", " ").replace("\u3000", " ")
        s = re.sub(r"[ \t\r\f\v]+", " ", s)
        s = re.sub(r"\n\s*\n+", "\n", s)
        return s.strip()

    def _attr(self, tag, name):
        m = re.search(r'%s\s*=\s*"([^"]*)"' % re.escape(name), tag or "", re.I)
        return html_lib.unescape(m.group(1)) if m else ""

    def _esc(self, s):
        return (s or "").replace("$", "＄").replace("#", "＃").replace("\n", " ").strip()

    def _clean(self, s, limit=46):
        t = self._text(s)
        t = re.sub(r"\s*更新于.*$", "", t)
        t = re.sub(r"[\s　]*(刚刚|今天|昨天|前天|\d+\s*(?:秒|分钟|小时|天|周|月)前)\s*$", "", t)
        t = re.sub(r"\s*提取码[:：].*$", "", t)
        t = re.sub(r"\s+", " ", t).strip(" -_|·")
        return t[:limit].rstrip() if len(t) > limit else t

    # ------------------------------------------------------------------ 列表解析
    def _parse_covers(self, page):
        out = []
        if not page:
            return out
        start = page.find('<div class="cover-container">')
        if start > 0:
            page = page[start:]
        seen = set()
        for blk in page.split('<div class="cover">')[1:]:
            blk = blk.split("</ul>")[0]
            m = re.search(r'href="(/movies/(\d+)/)"', blk)
            if not m:
                continue
            mid = m.group(2)
            if mid in seen:
                continue
            seen.add(mid)

            name = ""
            h2 = re.search(r"</a>\s*([^<]+?)\s*</h2>", blk, re.S)
            if h2:
                name = html_lib.unescape(h2.group(1)).strip()
            img = re.search(r"<img[^>]*>", blk)
            if not name and img:
                name = self._attr(img.group(0), "alt")
            if not name:
                continue

            pic = self._attr(img.group(0), "src") if img else ""
            if pic.startswith("//"):
                pic = "https:" + pic

            year = area = actor = score = ""
            meta = re.search(r"</h2></li>\s*<li>(.*?)</li>", blk, re.S)
            if meta:
                parts = [p.strip() for p in self._text(meta.group(1)).split("/")]
                if len(parts) >= 4:
                    year, area = parts[0], parts[2]
                    actor = " / ".join(parts[4:]).strip(" /")
                elif parts:
                    year = parts[0]
            sm = re.search(r"豆瓣评分:\s*<a[^>]*>(.*?)</a>", blk, re.S)
            if sm:
                score = self._text(sm.group(1))

            remarks = "%s / 豆瓣 %s" % (year or "—", score or "0")
            out.append({"id": mid, "name": name, "pic": pic, "year": year,
                        "area": area, "actor": actor, "score": score,
                        "remarks": remarks})
        return out

    def _parse_movie(self, page):
        d = {"name": "", "pic": "", "meta": {}, "desc": "", "seeds": [], "pans": []}
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

        start = page.find('<div class="cover-container">')
        if start > 0:
            end = page.find("</ul>", start)
            block = page[start:end if end > 0 else start + 6000]
            for li in re.findall(r"<li>(.*?)</li>", block, re.S):
                txt = self._text(li)
                parts = re.split(r"[:：]", txt, 1)
                if len(parts) == 2 and parts[0].strip() and parts[0].strip() not in d["meta"]:
                    d["meta"][parts[0].strip()] = parts[1].strip()

        m = re.search(r'<h2 id="description">.*?</h2>\s*<p>(.*?)</p>', page, re.S)
        if m:
            d["desc"] = self._text(m.group(1))

        m = re.search(r'<ul class="seeds"[^>]*>(.*?)</ul>', page, re.S)
        if m:
            for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S)[:self.MAX_SEEDS]:
                hm = re.search(r'seed_id=(\d+)', li)
                if not hm:
                    continue
                tag = re.search(r"<a[^>]*>", li)
                title = self._attr(tag.group(0), "title") if tag else ""
                size = ""
                sm = re.search(r'class="size"[^>]*>([^<]*)<', li)
                if sm:
                    size = sm.group(1).strip()
                feats = [f.strip() for f in
                         re.findall(r'class="seed-feature"[^>]*>([^<]*)<', li) if f.strip()]
                d["seeds"].append({"seed_id": hm.group(1), "title": title,
                                   "size": size, "features": feats})

        m = re.search(r'<ul class="pan-links"[^>]*>(.*?)</ul>', page, re.S)
        if m:
            for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S)[:self.MAX_PANS]:
                tag_m = re.search(r"<a[^>]*>", li)
                if not tag_m:
                    continue
                tag = tag_m.group(0)
                href = self._attr(tag, "href")
                pm = re.search(r"redirect_to=(pan_id_\d+)", href)
                if not pm:
                    continue
                host = self._attr(tag, "data-link").lower()
                d["pans"].append({
                    "pan_id": pm.group(1),
                    "host": host,
                    "pan": self._pan_name(host),
                    "title": self._clean(self._attr(tag, "title") or self._text(li), 60),
                })
        return d

    def _pan_name(self, host):
        h = (host or "").lower()
        for dom, name in self.PAN_NAMES.items():
            if h.endswith(dom):
                return name
        return h or "网盘"

    def _seed_label(self, seed, idx):
        parts = []
        if seed["features"]:
            parts.append("·".join(seed["features"][:2]))
        if seed["size"]:
            parts.append(seed["size"])
        head = " ".join(parts)
        title = self._clean(seed["title"], 44)
        if title:
            return "[%02d]%s %s" % (idx, (" " + head + " |") if head else "", title)
        return "[%02d] %s" % (idx, head or "整包")

    # ------------------------------------------------------------------ 缓存
    def _movie(self, mid):
        hit = self._movie_cache.get(mid)
        if hit and time.time() - hit[1] < 300:
            return hit[0]
        page = self._fetch("/movies/%s/" % mid, self.base + "/")
        movie = self._parse_movie(page)
        if movie.get("name"):
            self._movie_cache[mid] = (movie, time.time())
        return movie

    def _groups(self, movie):
        """[(类型名, [条目...])]，磁力固定第一组"""
        groups = []
        if movie.get("seeds"):
            groups.append(("磁力", movie["seeds"]))
        buckets = {}
        for p in movie.get("pans") or []:
            buckets.setdefault(p["pan"], []).append(p)
        order = [n for n in self.PAN_ORDER if n in buckets]
        order += [n for n in buckets if n not in order]
        for name in order:
            groups.append((name, buckets[name]))
        return groups

    # ------------------------------------------------------------------ 链接解析
    def _extract_link(self, page):
        """从中转页取真实链接：磁力(base64 内嵌/明文) 或 网盘直达链接"""
        if not page:
            return ""
        url = ""
        # 磁力：const data = "base64"
        for m in re.finditer(r'(?:const|let|var)\s+\w+\s*=\s*"([A-Za-z0-9+/=]{24,})"', page):
            try:
                dec = base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore")
            except Exception:
                continue
            if dec.lower().startswith("magnet:"):
                url = dec.strip()
                break
        if not url:
            m = re.search(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{16,}[^\"'<>\s\\]*", page)
            if m:
                url = html_lib.unescape(m.group(0))
        # 网盘：直达链接（含提取码）
        if not url:
            m = re.search(r'class="direct-pan"[^>]*>', page)
            if m:
                url = self._attr(m.group(0), "href")
            if not url:
                m = re.search(r'href="(https?://[^"]*(?:pan\.baidu\.com|pan\.quark\.cn|'
                              r'drive\.uc\.cn|pan\.xunlei\.com|alipan\.com|'
                              r'aliyundrive\.com)[^"]*)"', page)
                if m:
                    url = html_lib.unescape(m.group(1))
        return url

    def _link_start(self, query):
        hit = self._link_cache.get(query)
        if hit and time.time() - hit[1] < 600:
            return hit[0]
        page = self._fetch("/link_start/?" + query, self.base + "/")
        url = self._extract_link(page)
        if url:
            self._link_cache[query] = (url, time.time())
        return url

    # ------------------------------------------------------------------ 卡片
    def _movie_card(self, item):
        return {
            "vod_id": "m:%s" % item["id"],
            "vod_name": item["name"],
            "vod_pic": item["pic"],
            "vod_tag": "folder",
            "vod_remarks": item["remarks"],
        }

    def _type_cards(self, mid, movie):
        cards = []
        for gi, (pan, items) in enumerate(self._groups(movie)):
            cards.append({
                "vod_id": "p:%s:%d" % (mid, gi),
                "vod_name": movie.get("name") or "",
                "vod_pic": movie.get("pic") or "",
                "vod_tag": "folder",
                "vod_remarks": "[%s] %d个链接" % (pan, len(items)),
            })
        return cards

    def _share_cards(self, mid, movie, gi):
        groups = self._groups(movie)
        if gi < 0 or gi >= len(groups):
            return []
        pan, items = groups[gi]
        cards = []
        for ii, it in enumerate(items):
            if "seed_id" in it:
                label = self._seed_label(it, ii + 1)
            else:
                label = it.get("title") or ("%s #%d" % (pan, ii + 1))
            cards.append({
                "vod_id": "s:%s:%d:%d" % (mid, gi, ii),
                "vod_name": label,
                "vod_pic": movie.get("pic") or "",
                "vod_tag": "file",
                "vod_remarks": "[%s] #%d" % (pan, ii + 1),
            })
        return cards

    # ------------------------------------------------------------------ 首页 / 分类
    def _home_html(self):
        html, ts = self._home_cache
        if html and time.time() - ts < 600:
            return html
        html = self._fetch("/")
        if html:
            self._home_cache = (html, time.time())
        return html

    def homeContent(self, filter=True):
        classes = [{"type_id": k, "type_name": n} for k, n, _ in self.CLASSES]
        try:
            order_values = [{"n": n, "v": v} for v, n in self.ORDERS]
            filters = {}
            for k, _, tpl in self.CLASSES:
                if not tpl:
                    continue
                filters[k] = [
                    {"key": "排序", "name": "排序", "value": order_values},
                ]
            return {"class": classes, "filters": filters}
        except Exception:
            return {"class": classes, "filters": {}}

    def homeVideoContent(self):
        try:
            return {"list": [self._movie_card(i) for i in self._parse_covers(self._home_html())]}
        except Exception:
            return {"list": []}

    def categoryContent(self, tid, pg, filter=True, extend=None):
        tid = str(tid or "").strip()
        try:
            page = max(1, int(str(pg or "1")))
        except Exception:
            page = 1

        try:
            # 三级目录：影片 → 网盘类型 → 分享文件
            if tid.startswith("m:") or tid.startswith("p:"):
                if page > 1:
                    return {"list": [], "page": page, "pagecount": 1, "limit": 30, "total": 0}
                parts = tid.split(":")
                mid = parts[1] if len(parts) > 1 else ""
                movie = self._movie(mid)
                if not movie.get("name"):
                    return {"list": [], "page": 1, "pagecount": 1, "limit": 30, "total": 0}
                if tid.startswith("m:"):
                    cards = self._type_cards(mid, movie)
                else:
                    gi = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                    cards = self._share_cards(mid, movie, gi)
                return {"list": cards, "page": 1, "pagecount": 1,
                        "limit": max(len(cards), 30), "total": len(cards)}

            # 普通分类
            if tid == "rec":
                if page > 1:
                    return {"list": [], "page": page, "pagecount": 1, "limit": 20, "total": 20}
                path = "/"
            else:
                tpl = None
                for k, _, t in self.CLASSES:
                    if k == tid:
                        tpl = t
                        break
                if not tpl:
                    return {"list": [], "page": page, "pagecount": 0, "total": 0}
                ext = extend if isinstance(extend, dict) else {}
                order = str(ext.get("排序") or "update")
                if order not in [v for v, _ in self.ORDERS]:
                    order = "update"
                path = (tpl % page).replace("order=update", "order=" + order)

            html = self._fetch(path, self.base + "/")
            items = self._parse_covers(html)
            pages = [int(x) for x in re.findall(r"\?page=(\d+)", html or "")] or [1]
            pagecount = max(pages)
            return {"list": [self._movie_card(i) for i in items], "page": page,
                    "pagecount": pagecount, "limit": self.PAGE_SIZE,
                    "total": pagecount * self.PAGE_SIZE}
        except Exception:
            return {"list": [], "page": page, "pagecount": 0, "total": 0}

    # ------------------------------------------------------------------ 详情
    def detailContent(self, ids):
        try:
            raw = ids[0] if isinstance(ids, (list, tuple)) and ids else str(ids)
            raw = str(raw).strip()
            parts = raw.split(":")

            # 目录节点被误当详情请求：回一份可继续点的空壳
            if raw.startswith("m:") or raw.startswith("p:"):
                mid = parts[1] if len(parts) > 1 else ""
                movie = self._movie(mid)
                return {"list": [{
                    "vod_id": raw,
                    "vod_name": movie.get("name") or "SeedHub",
                    "vod_pic": movie.get("pic") or "",
                    "vod_tag": "folder",
                    "vod_content": "请返回上一级选择网盘类型 / 分享文件。",
                }]}

            if not raw.startswith("s:") or len(parts) < 4:
                return {"list": []}

            mid = parts[1]
            gi = int(parts[2]) if parts[2].isdigit() else 0
            ii = int(parts[3]) if parts[3].isdigit() else 0

            movie = self._movie(mid)
            groups = self._groups(movie)
            if gi >= len(groups):
                return {"list": []}
            pan, items = groups[gi]
            if ii >= len(items):
                return {"list": []}
            it = items[ii]

            if "seed_id" in it:
                url = self._link_start("seed_id=%s" % it["seed_id"])
                line = "磁力"
                label = self._seed_label(it, ii + 1)
            else:
                url = self._link_start("redirect_to=%s" % it["pan_id"])
                line = pan
                label = it.get("title") or ("%s #%d" % (pan, ii + 1))

            meta = movie.get("meta") or {}
            year = ""
            for k in ("首播", "上映日期", "上映时间", "年份"):
                if meta.get(k):
                    ym = re.search(r"(19|20)\d{2}", meta[k])
                    if ym:
                        year = ym.group(0)
                        break

            content = movie.get("desc") or ""
            tip = ("【说明】本站为磁力 / 网盘资源索引，当前选中：%s（%s）\n"
                   "网盘分享里的文件清单由客户端网盘模块自动展开。") % (pan, label)
            content = (content + "\n\n" if content else "") + tip

            vod = {
                "vod_id": raw,
                "vod_name": movie.get("name") or label,
                "vod_pic": movie.get("pic") or "",
                "vod_year": year,
                "vod_area": meta.get("制片国家/地区", ""),
                "vod_lang": meta.get("语言", ""),
                "vod_actor": meta.get("主演", "").strip(" /"),
                "vod_director": meta.get("导演", ""),
                "vod_remarks": "%s · 共%d个分享" % (pan, len(items)),
                "vod_content": content,
                "vod_tag": "file",
            }
            if url:
                vod["vod_play_from"] = line
                vod["vod_play_url"] = "%s$%s" % (self._esc(label or pan), url)
            return {"list": [vod]}
        except Exception:
            return {"list": []}

    # ------------------------------------------------------------------ 搜索
    def searchContent(self, key, quick=False, pg="1"):
        try:
            kw = (key or "").strip()
            if not kw:
                return {"list": []}
            kw = quote(re.sub(r"[/\\?#]+", " ", kw), safe="")
            try:
                page = max(1, int(str(pg or "1")))
            except Exception:
                page = 1
            path = "/s/%s/" % kw + (("?page=%d" % page) if page > 1 else "")
            html = self._fetch(path, self.base + "/")
            items = self._parse_covers(html)
            pages = [int(x) for x in re.findall(r"\?page=(\d+)", html or "")] or [1]
            return {"list": [self._movie_card(i) for i in items], "page": page,
                    "pagecount": max(pages), "limit": self.PAGE_SIZE, "total": len(items)}
        except Exception:
            return {"list": []}

    # ------------------------------------------------------------------ 播放
    def playerContent(self, flag, id, vipFlags=None):
        url = (id or "").strip()
        header = {"User-Agent": self.UA, "Referer": self.base + "/"}
        if not url:
            return {"parse": 0, "playUrl": "", "url": "", "header": header}

        low = url.lower()
        # 网盘分享链接 / 磁力链：原样交给宿主，由客户端网盘模块解析画质
        if low.startswith(("http://", "https://", "magnet:", "ed2k:", "thunder:")):
            return {"parse": 0, "playUrl": "", "url": url,
                    "header": ({} if low.startswith("magnet:") else header)}

        # 兜底：站内中转页，解析成真实链接
        page_url = self._abs(url)
        page = self._fetch(page_url, self.base + "/")
        link = self._extract_link(page)
        if link:
            return {"parse": 0, "playUrl": "", "url": link,
                    "header": ({} if link.startswith("magnet:") else header)}
        return {"parse": 0, "playUrl": "", "url": page_url, "header": header}

    # ------------------------------------------------------------------ 其他
    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        return False
