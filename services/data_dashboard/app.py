#!/usr/bin/env python3
"""
Data Dashboard v2 — 数据看板服务
=========================================================
Two-level navigation:
  L1: 数据中心 → L2: 总览/量表/饮食/运动/学生档案/统计分析/数据导出
  L1: 完成情况统计

Port: 8090
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
import json, csv, io
from datetime import datetime
import data as db
import ops

app = Flask(__name__)
CORS(app)

@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# =========================================================================
# PAGE ROUTES — Two-level nav
# =========================================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/scales')
def scales():
    return render_template('scales.html')

@app.route('/scales/<scale_name>')
def scale_detail(scale_name):
    return render_template('scales.html', active_scale=scale_name)

@app.route('/diet')
def diet():
    return render_template('diet.html')

@app.route('/exercise')
def exercise():
    return render_template('exercise.html')

@app.route('/students')
def students():
    return render_template('students.html')

@app.route('/student/<student_id>')
def student_detail(student_id):
    return render_template('student.html', student_id=student_id)

@app.route('/stats')
def stats():
    return render_template('stats.html')

@app.route('/export')
def export_center():
    return render_template('export.html')

@app.route('/completion')
def completion():
    rows = db.get_completion_table()
    summary = db.get_completion_summary()
    update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 饮食打卡日期标签由研究设计日历推导（不再硬编码 2026-07-06 起 24 天）
    diet_dates = db.DIET_DATE_LABELS or ['%d/%d' % (7, 6 + i) for i in range(24)]
    return render_template('completion.html', rows=rows, summary=summary, update_time=update_time, diet_dates=diet_dates, version='2.4')

# ── D：运行与质量（队列 / 依从性 / 数据质量 / 系统健康）─────────────────

@app.route('/queues')
def queues_page():
    return render_template('queues.html',
                           update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/adherence')
def adherence_page():
    days = request.args.get('days', 30, type=int)
    return render_template('adherence.html', days=days,
                           update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/quality')
def quality_page():
    return render_template('quality.html',
                           update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/system')
def system_page():
    return render_template('system.html',
                           update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

# =========================================================================
# API: DASHBOARD
# =========================================================================

@app.route('/api/dashboard/summary')
def api_dashboard_summary():
    try:
        return jsonify(db.get_dashboard_summary())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/activity')
def api_dashboard_activity():
    try:
        limit = request.args.get('limit', 10, type=int)
        return jsonify(db.get_recent_activity(limit))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/warnings')
def api_dashboard_warnings():
    try:
        return jsonify(db.get_warnings())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================================================================
# API: SCALES
# =========================================================================

@app.route('/api/scales/all')
def api_scales_all():
    try:
        return jsonify(db.get_all_scales_table())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scales/<scale_name>')
def api_scale_data(scale_name):
    if scale_name not in db.SCALE_DEFS:
        return jsonify({'error': 'scale not found'}), 404
    try:
        dat = db.get_scale_data(scale_name)
        sd = db.SCALE_DEFS[scale_name]
        dat['definition'] = {
            'name': sd['name'], 'short': sd['short'],
            'full_name': sd.get('full_name', sd['name']),
            'description': sd.get('description', ''),
            'range': sd.get('range', ''),
            'normal_label': sd.get('normal_label', ''),
            'borderline_label': sd.get('borderline_label', ''),
            'abnormal_label': sd.get('abnormal_label', ''),
            'max_val': sd.get('max_val', 100),
            'min_val': sd.get('min_val', 0),
        }
        return jsonify(dat)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scales/cross/<scale1>/<scale2>')
def api_cross_scale(scale1, scale2):
    if scale1 not in db.SCALE_DEFS or scale2 not in db.SCALE_DEFS:
        return jsonify({'error': 'scale not found'}), 404
    try:
        return jsonify(db.get_cross_scale_data(scale1, scale2))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================================================================
# API: DIET
# =========================================================================

@app.route('/api/diet/summary')
def api_diet_summary():
    try:
        return jsonify(db.get_diet_summary())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/diet/trend')
def api_diet_trend():
    try:
        return jsonify(db.get_diet_trend())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/diet/student_distribution')
def api_diet_student_dist():
    try:
        return jsonify(db.get_diet_student_distribution())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/diet/list')
def api_diet_list():
    try:
        student_id = request.args.get('student_id')
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        keyword = request.args.get('keyword')
        page = request.args.get('page', 1, type=int)
        return jsonify(db.get_diet_list(student_id, date_from, date_to, keyword, page))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================================================================
# API: EXERCISE (new)
# =========================================================================

@app.route('/api/exercise/summary')
def api_exercise_summary():
    try:
        return jsonify(db.get_exercise_summary())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/exercise/trend')
def api_exercise_trend():
    try:
        return jsonify(db.get_exercise_trend())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/exercise/student_distribution')
def api_exercise_student_dist():
    try:
        return jsonify(db.get_exercise_student_distribution())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/exercise/list')
def api_exercise_list():
    try:
        student_id = request.args.get('student_id')
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        keyword = request.args.get('keyword')
        page = request.args.get('page', 1, type=int)
        return jsonify(db.get_exercise_list(student_id, date_from, date_to, keyword, page))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================================================================
# API: STUDENTS
# =========================================================================

@app.route('/api/students/list')
def api_students_list():
    try:
        search = request.args.get('search')
        status = request.args.get('status')
        page = request.args.get('page', 1, type=int)
        return jsonify(db.get_students_list(search, status, page))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/<student_id>/overview')
def api_student_overview(student_id):
    try:
        dat = db.get_student_overview(student_id)
        if not dat:
            return jsonify({'error': 'student not found'}), 404
        return jsonify(dat)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/<student_id>/responses')
def api_student_responses(student_id):
    try:
        return jsonify(db.get_student_responses(student_id))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/<student_id>/diet')
def api_student_diet(student_id):
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        return jsonify(db.get_student_diet(student_id, date_from, date_to))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/<student_id>/exercise')
def api_student_exercise(student_id):
    try:
        return jsonify(db.get_exercise_list(student_id=student_id))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/<student_id>/raw')
def api_student_raw(student_id):
    try:
        return jsonify(db.get_student_raw(student_id))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/<student_id>/trends')
def api_student_trends(student_id):
    try:
        return jsonify(db.get_student_trends(student_id))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================================================================
# API: STATS
# =========================================================================

@app.route('/api/stats/overview')
def api_stats_overview():
    try:
        dat = db.get_stats_overview()
        return jsonify({'scales': dat})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats/correlation')
def api_correlation():
    try:
        return jsonify(db.get_correlation_matrix())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================================================================
# API: COMPLETION STATUS (new)
# =========================================================================

@app.route('/api/completion/table')
def api_completion_table():
    try:
        return jsonify(db.get_completion_table())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/completion/summary')
def api_completion_summary():
    try:
        return jsonify(db.get_completion_summary())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/completion/student/<student_id>')
def api_completion_student(student_id):
    try:
        scale = db.check_scale_completion(student_id)
        exercise = db.check_exercise_completion(student_id)
        diet = db.check_diet_completion(student_id)
        return jsonify({
            'student_id': student_id,
            'scale': scale,
            'exercise': exercise,
            'diet': diet,
            'total_score': round(scale['score'] + exercise['score'] + diet['score'], 1)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================================================================
# API: EXPORT
# =========================================================================

@app.route('/api/export/scales')
def api_export_scales():
    try:
        scale_name = request.args.get('scale')
        student_id = request.args.get('student_id')
        fmt = request.args.get('format', 'csv')
        rows = db.get_export_scales(scale_name, student_id)
        if fmt == 'json':
            return Response(json.dumps(rows, ensure_ascii=False, default=str),
                          mimetype='application/json',
                          headers={'Content-Disposition': 'attachment;filename=scale_scores.json'})
        output = io.StringIO()
        writer = csv.writer(output)
        if rows:
            writer.writerow(rows[0].keys())
            for r in rows:
                writer.writerow([str(v) if v is not None else '' for v in r.values()])
        return Response(output.getvalue(), mimetype='text/csv; charset=utf-8',
                      headers={'Content-Disposition': 'attachment;filename=scale_scores.csv'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export/responses')
def api_export_responses():
    try:
        student_id = request.args.get('student_id')
        fmt = request.args.get('format', 'csv')
        rows = db.get_export_responses(student_id)
        if fmt == 'json':
            return Response(json.dumps(rows, ensure_ascii=False, default=str),
                          mimetype='application/json',
                          headers={'Content-Disposition': 'attachment;filename=survey_responses.json'})
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['学生ID', '姓名', '提交时间', '题目', '答案'])
        for r in rows:
            for q, a in r.get('responses', {}).items():
                writer.writerow([r['student_id'], r['name'], r['submitted_at'], q, a])
        return Response(output.getvalue(), mimetype='text/csv; charset=utf-8',
                      headers={'Content-Disposition': 'attachment;filename=survey_responses.csv'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export/diet')
def api_export_diet():
    try:
        student_id = request.args.get('student_id')
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        fmt = request.args.get('format', 'csv')
        rows = db.get_export_diet(student_id, date_from, date_to)
        if fmt == 'json':
            return Response(json.dumps(rows, ensure_ascii=False, default=str),
                          mimetype='application/json',
                          headers={'Content-Disposition': 'attachment;filename=diet_records.json'})
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['学生ID', '姓名', '提交时间', '记录日期', '饮食描述', '餐数', '营养建议'])
        for r in rows:
            writer.writerow([r['student_id'], r['name'], r['submitted_at'], r['record_date'],
                           r['diet_description'], r['meal_count'], r['dietary_advice']])
        return Response(output.getvalue(), mimetype='text/csv; charset=utf-8',
                      headers={'Content-Disposition': 'attachment;filename=diet_records.csv'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export/exercise')
def api_export_exercise():
    try:
        student_id = request.args.get('student_id')
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        fmt = request.args.get('format', 'csv')
        rows = db.get_export_exercise(student_id, date_from, date_to)
        if fmt == 'json':
            return Response(json.dumps(rows, ensure_ascii=False, default=str),
                          mimetype='application/json',
                          headers={'Content-Disposition': 'attachment;filename=exercise_records.json'})
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['学生ID', '姓名', '提交时间', '步行天数', '步行分钟数', '久坐小时', '运动详情'])
        for r in rows:
            writer.writerow([r['student_id'], r['name'], r['submitted_at'],
                           r['walk_days'], r['walk_minutes'], r['sedentary_hours'], r['exercises']])
        return Response(output.getvalue(), mimetype='text/csv; charset=utf-8',
                      headers={'Content-Disposition': 'attachment;filename=exercise_records.csv'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export/combined')
def api_export_combined():
    try:
        scales = db.get_export_scales()
        diet_rows = db.get_export_diet()
        exercise_rows = db.get_export_exercise()
        combined = {'scales': scales, 'diet': diet_rows, 'exercise': exercise_rows}
        return Response(json.dumps(combined, ensure_ascii=False, default=str),
                      mimetype='application/json',
                      headers={'Content-Disposition': 'attachment;filename=combined_export.json'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================================================================
# API: RUN & QUALITY (D)
# =========================================================================

def _csv_response(rows, filename):
    """把 [{...}] 导出为 CSV（UTF-8 BOM，Excel 友好）。"""
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return Response('\ufeff' + output.getvalue(),
                    mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment;filename={filename}'})


def _rows_response(rows, filename, fmt):
    if fmt == 'json':
        return Response(json.dumps(rows, ensure_ascii=False, default=str),
                        mimetype='application/json',
                        headers={'Content-Disposition': f'attachment;filename={filename.replace(".csv", ".json")}'})
    return _csv_response(rows, filename)


@app.route('/api/queues')
def api_queues():
    try:
        return jsonify(ops.queue_overview())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/adherence')
def api_adherence():
    try:
        days = request.args.get('days', 30, type=int)
        end = request.args.get('end')
        return jsonify(ops.adherence_series(days=days, end=end))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/quality')
def api_quality():
    try:
        return jsonify(ops.data_quality())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/system/health')
def api_system_health():
    try:
        return jsonify(ops.system_health())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/queues')
def api_export_queues():
    try:
        return _rows_response(ops.queue_rows(), 'queue_status.csv',
                              request.args.get('format', 'csv'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/adherence')
def api_export_adherence():
    try:
        days = request.args.get('days', 30, type=int)
        return _rows_response(ops.adherence_rows(ops.adherence_series(days=days)),
                              'adherence_matrix.csv', request.args.get('format', 'csv'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/quality')
def api_export_quality():
    try:
        return _rows_response(ops.quality_rows(), 'data_quality.csv',
                              request.args.get('format', 'csv'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/system')
def api_export_system():
    try:
        return _rows_response(ops.health_rows(), 'system_health.csv',
                              request.args.get('format', 'csv'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =========================================================================
# Health check
# =========================================================================

@app.route('/healthz')
def healthz():
    """统一健康探针：/healthz（含三个库的可读性检查）。"""
    checks, ok = {}, True
    for name, fn in (('survey', get_survey_conn), ('diet', get_diet_conn),
                     ('exercise', get_exercise_conn)):
        try:
            c = fn()
            c.execute('SELECT 1')
            c.close()
            checks[name + '_db'] = {'readable': True}
        except Exception as e:                    # noqa: BLE001
            checks[name + '_db'] = {'readable': False, 'error': str(e)}
            ok = False
    return jsonify({
        'status': 'ok' if ok else 'degraded',
        'service': 'data-dashboard',
        'version': '2.0',
        'pid': os.getpid(),
        'checks': checks,
        'time': datetime.now().isoformat(timespec='seconds'),
    }), (200 if ok else 503)


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '2.0'})

if __name__ == '__main__':
    # 端口来自 app.yaml（services.data_dashboard.port），缺省 8090
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT_DATA_DASHBOARD', '8090')),
            debug=False)
