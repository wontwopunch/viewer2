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
from PIL import ImageFont
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
    r'^/$',
    r'^/static/.*',
    r'^/slide/.*\.svs/.*$',
    r'^/public/.*\.svs/.*$',
    r'^/viewer\.html$',
    r'^/dashboard\.html$',
    r'^/files$',
    r'^/files/.*$',
    r'^/upload$',  # upload 경로 추가 확인
    r'^/status$',
]

@app.before_request
def security_check():
    # 허용된 경로가 아니면 차단
    path = request.path
    if not any(re.match(pattern, path) for pattern in ALLOWED_PATHS):
        print(f"Blocked unauthorized access to: {path}")
        return abort(404)
    
    # 악의적인 문자열 체크
    if any(bad in path.lower() for bad in ['.env', 'admin', 'login', 'console', 'api']):
        print(f"Blocked suspicious request to: {path}")
        return abort(404)
    
    # 메소드 제한 수정
    if request.method not in ['GET', 'POST', 'OPTIONS', 'DELETE']:  # OPTIONS와 DELETE 추가
        return abort(405)

    # OPTIONS 요청 처리
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
        if file.filename == '':
            return jsonify({'error': '선택된 파일이 없습니다'}), 400
        
        if file and file.filename.endswith('.svs'):
            filename = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(filename)
            print(f'File saved to: {filename}')  # 디버그 로그 추가
            return jsonify({'message': '파일이 업로드되었습니다', 'filename': file.filename})
        
        return jsonify({'error': 'SVS 파일만 업로드 가능합니다'}), 400
    except Exception as e:
        print(f'Upload error: {str(e)}')  # 디버그 로그 추가
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
    basename = os.path.splitext(filename)[0]
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
        factor = slide.level_downsamples[level]
        x_pos = int(x * TILE_SIZE * factor)
        y_pos = int(y * TILE_SIZE * factor)
        
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
            tile.save(output, format='JPEG', quality=JPEG_QUALITY, optimize=True)
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
        print(f"\n===== 타일 요청: {filename}, level={level}, x={x}, y={y} =====")

        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(slide_path):
            print(f"❌ 슬라이드 파일 없음: {slide_path}")
            return jsonify({'error': 'Slide not found'}), 404

        slide = slide_cache.get(slide_path)
        if slide is None:
            print(f"📂 슬라이드를 새로 엶: {slide_path}")
            try:
                slide = openslide.OpenSlide(slide_path)
                slide_cache[slide_path] = slide
            except Exception as e:
                import traceback
                print(f"🧨 슬라이드 열기 실패: {str(e)}")
                print(traceback.format_exc())
                return send_file(create_debug_tile(f"OpenSlide Error: {str(e)}"), mimetype='image/jpeg')
        else:
            print("✅ 슬라이드 캐시에서 불러옴")

        # 슬라이드 메타 정보
        width, height = slide.dimensions
        level = min(level, slide.level_count - 1)
        downsample = slide.level_downsamples[level]

        # 서버 기본 타일 크기
        tile_size = 2048

        # 타일 인덱스 최대값 계산 (뷰어가 요청 가능한 최대 x/y)
        tiles_x = math.ceil(width / (tile_size * downsample))
        tiles_y = math.ceil(height / (tile_size * downsample))

        if x >= tiles_x or y >= tiles_y:
            print(f"🚫 타일 인덱스 초과: x={x}, y={y}, max=({tiles_x - 1}, {tiles_y - 1})")
            return send_file(create_debug_tile(f"index 초과: x={x}, y={y}"), mimetype='image/jpeg')

        # 좌표 계산 (레벨 기준)
        x_pos = int(x * tile_size * downsample)
        y_pos = int(y * tile_size * downsample)

        read_width = min(tile_size, width - x_pos)
        read_height = min(tile_size, height - y_pos)

        print(f"📏 읽는 위치: ({x_pos}, {y_pos}), 크기: {read_width}x{read_height}")

        tile = slide.read_region((x_pos, y_pos), level, (read_width, read_height)).convert('RGB')

        if read_width != tile_size or read_height != tile_size:
            tile = tile.resize((tile_size, tile_size), PIL.Image.LANCZOS)

        output = io.BytesIO()
        tile.save(output, format='JPEG')
        output.seek(0)

        response = send_file(output, mimetype='image/jpeg', as_attachment=False)
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    except Exception as e:
        import traceback
        print(f"🧨 예외 발생: {str(e)}")
        print(traceback.format_exc())
        return send_file(create_debug_tile(f"타일 오류: {str(e)}"), mimetype='image/jpeg')



