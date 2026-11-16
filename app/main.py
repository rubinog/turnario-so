import calendar
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)
    APP_DATA_DIR = Path(sys.executable).parent / "data"
    STATIC_DIR = BASE_DIR / "app" / "static"
elif os.path.exists("/app/data"):
    BASE_DIR = Path(__file__).parent
    APP_DATA_DIR = Path("/app/data")
    STATIC_DIR = BASE_DIR / "static"
else:
    BASE_DIR = Path(__file__).parent
    APP_DATA_DIR = Path(__file__).parent.parent / "data"
    STATIC_DIR = BASE_DIR / "static"

DB_PATH = APP_DATA_DIR / "turnario.db"

# Sequenza perpetua 32 giorni ancorata al 01/01/2027 (Diurno = C1)
SEQUENZA_32 = [
    "C1", "D1", "A2", "B2", "C2", "D2", "A3", "B3",
    "C3", "D3", "A4", "B4", "C4", "D4", "A5", "B5",
    "C5", "D5", "A6", "B6", "C6", "D6", "A7", "B7",
    "C7", "D7", "A8", "B8", "C8", "D8", "A1", "B1"
]
ANCHOR_DATE = date(2027, 1, 1)

app = FastAPI(title="Turnario Sala Operativa - Sez. D")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def calcola_turno_giorno(d: date) -> dict:
    """Restituisce i codici Diurno/Notturno e l'eventuale turno della Sezione D."""
    delta_d = (d - ANCHOR_DATE).days
    diurno = SEQUENZA_32[delta_d % 32]
    notturno = SEQUENZA_32[(delta_d - 1) % 32]

    turno_sez_d = None
    tipo_sez_d = None
    if diurno.startswith("D"):
        turno_sez_d = diurno
        tipo_sez_d = "D"
    elif notturno.startswith("D"):
        turno_sez_d = notturno
        tipo_sez_d = "N"

    return {
        "data": d.isoformat(),
        "giorno": d.day,
        "diurno": diurno,
        "notturno": notturno,
        "turno_sez_d": turno_sez_d,
        "tipo_sez_d": tipo_sez_d,
    }


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dipendenti (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
                salto TEXT NOT NULL CHECK(salto IN ('D1','D2','D3','D4','D5','D6','D7','D8')),
                qualifica TEXT NOT NULL DEFAULT 'V' CHECK(qualifica IN ('Q','V')),
                responsabile INTEGER NOT NULL DEFAULT 0,
                attivo INTEGER NOT NULL DEFAULT 1
            );
        """)

        cols = [row[1] for row in conn.execute("PRAGMA table_info(dipendenti)").fetchall()]
        if "qualifica" not in cols:
            conn.execute("ALTER TABLE dipendenti ADD COLUMN qualifica TEXT NOT NULL DEFAULT 'V'")
        if "responsabile" not in cols:
            conn.execute("ALTER TABLE dipendenti ADD COLUMN responsabile INTEGER NOT NULL DEFAULT 0")

        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='assegnazioni'"
        ).fetchone()

        if table_sql_row and "'L'" not in table_sql_row["sql"]:
            conn.execute("""
                CREATE TABLE assegnazioni_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dipendente_id INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    mansione TEXT NOT NULL CHECK(mansione IN ('T','TN','UCL','L')),
                    UNIQUE(dipendente_id, data),
                    FOREIGN KEY(dipendente_id) REFERENCES dipendenti(id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                INSERT INTO assegnazioni_new (id, dipendente_id, data, mansione)
                SELECT id, dipendente_id, data, mansione FROM assegnazioni;
            """)
            conn.execute("DROP TABLE assegnazioni;")
            conn.execute("ALTER TABLE assegnazioni_new RENAME TO assegnazioni;")
        else:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS assegnazioni (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dipendente_id INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    mansione TEXT NOT NULL CHECK(mansione IN ('T','TN','UCL','L')),
                    UNIQUE(dipendente_id, data),
                    FOREIGN KEY(dipendente_id) REFERENCES dipendenti(id) ON DELETE CASCADE
                );
            """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_assegnazioni_data ON assegnazioni(data);")

        tn_rows = conn.execute("SELECT id, data FROM assegnazioni WHERE mansione = 'TN'").fetchall()
        ids_da_convertire = []
        for r in tn_rows:
            try:
                info = calcola_turno_giorno(date.fromisoformat(r["data"]))
                if info["tipo_sez_d"] != "N":
                    ids_da_convertire.append((r["id"],))
            except ValueError:
                pass
        if ids_da_convertire:
            conn.executemany("UPDATE assegnazioni SET mansione = 'T' WHERE id = ?", ids_da_convertire)


@app.on_event("startup")
def startup_event():
    init_db()


def auto_compila_periodo(conn: sqlite3.Connection, anno: int, mese: Optional[int] = None) -> int:
    dipendenti = [
        dict(r) for r in conn.execute(
            "SELECT id, nome, salto, qualifica, responsabile FROM dipendenti WHERE attivo = 1"
        ).fetchall()
    ]
    if not dipendenti:
        return 0

    mesi_da_elaborare = [mese] if mese else list(range(1, 13))
    primo_mese = mesi_da_elaborare[0]
    ultimo_mese = mesi_da_elaborare[-1]
    ultimo_giorno_periodo = calendar.monthrange(anno, ultimo_mese)[1]

    inizio_anno_str = f"{anno:04d}-01-01"
    fine_periodo_str = f"{anno:04d}-{ultimo_mese:02d}-{ultimo_giorno_periodo:02d}"

    rows = conn.execute(
        "SELECT dipendente_id, data, mansione FROM assegnazioni WHERE data BETWEEN ? AND ?",
        (inizio_anno_str, fine_periodo_str)
    ).fetchall()

    assegnazioni = {(r["dipendente_id"], r["data"]): r["mansione"] for r in rows}

    inizio_periodo_str = f"{anno:04d}-{primo_mese:02d}-01"
    conteggio_tn = {d["id"]: 0 for d in dipendenti}
    conteggio_tot = {d["id"]: 0 for d in dipendenti}

    for (did, d_str), mansione in assegnazioni.items():
        if d_str < inizio_periodo_str and did in conteggio_tot:
            if mansione in ("T", "TN", "UCL"):
                conteggio_tot[did] += 1
            if mansione == "TN":
                conteggio_tn[did] += 1

    giorni_sez_d = []
    for m in mesi_da_elaborare:
        num_g = calendar.monthrange(anno, m)[1]
        for g in range(1, num_g + 1):
            info = calcola_turno_giorno(date(anno, m, g))
            if info["turno_sez_d"]:
                giorni_sez_d.append(info)

    blocchi = []
    i = 0
    while i < len(giorni_sez_d):
        curr = giorni_sez_d[i]
        if i + 1 < len(giorni_sez_d):
            nxt = giorni_sez_d[i + 1]
            d_curr = date.fromisoformat(curr["data"])
            d_nxt = date.fromisoformat(nxt["data"])
            if (
                curr["turno_sez_d"] == nxt["turno_sez_d"]
                and curr["tipo_sez_d"] == "D"
                and nxt["tipo_sez_d"] == "N"
                and (d_nxt - d_curr).days == 1
            ):
                blocchi.append([curr, nxt])
                i += 2
                continue
        blocchi.append([curr])
        i += 1

    nuove_assegnazioni = []

    def registra(did: int, dt: str, mansione: str):
        if (did, dt) not in assegnazioni:
            assegnazioni[(did, dt)] = mansione
            nuove_assegnazioni.append((did, dt, mansione))
            conteggio_tot[did] += 1
            if mansione == "TN":
                conteggio_tn[did] += 1

    for blocco in blocchi:
        codice_turno = blocco[0]["turno_sez_d"]
        date_blocco = [b["data"] for b in blocco]
        blocco_ha_notte = any(b["tipo_sez_d"] == "N" for b in blocco)

        gia_q_t, gia_q_tn = set(), set()
        gia_v_t, gia_v_tn = set(), set()

        for d in dipendenti:
            did = d["id"]
            mansioni_dip = [assegnazioni.get((did, dt)) for dt in date_blocco]
            if any(m in ("T", "TN", "UCL") for m in mansioni_dip):
                is_tn = any(m == "TN" for m in mansioni_dip)
                if d["qualifica"] == "Q":
                    (gia_q_tn if is_tn else gia_q_t).add(did)
                else:
                    (gia_v_tn if is_tn else gia_v_t).add(did)

        gia_assegnati_blocco = gia_q_t | gia_q_tn | gia_v_t | gia_v_tn

        def libero_intero_blocco(dip_obj: dict) -> bool:
            if dip_obj["salto"] == codice_turno:
                return False
            for dt in date_blocco:
                if (dip_obj["id"], dt) in assegnazioni:
                    return False
            return True

        disp_q = [d for d in dipendenti if d["qualifica"] == "Q" and d["id"] not in gia_assegnati_blocco and libero_intero_blocco(d)]
        disp_v = [d for d in dipendenti if d["qualifica"] == "V" and d["id"] not in gia_assegnati_blocco and libero_intero_blocco(d)]

        scelti_q_tn = list(gia_q_tn)
        scelti_q_t = list(gia_q_t)

        if blocco_ha_notte and len(scelti_q_tn) < 1 and disp_q:
            candidati_q_tn = sorted(
                disp_q,
                key=lambda x: (conteggio_tn[x["id"]], 0 if x["responsabile"] else 1, conteggio_tot[x["id"]], x["id"])
            )
            scelto = candidati_q_tn[0]
            scelti_q_tn.append(scelto["id"])
            disp_q.remove(scelto)

        while (len(scelti_q_tn) + len(scelti_q_t)) < 2 and disp_q:
            candidati_q_t = sorted(
                disp_q,
                key=lambda x: (0 if x["responsabile"] else 1, conteggio_tot[x["id"]], conteggio_tn[x["id"]], x["id"])
            )
            scelto = candidati_q_t[0]
            scelti_q_t.append(scelto["id"])
            disp_q.remove(scelto)

        scelti_v_tn = list(gia_v_tn)
        scelti_v_t = list(gia_v_t)

        if blocco_ha_notte and len(scelti_v_tn) < 1 and disp_v:
            candidati_v_tn = sorted(
                disp_v,
                key=lambda x: (conteggio_tn[x["id"]], conteggio_tot[x["id"]], x["id"])
            )
            scelto = candidati_v_tn[0]
            scelti_v_tn.append(scelto["id"])
            disp_v.remove(scelto)

        while (len(scelti_v_tn) + len(scelti_v_t)) < 4 and disp_v:
            candidati_v_t = sorted(
                disp_v,
                key=lambda x: (conteggio_tot[x["id"]], conteggio_tn[x["id"]], x["id"])
            )
            scelto = candidati_v_t[0]
            scelti_v_t.append(scelto["id"])
            disp_v.remove(scelto)

        for b_info in blocco:
            dt = b_info["data"]
            is_notte = (b_info["tipo_sez_d"] == "N")
            for did in scelti_q_tn + scelti_v_tn:
                registra(did, dt, "TN" if is_notte else "T")
            for did in scelti_q_t + scelti_v_t:
                registra(did, dt, "T")

        for b_info in blocco:
            dt = b_info["data"]
            is_notte = (b_info["tipo_sez_d"] == "N")

            q_presenti = [
                d for d in dipendenti
                if d["qualifica"] == "Q" and assegnazioni.get((d["id"], dt)) in ("T", "TN", "UCL")
            ]
            v_presenti = [
                d for d in dipendenti
                if d["qualifica"] == "V" and assegnazioni.get((d["id"], dt)) in ("T", "TN", "UCL")
            ]

            has_q_tn = any(assegnazioni.get((d["id"], dt)) == "TN" for d in q_presenti)
            has_v_tn = any(assegnazioni.get((d["id"], dt)) == "TN" for d in v_presenti)

            disp_q_giorno = [
                d for d in dipendenti
                if d["qualifica"] == "Q" and d["salto"] != codice_turno and (d["id"], dt) not in assegnazioni
            ]
            disp_v_giorno = [
                d for d in dipendenti
                if d["qualifica"] == "V" and d["salto"] != codice_turno and (d["id"], dt) not in assegnazioni
            ]

            while len(q_presenti) < 2 and disp_q_giorno:
                if is_notte and not has_q_tn:
                    candidati = sorted(
                        disp_q_giorno,
                        key=lambda x: (conteggio_tn[x["id"]], 0 if x["responsabile"] else 1, conteggio_tot[x["id"]], x["id"])
                    )
                    scelto = candidati[0]
                    registra(scelto["id"], dt, "TN")
                    has_q_tn = True
                else:
                    candidati = sorted(
                        disp_q_giorno,
                        key=lambda x: (0 if x["responsabile"] else 1, conteggio_tot[x["id"]], conteggio_tn[x["id"]], x["id"])
                    )
                    scelto = candidati[0]
                    registra(scelto["id"], dt, "T")
                q_presenti.append(scelto)
                disp_q_giorno.remove(scelto)

            while len(v_presenti) < 4 and disp_v_giorno:
                if is_notte and not has_v_tn:
                    candidati = sorted(
                        disp_v_giorno,
                        key=lambda x: (conteggio_tn[x["id"]], conteggio_tot[x["id"]], x["id"])
                    )
                    scelto = candidati[0]
                    registra(scelto["id"], dt, "TN")
                    has_v_tn = True
                else:
                    candidati = sorted(
                        disp_v_giorno,
                        key=lambda x: (conteggio_tot[x["id"]], conteggio_tn[x["id"]], x["id"])
                    )
                    scelto = candidati[0]
                    registra(scelto["id"], dt, "T")
                v_presenti.append(scelto)
                disp_v_giorno.remove(scelto)

    if nuove_assegnazioni:
        conn.executemany(
            """
            INSERT INTO assegnazioni (dipendente_id, data, mansione)
            VALUES (?, ?, ?)
            ON CONFLICT(dipendente_id, data) DO NOTHING
            """,
            nuove_assegnazioni
        )

    return len(nuove_assegnazioni)


# --- SCHEMI PYDANTIC ---

class DipendenteCreate(BaseModel):
    nome: str
    salto: Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"]
    qualifica: Literal["Q", "V"] = "V"
    responsabile: bool = False


class DipendenteUpdate(BaseModel):
    nome: Optional[str] = None
    salto: Optional[Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"]] = None
    qualifica: Optional[Literal["Q", "V"]] = None
    responsabile: Optional[bool] = None
    attivo: Optional[bool] = None


class AssegnazioneInput(BaseModel):
    dipendente_id: int
    data: str  # YYYY-MM-DD
    mansione: Optional[Literal["T", "TN", "UCL", "L", ""]] = ""


# --- ENDPOINT API ---

@app.get("/api/backup")
def scarica_backup_db(background_tasks: BackgroundTasks):
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database non trovato")

    tmp = NamedTemporaryFile(delete=False, suffix=".db")
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        with sqlite3.connect(DB_PATH) as src, sqlite3.connect(tmp_path) as dst:
            src.backup(dst)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Errore creazione backup: {exc}")

    background_tasks.add_task(tmp_path.unlink, missing_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"turnario_backup_{timestamp}.db"

    return FileResponse(
        path=tmp_path,
        filename=filename,
        media_type="application/x-sqlite3",
    )


@app.post("/api/restore")
async def ripristina_backup_db(file: UploadFile = File(...)):
    contenuto = await file.read()
    if len(contenuto) < 100 or not contenuto.startswith(b"SQLite format 3\x00"):
        raise HTTPException(status_code=400, detail="Il file selezionato non è un database SQLite valido.")

    tmp = NamedTemporaryFile(delete=False, suffix=".db")
    tmp_path = Path(tmp.name)
    try:
        tmp.write(contenuto)
        tmp.close()

        with sqlite3.connect(tmp_path) as check_conn:
            integrity = check_conn.execute("PRAGMA integrity_check;").fetchone()[0]
            if integrity != "ok":
                raise HTTPException(status_code=400, detail="Il file di backup risulta corrotto.")

            tabelle = {
                row[0] for row in check_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not {"dipendenti", "assegnazioni"}.issubset(tabelle):
                raise HTTPException(
                    status_code=400,
                    detail="Il file non contiene le tabelle del turnario ('dipendenti' e 'assegnazioni')."
                )

        with sqlite3.connect(tmp_path) as src, sqlite3.connect(DB_PATH) as dst:
            src.backup(dst)

        init_db()

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Errore durante il ripristino: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"status": "ok"}


@app.get("/api/turnario/{anno}/{mese}")
def get_turnario_mensile(anno: int, mese: int):
    if not (1 <= mese <= 12):
        raise HTTPException(status_code=400, detail="Mese non valido")

    num_giorni = calendar.monthrange(anno, mese)[1]
    giorni_info = [calcola_turno_giorno(date(anno, mese, g)) for g in range(1, num_giorni + 1)]

    data_inizio = f"{anno:04d}-{mese:02d}-01"
    data_fine = f"{anno:04d}-{mese:02d}-{num_giorni:02d}"

    with get_db() as conn:
        dipendenti = [
            dict(row) for row in conn.execute(
                """
                SELECT id, nome, salto, qualifica, responsabile
                FROM dipendenti
                WHERE attivo = 1
                ORDER BY qualifica ASC, responsabile DESC, nome ASC
                """
            ).fetchall()
        ]
        assegnazioni_rows = conn.execute(
            "SELECT dipendente_id, data, mansione FROM assegnazioni WHERE data BETWEEN ? AND ?",
            (data_inizio, data_fine)
        ).fetchall()

        annual_rows = conn.execute(
            """
            SELECT dipendente_id,
                   SUM(CASE WHEN mansione IN ('T', 'TN', 'UCL') THEN 1 ELSE 0 END) as tot_turni,
                   SUM(CASE WHEN mansione = 'TN' THEN 1 ELSE 0 END) as tot_tn
            FROM assegnazioni
            WHERE data BETWEEN ? AND ?
            GROUP BY dipendente_id
            """,
            (f"{anno:04d}-01-01", data_fine)
        ).fetchall()

    assegnazioni_map = {
        f"{r['dipendente_id']}_{r['data']}": r["mansione"]
        for r in assegnazioni_rows
    }
    tn_annuali_map = {str(r["dipendente_id"]): r["tot_tn"] or 0 for r in annual_rows}
    tot_annuali_map = {str(r["dipendente_id"]): r["tot_turni"] or 0 for r in annual_rows}

    return {
        "anno": anno,
        "mese": mese,
        "giorni": giorni_info,
        "dipendenti": dipendenti,
        "assegnazioni": assegnazioni_map,
        "tn_annuali": tn_annuali_map,
        "tot_annuali": tot_annuali_map,
    }


@app.post("/api/autocompila/{anno}")
def autocompila_anno(anno: int):
    with get_db() as conn:
        inseriti = auto_compila_periodo(conn, anno, mese=None)
    return {"status": "ok", "inseriti": inseriti}


@app.post("/api/autocompila/{anno}/{mese}")
def autocompila_mese(anno: int, mese: int):
    if not (1 <= mese <= 12):
        raise HTTPException(status_code=400, detail="Mese non valido")
    with get_db() as conn:
        inseriti = auto_compila_periodo(conn, anno, mese=mese)
    return {"status": "ok", "inseriti": inseriti}


@app.delete("/api/turnario/{anno}/{mese}")
def svuota_turnario_mensile(anno: int, mese: int, solo_turni: bool = False):
    if not (1 <= mese <= 12):
        raise HTTPException(status_code=400, detail="Mese non valido")

    num_giorni = calendar.monthrange(anno, mese)[1]
    data_inizio = f"{anno:04d}-{mese:02d}-01"
    data_fine = f"{anno:04d}-{mese:02d}-{num_giorni:02d}"

    with get_db() as conn:
        if solo_turni:
            cur = conn.execute(
                "DELETE FROM assegnazioni WHERE data BETWEEN ? AND ? AND mansione IN ('T', 'TN', 'UCL')",
                (data_inizio, data_fine)
            )
        else:
            cur = conn.execute(
                "DELETE FROM assegnazioni WHERE data BETWEEN ? AND ?",
                (data_inizio, data_fine)
            )
    return {"status": "ok", "eliminati": cur.rowcount}


@app.post("/api/assegna")
def salva_assegnazione(payload: AssegnazioneInput):
    try:
        d = date.fromisoformat(payload.data)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato data non valido")

    info_giorno = calcola_turno_giorno(d)

    if payload.mansione == "TN" and info_giorno["tipo_sez_d"] != "N":
        raise HTTPException(
            status_code=400,
            detail="Il turno notturno (TN) può essere assegnato solo nei giorni con turno Notturno (N)."
        )

    with get_db() as conn:
        dip = conn.execute(
            "SELECT id, salto FROM dipendenti WHERE id = ?", (payload.dipendente_id,)
        ).fetchone()
        if not dip:
            raise HTTPException(status_code=404, detail="Dipendente non trovato")

        if payload.mansione == "L" and info_giorno["turno_sez_d"] == dip["salto"]:
            raise HTTPException(
                status_code=400,
                detail="Impossibile inserire Licenza (L) in un giorno di Riposo Compensativo (R)."
            )

        if not payload.mansione:
            conn.execute(
                "DELETE FROM assegnazioni WHERE dipendente_id = ? AND data = ?",
                (payload.dipendente_id, payload.data)
            )
        else:
            conn.execute("""
                INSERT INTO assegnazioni (dipendente_id, data, mansione)
                VALUES (?, ?, ?)
                ON CONFLICT(dipendente_id, data) DO UPDATE SET mansione = excluded.mansione
            """, (payload.dipendente_id, payload.data, payload.mansione))

    return {"status": "ok"}


@app.get("/api/dipendenti")
def lista_dipendenti(anno: int = 2027, mese: Optional[int] = None):
    with get_db() as conn:
        dipendenti = [
            dict(row) for row in conn.execute(
                """
                SELECT id, nome, salto, qualifica, responsabile, attivo
                FROM dipendenti
                ORDER BY attivo DESC, qualifica ASC, responsabile DESC, nome ASC
                """
            ).fetchall()
        ]

        if mese:
            num_giorni = calendar.monthrange(anno, mese)[1]
            start_d, end_d = f"{anno:04d}-{mese:02d}-01", f"{anno:04d}-{mese:02d}-{num_giorni:02d}"
        else:
            start_d, end_d = f"{anno:04d}-01-01", f"{anno:04d}-12-31"

        stats_rows = conn.execute("""
            SELECT dipendente_id, mansione, COUNT(*) as totale
            FROM assegnazioni
            WHERE data BETWEEN ? AND ?
              AND mansione IN ('T', 'TN', 'UCL')
            GROUP BY dipendente_id, mansione
        """, (start_d, end_d)).fetchall()

    stats = {}
    for r in stats_rows:
        did = r["dipendente_id"]
        stats.setdefault(did, {"T": 0, "TN": 0, "UCL": 0})
        stats[did][r["mansione"]] = r["totale"]

    for d in dipendenti:
        s = stats.get(d["id"], {"T": 0, "TN": 0, "UCL": 0})
        d["conteggio_turni"] = s["T"] + s["TN"] + s["UCL"]
        d["conteggio_t"] = s["T"]
        d["conteggio_tn"] = s["TN"]
        d["conteggio_ucl"] = s["UCL"]

    return dipendenti


@app.post("/api/dipendenti")
def crea_dipendente(payload: DipendenteCreate):
    nome_pulito = payload.nome.strip()
    if not nome_pulito:
        raise HTTPException(status_code=400, detail="Nome obbligatorio")
    is_resp = 1 if (payload.qualifica == "Q" and payload.responsabile) else 0
    try:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO dipendenti (nome, salto, qualifica, responsabile) VALUES (?, ?, ?, ?)",
                (nome_pulito, payload.salto, payload.qualifica, is_resp)
            )
            return {
                "id": cur.lastrowid,
                "nome": nome_pulito,
                "salto": payload.salto,
                "qualifica": payload.qualifica,
                "responsabile": is_resp,
                "attivo": 1
            }
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Dipendente già presente")


@app.patch("/api/dipendenti/{dipendente_id}")
def aggiorna_dipendente(dipendente_id: int, payload: DipendenteUpdate):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM dipendenti WHERE id = ?", (dipendente_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dipendente non trovato")

        nuovo_nome = payload.nome.strip() if payload.nome is not None else row["nome"]
        nuovo_salto = payload.salto if payload.salto is not None else row["salto"]
        nuova_qualifica = payload.qualifica if payload.qualifica is not None else row["qualifica"]
        nuovo_attivo = (1 if payload.attivo else 0) if payload.attivo is not None else row["attivo"]

        if payload.responsabile is not None:
            nuovo_resp = 1 if (nuova_qualifica == "Q" and payload.responsabile) else 0
        else:
            nuovo_resp = row["responsabile"] if nuova_qualifica == "Q" else 0

        conn.execute(
            "UPDATE dipendenti SET nome = ?, salto = ?, qualifica = ?, responsabile = ?, attivo = ? WHERE id = ?",
            (nuovo_nome, nuovo_salto, nuova_qualifica, nuovo_resp, nuovo_attivo, dipendente_id)
        )
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def serve_index():
    return FileResponse(STATIC_DIR / "index.html")