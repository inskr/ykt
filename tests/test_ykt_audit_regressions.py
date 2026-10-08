"""Offline regressions for the issues found during the contribution review."""

import contextlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from test_ykt_progress import OfflineResponse, OfflineSession, mp4_fixture, progress_sequence


SCRIPT = Path(__file__).resolve().parents[1] / 'ykt.py'
MAIN = 'https://www.yuketang.cn'
SCHOOL = 'https://xidianyjs.yuketang.cn'


def load_course(session):
    # Load the complete module; reject unexpected login before any real connection.
    namespace = {'__file__': str(SCRIPT), '__name__': 'offline_ykt'}
    with patch('websockets.asyncio.client.connect',
               side_effect=AssertionError('login started on import')):
        exec(compile(SCRIPT.read_text(encoding='utf-8-sig'), str(SCRIPT), 'exec'), namespace)
    namespace.update(session=session, input=lambda _: '0',
                     time=SimpleNamespace(time=lambda: 1700000000, sleep=lambda _: None))
    return namespace


def process_course(session):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        load_course(session)['ssxx']()
    return output.getvalue()


class RetryFailureSession(OfflineSession):
    def __init__(self, failure):
        super().__init__(
            {'101': [{'data': {'101': {'completed': 0}}}],
             '102': progress_sequence('102')},
            media_by_id={'101': {'ccid': 'test-first', 'duration': 0}},
            content_info=[{'section_list': [{'leaf_list': [{'id': '101'}, {'id': '102'}]}]}])
        self.failure = failure
        self.retry_options = []

    def get(self, url, **kwargs):
        if '/leaf_info/' in url and url.rstrip('/').endswith('/101') and '101' in self.leaf_requests:
            self.leaf_requests.append('101')
            self.retry_options.append(kwargs)
            if self.failure == 'timeout':
                raise requests.Timeout('offline failure')
            if self.failure == 'html':
                return OfflineResponse('<html>gateway failure</html>', 502)
            if self.failure == 'empty-data':
                return OfflineResponse(json.dumps({'data': {}}))
            return OfflineResponse(json.dumps({'data': {'id': '101', 'user_id': 1,
                'content_info': {'media': {'ccid': 'bad-data', 'duration': 60}}}}), 500)
        return super().get(url, **kwargs)


