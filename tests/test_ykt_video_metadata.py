import struct
import unittest

import requests

from test_ykt_progress import OfflineResponse, OfflineSession, mp4_fixture
from ykt_video_metadata import read_playback_duration


ORIGINS = ('https://xidianyjs.yuketang.cn', 'https://www.yuketang.cn')


class PlaylistResponse(OfflineResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False
        self.bytes_read = 0

    def iter_content(self, chunk_size=8192):
        for chunk in super().iter_content(chunk_size):
            self.bytes_read += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


class PlaylistSession(OfflineSession):
    def __init__(self, resources, content_type='', supports_ranges=False):
        super().__init__([{}])
        self.resources = resources
        self.content_type = content_type
        self.supports_ranges = supports_ranges
        self.responses = []

    def get(self, url, **kwargs):
        if url not in self.resources:
            return super().get(url, **kwargs)
        self.playback_requests.append((url, kwargs))
        body, final_url = self.resources[url]
        body = body.encode('utf-8') if isinstance(body, str) else body
        headers = {'Content-Type': self.content_type}
        status = 200
        range_header = kwargs.get('headers', {}).get('Range')
        if self.supports_ranges and range_header:
            start, end = map(int, range_header.removeprefix('bytes=').split('-'))
            total = len(body)
            body = body[start:end + 1]
            headers['Content-Range'] = f'bytes {start}-{start + len(body) - 1}/{total}'
            status = 206
        response = PlaylistResponse(body, status, headers)
        response.url = final_url or url
        self.responses.append(response)
        return response


class VideoMetadataTests(unittest.TestCase):
    def read(self, playurl, media_files, playback_payload=None):
        session = OfflineSession([{}], media_files=media_files,
                                 playback_payload=playback_payload)
        result = read_playback_duration(session, {
            'ccid': 'test-video', 'playurl': playurl,
        }, ORIGINS, {'xtbz': 'ykt'})
        return session, result

    def test_extensionless_hls_with_mime_and_without_content_length(self):
        url = 'https://media.example/manifest?id=example&signature=private-token'
        for content_type in ('application/vnd.apple.mpegurl; charset=utf-8',
                             'application/x-mpegURL'):
            with self.subTest(content_type=content_type):
                session = PlaylistSession({url: (
                    '#EXTM3U\n#EXTINF:8.25,\na.ts\n#EXTINF:12,\nb.ts\n#EXT-X-ENDLIST\n',
                    None)}, content_type)
                duration, source = read_playback_duration(session, {
                    'ccid': 'test-video', 'playurl': {'sources': {'quality10': [url]}},
                }, ORIGINS, {})
                self.assertEqual(duration, 20.25)
                self.assertEqual(source, 'HLS 播放清单')
                self.assertTrue(all(response.closed for response in session.responses))

    def test_extensionless_hls_detects_content_despite_generic_mime(self):
        url = 'https://media.example/manifest'
        session = PlaylistSession({url: (
            '\ufeff#EXTM3U\n#EXTINF:20.25,\na.ts\n#EXT-X-ENDLIST\n', None)},
            'application/octet-stream')
        duration, _ = read_playback_duration(session, {
            'ccid': 'test-video', 'playurl': url,
        }, ORIGINS, {})
        self.assertEqual(duration, 20.25)

    def test_extensionless_hls_reads_beyond_initial_range(self):
        url = 'https://media.example/manifest'
        body = '#EXTM3U\n' + '# comment\n' * 7000 + '#EXTINF:20.25,\na.ts\n#EXT-X-ENDLIST\n'
        session = PlaylistSession({url: (body, None)}, supports_ranges=True)
        duration, _ = read_playback_duration(session, {
            'ccid': 'test-video', 'playurl': url,
        }, ORIGINS, {})
        self.assertEqual(duration, 20.25)
        self.assertTrue(all(response.closed for response in session.responses))

    def test_extensionless_redirected_master_resolves_relative_playlist(self):
        master = 'https://media.example/manifest'
        final = 'https://cdn.example/path/master'
        child = 'https://cdn.example/path/low/manifest'
        session = PlaylistSession({
            master: ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/manifest\n', final),
            final: ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/manifest\n', final),
            child: ('#EXTM3U\n#EXTINF:20.25,\na.ts\n#EXT-X-ENDLIST\n', None),
        })
        duration, _ = read_playback_duration(session, {
            'ccid': 'test-video', 'playurl': master,
        }, ORIGINS, {})
        self.assertEqual(duration, 20.25)
        self.assertIn(child, [url for url, _ in session.playback_requests])
        self.assertTrue(all(response.closed for response in session.responses))

    def test_extensionless_live_hls_has_no_confirmed_duration(self):
        url = 'https://media.example/live?signature=private-token'
        session = PlaylistSession({url: ('#EXTM3U\n#EXTINF:6,\na.ts\n', None)})
        duration, reason = read_playback_duration(session, {
            'ccid': 'test-video', 'playurl': url,
        }, ORIGINS, {})
        self.assertEqual(duration, 0)
        self.assertIn('没有可确认总时长', reason)
        self.assertNotIn('private-token', reason)
        self.assertTrue(all(response.closed for response in session.responses))

    def test_extensionless_oversized_hls_is_rejected_and_closed(self):
        url = 'https://media.example/manifest'
        body = '#EXTM3U\n' + '# comment\n' * 220000 + '#EXTINF:6,\na.ts\n#EXT-X-ENDLIST\n'
        session = PlaylistSession({url: (body, None)})
        duration, reason = read_playback_duration(session, {
            'ccid': 'test-video', 'playurl': url,
        }, ORIGINS, {})
        self.assertEqual(duration, 0)
        self.assertIn('超过读取上限', reason)
        self.assertTrue(all(response.closed for response in session.responses))
        self.assertTrue(all(response.bytes_read <= 1024 * 1024 + 8192
                            for response in session.responses))

    def test_mp4_metadata_at_file_end_skips_video_data(self):
        url = 'https://media.example/end.mp4'
        original = mp4_fixture(73.25)
        video_data = struct.pack('>I4s', 200008, b'mdat') + bytes(200000)
        body = original[:24] + video_data + original[24:]
        session, (duration, _) = self.read(url, {url: body})
        self.assertEqual(duration, 73.25)
        ranges = [kwargs['headers']['Range'] for _, kwargs in session.playback_requests]
        self.assertTrue(any(int(value.split('=')[1].split('-')[0]) > 200000
                            for value in ranges))
        bytes_requested = sum(int(value.split('-')[1]) -
                              int(value.split('=')[1].split('-')[0]) + 1
                              for value in ranges)
        self.assertLess(bytes_requested, 66000)
        self.assertTrue(all(kwargs['stream'] for _, kwargs in session.playback_requests))

    def test_mp4_version_one_uses_64_bit_duration(self):
        url = 'https://media.example/long.mp4'
        mvhd_body = bytes([1, 0, 0, 0]) + bytes(16) + struct.pack('>IQ', 1000, 991250)
        mvhd = struct.pack('>I4s', len(mvhd_body) + 8, b'mvhd') + mvhd_body
        moov = struct.pack('>I4sQ', 1, b'moov', len(mvhd) + 16) + mvhd
        _, (duration, _) = self.read(url, {url: moov})
        self.assertEqual(duration, 991.25)

    def test_hls_master_resolves_relative_video_playlist(self):
        master = 'https://media.example/path/master.m3u8'
        child = 'https://media.example/path/low/index.m3u8'
        session, (duration, _) = self.read(master, {
            master: '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n',
            child: '#EXTM3U\n#EXTINF:5.25,\na.ts\n#EXTINF:7.5,\nb.ts\n#EXT-X-ENDLIST\n',
        })
        self.assertEqual(duration, 12.75)
        self.assertEqual([url for url, _ in session.playback_requests], [master, child])

    def test_live_hls_is_not_mistaken_for_complete_video(self):
        url = 'https://media.example/live.m3u8'
        _, (duration, reason) = self.read(url, {
            url: '#EXTM3U\n#EXTINF:6,\na.ts\n#EXTINF:6,\nb.ts\n',
        })
        self.assertEqual(duration, 0)
        self.assertIn('没有可确认总时长', reason)

    def test_failed_source_does_not_prevent_trying_another_source(self):
        bad = 'https://media.example/broken.mp4'
        good = 'https://media.example/good.mp4'
        _, (duration, _) = self.read({'sources': {'quality10': [bad, good]}}, {
            bad: b'<html>not a movie</html>', good: mp4_fixture(100),
        })
        self.assertEqual(duration, 100)

    def test_timeout_does_not_leak_signed_media_url(self):
        url = 'https://media.example/video.mp4?signature=private-token'
        _, (duration, reason) = self.read(url, {url: requests.Timeout(url)})
        self.assertEqual(duration, 0)
        self.assertNotIn('private-token', reason)
        self.assertIn('请求失败或超时', reason)

    def test_malformed_direct_playurl_can_recover_via_playback_api(self):
        url = 'https://media.example/good.mp4'
        _, (duration, _) = self.read({'other': 42}, {url: mp4_fixture(60)},
                                    playback_payload={'success': True, 'data': {
                                        'playurl': {'sources': {'quality10': [url]}}}})
        self.assertEqual(duration, 60)


if __name__ == '__main__':
    unittest.main()