# 디버그 타일 생성 함수 추가
def create_debug_tile(message="Error"):
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

@app.route('/files/<filename>/toggle-public', methods=['POST'])
def toggle_file_public(filename):
    try:
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(file_path):
            return jsonify({'error': '파일을 찾을 수 없습니다'}), 404
        
        is_public = public_files.get(filename, False)
        public_files[filename] = not is_public
        
        if save_public_files():
            print(f"Successfully toggled public state for {filename}: {not is_public}")
            print(f"Current public files: {public_files}")
            return jsonify({
                'message': '파일이 {}되었습니다'.format('공개' if not is_public else '비공개'),
                'is_public': not is_public
            })
        else:
            return jsonify({'error': '상태 저장에 실패했습니다'}), 500
            
    except Exception as e:
        print(f"Error in toggle_public: {str(e)}")
        return jsonify({'error': str(e)}), 500

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
                    'is_public': public_files.get(filename, False),
                    'public_url': f'/public/{filename}' if public_files.get(filename, False) else None
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
            cache_dir = os.path.join(TILE_CACHE_DIR, basename)
            if os.path.exists(cache_dir):
                import shutil
                shutil.rmtree(cache_dir)
            
            slide_path = os.path.join(UPLOAD_FOLDER, filename)
            if slide_path in slide_cache:
                del slide_cache[slide_path]
            
            prefix = f"{slide_path}_"
            keys_to_delete = [k for k in tile_cache.keys() if k.startswith(prefix)]
            for key in keys_to_delete:
                del tile_cache[key]
            
            data_path = get_data_path(filename)
            if os.path.exists(data_path):
                os.remove(data_path)
            
            if filename in public_files:
                del public_files[filename]
                save_public_files()
                
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

@app.route('/public/<path:filename>')
def serve_public_file(filename):
    try:
        print(f"Accessing public file: {filename}")
        
        # SVS 파일 처리
        if filename.endswith('.svs'):
            if filename not in public_files:
                return "File not found", 404
            if not public_files[filename]:
                return "File is not public", 403
            return send_file('viewer.html')
        
        # static 파일 처리
        return send_from_directory(STATIC_FOLDER, filename)
        
    except Exception as e:
        print(f"Error serving public file: {str(e)}")
        return str(e), 500

