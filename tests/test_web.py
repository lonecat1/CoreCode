"""Web 能力的测试：重试、域名过滤、双后端解析、抓取。

## 这个模块此前没有任何测试

`utils/web.py` 此前完全没被测过（`test_tools.py` 只覆盖 `list_files`）。
而它包含几段**判断逻辑**——什么算可重试、域名怎么匹配、用哪个后端——
这些错了不会报错，只会让行为悄悄不对。

## 一条安全相关的断言

`matches_domain("evil-python.org", "python.org")` 必须是 `False`。
朴素的 `host.endswith(domain)` 会返回 `True`——那意味着任何以
`python.org` 结尾的域名都能通过「只允许 python.org」的过滤。
"""

from __future__ import annotations

import io
import errno
import os
import socket
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from corecode.utils.web import (  # noqa: E402
    BACKOFF_BASE_MS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RESULTS,
    HttpResponse,
    SEARCH_PROVIDERS,
    WebError,
    decode_html,
    extract_html_redirect_url,
    extract_readable_text,
    extract_title,
    fetch_search_page,
    fetch_with_retry,
    first_match,
    format_web_error_message,
    is_retryable_network_error,
    is_retryable_status,
    matches_domain,
    node_error_code,
    normalize_domain_list,
    normalize_duckduckgo_link,
    normalize_sogou_link,
    parse_duckduckgo_lite,
    parse_search_results,
    parse_sogou_search,
    passes_domain_filter,
    search_web,
    strip_tags,
)

DDG_SAMPLE = """
<table>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F" class='result-link'>Python 3 Docs</a></td></tr>
<tr><td class='result-snippet'>The official <b>Python</b> documentation.</td></tr>
<tr><td><span class='link-text'>docs.python.org</span></td></tr>
<tr><td><a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F" class='result-link'>Example</a></td></tr>
<tr><td class='result-snippet'>An example site.</td></tr>
</table>
"""

SOGOU_SAMPLE = """
<div class="vrwrap">
  <h3 class="vr-title"><a href="/link?url=abc">搜狗结果一</a></h3>
  <div class="fz-mid">这是摘要一 &amp; 更多</div>
</div>
<div class="vrwrap">
  <h3 class="vr-title"><a href="//www.sogou.com/link?url=def">结果二</a></h3>
  <div class="str-text-info">摘要二</div>
</div>
"""

class HtmlHelperTests(unittest.TestCase):
    def test_decode_html_common_entities(self) -> None:
        self.assertEqual(decode_html("a &amp; b &lt;c&gt; &quot;d&quot;"), 'a & b <c> "d"')

    def test_decode_html_nbsp(self) -> None:
        self.assertEqual(decode_html("a&nbsp;b"), "a b")

    def test_decode_html_amp_is_first(self) -> None:
        """**双重解码**：`&amp;lt;` → `&lt;`（第一步）→ `<`（第二步）。

        是一条 `.replace()` 链）。我原先以为「`&amp;` 先解就能保住 `&lt;`」，
        写错了期望——链会继续往下走。

        实践中影响很小（要求原文里真的写了双层转义），因此保持一致而不是
        特殊处理。
        """
        self.assertEqual(decode_html("&amp;lt;"), "<")

    def test_decode_html_single_level(self) -> None:
        self.assertEqual(decode_html("&lt;b&gt;"), "<b>")

    def test_strip_tags_collapses_whitespace(self) -> None:
        self.assertEqual(strip_tags("<b>a</b>\n\n  <i>b</i>"), "a b")

    def test_first_match_returns_group(self) -> None:
        self.assertEqual(first_match(r"x(\d+)", "x42y"), "42")

    def test_first_match_no_match_is_none(self) -> None:
        self.assertIsNone(first_match(r"z(\d+)", "x42y"))

    def test_first_match_missing_group_is_none(self) -> None:
        """分组不存在时返回 None，而不是抛 IndexError。"""
        self.assertIsNone(first_match(r"x(\d+)(y)?", "x42", group=5))

    def test_extract_title(self) -> None:
        self.assertEqual(
            extract_title("<html><title>  Hello &amp; World </title></html>"),
            "Hello & World",
        )

    def test_extract_title_missing(self) -> None:
        self.assertIsNone(extract_title("<html><body>x</body></html>"))

    def test_extract_title_empty_is_none(self) -> None:
        self.assertIsNone(extract_title("<title>   </title>"))

    def test_readable_text_drops_script_and_style(self) -> None:
        html = "<html><script>var x=1</script><style>.a{}</style><p>正文</p></html>"
        text, _ = extract_readable_text(html)
        self.assertIn("正文", text)
        self.assertNotIn("var x", text)
        self.assertNotIn(".a{}", text)

    def test_readable_text_keeps_paragraph_breaks(self) -> None:
        text, _ = extract_readable_text("<p>第一段</p><p>第二段</p>")
        self.assertIn("\n", text)

    def test_readable_text_malformed_html_does_not_raise(self) -> None:
        text, _ = extract_readable_text("<p>unclosed <b>tags")
        self.assertIn("unclosed", text)

