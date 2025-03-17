from flask import Flask, send_file, jsonify, request, send_from_directory, make_response
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

app = Flask(__name__, static_url_path='', static_folder=STATIC_FOLDER)
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
DATA_FOLDER = os.path.join(BASE_DIR, 'data')
STATIC_FOLDER = BASE_DIR
PUBLIC_FILES_PATH = os.path.join(BASE_DIR, 'public_files.json')

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
        # 먼저 static 폴더에서 찾기
        if os.path.exists(os.path.join(STATIC_FOLDER, filename)):
            return send_from_directory(STATIC_FOLDER, filename)
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
            print(f'File saved to: {filename}')
            return jsonify({'message': '파일이 업로드되었습니다', 'filename': file.filename})
        
        return jsonify({'error': 'SVS 파일만 업로드 가능합니다'}), 400
    except Exception as e:
        print(f'Upload error: {str(e)}')
        return jsonify({'error': str(e)}), 500

# 스레드 풀 크기와 캐시 설정 조정
executor = ThreadPoolExecutor(max_workers=8)  # 다시 8로 증가
slide_cache = TTLCache(maxsize=5, ttl=7200)  # TTL 2시간으로 증가
tile_cache = TTLCache(maxsize=1000, ttl=3600)  # 캐시 크기 증가

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

def create_tile(slide, level, x, y, tile_size, filename):
    try:
        cache_key = f"{filename}_{level}_{x}_{y}"
        
        if cache_key in tile_cache:
            return tile_cache[cache_key]
        
        # 타일 크기 최적화
        tile_size = min(tile_size, 1024)  # 타일 크기 줄임
        
        factor = slide.level_downsamples[level]
        x_pos = int(x * tile_size * factor)
        y_pos = int(y * tile_size * factor)
        
        # 메모리 사용량 최적화
        tile = slide.read_region((x_pos, y_pos), level, (tile_size, tile_size))
        tile = tile.convert('RGB')
        tile.load()
        
        # 이미지 압축 품질 조정
        tile_copy = tile.copy()
        tile.close()
        
        # 캐시에 저장
        tile_cache[cache_key] = tile_copy
        return tile_copy
        
    except Exception as e:
        print(f"Error creating tile: {str(e)}")
        return None

@app.route('/slide/<filename>/tile/<int:level>/<int:x>/<int:y>')
def get_tile(filename, level, x, y):
    try:
        slide_path = os.path.join(UPLOAD_FOLDER, filename)
        
        if slide_path not in slide_cache:
            slide = openslide.OpenSlide(slide_path)
            slide_cache[slide_path] = slide
        slide = slide_cache[slide_path]
        
        # 타일 크기 줄임
        tile = create_tile(slide, level, x, y, 1024, filename)
        if tile is None:
            return jsonify({'error': 'Failed to create tile'}), 500
        
        output = io.BytesIO()
        # JPEG 품질 더 낮춤
        tile.save(output, format='JPEG', quality=70, optimize=True)
        output.seek(0)
        
        response = make_response(send_file(
            output,
            mimetype='image/jpeg',
            as_attachment=False
        ))
        response.headers['Cache-Control'] = 'public, max-age=7200'  # 캐시 시간 증가
        return response
        
    except Exception as e:
        print(f"Error in get_tile: {str(e)}")
        return jsonify({'error': str(e)}), 500

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
        
        if slide_path not in slide_cache:
            slide_cache[slide_path] = openslide.OpenSlide(slide_path)
        slide = slide_cache[slide_path]
        
        tile_size = 2048
        
        info = {
            'dimensions': slide.dimensions,
            'level_count': slide.level_count,
            'level_dimensions': slide.level_dimensions,
            'level_downsamples': [float(ds) for ds in slide.level_downsamples],
            'tile_size': tile_size,
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
        
        info = {
            'dimensions': slide.dimensions,
            'level_count': slide.level_count,
            'level_dimensions': slide.level_dimensions,
            'level_downsamples': [float(ds) for ds in slide.level_downsamples],
            'tile_size': tile_size,
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
            cached_tile.save(output, format='JPEG', quality=90)
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
        tile.save(output, format='JPEG', quality=90)
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True, processes=1)