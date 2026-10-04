import base64
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import load_credentials

BASE = "https://intervals.icu"


class ApiError(Exception):
    pass


class Intervals:
    def __init__(self):
        self.athlete_id, token = load_credentials()
        self._auth = "Basic " + base64.b64encode(f"API_KEY:{token}".encode()).decode()

    def get(self, path, params=None, retries=3):
        url = BASE + path.replace("{aid}", self.athlete_id)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": self._auth,
            "User-Agent": "tokyo-marathon-coach/1.0",
            "Accept": "application/json",
        })
        last = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise ApiError(
                        f"HTTP {e.code}：认证/访问被拒。检查 token 是否更新、Basic Auth 用户名是否为 API_KEY；"
                        f"若在 sandbox 中运行，请以 full_network 权限重试。") from None
                if e.code == 404:
                    raise ApiError(f"HTTP 404：接口或资源不存在 ({path})") from None
                last = f"HTTP {e.code}"
                if e.code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
            except urllib.error.URLError as e:
                reason = e.reason
                if isinstance(reason, socket.gaierror):
                    raise ApiError("DNS 解析失败：sandbox 可能禁止网络，请以 full_network 权限重试。") from None
                last = f"网络错误: {reason}"
            except (TimeoutError, socket.timeout):
                last = "超时"
            time.sleep(1.5 * (attempt + 1))
        raise ApiError(f"请求失败（{last}）：{path}")

    def wellness(self, oldest, newest):
        return self.get("/api/v1/athlete/{aid}/wellness", {"oldest": str(oldest), "newest": str(newest)})

    def activities(self, oldest, newest):
        return self.get("/api/v1/athlete/{aid}/activities", {"oldest": str(oldest), "newest": str(newest)})

    def activity(self, aid):
        return self.get(f"/api/v1/activity/{aid}", {"intervals": "true"})

    def streams(self, aid, types):
        return self.get(f"/api/v1/activity/{aid}/streams.json", {"types": ",".join(types)})

    def events(self, oldest, newest):
        return self.get("/api/v1/athlete/{aid}/events", {"oldest": str(oldest), "newest": str(newest)})
