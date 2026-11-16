"""Run from the QGIS Python console editor. No additional Python packages are required."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import csv

# Allow long WKT fields using a limit compatible with Windows C long.
csv.field_size_limit(2_147_483_647)
import math
import re
import sqlite3
import traceback
import json
import hashlib

# Date range. All selected files in nested folders are processed in one run.
csv_folder = ""  # Leave blank to select the CSV root in QGIS.
start_date = "2026-10-01"
end_date = "2026-10-27"
gap_minutes = 60  # Split vessel paths when consecutive reports exceed this time gap.
min_inside_records = 2
min_inside_seconds = 60
min_outside_records = 3
exit_direction = "W"  # N, E, S, W. Applies to observed positions, not AIS Heading.
edge_margin_degrees = 0.01  # Extra latitude/longitude margin beyond the origin extent.
min_forward_metres = 5000  # Minimum movement from the final position inside the origin.
jump_knots = 80  # Split when speed inferred from consecutive positions exceeds this value.
origin_layer_name = 'esbjerg'  # Layer in the bundled example; change for another port.
target_layer_name = ""  # Set a polygon layer name to require an outside point in a target area.
sea_layer_name = ""  # Set a marine polygon layer name to exclude land and inland waters.
min_forward_cross_ratio = 1.0  # Minimum forward extent relative to cross-direction extent.


def epoch(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def parse_time(value):
    """Parse Danish day/month/year timestamps and ISO timestamps.
    Naive timestamps are labelled as UTC without changing clock values.
    Verify the source time zone separately; no local-time conversion is performed.
    """
    value = value.strip()
    if len(value) >= 19 and value[2] == '/' and value[5] == '/':
        day, month, year = value[:10].split('/')
        value = f'{year}-{month}-{day}' + value[10:]
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def number(value, ceiling=None):
    try:
        x = float(value)
        return x if math.isfinite(x) and x >= 0 and (ceiling is None or x < ceiling) else None
    except (ValueError, TypeError):
        return None


def norm(value):
    return re.sub(r'[^a-z0-9]', '', value.lower().lstrip('\ufeff'))


def rows(path):
    """Stream records and match columns by their header names."""
    with open(path, encoding='utf-8-sig', newline='') as f:
        head = f.readline()
        delimiter = ';' if head.count(';') > head.count(',') else ','
        f.seek(0)
        reader = csv.reader(f, delimiter=delimiter)
        names = next(reader, [])
        fields = {norm(name): i for i, name in enumerate(names)}
        required = ('timestamp', 'typeofmobile', 'mmsi', 'latitude', 'longitude')
        absent = [x for x in required if x not in fields]
        if absent:
            raise ValueError(f'{path.name}: missing required columns: {absent}. Header: {names}')
        for row in reader:
            if row:
                yield row, fields


def get(row, fields, key):
    i = fields.get(key)
    return row[i].strip() if i is not None and i < len(row) else ''


def position(row, fields):
    if get(row, fields, 'typeofmobile').lower() not in ('class a', 'class b'):
        return None
    try:
        m = get(row, fields, 'mmsi')
        if not (m.isdigit() and len(m) == 9):
            return None
        x, y = float(get(row, fields, 'longitude')), float(get(row, fields, 'latitude'))
        if not (math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -90 <= y <= 90):
            return None
        return m, x, y
    except ValueError:
        return None


def files_for_dates(root, first, last):
    files = []
    for path in root.rglob('*'):
        if path.is_file() and path.suffix.lower() == '.csv':
            # Use dates in standard aisdk folder/file names for preliminary selection.
            dates = re.findall(r'aisdk[-_](\d{4}-\d{2}-\d{2})', str(path), flags=re.I)
            if not dates or any(first <= d <= last for d in dates):
                files.append(path)
    return sorted(files)


def distance_m(x1, y1, x2, y2):
    p1, p2 = math.radians(y1), math.radians(y2)
    a = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(x2-x1)/2)**2
    return 12742000 * math.asin(min(1, math.sqrt(a)))


def direction_gate(bbox):
    xmin, ymin, xmax, ymax = bbox
    limits = {"W": -xmin, "E": xmax, "S": -ymin, "N": ymax}
    return limits[exit_direction] + edge_margin_degrees


def movement_components(x, y, x0, y0):
    east = math.radians(x - x0) * 6371000 * math.cos(math.radians(y0))
    north = math.radians(y - y0) * 6371000
    if exit_direction in ("E", "W"):
        return (east if exit_direction == "E" else -east), abs(north)
    return (north if exit_direction == "N" else -north), abs(east)


def select_trips(db, contains, gate, log, cancel, target=None):
    """Identify candidate departures in chronological order.
    The broad origin mask does not establish a confirmed berth departure.
    """
    db.execute('create index if not exists vessel_time on points(mmsi,t)')
    db.execute('create table trips(trip_id integer primary key,mmsi text,t0 integer,t1 integer,reason text,exit_gate_seen integer)')
    current = None
    previous = None
    inside_start = None
    inside_count = 0
    last_inside = None
    active = None
    seq = 0
    counts = {'accepted': 0, 'rejected': 0, 'direction_rejected': 0, 'gap_breaks': 0, 'jump_breaks': 0}
    db.execute('create table direction_review(trip_id integer,mmsi text,forward_m real,cross_m real,accepted integer,exit_gate_seen integer,target_seen integer)')

    def finish(reason):
        nonlocal active
        if active is None:
            return
        tid, m, t0, t1, count, saw_gate, x0, y0, forward, cross, saw_target = active
        # Decide from the full observed outside segment, not just its initial course.
        direction_ok = forward >= min_forward_metres and forward >= min_forward_cross_ratio * cross
        accepted = count >= min_outside_records and saw_gate and direction_ok and saw_target
        db.execute('insert into direction_review values(?,?,?,?,?,?,?)', (tid,m,forward,cross,int(accepted),int(saw_gate),int(saw_target)))
        if accepted:
            db.execute('insert into trips values(?,?,?,?,?,?)', (tid,m,t0,t1,reason,1))
            db.execute('update points set trip_id=? where mmsi=? and t>=? and t<=?', (tid,m,t0,t1))
            counts['accepted'] += 1
        else:
            counts['rejected'] += 1
            if count >= min_outside_records and saw_gate and not direction_ok:
                counts['direction_rejected'] += 1
        active = None

    for i, (m,t,x,y) in enumerate(db.execute('select mmsi,t,lon,lat from points order by mmsi,t,lon,lat')):
        if i % 50000 == 0:
            if cancel():
                raise InterruptedError('Processing cancelled by the user.')
            log(f'Reviewing trips: {i:,} records')
        if m != current:
            finish('data_end')
            current, previous = m, None
            inside_start, inside_count, last_inside = None, 0, None
        if previous:
            dt = t - previous[0]
            # Different positions within the same second are ambiguous; use the first.
            if dt == 0:
                continue
            gap = dt > gap_minutes * 60
            jump = distance_m(previous[1], previous[2], x, y) / dt > jump_knots * 1852 / 3600
            if gap or jump:
                finish('time_gap' if gap else 'position_jump')
                counts['gap_breaks' if gap else 'jump_breaks'] += 1
                inside_start, inside_count, last_inside = None, 0, None
        # Actual polygon intersection is used, including the polygon boundary.
        inside = contains(x,y)
        if inside:
            finish('area_reentry')
            if inside_start is None:
                inside_start = t
            inside_count += 1
            last_inside = (t,x,y)
        else:
            if active is None and last_inside is not None:
                if inside_count >= min_inside_records and last_inside[0]-inside_start >= min_inside_seconds:
                    seq += 1
                    # Include the entire last continuous area visit, not only its final point.
                    active = [seq,m,inside_start,t,0,False,last_inside[1],last_inside[2],0.0,0.0,target is None]
            if active is not None:
                active[3] = t
                active[4] += 1
                position_on_axis = {"W": -x, "E": x, "S": -y, "N": y}[exit_direction]
                active[5] = active[5] or position_on_axis > gate
                forward, cross = movement_components(x, y, active[6], active[7])
                active[8] = max(active[8], forward)
                active[9] = max(active[9], cross)
                active[10] = active[10] or (target is not None and target(x, y))
            inside_start, inside_count, last_inside = None, 0, None
        previous = (t,x,y)
    finish('data_end')
    db.commit()
    return counts


def run_analysis(root, output, contains, bbox, log=lambda s: None, progress=lambda p: None, cancel=lambda: False, *, sea=None, target=None, geometry_keys=None):
    first, end = epoch(start_date), epoch(end_date) + 86400
    files = files_for_dates(root, start_date, end_date)
    if not files:
        raise ValueError('No CSV files matching the date range were found in the selected folder.')
    xmin,ymin,xmax,ymax = bbox
    vessels = set()
    stats = {'files': len(files), 'bad_time': 0, 'region_records': 0}
    log(f'CSV files found: {len(files)}. First pass: vessels observed in the origin area.')
    for fi,path in enumerate(files):
        log(f'Pass 1/2: {path.name}')
        for n,(r,h) in enumerate(rows(path)):
            if n % 100000 == 0 and cancel():
                raise InterruptedError('Processing cancelled by the user.')
            p = position(r,h)
            if p is None:
                continue
            m,x,y = p
            if xmin <= x <= xmax and ymin <= y <= ymax and contains(x,y) and (sea is None or sea(x, y)):
                try:
                    t = parse_time(get(r,h,'timestamp'))
                except ValueError:
                    stats['bad_time'] += 1
                    continue
                if first <= t < end:
                    vessels.add(m)
                    stats['region_records'] += 1
        progress(30*(fi+1)/len(files))
    if not vessels:
        raise ValueError('No Class A/B vessel records matched the origin area and date range. Check the polygon and CSV dates.')
    log(f'Distinct MMSIs observed in the area: {len(vessels)}. Second pass: all available positions of these vessels.')
    db = sqlite3.connect(str(output/'working.sqlite'))
    try:
        db.execute('pragma temp_store=FILE')
        db.execute('create table points(mmsi text,t integer,lon real,lat real,sog real,cog real,trip_id integer,unique(mmsi,t,lon,lat))')
        batch = []
        # Keep all period positions for candidate MMSIs, including outside the mask.
        for fi,path in enumerate(files):
            log(f'Pass 2/2: {path.name}')
            for n,(r,h) in enumerate(rows(path)):
                if n % 100000 == 0 and cancel():
                    raise InterruptedError('Processing cancelled by the user.')
                m = get(r,h,'mmsi')
                if m not in vessels:
                    continue
                p = position(r,h)
                if p is None:
                    continue
                try:
                    t = parse_time(get(r,h,'timestamp'))
                except ValueError:
                    stats['bad_time'] += 1
                    continue
                if first <= t < end and (sea is None or sea(p[1], p[2])):
                    batch.append((m,t,p[1],p[2],number(get(r,h,'sog'),102.3),number(get(r,h,'cog'),360),None))
                if len(batch) >= 20000:
                    db.executemany('insert or ignore into points values(?,?,?,?,?,?,?)', batch)
                    db.commit()
                    batch.clear()
            progress(30+40*(fi+1)/len(files))
        db.executemany('insert or ignore into points values(?,?,?,?,?,?,?)', batch)
        db.commit()
        stats['vessels_in_area'] = len(vessels)
        stats['staged_points'] = db.execute('select count(*) from points').fetchone()[0]
        return finalize_analysis(db,output,contains,bbox,stats,log,progress,cancel,target,geometry_keys)
    finally:
        db.close()


def finalize_analysis(db,output,contains,bbox,stats,log,progress,cancel,target,geometry_keys):
    gate = direction_gate(bbox)
    stats.update(select_trips(db,contains,gate,log,cancel,target))
    db.execute('create index if not exists selected_trip on points(trip_id,t)')
    progress(90)
    export_results(db,output,cancel)
    with open(output/'direction_review.csv','w',encoding='utf-8',newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['trip_id','mmsi','forward_m','cross_m','accepted','exit_gate_seen','target_seen'])
        writer.writerows(db.execute('select * from direction_review order by trip_id'))
    gate_coordinate = -gate if exit_direction in ("W", "S") else gate
    stats.update({
        'version': 4, 'start': start_date, 'end': end_date, 'bbox': bbox,
        'geometry_keys': geometry_keys, 'exit_direction': exit_direction,
        'exit_gate_coordinate_degrees': gate_coordinate,
        'target_enabled': target is not None, 'sea_mask_enabled': bool(geometry_keys and geometry_keys['sea']),
        'note': 'Candidate departures include the last continuous origin visit. No anomaly score is calculated.',
        'parameters': {'gap_minutes': gap_minutes, 'jump_knots': jump_knots,
                       'min_inside_records': min_inside_records, 'min_inside_seconds': min_inside_seconds,
                       'min_outside_records': min_outside_records, 'min_forward_metres': min_forward_metres,
                       'edge_margin_degrees': edge_margin_degrees, 'min_forward_cross_ratio': min_forward_cross_ratio},
    })
    (output/'summary.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
    progress(100)
    return stats


def reprocess(cache,output,contains,bbox,log,progress,cancel,target,geometry_keys):
    metadata_path = cache.parent/'summary.json'
    meta = json.loads(metadata_path.read_text(encoding='utf-8'))
    if meta.get('start') != start_date or meta.get('end') != end_date:
        raise ValueError('The cached date range differs. Scan the raw files again.')
    previous_keys = meta.get('geometry_keys') or {}
    if any(previous_keys.get(key) != geometry_keys[key] for key in ('origin', 'sea')):
        raise ValueError('The cache does not match the origin and sea polygons. Scan the raw files again.')
    log('Reusing staged records from a previous scan; raw CSV files will not be read.')
    source = sqlite3.connect(cache.resolve().as_uri()+'?mode=ro',uri=True)
    db = sqlite3.connect(str(output/'working.sqlite'))
    try:
        def backup_progress(status,remaining,total):
            if cancel():
                raise InterruptedError('Processing cancelled by the user.')
            progress(20*(total-remaining)/max(total,1))
        source.backup(db,pages=2048,progress=backup_progress)
        db.execute('update points set trip_id=null')
        db.execute('drop table if exists trips')
        db.execute('drop table if exists direction_review')
        db.commit()
        meta['reused_cache'] = str(cache)
        return finalize_analysis(db,output,contains,bbox,meta,log,progress,cancel,target,geometry_keys)
    finally:
        source.close()
        db.close()


def export_results(db,output,cancel):
    with open(output/'trip_points.csv','w',encoding='utf-8',newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['trip_id','mmsi','time','lon','lat','sog','cog'])
        previous_key = None
        for i,(tid,m,t,x,y,s,c) in enumerate(db.execute('select trip_id,mmsi,t,lon,lat,sog,cog from points where trip_id is not null order by trip_id,t,lon,lat')):
            if i % 50000 == 0 and cancel():
                raise InterruptedError('Processing cancelled by the user.')
            key = (tid, t)
            if key != previous_key:
                writer.writerow([tid,m,stamp(t),x,y,s,c])
                previous_key = key
    with open(output/'trip_paths.csv','w',encoding='utf-8',newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['trip_id','mmsi','start_time','end_time','end_reason','point_count','wkt'])
        for tid,m,t0,t1,reason,_ in db.execute('select * from trips order by trip_id'):
            if cancel():
                raise InterruptedError('Processing cancelled by the user.')
            coords = []
            previous_t = None
            for t,x,y in db.execute('select t,lon,lat from points where trip_id=? order by t,lon,lat',(tid,)):
                if t != previous_t:
                    coords.append(f'{x:.8f} {y:.8f}')
                    previous_t = t
            if len(coords) >= 2:
                writer.writerow([tid,m,stamp(t0),stamp(t1),reason,len(coords),'LINESTRING ('+', '.join(coords)+')'])


def trips_by_day(csv_path):
    """Group each trip by its first included record; do not split at midnight."""
    # Apply the field limit to generated WKT as well as raw AIS records.
    csv.field_size_limit(2_147_483_647)
    grouped = {}
    csv_path = Path(csv_path)
    cache = csv_path.parent/'working.sqlite'
    if not cache.is_file():
        cache = csv_path.parent/'calisma.sqlite'  # Earlier output folders.
    if cache.is_file():
        try:
            with sqlite3.connect(cache.resolve().as_uri()+'?mode=ro',uri=True) as db:
                records = db.execute('select trip_id,t0 from trips order by trip_id').fetchall()
            for tid,t0 in records:
                grouped.setdefault(stamp(t0)[:10],[]).append(int(tid))
            return grouped
        except sqlite3.Error:
            pass  # Fall back to CSV if the cache or its table cannot be read.
    with open(csv_path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            grouped.setdefault(row['start_time'][:10], []).append(int(row['trip_id']))
    return grouped


def load_results(output):
    from qgis.core import QgsVectorLayer,QgsProject,QgsCategorizedSymbolRenderer,QgsRendererCategory,QgsMarkerSymbol,QgsLineSymbol
    from qgis.PyQt.QtCore import QUrl,QUrlQuery
    from qgis.PyQt.QtGui import QColor
    from qgis.PyQt.QtWidgets import QMessageBox
    from qgis.utils import iface
    loaded = 0
    path_file = 'trip_paths.csv' if (output/'trip_paths.csv').is_file() else 'sefer_cizgileri.csv'
    point_file = 'trip_points.csv' if (output/'trip_points.csv').is_file() else 'sefer_noktalari.csv'
    grouped = trips_by_day(output/path_file)
    for day, trip_ids in sorted(grouped.items()):
        label = datetime.fromisoformat(day).strftime('%d-%m')
        subset = '"trip_id" IN (' + ','.join(map(str,trip_ids)) + ')'
        for filename,prefix,params in [
            (path_file,'path_',{'wktField':'wkt','geomType':'line'}),
            (point_file,'ais_',{'xField':'lon','yField':'lat','geomType':'point'})]:
            url = QUrl.fromLocalFile(str(output/filename))
            query = QUrlQuery()
            for k,v in dict(type='csv',delimiter=',',useHeader='yes',detectTypes='yes',crs='EPSG:4326',**params).items():
                query.addQueryItem(k,v)
            url.setQuery(query)
            result = QgsVectorLayer(url.toString(),prefix+label,'delimitedtext')
            if not result.isValid() or not result.setSubsetString(subset):
                QMessageBox.warning(iface.mainWindow(),'Unable to load layer',str(output/filename))
                continue
            categories = []
            for tid in trip_ids:
                color = QColor.fromHsv((tid*137)%360,175,210)
                if prefix == 'path_':
                    symbol = QgsLineSymbol.createSimple({'line_color':color.name(),'line_width':'0.45'})
                else:
                    symbol = QgsMarkerSymbol.createSimple({'name':'circle','color':color.name(),'size':'1.5','outline_style':'no'})
                categories.append(QgsRendererCategory(tid,symbol,'Trip '+str(tid)))
            result.setRenderer(QgsCategorizedSymbolRenderer('trip_id',categories))
            result.setCustomProperty('ais_path_filter/start_day',day)
            QgsProject.instance().addMapLayer(result)
            loaded += 1
    return loaded


def launch():
    from qgis.core import QgsApplication,QgsTask,QgsVectorLayer,QgsGeometry,QgsCoordinateReferenceSystem,QgsCoordinateTransform,QgsProject
    from qgis.PyQt.QtWidgets import QFileDialog,QMessageBox
    from qgis.utils import iface
    from qgis.core import QgsWkbTypes

    if exit_direction not in ("N", "E", "S", "W"):
        raise ValueError('Set exit_direction to N, E, S or W.')
    if len(start_date) != 10 or len(end_date) != 10 or epoch(start_date) > epoch(end_date):
        raise ValueError('start_date must be at or before end_date.')

    def select_polygon(label, layer_name):
        filename, _ = QFileDialog.getOpenFileName(
            iface.mainWindow(), f'Select the {label} GeoPackage', '', 'GeoPackage (*.gpkg)')
        if not filename:
            return None
        layer = QgsVectorLayer(filename + '|layername=' + layer_name, label, 'ogr')
        if not layer.isValid() or layer.geometryType() != QgsWkbTypes.PolygonGeometry or not layer.crs().isValid():
            raise ValueError(f'Expected a polygon layer with a valid CRS named {layer_name!r} in {filename}.')
        geometries = [feature.geometry() for feature in layer.getFeatures() if feature.hasGeometry()]
        geom = QgsGeometry.unaryUnion([g if g.isGeosValid() else g.makeValid() for g in geometries])
        crs = QgsCoordinateReferenceSystem('EPSG:4326')
        if layer.crs() != crs:
            geom.transform(QgsCoordinateTransform(layer.crs(), crs, QgsProject.instance()))
        if geom.isEmpty() or not geom.isGeosValid():
            raise ValueError(f'The {label} polygon is empty or invalid.')
        box = geom.boundingBox()
        bounds = (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
        return bytes(geom.asWkb()), bounds

    polygons = {}
    for label, layer_name in (('origin', origin_layer_name), ('sea', sea_layer_name), ('target', target_layer_name)):
        polygons[label] = select_polygon(label, layer_name) if layer_name else None
        if layer_name and polygons[label] is None:
            return
    if polygons['origin'] is None:
        raise ValueError('Set origin_layer_name to the origin polygon layer.')
    bbox = polygons['origin'][1]
    geometry_keys = {label: hashlib.sha256(data[0]).hexdigest() if data else None
                     for label, data in polygons.items()}
    root = Path(csv_folder)
    cache = None
    answer = QMessageBox.question(iface.mainWindow(),'Reuse previous staged data',
        'Do you have a working.sqlite cache from this version for the same dates, origin and sea mask?\n'
        'Choose Yes to reuse staged records without rereading raw CSV files. Select the previous output database.',
        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
    if answer == QMessageBox.Yes:
        chosen,_ = QFileDialog.getOpenFileName(iface.mainWindow(),'Select the database in the previous output folder',str(root.parent),'SQLite (*.sqlite)')
        if not chosen:
            return
        cache = Path(chosen)
    elif not csv_folder or not root.is_dir():
        chosen = QFileDialog.getExistingDirectory(iface.mainWindow(), 'Select the parent CSV folder')
        if not chosen:
            return
        root = Path(chosen)
    outroot = Path(QFileDialog.getExistingDirectory(iface.mainWindow(),'Select the output parent folder',str(root.parent)) or '')
    if str(outroot) == '.':
        return
    output = outroot / ('ais_'+start_date+'_'+end_date+'_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    output.mkdir(parents=True,exist_ok=False)

    class AisTask(QgsTask):
        def __init__(self):
            super().__init__('AIS trip selection',QgsTask.CanCancel)
            self.error = None
            self.stats = None

        def run(self):
            try:
                # Use a private geometry in the worker thread; do not access project layers.
                from qgis.core import QgsPointXY

                def polygon_check(data):
                    if data is None:
                        return None
                    wkb, bounds = data
                    geometry = QgsGeometry()
                    geometry.fromWkb(wkb)
                    engine = QgsGeometry.createGeometryEngine(geometry.constGet())
                    engine.prepareGeometry()

                    def intersects(x, y):
                        if not (bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]):
                            return False
                        point = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                        return engine.intersects(point.constGet())

                    # Keep the geometry alive for the lifetime of its engine.
                    return geometry, intersects

                origin = polygon_check(polygons['origin'])
                sea = polygon_check(polygons['sea'])
                target = polygon_check(polygons['target'])
                contains = origin[1]
                sea_check = sea[1] if sea else None
                target_check = target[1] if target else None
                with open(output/'processing_log.txt','w',encoding='utf-8',buffering=1) as logfile:
                    def log(message):
                        logfile.write(datetime.now().strftime('%H:%M:%S')+' '+message+'\n')
                    if cache is not None:
                        self.stats = reprocess(cache,output,contains,bbox,log,self.setProgress,self.isCanceled,target_check,geometry_keys)
                    else:
                        self.stats = run_analysis(root,output,contains,bbox,log,self.setProgress,self.isCanceled,sea=sea_check,target=target_check,geometry_keys=geometry_keys)
                return True
            except Exception:
                self.error = traceback.format_exc()
                (output/'error.txt').write_text(self.error,encoding='utf-8')
                return False

        def finished(self,ok):
            if not ok:
                QMessageBox.warning(iface.mainWindow(),'AIS processing could not be completed',f'Details: {output / "error.txt"}\n\n{self.error or "Cancelled"}')
                return
            if self.stats['accepted']:
                try:
                    load_results(output)
                except Exception:
                    detail = traceback.format_exc()
                    (output/'layer_loading_error.txt').write_text(detail,encoding='utf-8')
                    QMessageBox.warning(iface.mainWindow(),'Processing finished, but layers could not be loaded',
                        f'Results preserved: {output}\nThe CSV outputs can be added through QGIS as delimited text layers.\n\n{detail}')
                    return
            QMessageBox.information(iface.mainWindow(),'AIS processing completed',f'Distinct MMSIs observed in the area: {self.stats["vessels_in_area"]}\nSelected candidate trips: {self.stats["accepted"]}\nResults: {output}\n\nThis is preliminary trip selection; no anomaly score is calculated.')

    global ais_filter_task
    ais_filter_task = AisTask()
    QgsApplication.taskManager().addTask(ais_filter_task)
    iface.messageBar().pushInfo('AIS','Processing started. Check the task manager and processing_log.txt in the output folder.')


if __name__ in ('__main__','__console__'):
    launch()
