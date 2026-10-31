"""Conservative health checks for YouTube Netscape cookies.

This module never logs or returns cookie values. A network denial is reported as
unknown rather than expired because YouTube may reject Railway's IP independently
of the account session.
"""
from __future__ import annotations

import http.cookiejar
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any


_AUTH_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "LOGIN_INFO",
}
_EXPIRY_WARNING_SECONDS = 7 * 24 * 60 * 60
_CHECK_URL = "https://www.youtube.com/feed/subscriptions"


def check_youtube_cookie_status(
    cookie_file: str | None,
    cookies_configured: bool = False,
) -> dict[str, Any]:
    """Return status/detail without exposing cookie values or request headers."""
    if not cookie_file or not os.path.isfile(cookie_file):
        return {
            "status": "malformed" if cookies_configured else "missing",
            "detail": "تحقق من وجود ملف Netscape صالح داخل إعدادات Railway.",
            "expires_at": None,
        }

    jar = http.cookiejar.MozillaCookieJar(cookie_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError, http.cookiejar.LoadError):
        return {
            "status": "malformed",
            "detail": "تعذر قراءة ملف الكوكيز بصيغة Netscape.",
            "expires_at": None,
        }

    auth_cookies = [
        cookie
        for cookie in jar
        if cookie.name in _AUTH_COOKIE_NAMES
        and (
            cookie.domain.lstrip(".") == "youtube.com"
            or cookie.domain.lstrip(".").endswith(".youtube.com")
            or cookie.domain.lstrip(".") == "google.com"
            or cookie.domain.lstrip(".").endswith(".google.com")
        )
    ]
    if not auth_cookies:
        return {
            "status": "malformed",
            "detail": "لم يتم العثور على كوكيز جلسة تسجيل دخول معروفة.",
            "expires_at": None,
        }

    now = int(time.time())
    active_cookies = [
        cookie for cookie in auth_cookies
        if cookie.expires is None or cookie.expires > now
    ]
    if not active_cookies:
        return {
            "status": "expired",
            "detail": "تواريخ انتهاء كوكيز تسجيل الدخول المسجلة قد مضت.",
            "expires_at": None,
        }

    future_expiries = [
        int(cookie.expires)
        for cookie in active_cookies
        if cookie.expires is not None and cookie.expires > now
    ]
    nearest_expiry = min(future_expiries) if future_expiries else None
    expiry_is_soon = (
        nearest_expiry is not None
        and nearest_expiry - now <= _EXPIRY_WARNING_SECONDS
    )

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar)
    )
    request = urllib.request.Request(
        _CHECK_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with opener.open(request, timeout=12) as response:
            final_url = response.geturl().lower()
            body = response.read(2 * 1024 * 1024).decode(
                "utf-8", errors="replace"
            )
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429):
            return {
                "status": "expiring" if expiry_is_soon else "unknown",
                "detail": (
                    "رفض يوتيوب طلب الفحص من الخادم؛ لا يمكن اعتبار ذلك دليلًا "
                    "على انتهاء الجلسة."
                ),
                "expires_at": nearest_expiry,
            }
        return {
            "status": "unknown",
            "detail": f"تعذر فحص صفحة الاشتراكات (HTTP {error.code}).",
            "expires_at": nearest_expiry,
        }
    except (OSError, TimeoutError, urllib.error.URLError):
        return {
            "status": "expiring" if expiry_is_soon else "unknown",
            "detail": "تعذر الوصول إلى يوتيوب أثناء الفحص؛ لم يتم الحكم على الجلسة.",
            "expires_at": nearest_expiry,
        }

    if (
        "accounts.google.com" in final_url
        or "/signin" in final_url
        or "servicelogin" in final_url
    ):
        return {
            "status": "invalid",
            "detail": "تم تحويل طلب الفحص إلى صفحة تسجيل الدخول.",
            "expires_at": nearest_expiry,
        }

    login_match = re.search(
        r'"LOGGED_IN"\s*:\s*(true|false)',
        body,
        flags=re.IGNORECASE,
    )
    if login_match and login_match.group(1).lower() == "false":
        return {
            "status": "invalid",
            "detail": "صفحة يوتيوب أفادت بأن الحساب غير مسجل الدخول.",
            "expires_at": nearest_expiry,
        }
    if login_match and login_match.group(1).lower() == "true":
        return {
            "status": "expiring" if expiry_is_soon else "valid",
            "detail": (
                "تم تأكيد تسجيل الدخول، لكن بعض الكوكيز تقترب من الانتهاء."
                if expiry_is_soon
                else "أكدت صفحة يوتيوب أن جلسة الحساب مسجلة الدخول."
            ),
            "expires_at": nearest_expiry,
        }

    return {
        "status": "expiring" if expiry_is_soon else "unknown",
        "detail": (
            "توجد كوكيز جلسة بتاريخ انتهاء قريب، لكن تعذر تأكيد حالة تسجيل الدخول."
            if expiry_is_soon
            else "استجاب يوتيوب دون إشارة موثوقة تؤكد حالة تسجيل الدخول."
        ),
        "expires_at": nearest_expiry,
    }
