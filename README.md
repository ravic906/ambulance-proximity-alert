# 🚑 Ambulance Proximity Alert

A prototype system that warns nearby drivers in real time when an ambulance is approaching on their route — before they can see or hear it.

## The problem

Ambulances still get stuck in traffic even with modern navigation apps, because nearby drivers have no way of knowing an ambulance is coming until it's right behind them, often too late to safely make way. This project explores whether real-time, route-aware alerts could close that gap.

## How it works

1. **Ambulance driver verifies** identity (name, license ID, live photo) before going live.
2. **Sets a destination** on the map — the app fetches a real road route (via OSRM).
3. **Broadcasts location** along that route as the ambulance drives — one-way only, the ambulance never sees who's around it.
4. **Nearby drivers' phones** independently check their own GPS against the ambulance's live route.
5. If a driver is within a **150m corridor of the road ahead** of the ambulance (not just "nearby" in any direction), their phone shows an alert and vibrates.

This route-aware check is the core idea: it only warns drivers actually in the ambulance's path, not everyone within a radius.

## Tech stack

- **Backend:** Python (Flask), in-memory state (no database — this is a prototype)
- **Routing:** [OSRM](http://project-osrm.org/) public demo server for real road routes
- **Frontend:** Vanilla HTML/JS + [Leaflet](https://leafletjs.com/) for maps
- **Proximity logic:** Haversine distance + point-to-segment geometry to check if a driver is on the road corridor ahead of the ambulance

## Running it

This is deployed on [Render](https://render.com), so it has a permanent HTTPS URL — no local setup needed to try it.

**To deploy your own copy:**
1. Push this repo to GitHub
2. Create a free Render account, click **New → Web Service**, and connect the repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python3 app.py`
5. Render gives you a URL like `https://your-app-name.onrender.com`

Open that URL:
- `https://your-app-name.onrender.com/ambulance` on one phone
- `https://your-app-name.onrender.com/driver` on another

Note: Render's free tier sleeps after inactivity and takes ~30 seconds to wake up on the first request — worth a heads up before a live demo.

**To run it locally instead** (e.g. for development):
```bash
pip install -r requirements.txt
python3 app.py
```
This starts the server on `localhost:5000`.

## What's real vs. simulated in this prototype

- ✅ Real GPS tracking, real road routing, real proximity math, real alerts
- ⚠️ Verification is a mock — there's no actual ID-checking behind it
- ⚠️ Routing uses OSRM's free public demo server, not production-grade infrastructure
- ⚠️ "Other traffic" shown on the driver's map is simulated to demonstrate what a live street would look like

## Why this matters

This came out of a personal experience — a pregnant relative who didn't make it to the hospital in time because the ambulance got stuck in traffic. Technology has moved forward since then, but the core problem — nearby drivers not knowing to make way — hasn't been solved. This prototype is a starting point for a conversation about whether mapping platforms (Google Maps, Waze) or ride-hailing/logistics companies (Uber, etc.) could build this at scale.

## Status

Early-stage prototype, built to validate the technical feasibility of route-aware proximity alerts. Not production-ready. Looking for feedback and potential collaboration from teams working on maps, navigation, or emergency response technology.
