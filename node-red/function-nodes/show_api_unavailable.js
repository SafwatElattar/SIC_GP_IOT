const errorText = (msg.error && msg.error.message) || 'Python API is unavailable';
msg.payload = { sensor_status: 'API error', review_state: 'Unavailable', reason: errorText, recommended_action: 'Start vitals_ai.py serve and confirm the API URL/port.', risk_estimate: { available: false, status: 'unavailable', reason: errorText } };
return msg;