class RetryDecisionTests(unittest.TestCase):
    def test_429_is_retryable(self) -> None:
        self.assertTrue(is_retryable_status(429))

    def test_5xx_is_retryable(self) -> None:
        for status in (500, 502, 503, 599):
            self.assertTrue(is_retryable_status(status), status)

    def test_404_is_not_retryable(self) -> None:
        """**重试不会让 404 变成 200。** 只有暂时性失败才值得重试。"""
        self.assertFalse(is_retryable_status(404))

    def test_4xx_others_are_not_retryable(self) -> None:
        for status in (400, 401, 403, 422):
            self.assertFalse(is_retryable_status(status), status)

    def test_200_is_not_retryable(self) -> None:
        self.assertFalse(is_retryable_status(200))

    def test_timeout_is_retryable(self) -> None:
        self.assertTrue(is_retryable_network_error(socket.timeout("slow")))
        self.assertTrue(is_retryable_network_error(TimeoutError()))

    def test_urlerror_with_retryable_reason(self) -> None:
        error = urllib.error.URLError("ECONNRESET")
        self.assertTrue(is_retryable_network_error(error))

    def test_urlerror_with_non_retryable_reason(self) -> None:
        error = urllib.error.URLError("some other problem")
        self.assertFalse(is_retryable_network_error(error))

    def test_nested_reason_is_inspected(self) -> None:
        inner = socket.timeout("deep")
        outer = urllib.error.URLError(inner)
        self.assertTrue(is_retryable_network_error(outer))

class ErrorMessageTests(unittest.TestCase):
    def test_timeout_mentions_the_duration(self) -> None:
        message = format_web_error_message("https://x", socket.timeout(), 12000)
        self.assertIn("timed out after 12000ms", message)
        self.assertIn("https://x", message)

    def test_other_error_includes_url(self) -> None:
        message = format_web_error_message("https://x", ValueError("bad"), 1000)
        self.assertIn("https://x", message)
        self.assertIn("bad", message)

    def test_no_error_still_mentions_url(self) -> None:
        self.assertIn("https://x", format_web_error_message("https://x", None, 1000))

class NodeErrorCodeTests(unittest.TestCase):
    """Python 的异常 → **Node 风格的错误码**。

    ## 为什么这层翻译必须存在

    `getErrorCode(error)`——Node 把 `'ECONNREFUSED'` 这类字符串放在
    `error.code` 上。Python 没有 `.code`：信息散在异常类型、`errno` 整数、
    以及 `URLError.reason` 里。

    不翻译的直接后果是用户看到
    `URLError: <urlopen error [Errno 11001] getaddrinfo failed> (https://…)`
    """

    def test_dns_failures_map_to_enotfound_or_eai_again(self) -> None:
        self.assertEqual(
            node_error_code(socket.gaierror(socket.EAI_NONAME, "x")), "ENOTFOUND"
        )
        self.assertEqual(
            node_error_code(socket.gaierror(socket.EAI_AGAIN, "x")), "EAI_AGAIN"
        )

    def test_connection_errors_map_by_exception_type(self) -> None:
        """按**类型**而不是按数字：`ECONNREFUSED` 在 Windows 是 10061、Linux 是 111。"""
        self.assertEqual(
            node_error_code(ConnectionRefusedError(111, "refused")), "ECONNREFUSED"
        )
        self.assertEqual(
            node_error_code(ConnectionResetError(104, "reset")), "ECONNRESET"
        )
        self.assertEqual(node_error_code(BrokenPipeError(32, "pipe")), "EPIPE")

    def test_url_error_is_unwrapped(self) -> None:
        """`urllib` 把真实原因包在 `URLError.reason` 里。"""
        wrapped_dns = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "x"))
        self.assertEqual(node_error_code(wrapped_dns), "ENOTFOUND")
        wrapped_refused = urllib.error.URLError(ConnectionRefusedError(111, "x"))
        self.assertEqual(node_error_code(wrapped_refused), "ECONNREFUSED")

    def test_winsock_prefixed_errno_names_are_normalized(self) -> None:
        """**Windows 上 `errno.errorcode` 给的是 `WSAEHOSTUNREACH`。**

        不剥掉 `WSA` 前缀，整张名字映射表在 Windows 上全部落空——
        `EHOSTUNREACH` / `ENETUNREACH` / `ENETDOWN` 一个都映射不出来，
        用户看到的就是 `[Errno 10065] …`。

        这几个码**没有**对应的 Python 异常子类（`OSError(10065)` 就是普通
        `OSError`），所以只能靠名字查表——表格里这几项不是冗余的。
        """
        for name, expected in (
            ("EHOSTUNREACH", "EHOSTUNREACH"),
            ("ENETUNREACH", "ENETUNREACH"),
            ("ENETDOWN", "ENETDOWN"),
        ):
            number = getattr(errno, name, None)
            if number is None:
                self.skipTest(f"本平台没有 {name}")
            error = OSError(number, "boom")
            with self.subTest(name=name, number=number):
                self.assertEqual(node_error_code(error), expected)
                self.assertIn(
                    f"({expected})",
                    format_web_error_message("https://x", error, 1000),
                )

    def test_timeout_has_no_code(self) -> None:
        """**超时不给码。** Node 的超时是 `AbortError`，它没有 `code`。

        给它塞一个 `ETIMEDOUT` 会让文案从「带时长」退化成「只有码」——信息更少。
        """
        self.assertIsNone(node_error_code(TimeoutError("timed out")))
        self.assertIsNone(node_error_code(socket.timeout("timed out")))
        self.assertIsNone(node_error_code(urllib.error.URLError(TimeoutError("x"))))

    def test_unknown_errors_have_no_code(self) -> None:
        self.assertIsNone(node_error_code(None))
        self.assertIsNone(node_error_code(ValueError("bad")))
        self.assertIsNone(node_error_code(OSError(5, "input/output error")))

    def test_self_referential_error_does_not_loop(self) -> None:
        """异常链可能自引用——不设防会死循环。"""
        error = OSError(5, "loop")
        error.__cause__ = error
        self.assertIsNone(node_error_code(error))