@app.route('/slide/<filename>/info')
def get_slide_info(filename):
    try:
        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(slide_path):
            return jsonify({'error': 'Slide not found'}), 404
            
        if slide_path not in slide_cache:
            slide_cache[slide_path] = openslide.OpenSlide(slide_path)
        slide = slide_cache[slide_path]
        
        # 타일 크기를 정의
        tile_size = 2048
        
        # 상세 로그 추가
        print("=" * 50)
        print(f"SVS 파일 정보 요청: {filename}")
        print(f"원본 차원: {slide.dimensions}")
        print(f"레벨 수: {slide.level_count}")
        for i in range(slide.level_count):
            print(f"  레벨 {i}: {slide.level_dimensions[i]}, 축소 계수: {slide.level_downsamples[i]}")
        print("=" * 50)
        
        # SVS 파일 정보 반환
        tile_width = int(slide.properties.get("openslide.level[0].tile-width", 2048))
        tile_height = int(slide.properties.get("openslide.level[0].tile-height", 2048))

        info = {
            'dimensions': slide.dimensions,
            'level_count': slide.level_count,
            'level_dimensions': slide.level_dimensions,
            'level_downsamples': [float(ds) for ds in slide.level_downsamples],
            'tile_size': [tile_width, tile_height],  # ← 수정된 부분
            'properties': dict(slide.properties)
        }
        
        return jsonify(info)
    except Exception as e:
        print(f"Error in get_slide_info: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/slide/<filename>/data', methods=['GET'])
def get_slide_data(filename):
    try:
        data = load_file_data(filename)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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

@app.route('/public/<path:filename>/info')
def get_public_slide_info(filename):
    try:
        if filename not in public_files or not public_files[filename]:
            return jsonify({'error': 'File not accessible'}), 403
            
        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        
        if slide_path not in slide_cache:
            slide_cache[slide_path] = openslide.OpenSlide(slide_path)
        slide = slide_cache[slide_path]
        
        tile_size = 2048
        
        tile_width = int(slide.properties.get("openslide.level[0].tile-width", 2048))
        tile_height = int(slide.properties.get("openslide.level[0].tile-height", 2048))

        info = {
            'dimensions': slide.dimensions,
            'level_count': slide.level_count,
            'level_dimensions': slide.level_dimensions,
            'level_downsamples': [float(ds) for ds in slide.level_downsamples],
            'tile_size': [tile_width, tile_height],  # ← 수정된 부분
            'properties': dict(slide.properties)
        }
        
        return jsonify(info)
    except Exception as e:
        print(f"Error in get_public_slide_info: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/public/<path:filename>/tile/<int:level>/<int:x>/<int:y>')
def get_public_tile(filename, level, x, y):
    try:
        if filename not in public_files or not public_files[filename]:
            return jsonify({'error': 'File not accessible'}), 403
            
        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        tile_size = 2048
        
        cache_key = f"{slide_path}_{level}_{x}_{y}"
        
        # 캐시된 이미지가 있는지 확인
        cached_tile = tile_cache.get(cache_key)
        if cached_tile is not None:
            output = io.BytesIO()
            cached_tile.save(output, format='JPEG')
            output.seek(0)
            response = make_response(send_file(
                output,
                mimetype='image/jpeg',
                as_attachment=False
            ))
            response.headers['Cache-Control'] = 'public, max-age=3600'
            return response
        
        if slide_path not in slide_cache:
            slide_cache[slide_path] = openslide.OpenSlide(slide_path)
        slide = slide_cache[slide_path]
        
        tile = get_tile_image(slide_path, level, x, y, tile_size)
        # 타일 이미지를 캐시에 저장
        tile_cache[cache_key] = tile.copy()
        
        output = io.BytesIO()
        tile.save(output, format='JPEG')
        output.seek(0)
        
        response = make_response(send_file(
            output,
            mimetype='image/jpeg',
            as_attachment=False
        ))
        
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response
        
    except Exception as e:
        print(f"Error in get_public_tile: {str(e)}")
        return jsonify({'error': str(e)}), 500

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
            return create_debug_tile(f"타일 오류: {str(e)}")

        slide = slide_cache.get(slide_path)
        if slide is None:
            slide = openslide.OpenSlide(slide_path)
            slide_cache[slide_path] = slide

        # 슬라이드 기본 정보
        width, height = slide.dimensions
        level = min(level, slide.level_count - 1)
        downsample = slide.level_downsamples[level]

        # 타일 크기 동적으로 설정 (정사각형만 지원)
        tile_width = int(slide.properties.get("openslide.level[0].tile-width", 240))
        tile_height = int(slide.properties.get("openslide.level[0].tile-height", 240))
        tile_size = tile_width  # 둘 다 240이지만 정사각형 기준으로 고정

        # 타일 인덱스 유효성 검사
        tiles_x = math.ceil(width / tile_size)
        tiles_y = math.ceil(height / tile_size)
        if x >= tiles_x or y >= tiles_y:
            return create_debug_tile(f"타일 오류: {str(e)}")

        # 좌표 계산
        x_pos = int(x * tile_size * downsample)
        y_pos = int(y * tile_size * downsample)
        read_width = min(tile_size, width - x_pos)
        read_height = min(tile_size, height - y_pos)

        tile = slide.read_region((x_pos, y_pos), level, (read_width, read_height)).convert('RGB')

        # 필요 시 OpenSeadragon 맞춤 사이즈로 리사이즈
        if read_width != tile_size or read_height != tile_size:
            tile = tile.resize((tile_size, tile_size), PIL.Image.LANCZOS)

        output = io.BytesIO()
        tile.save(output, format='JPEG')
        output.seek(0)

        return send_file(output, mimetype='image/jpeg')

    except Exception as e:
        import traceback
        print(f"🧨 예외 발생: {str(e)}")
        print(traceback.format_exc())
        return send_file(create_debug_tile(f"타일 오류: {str(e)}"), mimetype='image/jpeg')





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