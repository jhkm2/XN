# coding=utf-8
import json
import re
import sys
from urllib.parse import quote

from base.spider import Spider as BaseSpider

PLUGIN_ID = "e94d755c027c456992bb47dad7b1833b711d"

sys.path.append("..")


class Spider(BaseSpider):
    # alist-tvbox 的 Atvp.py 兼容层的“目录化后端解析”开关（作者插件文档明确：返回网盘/磁力资源时为必填）。
    # 置 True 后：分类/搜索结果会被包装成目录入口；详情的 vod["group"] 分组会被 Atvp 拆成
    # “二级目录”（zxzj 一个入口、百度等网盘各一个入口，见 _detail_from_target 的组构建）：
    #   · 点“zxzj”入口 → 本源按播放页 id 返回完整分集（见 detailContent）；
    #   · 点“百度”入口 → Atvp 调宿主 /parse/{token} 把分享展开成真正的分集，
    #     分集播放 id（形如 1@pid@folder@file）再由 Atvp 调 /play/{token} 换成真实播放地址。
    # 不开这个开关：网盘线路只能透传分享链接，播放器里只会看到一个点不开的“合集”占位。
    backend_parse = True

    def __init__(self):
        self.name = "在线之家[盘]"
        self.host = "https://www.zxzj.run"
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Referer": self.host + "/",
        }
        self.extend = {}
        # 宿主 alist-tvbox 注入的地址与订阅 token（用来把网盘分享解析成分集）
        self.atv_api = ""
        self.atv_token = ""
        self.classes = [
            {"type_id": "1", "type_name": "电影"},
            {"type_id": "2", "type_name": "美剧"},
            {"type_id": "3", "type_name": "韩剧"},
            {"type_id": "4", "type_name": "日剧"},
            {"type_id": "5", "type_name": "泰剧"},
            {"type_id": "6", "type_name": "动漫"},
        ]
        sort_values = [
            {"n": "时间", "v": "time"},
            {"n": "人气", "v": "hits"},
            {"n": "评分", "v": "score"},
        ]
        common_years = [
            {"n": "全部", "v": ""},
            {"n": "2026", "v": "2026"},
            {"n": "2025", "v": "2025"},
            {"n": "2024", "v": "2024"},
            {"n": "2023", "v": "2023"},
            {"n": "2022", "v": "2022"},
            {"n": "2021", "v": "2021"},
            {"n": "2020", "v": "2020"},
            {"n": "2019", "v": "2019"},
            {"n": "2018", "v": "2018"},
            {"n": "2017", "v": "2017"},
            {"n": "2016", "v": "2016"},
            {"n": "2015", "v": "2015"},
            {"n": "2014", "v": "2014"},
            {"n": "2013", "v": "2013"},
            {"n": "2012", "v": "2012"},
            {"n": "2011", "v": "2011"},
        ]
        drama_classes = [
            {"n": "全部", "v": ""},
            {"n": "剧情", "v": "剧情"},
            {"n": "喜剧", "v": "喜剧"},
            {"n": "爱情", "v": "爱情"},
            {"n": "动作", "v": "动作"},
            {"n": "悬疑", "v": "悬疑"},
            {"n": "恐怖", "v": "恐怖"},
            {"n": "奇幻", "v": "奇幻"},
            {"n": "惊悚", "v": "惊悚"},
            {"n": "犯罪", "v": "犯罪"},
            {"n": "科幻", "v": "科幻"},
            {"n": "音乐", "v": "音乐"},
            {"n": "其他", "v": "其他"},
        ]
        self.filter_def = {key: {"cateId": key} for key in ["1", "2", "3", "4", "5", "6"]}
        self.filters = {
            "1": [
                {
                    "key": "class",
                    "name": "剧情",
                    "init": "",
                    "value": [
                        {"n": "全部", "v": ""},
                        {"n": "喜剧", "v": "喜剧"},
                        {"n": "爱情", "v": "爱情"},
                        {"n": "恐怖", "v": "恐怖"},
                        {"n": "动作", "v": "动作"},
                        {"n": "科幻", "v": "科幻"},
                        {"n": "剧情", "v": "剧情"},
                        {"n": "战争", "v": "战争"},
                        {"n": "警匪", "v": "警匪"},
                        {"n": "犯罪", "v": "犯罪"},
                        {"n": "动画", "v": "动画"},
                        {"n": "奇幻", "v": "奇幻"},
                        {"n": "冒险", "v": "冒险"},
                    ],
                },
                {
                    "key": "area",
                    "name": "地区",
                    "init": "",
                    "value": [
                        {"n": "全部", "v": ""},
                        {"n": "大陆", "v": "大陆"},
                        {"n": "香港", "v": "香港"},
                        {"n": "台湾", "v": "台湾"},
                        {"n": "欧美", "v": "欧美"},
                        {"n": "韩国", "v": "韩国"},
                        {"n": "日本", "v": "日本"},
                        {"n": "泰国", "v": "泰国"},
                        {"n": "印度", "v": "印度"},
                        {"n": "俄罗斯", "v": "俄罗斯"},
                        {"n": "其他", "v": "其他"},
                    ],
                },
                {"key": "year", "name": "年份", "init": "", "value": list(common_years)},
                {"key": "by", "name": "排序", "init": "time", "value": list(sort_values)},
            ],
            "2": [
                {"key": "class", "name": "剧情", "init": "", "value": list(drama_classes)},
                {"key": "year", "name": "年份", "init": "", "value": list(common_years)},
                {"key": "by", "name": "排序", "init": "time", "value": list(sort_values)},
            ],
            "3": [
                {"key": "class", "name": "剧情", "init": "", "value": list(drama_classes)},
                {"key": "year", "name": "年份", "init": "", "value": list(common_years)},
                {"key": "by", "name": "排序", "init": "time", "value": list(sort_values)},
            ],
            "4": [
                {"key": "class", "name": "剧情", "init": "", "value": list(drama_classes)},
                {"key": "year", "name": "年份", "init": "", "value": list(common_years)},
                {"key": "by", "name": "排序", "init": "time", "value": list(sort_values)},
            ],
            "5": [
                {"key": "year", "name": "年份", "init": "", "value": list(common_years)},
                {"key": "by", "name": "排序", "init": "time", "value": list(sort_values)},
            ],
            "6": [
                {
                    "key": "class",
                    "name": "剧情",
                    "init": "",
                    "value": [
                        {"n": "全部", "v": ""},
                        {"n": "情感", "v": "情感"},
                        {"n": "科幻", "v": "科幻"},
                        {"n": "热血", "v": "热血"},
                        {"n": "推理", "v": "推理"},
                        {"n": "搞笑", "v": "搞笑"},
                        {"n": "冒险", "v": "冒险"},
                        {"n": "萝莉", "v": "萝莉"},
                        {"n": "校园", "v": "校园"},
                        {"n": "动作", "v": "动作"},
                        {"n": "机战", "v": "机战"},
                        {"n": "运动", "v": "运动"},
                        {"n": "战争", "v": "战争"},
                        {"n": "少年", "v": "少年"},
                    ],
                },
                {
                    "key": "area",
                    "name": "地区",
                    "init": "",
                    "value": [
                        {"n": "全部", "v": ""},
                        {"n": "国产", "v": "国产"},
                        {"n": "日本", "v": "日本"},
                        {"n": "欧美", "v": "欧美"},
                        {"n": "其他", "v": "其他"},
                    ],
                },
                {"key": "year", "name": "年份", "init": "", "value": list(common_years)},
                {"key": "by", "name": "排序", "init": "time", "value": list(sort_values)},
            ],
        }

    def init(self, extend=""):
        """宿主加载源时调用。

        两种运行形态：
        1) 新版 alist-tvbox 把本源经 Atvp.py 包装层加载（ext: loader=Atvp.py、
           raw=true）。此时交给本类的 extend 只有 {"token":..., "local_proxy_config":...}，
           **拿不到宿主 api 地址**——网盘线路由 Atvp 的 backend_parse 目录模式展开，
           本源只需如实输出分享链接（见类注释）。
        2) 直接加载的宿主（旧版 PyProxy 等）ext 是完整 base64 JSON，含 api/token，
           此时本源自己调 /parse、/play 把网盘分享展开成分集（_atv_* 系列方法）。
        另外兼容 {"host": "https://..."} 覆盖站点地址。
        """
        ext = self._normalize_ext(extend)
        if not ext and isinstance(extend, str) and extend.strip():
            # 兜底：部分宿主把 ext 以 base64 直接透传
            try:
                import base64 as _b64

                raw = extend.strip()
                raw += "=" * (-len(raw) % 4)
                ext = self._normalize_ext(_b64.b64decode(raw).decode("utf-8", "ignore"))
            except Exception:
                ext = {}
        self.extend = ext or {}
        self.atv_api = str(self.extend.get("api") or self.extend.get("atv_api") or "").rstrip("/")
        self.atv_token = str(self.extend.get("token") or self.extend.get("atv_token") or "")
        host = str(self.extend.get("host") or "").strip()
        if host.startswith("http"):
            self.host = host.rstrip("/")
            self.headers["Referer"] = self.host + "/"
        return None

    def setExtendInfo(self, extend):
        self.init(extend)

    def getName(self):
        return self.name

    def isVideoFormat(self, url):
        text = str(url or "").strip().lower().split("?")[0]
        return text.endswith((".m3u8", ".mp4", ".mkv", ".flv", ".avi", ".ts", ".m4v", ".mov", ".rmvb", ".webm"))

    def manualVideoCheck(self):
        return False

    def danmaku(self):
        return True

    def localProxy(self, param):
        # FongMi 的 proxy:// 本地代理回调，本源不需要，返回 None 即可
        return None

    def destroy(self):
        return None

    def homeContent(self, filter):
        return {"class": self.classes, "filters": self.filters}

    def homeVideoContent(self):
        return {"list": []}

    def _normalize_ext(self, extend):
        if isinstance(extend, dict):
            return extend
        if not extend:
            return {}
        try:
            return json.loads(str(extend))
        except Exception:
            return {}

    def _build_url(self, value):
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw.startswith(("http://", "https://")):
            return raw
        if raw.startswith("//"):
            return "https:" + raw
        if raw.startswith("/"):
            return self.host + raw
        return self.host + "/" + raw

    def _to_relative_play_id(self, value):
        """把站内播放地址归一成相对路径（play/xxx-1-1.html）。

        Atvp 二级目录会取线路第一条的地址作为目录项 id：相对路径不会被
        Atvp._decode_parse 当成网盘/磁力链接去调 /parse，点“zxzj”条目时
        会回落到本源详情页（见 detailContent 对播放页 id 的处理）；
        绝对 http 地址则会被误判成分享链接。播放时 playerContent 仍按相对路径拼回完整地址。
        """
        text = str(value or "").strip()
        if text.startswith(self.host):
            text = text[len(self.host):]
        return re.sub(r"^/+", "", text)

    def _build_category_url(self, tid, pg, extend):
        values = dict(self.filter_def.get(str(tid), {"cateId": str(tid)}))
        values.update(self._normalize_ext(extend))
        path = (
            f"{values.get('cateId', tid)}-"
            f"{values.get('area', '')}-"
            f"{values.get('by', '')}-"
            f"{values.get('class', '')}"
            f"-----{int(pg)}---"
            f"{values.get('year', '')}"
        )
        return self._build_url(f"/vodshow/{path}.html")

    def _clean_text(self, text):
        return re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip()

    def _fix_json_wrapped_html(self, html):
        value = str(html or "").strip()
        if value.startswith("<"):
            return value
        if value.startswith('"') and value.endswith('"'):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, str) and parsed.startswith("<"):
                    return parsed.strip()
            except Exception:
                return value
        return value

    def _request_html(self, path_or_url, referer=None, extra_headers=None, retries=2):
        # 站点偶发 5xx/空响应（同一地址重试即可拿到），关键解析流程需要重试
        target = path_or_url if str(path_or_url).startswith("http") else self._build_url(path_or_url)
        headers = dict(self.headers)
        headers["Referer"] = referer or self.headers["Referer"]
        if isinstance(extra_headers, dict):
            headers.update(extra_headers)
        import time as _time
        for attempt in range(max(int(retries), 1)):
            try:
                response = self.fetch(target, headers=headers, timeout=10)
            except Exception:
                response = None
            if response is not None and response.status_code == 200:
                text = self._fix_json_wrapped_html(response.text or "")
                if text:
                    return text
            if attempt < retries - 1:
                _time.sleep(0.6)
        return ""

    def _build_player_v2_headers(self):
        return {
            "Origin": self.host,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Sec-Fetch-Dest": "iframe",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _extract_vod_id(self, href):
        raw = str(href or "").strip()
        matched = re.search(r"/voddetail/(\d+)\.html", raw)
        return matched.group(1) if matched else ""

    def _build_detail_url(self, vod_id):
        return self._build_url(f"/voddetail/{str(vod_id or '').strip()}.html")

    def _parse_cards(self, html):
        root = self.html(html)
        if root is None:
            return []
        items = []
        seen = set()
        for node in root.xpath("//ul[contains(@class,'stui-vodlist')]//li"):
            href = ((node.xpath(".//a[@href][1]/@href") or [""])[0]).strip()
            vod_id = self._extract_vod_id(href)
            title = ((node.xpath(".//a[@title][1]/@title") or [""])[0]).strip()
            pic = (
                ((node.xpath(".//a[@data-original][1]/@data-original") or [""])[0]).strip()
                or ((node.xpath(".//img[@data-original][1]/@data-original") or [""])[0]).strip()
                or ((node.xpath(".//img[@src][1]/@src") or [""])[0]).strip()
            )
            remarks = self._clean_text("".join(node.xpath(".//*[contains(@class,'pic-text')][1]//text()")))
            if not vod_id or not title or vod_id in seen:
                continue
            seen.add(vod_id)
            items.append(
                {
                    "vod_id": vod_id,
                    "vod_name": title,
                    "vod_pic": self._build_url(pic),
                    "vod_remarks": remarks,
                }
            )
        return items

    def categoryContent(self, tid, pg, filter, extend):
        page = int(pg)
        items = self._parse_cards(self._request_html(self._build_category_url(tid, pg, extend)))
        return {"list": items, "page": page, "pagecount": page + 1 if items else page, "limit": 24, "total": page * 30 + len(items)}

    def searchContent(self, key, quick, pg="1"):
        page = int(pg)
        url = self._build_url("/vodsearch/{0}-------------.html".format(quote(str(key or "").strip())))
        items = self._parse_cards(self._request_html(url))
        return {"list": items, "page": page, "pagecount": page + 1 if items else page, "limit": len(items), "total": len(items)}

    def _extract_actor_like_field(self, html, label):
        matched = re.search(rf"{label}：</span>([\s\S]*?)</p>", str(html or ""))
        if not matched:
            return ""
        texts = re.findall(r">([^<>]+)<", matched.group(1))
        clean = [self._clean_text(text) for text in texts if self._clean_text(text)]
        return ",".join(clean)

    def _parse_detail_meta(self, html):
        root = self.html(html)
        if root is None:
            return {}
        title_line = self._clean_text(
            "".join(root.xpath("//*[contains(@class,'stui-content__detail')]//*[contains(@class,'title')][1]//text()"))
        )
        title_match = re.match(r"^(.*?)(?:\s+(19\d{2}|20\d{2}))?(?:\s+([^\s]+))?(?:\s+([^\s]+))?$", title_line)
        vod_name = self._clean_text(title_match.group(1) if title_match else title_line)
        vod_year = self._clean_text(title_match.group(2) if title_match else "")
        vod_area = self._clean_text(title_match.group(3) if title_match else "")
        vod_class = self._clean_text(title_match.group(4) if title_match else "")
        return {
            "vod_name": vod_name,
            "vod_year": vod_year,
            "vod_area": vod_area,
            "vod_class": vod_class,
            "vod_pic": self._build_url(
                ((root.xpath("//*[contains(@class,'stui-content__thumb')]//img/@data-original") or [""])[0]).strip()
            ),
            "vod_content": self._clean_text(
                "".join(
                    root.xpath(
                        "//*[contains(concat(' ', normalize-space(@class), ' '), ' detail ')][1]//text()"
                    )
                )
            ),
            "vod_director": self._extract_actor_like_field(html, "导演"),
            "vod_actor": self._extract_actor_like_field(html, "主演"),
        }

    def _extract_playlists(self, html):
        root = self.html(html)
        if root is None:
            return {"normal": [], "pan": []}
        tabs = [self._clean_text("".join(node.xpath(".//text()"))) for node in root.xpath("//*[contains(@class,'stui-vodlist__head')]//h3")]
        playlists = root.xpath("//*[contains(@class,'stui-content__playlist')]")
        normal = []
        pan = []
        for index, playlist in enumerate(playlists):
            tab_name = tabs[index] if index < len(tabs) else ""
            items = []
            for anchor in playlist.xpath(".//a[@href]"):
                href = ((anchor.xpath("./@href") or [""])[0]).strip()
                name = self._clean_text("".join(anchor.xpath(".//text()"))) or "正片"
                play_id = re.sub(r"^/+", "", href)
                if not play_id:
                    continue
                items.append(
                    {
                        "name": name,
                        "url": self._build_url(play_id),
                        "play_id": play_id,
                        "tab_name": tab_name,
                    }
                )
            if any(keyword in tab_name for keyword in ["网盘", "百度", "夸克", "UC", "阿里", "迅雷"]):
                pan.extend(items)
            elif items:
                # 正常线路统一输出相对播放路径：Atvp 二级目录取第一条地址做目录项 id 时，
                # 相对路径会走本源详情页而不是被误当分享链接（见 _to_relative_play_id）
                normal.extend([f"{item['name']}${self._to_relative_play_id(item['play_id'])}" for item in items])
        return {"normal": normal, "pan": pan}

    def _extract_player_payload(self, html):
        """稳妥提取 `player_xxx = {...}` 的 JSON 对象。

        站点的 player JSON 内嵌了 vod_data 等子对象，旧版用非贪婪正则
        `\\{[\\s\\S]*?\\}` 会在第一个 `}` 处提前截断，导致 json.loads 抛错、
        网盘分享链接被静默丢弃（播放线路里看不到网盘源）。
        这里改为按大括号配平截取（并跳过字符串内部的括号/转义），以正确取到
        url / from / link 等字段。
        """
        text = str(html or "")
        matched = re.search(r"player_[a-zA-Z0-9_]+\s*=\s*(\{)", text)
        if not matched:
            return {}
        start = matched.start(1)
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        payload = json.loads(text[start : index + 1])
                    except Exception:
                        return {}
                    return payload if isinstance(payload, dict) else {}
        return {}

    def _extract_pan_url_from_play_page(self, html):
        payload = self._extract_player_payload(html)
        return str(payload.get("url") or "").strip()

    def _detect_pan_type(self, tab_name, share_url):
        text = str(share_url or "")
        if "pan.baidu.com" in text:
            return "baidu"
        if "pan.quark.cn" in text:
            return "quark"
        if "drive.uc.cn" in text or "uc.cn" in text:
            return "uc"
        if "alipan.com" in text or "aliyundrive.com" in text or "pan.aliyun.com" in text:
            return "aliyun"
        if "pan.xunlei.com" in text or "xunlei.com" in text:
            return "xunlei"
        if "123pan.com" in text or "123pan.cn" in text or "123684.com" in text or "123865.com" in text or "123912.com" in text or "123592.com" in text:
            return "123"
        if "yun.139.com" in text or "mcloud.139.com" in text or "139.com" in text:
            return "mobile"
        if "115.com" in text or "115cdn.com" in text or "anxia.com" in text:
            return "115"
        if "cloud.189.cn" in text or "189.cn" in text or "21cn.com" in text:
            return "tianyi"
        name = str(tab_name or "").lower()
        if "百度" in str(tab_name or ""):
            return "baidu"
        if "夸克" in str(tab_name or ""):
            return "quark"
        if "uc" in name:
            return "uc"
        if "阿里" in str(tab_name or "") or "aliyun" in name:
            return "aliyun"
        if "迅雷" in str(tab_name or ""):
            return "xunlei"
        if "123" in name:
            return "123"
        if "移动" in str(tab_name or "") or "139" in name:
            return "mobile"
        if "115" in name:
            return "115"
        if "天翼" in str(tab_name or "") or "189" in name:
            return "tianyi"
        return ""

    def _extract_pan_groups(self, items):
        grouped = {}
        seen = set()
        order = ["quark", "baidu", "uc", "aliyun", "xunlei", "123", "mobile", "115", "tianyi"]
        for item in items:
            play_html = self._request_html(item["url"], referer=self.headers["Referer"])
            share_url = self._extract_pan_url_from_play_page(play_html)
            pan_type = self._detect_pan_type(item.get("tab_name", ""), share_url)
            if not share_url or not pan_type:
                continue
            key = (pan_type, share_url)
            if key in seen:
                continue
            seen.add(key)
            grouped.setdefault(pan_type, []).append(f"{item['name']}${share_url}")
        return [{"from": key, "urls": "#".join(grouped[key])} for key in order if grouped.get(key)]

    # ---------- 宿主 alist-tvbox 集成 ----------
    # 形态 1（新版，Atvp.py 包装层）：网盘分集由包装层调宿主 /parse、/play 展开，
    #   本源只负责输出分享链接与 vod["group"] 分组（见 _detail_from_target）；
    # 形态 2（旧版/直接加载，ext 含 api/token）：本源自己调宿主接口展开分集。
    # 下面的 _atv_* 方法服务于形态 2；形态 1 下 _atv_ready() 为 False 时自动跳过。

    def _atv_ready(self):
        return bool(self.atv_api and self.atv_token)

    def _atv_http(self, method, url, body=None, params=None):
        """调宿主接口。优先 requests（alist-tvbox 插件运行时提供），回退 self.fetch/self.post。"""
        headers = {"User-Agent": self.headers.get("User-Agent", ""), "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            import requests

            response = requests.request(method, url, json=body, params=params, headers=headers, timeout=20)
            code = getattr(response, "status_code", 0) or 0
            try:
                return code, response.json()
            except Exception:
                return code, None
        except Exception:
            pass
        if params:
            from urllib.parse import urlencode

            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}{urlencode(params)}"
        try:
            if str(method).upper() == "POST" and hasattr(self, "post"):
                response = self.post(url, data=json.dumps(body or {}), headers=headers, timeout=20)
            else:
                response = self.fetch(url, headers=headers, timeout=20)
        except Exception:
            return 0, None
        code = getattr(response, "status_code", 0) or 0
        text = getattr(response, "text", "") or ""
        try:
            return code, json.loads(text)
        except Exception:
            return code, None

    def _atv_parse_share(self, share_url, title=""):
        """POST /parse/{token}?ac=play：让宿主挂载网盘分享并返回标准 TvBox 详情（含全部分集）。

        ac=play 与 Atvp.py 兼容层同款拼法，保证深度/线路命名行为一致。
        """
        if not self._atv_ready() or not share_url:
            return None
        code, data = self._atv_http(
            "POST",
            f"{self.atv_api}/parse/{self.atv_token}",
            {"url": share_url, "title": str(title or "")},
            params={"ac": "play"},
        )
        return data if (code == 200 and isinstance(data, dict)) else None

    def _atv_resolve_play(self, play_id):
        """GET /play/{token}?id=<集id>：把宿主的集 id 换成真实播放地址（含 header）。"""
        if not self._atv_ready() or not play_id:
            return None
        code, data = self._atv_http(
            "GET",
            f"{self.atv_api}/play/{self.atv_token}?id={quote(str(play_id), safe='')}",
        )
        if code != 200 or not isinstance(data, dict) or not data.get("url"):
            return None
        url = str(data["url"])
        if url.startswith("/"):
            url = self.atv_api + url
        result = {"url": url}
        if isinstance(data.get("header"), dict):
            result["header"] = data["header"]
        return result

    def _looks_like_atv_id(self, value):
        """判断是否是宿主解析出来的集 id（形如 1@12345 / 1@12345@a@b / 1$/path）。"""
        text = str(value or "").strip()
        if not text or text.startswith("http"):
            return False
        if text.startswith("1$"):
            return True
        return "@" in text and text.split("@", 1)[0].isdigit()

    def _merge_atv_lines(self, detail, fallback_name, play_from, play_url):
        """把宿主详情里的网盘线路并入播放线路；单条时用网盘名，多条保留宿主命名。"""
        items = detail.get("list") if isinstance(detail, dict) else None
        if not items or not isinstance(items[0], dict):
            return False
        vod = items[0]
        names = [x.strip() for x in str(vod.get("vod_play_from") or "").split("$$$")]
        lines = str(vod.get("vod_play_url") or "").split("$$$")
        merged = []
        for index, name in enumerate(names):
            line = lines[index] if index < len(lines) else ""
            if name and line.strip():
                merged.append((name, line))
        if not merged:
            return False
        if len(merged) == 1 and fallback_name:
            merged[0] = (fallback_name, merged[0][1])
        for name, line in merged:
            play_from.append(name)
            play_url.append(line)
        return True

    def _resolve_detail_target(self, raw_id):
        """识别 Atvp 二级目录/历史记录回传的详情目标，返回 (target, is_play_page)。

        - 播放页地址（play/152434-1-1.html 或其绝对地址）→ (vod_id, True)：
          点二级目录里“zxzj”条目时回传的就是它，此时只返回站点线路详情。
        - 详情页地址 / 纯 vod_id → (原值, False)：完整详情（含网盘线路，
          供 Atvp 拆分二级目录、复制元数据与续播定位使用）。
        """
        text = str(raw_id or "").strip()
        if not text:
            return "", False
        path = re.sub(r"^https?://[^/]+", "", text)
        matched = re.search(r"(?:^|/)play/(\d+)", path)
        if matched:
            return matched.group(1), True
        matched = re.search(r"(?:^|/)voddetail/(\d+)", path)
        if matched:
            return matched.group(1), False
        return text, False

    def detailContent(self, ids):
        raw_id = str(ids[0]).strip()
        # Atvp 兼容：个别宿主会把目录项 id 原样带回（如 atvp_detail:152434），先剥前缀
        if raw_id.startswith("atvp_detail:"):
            raw_id = raw_id[len("atvp_detail:"):].strip()
        target, is_play_page = self._resolve_detail_target(raw_id)
        # 播放页 id 来自二级目录的“zxzj”条目：只返回站点线路详情；网盘线路留给
        # 二级目录里的“百度/夸克”条目（由 Atvp 调宿主 /parse 展开成分集），
        # 避免详情页里再出现点不开的网盘占位项
        return self._detail_from_target(target, raw_id, include_pan=not is_play_page)

    def _detail_from_target(self, target, response_id, include_pan):
        url = target if str(target).startswith("http") else self._build_detail_url(target)
        html = self._request_html(url, retries=3)
        meta = self._parse_detail_meta(html)
        playlists = self._extract_playlists(html)
        if not playlists["normal"] and not playlists["pan"] and html:
            # 详情页偶发返回残缺内容，再补拉一次
            html = self._request_html(url, retries=3)
            playlists = self._extract_playlists(html)
        pan_names = {
            "baidu": "百度",
            "quark": "夸克",
            "uc": "UC",
            "aliyun": "阿里",
            "xunlei": "迅雷",
            "123": "123",
            "mobile": "移动",
            "115": "115",
            "tianyi": "天翼",
            "pikpak": "PikPak",
        }
        play_from = []
        play_url = []
        # Atvp 二级目录的分组结构（backend_parse 模式读取）：
        # 每条线路一个分组；组内 media 是"资源"——zxzj 收敛成单条"正片"（避免把整集列表
        # 塞进目录项 id），网盘分享逐条列全。普通 TVBox 客户端忽略该字段，仍看 vod_play_from/url。
        groups = []
        if playlists["normal"]:
            play_from.append("zxzj")
            play_url.append("#".join(playlists["normal"]))
            first_name, _, first_path = playlists["normal"][0].partition("$")
            first_url = self._to_relative_play_id(first_path or first_name)
            if first_url:
                groups.append({"name": "zxzj", "media": [{"name": "正片", "url": first_url}]})
        if include_pan:
            for group in self._extract_pan_groups(playlists["pan"]):
                line_name = pan_names.get(group["from"], group["from"])
                media = []
                for entry in str(group.get("urls") or "").split("#"):
                    item_name, separator, item_url = entry.partition("$")
                    item_url = str(item_url or item_name).strip()
                    if not item_url:
                        continue
                    media.append({"name": (str(item_name).strip() if separator else "") or "资源", "url": item_url})
                if media:
                    groups.append({"name": line_name, "media": media})
                share_url = media[0]["url"] if media else ""
                # 形态 2（ext 含 api/token 的直接加载宿主）：本源自己把分享交给宿主
                # /parse 展开成真分集；形态 1（Atvp 包装）拿不到 api，这里必然跳过，
                # 由 Atvp 的 backend_parse 目录模式在用户点开网盘分组时展开
                expanded = self._atv_parse_share(share_url, meta.get("vod_name", "")) if share_url else None
                if expanded and self._merge_atv_lines(expanded, line_name, play_from, play_url):
                    continue
                play_from.append(line_name)
                play_url.append(group["urls"])
        vod = {
            "vod_id": response_id,
            "vod_name": meta.get("vod_name", ""),
            "vod_pic": meta.get("vod_pic", ""),
            "vod_content": meta.get("vod_content", ""),
            "vod_remarks": "",
            "vod_year": meta.get("vod_year", ""),
            "vod_area": meta.get("vod_area", ""),
            "vod_class": meta.get("vod_class", ""),
            "vod_lang": "",
            "vod_director": meta.get("vod_director", ""),
            "vod_actor": meta.get("vod_actor", ""),
            "vod_play_from": "$$$".join(play_from),
            "vod_play_url": "$$$".join(play_url),
        }
        if groups:
            # Atvp 兼容层在 backend_parse 模式据此生成“二级目录”（vod_play_from/url 仍照旧下发，
            # 普通客户端历史记录等场景不受影响）
            vod["group"] = groups
        return {"list": [vod]}

    def _decrypt_url(self, encrypted_data):
        raw = str(encrypted_data or "").strip()
        if not raw or len(raw) % 2 != 0:
            return ""
        try:
            reversed_hex = raw[::-1]
            decoded = []
            for index in range(0, len(reversed_hex), 2):
                decoded.append(chr(int(reversed_hex[index : index + 2], 16)))
            text = "".join(decoded)
            split_len = max((len(text) - 7) // 2, 0)
            candidate = text[:split_len] + text[split_len + 7 :]
            return candidate if candidate.startswith("http") else ""
        except Exception:
            return ""

    def _extract_iframe_url(self, html):
        matched = re.search(r'"url"\s*:\s*"(https:[^"]*?)"', str(html or ""))
        if matched:
            value = matched.group(1).replace("\\/", "/")
            if any(token in value for token in ["jx.zxzj", "player-v2", "/player", "/embed"]):
                return value
        payload = self._extract_player_payload(html)
        value = str(payload.get("url") or "").replace("\\/", "/")
        return value if any(token in value for token in ["jx.zxzj", "player-v2", "/player", "/embed"]) else ""

    def _extract_result_v2_data(self, html):
        matched = re.search(r"result_v2\s*=\s*(\{[\s\S]*?\})\s*;", str(html or ""))
        if not matched:
            return ""
        try:
            payload = json.loads(matched.group(1))
        except Exception:
            return ""
        return str(payload.get("data") or payload.get("url") or "").strip()

    def playerContent(self, flag, id, vipFlags):
        raw_id = str(id or "").strip()

        # 1) 网盘分享被宿主 alist-tvbox 解析出的分集（形如 1@12345）：交给宿主换真实地址
        if self._looks_like_atv_id(raw_id):
            resolved = self._atv_resolve_play(raw_id)
            if resolved:
                return {
                    "parse": 0,
                    "jx": 0,
                    "playUrl": "",
                    "url": resolved["url"],
                    "header": resolved.get("header") or {},
                }
            return {"parse": 1, "jx": 1, "playUrl": "", "url": "", "header": {}}

        # 2) 网盘分享链接（宿主未解析时的透传，交给客户端网盘能力）
        if self._detect_pan_type("", raw_id):
            return {"parse": 0, "jx": 0, "playUrl": "", "url": raw_id, "header": {}}

        raw_flag = str(flag or "").lower()
        if raw_flag in ("baidu", "quark", "uc", "aliyun", "xunlei", "123", "mobile", "115", "tianyi"):
            return {"parse": 0, "jx": 0, "playUrl": "", "url": raw_id, "header": {}}

        play_url = raw_id if raw_id.startswith("http") else self._build_url("/" + raw_id.lstrip("/"))
        play_html = self._request_html(play_url, referer=self.headers["Referer"])
        iframe_url = self._extract_iframe_url(play_html)
        if not iframe_url:
            return {"parse": 1, "jx": 1, "playUrl": "", "url": play_url, "header": self.headers}

        iframe_headers = self._build_player_v2_headers() if "player-v2" in iframe_url else None
        iframe_html = self._request_html(iframe_url, referer=play_url, extra_headers=iframe_headers)
        encrypted = self._extract_result_v2_data(iframe_html)
        final_url = self._decrypt_url(encrypted)
        if not final_url:
            # player-v2 iframe 对非浏览器请求直接 403，回退用站内播放页地址做网页解析
            return {
                "parse": 1,
                "jx": 1,
                "playUrl": "",
                "url": play_url,
                "header": self.headers,
            }
        return {
            "parse": 0,
            "jx": 0,
            "playUrl": "",
            "url": final_url,
            "header": {"Referer": iframe_url, "User-Agent": self.headers["User-Agent"]},
        }
