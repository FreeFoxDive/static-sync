import json
import os
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import httpx

from app.config_loader import ConfigError, JobGroup, Schedule, Source, SyncSettings, load_config
from app.http_server import serve
from app.scheduler import build_scheduler
from app.security.path import PathEscapeError, safe_join
from app.security.ssrf import SsrfError, assert_public_http_url, resolve_public_http_url
from app.status import StatusStore
from app.sync.atomic import atomic_write
from app.sync.download import DownloadError, download_to_file
from app.sync.github import sync_github
from app.sync.runner import run_group
from app.sync.site import _join_same_origin, sync_site

ROOT = Path(__file__).resolve().parents[2]


def settings(tmp: Path) -> SyncSettings:
    return SyncSettings(
        data_dir=tmp / "public",
        state_dir=tmp / "state",
        timeout_seconds=5,
        max_bytes=1024,
        max_redirects=3,
        user_agent="test",
    )


def group_for(source: Source, name: str = "box") -> JobGroup:
    return JobGroup(
        name=name,
        enabled=True,
        output_dir=name,
        schedule=Schedule("0 5 * * *", "Asia/Shanghai", True),
        sources=[source],
    )


class SecurityTests(unittest.TestCase):
    def test_blocks_private_and_metadata_literals(self):
        for url in (
            "http://127.0.0.1/secret",
            "http://10.1.2.3/a",
            "http://192.168.1.1/a",
            "http://169.254.169.254/latest",
            "http://[::1]/a",
            "file:///etc/passwd",
            "http://user:pass@1.1.1.1/a",
            "http://localhost/a",
            "http://metadata.google.internal/computeMetadata/v1/",
        ):
            with self.assertRaises(SsrfError):
                assert_public_http_url(url)

    def test_blocks_public_name_resolving_private(self):
        with patch("app.security.ssrf.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.1.1.1", 0))]):
            with self.assertRaises(SsrfError):
                assert_public_http_url("https://example.test/a")

    def test_allows_public_literal(self):
        assert_public_http_url("https://1.1.1.1/geosite-cn.srs")

    def test_pins_hostname_to_resolved_public_ip(self):
        with patch(
            "app.security.ssrf.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("1.1.1.1", 443))],
        ):
            resolved = resolve_public_http_url("https://rules.example/geo.srs")
        self.assertEqual(resolved.ip, "1.1.1.1")
        self.assertEqual(resolved.pinned_url, "https://1.1.1.1/geo.srs")
        self.assertEqual(resolved.host_header, "rules.example")
        self.assertEqual(resolved.sni_hostname, "rules.example")

    def test_path_escape(self):
        root = Path("public")
        with self.assertRaises(PathEscapeError):
            safe_join(root, "../secret")
        with self.assertRaises(PathEscapeError):
            safe_join(root, "/etc/passwd")


class DownloadTests(unittest.TestCase):
    def test_atomic_replace_and_keep_old_on_failure(self):
        dest = Path(self.id().replace(".", "_") + ".bin")
        try:
            atomic_write(dest, [b"good"], 100)
            self.assertEqual(dest.read_bytes(), b"good")
            with self.assertRaises(Exception):
                atomic_write(dest, [b"x" * 50], 10)
            self.assertEqual(dest.read_bytes(), b"good")
        finally:
            dest.unlink(missing_ok=True)
            parent_parts = dest.parent.glob(".tmp-*.part")
            for leftover in parent_parts:
                leftover.unlink(missing_ok=True)

    def test_redirect_to_private_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "1.1.1.1":
                return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
            return httpx.Response(200, content=b"nope")

        dest = Path(self.id().replace(".", "_") + ".out")
        try:
            with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
                with self.assertRaises(DownloadError):
                    download_to_file(
                        client,
                        "https://1.1.1.1/file",
                        dest,
                        max_bytes=100,
                        max_redirects=2,
                        user_agent="test",
                    )
            self.assertFalse(dest.exists())
        finally:
            dest.unlink(missing_ok=True)

    def test_connects_to_pinned_ip_not_rebinding_hostname(self):
        seen: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.host, request.headers.get("host")))
            return httpx.Response(200, content=b"ok")

        dest = Path(self.id().replace(".", "_") + ".out")
        try:
            with patch(
                "app.security.ssrf.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("1.1.1.1", 443))],
            ):
                with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
                    download_to_file(
                        client,
                        "https://evil.example/file.srs",
                        dest,
                        max_bytes=100,
                        max_redirects=1,
                        user_agent="test",
                    )
            self.assertEqual(seen, [("1.1.1.1", "evil.example")])
            self.assertEqual(dest.read_bytes(), b"ok")
        finally:
            dest.unlink(missing_ok=True)