class CourseBoundaryTests(unittest.TestCase):
    def test_chapter_direct_video_is_processed(self):
        session = OfflineSession({'101': progress_sequence('101')},
            content_info=[{'section_list': [], 'leaf_list': [{'id': '101'}]}])
        process_course(session)
        self.assertEqual([item['v'] for item in session.heartbeats], [101])

    def test_video_repeated_in_section_and_chapter_is_processed_once(self):
        session = OfflineSession({'101': progress_sequence('101'), '102': progress_sequence('102')},
            content_info=[{'section_list': [{'leaf_list': [{'id': '101'}]}],
                           'leaf_list': [{'id': '101'}, {'id': '102'}]}])
        process_course(session)
        self.assertEqual(session.leaf_requests, ['101', '102'])
        self.assertEqual([item['v'] for item in session.heartbeats], [101, 102])

    def test_failed_duration_retries_continue_to_the_next_video(self):
        for failure in ('timeout', 'html', 'empty-data'):
            with self.subTest(failure=failure):
                session = RetryFailureSession(failure)
                try:
                    output = process_course(session)
                except (requests.RequestException, ValueError, KeyError) as error:
                    self.fail(f'duration retry aborted the course: {type(error).__name__}')
                self.assertEqual([item['v'] for item in session.heartbeats], [102])
                self.assertIn('未能确认', output)
                self.assertNotIn('这门课看完', output)
                self.assertTrue(all(options.get('timeout') for options in session.retry_options))

    def test_error_response_cannot_supply_a_video_duration(self):
        session = RetryFailureSession('http-error')
        output = process_course(session)
        self.assertEqual([item['v'] for item in session.heartbeats], [102])
        self.assertIn('未能确认', output)

    def test_school_fallback_works_without_a_fixed_classroom_id(self):
        class CloudSession(OfflineSession):
            def get(self, url, **kwargs):
                if '/leaf_info/' in url and urlsplit(url).hostname == 'www.yuketang.cn':
                    return OfflineResponse('{}', 404)
                return super().get(url, **kwargs)
        session = CloudSession({'101': progress_sequence('101')}, classroom=7,
            content_info=[{'section_list': [{'leaf_list': [{'id': '101'}]}]}])
        output = process_course(session)
        self.assertEqual([item['v'] for item in session.heartbeats], [101])
        self.assertTrue(session.heartbeat_requests[0][0].startswith(SCHOOL + '/'))
        self.assertNotIn('未能确认', output)

    def test_import_does_not_start_qr_login(self):
        with patch('websockets.asyncio.client.connect',
                   side_effect=AssertionError('login started on import')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                runpy.run_path(str(SCRIPT), run_name='offline_import')
            except AssertionError as error:
                self.fail(str(error))


class MediaOriginTests(unittest.TestCase):
    def test_main_relative_media_url_uses_the_main_origin(self):
        url = MAIN + '/media/video.mp4'
        class MainMediaSession(OfflineSession):
            def get(self, url, **kwargs):
                if url.endswith('/media/video.mp4') and url not in self.media_files:
                    return OfflineResponse('missing', 404)
                return super().get(url, **kwargs)
        progress = [{'data': {'101': {'completed': 0}}}] * 3 + [
            {'data': {'101': {'completed': 1}}}]
        session = MainMediaSession({'101': progress}, duration=0,
            content_info=[{'section_list': [{'leaf_list': [{'id': '101'}]}]}],
            media_by_id={'101': {'ccid': 'test-video', 'duration': 0,
                                 'playurl': '/media/video.mp4'}},
            media_files={url: mp4_fixture(60)})
        output = process_course(session)
        self.assertEqual([item['d'] for item in session.heartbeats], [60])
        self.assertNotIn('未能确认', output)

    def test_fallback_playback_api_receives_its_own_origin_headers(self):
        class HeaderCheckedSession(OfflineSession):
            def get(self, url, **kwargs):
                if '/leaf_info/' in url and url.startswith(MAIN):
                    return OfflineResponse('{}', 404)
                if '/api/open/audiovideo/playurl' in url:
                    headers = kwargs.get('headers', {})
                    if url.startswith(MAIN) and headers.get('xtbz') == 'ykt' and headers.get('Referer') == MAIN + '/':
                        return OfflineResponse(json.dumps({'success': True, 'data': {'duration': 60}}))
                    return OfflineResponse(json.dumps({'success': False, 'data': {}}))
                return super().get(url, **kwargs)
        progress = [{'data': {'101': {'completed': 0}}}] * 3 + [
            {'data': {'101': {'completed': 1}}}]
        session = HeaderCheckedSession({'101': progress}, duration=0,
            classroom=33356219,
            content_info=[{'section_list': [{'leaf_list': [{'id': '101'}]}]}])
        process_course(session)
        self.assertEqual([item['d'] for item in session.heartbeats], [60])

    def test_redirected_hls_master_uses_its_final_url(self):
        from ykt_video_metadata import read_playback_duration
        class RedirectSession:
            def get(self, url, **kwargs):
                if url == 'https://media.example/master.m3u8':
                    response = OfflineResponse('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n')
                    response.url = 'https://cdn.example/path/master.m3u8'
                    return response
                if url == 'https://cdn.example/path/low/index.m3u8':
                    return OfflineResponse('#EXTM3U\n#EXTINF:5.25,\na.ts\n#EXTINF:7.5,\nb.ts\n#EXT-X-ENDLIST\n')
                if '/api/open/audiovideo/playurl' in url:
                    return OfflineResponse(json.dumps({'success': False, 'data': {}}))
                return OfflineResponse('missing', 404)
        duration, _ = read_playback_duration(RedirectSession(),
            {'ccid': 'test-video', 'playurl': 'https://media.example/master.m3u8'}, (MAIN,), {})
        self.assertEqual(duration, 12.75)

    def test_oversized_playback_api_response_is_rejected_and_closed(self):
        from ykt_video_metadata import read_playback_duration
        class LargeResponse(OfflineResponse):
            closed = False
            def close(self):
                self.closed = True
        response = LargeResponse(json.dumps({'data': {'duration': 60}, 'padding': 'x' * (2 * 1024 * 1024)}))
        session = SimpleNamespace(get=lambda *args, **kwargs: response)
        duration, reason = read_playback_duration(session, {'ccid': 'test-video'}, (MAIN,), {})
        self.assertEqual(duration, 0)
        self.assertTrue(response.closed)
        self.assertIn('超过', reason)


if __name__ == '__main__':
    unittest.main()
