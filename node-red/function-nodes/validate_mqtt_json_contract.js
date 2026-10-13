let p = msg.payload;
if (typeof p === 'string') { try { p = JSON.parse(p); } catch (e) { node.error('MQTT payload is not valid JSON', msg); return null; } }
if (!p || typeof p !== 'object' || Array.isArray(p)) { node.error('MQTT payload must be a JSON object', msg); return null; }
if (!p.mode) p.mode = 'physical';
if (!p.timestamp || !p.device_id) { node.error('timestamp and device_id are required', msg); return null; }
// Keep the physical contract to the three device readings; the API marks partial data.
msg.payload = p;
return msg;