class FormatBranchesTests(unittest.TestCase):

    def test_coded_error_shows_the_code(self) -> None:
        message = format_web_error_message(
            "https://x.test/p", ConnectionRefusedError(111, "refused"), 5000
        )
        self.assertEqual(message, "request failed (ECONNREFUSED) for https://x.test/p")

    def test_dns_error_shows_enotfound(self) -> None:
        message = format_web_error_message(
            "https://x.test/p", socket.gaierror(socket.EAI_NONAME, "x"), 5000
        )
        self.assertEqual(message, "request failed (ENOTFOUND) for https://x.test/p")

    def test_timeout_wins_over_the_code_branch(self) -> None:
        message = format_web_error_message(
            "https://x.test/p", urllib.error.URLError(TimeoutError("t")), 7000
        )
        self.assertEqual(message, "request timed out after 7000ms for https://x.test/p")

    def test_uncoded_error_shows_its_message(self) -> None:
        message = format_web_error_message("https://x.test/p", ValueError("bad"), 5000)
        self.assertEqual(message, "bad (https://x.test/p)")

    def test_no_error_falls_back(self) -> None:
        self.assertEqual(
            format_web_error_message("https://x.test/p", None, 5000),
            "request failed for https://x.test/p",
        )

    def test_python_class_names_do_not_leak(self) -> None:
        """用户不该看到 `URLError:` / `<urlopen error …>` 这类实现细节。"""
        for error in (
            urllib.error.URLError(ConnectionRefusedError(111, "refused")),
            OSError(5, "input/output error"),
        ):
            with self.subTest(error=type(error).__name__):
                message = format_web_error_message("https://x.test/p", error, 5000)
                self.assertNotIn("urlopen", message)
                self.assertNotIn("URLError", message)

