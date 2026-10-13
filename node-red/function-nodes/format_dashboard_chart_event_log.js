const r = msg.payload || {};
const estimate = r.risk_estimate || {};
const noise = r.noise_guard || {};
const rows = flow.get('vitalsEventRows') || [];
rows.unshift({time:r.timestamp || '', device_id:r.device_id || '', sensor_status:r.sensor_status || '', review_state:r.review_state || '', trend:r.trend || '', estimate:estimate.label || 'Unavailable', score:estimate.high_risk_score == null ? '' : estimate.high_risk_score, noise_guard:noise.status || '', reason:r.reason || ''});
if (rows.length > 20) rows.length = 20;
flow.set('vitalsEventRows', rows);
const dashboard = RED.util.cloneMessage(msg);
dashboard.payload.risk_estimate_display = estimate.available
    ? estimate.label + ' | score: ' + estimate.high_risk_score + ' | ' + estimate.status
    : 'Unavailable | ' + (estimate.reason || 'No valid sample');
dashboard.payload.noise_display = (noise.status || 'not reported') + ' — ' + (noise.method || 'adaptive screen');
const raw = (r.input && r.input.raw) || {};
if (r.sensor_status === 'Online' || r.sensor_status === 'Partial data') {
    const show = (value, unit) => value == null ? 'Unavailable' : String(value) + unit;
    dashboard.payload.readings_display = 'BPM: ' + show(raw.bpm, '') + ' | SpO2: ' + show(raw.spo2_pct, '%') + ' | Temp: ' + show(raw.temp_c, ' °C');
} else if (r.sensor_status === 'Possible noise') {
    dashboard.payload.readings_display = 'Reading withheld as possible noise';
} else {
    dashboard.payload.readings_display = 'Unavailable';
}
let chart = RED.util.cloneMessage(msg);
const bpm = raw.bpm;
const chartableStatus = r.sensor_status === 'Online' || r.sensor_status === 'Partial data';
if (chartableStatus && noise.status !== 'single_spike_withheld' && bpm != null && Number.isFinite(Number(bpm)) && r.timestamp) {
    chart.topic = 'BPM';
    chart.payload = {ts: Date.parse(r.timestamp), bpm: Number(bpm)};
} else {
    chart = null;
}
const log = RED.util.cloneMessage(msg);
log.payload = JSON.stringify(r);
const table = RED.util.cloneMessage(msg);
table.payload = rows;
const telegram = RED.util.cloneMessage(msg);
return [dashboard, chart, log, table, telegram];
