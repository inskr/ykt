import struct
import unittest

import requests

from test_ykt_progress import OfflineSession, mp4_fixture
from ykt_video_metadata import read_playback_duration


ORIGINS = ('https://xidianyjs.yuketang.cn', 'https://www.yuketang.cn')


class VideoMetadataTests(unittest.TestCase):
    def read(self, playurl, media_files, playback_payload=None):
        session = OfflineSession([{}], media_files=media_files,
                                 playback_payload=playback_payload)
        result = read_playback_duration(session, {
            'ccid': 'test-video', 'playurl': playurl,
        }, ORIGINS, {'xtbz': 'ykt'})
        return session, result

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