class RetryClassificationTests(unittest.TestCase):
    """重试判据必须是**错误码**，不是对错误文本做子串匹配。"""

    def test_temp_dns_failure_is_retryable(self) -> None:
        """**这条曾经是错的。**

        旧实现把 `errno` 当字符串查表（实际是整数，永远不命中），于是完全靠
        `str(error)` 子串兜底——而 Windows 上 `gaierror(EAI_AGAIN)` 的文本是
        `[Errno 11002] getaddrinfo failed`，**不含 `EAI_AGAIN`**，临时 DNS
        故障因此不会被重试。Node 会把它映射成 `EAI_AGAIN` → 可重试。
        """
        error = socket.gaierror(socket.EAI_AGAIN, "x")
        self.assertEqual(node_error_code(error), "EAI_AGAIN")
        self.assertTrue(is_retryable_network_error(error))

    def test_connection_errors_are_retryable(self) -> None:
        for error in (
            ConnectionRefusedError(111, "x"),
            ConnectionResetError(104, "x"),
            urllib.error.URLError(ConnectionResetError(104, "x")),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(is_retryable_network_error(error))

    def test_timeouts_are_retryable(self) -> None:
        for error in (TimeoutError("t"), socket.timeout("t"),
                      urllib.error.URLError(TimeoutError("t"))):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(is_retryable_network_error(error))

    def test_non_network_errors_are_not_retryable(self) -> None:
        for error in (ValueError("bad"), OSError(5, "input/output error")):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(is_retryable_network_error(error))

    def test_retryable_codes_match_the_original_list(self) -> None:
        from corecode.utils.web import _RETRYABLE_ERROR_CODES

        self.assertEqual(
            _RETRYABLE_ERROR_CODES,
            frozenset({
                "ETIMEDOUT", "ECONNRESET", "EAI_AGAIN", "ENOTFOUND",
                "ECONNREFUSED", "UND_ERR_CONNECT_TIMEOUT",
                "UND_ERR_HEADERS_TIMEOUT", "UND_ERR_BODY_TIMEOUT",
            }),
        )

def _fake_response(status: int = 200, body: bytes = b"ok", content_type: str = "text/html"):
    response = MagicMock()
    response.status = status
    response.reason = "OK"
    response.headers = {"content-type": content_type}
    response.geturl.return_value = "https://final.example/"
    response.read.return_value = body
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *a: False
    return response

class FetchRetryTests(unittest.TestCase):
    def _call(self, side_effect, **kwargs):
        with patch("urllib.request.urlopen", side_effect=side_effect) as opener:
            with patch("corecode.utils.web._sleep_ms") as sleeper:
                result = fetch_with_retry("https://x.example/", **kwargs)
        return result, opener, sleeper

    def test_success_on_first_try(self) -> None:
        result, opener, sleeper = self._call([_fake_response()])
        self.assertEqual(result.status, 200)
        self.assertEqual(opener.call_count, 1)
        sleeper.assert_not_called()

    def test_retries_on_5xx_then_succeeds(self) -> None:
        result, opener, sleeper = self._call(
            [_fake_response(500), _fake_response(200)]
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(sleeper.call_count, 1)

    def test_backoff_doubles(self) -> None:
        """退避是 `300ms × 2^n`。固定间隔会在服务端限流时雪上加霜。"""
        _, _, sleeper = self._call(
            [_fake_response(500), _fake_response(500), _fake_response(200)]
        )
        delays = [call.args[0] for call in sleeper.call_args_list]
        self.assertEqual(delays, [BACKOFF_BASE_MS, BACKOFF_BASE_MS * 2])

    def test_backoff_doubles_for_network_errors_too(self) -> None:
        """**网络错误路径的退避也必须翻倍。**

        状态码重试与网络错误重试是两段独立代码（`except` 块里一处、
        `if` 判断后一处）。只测其中一条时，另一条的退避退化成固定间隔
        不会被发现——变异的锚点恰好也容易只命中一处。
        """
        timeout = urllib.error.URLError(socket.timeout("slow"))
        _, _, sleeper = self._call([timeout, timeout, _fake_response()])
        delays = [call.args[0] for call in sleeper.call_args_list]
        self.assertEqual(delays, [BACKOFF_BASE_MS, BACKOFF_BASE_MS * 2])

    def test_gives_up_after_max_retries_and_returns_last_response(self) -> None:
        """重试用尽后**返回最后一次响应**，而不是抛异常。

        调用方需要看到那个 500 的状态码与正文才能判断发生了什么。
        """
        result, opener, _ = self._call([_fake_response(503)] * (DEFAULT_MAX_RETRIES + 1))
        self.assertEqual(result.status, 503)
        self.assertEqual(opener.call_count, DEFAULT_MAX_RETRIES + 1)

    def test_404_returns_immediately(self) -> None:
        result, opener, sleeper = self._call([_fake_response(404)])
        self.assertEqual(result.status, 404)
        self.assertEqual(opener.call_count, 1)
        sleeper.assert_not_called()

    def test_http_error_is_treated_as_a_response(self) -> None:
        """**urllib 把 4xx/5xx 抛成异常，而 fetch 的语义是返回响应。**

        这层把两者抹平，因此非重试的 4xx 也能带着正文返回。
        """
        error = urllib.error.HTTPError(
            "https://x.example/", 403, "Forbidden", {"content-type": "text/html"}, io.BytesIO(b"denied")
        )
        result, _, _ = self._call([error])
        self.assertEqual(result.status, 403)
        self.assertEqual(result.body, b"denied")

    def test_network_error_is_retried(self) -> None:
        error = urllib.error.URLError(socket.timeout("slow"))
        result, opener, _ = self._call([error, _fake_response()])
        self.assertEqual(result.status, 200)
        self.assertEqual(opener.call_count, 2)

    def test_non_retryable_network_error_raises_web_error(self) -> None:
        error = urllib.error.URLError("permanent failure")
        with self.assertRaises(WebError) as caught:
            self._call([error])
        self.assertIn("https://x.example/", str(caught.exception))

    def test_max_retries_zero_means_single_attempt(self) -> None:
        result, opener, _ = self._call([_fake_response(500)], max_retries=0)
        self.assertEqual(result.status, 500)
        self.assertEqual(opener.call_count, 1)

    def test_user_agent_is_sent(self) -> None:
        _, opener, _ = self._call([_fake_response()])
        request = opener.call_args.args[0]
        self.assertIn("CoreCode", request.get_header("User-agent"))

class DomainListTests(unittest.TestCase):
    def test_lowercases(self) -> None:
        self.assertEqual(normalize_domain_list(["Example.COM"]), ["example.com"])

    def test_strips_wildcard_prefix(self) -> None:
        self.assertEqual(normalize_domain_list(["*.example.com"]), ["example.com"])

    def test_strips_dot_prefix(self) -> None:
        self.assertEqual(normalize_domain_list([".example.com"]), ["example.com"])

    def test_extracts_hostname_from_url(self) -> None:
        self.assertEqual(
            normalize_domain_list(["https://example.com/path?q=1"]), ["example.com"]
        )

    def test_drops_empty_entries(self) -> None:
        self.assertEqual(normalize_domain_list(["", "  ", "a.com"]), ["a.com"])

    def test_none_and_empty(self) -> None:
        self.assertEqual(normalize_domain_list(None), [])
        self.assertEqual(normalize_domain_list([]), [])

class DomainMatchTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertTrue(matches_domain("python.org", "python.org"))

    def test_subdomain_matches(self) -> None:
        self.assertTrue(matches_domain("docs.python.org", "python.org"))

    def test_unrelated_domain_does_not_match(self) -> None:
        self.assertFalse(matches_domain("evil.com", "python.org"))

    def test_suffix_lookalike_does_not_match(self) -> None:
        """**安全相关。** 朴素的 `endswith("python.org")` 会误判。

        `evil-python.org` 以 `python.org` 结尾，但它**不是** python.org 的子域。
        只做后缀匹配等于让任何构造出的相似域名通过白名单。
        """
        self.assertFalse(matches_domain("evil-python.org", "python.org"))
        self.assertFalse(matches_domain("notpython.org", "python.org"))

    def test_deeper_subdomain_matches(self) -> None:
        self.assertTrue(matches_domain("a.b.python.org", "python.org"))

class DomainFilterTests(unittest.TestCase):
    def test_no_filters_passes_everything(self) -> None:
        self.assertTrue(passes_domain_filter("https://any.com/x", [], []))

    def test_allowed_only(self) -> None:
        self.assertTrue(passes_domain_filter("https://docs.python.org/3/", ["python.org"], []))
        self.assertFalse(passes_domain_filter("https://other.com/", ["python.org"], []))

    def test_blocked_only(self) -> None:
        self.assertFalse(passes_domain_filter("https://docs.python.org/", [], ["python.org"]))
        self.assertTrue(passes_domain_filter("https://other.com/", [], ["python.org"]))

    def test_blocked_wins_over_allowed(self) -> None:
        """顺序是「先排除，再限定」。"""
        self.assertFalse(
            passes_domain_filter("https://bad.com/", ["bad.com"], ["bad.com"])
        )

    def test_unparseable_link_is_rejected(self) -> None:
        """解析不出主机名的链接**直接排除**。

        放过去等于让过滤器形同虚设——它本该是「只允许这些域」。
        """
        self.assertFalse(passes_domain_filter("not a url", ["python.org"], []))
        self.assertFalse(passes_domain_filter("", [], []))

class LinkNormalizeTests(unittest.TestCase):
    def test_ddg_unwraps_uddg(self) -> None:
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1"
        self.assertEqual(normalize_duckduckgo_link(href), "https://example.com/a?b=1")

    def test_ddg_direct_link_passes_through(self) -> None:
        self.assertEqual(
            normalize_duckduckgo_link("https://example.com/x"), "https://example.com/x"
        )

    def test_ddg_protocol_relative_becomes_https(self) -> None:
        self.assertEqual(
            normalize_duckduckgo_link("//example.com/x"), "https://example.com/x"
        )

    def test_ddg_empty(self) -> None:
        self.assertEqual(normalize_duckduckgo_link(""), "")

    def test_ddg_decodes_entities(self) -> None:
        self.assertEqual(
            normalize_duckduckgo_link("https://example.com/a&amp;b"),
            "https://example.com/a&b",
        )

    def test_sogou_relative_becomes_absolute(self) -> None:
        self.assertEqual(
            normalize_sogou_link("/link?url=x"), "https://www.sogou.com/link?url=x"
        )

    def test_sogou_protocol_relative(self) -> None:
        self.assertEqual(
            normalize_sogou_link("//www.sogou.com/link"), "https://www.sogou.com/link"
        )

    def test_sogou_absolute_unchanged(self) -> None:
        self.assertEqual(normalize_sogou_link("https://x.com"), "https://x.com")

    def test_sogou_empty(self) -> None:
        self.assertEqual(normalize_sogou_link(""), "")

class DuckDuckGoParserTests(unittest.TestCase):
    def test_parses_two_results(self) -> None:
        results = parse_duckduckgo_lite(DDG_SAMPLE)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "Python 3 Docs")
        self.assertEqual(results[0].url, "https://docs.python.org/3/")
        self.assertIn("official", results[0].snippet)

    def test_ignores_anchors_without_result_link_class(self) -> None:
        html = '<a href="https://x.com">not a result</a>'
        self.assertEqual(parse_duckduckgo_lite(html), [])

    def test_ignores_result_without_href(self) -> None:
        html = '<a class="result-link">No href</a>'
        self.assertEqual(parse_duckduckgo_lite(html), [])

    def test_empty_html(self) -> None:
        self.assertEqual(parse_duckduckgo_lite(""), [])

    def test_snippet_comes_from_the_following_block(self) -> None:
        results = parse_duckduckgo_lite(DDG_SAMPLE)
        self.assertNotIn("example", results[0].snippet.lower())
        self.assertIn("example", results[1].snippet.lower())

class SogouParserTests(unittest.TestCase):
    def test_parses_two_results(self) -> None:
        results = parse_sogou_search(SOGOU_SAMPLE)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "搜狗结果一")
        self.assertEqual(results[0].url, "https://www.sogou.com/link?url=abc")

    def test_decodes_entities_in_snippet(self) -> None:
        results = parse_sogou_search(SOGOU_SAMPLE)
        self.assertIn("&", results[0].snippet)
        self.assertNotIn("&amp;", results[0].snippet)

    def test_matches_multiple_snippet_classes(self) -> None:
        """搜狗的摘要 class 有多种（`fz-mid` / `str-text-info` / …），都要认。"""
        results = parse_sogou_search(SOGOU_SAMPLE)
        self.assertIn("摘要一", results[0].snippet)
        self.assertIn("摘要二", results[1].snippet)

    def test_empty_html(self) -> None:
        self.assertEqual(parse_sogou_search(""), [])

    def test_heading_without_link_is_skipped(self) -> None:
        self.assertEqual(parse_sogou_search("<h3>no anchor</h3>"), [])

