# Shuffle Cup Tabulation

A compact, server-rendered Django tab system for an individual-entry British Parliamentary tournament with four preliminary rounds, automatic swings, random temporary partnerships, open semifinals, and a final.

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`, click **Login**, and sign in. You will be redirected to `/manage/` for tournament operations. Local development uses SQLite by default, so Render is not required for testing.

CSV imports validate and show a preview before committing. Required headers are:

```text
Debaters: name,email,society
Judges: name,email,society
Partner conflicts: debater_1_email,debater_2_email
Judge conflicts: judge_email,debater_email
```

Conflict imports can alternatively include `debater_1_name`, `debater_2_name`, `judge_name`, or `debater_name` for exact-name lookup when the corresponding email is blank.

## Tournament workflow

Use the management dashboard to generate and publish the draw, review warnings, and allocate judges. On each preliminary round page, mark any debaters unavailable for that round before generating or regenerating the draw; they remain active for later rounds. Each judge has a fixed private URL; chairs use it to submit the result of their room in the current round. Submitted results remain unofficial until the tab team confirms each room. Public standings show only total points through Round 2 until the tournament is closed. Rounds 3 and 4 are created as silent by default.

After Round 4, the top 16 debaters break to the open semifinals. Once semifinal results are confirmed, generate the final. Publish final tabs only after the final result is confirmed.

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
