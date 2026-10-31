import unittest
from types import SimpleNamespace

from youtube_cookie_monitor import has_ytdlp_auth_cookies


def cookie(name, domain=".youtube.com", expires=None):
    return SimpleNamespace(name=name, domain=domain, expires=expires)


class YtDlpCookieAuthenticationTests(unittest.TestCase):
    def test_login_info_and_sapisid_are_recognized(self):
        self.assertTrue(
            has_ytdlp_auth_cookies(
                [cookie("LOGIN_INFO"), cookie("SAPISID")]
            )
        )

    def test_login_info_and_secure_1papisid_are_recognized(self):
        self.assertTrue(
            has_ytdlp_auth_cookies(
                [cookie("LOGIN_INFO"), cookie("__Secure-1PAPISID")]
            )
        )

    def test_login_info_and_secure_3papisid_are_recognized(self):
        self.assertTrue(
            has_ytdlp_auth_cookies(
                [cookie("LOGIN_INFO"), cookie("__Secure-3PAPISID")]
            )
        )

    def test_sid_without_login_info_is_not_authenticated(self):
        self.assertFalse(
            has_ytdlp_auth_cookies([cookie("SAPISID")])
        )

    def test_login_info_with_non_ytdlp_sid_cookie_is_not_authenticated(self):
        self.assertFalse(
            has_ytdlp_auth_cookies(
                [cookie("LOGIN_INFO"), cookie("__Secure-1PSID")]
            )
        )

    def test_google_com_cookie_is_not_a_youtube_auth_cookie(self):
        self.assertFalse(
            has_ytdlp_auth_cookies(
                [cookie("LOGIN_INFO", ".google.com"), cookie("SAPISID", ".google.com")]
            )
        )

    def test_expired_auth_cookies_are_not_authenticated(self):
        self.assertFalse(
            has_ytdlp_auth_cookies(
                [cookie("LOGIN_INFO", expires=1), cookie("SAPISID", expires=1)]
            )
        )


if __name__ == "__main__":
    unittest.main()