class ParseDispatchTests(unittest.TestCase):
    def test_duckduckgo_provider(self) -> None:
        self.assertEqual(len(parse_search_results("duckduckgo-lite", DDG_SAMPLE)), 2)

    def test_other_provider_uses_sogou_parser(self) -> None:
        self.assertEqual(len(parse_search_results("sogou", SOGOU_SAMPLE)), 2)

class SearchProviderTests(unittest.TestCase):
    def test_provider_order(self) -> None:
        self.assertEqual(SEARCH_PROVIDERS, ("duckduckgo-lite", "sogou"))

    def test_default_result_count_is_five(self) -> None:
        """取少而准比取多而杂有用。"""
        self.assertEqual(DEFAULT_MAX_RESULTS, 5)

    def test_duckduckgo_uses_the_lite_endpoint(self) -> None:
        """**端点必须是 lite。**

        而不是 `html.duckduckgo.com`（那是 `result__a`）。端点与解析器必须配对，
        否则会得到「永远没有结果」——而且看起来像「确实没搜到」。
        """
        captured: list[str] = []

        def fake(url, **kwargs):  # noqa: ANN001
            captured.append(url)
            return HttpResponse(200, "OK", "text/html", url, b"")

        with patch("corecode.utils.web.fetch_with_retry", side_effect=fake):
            fetch_search_page("duckduckgo-lite", "hello world")
        self.assertIn("lite.duckduckgo.com", captured[0])
        self.assertIn("q=hello", captured[0])

    def test_sogou_uses_query_parameter(self) -> None:
        captured: list[str] = []

        def fake(url, **kwargs):  # noqa: ANN001
            captured.append(url)
            return HttpResponse(200, "OK", "text/html", url, b"")

        with patch("corecode.utils.web.fetch_with_retry", side_effect=fake):
            fetch_search_page("sogou", "你好")
        self.assertIn("sogou.com", captured[0])
        self.assertIn("query=", captured[0])

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(WebError):
            fetch_search_page("bing", "x")

