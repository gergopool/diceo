"""URL inputs use the ordinary readers and the same DiceoError contract."""

import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import diceo
from diceo.__main__ import main


@pytest.fixture(scope="module")
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/download.php"):
                body = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.ms-excel")
                if "filename" in self.path:
                    self.send_header("Content-Disposition", 'attachment; filename="report.xls"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/page.php")
                self.end_headers()
                return
            if self.path == "/bad-redirect":
                self.send_response(302)
                self.send_header("Location", "file:///unused")
                self.end_headers()
                return
            if self.path == "/missing":
                self.send_error(404)
                return
            body = b"<main><header><h1>Caf\xe9</h1></header><p>Revenue is 42.</p></main>"
            if self.path == "/bom":
                body = b"\xef\xbb\xbf" + body.decode("iso-8859-1").encode("utf-8")
            self.send_response(200)
            charset = "rot_13" if self.path == "/bad-charset" else "iso-8859-1"
            self.send_header("Content-Type", f"text/html; charset={charset}")
            if self.path == "/gzip":
                body = gzip.compress(body)
                self.send_header("Content-Encoding", "gzip")
            if self.path == "/gzip-empty-members":
                body = gzip.compress(b"") * 100
                self.send_header("Content-Encoding", "gzip")
            else:
                self.send_header(
                    "Content-Length", str(len(body) + (100 if self.path == "/truncated" else 0))
                )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("query", ["?filename=1", "?year=2018"])
def test_url_format_hints_override_an_endpoint_suffix(site, query):
    assert diceo.sniff(site + "/download.php" + query) == "xls"


def test_url_options_preserve_positional_limits():
    limits = diceo.Limits(1800, None, 0, 0, None, None, None, None, 0, False, False)
    assert limits.reopen_every == 0
    assert limits.detect_tables is False
    assert limits.suppress_furniture is False
    assert limits.download_timeout == 30.0


@pytest.mark.parametrize("path", ["/page.php", "/redirect", "/gzip", "/bom"])
def test_url_charset_redirect_and_compression(site, path):
    report = diceo.Diagnostics()
    chunks = list(diceo.chunk(site + path, diagnostics=report))
    assert "Café" in chunks[0].text and "Revenue is 42." in chunks[0].text
    assert chunks[0].meta["source_url"] == site + ("/page.php" if path == "/redirect" else path)
    assert report.chunks == len(chunks)
    assert report.chars == sum(len(c.text) for c in chunks)
    assert not report.lost_data
    assert diceo.sniff(site + path) == "html"
    assert next(diceo.extract(site + path)).text == "Café"


@pytest.mark.parametrize("path", ["/missing", "/truncated", "/bad-redirect", "/bad-charset"])
def test_url_failures_are_diceo_errors(site, path):
    with pytest.raises(diceo.DiceoError):
        diceo.chunk(site + path)


def test_url_body_limit_applies_to_chunk_extract_and_sniff(site):
    for reader in (diceo.chunk, diceo.extract, diceo.sniff):
        with pytest.raises(diceo.DiceoError, match="max_download_bytes"):
            reader(site + "/page.php", limits=diceo.Limits(max_download_bytes=10))
    with pytest.raises(diceo.DiceoError, match="max_download_bytes"):
        diceo.chunk(site + "/gzip", limits=diceo.Limits(max_download_bytes=50))
    # Empty concatenated members produce no decoded bytes; the encoded stream must
    # still be bounded when its HTTP response has no declared length.
    with pytest.raises(diceo.DiceoError, match="max_download_bytes"):
        diceo.chunk(site + "/gzip-empty-members", limits=diceo.Limits(max_download_bytes=50))


def test_url_cli_keeps_real_text_and_failure_priority(site, capsys):
    assert main([site + "/page.php", "--meta", "text=REPLACED"]) == 0
    output = capsys.readouterr().out
    assert "Revenue is 42." in output and '"text":"REPLACED"' not in output
    assert main([site + "/missing", site + "/page.php", "--count"]) == 1


@pytest.mark.parametrize("missing_first", [True, False])
def test_cli_failure_has_priority_over_incomplete_document(tmp_path, capsys, missing_first):
    document = tmp_path / "rows.csv"
    document.write_text("name,value\nA,42\nB,17\n")
    paths = [str(tmp_path / "missing.csv"), str(document)]
    if not missing_first:
        paths.reverse()
    assert main([*paths, "--max-rows", "1", "--count"]) == 1
    assert "max_rows=1" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    ["http://", "http://[broken", "http://example.org:bad/", "http://user:pass@example.org/"],
)
def test_invalid_url_fails_before_download(url):
    with pytest.raises(diceo.DiceoError):
        diceo.chunk(url)
