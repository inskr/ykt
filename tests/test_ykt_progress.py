import ast
import contextlib
import io
import json
import struct
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests


VIDEO_ID = "90948771"
SCRIPT = Path(__file__).resolve().parents[1] / "ykt.py"


class OfflineResponse:
    def __init__(self, body, status_code=200, headers=None):
        self.content = body if isinstance(body, bytes) else body.encode('utf-8')
        self.text = self.content.decode('utf-8', errors='replace')
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('offline HTTP error', response=self)

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        pass


def actual_playback_duration(*args, **kwargs):
    from ykt_video_metadata import read_playback_duration
    return read_playback_duration(*args, **kwargs)


def mp4_fixture(seconds, timescale=1000):
    def atom(kind, body):
        return struct.pack('>I4s', len(body) + 8, kind) + body
    mvhd = atom(b'mvhd', bytes(12) + struct.pack('>II', timescale,
                                               int(seconds * timescale)))
    return atom(b'ftyp', b'isom' + bytes(12)) + atom(b'moov', mvhd)


class OfflineSession:
    def __init__(self, progress_responses, duration=60,
                 content_info=None, media_by_id=None,
                 playback_payload=None, media_files=None,
                 leaf_details=None, heartbeat_responses=None, classroom=1):
        self.progress_responses = progress_responses
        self.duration = duration
        self.progress_requests = 0
        self.progress_requests_by_video = {}
        self.heartbeats = []
        self.leaf_requests = []
        self.media_by_id = media_by_id or {}
        self.playback_payload = playback_payload
        self.media_files = media_files or {}
        self.playback_requests = []
        self.leaf_details = leaf_details or {}
        self.heartbeat_responses = heartbeat_responses or [{}]
        self.classroom = classroom
        self.heartbeat_requests = []
        self.progress_queries = []
        self.sleep_calls = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set('csrftoken', 'private-csrf-token', domain='.yuketang.cn')
        self.content_info = content_info if content_info is not None else [
            {"section_list": [{"leaf_list": [{"id": VIDEO_ID}]}]},
        ]

    def get(self, url, **kwargs):
        if "/api/open/audiovideo/playurl" in url:
            self.playback_requests.append((url, kwargs))
            payload = (self.playback_payload if self.playback_payload is not None
                       else {"success": False, "data": {}})
        elif url in self.media_files:
            self.playback_requests.append((url, kwargs))
            body = self.media_files[url]
            if isinstance(body, Exception):
                raise body
            body = body.encode('utf-8') if isinstance(body, str) else body
            range_header = kwargs.get('headers', {}).get('Range')
            if range_header:
                start, end = map(int, range_header.removeprefix('bytes=').split('-'))
                sliced = body[start:end + 1]
                return OfflineResponse(sliced, 206, {
                    'Content-Range': f'bytes {start}-{start + len(sliced) - 1}/{len(body)}',
                    'Content-Length': str(len(sliced)),
                })
            return OfflineResponse(body, headers={'Content-Length': str(len(body))})
        elif "/courses/list" in url:
            payload = {"data": {"list": [
                {"name": "course one", "classroom_id": self.classroom},
                {"name": "course two", "classroom_id": 2},
            ]}}
        elif "/logs/learn/" in url:
            payload = {"data": {"activities": [
                {"courseware_id": 1}, {"courseware_id": 2},
            ]}}
        elif "/pub_news/" in url:
            payload = {"data": {
                "course_id": 1, "s_id": 1, "c_short_name": "test course",
                "content_info": self.content_info,
            }}
        elif "/leaf_info/" in url:
            video_id = url.rstrip("/").rsplit("/", 1)[-1]
            self.leaf_requests.append(video_id)
            media = self.media_by_id.get(video_id, {
                "ccid": "test-video", "duration": self.duration,
            })
            if isinstance(media, list):
                index = min(self.leaf_requests.count(video_id) - 1,
                            len(media) - 1)
                media = media[index]
            payload = {"data": {
                "id": video_id, "user_id": 1,
                "content_info": {"media": media},
            }}
            payload['data'].update(self.leaf_details.get(video_id, {}))
        elif "/get_video_watch_progress/" in url:
            self.progress_requests += 1
            video_id = parse_qs(urlsplit(url).query)["video_id"][0]
            self.progress_queries.append((url, kwargs))
            self.progress_requests_by_video[video_id] = (
                self.progress_requests_by_video.get(video_id, 0) + 1)
            responses = (self.progress_responses[video_id]
                         if isinstance(self.progress_responses, dict)
                         else self.progress_responses)
            index = min(self.progress_requests_by_video[video_id] - 1,
                        len(responses) - 1)
            payload = responses[index]
        else:
            raise AssertionError(f"Unexpected GET: {url}")
        return OfflineResponse(json.dumps(payload))

    def post(self, url, data, **kwargs):
        if "/video-log/heartbeat/" not in url:
            raise AssertionError(f"Unexpected POST: {url}")
        self.heartbeats.append(json.loads(data)["heart_data"][0])
        self.heartbeat_requests.append((url, kwargs))
        if len(self.heartbeats) > 75:
            raise AssertionError("Missing progress caused an unbounded loop")
        index = min(len(self.heartbeats) - 1, len(self.heartbeat_responses) - 1)
        reply = self.heartbeat_responses[index]
        return reply if isinstance(reply, OfflineResponse) else OfflineResponse(json.dumps(reply))


