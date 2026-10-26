# SPDX-License-Identifier: MPL-2.0
"""Existing console transport. No tokens, domain imports, or agent loop."""

import json
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Agents:
    def __init__(self, url="http://127.0.0.1:8098"):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in {"", "/"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("agent backend must be a loopback HTTP origin")
        self.url = f"http://127.0.0.1:{parsed.port or 80}"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, body=None):
        headers = {"Accept": "application/json"}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Origin": self.url})
        request = Request(self.url + path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(request, timeout=12) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Agent request refused ({exc.code}); refresh before retrying") from None

    def snapshot(self):
        return self.request("/api/harnesses")

    def create(self, profile):
        return self.request("/api/chats", {"profile": profile})

    def detail(self, profile, session):
        return self.request(f"/api/chats/{quote(profile, safe='')}/{quote(session, safe='')}")

    def send(self, profile, session, request_key, text):
        return self.request(f"/api/chats/{quote(profile, safe='')}/{quote(session, safe='')}/messages", {"id": request_key, "text": text})
