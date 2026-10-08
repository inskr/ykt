import asyncio
import time
import requests
from websockets.asyncio.client import connect
import json
import webbrowser
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from ykt_video_metadata import read_playback_duration


# 学校视频页面使用的站点；保留主站作为播放信息的备用来源。
PLAYBACK_ORIGINS = ('https://xidianyjs.yuketang.cn', 'https://www.yuketang.cn')
# 可选的课堂站点覆盖；未配置的课堂按候选站点查询。
COURSE_ORIGINS = {}


# VS Code 的输出面板按 UTF-8 解码，统一标准输出和错误输出的编码。
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


session = requests.Session()


def save_login_qrcode(image_bytes):
    """保存到独立的绝对路径；脚本目录不可写时使用临时目录。"""
    last_error = None
    directories = (Path(__file__).resolve().parent, Path(tempfile.gettempdir()))
    for directory in directories:
        try:
            with tempfile.NamedTemporaryFile(
                    mode='wb', prefix='ykt-qrcode-', suffix='.png',
                    dir=str(directory), delete=False) as image_file:
                image_file.write(image_bytes)
                qr_path = Path(image_file.name)
            return qr_path.resolve()
        except OSError as error:
            last_error = error
    raise OSError(f"脚本目录和临时目录均无法写入：{last_error}") from last_error


async def websocket_session():
    uri = "wss://www.yuketang.cn/wsapp"  # WebSocket 服务器的 URI
    headers = {

        'User-Agent': 'Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.71 Mobile Safari/537.36',
        'Origin': "https://www.yuketang.cn",
    }
    data = {
        "op": "requestlogin",
        "role": "web",
        "version": 1.4,
        "type": "qrcode",
        "from": "web"
    }

    async with connect(uri, additional_headers=headers) as websocket:
        # 将字典转换为JSON字符串并发送

        json_data = json.dumps(data)
        await websocket.send(json_data)


        # 保持连接并监听服务器的消息
        while True:

                response = await websocket.recv()

                if 'ticket' in response:
                    response_json = json.loads(response)
                    url = response_json['ticket']

                    qr_response = session.get(url=url)

                    # 使用默认的图像查看器打开图像
                    if qr_response.status_code == 200:
                        try:
                            qr_path = save_login_qrcode(qr_response.content)
                        except OSError as error:
                            print(f"无法保存登录二维码：{error}")
                            return

                        print("大人请微信扫码！！")
                        print(f"二维码保存位置：{qr_path}")
                        webbrowser.open(qr_path.as_uri())
                    else:
                        print(f"Failed to retrieve the image. Status code: {qr_response.status_code}")
                if 'subscribe_status' in response:

                    json_data = json.loads(response)
                    auth = json_data['Auth']
                    UserID = json_data['UserID']

                    url = "https://www.yuketang.cn/pc/web_login"
                    data = '{"UserID":'+str(UserID)+',"Auth":"'+auth+'"}'
                    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko'}
                    response = session.post(url,data,headers)
                    break

        ssxx()




def get_video_progress(payload, video_id):
    """兼容视频进度位于 data 下或响应顶层的情况。"""
    if not isinstance(payload, dict):
        return {}
    top_progress = payload.get(video_id)
    progress = dict(top_progress) if isinstance(top_progress, dict) else {}
    data = payload.get('data')
    if isinstance(data, dict):
        nested_progress = data.get(video_id)
        if isinstance(nested_progress, dict):
            progress.update(nested_progress)
    return progress


def video_is_complete(progress):
    completed = progress.get('completed')
    if completed is not None:
        return completed in (1, '1', True)
    rate = progress.get('rate')
    if isinstance(rate, bool):
        return False
    try:
        return float(rate) == 1
    except (TypeError, ValueError, OverflowError):
        return False


