import ast
import asyncio
import base64
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import url2pathname


SCRIPT = Path(__file__).resolve().parents[1] / "ykt.py"
PNG_DATA = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
    "QVR42mP8/x8AAwMCAO+jzFcAAAAASUVORK5CYII=")


class QRResponse:
    status_code = 200
    content = PNG_DATA

    def __iter__(self):
        return iter((self.content,))


class OfflineWebSocket:
    def __init__(self):
        self.messages = iter([
            json.dumps({"ticket": "https://example.invalid/qr"}),
            json.dumps({"subscribe_status": True, "Auth": "test", "UserID": 1}),
        ])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        pass

    async def recv(self):
        return next(self.messages)


class QRCodeRegressionTests(unittest.TestCase):
    def run_login(self, project_dir, cwd):
        opened_urls = []
        course_calls = []
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8-sig"))
        functions = [node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        namespace = {
            "__file__": str(project_dir / "ykt.py"),
            "json": json, "os": os, "Path": Path, "tempfile": tempfile,
            "connect": lambda *args, **kwargs: OfflineWebSocket(),
            "session": SimpleNamespace(
                get=lambda **kwargs: QRResponse(),
                post=lambda *args, **kwargs: None),
            "webbrowser": SimpleNamespace(
                open=lambda url: opened_urls.append(url) or True),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]),
                     str(SCRIPT), "exec"), namespace)
        namespace["ssxx"] = lambda: course_calls.append(True)
        output = io.StringIO()
        with contextlib.chdir(cwd), contextlib.redirect_stdout(output):
            asyncio.run(namespace["websocket_session"]())
        return opened_urls, course_calls, output.getvalue()

    def image_path(self, url):
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, "file")
        self.assertEqual(parsed.netloc, "")
        return Path(url2pathname(parsed.path))

    def test_rejected_old_relative_filename_does_not_abort_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            real_open = open

            def reject_old_target(file, *args, **kwargs):
                if file == "sunci.png":
                    raise OSError(errno.EINVAL, "Invalid argument", file)
                return real_open(file, *args, **kwargs)

            with patch("builtins.open", side_effect=reject_old_target):
                urls, course_calls, _ = self.run_login(project, project)
            self.assertEqual(course_calls, [True])
            self.assertEqual(self.image_path(urls[0]).read_bytes(), PNG_DATA)

    def test_qrcode_path_is_independent_of_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "脚本 目录"
            elsewhere = Path(tmp) / "elsewhere"
            project.mkdir()
            elsewhere.mkdir()
            urls, course_calls, _ = self.run_login(project, elsewhere)
            image_path = self.image_path(urls[0])
            self.assertEqual(image_path.parent, project)
            self.assertEqual(image_path.read_bytes(), PNG_DATA)
            self.assertEqual(course_calls, [True])
            self.assertFalse((elsewhere / "sunci.png").exists())

    def test_each_login_uses_a_new_image_and_preserves_old_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            old_image = project / "sunci.png"
            old_image.write_bytes(b"existing image")
            first, _, _ = self.run_login(project, project)
            second, _, _ = self.run_login(project, project)
            self.assertNotEqual(first[0], second[0])
            self.assertEqual(old_image.read_bytes(), b"existing image")
            self.assertEqual(self.image_path(first[0]).read_bytes(), PNG_DATA)
            self.assertEqual(self.image_path(second[0]).read_bytes(), PNG_DATA)

    def test_unwritable_project_falls_back_to_temporary_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            fallback = Path(tmp) / "fallback"
            project.mkdir()
            fallback.mkdir()
            real_temporary_file = tempfile.NamedTemporaryFile

            def deny_project(*args, **kwargs):
                if Path(kwargs["dir"]) == project:
                    raise PermissionError("project cannot be written")
                return real_temporary_file(*args, **kwargs)

            with patch.object(tempfile, "NamedTemporaryFile", side_effect=deny_project), \
                    patch.object(tempfile, "gettempdir", return_value=str(fallback)):
                urls, course_calls, _ = self.run_login(project, project)
            image_path = self.image_path(urls[0])
            self.assertEqual(image_path.parent, fallback)
            self.assertEqual(image_path.read_bytes(), PNG_DATA)
            self.assertEqual(course_calls, [True])

    def test_write_failure_reports_error_without_starting_course(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            with patch("builtins.open", side_effect=OSError(errno.EINVAL, "Invalid argument")), \
                    patch.object(tempfile, "NamedTemporaryFile",
                                 side_effect=OSError(errno.EINVAL, "Invalid argument")):
                urls, course_calls, output = self.run_login(project, project)
            self.assertEqual(urls, [])
            self.assertEqual(course_calls, [])
            self.assertIn("无法保存登录二维码", output)


if __name__ == "__main__":
    unittest.main()