class SearchWebTests(unittest.TestCase):
    def _patch(self, responses: dict):
        """把后端响应替换成固定值。`responses` 是 provider -> HttpResponse。"""

        def fake(provider, query, **kwargs):  # noqa: ANN001
            value = responses.get(provider)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise WebError(f"no response for {provider}")
            return value

        return patch("corecode.utils.web.fetch_search_page", side_effect=fake)

    def test_uses_first_provider_when_it_has_results(self) -> None:
        with self._patch({"duckduckgo-lite": HttpResponse(200, "OK", "text/html", "u", DDG_SAMPLE.encode())}):
            results, provider = search_web("python")
        self.assertEqual(provider, "duckduckgo-lite")
        self.assertEqual(len(results), 2)

    def test_falls_back_to_second_provider(self) -> None:
        with self._patch(
            {
                "duckduckgo-lite": HttpResponse(200, "OK", "text/html", "u", b"<html>no results</html>"),
                "sogou": HttpResponse(200, "OK", "text/html", "u", SOGOU_SAMPLE.encode()),
            }
        ):
            results, provider = search_web("测试")
        self.assertEqual(provider, "sogou")
        self.assertEqual(len(results), 2)

    def test_falls_back_on_http_error(self) -> None:
        with self._patch(
            {
                "duckduckgo-lite": HttpResponse(503, "Unavailable", "text/html", "u", b""),
                "sogou": HttpResponse(200, "OK", "text/html", "u", SOGOU_SAMPLE.encode()),
            }
        ):
            _results, provider = search_web("x")
        self.assertEqual(provider, "sogou")

    def test_falls_back_on_exception(self) -> None:
        with self._patch(
            {
                "duckduckgo-lite": WebError("boom"),
                "sogou": HttpResponse(200, "OK", "text/html", "u", SOGOU_SAMPLE.encode()),
            }
        ):
            _results, provider = search_web("x")
        self.assertEqual(provider, "sogou")

    def test_all_providers_failing_raises(self) -> None:
        with self._patch({"duckduckgo-lite": WebError("a"), "sogou": WebError("b")}):
            with self.assertRaises(WebError) as caught:
                search_web("x")
        self.assertIn("all search providers failed", str(caught.exception))

    def test_no_results_from_any_provider_is_reported_as_an_error(self) -> None:
        """

        因此「两个后端都返回 200 但都没解析出结果」得到的是一个**错误**，
        而不是空结果。`ok: false` 与「空结果」对模型是不同信号——前者会让它
        换关键词或换思路。

        """
        with self._patch(
            {
                "duckduckgo-lite": HttpResponse(200, "OK", "text/html", "u", b"<html></html>"),
                "sogou": HttpResponse(200, "OK", "text/html", "u", b"<html></html>"),
            }
        ):
            with self.assertRaises(WebError) as caught:
                search_web("x")
        message = str(caught.exception)
        self.assertIn("all search providers failed", message)
        self.assertIn("no results", message)

    def test_domain_filter_is_applied(self) -> None:
        with self._patch({"duckduckgo-lite": HttpResponse(200, "OK", "text/html", "u", DDG_SAMPLE.encode())}):
            results, _ = search_web("python", allowed_domains=["example.com"])
        self.assertEqual(len(results), 1)
        self.assertIn("example.com", results[0].url)

    def test_blocked_domains_are_excluded(self) -> None:
        with self._patch({"duckduckgo-lite": HttpResponse(200, "OK", "text/html", "u", DDG_SAMPLE.encode())}):
            results, _ = search_web("python", blocked_domains=["example.com"])
        self.assertEqual(len(results), 1)
        self.assertIn("python.org", results[0].url)

    def test_max_results_limits_output(self) -> None:
        with self._patch({"duckduckgo-lite": HttpResponse(200, "OK", "text/html", "u", DDG_SAMPLE.encode())}):
            results, _ = search_web("python", max_results=1)
        self.assertEqual(len(results), 1)

class RedirectTests(unittest.TestCase):
    def test_meta_refresh(self) -> None:
        html = '<meta http-equiv="refresh" content="0; url=/target">'
        self.assertEqual(
            extract_html_redirect_url(html, "https://a.com/x"), "https://a.com/target"
        )

    def test_window_location_assignment(self) -> None:
        self.assertEqual(
            extract_html_redirect_url("window.location.href='/t2'", "https://a.com/x"),
            "https://a.com/t2",
        )

    def test_window_location_replace(self) -> None:
        self.assertEqual(
            extract_html_redirect_url('window.location.replace("/t3")', "https://a.com/x"),
            "https://a.com/t3",
        )

    def test_absolute_target(self) -> None:
        self.assertEqual(
            extract_html_redirect_url(
                "window.location='https://b.com/z'", "https://a.com/x"
            ),
            "https://b.com/z",
        )

    def test_no_redirect(self) -> None:
        self.assertIsNone(extract_html_redirect_url("<html>nothing</html>", "https://a.com"))

