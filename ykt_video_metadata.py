"""通过雨课堂播放信息和媒体元数据读取真实时长。"""

import math
import json
import re
import struct
from urllib.parse import urljoin, urlsplit

import requests


class MetadataError(ValueError):
    pass


def positive_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        seconds = float(value)
    except (ValueError, OverflowError):
        return 0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0


def playback_urls(playurl, origin):
    """按播放器支持的结构取地址，优先选择完整的 HLS 清单。"""
    candidates = []

    def add(value, hls=False):
        if isinstance(value, str):
            try:
                url = urljoin(origin + '/', value)
                parsed = urlsplit(url)
            except ValueError:
                return
            if parsed.scheme in ('https', 'http') and parsed.hostname:
                candidates.append((url, hls or parsed.path.lower().endswith('.m3u8')))
        elif isinstance(value, list):
            for item in value[:3]:
                add(item, hls)

    def collect(value, hls=False):
        if isinstance(value, str):
            add(value, hls)
        elif isinstance(value, dict):
            if 'm3u8' in value:
                collect(value['m3u8'], True)
            sources = value.get('sources')
            if isinstance(sources, dict):
                keys = ['quality10', 'quality20'] + [
                    key for key in sources if key not in ('quality10', 'quality20', 'group')]
                for key in keys:
                    add(sources.get(key), hls)
            other_sources = value.get('other')
            other_sources = other_sources if isinstance(other_sources, list) else []
            for other in other_sources[:3]:
                if isinstance(other, dict):
                    for key, urls in other.items():
                        if key != 'group':
                            add(urls, hls)

    collect(playurl)
    seen = set()
    for url, hls in sorted(candidates, key=lambda item: not item[1]):
        if url not in seen:
            seen.add(url)
            yield url, hls


def read_resource(session, url, origin, limit, range_start=None):
    """只读取清单或少量元数据，及时关闭视频响应。"""
    headers = {'Referer': origin + '/', 'Accept-Encoding': 'identity'}
    if range_start is not None:
        headers['Range'] = f'bytes={range_start}-{range_start + limit - 1}'
    response = session.get(url, headers=headers, stream=True, timeout=(5, 10))
    try:
        if response.status_code not in (200, 206):
            raise MetadataError(f'媒体请求返回 HTTP {response.status_code}')
        total = None
        if range_start is not None:
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',
                                     response.headers.get('Content-Range', ''))
                if not match or int(match[1]) != range_start:
                    raise MetadataError('媒体服务器返回了不匹配的分段数据')
                total = int(match[3])
            elif range_start != 0:
                raise MetadataError('媒体服务器不支持读取指定位置的元数据')
            else:
                try:
                    total = int(response.headers['Content-Length'])
                except (KeyError, TypeError, ValueError):
                    raise MetadataError('媒体服务器没有提供文件长度') from None
        body = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            if range_start is None and len(body) + len(chunk) > limit:
                raise MetadataError('播放清单超过读取上限')
            body.extend(chunk[:limit - len(body)])
            if len(body) >= limit:
                break
        if not body:
            raise MetadataError('媒体服务器返回空内容')
        return bytes(body), total, getattr(response, 'url', None) or url
    finally:
        response.close()


def read_hls_duration(session, url, origin, depth=0):
    if depth > 2:
        raise MetadataError('播放清单嵌套过多')
    body, _, final_url = read_resource(session, url, origin, 1024 * 1024)
    lines = [line.strip() for line in body.decode('utf-8-sig').splitlines()]
    if not lines or lines[0] != '#EXTM3U':
        raise MetadataError('返回内容不是 HLS 播放清单')
    segments = []
    for line in lines:
        if line.startswith('#EXTINF:'):
            seconds = positive_seconds(line.partition(':')[2].partition(',')[0])
            if not seconds:
                raise MetadataError('播放清单含无效片段时长')
            segments.append(seconds)
    if segments:
        if '#EXT-X-ENDLIST' not in lines:
            raise MetadataError('直播或未结束的播放清单没有可确认总时长')
        return math.fsum(segments)
    variants = []
    waiting = False
    for line in lines:
        if line.startswith('#EXT-X-STREAM-INF:'):
            waiting = True
        elif waiting and line and not line.startswith('#'):
            variants.append(urljoin(final_url, line))
            waiting = False
    for variant in variants[:2]:
        try:
            return read_hls_duration(session, variant, origin, depth + 1)
        except (MetadataError, requests.RequestException, UnicodeError):
            continue
    raise MetadataError('播放清单未提供可确认的视频时长')