class CourseTestCase(unittest.TestCase):
    def run_course(self, responses, duration=60,
                   content_info=None, media_by_id=None,
                   playback_payload=None, media_files=None,
                   leaf_details=None, heartbeat_responses=None, classroom=1):
        session = OfflineSession(responses, duration,
                                 content_info, media_by_id,
                                 playback_payload, media_files,
                                 leaf_details, heartbeat_responses, classroom)
        # Load the real functions without triggering QR login or network I/O.
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8-sig"))
        functions = [node for node in tree.body
                     if isinstance(node, ast.FunctionDef)]
        namespace = {
            "json": json, "session": session, "input": lambda _: "0",
            "time": SimpleNamespace(time=lambda: 1700000000,
                                    sleep=session.sleep_calls.append),
            "read_playback_duration": actual_playback_duration,
            "PLAYBACK_ORIGINS": ('https://xidianyjs.yuketang.cn',
                                 'https://www.yuketang.cn'),
            "COURSE_ORIGINS": {'33356219': 'https://xidianyjs.yuketang.cn'},
            "requests": requests, "urlsplit": urlsplit,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]),
                     str(SCRIPT), "exec"), namespace)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            namespace["ssxx"]()
        return session, output.getvalue()


class ProgressRegressionTests(CourseTestCase):
    def test_failed_metadata_requests_are_not_reported_as_completed_course(self):
        original_get = OfflineSession.get

        def deny_metadata(session, url, **kwargs):
            if '/leaf_info/' in url:
                return OfflineResponse('{}', 403)
            return original_get(session, url, **kwargs)

        with patch.object(OfflineSession, 'get', new=deny_metadata):
            session, output = self.run_course([{}], classroom=33356219)
        self.assertEqual(session.heartbeats, [])
        self.assertIn('未能确认', output)
        self.assertNotIn('这门课看完', output)

    def test_submission_business_error_is_reported_without_leaking_response(self):
        session, output = self.run_course([
            {'data': {VIDEO_ID: {'completed': 0}}},
        ], heartbeat_responses=[{'success': False, 'error_code': 10016,
                                'msg': 'private-user-info', 'Auth': 'private-auth'}])
        self.assertEqual(len(session.heartbeats), 1)
        self.assertIn('error_code=10016', output)
        self.assertIn('未能确认', output)
        self.assertNotIn('private-user-info', output)
        self.assertNotIn('private-auth', output)

    def test_unconfirmed_rate_record_stops_after_three_rounds(self):
        session, output = self.run_course([
            {'data': {VIDEO_ID: {'rate': 0, 'watch_length': 0}}},
        ])
        self.assertEqual(len(session.heartbeats), 75)
        self.assertIn('处理 3 轮后', output)
        self.assertIn('未能确认', output)

    def test_school_metadata_rejection_falls_back_to_main_site(self):
        original_get = OfflineSession.get

        def deny_school(session, url, **kwargs):
            if '/leaf_info/' in url and urlsplit(url).hostname == 'xidianyjs.yuketang.cn':
                return OfflineResponse('{}', 403)
            return original_get(session, url, **kwargs)

        with patch.object(OfflineSession, 'get', new=deny_school):
            session, _ = self.run_course([
                {'data': {VIDEO_ID: {'completed': 0}}},
                {'data': {VIDEO_ID: {'completed': 1}}},
            ], classroom=33356219)
        self.assertEqual(len(session.heartbeats), 1)
        self.assertTrue(session.heartbeat_requests[0][0].startswith('https://www.yuketang.cn/'))

    def test_progress_and_submission_use_each_video_course_and_sku(self):
        session, _ = self.run_course([
            {'data': {VIDEO_ID: {'completed': 0}}},
            {'data': {VIDEO_ID: {'completed': 1}}},
        ], leaf_details={VIDEO_ID: {'course_id': 55, 'sku_id': 44,
                                   'classroom_id': 33356219, 'university_id': 33}},
            classroom=33356219)
        query = parse_qs(urlsplit(session.progress_queries[0][0]).query)
        self.assertEqual(query['cid'], ['55'])
        self.assertTrue(session.progress_queries[0][0].startswith('https://xidianyjs.yuketang.cn/'))
        heartbeat = session.heartbeats[0]
        self.assertEqual(heartbeat['c'], 55)
        self.assertEqual(heartbeat['skuid'], 44)
        self.assertEqual(heartbeat['classroomid'], '33356219')
        self.assertEqual(heartbeat['lob'], 'cloud4')
        headers = session.heartbeat_requests[0][1]['headers']
        self.assertEqual(headers['X-CSRFToken'], 'private-csrf-token')
        self.assertEqual(headers['university-id'], '33')
        self.assertNotIn('authority', headers)
        self.assertNotIn('method', headers)

    def test_rejected_submission_is_reported_without_claiming_completion(self):
        session, output = self.run_course([
            {'data': {VIDEO_ID: {'completed': 0}}},
            {'data': {VIDEO_ID: {'completed': 1}}},
        ], heartbeat_responses=[OfflineResponse('forbidden', 403)])
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.progress_requests, 1)
        self.assertIn('HTTP 403', output)
        self.assertIn('未能确认', output)
        self.assertNotIn('这门课看完', output)

    def test_missing_progress_is_polled_without_repeated_submission(self):
        session, output = self.run_course([
            {'data': {VIDEO_ID: {'completed': 0}}}, {'data': {}},
        ])
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.progress_requests, 4)
        self.assertEqual(session.sleep_calls, [0.6, 2, 4])
        self.assertIn('进度诊断', output)

    def test_submission_timestamp_is_current_and_sequence_increases(self):
        session, _ = self.run_course([
            {'data': {VIDEO_ID: {'completed': 0}}},
            {'data': {VIDEO_ID: {'completed': 0}}},
            {'data': {VIDEO_ID: {'completed': 1}}},
        ])
        self.assertEqual(len(session.heartbeats), 2)
        self.assertEqual([int(hb['ts']) for hb in session.heartbeats],
                         [1700000000000, 1700000000000])
        self.assertLess(session.heartbeats[0]['sq'], session.heartbeats[1]['sq'])
        self.assertEqual(session.heartbeats[0]['fp'], 0)
        self.assertEqual(session.heartbeats[0]['tp'], 0)

    def test_progress_with_rate_but_without_completed_is_recognized(self):
        session, output = self.run_course([
            {'data': {VIDEO_ID: {'rate': 0, 'watch_length': 0}}},
            {'data': {VIDEO_ID: {'rate': 1, 'watch_length': 60}}},
        ])
        self.assertEqual(len(session.heartbeats), 1)
        self.assertNotIn('未能确认', output)

    def test_zero_catalogue_duration_is_recovered_from_real_mp4_metadata(self):
        playback_url = 'https://media.example/video.mp4?signature=private'
        session, output = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}},
        ] * 3 + [{"data": {VIDEO_ID: {"completed": 1}}}], duration=0,
            playback_payload={"success": True, "data": {
                "playurl": {"sources": {"quality10": [playback_url]}}}},
            media_files={playback_url: mp4_fixture(125.5)})
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.heartbeats[0]['d'], 125.5)
        api_request = session.playback_requests[0]
        self.assertTrue(api_request[0].startswith('https://www.yuketang.cn/'))
        self.assertEqual(api_request[1]['params']['video_id'], 'test-video')
        self.assertEqual(api_request[1]['params']['domain'], 'www.yuketang.cn')
        self.assertNotIn('private', output)
        self.assertNotIn('未能确认', output)

    def test_zero_duration_is_recovered_from_a_complete_hls_playlist(self):
        playback_url = 'https://media.example/video.m3u8'
        session, output = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}},
        ] * 3 + [{"data": {VIDEO_ID: {"completed": 1}}}], duration=0,
            playback_payload={"success": True, "data": {"playurl": {
                "m3u8": {"sources": {"quality10": [playback_url]}}}}},
            media_files={playback_url: '#EXTM3U\n#EXTINF:1.5,\na.ts\n'
                         '#EXTINF:20.25,\nb.ts\n#EXT-X-ENDLIST\n'})
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.heartbeats[0]['d'], 21.75)
        self.assertNotIn('未能确认', output)

    def test_completed_video_does_not_request_playback_resources(self):
        session, _ = self.run_course([
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], duration=0)
        self.assertEqual(session.playback_requests, [])

    def test_top_level_length_is_kept_when_nested_progress_has_no_length(self):
        session, _ = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}},
             VIDEO_ID: {"video_length": 60}},
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], duration=0)
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.heartbeats[0]["d"], 60)

    def test_shared_progress_length_can_supply_video_duration(self):
        session, _ = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}, "video_length": 45}},
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], duration=0)
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.heartbeats[0]["d"], 45)

    def test_chapter_leaf_duration_is_used_when_media_has_no_duration(self):
        session, _ = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}},
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], duration=0, content_info=[{"section_list": [
            {"leaf_list": [{"id": VIDEO_ID, "duration": 30}]},
        ]}])
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.heartbeats[0]["d"], 30)

    def test_text_duration_is_converted_to_a_number(self):
        session, _ = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}},
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], duration="60.5")
        self.assertEqual(session.heartbeats[0]["d"], 60.5)

    def test_video_metadata_is_retried_before_missing_duration_is_skipped(self):
        session, _ = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}},
            {"data": {VIDEO_ID: {"completed": 0}}},
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], media_by_id={VIDEO_ID: [
            {"ccid": "test-video", "duration": 0},
            {"ccid": "test-video", "duration": 45},
        ]})
        self.assertEqual(session.leaf_requests, [VIDEO_ID, VIDEO_ID])
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.heartbeats[0]["d"], 45)

    def test_completed_during_metadata_retry_is_skipped(self):
        session, output = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}},
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], duration=0)
        self.assertEqual(session.leaf_requests, [VIDEO_ID, VIDEO_ID])
        self.assertEqual(len(session.heartbeats), 0)
        self.assertIn("已完成，跳过", output)
        self.assertNotIn("未能确认", output)

    def test_invalid_durations_are_retried_twice_then_reported(self):
        for duration in (True, -1, "NaN", "Infinity", "invalid", None):
            with self.subTest(duration=duration):
                session, output = self.run_course([
                    {"data": {VIDEO_ID: {"completed": 0},
                              "Auth": "private-auth", "video_length": {
                                  "url": "private-playback-url"}}},
                ], duration=duration)
                self.assertEqual(session.leaf_requests, [VIDEO_ID] * 3)
                self.assertEqual(session.progress_requests, 3)
                self.assertEqual(len(session.heartbeats), 0)
                self.assertIn("时长诊断", output)
                self.assertIn("未能确认", output)
                self.assertNotIn("private-auth", output)
                self.assertNotIn("private-playback-url", output)

    def test_missing_video_record_stops_without_claiming_completion(self):
        session, output = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}}, {"data": {}},
        ])
        self.assertLessEqual(len(session.heartbeats), 3)
        self.assertIn(VIDEO_ID, output)
        self.assertIn("未能确认", output)

    def test_progress_can_recover_after_a_missing_record(self):
        session, output = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}}, {"data": {}},
            {"data": {VIDEO_ID: {"completed": 1, "watch_length": 60}}},
        ])
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.progress_requests, 3)
        self.assertNotIn("未能确认", output)

    def test_completed_progress_does_not_require_watch_length(self):
        session, output = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0}}},
            {"data": {VIDEO_ID: {"completed": 1}}},
        ])
        self.assertEqual(len(session.heartbeats), 1)
        self.assertNotIn("未能确认", output)

    def test_already_completed_video_sends_no_heartbeats(self):
        session, output = self.run_course([
            {"data": {VIDEO_ID: {"completed": 1}}},
        ])
        self.assertEqual(len(session.heartbeats), 0)
        self.assertIn("已完成，跳过", output)

    def test_nested_video_length_is_used_when_media_duration_is_zero(self):
        session, _ = self.run_course([
            {"data": {VIDEO_ID: {"completed": 0, "video_length": 60}}},
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], duration=0)
        self.assertEqual(len(session.heartbeats), 1)
        self.assertEqual(session.heartbeats[0]["d"], 60)

    def test_completed_video_with_missing_duration_needs_no_retry(self):
        session, output = self.run_course([
            {"data": {VIDEO_ID: {"completed": 1}}},
        ], duration=0)
        self.assertEqual(len(session.heartbeats), 0)
        self.assertEqual(session.leaf_requests, [VIDEO_ID])
        self.assertNotIn("未能确认", output)

    def test_missing_video_length_skips_invalid_zero_duration(self):
        session, output = self.run_course([{"data": {}}], duration=0)
        self.assertEqual(len(session.heartbeats), 0)
        self.assertIn("未能确认", output)

    def test_top_level_video_progress_is_supported(self):
        session, _ = self.run_course([
            {VIDEO_ID: {"completed": 0, "video_length": 60}},
            {VIDEO_ID: {"completed": 1}},
        ])
        self.assertEqual(len(session.heartbeats), 1)

    def test_invalid_progress_payloads_are_bounded(self):
        for payload in ({"data": None}, {"data": []},
                        {"data": {VIDEO_ID: None}}, None):
            with self.subTest(payload=payload):
                session, output = self.run_course([
                    {"data": {VIDEO_ID: {"completed": 0}}}, payload,
                ])
                self.assertLessEqual(len(session.heartbeats), 3)
                self.assertIn("未能确认", output)