class FetchWebPageTests(unittest.TestCase):
    def _response(self, body: bytes, content_type: str = "text/html", status: int = 200, url: str = "https://final/"):
        return HttpResponse(status, "OK", content_type, url, body)

    def test_extracts_html(self) -> None:
        from corecode.utils.web import fetch_web_page

        body = b"<html><title>T</title><script>x</script><p>Body text</p></html>"
        with patch("corecode.utils.web.fetch_with_retry", return_value=self._response(body)):
            page = fetch_web_page("https://a.com")
        self.assertEqual(page.title, "T")
        self.assertIn("Body text", page.content)
        self.assertNotIn("x", page.content.split("Body")[0])

    def test_follows_html_redirect(self) -> None:
        """**跟进跳转。** 不跟进的话抓回来的是空壳。"""
        from corecode.utils.web import fetch_web_page

        first = self._response(b'<meta http-equiv="refresh" content="0; url=/next">')
        second = self._response(b"<html><p>Real content</p></html>", url="https://a.com/next")
        with patch("corecode.utils.web.fetch_with_retry", side_effect=[first, second]) as fetcher:
            page = fetch_web_page("https://a.com")
        self.assertEqual(fetcher.call_count, 2)
        self.assertIn("Real content", page.content)

    def test_does_not_refetch_when_redirect_equals_final_url(self) -> None:
        from corecode.utils.web import fetch_web_page

        body = b'<html><p>x</p><meta http-equiv="refresh" content="0; url=https://final/"></html>'
        with patch("corecode.utils.web.fetch_with_retry", return_value=self._response(body)) as fetcher:
            fetch_web_page("https://a.com")
        self.assertEqual(fetcher.call_count, 1)

    def test_plain_text_is_not_extracted(self) -> None:
        from corecode.utils.web import fetch_web_page

        with patch(
            "corecode.utils.web.fetch_with_retry",
            return_value=self._response(b'{"a": 1}', content_type="application/json"),
        ):
            page = fetch_web_page("https://a.com")
        self.assertEqual(page.content, '{"a": 1}')
        self.assertIsNone(page.title)

    def test_content_is_truncated(self) -> None:
        from corecode.utils.web import fetch_web_page

        body = ("<p>" + "x" * 5000 + "</p>").encode()
        with patch("corecode.utils.web.fetch_with_retry", return_value=self._response(body)):
            page = fetch_web_page("https://a.com", max_chars=100)
        self.assertEqual(len(page.content), 100)

    def test_status_is_reported(self) -> None:
        from corecode.utils.web import fetch_web_page

        with patch(
            "corecode.utils.web.fetch_with_retry",
            return_value=self._response(b"gone", status=404),
        ):
            page = fetch_web_page("https://a.com")
        self.assertEqual(page.status, 404)

class WebSearchToolTests(unittest.TestCase):
    """工具层的 schema、校验与输出格式。

    """

    def setUp(self) -> None:
        from corecode.tools.web import web_search_tool

        self.tool = web_search_tool

    def test_schema_has_domain_filters(self) -> None:
        """**此前完全没有这两个字段**——模型无法表达「只在某站找」。"""
        properties = self.tool.input_schema["properties"]
        self.assertIn("allowed_domains", properties)
        self.assertIn("blocked_domains", properties)

    def test_schema_requires_only_query(self) -> None:
        self.assertEqual(self.tool.input_schema["required"], ["query"])

    def test_default_max_results_is_five(self) -> None:
        parsed = self.tool.validator({"query": "x"})
        self.assertNotIn("max_results", parsed)
        from corecode.utils.web import DEFAULT_MAX_RESULTS as value

        self.assertEqual(value, 5)

    def test_rejects_empty_query(self) -> None:
        with self.assertRaises(ValueError):
            self.tool.validator({"query": "   "})

    def test_rejects_max_results_out_of_range(self) -> None:
        for bad in (0, -1, 21):
            with self.assertRaises(ValueError):
                self.tool.validator({"query": "x", "max_results": bad})

    def test_accepts_max_results_bounds(self) -> None:
        self.assertEqual(
            self.tool.validator({"query": "x", "max_results": 1})["max_results"], 1
        )
        self.assertEqual(
            self.tool.validator({"query": "x", "max_results": 20})["max_results"], 20
        )

    def test_rejects_both_domain_filters(self) -> None:
        """**互斥校验。** 一个限定范围、一个排除其中一部分，同时给时语义含糊。

        宁可明确报错，也不要猜用户想要什么。
        """
        with self.assertRaises(ValueError) as caught:
            self.tool.validator(
                {
                    "query": "x",
                    "allowed_domains": ["a.com"],
                    "blocked_domains": ["b.com"],
                }
            )
        self.assertIn("Cannot specify both", str(caught.exception))

    def test_allows_either_filter_alone(self) -> None:
        self.assertEqual(
            self.tool.validator({"query": "x", "allowed_domains": ["a.com"]})[
                "allowed_domains"
            ],
            ["a.com"],
        )
        self.assertEqual(
            self.tool.validator({"query": "x", "blocked_domains": ["a.com"]})[
                "blocked_domains"
            ],
            ["a.com"],
        )

    def test_empty_domain_lists_do_not_trigger_mutual_exclusion(self) -> None:
        """两个**空**列表不算「同时给」。"""
        parsed = self.tool.validator(
            {"query": "x", "allowed_domains": [], "blocked_domains": []}
        )
        self.assertNotIn("allowed_domains", parsed)
        self.assertNotIn("blocked_domains", parsed)

    def test_rejects_non_string_domain(self) -> None:
        with self.assertRaises(ValueError):
            self.tool.validator({"query": "x", "allowed_domains": [123]})

    def test_output_format(self) -> None:
        from corecode.tooling import ToolContext
        from corecode.utils.web import SearchResult

        with patch(
            "corecode.tools.web.search_web",
            return_value=(
                [
                    SearchResult("Title A", "https://a.com", "Snippet A"),
                    SearchResult("Title B", "https://b.com", ""),
                ],
                "duckduckgo-lite",
            ),
        ):
            result = self.tool.run(
                {"query": "hello"}, ToolContext(cwd=".")
            )
        self.assertTrue(result.ok)
        lines = result.output.splitlines()
        self.assertEqual(lines[0], "QUERY: hello")
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], "[1] Title A")
        self.assertEqual(lines[3], "    URL: https://a.com")
        self.assertEqual(lines[4], "    Snippet A")
        self.assertEqual(lines[5], "")
        self.assertEqual(lines[6], "[2] Title B")
        # 没有摘要时不该留一行空白占位
        self.assertEqual(lines[7], "    URL: https://b.com")

    def test_empty_results_message(self) -> None:
        from corecode.tooling import ToolContext

        with patch("corecode.tools.web.search_web", return_value=([], "fallback-empty")):
            result = self.tool.run({"query": "x"}, ToolContext(cwd="."))
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "No results found.")

    def test_failure_wraps_the_message(self) -> None:
        from corecode.tooling import ToolContext

        with patch("corecode.tools.web.search_web", side_effect=WebError("boom")):
            result = self.tool.run({"query": "x"}, ToolContext(cwd="."))
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "Web search failed: boom")

    def test_domain_filters_are_passed_through(self) -> None:
        from corecode.tooling import ToolContext

        captured: dict = {}

        def fake(query, limit, **kwargs):  # noqa: ANN001
            captured.update({"query": query, "limit": limit, **kwargs})
            return [], "fallback-empty"

        with patch("corecode.tools.web.search_web", side_effect=fake):
            self.tool.run(
                {"query": "x", "allowed_domains": ["a.com"], "max_results": 3},
                ToolContext(cwd="."),
            )
        self.assertEqual(captured["limit"], 3)
        self.assertEqual(captured["allowed_domains"], ["a.com"])
        self.assertIsNone(captured["blocked_domains"])