def read_mp4_duration(session, url, origin):
    """定位 moov/mvhd，只读取文件头和电影时长信息。"""
    initial, total, _ = read_resource(session, url, origin, 65536, range_start=0)
    if not total or total < 8:
        raise MetadataError('视频文件长度无效')
    requests_left = 8

    def read(offset, length):
        nonlocal requests_left
        if offset < 0 or offset + length > total:
            raise MetadataError('视频元数据越过文件边界')
        if offset + length <= len(initial):
            return initial[offset:offset + length]
        if requests_left <= 0:
            raise MetadataError('视频元数据查询超过次数上限')
        requests_left -= 1
        body, current_total, _ = read_resource(session, url, origin, length,
                                               range_start=offset)
        if current_total != total or len(body) != length:
            raise MetadataError('视频元数据不完整或文件长度已变化')
        return body

    def atoms(start, end):
        offset = start
        for _ in range(64):
            if offset + 8 > end:
                return
            size, kind = struct.unpack('>I4s', read(offset, 8))
            header = 8
            if size == 1:
                size = struct.unpack('>Q', read(offset + 8, 8))[0]
                header = 16
            elif size == 0:
                size = end - offset
            if size < header or offset + size > end:
                raise MetadataError('视频文件的元数据结构无效')
            yield kind, offset + header, offset + size
            offset += size
        raise MetadataError('视频元数据结构超过解析上限')

    for kind, start, end in atoms(0, total):
        if kind != b'moov':
            continue
        for child, payload_start, payload_end in atoms(start, end):
            if child != b'mvhd':
                continue
            size = min(32, payload_end - payload_start)
            data = read(payload_start, size)
            if not data:
                break
            version = data[0]
            if version == 0 and len(data) >= 20:
                timescale, duration = struct.unpack('>II', data[12:20])
                unknown = 0xffffffff
            elif version == 1 and len(data) >= 32:
                timescale, duration = struct.unpack('>IQ', data[20:32])
                unknown = 0xffffffffffffffff
            else:
                raise MetadataError('视频使用了无法识别的时长元数据')
            if timescale and duration != unknown:
                seconds = positive_seconds(duration / timescale)
                if seconds:
                    return seconds
    raise MetadataError('视频文件没有提供有效的电影时长')


def read_playback_duration(session, media, origins, headers, *,
                           media_origin=None, headers_for_origin=None):
    """返回 (真实秒数, 来源或失败原因)，不输出签名地址或凭据。"""
    failures = []

    def probe(playurl, origin):
        for index, (url, hls) in enumerate(playback_urls(playurl, origin)):
            if index >= 3:
                break
            try:
                if hls:
                    seconds = read_hls_duration(session, url, origin)
                    source = 'HLS 播放清单'
                else:
                    seconds = read_mp4_duration(session, url, origin)
                    source = 'MP4 视频元数据'
                if positive_seconds(seconds):
                    return seconds, source
            except MetadataError as error:
                failures.append(str(error))
            except requests.RequestException:
                failures.append('媒体请求失败或超时')
            except (UnicodeError, ValueError, TypeError, struct.error):
                failures.append('媒体元数据无法解析')
        return 0, ''

    origins = tuple(dict.fromkeys(([media_origin] if media_origin else []) + list(origins)))
    if not origins:
        return 0, '未配置播放信息站点'
    if media.get('playurl'):
        seconds, source = probe(media['playurl'], origins[0])
        if seconds:
            return seconds, source
    for origin in origins:
        try:
            origin_headers = headers_for_origin(origin) if headers_for_origin else headers
            response = session.get(origin + '/api/open/audiovideo/playurl',
                                   params={
                                       'video_id': media.get('ccid'),
                                       'provider': media.get('video_provider') or 'cc',
                                       'file_type': 1, 'is_single': 0,
                                       'domain': urlsplit(origin).netloc,
                                    }, headers=origin_headers, timeout=(5, 10), stream=True)
            try:
                if response.status_code != 200:
                    failures.append(f'播放接口返回 HTTP {response.status_code}')
                    continue
                body = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if len(body) + len(chunk) > 1024 * 1024:
                        raise MetadataError('播放接口响应超过读取上限')
                    body.extend(chunk)
                payload = json.loads(body)
            finally:
                response.close()
            data = payload.get('data') if isinstance(payload, dict) else None
            if payload.get('success') is False or not isinstance(data, dict):
                failures.append('播放接口未返回可用的视频资源')
                continue
            playurl = data.get('playurl')
            for source in (data, playurl):
                if isinstance(source, dict):
                    for field in ('duration', 'video_length'):
                        seconds = positive_seconds(source.get(field))
                        if seconds:
                            return seconds, '播放信息接口'
            seconds, source = probe(playurl, origin)
            if seconds:
                return seconds, source
            failures.append('播放接口未提供可读取的时长或媒体资源')
        except requests.RequestException:
            failures.append('播放接口请求失败或超时')
        except MetadataError as error:
            failures.append(str(error))
        except (ValueError, TypeError, AttributeError):
            failures.append('播放接口返回格式无效')
    return 0, '；'.join(dict.fromkeys(failures)) or '未取得可读取的播放资源'