class StatusStoreTests(unittest.TestCase):
    def test_init_group_preserves_previous_results(self):
        tmp = Path(self.id().replace(".", "_") + "_data")
        path = tmp / "state" / "status.json"
        try:
            first = StatusStore(path)
            first.init_group(
                "sing-box",
                enabled=True,
                output_dir="sing-box",
                cron="0 5 * * *",
                timezone_name="Asia/Shanghai",
            )
            first.mark_started("sing-box", "2026-01-01T00:00:00+00:00")
            first.mark_source(
                "sing-box",
                "geosite-cn",
                ok=True,
                dest="sing-box/geosite-cn.srs",
                bytes_written=12,
                error=None,
                skipped=False,
                finished_at="2026-01-01T00:00:01+00:00",
            )
            first.mark_finished("sing-box", "2026-01-01T00:00:01+00:00", True)

            second = StatusStore(path)
            second.init_group(
                "sing-box",
                enabled=True,
                output_dir="sing-box",
                cron="0 6 * * *",
                timezone_name="Asia/Shanghai",
            )
            second.retain_groups({"sing-box"})
            snap = second.snapshot()["groups"]["sing-box"]
            self.assertEqual(snap["cron"], "0 6 * * *")
            self.assertTrue(snap["ok"])
            self.assertEqual(snap["last_finished_at"], "2026-01-01T00:00:01+00:00")
            self.assertEqual(snap["sources"]["geosite-cn"]["bytes"], 12)
        finally:
            _rmtree(tmp)