def video_request_headers(origin, classroom, university=None):
    host = urlsplit(origin).hostname
    headers = {
        'xtbz': 'ykt' if host == 'www.yuketang.cn' else 'cloud',
        'classroom-id': str(classroom),
        'Content-Type': 'application/json',
        'Referer': origin + '/',
    }
    if university:
        headers['university-id'] = str(university)
        headers['uv-id'] = str(university)
    matches = []
    for cookie in getattr(session, 'cookies', ()):
        domain = getattr(cookie, 'domain', '').lstrip('.')
        if (getattr(cookie, 'name', '') == 'csrftoken' and domain
                and (host == domain or host.endswith('.' + domain))):
            matches.append(cookie)
    if matches:
        headers['X-CSRFToken'] = max(matches, key=lambda cookie: len(cookie.domain)).value
    return headers


def fetch_video_leaf(classroom, video_id):
    preferred = COURSE_ORIGINS.get(str(classroom), 'https://www.yuketang.cn')
    for origin in dict.fromkeys((preferred, *PLAYBACK_ORIGINS)):
        url = f'{origin}/mooc-api/v1/lms/learn/leaf_info/{classroom}/{video_id}/'
        try:
            response = session.get(url=url, headers=video_request_headers(origin, classroom),
                                   timeout=15)
            payload = json.loads(response.text)
        except (requests.RequestException, ValueError):
            continue
        data = payload.get('data') if isinstance(payload, dict) else None
        if (response.status_code == 200 and isinstance(data, dict)
                and data.get('id') and data.get('user_id') is not None):
            return origin, url, data
    return preferred, '', {}


def video_context(leaf_data, course_id, sku_id, classroom):
    return {
        'v': str(leaf_data['id']), 'u': str(leaf_data['user_id']),
        'course': str(leaf_data.get('course_id') or course_id),
        'sku': leaf_data.get('sku_id') or int(sku_id),
        'classroom': str(leaf_data.get('classroom_id') or classroom),
        'university': leaf_data.get('university_id'),
    }


def progress_url_for(origin, context):
    return (f"{origin}/video-log/get_video_watch_progress/?cid={context['course']}"
            f"&user_id={context['u']}&classroom_id={context['classroom']}"
            f"&video_type=video&vtype=rate&video_id={context['v']}&snapshot=1")


def api_response_problem(response, payload):
    if not 200 <= response.status_code < 300:
        return f'HTTP {response.status_code}'
    if not isinstance(payload, dict):
        return '服务器返回了无法识别的响应格式'
    code = payload.get('error_code')
    if payload.get('success') is False or code not in (None, 0, '0', ''):
        if isinstance(code, (int, float)) or isinstance(code, str) and code.isdigit():
            return f'服务器拒绝请求（error_code={code}）'
        return '服务器拒绝请求（success=false）'
    return None


def progress_diagnostic(response, payload, video_id):
    """只展示响应状态和进度字段，不输出用户信息或凭据。"""
    parts = [f'HTTP {response.status_code}' if response is not None else '无 HTTP 响应']
    if isinstance(payload, dict):
        for field in ('success', 'error_code'):
            value = payload.get(field)
            if value is None or isinstance(value, (bool, int, float)):
                parts.append(f'{field}={value!r}')
            elif isinstance(value, str) and value.isdigit():
                parts.append(f'{field}={value}')
        data = payload.get('data')
        if isinstance(data, dict):
            ids = [str(key) for key in data if str(key).isdigit()]
            parts.append('data 中的视频编号=' + (','.join(ids[:10]) or '无'))
        else:
            parts.append('data 类型=' + type(data).__name__)
        record = get_video_progress(payload, video_id)
        for field in ('completed', 'rate', 'watch_length', 'video_length', 'last_point'):
            value = record.get(field)
            if isinstance(value, (bool, int, float)):
                parts.append(f'{field}={value}')
    return '；'.join(parts)


def query_video_progress(url, headers, video_id, wait_for_record=False):
    tries = 3 if wait_for_record else 1
    response, payload = None, {}
    for attempt in range(tries):
        try:
            response = session.get(url=url, headers=headers, timeout=15)
            try:
                payload = json.loads(response.text)
            except ValueError:
                payload = None
        except requests.RequestException:
            return {}, payload, response, '进度查询连接失败或超时'
        problem = api_response_problem(response, payload)
        if problem:
            return {}, payload, response, problem
        progress = get_video_progress(payload, video_id)
        if any(progress.get(field) is not None
               for field in ('completed', 'rate', 'watch_length', 'last_point')):
            return progress, payload, response, None
        if attempt + 1 < tries:
            delay = 2 * (attempt + 1)
            print(f'视频 {video_id} 进度记录暂未更新，等待 {delay} 秒后再次查询。')
            time.sleep(delay)
    return {}, payload, response, ('连续 3 次查询未返回进度记录'
                                  if wait_for_record else None)