def progress_sequence(video_id):
    return [
        {"data": {video_id: {"completed": 0}}},
        {"data": {video_id: {"completed": 1}}},
    ]


class ChapterTraversalTests(CourseTestCase):
    def test_two_videos_in_one_section_are_both_processed(self):
        session, _ = self.run_course({
            "101": progress_sequence("101"),
            "102": progress_sequence("102"),
        }, content_info=[{"section_list": [
            {"name": "1.3", "leaf_list": [{"id": "101"}, {"id": "102"}]},
        ]}])
        self.assertEqual(session.leaf_requests, ["101", "102"])
        self.assertEqual([item["v"] for item in session.heartbeats], [101, 102])

    def test_completed_first_video_does_not_skip_second_video(self):
        session, _ = self.run_course({
            "101": [{"data": {"101": {"completed": 1}}}],
            "102": progress_sequence("102"),
        }, content_info=[{"section_list": [
            {"leaf_list": [{"id": "101"}, {"id": "102"}]},
        ]}])
        self.assertEqual(session.leaf_requests, ["101", "102"])
        self.assertEqual([item["v"] for item in session.heartbeats], [102])

    def test_missing_first_video_progress_does_not_skip_second_video(self):
        session, output = self.run_course({
            "101": [{"data": {"101": {"completed": 0}}}, {"data": {}}],
            "102": progress_sequence("102"),
        }, content_info=[{"section_list": [
            {"leaf_list": [{"id": "101"}, {"id": "102"}]},
        ]}])
        self.assertEqual(session.leaf_requests, ["101", "102"])
        self.assertEqual([item["v"] for item in session.heartbeats],
                         [101, 102])
        self.assertIn("未能确认", output)

    def test_empty_sections_and_multiple_chapters_preserve_all_videos(self):
        session, _ = self.run_course({
            "101": progress_sequence("101"),
            "102": progress_sequence("102"),
            "201": progress_sequence("201"),
        }, content_info=[
            {"section_list": [
                {"leaf_list": []}, {"leaf_list": []},
                {"name": "1.3", "leaf_list": [{"id": "101"}, {"id": "102"}]},
            ]},
            {"section_list": [{"leaf_list": [{"id": "201"}]}]},
        ])
        self.assertEqual(session.leaf_requests, ["101", "102", "201"])
        self.assertEqual([item["v"] for item in session.heartbeats], [101, 102, 201])

    def test_non_video_content_does_not_block_following_videos(self):
        session, _ = self.run_course({
            "101": progress_sequence("101"),
            "102": progress_sequence("102"),
        }, content_info=[{"section_list": [
            {"leaf_list": [{"id": "100"}, {"id": "101"}, {"id": "102"}]},
        ]}], media_by_id={"100": None})
        self.assertEqual(session.leaf_requests, ["100", "101", "102"])
        self.assertEqual([item["v"] for item in session.heartbeats], [101, 102])


if __name__ == "__main__":
    unittest.main()
