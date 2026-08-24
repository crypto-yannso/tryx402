import sqlite3
import secrets
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

def get_db(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str):
    conn = get_db(db_path)
    cursor = conn.cursor()
    # Provider tools registry (onboarding clients fournisseurs)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tools (
            id TEXT PRIMARY KEY,
            provider_name TEXT NOT NULL,
            provider_email TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            origin TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            method TEXT DEFAULT 'POST',
            description TEXT DEFAULT '',
            price_usd REAL NOT NULL,
            payout_eur_per_call REAL NOT NULL,
            is_active INTEGER DEFAULT 0,
            verified_at REAL,
            last_check_status INTEGER,
            created_at REAL NOT NULL
        )
    """)
    # Provider payouts (reversements dus/payés par appel réussi)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payouts (
            id TEXT PRIMARY KEY,
            tool_id TEXT NOT NULL,
            provider_name TEXT NOT NULL,
            amount_eur REAL NOT NULL,
            tx_id TEXT,
            status TEXT DEFAULT 'due',
            paid_reference TEXT,
            settled_at REAL,
            created_at REAL NOT NULL
        )
    """)
    # Tokens d'accès fournisseur (portail /provider)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS provider_tokens (
            token TEXT PRIMARY KEY,
            provider_name TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            last_used_at REAL
        )
    """)
    conn.commit()
    conn.close()
    _migrate_db(db_path)

def _migrate_db(db_path: str):
    """Migrations légères : ALTER TABLE idempotents pour les bases existantes."""
    conn = get_db(db_path)
    cursor = conn.cursor()
    existing_cols = {r[1] for r in cursor.execute("PRAGMA table_info(tools)")}
    if "memory_mbytes" not in existing_cols:
        cursor.execute("ALTER TABLE tools ADD COLUMN memory_mbytes INTEGER")
    conn.commit()
    conn.close()

def register_tool(db_path: str, provider_name: str, provider_email: str, slug: str,
                  origin: str, endpoint: str, method: str,
                  description: str, price_usd: float,
                  payout_eur_per_call: float,
                  memory_mbytes: Optional[int] = None) -> Dict[str, Any]:
    conn = get_db(db_path)
    cursor = conn.cursor()
    tool_id = f"tool_{secrets.token_hex(6)}"
    now = time.time()
    try:
        cursor.execute("""
            INSERT INTO tools (id, provider_name, provider_email, slug, origin,
                endpoint, method, description, price_usd, payout_eur_per_call,
                memory_mbytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tool_id, provider_name, provider_email, slug, origin, endpoint,
              method.upper(), description, price_usd, payout_eur_per_call,
              memory_mbytes, now))
        conn.commit()
        return {"id": tool_id, "slug": slug, "status": "pending_verification"}
    except sqlite3.IntegrityError:
        raise ValueError(f"slug '{slug}' déjà pris")
    finally:
        conn.close()

def verify_tool(db_path: str, slug: str) -> Dict[str, Any]:
    conn = get_db(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE tools SET is_active = 1, verified_at = ?
        WHERE slug = ? AND last_check_status BETWEEN 200 AND 299
    """, (time.time(), slug))
    if cursor.rowcount == 0:
        conn.close()
        raise ValueError("outil introuvable ou dernier check non-2xx : lancez d'abord un healthcheck")
    conn.commit()
    conn.close()
    return {"slug": slug, "is_active": True}

def record_tool_check(db_path: str, slug: str, status_code: int):
    conn = get_db(db_path)
    cursor = conn.cursor()
    cursor.execute("UPDATE tools SET last_check_status = ? WHERE slug = ?", (status_code, slug))
    conn.commit()
    conn.close()

def list_tools(db_path: str, active_only: bool = True) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    cursor = conn.cursor()
    q = "SELECT * FROM tools" + (" WHERE is_active = 1" if active_only else "")
    cursor.execute(q)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def get_tool_by_slug(db_path: str, slug: str) -> Optional[Dict[str, Any]]:
    conn = get_db(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tools WHERE slug = ?", (slug,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_tool_memory_mbytes(db_path: str, slug: str) -> Optional[int]:
    """Mémoire recommandée du tool (Apify), None = défaut global."""
    tool = get_tool_by_slug(db_path, slug)
    return tool.get("memory_mbytes") if tool else None

def record_payout(db_path: str, tool_id: str, provider_name: str, amount_eur: float, tx_id: Optional[str]):
    conn = get_db(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO payouts (id, tool_id, provider_name, amount_eur, tx_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'due', ?)
    """, (f"pay_{secrets.token_hex(6)}", tool_id, provider_name, round(amount_eur, 4), tx_id, time.time()))
    conn.commit()
    conn.close()

def provider_payout_summary(db_path: str, provider_name: str) -> Dict[str, Any]:
    conn = get_db(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT tool_id, status, SUM(amount_eur) as total_eur, COUNT(*) as calls
        FROM payouts WHERE provider_name = ?
        GROUP BY tool_id, status
    """, (provider_name,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    due = round(sum(r["total_eur"] for r in rows if r["status"] == "due"), 2)
    paid = round(sum(r["total_eur"] for r in rows if r["status"] == "paid"), 2)
    return {"provider": provider_name, "due_eur": due, "paid_eur": paid, "by_tool": rows}

def settle_provider(db_path: str, provider_name: str, reference: str = "") -> Dict[str, Any]:
    conn = get_db(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount_eur) FROM payouts WHERE provider_name = ? AND status = 'due'", (provider_name,))
    row = cursor.fetchone()
    total = row[0] if row and row[0] else 0.0
    if total == 0:
        conn.close()
        raise ValueError("aucun reversement en attente pour ce fournisseur")
    cursor.execute("""
        UPDATE payouts SET status = 'paid', paid_reference = ?, settled_at = ?
        WHERE provider_name = ? AND status = 'due'
    """, (reference or f"wire_{secrets.token_hex(4)}", time.time(), provider_name))
    conn.commit()
    conn.close()
    return {"provider": provider_name, "settled_eur": round(total, 2)}

def issue_provider_token(db_path: str, provider_name: str) -> str:
    conn = get_db(db_path)
    cursor = conn.cursor()
    token = f"pt_{secrets.token_urlsafe(24)}"
    now = time.time()
    cursor.execute("DELETE FROM provider_tokens WHERE provider_name = ?", (provider_name,))
    cursor.execute(
        "INSERT INTO provider_tokens (token, provider_name, created_at) VALUES (?, ?, ?)",
        (token, provider_name, now))
    conn.commit()
    conn.close()
    return token

def auth_provider(db_path: str, x_provider_token: Optional[str], provider_name: str) -> bool:
    if not x_provider_token:
        return False
    conn = get_db(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT provider_name FROM provider_tokens WHERE token = ?",
        (x_provider_token,))
    row = cursor.fetchone()
    if row and row["provider_name"] == provider_name:
        cursor.execute("UPDATE provider_tokens SET last_used_at = ? WHERE token = ?",
                       (time.time(), x_provider_token))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False
