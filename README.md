# PulseCare - Smart IoT Heart Monitoring & Healthcare Dashboard

PulseCare is a web dashboard that receives live vital signs from a wearable IoT sensor, classifies each
patient's condition automatically, raises alerts to the on-duty care team, and helps staff react quickly
(emergency panel, nearest-hospital map, health analysis).

It is a pure front-end project: HTML + CSS + JavaScript, with Firebase Realtime Database as the cloud
data store. No build step is needed.

---

## 1. Features at a glance

| Area | What it does |
|---|---|
| **Sign in & roles** | Administrator, Nurse and Hospital accounts with different permissions |
| **Overview** | Clickable KPI cards, ward status bar, "Needs action now" queue, patient cards |
| **Patient focus** | Opened from the Overview cards (Streaming, Critical, Needs attention...). Cards or table view, search and sorting |
| **Live monitor** | Real-time HR, temperature and SpO2, ECG-style pulse, trend chart, statistics, risk score, recommended actions |
| **Health analysis** | Health score, time in each zone, HR distribution, trends, automatic insights, patient ranking, CSV export |
| **Alerts** | Automatic alerts with timeline (Detected, Acknowledged, Resolved), response time, 7-day chart |
| **Care team** | Staff cards, shift coverage, workload per person, on-duty switch |
| **Patients** | Profiles with record number (MRN), baseline condition, emergency contact, assigned staff |
| **Emergency map** | Nearest hospitals and emergency checklist for critical patients |
| **Dynamic rules** | Admins edit every threshold from the UI. Changes apply to all dashboards instantly |
| **Demo mode** | Simulated patients and scenarios, so you can present without hardware |

---

## 2. How it works

```
 Wearable sensor ──► Firebase Realtime DB ──► Dashboard (browser)
 (HR, temp, SpO2,     iot/current              │
  is_wearing)                                  ├─ normalises readings
                                               ├─ classifies each patient (rules engine)
                                               ├─ detects signal loss and its likely cause
                                               ├─ creates alerts and notifies on-duty staff
                                               └─ renders views, charts and the map
```

1. The sensor writes a reading to `iot/current` (or `live/{patientId}` for several wearables).
2. The dashboard listens in real time, links the reading to a patient (`iot/linked_patient`) and keeps a
   history of recent readings in memory.
3. Every second each patient is evaluated by the classification engine.
4. When a patient becomes **Needs attention** or **Critical**, an alert is created once (no duplicates)
   and the on-duty staff are recorded as notified. Critical alerts also play an alarm sound.

---

## 3. Classification engine

Each reading is checked against the **rule thresholds** (editable in Settings):

| Vital | Attention (defaults) | Critical (defaults) |
|---|---|---|
| Heart rate (BPM) | above 100 or below 50 | 130 or higher, 40 or lower |
| Body temperature (°C) | above 37.8 or below 35.5 | 39 or higher, 34 or lower |
| SpO2 (%) | below 95 | 90 or lower |

* **Severity per vital** (0 to 100) grows the further a value is outside its normal range.
* **Risk score** = 60% of the worst vital + 40% of the weighted average (HR 45%, SpO2 35%, temperature 20%).
* **Rapid change**: a heart-rate rise of 30+ BPM inside a 30 second window adds risk and triggers attention.
* **Baseline condition** (Normal / Needs Attention / Critical in the patient profile) slightly raises the score.
* **Age adjustment** (optional): high heart-rate limits are raised by 20 BPM under 12 years and 10 BPM under 18.
* **Result**: *Critical* if any vital reaches its critical limit or the score is 75+, *Needs attention* if any
  vital is out of range, the score is 20+, or a rapid rise is detected, otherwise *Normal*.

### Signal-loss analysis
When a wearable stops sending data the dashboard looks at what happened just before:

| Situation | Verdict |
|---|---|
| HR rose sharply, then the signal dropped | **Critical** - emergency, treat as a critical case |
| Vitals were already critical | **Critical** - emergency |
| Vitals were normal and stable | **Attention** - sensor probably not secure (slipped or loose) |
| Vitals were abnormal | **Attention** - signal lost while abnormal |

---

## 4. Roles and permissions

| Capability | Administrator | Nurse | Hospital |
|---|:-:|:-:|:-:|
| Overview, live monitor, health analysis, alerts, map | Yes | Yes | Yes |
| View patients | Yes | Yes | Yes |
| Add / edit patients, link IoT device | Yes | Yes | - |
| Delete patients | Yes | - | - |
| View care team | Yes | Yes | - |
| Add / edit / delete staff | Yes | - | - |
| Acknowledge alerts | Yes | Yes | Yes |
| Resolve alerts | Yes | Yes | - |
| Export vitals / analysis (CSV) | Yes | Yes | - |
| Edit rule thresholds | Yes | - | - |
| Data source and database tools | Yes | - | - |
| Manage user accounts | Yes | - | - |

**Demo accounts**

| Role | Username | Password |
|---|---|---|
| Administrator | `admin` | `admin123` |
| Nurse | `nurse` | `nurse123` |
| Hospital | `hospital` | `hospital123` |