def iter_video_duration_sources(media, payload, video_id, leaf, leaf_data):
    """只检查当前视频的已知时长字段，不猜测视频长度。"""
    payload = payload if isinstance(payload, dict) else {}
    data = payload.get('data')
    data = data if isinstance(data, dict) else {}
    leaf_data = leaf_data if isinstance(leaf_data, dict) else {}
    sources = (
        ('媒体信息', media),
        ('嵌套视频进度', data.get(video_id)),
        ('顶层视频进度', payload.get(video_id)),
        ('进度公共信息data', data),
        ('进度公共信息顶层', payload),
        ('课程目录', leaf),
        ('视频详情', leaf_data),
        ('内容信息', leaf_data.get('content_info')),
    )
    for name, source in sources:
        if isinstance(source, dict):
            yield name, source


def get_video_duration(media, payload, video_id, leaf, leaf_data):
    """读取有限、正数的秒数，兼容字符串及小数时长。"""
    for _, source in iter_video_duration_sources(
            media, payload, video_id, leaf, leaf_data):
        for field in ('duration', 'video_length'):
            value = source.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                continue
            try:
                seconds = float(value)
            except (ValueError, OverflowError):
                continue
            if 0 < seconds < float('inf'):
                return seconds
    return 0


def describe_video_duration(media, payload, video_id, leaf, leaf_data):
    """诊断仅包含时长字段，不输出登录信息或播放地址。"""
    fields = []
    for name, source in iter_video_duration_sources(
            media, payload, video_id, leaf, leaf_data):
        for field in ('duration', 'video_length'):
            if field not in source:
                continue
            value = source[field]
            if isinstance(value, str):
                try:
                    value = float(value)
                except (ValueError, OverflowError):
                    value = '<非数值字符串>'
            elif value is not None and not isinstance(value, (bool, int, float)):
                value = '<非数值类型>'
            fields.append(f'{name}.{field}={value!r}')
    return '；'.join(fields) or '各来源均无 duration / video_length 字段'


def iter_chapter_leaves(chapter):
    """遍历小节及章节直属内容，同一章节内相同资源只处理一次。"""
    groups = [(index, section.get('leaf_list') or [])
              for index, section in enumerate(chapter.get('section_list') or [])]
    groups.append((-1, chapter.get('leaf_list') or []))
    seen = set()
    for section_index, leaves in groups:
        for leaf in leaves:
            video_id = str(leaf['id'])
            if video_id in seen:
                continue
            seen.add(video_id)
            yield section_index, leaf


