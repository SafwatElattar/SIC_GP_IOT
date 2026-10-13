let p = msg.payload || {};
if (typeof p === 'string') { try { p = JSON.parse(p); } catch (e) { node.error('Demo payload is not valid JSON', msg); return null; } }
p.mode = 'simulated';
p.device_id = p.device_id || 'demo-pi';
p.timestamp = new Date().toISOString();
msg.payload = p;
return msg;
