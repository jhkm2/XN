# -*- coding: utf-8 -*-

import base64
import gzip
import html as html_lib
import json
import re
import ssl
import time
from urllib.parse import quote, unquote, urljoin, urlparse

try:  # TVBox / Atvp 运行时提供 base.spider
    from base.spider import Spider as BaseSpider
except Exception:  # 本地自测兜底
    class BaseSpider(object):
        def init(self, extend=""):
            pass

try:
    import requests
except Exception:
    requests = None

try:
    from concurrent.futures import ThreadPoolExecutor
except Exception:
    ThreadPoolExecutor = None

import urllib.error
import urllib.request


class Spider(BaseSpider):

    # ------------------------------------------------------------------ 配置
    site_name = "SeedHub"

    # ★ T4/alist-tvbox 必填：网盘线路交给后端挂载解析
    backend_parse = True

    # 磁力线路在 TVBox 客户端播不了，默认不输出
    ENABLE_MAGNET_LINES = False

    # 网盘链接预解析的并发/超时/上限（detail 会因此变慢，别调太大）
    PAN_RESOLVE_WORKERS = 6
    PAN_RESOLVE_TIMEOUT = 8
    PAN_RESOLVE_MAX = 40
    PAN_CACHE_TTL = 6 * 3600

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

    ORDERS = (
        ("update", "最近更新"),
        ("view", "近期热门"),
        ("date", "上映时间"),
        ("score", "豆瓣评分"),
    )

    # ★ 本站真实分类（页面上只有 /categories/1|2|3/movies/）
    REAL_CATEGORIES = (("1", "电影"), ("2", "动漫"), ("3", "剧集"))
    TID_ALIASES = {
        "": "", "rec": "", "recommend": "", "home": "",
        "1": "1", "mv": "1", "movie": "1", "film": "1", "dy": "1", "电影": "1",
        "2": "2", "ac": "2", "anime": "2", "dongman": "2", "动漫": "2",
        "3": "3", "tv": "3", "tvshow": "3", "series": "3", "剧集": "3", "电视剧": "3",
    }

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

    PAGE_SIZE = 20
    MAX_SEEDS = 100
    MAX_PANS = 120

    # ------------------------------------------------------------------ 生命周期
    def __init__(self):
        self.base = self.base_hosts[0]
        self._sess = None
        self._home_cache = ("", 0.0)
        self._sidebar_cache = (None, 0.0)
        self._pan_cache = {}

    def getName(self):
        return self.site_name

    def init(self, extend=""):
        """extend 可能是 JSON 字符串（Atvp 传 {"token":...}）或自定义域名"""
        self.extend = extend
        try:
            if extend:
                host = ""
                ext = extend if isinstance(extend, str) else str(extend)
                ext = ext.strip()
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
        headers = dict(self.HEADERS)
        if referer:
            headers["Referer"] = referer
        for _ in range(retries + 1):
            try:
                if requests is not None:
                    sess = self._session()
                    try:
                        r = sess.get(url, headers=headers, timeout=timeout,
                                     allow_redirects=True, verify=True)
                    except Exception as e:
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
                        if r.status_code in (403, 404, 410):
                            return ""
                        continue
                    return self._decode(r.content,
                                        r.headers.get("Content-Type", ""))
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
        url = self._abs(path)
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
        return (s or "").replace("$", "＄").replace("#", "＃").replace("\n", " ").strip()

    # ------------------------------------------------------------------ 列表解析
    def _parse_covers(self, page):
        out = []
        if not page:
            return out
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
        """★ 改动 2：带上 list，否则 T4 首页是空白（T4 只调 homeContent）"""
        try:
            cats, types = self._sidebar()
            if not cats:
                cats = list(self.REAL_CATEGORIES)
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
            return {
                "class": classes,
                "filters": filters,
                "list": self._parse_covers(self._home_html()),
            }
        except Exception:
            return {"class": [{"type_id": cid, "type_name": cname}
                              for cid, cname in self.REAL_CATEGORIES],
                    "filters": {}, "list": []}

    def homeVideoContent(self):
        try:
            return {"list": self._parse_covers(self._home_html())}
        except Exception:
            return {"list": []}

    # ------------------------------------------------------------------ 分类
    def _norm_tid(self, tid):
        """★ 改动 3：数字 / slug(rec,mv,tv,ac) / atvp_detail:xxx / 整条路径 → 真实分类 id"""
        t = str(tid or "").strip()
        for pre in ("atvp_detail:", "atvp_group:", "atvp_resume:"):
            if t.startswith(pre):
                t = t[len(pre):]
        m = re.search(r"/categories/(\d+)/", t)
        if m:
            return m.group(1)
        low = t.lower()
        if low in self.TID_ALIASES:
            return self.TID_ALIASES[low]
        m = re.search(r"\d+", t)
        return m.group(0) if m else ""

    def categoryContent(self, tid, pg, filter=True, extend=None):
        tid_raw = str(tid or "").strip()
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

        tid = self._norm_tid(tid_raw)
        type_id = str(ext.get("类型", "") or "").strip()
        order = str(ext.get("排序", "update") or "update").strip()
        if order not in [v for v, _ in self.ORDERS]:
            order = "update"
        if not type_id.isdigit():
            type_id = ""

        # 「推荐」类 tid（rec/空）：直接用首页列表，避免拼出 404 路径
        if not tid:
            items = self._parse_covers(self._home_html()) if page_no == 1 else []
            return {"list": items, "page": page_no,
                    "pagecount": 1 if page_no == 1 else page_no,
                    "limit": self.PAGE_SIZE, "total": len(items)}

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
        total = pagecount * self.PAGE_SIZE if items else 0
        return {
            "list": items,
            "page": page_no,
            "pagecount": pagecount,
            "limit": self.PAGE_SIZE,
            "total": total,
        }

    # ------------------------------------------------------------------ 详情
    def _clean_pan_title(self, title, limit=42):
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
        d = {"vod_id": vid, "name": "", "pic": "", "meta": {}, "desc": "",
             "seeds": [], "pans": []}
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

    def _pan_share(self, pan):
        """★ 改动 4：把 /link_start/?redirect_to=pan_id_N 换成真实分享链接（带缓存）"""
        pid = pan.get("pan_id") or ""
        if not pid:
            return ""
        hit = self._pan_cache.get(pid)
        if hit and time.time() - hit[1] < self.PAN_CACHE_TTL:
            return hit[0]
        page = self._fetch(pan.get("url") or "", referer=self.base + "/",
                           timeout=self.PAN_RESOLVE_TIMEOUT)
        share = self._pan_from_page(page)
        if not share:
            return ""
        self._pan_cache[pid] = (share, time.time())
        return share

    def _pan_name(self, host):
        for dom, cname in self.PAN_NAMES.items():
            if host.endswith(dom):
                return cname
        return host or "网盘"

    def _build_groups(self, d):
        """把资源整理成 Atvp 的 group 结构：[{name, media:[{name,url}]}]"""
        groups, order = [], []

        if self.ENABLE_MAGNET_LINES and d["seeds"]:
            media = []
            for i, s in enumerate(d["seeds"]):
                label = " ".join([x for x in ("·".join(s["features"][:3]), s["size"]) if x])
                label = label or (s["title"][:30] or ("资源%d" % (i + 1)))
                media.append({"name": "%02d. %s" % (i + 1, label), "url": s["url"]})
            groups.append({"name": "磁力下载", "media": media})
            order.append("磁力下载")

        pans = d["pans"][:self.PAN_RESOLVE_MAX]
        if pants := pans:
            # 并发预解析，把中转页换成真实分享链接（Atvp 的 /parse 只认分享链接）
            shares = [""] * len(pants)
            if ThreadPoolExecutor is not None and len(pants) > 1:
                with ThreadPoolExecutor(max_workers=self.PAN_RESOLVE_WORKERS) as ex:
                    for idx, url in enumerate(ex.map(self._pan_share, pants)):
                        shares[idx] = url
            else:
                shares = [self._pan_share(p) for p in pants]

            buckets, names = {}, []
            for p, share in zip(pants, shares):
                if not share:
                    continue
                key = self._pan_name(p.get("host") or "")
                if key not in buckets:
                    buckets[key] = []
                    names.append(key)
                title = self._clean_pan_title(p.get("title") or "资源")
                used = [m["name"] for m in buckets[key]]
                n = 2
                base_title = title
                while title in used:
                    title = "%s (%d)" % (base_title, n)
                    n += 1
                buckets[key].append({"name": title, "url": share})

            for name in names:
                if buckets[name]:
                    groups.append({"name": name, "media": buckets[name]})
                    order.append(name)
        return groups, order

    def detailContent(self, ids):
        try:
            raw = ids[0] if isinstance(ids, (list, tuple)) and ids else str(ids)
            raw = str(raw)
            for pre in ("atvp_detail:", "atvp_group:"):
                if raw.startswith(pre):
                    raw = raw[len(pre):]
            m = re.search(r"(\d+)", raw)
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

            remarks, ep_num = [], 0
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

            groups, group_names = self._build_groups(d)

            info_lines = []
            for k in ("资源更新于", "导演", "编剧", "主演", "类型",
                      "制片国家/地区", "语言", "首播", "集数", "单集片长",
                      "又名", "豆瓣评分", "常用标签"):
                if meta.get(k):
                    info_lines.append("%s: %s" % (k, meta[k]))

            content = d["desc"].strip()
            if info_lines:
                content = (content + "\n\n" if content else "") + "\n".join(info_lines)
            content += ("\n\n【提示】本站为磁力 / 网盘资源索引站，网盘线路由 alist-tvbox "
                        "自动挂载，需先在「网盘账户」里登录对应网盘。")

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
            }

            # ★ group 优先：Atvp 会按组生成「夸克网盘 (3)」这样的二级目录
            if groups:
                vod["group"] = groups
                # 非 Atvp 宿主（纯 T4）没有 group 概念，用每组的首条兜底
                vod["vod_play_from"] = "$$$".join(group_names)
                vod["vod_play_url"] = "$$$".join(
                    "%s$%s" % (self._esc(g["media"][0]["name"]), g["media"][0]["url"])
                    for g in groups if g.get("media"))
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
        if not page:
            return ""
        for m in re.finditer(r'(?:const|let|var)\s+\w+\s*=\s*"([A-Za-z0-9+/=]{24,})"', page):
            try:
                dec = base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore")
            except Exception:
                continue
            if dec.lower().startswith("magnet:"):
                return dec.strip()
        for m in re.finditer(r'atob\(\s*["\']([A-Za-z0-9+/=]{24,})["\']\s*\)', page):
            try:
                dec = base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore")
            except Exception:
                continue
            if dec.lower().startswith("magnet:"):
                return dec.strip()
        m = re.search(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{16,}[^\"'<>\s\\]*", page)
        if m:
            return html_lib.unescape(m.group(0))
        return ""

    def _pan_from_page(self, page):
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
        if not page:
            return ""
        text = page.replace("\\/", "/").replace("&amp;", "&")
        text = unquote(text)
        found = []

        def _push(u):
            u = (u or "").strip().rstrip("),;]}>'\",，。")
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

        for m in re.finditer(r'https?://[^\s"\'<>\\\u4e00-\u9fa5]+', text):
            _push(m.group(0))
        for m in re.finditer(r'["\'](/[^\s"\'<>]*?\.(?:m3u8|mp4)(?:\?[^"\']*)?)["\']',
                             text, re.I):
            _push(m.group(1))
        for m in re.finditer(r'(?:atob\(|base64,)\s*["\']?([A-Za-z0-9+/=]{24,})', text):
            try:
                dec = base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore")
            except Exception:
                continue
            for mm in re.finditer(r'https?://[^\s"\'<>\\]+', dec):
                _push(mm.group(0))

        if not found:
            return ""
        found.sort(key=lambda x: x[0])
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
        header = {"User-Agent": self.UA, "Referer": self.base + "/", "Accept": "*/*"}
        if not url:
            return {"parse": 0, "playUrl": "", "url": "", "header": header}

        low = url.lower()
        if low.startswith("magnet:"):
            return {"parse": 0, "playUrl": "", "url": url, "header": {}}
        if self._is_media(url):
            return {"parse": 0, "playUrl": "", "url": url, "header": header}

        page_url = self._abs(url)
        page = self._fetch(page_url, referer=self.base + "/")

        if "/link_start/" in page_url:
            magnet = self._magnet_from_page(page)
            if magnet:
                return {"parse": 0, "playUrl": "", "url": magnet, "header": {}}
            pan = self._pan_from_page(page)
            if pan:
                return {"parse": 0, "playUrl": "", "url": pan, "header": header}

        media = self._extract_media(page, page_url)
        if media:
            return {"parse": 0, "playUrl": "", "url": media, "header": header}

        return {"parse": 0, "playUrl": "", "url": page_url, "header": header}

    # ------------------------------------------------------------------ 其他
    def isVideoFormat(self, url):
        return self._is_media(url)

    def manualVideoCheck(self):
        return False