def ssxx():
    unconfirmed_videos = set()
    url = 'https://www.yuketang.cn/v2/api/web/courses/list?identity=2'

    response = session.get(url=url)

    JSON = json.loads(response.text)

    if len(JSON['data']['list']) > 1:
        for i in range(0, len(JSON['data']['list'])):
            print("序号：" + str(i) + "-----" + JSON['data']['list'][i]['name'])
        print('---------------------------------------------')
        print('---------------------------------------------')
        print('---------------------------------------------')
        print('---------------------------------------------')

        min_value = 0  # 定义范围的最小值
        max_value = len(JSON['data']['list']) - 1  # 定义范围的最大值

        while True:
            user_input = input(f"请输入您想观看的课程序号：\n")
            try:
                num = int(user_input)
                if num >= min_value and num <= max_value:

                    global classroom_id
                    classroom_id = str(JSON['data']['list'][num]['classroom_id'])

                    url = "https://www.yuketang.cn/v2/api/web/logs/learn/" + str(
                        classroom_id) + "?actype=-1&page=0&offset=20&sort=-1"
                    response = session.get(url)

                    JSON = json.loads(response.text)

                    break
                else:
                    print(f"输入错误，请输入一个介于 {min_value} 和 {max_value} 之间的课程编号。")
            except ValueError:
                print("输入错误，请确保您输入的是一个整数。")

    else:
        print("你没选课？！ 你疯啦？")
        exit(-1)

    url = 'https://www.yuketang.cn/c27/online_courseware/xty/kls/pub_news/' + str(
        JSON['data']['activities'][1]['courseware_id']) + '/'
    headers = {
        'xtbz': 'ykt',
        'classroom-id': str(classroom_id)
    }
    response = session.get(url, headers=headers)

    JSON = json.loads(response.text)
    c_course_id = str(JSON['data']['course_id'])
    s_id = str(JSON['data']['s_id'])

    for i, chapter in enumerate(JSON['data']['content_info']):
        print(f"正在观看----{JSON['data']['c_short_name']} 第{i + 1}章----共找到"
              f"{len(chapter.get('section_list') or [])}节内容。")
        for j, leaf in iter_chapter_leaves(chapter):
            position = f'第{i + 1}章直属' if j == -1 else f'第{i + 1}.{j + 1}节'
            video_id = str(leaf['id'])
            video_origin, leaf_info_url, leaf_data = fetch_video_leaf(classroom_id, video_id)
            if not leaf_data:
                print(f'内容 {video_id} 无法取得有效详情，本轮未能确认完成。')
                unconfirmed_videos.add(video_id)
                continue
            content = leaf_data.get('content_info') if isinstance(leaf_data, dict) else None
            media = content.get('media') if isinstance(content, dict) else None
            if not isinstance(media, dict) or not media.get('ccid'):
                print(f"{position}的内容 {video_id} 没有可用视频信息，跳过。")
                continue

            print(f"正在处理{position}：{leaf.get('name') or video_id}（视频 {video_id}）")
            ccid = media['ccid']

            context = video_context(leaf_data, c_course_id, s_id, classroom_id)
            headers = video_request_headers(video_origin, context['classroom'], context['university'])
            progress_url = progress_url_for(video_origin, context)
            video_progress, JSON_NEW, response_new, problem = query_video_progress(
                progress_url, headers, context['v'])
            if problem:
                print(f'视频 {video_id} 无法查询进度：{problem}。')
                print('进度诊断：' + progress_diagnostic(response_new, JSON_NEW, context['v']))
                unconfirmed_videos.add(video_id)
                continue
            sunci = 1 if video_is_complete(video_progress) else 0
            if sunci == 1:
                print(f"视频 {video_id} 已完成，跳过。")
                continue

            d = get_video_duration(media, JSON_NEW, context['v'], leaf, leaf_data)
            if d <= 0:
                for retry in range(1, 3):
                    print(f"视频 {video_id} 时长暂未返回，正在重新查询（{retry}/2）。")
                    time.sleep(1)
                    try:
                        response = session.get(url=leaf_info_url, headers=headers, timeout=15)
                        retry_payload = json.loads(response.text)
                    except (requests.RequestException, ValueError):
                        print(f'视频 {video_id} 详情重试失败或超时。')
                        continue
                    if api_response_problem(response, retry_payload):
                        print(f'视频 {video_id} 详情重试未返回有效响应。')
                        continue
                    retry_leaf_data = (retry_payload.get('data')
                                       if isinstance(retry_payload, dict) else None)
                    if (not isinstance(retry_leaf_data, dict)
                            or not retry_leaf_data.get('id')
                            or retry_leaf_data.get('user_id') is None):
                        continue
                    leaf_data = retry_leaf_data
                    retry_content = leaf_data.get('content_info')
                    retry_media = (retry_content.get('media')
                                   if isinstance(retry_content, dict) else None)
                    if isinstance(retry_media, dict) and retry_media.get('ccid'):
                        media = retry_media
                        ccid = media['ccid']
                    context = video_context(leaf_data, c_course_id, s_id, classroom_id)
                    headers = video_request_headers(video_origin, context['classroom'], context['university'])
                    progress_url = progress_url_for(video_origin, context)
                    video_progress, JSON_NEW, response_new, problem = query_video_progress(
                        progress_url, headers, context['v'])
                    sunci = 1 if video_is_complete(video_progress) else 0
                    d = get_video_duration(media, JSON_NEW, context['v'], leaf, leaf_data)
                    if problem or sunci == 1 or d > 0:
                        break

            if problem:
                print(f'视频 {video_id} 无法查询进度：{problem}。')
                unconfirmed_videos.add(video_id)
                continue

            if sunci == 1:
                print(f"视频 {video_id} 已完成，跳过。")
                continue

            if d <= 0:
                print(f"视频 {video_id} 课程信息未提供时长，正在读取播放资源元数据。")
                d, duration_source = read_playback_duration(
                    session, media, PLAYBACK_ORIGINS, headers,
                    media_origin=video_origin,
                    headers_for_origin=lambda origin: video_request_headers(
                        origin, context['classroom'], context['university']))
                if d > 0:
                    print(f"视频 {video_id} 已确认时长 {d:g} 秒（来源：{duration_source}）。")
                else:
                    print(f"视频 {video_id} 播放资源诊断：{duration_source}。")

            if d <= 0:
                print(f"视频 {video_id} 未返回有效时长，跳过该视频，请稍后重试。")
                print('时长诊断：' + describe_video_duration(
                    media, JSON_NEW, video_id, leaf, leaf_data))
                unconfirmed_videos.add(video_id)
                continue

            stopped = False
            rounds = 0
            sequence = 0
            page_id = f"{context['v']}_{int(time.time() * 1000):x}"
            while sunci != 1 and not stopped and rounds < 3:
                rounds += 1
                for k in range(25):
                    time.sleep(0.6)
                    sequence += 1
                    heart_data = {
                        'i': 5, 'et': 'heartbeat', 'p': 'web',
                        'n': 'ali-cdn.xuetangx.com',
                        'lob': 'ykt' if video_origin == 'https://www.yuketang.cn' else 'cloud4',
                        'cp': d * (k + 1) / 25, 'fp': 0, 'tp': 0, 'sp': 5,
                        'ts': str(int(time.time() * 1000)), 'u': int(context['u']),
                        'uip': '', 'c': int(context['course']), 'v': int(context['v']),
                        'skuid': context['sku'], 'classroomid': context['classroom'],
                        'cc': ccid, 'd': d, 'pg': page_id, 'sq': sequence,
                        't': 'video', 'cards_id': 0, 'slide': 0, 'v_url': '',
                    }
                    try:
                        response = session.post(url=video_origin + '/video-log/heartbeat/',
                                                data=json.dumps({'heart_data': [heart_data]}),
                                                headers=headers, timeout=15)
                        try:
                            submission = json.loads(response.text)
                        except ValueError:
                            submission = None
                        problem = api_response_problem(response, submission)
                    except requests.RequestException:
                        problem = '提交连接失败或超时'
                    if problem:
                        print(f'视频 {video_id} 进度提交失败：{problem}。')
                        unconfirmed_videos.add(video_id)
                        stopped = True
                        break

                    print(f"正在处理{position} 视频{video_id}----已提交进度：{4 * (k + 1)}%，正在确认。")
                    video_progress, JSON_NEW, response_new, problem = query_video_progress(
                        progress_url, headers, context['v'], wait_for_record=True)
                    if problem:
                        print(f'视频 {video_id} {problem}，本轮跳过。')
                        print('进度诊断：' + progress_diagnostic(response_new, JSON_NEW, context['v']))
                        unconfirmed_videos.add(video_id)
                        stopped = True
                        break
                    sunci = 1 if video_is_complete(video_progress) else 0
                    if sunci == 1:
                        print(f'视频 {video_id} 已由服务器确认完成。')
                        break
            if sunci != 1 and not stopped:
                print(f'视频 {video_id} 处理 3 轮后仍未由服务器确认完成，本轮停止。')
                unconfirmed_videos.add(video_id)
    if unconfirmed_videos:
        print(f"本轮处理结束，有 {len(unconfirmed_videos)} 个视频未能确认完成，请检查上方提示。")
    else:
        print("这门课看完了啊！ 孙辞期待与您的下次相遇！")







# 运行异步函数
if __name__ == '__main__':
    asyncio.run(websocket_session())
