# Shuffle Cup Tabulation

A compact, server-rendered Django tab system for an individual-entry British Parliamentary tournament with five preliminary rounds, automatic swings, random temporary partnerships, open semifinals, an open final, and a novice final.

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/admin/` to sign in, then use `http://127.0.0.1:8000/manage/` for tournament operations. Click **Create standard rounds**, import the CSV files, and generate Round 1.

CSV imports validate and show a preview before committing. Required headers are:

```text
Debaters: name,email,society,is_novice
Judges: name,email,society
Partner conflicts: debater_1_email,debater_2_email,reason
Judge conflicts: judge_email,debater_email,reason
```

Conflict imports can alternatively include `debater_1_name`, `debater_2_name`, `judge_name`, or `debater_name` for exact-name lookup when the corresponding email is blank.

## Tournament workflow

Use the management dashboard to generate a draft draw, review warnings, swap participants, publish the draw, allocate judges, and create private ballot links. Submitted ballots remain unofficial until the tab admin confirms each room. Public standings include only confirmed, non-silent preliminary rounds. Rounds 4 and 5 are created as silent by default.

After Round 5, record choices for dual-eligible novices on **Manage break choices**. Generate the open semifinals and novice final from their round pages. Once semifinal results are confirmed, generate the open final. Publish final tabs only after both finals are confirmed.

## Tests

```powershell
python manage.py test
```

## Render deployment

The included `render.yaml` creates a web service and PostgreSQL database. In Render, create a Blueprint from the repository and adjust `ALLOWED_HOSTS` to the assigned hostname if it differs from `shuffle-cup.onrender.com`.

The service uses these environment variables:

- `DATABASE_URL`
- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`

The build command installs dependencies, collects static files, and runs migrations. The start command is:

```text
gunicorn shufflecup.wsgi:application
```

Create the production administrator from Render Shell:

```text
python manage.py createsuperuser
```
