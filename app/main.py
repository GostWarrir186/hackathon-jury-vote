import csv
import io
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

DB_PATH = os.getenv("DB_PATH", "/data/jury.db")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
P = urlparse(BASE_URL).path.rstrip("/")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
EVENT_TITLE = os.getenv("EVENT_TITLE", "Hackathon 2026")

CRITERIA = [
    ("c1", "Решение реальной проблемы", 15),
    ("c2", "Инновационность", 15),
    ("c3", "Технологическая реализация / MVP", 20),
    ("c4", "Реалистичность внедрения", 20),
    ("c5", "Экономический / социальный эффект", 10),
    ("c6", "Потенциал масштабирования в Центральной Азии", 10),
    ("c7", "Pitch + ответы на вопросы", 10),
]
CKEYS = [c[0] for c in CRITERIA]
WEIGHT = {c[0]: c[2] for c in CRITERIA}
TRIM_MIN_VOTES = 5

SEED_JURORS = [
    ("Член жюри 1", "Председатель жюри"),
    ("Член жюри 2", "Эксперт по e-commerce"),
    ("Член жюри 3", "Эксперт по trade facilitation"),
    ("Член жюри 4", "Fintech / regulator"),
    ("Член жюри 5", "Fintech / бизнес"),
    ("Член жюри 6", "Marketplace / e-commerce"),
    ("Член жюри 7", "Logistics / digital trade"),
    ("Член жюри 8", "Technology"),
    ("Член жюри 9", "Investment"),
]

app = FastAPI(title="Jury Vote")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.globals.update(EVENT_TITLE=EVENT_TITLE, CRITERIA=CRITERIA, P=P)
security = HTTPBasic()


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as con:
        con.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS jurors(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT DEFAULT '',
              token TEXT UNIQUE NOT NULL, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS projects(
              id INTEGER PRIMARY KEY, num INTEGER NOT NULL, name TEXT NOT NULL,
              team TEXT DEFAULT '', active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS scores(
              juror_id INTEGER REFERENCES jurors(id) ON DELETE CASCADE,
              project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
              {", ".join(k + " INTEGER" for k in CKEYS)},
              comment TEXT DEFAULT '', updated_at TEXT,
              PRIMARY KEY(juror_id, project_id));
            CREATE TABLE IF NOT EXISTS conflicts(
              juror_id INTEGER REFERENCES jurors(id) ON DELETE CASCADE,
              project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
              PRIMARY KEY(juror_id, project_id));
            CREATE TABLE IF NOT EXISTS audience_votes(
              voter TEXT PRIMARY KEY, project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
              created_at TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        cols = {r[1] for r in con.execute("PRAGMA table_info(jurors)")}
        if "onboarded" not in cols:
            con.execute("ALTER TABLE jurors ADD COLUMN onboarded INTEGER DEFAULT 0")
        if con.execute("SELECT COUNT(*) FROM jurors").fetchone()[0] == 0:
            for name, role in SEED_JURORS:
                con.execute("INSERT INTO jurors(name, role, token) VALUES(?,?,?)",
                            (name, role, secrets.token_urlsafe(9)))
        if con.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
            for i in range(1, 11):
                con.execute("INSERT INTO projects(num, name) VALUES(?,?)", (i, f"Проект {i}"))
        for k, v in {"jury_open": "0", "audience_open": "0", "results_public": "0",
                     "current_project": ""}.items():
            con.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))


@app.on_event("startup")
def _startup():
    init_db()


def get_settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT * FROM settings")}


def set_setting(con, key, value):
    con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))


