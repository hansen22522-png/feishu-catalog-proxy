"""
飞书多维表格代理服务
作用：换取 tenant_access_token 并代理读取多维表格记录，避免 app_secret 暴露在前端
部署：Render / Railway / 阿里云函数计算 / 任意支持 Python 的平台
"""
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ========== 环境变量配置 ==========
FEISHU_APP_ID = os.environ.get('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.environ.get('FEISHU_APP_SECRET', '')
FEISHU_APP_TOKEN = os.environ.get('FEISHU_APP_TOKEN', '')  # 多维表格 app_token（URL 中 /base/ 后面那段）
FEISHU_TABLE_ID = os.environ.get('FEISHU_TABLE_ID', '')    # 数据表 table_id

# ========== Token 缓存 ==========
_token_cache = {'token': None, 'expire_at': 0}

def get_tenant_token():
    """获取并缓存 tenant_access_token"""
    now = time.time()
    if _token_cache['token'] and _token_cache['expire_at'] > now:
        return _token_cache['token']
    resp = requests.post(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET},
        timeout=10
    )
    data = resp.json()
    if data.get('code') != 0:
        raise Exception(f"获取token失败: {data.get('msg')}")
    token = data['tenant_access_token']
    expire = data.get('expire', 7200)
    _token_cache['token'] = token
    _token_cache['expire_at'] = now + expire - 60  # 提前60秒过期
    return token

# ========== 字段映射：飞书列名 → 内部字段名 ==========
FIELD_MAP = {
    '类别1': 'cat1',
    '类别2': 'cat2',
    '子件名称': 'name',
    '英文名': 'enName',
    '瓶身材质': 'material',
    '容量/规格': 'capacity',
    '印刷工艺': 'print',
    '成本': 'cost',
    '参考SKU': 'sku',
    '重量kg': 'weightKg',
    '备注': 'remark',
    '图片URL': 'img',
}
REVERSE_FIELD_MAP = {v: k for k, v in FIELD_MAP.items()}
# 超链接类型的列，写入时需要用 {"text": "", "link": ""} 格式
# 如果你的图片URL列改成了"文本"类型，把这里改成空列表 []
LINK_FIELDS = ['图片URL']

def normalize_record(record):
    """把飞书记录转成统一格式"""
    fields = record.get('fields', {})
    item = {'record_id': record.get('record_id', '')}
    for cn_name, en_name in FIELD_MAP.items():
        val = fields.get(cn_name, '')
        # 飞书单选/多选字段返回的是 [{"text": "xxx"}] 格式
        if isinstance(val, list) and val and isinstance(val[0], dict):
            val = val[0].get('text', val[0].get('name', ''))
        # 飞书超链接字段返回的是 {"text": "xxx", "link": "xxx"} 格式
        elif isinstance(val, dict):
            val = val.get('link', val.get('text', ''))
        item[en_name] = val
    # 确保成本是数字
    if item['cost'] == '' or item['cost'] is None:
        item['cost'] = 0
    else:
        try:
            item['cost'] = float(item['cost'])
        except (ValueError, TypeError):
            item['cost'] = 0
    return item

def to_feishu_fields(item):
    """把内部数据格式转成飞书写入格式"""
    fields = {}
    for en_name, cn_name in REVERSE_FIELD_MAP.items():
        if en_name in item and item[en_name] != '' and item[en_name] is not None:
            val = item[en_name]
            if cn_name in LINK_FIELDS and isinstance(val, str) and val.startswith('http'):
                fields[cn_name] = {"text": val, "link": val}
            else:
                fields[cn_name] = val
    return fields

# ========== API 路由 ==========
@app.route('/api/records')
def get_records():
    """读取多维表格全部记录"""
    if not all([FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID]):
        return jsonify({'error': '后端环境变量未配置完整', 'records': []}), 500
    try:
        token = get_tenant_token()
    except Exception as e:
        return jsonify({'error': str(e), 'records': []}), 500

    headers = {'Authorization': f'Bearer {token}'}
    all_records = []
    page_token = None

    while True:
        params = {'page_size': 100}
        if page_token:
            params['page_token'] = page_token
        try:
            resp = requests.get(
                f'https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records',
                headers=headers, params=params, timeout=15
            )
            data = resp.json()
        except requests.RequestException as e:
            return jsonify({'error': f'请求飞书API失败: {str(e)}', 'records': []}), 502

        if data.get('code') != 0:
            return jsonify({'error': data.get('msg', '未知错误'), 'code': data.get('code'), 'records': []}), 502

        items = data.get('data', {}).get('items', [])
        all_records.extend(items)

        if not data.get('data', {}).get('has_more'):
            break
        page_token = data['data'].get('page_token')

    normalized = [normalize_record(r) for r in all_records]
    return jsonify({'records': normalized, 'count': len(normalized)})

@app.route('/api/health')
def health():
    """健康检查"""
    return jsonify({
        'status': 'ok',
        'app_id_configured': bool(FEISHU_APP_ID),
        'app_secret_configured': bool(FEISHU_APP_SECRET),
        'app_token_configured': bool(FEISHU_APP_TOKEN),
        'table_id_configured': bool(FEISHU_TABLE_ID),
    })

@app.route('/')
def index():
    return jsonify({'message': '飞书多维表格代理服务运行中', 'endpoints': ['GET /api/records', 'POST /api/records', 'PUT /api/records/<id>', 'DELETE /api/records/<id>', '/api/health']})

# ========== 写入接口 ==========
@app.route('/api/records', methods=['POST'])
def create_record():
    """新增一条记录"""
    if not all([FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID]):
        return jsonify({'error': '后端环境变量未配置完整'}), 500
    body = request.get_json(silent=True) or {}
    fields = to_feishu_fields(body)
    if not fields:
        return jsonify({'error': '没有有效字段'}), 400
    try:
        token = get_tenant_token()
        resp = requests.post(
            f'https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'fields': fields},
            timeout=15
        )
        data = resp.json()
    except Exception as e:
        return jsonify({'error': str(e)}), 502
    if data.get('code') != 0:
        return jsonify({'error': data.get('msg', '新增失败'), 'code': data.get('code')}), 502
    return jsonify({'success': True, 'record': normalize_record(data['data']['record'])})

@app.route('/api/records/<record_id>', methods=['PUT'])
def update_record(record_id):
    """更新一条记录"""
    if not all([FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID]):
        return jsonify({'error': '后端环境变量未配置完整'}), 500
    body = request.get_json(silent=True) or {}
    fields = to_feishu_fields(body)
    if not fields:
        return jsonify({'error': '没有有效字段'}), 400
    try:
        token = get_tenant_token()
        resp = requests.put(
            f'https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'fields': fields},
            timeout=15
        )
        data = resp.json()
    except Exception as e:
        return jsonify({'error': str(e)}), 502
    if data.get('code') != 0:
        return jsonify({'error': data.get('msg', '更新失败'), 'code': data.get('code')}), 502
    return jsonify({'success': True, 'record': normalize_record(data['data']['record'])})

@app.route('/api/records/<record_id>', methods=['DELETE'])
def delete_record(record_id):
    """删除一条记录"""
    if not all([FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID]):
        return jsonify({'error': '后端环境变量未配置完整'}), 500
    try:
        token = get_tenant_token()
        resp = requests.delete(
            f'https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15
        )
        data = resp.json()
    except Exception as e:
        return jsonify({'error': str(e)}), 502
    if data.get('code') != 0:
        return jsonify({'error': data.get('msg', '删除失败'), 'code': data.get('code')}), 502
    return jsonify({'success': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
