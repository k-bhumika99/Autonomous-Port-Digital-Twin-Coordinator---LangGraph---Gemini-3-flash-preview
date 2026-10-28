# Autonomous Port Digital Twin Coordinator

A decision-support web app for a container port. It builds a **virtual copy (Digital Twin)** of the port,
lets you inject a disruption (crane breakdown, delayed ship, yard congestion, truck surge), and then **six
cooperating agents** analyse the problem, propose plans, test every plan in the simulation, validate the results
and recommend the best *feasible* plan.

> All data is synthetic and all results are **simulated, not measured at a real port**.

## What happens when you click "Run Scenario"

1. A "Generating Digital Port Twins..." screen appears with six 3D emoji agents
   (ship, crane, container yard, truck, planner, validator). Each one lights up when **its real agent** starts and
   gets a green tick when it finishes. (The screen is shown for at least ~4.5 s so it can be seen.)
2. Behind the scenes a **LangGraph** workflow runs the agents (see below).
3. When it finishes, the **Digital Twin page** opens and starts playing the recommended plan automatically.
   If no feasible plan exists, the Results page opens instead and explains why.

## Quick start (step by step)

You need **Python 3.11 or newer**.

```bash
# 1. open a terminal in this folder
cd autonomous-port-digital-twin

# 2. create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows:  .venv\Scripts\activate

# 3. install the libraries
pip install -r requirements.txt

# 4. create your private settings file
cp .env.example .env               # Windows:  copy .env.example .env
#    then open .env and set:
#      FLASK_SECRET_KEY=<a long random string>
#      GEMINI_API_KEY=<your key from https://aistudio.google.com/apikey>   (optional, see below)

# 5. (optional) create a demo account with the sample port
python -m database.seed            # demo@example.com / Demo@12345

# 6. start the app
python app.py
```

Open <http://127.0.0.1:5000>. Sign up (or use the demo account), then:

**Scenario Setup -> pick a sample scenario -> Run Scenario**

Generate a secret key with: `python -c "import secrets; print(secrets.token_hex(32))"`

### Do I need a Gemini key?
No. Without a key everything numeric still works (simulation, OR-Tools, validation, charts, CSV).
Only the plain-language *explanations* are switched off, and the pages say so clearly - the app never invents an
AI answer. Gemini is **never** used to produce numbers or to override a failed constraint check.

## Running the tests

```bash
python -m pytest -q
```

44 tests, none of which need a Gemini key. They cover signup/login, route protection, user data isolation,
port validation, berth/crane/yard/truck rules, crane breakdowns, candidate-plan generation, rejection of infeasible
or tampered plans, the LangGraph workflow, retry limits, failed agents, metric maths and result persistence.

## Pages

| Page | Route |
|---|---|
| Home / Sign up / Sign in | `/`, `/signup`, `/signin` |
| Dashboard | `/dashboard` |
| Port Input (ships, cranes, yard, trucks) | `/port-input` |
| Scenario Setup + Run Scenario | `/scenario` |
| Digital Twin (2D playback) | `/digital-twin` |
| Six agent pages | `/agents/ship`, `/crane`, `/yard`, `/truck`, `/planning`, `/validation` |
| Final results (+ CSV) | `/results/<id>`, `/results/<id>/csv` |
| History | `/history` |

## The six agents

| # | Agent | Emoji | Does |
|---|---|---|---|
| 1 | Ship Operations | ship | arrivals, waiting ships, berth conflicts, schedule suggestions |
| 2 | Crane & Equipment | crane | availability, breakdowns, workload, compatible reassignment |
| 3 | Container Yard | containers | occupancy, overflow risk, in/out flows |
| 4 | Truck & Logistics | truck | queues, waiting times, gate bottlenecks, collection timing |
| 5 | Planning & Optimization | brain | builds candidate plans with **OR-Tools CP-SAT**, simulates each one |
| 6 | Validation & Decision | check | checks hard constraints, recomputes metrics, scores plans, picks the best feasible one |

Workflow (`orchestration/graph.py`):

```
load + validate -> baseline simulation (Plan A, disruption applied)
   -> Ship | Crane | Yard | Truck agents  (parallel fan-out / fan-in)
   -> Planning agent (OR-Tools, simulates plans B, C, D, E ...)
   -> Validation agent --(no feasible plan and retries left)--> Planning agent (with feedback)
   -> Supervisor compiles final recommendation -> save to SQLite
```

A failing agent is retried, then recorded as *failed* without crashing the run. Re-planning stops after
`MAX_REPLAN_RETRIES`; if nothing is feasible the result says **"no feasible plan"** and no recommendation is made.

## How a plan is scored

```
J = w_ship * total_ship_wait_h + w_truck * total_truck_wait_h + w_completion * completion_h + w_changes * plan_changes
```

Lower is better. Default weights: ship 1.0, truck 0.5, completion 0.25, changes 0.5 (editable on the Scenario page).
Only plans that pass **every** hard-constraint check are compared. The result is described as "best among the plans
tested", never as globally optimal.

## Simulation assumptions (SimPy, minutes)

- Time is in minutes from simulation start; crane rates are containers/hour.
- A berth hosts one ship at a time and only ships compatible with it; a crane serves one ship at a time.
- Unloaded containers go to the yard; a ship waits (and the crane is "blocked") if the yard is full - the yard never exceeds capacity.
- Trucks arrive at their scheduled time, queue at the gates, load up to their capacity per trip and make round trips.
- The simulation is deterministic: same inputs give identical results (no random numbers).
- Full list of assumptions is stored with every result and shown on the Results page (Trust and Validation section).

## Project layout

```
app.py                  Flask app factory
config.py               settings from environment variables
database/               schema.sql, db helpers, seed.py
models/                 Pydantic models for the port and for workflow state
simulation/             SimPy digital twin, events, metrics
agents/                 the six agents + supervisor logic
orchestration/graph.py  LangGraph workflow
optimization/           OR-Tools scheduling
services/               Gemini wrapper, simulation runner, results
routes/                 Flask blueprints (one per area)
templates/ static/      Jinja2 pages, CSS, vanilla JS, Plotly, emoji images
data/                   sample port and sample scenarios
tests/                  pytest suite
```

## Security notes

Passwords are hashed (Werkzeug), CSRF protection on forms and API calls, parameterised SQL only, every record is
tied to a user id, secrets come from `.env` (never committed), HTTP security headers are set, and errors shown in the
browser never contain stack traces or keys.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | activate the virtual environment, then `pip install -r requirements.txt` |
| "AI explanations unavailable" banner | `GEMINI_API_KEY` missing/invalid or no internet. Everything else still works |
| Logged out after every restart | set a real `FLASK_SECRET_KEY` in `.env` |
| Port 5000 already in use | change the port at the bottom of `app.py` |
| "Session expired" message | reload the page (the security token timed out) |
| Want a clean slate | stop the app and delete the `instance/` folder |
| Fonts look plain | the page uses Google Fonts; offline it falls back to system fonts |