def admin(creds: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest(creds.username, ADMIN_USER) and \
        secrets.compare_digest(creds.password, ADMIN_PASSWORD)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    return True


def juror_by_token(con, token):
    j = con.execute("SELECT * FROM jurors WHERE token=? AND active=1", (token,)).fetchone()
    if not j:
        raise HTTPException(404, "Ссылка недействительна")
    return j


def total_of(row):
    return round(sum(row[k] * WEIGHT[k] / 10 for k in CKEYS), 2)


def compute_results(con):
    projects = con.execute("SELECT * FROM projects WHERE active=1 ORDER BY num").fetchall()
    conflicts = {(r[0], r[1]) for r in con.execute("SELECT juror_id, project_id FROM conflicts")}
    active_j = {r[0] for r in con.execute("SELECT id FROM jurors WHERE active=1")}
    res = []
    for p in projects:
        rows = [r for r in con.execute("SELECT * FROM scores WHERE project_id=?", (p["id"],))
                if (r["juror_id"], p["id"]) not in conflicts and r["juror_id"] in active_j]
        totals = sorted(total_of(r) for r in rows)
        n = len(totals)
        trimmed = totals[1:-1] if n >= TRIM_MIN_VOTES else totals
        final = round(sum(trimmed) / len(trimmed), 2) if trimmed else None
        crit = {k: (round(sum(r[k] * WEIGHT[k] / 10 for r in rows) / n, 2) if n else 0) for k in CKEYS}
        eligible = len(active_j) - sum(1 for j in active_j if (j, p["id"]) in conflicts)
        res.append(dict(id=p["id"], num=p["num"], name=p["name"], team=p["team"], n=n,
                        eligible=eligible, totals=totals, trimmed=n >= TRIM_MIN_VOTES,
                        final=final, crit=crit))
    res.sort(key=lambda r: (r["final"] if r["final"] is not None else -1,
                            r["crit"]["c4"], r["crit"]["c3"], r["crit"]["c6"]), reverse=True)
    place = 0
    for i, r in enumerate(res):
        r["place"] = i + 1 if r["final"] is not None else None
        prev = res[i - 1] if i else None
        r["tie"] = bool(prev and r["final"] is not None and prev["final"] == r["final"]
                        and all(prev["crit"][k] == r["crit"][k] for k in ("c4", "c3", "c6")))
        if r["tie"]:
            prev["tie"] = True
    return res


def audience_results(con):
    return con.execute(
        """SELECT p.id, p.num, p.name, p.team, COUNT(v.voter) AS votes
           FROM projects p LEFT JOIN audience_votes v ON v.project_id=p.id
           WHERE p.active=1 GROUP BY p.id ORDER BY votes DESC, p.num""").fetchall()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/j/{token}/intro", response_class=HTMLResponse)
def jury_intro(request: Request, token: str):
    with db() as con:
        j = juror_by_token(con, token)
        n = con.execute("SELECT COUNT(*) FROM projects WHERE active=1").fetchone()[0]
    return templates.TemplateResponse(request, "jury_intro.html", dict(j=j, token=token, n=n))


@app.post("/j/{token}/start")
def jury_start(token: str):
    with db() as con:
        j = juror_by_token(con, token)
        con.execute("UPDATE jurors SET onboarded=1 WHERE id=?", (j["id"],))
        cur = get_settings(con)["current_project"]
        ok = cur and con.execute("SELECT 1 FROM projects WHERE id=? AND active=1", (cur,)).fetchone() and \
            not con.execute("SELECT 1 FROM conflicts WHERE juror_id=? AND project_id=?", (j["id"], cur)).fetchone()
    return RedirectResponse(f"{P}/j/{token}/p/{cur}" if ok else f"{P}/j/{token}", status_code=303)


@app.get("/j/{token}", response_class=HTMLResponse)
def jury_home(request: Request, token: str, saved: int = 0):
    with db() as con:
        j = juror_by_token(con, token)
        if not j["onboarded"]:
            return RedirectResponse(f"{P}/j/{token}/intro", status_code=303)
        s = get_settings(con)
        saved_p = con.execute("SELECT num, name FROM projects WHERE id=?", (saved,)).fetchone() if saved else None
        projects = con.execute("SELECT * FROM projects WHERE active=1 ORDER BY num").fetchall()
        scored = {r["project_id"]: total_of(r) for r in
                  con.execute("SELECT * FROM scores WHERE juror_id=?", (j["id"],))}
        conf = {r[0] for r in con.execute("SELECT project_id FROM conflicts WHERE juror_id=?", (j["id"],))}
    return templates.TemplateResponse(request, "jury_home.html", dict(
        j=j, s=s, projects=projects, scored=scored, conf=conf, token=token, saved=saved, saved_p=saved_p))


@app.get("/j/{token}/p/{pid}", response_class=HTMLResponse)
def jury_form(request: Request, token: str, pid: int, saved: int = 0):
    with db() as con:
        j = juror_by_token(con, token)
        s = get_settings(con)
        p = con.execute("SELECT * FROM projects WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            raise HTTPException(404)
        conflict = con.execute("SELECT 1 FROM conflicts WHERE juror_id=? AND project_id=?",
                               (j["id"], pid)).fetchone() is not None
        sc = con.execute("SELECT * FROM scores WHERE juror_id=? AND project_id=?", (j["id"], pid)).fetchone()
    return templates.TemplateResponse(request, "jury_form.html", dict(
        j=j, s=s, p=p, sc=sc, conflict=conflict, token=token, saved=saved))


@app.post("/j/{token}/p/{pid}")
async def jury_submit(request: Request, token: str, pid: int):
    form = await request.form()
    with db() as con:
        j = juror_by_token(con, token)
        if get_settings(con)["jury_open"] != "1":
            raise HTTPException(403, "Голосование жюри закрыто")
        if con.execute("SELECT 1 FROM conflicts WHERE juror_id=? AND project_id=?", (j["id"], pid)).fetchone():
            raise HTTPException(403, "Конфликт интересов: вы не оцениваете этот проект")
        if not con.execute("SELECT 1 FROM projects WHERE id=? AND active=1", (pid,)).fetchone():
            raise HTTPException(404)
        vals = []
        for k in CKEYS:
            try:
                v = int(form.get(k, ""))
            except ValueError:
                raise HTTPException(400, "Заполните все критерии")
            if not 1 <= v <= 10:
                raise HTTPException(400, "Оценка должна быть от 1 до 10")
            vals.append(v)
        comment = str(form.get("comment", ""))[:1000]
        con.execute(
            f"""INSERT INTO scores(juror_id, project_id, {",".join(CKEYS)}, comment, updated_at)
                VALUES(?,?,{",".join("?" * len(CKEYS))},?,?)
                ON CONFLICT(juror_id, project_id) DO UPDATE SET
                {",".join(f"{k}=excluded.{k}" for k in CKEYS)}, comment=excluded.comment,
                updated_at=excluded.updated_at""",
            (j["id"], pid, *vals, comment, datetime.now().isoformat(timespec="seconds")))
    return RedirectResponse(f"{P}/j/{token}?saved={pid}", status_code=303)


@app.get("/api/state")
def api_state():
    with db() as con:
        s = get_settings(con)
        cur = None
        if s["current_project"]:
            cur = con.execute("SELECT id, num, name, team FROM projects WHERE id=? AND active=1",
                              (s["current_project"],)).fetchone()
    return JSONResponse(
        {"jury_open": s["jury_open"] == "1", "audience_open": s["audience_open"] == "1",
         "current_project": str(cur["id"]) if cur else "",
         "current": dict(cur) if cur else None},
        headers={"Cache-Control": "no-store"})


def norm_voter(raw: str) -> str:
    raw = raw.strip().lower()
    if "@" in raw:
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:
        digits = "992" + digits
    return digits


@app.get("/vote", response_class=HTMLResponse)
def vote_page(request: Request, done: int = 0):
    with db() as con:
        s = get_settings(con)
        projects = con.execute("SELECT * FROM projects WHERE active=1 ORDER BY num").fetchall()
    voted = request.cookies.get("voted")
    return templates.TemplateResponse(request, "vote.html", dict(
        s=s, projects=projects, done=done, voted=voted, error=None))


@app.post("/vote", response_class=HTMLResponse)
def vote_submit(request: Request, voter: str = Form(...), project_id: int = Form(...)):
    v = norm_voter(voter)
    with db() as con:
        s = get_settings(con)
        projects = con.execute("SELECT * FROM projects WHERE active=1 ORDER BY num").fetchall()
        err = None
        if s["audience_open"] != "1":
            err = "Голосование зрителей сейчас закрыто."
        elif not (("@" in v and len(v) >= 5) or len(v) >= 9):
            err = "Укажите корректный номер телефона или e-mail."
        elif request.cookies.get("voted"):
            err = "С этого устройства уже проголосовали."
        elif con.execute("SELECT 1 FROM audience_votes WHERE voter=?", (v,)).fetchone():
            err = "С этого номера/e-mail уже был голос. Один участник — один голос."
        elif not con.execute("SELECT 1 FROM projects WHERE id=? AND active=1", (project_id,)).fetchone():
            err = "Проект не найден."
        if err:
            return templates.TemplateResponse(request, "vote.html", dict(
                s=s, projects=projects, done=0, voted=None, error=err), status_code=400)
        con.execute("INSERT INTO audience_votes VALUES(?,?,?)",
                    (v, project_id, datetime.now().isoformat(timespec="seconds")))
    r = RedirectResponse(f"{P}/vote?done=1", status_code=303)
    r.set_cookie("voted", "1", max_age=60 * 60 * 24 * 7)
    return r


@app.get("/screen", response_class=HTMLResponse)
def screen(request: Request):
    with db() as con:
        s = get_settings(con)
        res = compute_results(con) if s["results_public"] == "1" else []
        aud = audience_results(con) if s["results_public"] == "1" else []
    return templates.TemplateResponse(request, "screen.html", dict(s=s, res=res, aud=aud))


def qr_svg(data: str) -> str:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


@app.get("/qr.svg")
def qr_endpoint(data: str, _=Depends(admin)):
    return Response(qr_svg(data), media_type="image/svg+xml")


@app.get("/qr/audience.svg")
def qr_audience():
    return Response(qr_svg(f"{BASE_URL}/vote"), media_type="image/svg+xml")


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, _=Depends(admin)):
    with db() as con:
        s = get_settings(con)
        jurors = con.execute("SELECT * FROM jurors WHERE active=1 ORDER BY id").fetchall()
        projects = con.execute("SELECT * FROM projects WHERE active=1 ORDER BY num").fetchall()
        scored = {(r[0], r[1]) for r in con.execute("SELECT juror_id, project_id FROM scores")}
        conflicts = {(r[0], r[1]) for r in con.execute("SELECT juror_id, project_id FROM conflicts")}
        res = compute_results(con)
        aud = audience_results(con)
        aud_total = con.execute("SELECT COUNT(*) FROM audience_votes").fetchone()[0]
    return templates.TemplateResponse(request, "admin.html", dict(
        s=s, jurors=jurors, projects=projects, scored=scored, conflicts=conflicts,
        res=res, aud=aud, aud_total=aud_total, base=BASE_URL))


@app.post("/admin/settings")
async def admin_settings(request: Request, _=Depends(admin)):
    form = await request.form()
    with db() as con:
        for k in ("jury_open", "audience_open", "results_public", "current_project"):
            if k in form:
                set_setting(con, k, form[k])
    return RedirectResponse(f"{P}/admin", status_code=303)


@app.get("/admin/setup", response_class=HTMLResponse)
def admin_setup(request: Request, _=Depends(admin)):
    with db() as con:
        jurors = con.execute("SELECT * FROM jurors WHERE active=1 ORDER BY id").fetchall()
        projects = con.execute("SELECT * FROM projects WHERE active=1 ORDER BY num").fetchall()
        conflicts = {(r[0], r[1]) for r in con.execute("SELECT juror_id, project_id FROM conflicts")}
    return templates.TemplateResponse(request, "setup.html", dict(
        jurors=jurors, projects=projects, conflicts=conflicts))


@app.post("/admin/projects")
async def admin_projects(request: Request, _=Depends(admin)):
    form = await request.form()
    with db() as con:
        for p in con.execute("SELECT id FROM projects WHERE active=1").fetchall():
            pid = p["id"]
            if form.get(f"del_{pid}"):
                con.execute("UPDATE projects SET active=0 WHERE id=?", (pid,))
                continue
            con.execute("UPDATE projects SET num=?, name=?, team=? WHERE id=?",
                        (int(form.get(f"num_{pid}") or 0), form.get(f"name_{pid}", "").strip() or "—",
                         form.get(f"team_{pid}", "").strip(), pid))
        if form.get("new_name", "").strip():
            n = con.execute("SELECT COALESCE(MAX(num),0)+1 FROM projects WHERE active=1").fetchone()[0]
            con.execute("INSERT INTO projects(num, name, team) VALUES(?,?,?)",
                        (n, form["new_name"].strip(), form.get("new_team", "").strip()))
    return RedirectResponse(f"{P}/admin/setup#projects", status_code=303)


@app.post("/admin/jurors")
async def admin_jurors(request: Request, _=Depends(admin)):
    form = await request.form()
    with db() as con:
        for j in con.execute("SELECT id FROM jurors WHERE active=1").fetchall():
            jid = j["id"]
            if form.get(f"del_{jid}"):
                con.execute("UPDATE jurors SET active=0 WHERE id=?", (jid,))
                continue
            con.execute("UPDATE jurors SET name=?, role=? WHERE id=?",
                        (form.get(f"name_{jid}", "").strip() or "—", form.get(f"role_{jid}", "").strip(), jid))
            if form.get(f"regen_{jid}"):
                con.execute("UPDATE jurors SET token=?, onboarded=0 WHERE id=?", (secrets.token_urlsafe(9), jid))
        if form.get("new_name", "").strip():
            con.execute("INSERT INTO jurors(name, role, token) VALUES(?,?,?)",
                        (form["new_name"].strip(), form.get("new_role", "").strip(), secrets.token_urlsafe(9)))
    return RedirectResponse(f"{P}/admin/setup#jurors", status_code=303)


@app.post("/admin/conflicts")
async def admin_conflicts(request: Request, _=Depends(admin)):
    form = await request.form()
    with db() as con:
        con.execute("DELETE FROM conflicts")
        for key in form.keys():
            if key.startswith("cf_"):
                _, jid, pid = key.split("_")
                con.execute("INSERT OR IGNORE INTO conflicts VALUES(?,?)", (int(jid), int(pid)))
    return RedirectResponse(f"{P}/admin/setup#conflicts", status_code=303)


@app.post("/admin/reset")
async def admin_reset(request: Request, _=Depends(admin)):
    form = await request.form()
    if form.get("confirm") != "СБРОС":
        raise HTTPException(400, "Для сброса введите СБРОС")
    with db() as con:
        what = form.get("what")
        if what in ("scores", "all"):
            con.execute("DELETE FROM scores")
        if what in ("audience", "all"):
            con.execute("DELETE FROM audience_votes")
        if what == "all":
            con.execute("UPDATE jurors SET onboarded=0")
    return RedirectResponse(f"{P}/admin/setup", status_code=303)


@app.get("/admin/qr", response_class=HTMLResponse)
def admin_qr(request: Request, _=Depends(admin)):
    with db() as con:
        jurors = con.execute("SELECT * FROM jurors WHERE active=1 ORDER BY id").fetchall()
    cards = [dict(name=j["name"], role=j["role"], url=f"{BASE_URL}/j/{j['token']}",
                  svg=qr_svg(f"{BASE_URL}/j/{j['token']}")) for j in jurors]
    aud = dict(url=f"{BASE_URL}/vote", svg=qr_svg(f"{BASE_URL}/vote"))
    return templates.TemplateResponse(request, "qr.html", dict(cards=cards, aud=aud))


@app.get("/admin/export.csv")
def admin_export(_=Depends(admin)):
    with db() as con:
        res = compute_results(con)
        rows = con.execute(
            f"""SELECT j.name AS juror, p.num, p.name AS project, {",".join("s." + k for k in CKEYS)},
                s.comment, s.updated_at FROM scores s JOIN jurors j ON j.id=s.juror_id
                JOIN projects p ON p.id=s.project_id ORDER BY p.num, j.id""").fetchall()
        aud = audience_results(con)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ИТОГОВЫЙ РЕЙТИНГ ЖЮРИ"])
    w.writerow(["Место", "№", "Проект", "Команда", "Итог (усеч. среднее)", "Оценок", "Отброшены max/min",
                *[c[1] for c in CRITERIA]])
    for r in res:
        w.writerow([r["place"] or "", r["num"], r["name"], r["team"],
                    str(r["final"]).replace(".", ",") if r["final"] is not None else "",
                    r["n"], "да" if r["trimmed"] else "нет",
                    *[str(r["crit"][k]).replace(".", ",") for k in CKEYS]])
    w.writerow([])
    w.writerow(["ОЦЕНКИ ЖЮРИ (1–10 по критерию)"])
    w.writerow(["Член жюри", "№", "Проект", *[f"{c[1]} ({c[2]})" for c in CRITERIA], "Сумма /100",
                "Комментарий", "Время"])
    for r in rows:
        w.writerow([r["juror"], r["num"], r["project"], *[r[k] for k in CKEYS],
                    str(total_of(r)).replace(".", ","), r["comment"], r["updated_at"]])
    w.writerow([])
    w.writerow(["ВЫБОР АУДИТОРИИ"])
    w.writerow(["№", "Проект", "Голосов"])
    for a in aud:
        w.writerow([a["num"], a["name"], a["votes"]])
    data = "\ufeff" + buf.getvalue()
    fname = f"jury_results_{datetime.now():%Y%m%d_%H%M}.csv"
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})