class ConfigTests(unittest.TestCase):
    def test_repo_config_loads_groups(self):
        env = os.environ.copy()
        for key in ("DATA_DIR", "STATE_DIR", "BIND", "PORT"):
            env.pop(key, None)
        with patch.dict(os.environ, env, clear=True):
            config = load_config(ROOT / "config")
        names = {group.name: group.enabled for group in config.groups}
        self.assertEqual(names["sing-box"], True)
        self.assertEqual(names["site-example"], False)
        sing = next(group for group in config.groups if group.name == "sing-box")
        self.assertEqual(sing.schedule.cron, "0 5 * * *")
        self.assertEqual(sing.schedule.timezone, "Asia/Shanghai")
        self.assertTrue(sing.schedule.run_on_startup)
        self.assertEqual(sing.output_dir, "sing-box")
        self.assertIn("geosite-cn.srs", {source.dest for source in sing.sources})

    def test_rejects_escaping_dest(self):
        tmp = Path(self.id().replace(".", "_") + "_cfg")
        tmp.mkdir()
        try:
            (tmp / "defaults.yaml").write_text(
                "sync:\n  data_dir: data/public\n  state_dir: data/state\n",
                encoding="utf-8",
            )
            (tmp / "bad.yaml").write_text(
                "sources:\n  - id: bad\n    type: url\n    url: https://1.1.1.1/a\n    dest: ../secret\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(tmp)
        finally:
            for path in tmp.glob("*"):
                path.unlink()
            tmp.rmdir()

    def test_scheduler_skips_disabled_group(self):
        env = os.environ.copy()
        for key in ("DATA_DIR", "STATE_DIR", "BIND", "PORT"):
            env.pop(key, None)
        with patch.dict(os.environ, env, clear=True):
            config = load_config(ROOT / "config")
        scheduler = build_scheduler(config, lambda name: None)
        self.assertEqual([job.id for job in scheduler.get_jobs()], ["sing-box"])


class SyncTests(unittest.TestCase):
    def test_url_sync_writes_without_waiting_for_a_client(self):
        tmp = Path(self.id().replace(".", "_") + "_data")
        cfg = settings(tmp)
        store = StatusStore(cfg.state_dir / "status.json")
        source = Source(
            id="geosite-cn",
            enabled=True,
            type="url",
            dest="geosite-cn.srs",
            url="https://1.1.1.1/geosite-cn.srs",
        )
        job = group_for(source)
        store.init_group(job.name, enabled=True, output_dir=job.output_dir, cron=job.schedule.cron, timezone_name="Asia/Shanghai")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"srs-data")

        real = httpx.Client

        def fake_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        try:
            with patch("app.sync.runner.httpx.Client", fake_client):
                run_group(job, cfg, store)
            written = cfg.data_dir / "box" / "geosite-cn.srs"
            self.assertEqual(written.read_bytes(), b"srs-data")
            snap = store.snapshot()
            self.assertTrue(snap["groups"]["box"]["ok"])
            self.assertEqual(snap["groups"]["box"]["sources"]["geosite-cn"]["dest"], "box/geosite-cn.srs")
            self.assertNotIn("1.1.1.1", json.dumps(snap["groups"]["box"]["sources"]))
        finally:
            _rmtree(tmp)

    def test_failed_download_keeps_previous_file(self):
        tmp = Path(self.id().replace(".", "_") + "_data")
        cfg = settings(tmp)
        store = StatusStore(cfg.state_dir / "status.json")
        source = Source(id="geo", enabled=True, type="url", dest="geoip-cn.srs", url="http://127.0.0.1/geoip-cn.srs")
        job = group_for(source)
        store.init_group(job.name, enabled=True, output_dir="box", cron="0 5 * * *", timezone_name="Asia/Shanghai")
        target = cfg.data_dir / "box"
        target.mkdir(parents=True)
        (target / "geoip-cn.srs").write_bytes(b"old")
        try:
            run_group(job, cfg, store)
            self.assertEqual((target / "geoip-cn.srs").read_bytes(), b"old")
            self.assertFalse(store.snapshot()["groups"]["box"]["ok"])
        finally:
            _rmtree(tmp)

    def test_github_include_filter(self):
        tmp = Path(self.id().replace(".", "_") + "_data")
        cfg = settings(tmp)
        source = Source(
            id="meta",
            enabled=True,
            type="github",
            dest=".",
            repo="example/rules",
            ref="sing",
            path="geo",
            include=["*.srs"],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.github.com":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "type": "file",
                            "name": "cn.srs",
                            "path": "geo/cn.srs",
                            "size": 3,
                            "download_url": "https://1.1.1.1/cn.srs",
                        },
                        {
                            "type": "file",
                            "name": "skip.js",
                            "path": "geo/skip.js",
                            "size": 2,
                            "download_url": "https://1.1.1.1/skip.js",
                        },
                    ],
                )
            return httpx.Response(200, content=b"srs")

        try:
            with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
                with patch("app.sync.github.assert_public_http_url", lambda _url: None):
                    sync_github(client, source, cfg.data_dir, cfg)
            self.assertEqual((cfg.data_dir / "cn.srs").read_bytes(), b"srs")
            self.assertFalse((cfg.data_dir / "skip.js").exists())
        finally:
            _rmtree(tmp)

    def test_site_paths_stay_on_origin(self):
        with self.assertRaises(DownloadError):
            _join_same_origin("https://1.1.1.1/", "../secret")
        with self.assertRaises(DownloadError):
            _join_same_origin("https://1.1.1.1/", "css/%2e%2e/secret")
        self.assertEqual(_join_same_origin("https://1.1.1.1/", "css/app.css"), "https://1.1.1.1/css/app.css")

    def test_site_downloads_listed_paths_only(self):
        tmp = Path(self.id().replace(".", "_") + "_data")
        cfg = settings(tmp)
        source = Source(
            id="assets",
            enabled=True,
            type="site",
            dest=".",
            base_url="https://1.1.1.1/",
            paths=["css/app.css"],
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"body{color:black}")

        try:
            with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
                sync_site(client, source, cfg.data_dir, cfg)
            self.assertEqual((cfg.data_dir / "css" / "app.css").read_bytes(), b"body{color:black}")
        finally:
            _rmtree(tmp)


class StatusHttpTests(unittest.TestCase):
    def test_health_and_status_are_readonly(self):
        tmp = Path(self.id().replace(".", "_") + "_data")
        store = StatusStore(tmp / "state" / "status.json")
        store.init_group("sing-box", enabled=True, output_dir="sing-box", cron="0 5 * * *", timezone_name="Asia/Shanghai")
        server = serve("127.0.0.1", 0, store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read().decode()), {"ok": True})
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as response:
                body = json.loads(response.read().decode())
                self.assertIn("sing-box", body["groups"])
            request = urllib.request.Request(f"http://127.0.0.1:{port}/sing-box/geosite-cn.srs", method="GET")
            try:
                urllib.request.urlopen(request, timeout=3).close()
                self.fail("static path should not be served by syncer")
            except HTTPError as exc:
                self.assertEqual(exc.code, 404)
                exc.close()
        finally:
            server.shutdown()
            server.server_close()
            _rmtree(tmp)


def _rmtree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            child.rmdir()
    path.rmdir()


if __name__ == "__main__":
    unittest.main()