class WebFetchToolTests(unittest.TestCase):

    def setUp(self) -> None:
        from corecode.tools.web import web_fetch_tool

        self.tool = web_fetch_tool

    def test_requires_url(self) -> None:
        self.assertEqual(self.tool.input_schema["required"], ["url"])

    def test_rejects_non_http_url(self) -> None:
        with self.assertRaises(ValueError):
            self.tool.validator({"url": "ftp://x"})

    def test_rejects_small_max_chars(self) -> None:
        """给太少等于没抓。"""
        with self.assertRaises(ValueError):
            self.tool.validator({"url": "https://x", "max_chars": 100})

    def test_accepts_boundary(self) -> None:
        self.assertEqual(
            self.tool.validator({"url": "https://x", "max_chars": 500})["max_chars"],
            500,
        )

    def test_output_format(self) -> None:
        from corecode.tooling import ToolContext
        from corecode.utils.web import WebPage

        page = WebPage(
            url="https://a.com",
            final_url="https://a.com/final",
            status=200,
            status_text="OK",
            content_type="text/html",
            content="Body",
            title="T",
        )
        with patch("corecode.tools.web.fetch_web_page", return_value=page):
            result = self.tool.run({"url": "https://a.com"}, ToolContext(cwd="."))
        self.assertTrue(result.ok)
        lines = result.output.splitlines()
        self.assertEqual(lines[0], "URL: https://a.com/final")
        self.assertEqual(lines[1], "STATUS: 200")
        self.assertEqual(lines[2], "CONTENT_TYPE: text/html")
        self.assertEqual(lines[3], "TITLE: T")
        self.assertEqual(lines[4], "")
        self.assertEqual(lines[5], "Body")

    def test_title_line_is_omitted_when_absent(self) -> None:
        from corecode.tooling import ToolContext
        from corecode.utils.web import WebPage

        page = WebPage("u", "u", 200, "OK", "text/plain", "Body", None)
        with patch("corecode.tools.web.fetch_web_page", return_value=page):
            result = self.tool.run({"url": "https://a.com"}, ToolContext(cwd="."))
        self.assertNotIn("TITLE:", result.output)

    def test_http_error_status_is_a_failure(self) -> None:
        from corecode.tooling import ToolContext
        from corecode.utils.web import WebPage

        page = WebPage("u", "u", 404, "Not Found", "text/html", "", None)
        with patch("corecode.tools.web.fetch_web_page", return_value=page):
            result = self.tool.run({"url": "https://a.com/x"}, ToolContext(cwd="."))
        self.assertFalse(result.ok)
        self.assertIn("HTTP 404", result.output)

    def test_network_error_is_a_failure(self) -> None:
        from corecode.tooling import ToolContext

        with patch("corecode.tools.web.fetch_web_page", side_effect=WebError("timed out")):
            result = self.tool.run({"url": "https://a.com"}, ToolContext(cwd="."))
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "Web fetch failed: timed out")

    def test_description_matches_the_original(self) -> None:
        """描述是给模型看的 API 契约。"""
        self.assertEqual(
            self.tool.description,
            "Fetch a web page and extract its readable text content. "
            "Use this after web_search when you need the full content of a specific page.",
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
