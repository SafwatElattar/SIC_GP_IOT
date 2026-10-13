const kind = (msg.payload && msg.payload.case) || 'rapid';
const device = kind === 'stable' ? 'demo-stable-loss' : 'demo-rapid-loss';
const bpmValues = kind === 'stable' ? [79, 80, 81, 80, null] : [70, 78, 89, 103, null];
let i = 0;
function sendNext() {
    if (i >= bpmValues.length) { node.done(); return; }
    const bpm = bpmValues[i++];
    const p = { device_id: device, timestamp: new Date().toISOString(), mode: 'simulated' };
    if (bpm === null) { p.signal_status = 'no_signal'; p.sample_ok = false; }
    else { p.bpm = bpm; p.spo2_pct = 98.0; p.temp_c = 36.8; p.signal_quality = 0.9; p.contact_ok = true; }
    node.send({ payload: p, topic: 'demo/vitals' });
    setTimeout(sendNext, 650);
}
sendNext();
return null;
