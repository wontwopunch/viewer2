from flask import Flask, send_file, jsonify, request, send_from_directory, make_response, abort
import openslide
from flask_cors import CORS
import os
import json
from functools import lru_cache
from cachetools import TTLCache, LRUCache
import io
import PIL.Image
import PIL.ImageDraw
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
from werkzeug.middleware.proxy_fix import ProxyFix
import gc
import psutil
import time
from threading import Timer
import re
from werkzeug.serving import run_simple
from datetime import datetime
import numpy as np
import logging
from logging.handlers import RotatingFileHandler
from PIL import ImageFont, ImageDraw
import math
# 먼저 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
DATA_FOLDER = os.path.join(BASE_DIR, 'data')
STATIC_FOLDER = BASE_DIR  # 여기서 정의
PUBLIC_FILES_PATH = os.path.join(BASE_DIR, 'public_files.json')

# 그 다음 Flask 앱 초기화
app = Flask(__name__, static_url_path='', static_folder=STATIC_FOLDER)
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "DELETE", "PUT", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"]
    }
})

# 미들웨어 설정
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# 보안 설정
ALLOWED_PATHS = [
    r'^/$', r'^/static/.*', r'^/slide/.*\.svs/.*$', r'^/public/.*\.svs/.*$',
    r'^/viewer\.html$', r'^/dashboard\.html$', r'^/files$', r'^/files/.*$',
    r'^/upload$', r'^/status$', r'^/debug_images/.*$',
]

@app.before_request
def security_check():
    path = request.path
    if not any(re.match(pattern, path) for pattern in ALLOWED_PATHS):
        return abort(404)
    if any(bad in path.lower() for bad in ['.env', 'admin', 'login', 'console', 'api']):
        return abort(404)
    if request.method not in ['GET', 'POST', 'OPTIONS', 'DELETE']:
        return abort(405)
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        return response

# 에러 핸들러 추가
@app.errorhandler(404)
def not_found(e):
    return '', 404

@app.errorhandler(405)
def method_not_allowed(e):
    return '', 405

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
if not os.path.exists(DATA_FOLDER):
    os.makedirs(DATA_FOLDER)

def get_data_path(filename):
    return os.path.join(DATA_FOLDER, f"{filename}.json")

def load_file_data(filename):
    data_path = get_data_path(filename)
    if os.path.exists(data_path):
        with open(data_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'memos': [], 'annotations': []}

def save_file_data(filename, data):
    data_path = get_data_path(filename)
    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route('/')
def index():
    return send_from_directory(STATIC_FOLDER, 'index.html')