Admins manage accounts in **Settings & data → Access & users**. Passwords are stored hashed in the browser.

> **Security note:** sign-in and permissions are enforced in the browser. This is suitable for a demo or
> graduation project. For production, add Firebase Authentication and Firebase Security Rules.

---

## 5. Dynamic settings (Rules & thresholds)

Open **Settings & data → Rules & thresholds** (administrators only).

* Edit heart rate, temperature, SpO2, rapid-change window/rise, signal timeout and look-back window.
* Each field shows its default and when and by whom it was last changed.
* A live preview bar shows the Critical / Attention / Normal zones while you type.
* Invalid combinations are blocked (for example, critical high must be above attention high).
* **Save changes** stores the rules in Firebase (`config/rules`), so every open dashboard updates, and all
  patients are re-evaluated immediately.
* Every change is recorded in the **Change history** (`config/rules_log`).

---

## 6. Project structure

```
pulsecare/
├── index.html   Page layout: sign-in, sidebar, topbar, all views
├── styles.css   Design system (dark and light themes), responsive layout
├── app.js       All logic: Firebase/demo backend, rules, roles, classification, alerts, views, charts, map
└── README.md    This file
```

External libraries (loaded from CDNs): Chart.js, Leaflet (map), Firebase compat SDK, Google Fonts.

---

## 7. Running the project

1. Unzip the folder.
2. Open a terminal inside the `pulsecare` folder and run **one** of:
   * Python: `python -m http.server 8000`
   * Node: `npx serve .`
   * VS Code: install **Live Server**, right-click `index.html` → *Open with Live Server*
3. Open `http://localhost:8000` in Chrome or Edge and sign in.

An internet connection is required for the CDN libraries. A local server is recommended because the
location and map features need `http://localhost`.

### Quick preview without hardware
Sign in as `admin` → **Settings & data → General → Demo (simulated)**. Six sample patients stream
simulated vitals. In **Data & tools → Scenario simulator** trigger: Fever, Tachycardia, Low SpO2,
Sensor slips off, or BPM spike then signal lost, and watch the alerts react.

### Using real data (Firebase)
1. Create a Firebase project with a **Realtime Database**.
2. Put your Firebase config in the `firebaseConfig` object at the top of `app.js`.
3. Allow reads and writes for your paths (at least for development), including `config/`, so rule changes can be saved and shared.
4. Switch to **Live (Firebase)** in Settings, then use **Add sample patients & staff** if you want starter records.
5. Add or select a patient and click **Link IoT device to this patient**.

---

## 8. Database structure

```
patients/{patientId}          mrn, name, age, gender, room, phone, contactName, contactPhone,
                              condition, notes, assignedStaff, deviceId, createdAt
staff/{staffId}               name, role, department, phone, email, shift, onDuty, createdAt
vitals/{patientId}/{pushId}   bpm, temperature, spo2, is_wearing, timestamp
alerts/{alertId}              patientId, patientName, level, type, title, message, vitals,
                              status, notified, createdAt, ackBy, ackAt, resolvedBy, resolvedAt
config/rules                  values{...thresholds}, meta{key: {ts, by}}
config/rules_log/{id}         key, label, from, to, by, ts
iot/current                   heart_rate, temperature, spo2, is_wearing, timestamp   (written by the sensor)
iot/linked_patient            patientId of the person wearing the sensor
live/{patientId}              same payload as iot/current (optional, one node per wearable)
```

### Connecting your sensor
The device only needs to write JSON like this to `iot/current`:

```json
{ "heart_rate": 78, "temperature": 36.7, "spo2": 98, "is_wearing": true, "timestamp": 1760000000000 }
```

Missing or zero values are treated as "no reading", and `is_wearing: false` marks the sensor as not worn.

---

## 9. Health analysis explained

The **Health analysis** tab works on the last 30 / 100 / 300 stored readings:

* **Health score (0 to 100)** = 100 minus the average risk score of the period.
  85+ Excellent, 65 to 84 Fair, below 65 Poor.
* **Time in each zone** - share of readings that were Normal, Attention or Critical.
* **Rapid-change events** - number of separate heart-rate surges.
* **Signal uptime** - share of readings where the sensor was worn.
* **Insights** - plain-language findings (time in the critical zone, rising or falling HR, unstable HR,
  low SpO2, poor sensor contact), sorted by importance.

Analysis uses readings kept in the browser session (up to 300 per patient), not the full database history.

---

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| Blank page or charts missing | Check your internet connection (CDN libraries) and run through a local server |
| "Database offline" chip | Check `firebaseConfig` and the database rules |
| Rule changes are not shared between screens | Allow writes on `config/` in Firebase rules |
| Map does not locate you | Use `http://localhost` and allow location in the browser |
| Forgot admin password | Clear `pc_users` from the browser's local storage; demo accounts are recreated |

---

## 11. Possible next steps

* Firebase Authentication and security rules for real access control
* Push or SMS notifications for critical alerts
* Long-term history storage and reports
* Multi-ward support and per-patient custom thresholds