@app.route('/<path:filename>')
def serve_file(filename):
    # SVS 파일 공유 링크 처리
    if filename.endswith('.svs'):
        if filename not in public_files or not public_files[filename]:
            return "File not found or not public", 404
        return send_file('viewer.html')
    
    # 정적 파일 처리
    try:
        if os.path.exists(os.path.join(STATIC_FOLDER, filename)):
            print(f"Serving static file: {filename}")  # 디버그 로그 추가
            return send_from_directory(STATIC_FOLDER, filename)
        print(f"File not found: {filename}")  # 디버그 로그 추가
        return "File not found", 404
    except Exception as e:
        print(f"Error serving static file: {str(e)}")
        return "File not found", 404

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': '파일이 없습니다'}), 400

        file = request.files['file']
        if file and file.filename.endswith('.svs'):
            filename = file.filename
            save_path = os.path.join(UPLOAD_FOLDER, filename)
            file.save(save_path)

            # ✅ 디버그 이미지 생성 호출
            slide = openslide.OpenSlide(save_path)
            get_center_of_tissue(slide, filename)

            return jsonify({'message': '업로드 성공', 'filename': filename})
        return jsonify({'error': 'SVS 파일만 지원됩니다'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# 전역 상수 수정
TILE_SIZE = 2048  # 512에서 2048로 변경
JPEG_QUALITY = 40  # 50에서 40으로 감소
MAX_WORKERS = 4   # 8에서 4로 감소

# 스레드 풀과 캐시 설정 조정
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
slide_cache = TTLCache(maxsize=2, ttl=900)    # 더 작게
tile_cache = TTLCache(maxsize=200, ttl=600)   # 더 작게

TILE_CACHE_DIR = 'tile_cache'
if not os.path.exists(TILE_CACHE_DIR):
    os.makedirs(TILE_CACHE_DIR)

def get_tile_cache_path(filename, level, x, y):
    basename = os.path.splitext(filename)[0]  # 확장자 제거
    cache_dir = os.path.join(TILE_CACHE_DIR, basename, str(level))
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{x}_{y}.jpg")

def get_slide(slide_path):
    if slide_path not in slide_cache:
        slide_cache[slide_path] = openslide.OpenSlide(slide_path)
    return slide_cache[slide_path]

# 메모리 관리 상수 수정
MAX_MEMORY_GB = 4.0  # 6GB에서 4GB로 감소
GC_INTERVAL = 60     # 300초에서 60초로 감소

def check_memory_usage():
    try:
        process = psutil.Process()
        memory_gb = process.memory_info().rss / 1024 / 1024 / 1024
        
        if memory_gb > MAX_MEMORY_GB:
            print(f"Memory usage ({memory_gb:.2f}GB) exceeded limit. Clearing caches...")
            tile_cache.clear()
            
            # 슬라이드 캐시도 정리
            for key in list(slide_cache.keys()):
                slide = slide_cache[key]
                slide.close()  # OpenSlide 객체 닫기
                del slide_cache[key]
            
            gc.collect()
            print(f"Memory after cleanup: {process.memory_info().rss / 1024 / 1024 / 1024:.2f}GB")
    except Exception as e:
        print(f"Error in memory check: {str(e)}")

# 주기적 메모리 체크 함수
def periodic_memory_check():
    check_memory_usage()
    Timer(GC_INTERVAL, periodic_memory_check).start()

# 메모리 체크 시작
periodic_memory_check()

def create_tile(slide, level, x, y, tile_size, filename):
    try:
        cache_key = f"{filename}_{level}_{x}_{y}"
        
        if cache_key in tile_cache:
            return tile_cache[cache_key]
            
        # 메모리 체크는 여기서만 수행
        process = psutil.Process()
        if process.memory_info().rss / 1024 / 1024 / 1024 > MAX_MEMORY_GB * 0.8:
            print("Memory threshold reached, clearing caches...")
            tile_cache.clear()
            gc.collect()
        
        # 타일 크기 계산 단순화
        x_pos = x * TILE_SIZE
        y_pos = y * TILE_SIZE
        
        # 경계 체크
        if x_pos >= slide.dimensions[0] or y_pos >= slide.dimensions[1]:
            print(f"Invalid tile coordinates: {x_pos}, {y_pos}")
            return None
            
        # 읽기 크기 계산
        read_size = TILE_SIZE
        if x_pos + read_size > slide.dimensions[0]:
            read_size = slide.dimensions[0] - x_pos
        if y_pos + read_size > slide.dimensions[1]:
            read_size = slide.dimensions[1] - y_pos
            
        try:
            # 타일 읽기 시도
            tile = slide.read_region((x_pos, y_pos), level, (read_size, read_size))
            tile = tile.convert('RGB')
            
            # 타일 경계를 부드럽게 만들기 위한 처리 추가
            # 약간의 블러 효과 추가 (선택사항, 품질에 따라 조정)
            # tile = tile.filter(PIL.ImageFilter.SMOOTH)
            
            # 크기 조정이 필요한 경우
            if read_size != TILE_SIZE:
                tile = tile.resize((TILE_SIZE, TILE_SIZE), PIL.Image.Resampling.BILINEAR)  # LANCZOS 대신 BILINEAR 사용
            
            # 메모리 절약을 위한 JPEG 압축
            output = io.BytesIO()
            tile.save(output, format='JPEG', quality=85)
            output.seek(0)
            
            # 캐시에 저장
            tile_cache[cache_key] = output.getvalue()  # 바이트 데이터로 저장
            return PIL.Image.open(io.BytesIO(tile_cache[cache_key]))
            
        except Exception as inner_e:
            print(f"Error reading tile: {str(inner_e)}")
            return None
            
    except Exception as e:
        print(f"Error in create_tile: {str(e)}")
        return None

# 로딩 상태 추적을 위한 변수 추가
loading_tiles = set()

@app.route('/slide/<filename>/tile/<int:level>/<int:x>/<int:y>')
def get_tile(filename, level, x, y):
    try:
        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(slide_path): return jsonify({'error': '파일 없음'}), 404
        slide = get_slide(slide_path)

        level = min(level, slide.level_count - 1)
        downsample = slide.level_downsamples[level]
        tile_size = TILE_SIZE

        x_pos_0 = int(x * tile_size * downsample)
        y_pos_0 = int(y * tile_size * downsample)
        read_width_0 = int(min(tile_size * downsample, slide.dimensions[0] - x_pos_0))
        read_height_0 = int(min(tile_size * downsample, slide.dimensions[1] - y_pos_0))

        if read_width_0 <= 0 or read_height_0 <= 0:
            return create_debug_tile("⚠️ 읽기 크기 비정상", x, y, level)

        region_size = (int(read_width_0 / downsample), int(read_height_0 / downsample))
        tile = slide.read_region((x_pos_0, y_pos_0), level, region_size).convert('RGB')
        if tile.size != (tile_size, tile_size):
            tile = tile.resize((tile_size, tile_size), PIL.Image.LANCZOS)

        output = io.BytesIO()
        tile.save(output, format='JPEG')
        output.seek(0)
        return send_file(output, mimetype='image/jpeg')
    except Exception as e:
        return send_file(create_debug_tile(f"오류: {str(e)}", x, y, level), mimetype='image/jpeg')



# 디버그 타일 생성 함수 개선
def create_debug_tile(message="Error", x=None, y=None, level=None):
    """디버그 정보가 포함된 타일 생성 및 CORS 포함 응답"""
    tile_size = 1024
    tile = PIL.Image.new('RGB', (tile_size, tile_size), (255, 200, 200))
    draw = PIL.ImageDraw.Draw(tile)

    font = ImageFont.load_default()

    for i in range(0, tile_size, 100):
        draw.line([(0, i), (tile_size, i)], fill=(200, 200, 200), width=1)
        draw.line([(i, 0), (i, tile_size)], fill=(200, 200, 200), width=1)

    draw.text((100, 100), "DEBUG TILE", fill=(0, 0, 0), font=font)
    draw.text((100, 150), message, fill=(255, 0, 0), font=font)

    # level, x, y 좌표 추가 정보 출력
    if level is not None and x is not None and y is not None:
        draw.text((100, 200), f"Level: {level}", fill=(0, 0, 255), font=font)
        draw.text((100, 220), f"X: {x}", fill=(0, 0, 255), font=font)
        draw.text((100, 240), f"Y: {y}", fill=(0, 0, 255), font=font)

    output = io.BytesIO()
    tile.save(output, format='JPEG')
    output.seek(0)

    response = send_file(output, mimetype='image/jpeg', as_attachment=False)
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response



def load_public_files():
    try:
        if os.path.exists(PUBLIC_FILES_PATH):
            with open(PUBLIC_FILES_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                print(f"Loaded public files from: {PUBLIC_FILES_PATH}")
                print(f"Loaded data: {data}")
                return data
        else:
            print(f"Public files path does not exist: {PUBLIC_FILES_PATH}")
            save_public_files({})
    except Exception as e:
        print(f"Error loading public files: {str(e)}")
        print(f"Current working directory: {os.getcwd()}")
    return {}

def save_public_files(data=None):
    try:
        if data is None:
            data = public_files
        os.makedirs(os.path.dirname(PUBLIC_FILES_PATH), exist_ok=True)
        
        with open(PUBLIC_FILES_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"Successfully saved public files to: {PUBLIC_FILES_PATH}")
            print(f"Saved content: {data}")
        
        if os.path.exists(PUBLIC_FILES_PATH):
            with open(PUBLIC_FILES_PATH, 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
                print(f"Verification - read back data: {saved_data}")
                return True
        return False
    except Exception as e:
        print(f"Error saving public files: {str(e)}")
        print(f"Current working directory: {os.getcwd()}")
        return False


@app.route('/files')
def get_files():
    try:
        files = []
        for filename in os.listdir(UPLOAD_FOLDER):
            if filename.endswith('.svs'):
                file_path = os.path.join(UPLOAD_FOLDER, filename)
                stat = os.stat(file_path)
                files.append({
                    'name': filename,
                    'date': stat.st_mtime,
                    'size': stat.st_size,
                })
        return jsonify(files)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/files/<filename>', methods=['DELETE'])
def delete_file(filename):
    try:
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(file_path):
            os.remove(file_path)

            basename = os.path.splitext(filename)[0]

            # 타일 캐시 삭제 (타일 방식 유지 시)
            cache_dir = os.path.join(TILE_CACHE_DIR, basename)
            if os.path.exists(cache_dir):
                import shutil
                shutil.rmtree(cache_dir)

            # slide 캐시 정리
            slide_path = os.path.join(UPLOAD_FOLDER, filename)
            if slide_path in slide_cache:
                del slide_cache[slide_path]

            # 타일 캐시 키 제거
            prefix = f"{slide_path}_"
            keys_to_delete = [k for k in tile_cache.keys() if k.startswith(prefix)]
            for key in keys_to_delete:
                del tile_cache[key]

            # 데이터 파일 삭제
            data_path = get_data_path(filename)
            if os.path.exists(data_path):
                os.remove(data_path)

            # 공개 상태 제거
            if filename in public_files:
                del public_files[filename]
                save_public_files()

            # ✅ debug_center 이미지도 삭제
            debug_img_path = os.path.join(BASE_DIR, 'debug_images', f"{basename}_debug_center.jpg")
            if os.path.exists(debug_img_path):
                os.remove(debug_img_path)
                print(f"🗑 debug 이미지 삭제됨: {debug_img_path}")

            return jsonify({'message': '파일이 삭제되었습니다'})

        return jsonify({'error': '파일을 찾을 수 없습니다'}), 404
    except Exception as e:
        print(f"Error deleting file: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/files/<filename>/rename', methods=['POST'])
def rename_file(filename):
    try:
        data = request.json
        new_name = data.get('newName')
        if not new_name:
            return jsonify({'error': '새 파일명이 필요합니다'}), 400

        old_path = os.path.join(UPLOAD_FOLDER, filename)
        new_path = os.path.join(UPLOAD_FOLDER, new_name)
        
        if not os.path.exists(old_path):
            return jsonify({'error': '파일을 찾을 수 없습니다'}), 404
        
        if os.path.exists(new_path):
            return jsonify({'error': '이미 존재하는 파일명입니다'}), 400
        
        os.rename(old_path, new_path)
        
        old_data_path = get_data_path(filename)
        new_data_path = get_data_path(new_name)
        if os.path.exists(old_data_path):
            os.rename(old_data_path, new_data_path)
        
        return jsonify({'message': '파일 이름이 변경되었습니다'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/slide/<filename>/info')
def get_slide_info(filename):
    try:
        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(slide_path):
            return jsonify({'error': 'Slide not found'}), 404

        slide = slide_cache.get(slide_path)
        if slide is None:
            slide = openslide.OpenSlide(slide_path)
            slide_cache[slide_path] = slide

        tile_width = TILE_SIZE
        tile_height = TILE_SIZE

        tiles_per_level = []
        for (w, h) in slide.level_dimensions:
            tiles_x = math.ceil(w / tile_width)
            tiles_y = math.ceil(h / tile_height)
            tiles_per_level.append([tiles_x, tiles_y])

        # ✅ 조직 중심 자동 탐지 + 디버그 이미지 저장
        center_hint = get_center_of_tissue(slide, filename)

        info = {
            'dimensions': slide.dimensions,
            'level_count': slide.level_count,
            'level_dimensions': slide.level_dimensions,
            'level_downsamples': [float(ds) for ds in slide.level_downsamples],
            'tile_size': [tile_width, tile_height],
            'tiles_per_level': tiles_per_level,
            'center_hint': center_hint,
            'properties': dict(slide.properties)
        }

        print("🧭 중심 좌표 center_hint:", center_hint)
        return jsonify(info)

    except Exception as e:
        return jsonify({'error': str(e)}), 500




def get_center_of_tissue(slide, filename):
    level = 2 if slide.level_count > 2 else slide.level_count - 1
    downsample = slide.level_downsamples[level]
    w, h = slide.level_dimensions[level]

    arr = np.array(slide.read_region((0, 0), level, (w, h)).convert("L"))
    mask = arr < 220

    if not np.any(mask):
        cx = slide.dimensions[0] // 2
        cy = slide.dimensions[1] // 2
    else:
        y_coords, x_coords = np.where(mask)
        cx = int(np.median(x_coords) * downsample)
        cy = int(np.median(y_coords) * downsample)

    rgb = slide.read_region((0, 0), level, (w, h)).convert("RGB")
    draw = ImageDraw.Draw(rgb)
    draw.ellipse((int(cx/downsample)-5, int(cy/downsample)-5, int(cx/downsample)+5, int(cy/downsample)+5), fill=(255, 0, 0))

    os.makedirs(os.path.join(BASE_DIR, 'debug_images'), exist_ok=True)
    save_path = os.path.join(BASE_DIR, 'debug_images', f'{filename}_debug_center.jpg')
    rgb.save(save_path)
    print(f"✅ 조직 중심 이미지 저장됨: {save_path}")



@app.route('/public_data/<filename>')
def get_public_data(filename):
    if filename not in public_files or not public_files[filename]:
        return abort(404)
    return jsonify(load_file_data(filename))



@app.route('/slide/<filename>/data', methods=['POST'])
def save_slide_data(filename):
    try:
        data = request.json
        save_file_data(filename, data)
        return jsonify({'message': '저장되었습니다'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if not os.path.exists(PUBLIC_FILES_PATH):
    print(f"Creating initial public_files.json at: {PUBLIC_FILES_PATH}")
    save_public_files({})

public_files = load_public_files()
print(f"Initial public files state: {public_files}")


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(STATIC_FOLDER, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/slide/<filename>/check')
def check_slide(filename):
    try:
        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        
        # 파일 존재 확인
        if not os.path.exists(slide_path):
            return jsonify({
                'status': 'error',
                'message': '파일을 찾을 수 없습니다',
                'path': slide_path
            })
        
        # 파일 크기 확인
        file_size = os.path.getsize(slide_path) / (1024 * 1024)  # MB 단위
        
        result = {
            'status': 'checking',
            'filename': filename,
            'path': slide_path,
            'filesize_mb': round(file_size, 2),
            'exists': True,
            'details': []
        }
        
        # OpenSlide로 파일 열기 시도
        try:
            slide = openslide.OpenSlide(slide_path)
            result['status'] = 'success'
            result['can_open'] = True
            result['dimensions'] = slide.dimensions
            result['level_count'] = slide.level_count
            result['level_dimensions'] = slide.level_dimensions
            result['level_downsamples'] = [float(ds) for ds in slide.level_downsamples]
            result['properties'] = dict(slide.properties)
            
            # 첫 번째 타일 읽기 시도
            try:
                test_tile = slide.read_region((0, 0), 0, (256, 256))
                result['can_read_tile'] = True
                result['details'].append("타일 읽기 성공")
                
                # 타일 이미지 상태 확인
                tile_array = np.array(test_tile)
                if np.all(tile_array[:,:,0:3] == 255):  # 모든 픽셀이 흰색인지 확인
                    result['details'].append("경고: 타일 내용이 모두 흰색입니다")
                else:
                    result['details'].append("타일에 내용이 있습니다")
            except Exception as e:
                result['can_read_tile'] = False
                result['details'].append(f"타일 읽기 오류: {str(e)}")
                
        except Exception as e:
            result['status'] = 'error'
            result['can_open'] = False
            result['error'] = str(e)
            result['details'].append(f"파일 열기 오류: {str(e)}")
        
        return jsonify(result)
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        })

@app.route('/slide/<filename>/simple_tile/<int:level>/<int:x>/<int:y>')
def get_simple_tile(filename, level, x, y):
    try:
        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        print(f"📥 요청된 파일 경로: {slide_path}")
        print(f"🔍 요청된 타일: level={level}, x={x}, y={y}")

        if not os.path.exists(slide_path):
            return create_debug_tile("타일 오류: 파일 없음")

        slide = slide_cache.get(slide_path)
        if slide is None:
            slide = openslide.OpenSlide(slide_path)
            slide_cache[slide_path] = slide

        level = min(level, slide.level_count - 1)
        downsample = slide.level_downsamples[level]
        tile_size = TILE_SIZE

        x_pos_0 = int(x * tile_size * downsample)
        y_pos_0 = int(y * tile_size * downsample)

        read_width_0 = int(min(tile_size * downsample, slide.dimensions[0] - x_pos_0))
        read_height_0 = int(min(tile_size * downsample, slide.dimensions[1] - y_pos_0))

        if read_width_0 <= 0 or read_height_0 <= 0:
            return create_debug_tile("⚠️ 읽기 크기 비정상", x, y, level)

        region_size = (int(read_width_0 / downsample), int(read_height_0 / downsample))
        region = slide.read_region((x_pos_0, y_pos_0), level, region_size).convert('RGB')

        if region.size != (tile_size, tile_size):
            region = region.resize((tile_size, tile_size), PIL.Image.LANCZOS)

        output = io.BytesIO()
        region.save(output, format='JPEG')
        output.seek(0)
        return send_file(output, mimetype='image/jpeg')
    except Exception as e:
        return send_file(create_debug_tile(str(e), x, y, level), mimetype='image/jpeg')



# 디버그 엔드포인트 추가 - 서버 상태 확인용
@app.route('/status')
def server_status():
    return jsonify({
        'status': 'online',
        'memory': {
            'used_gb': psutil.Process().memory_info().rss / (1024 * 1024 * 1024),
            'percent': psutil.virtual_memory().percent
        },
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


@app.route('/debug_images/<path:filename>')
def serve_debug_image(filename):
    debug_dir = os.path.join(BASE_DIR, 'debug_images')
    path = os.path.join(debug_dir, filename)

    if os.path.exists(path):
        return send_file(path, mimetype='image/jpeg', as_attachment=False)
    else:
        return 'debug_center image not found', 404


@app.route('/public_data/<filename>', methods=['GET'])
def get_public_memo_data(filename):
    if filename not in public_files or not public_files[filename]:
        return jsonify({'error': '파일이 공개 상태가 아닙니다'}), 403

    try:
        data = load_file_data(filename)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/slide/<filename>/data', methods=['GET'])
def get_slide_data(filename):
    try:
        return jsonify(load_file_data(filename))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

        

# Flask 앱 초기화 후에 추가
if __name__ != '__main__':
    # 로그 파일 설정
    handler = RotatingFileHandler('server.log', maxBytes=10000, backupCount=1)
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)

if __name__ == '__main__':
    # 디렉터리 확인 및 생성
    for directory in [UPLOAD_FOLDER, DATA_FOLDER, TILE_CACHE_DIR]:
        os.makedirs(directory, exist_ok=True)
    
    # 공개 파일 로드
    public_files = load_public_files()
    print(f"Initial public files state: {public_files}")
    
    # 디버그 모드에서 실행 (개발 중에만)
    app.run(host='0.0.0.0', port=5000, debug=False)